# -*- coding: utf-8 -*-
"""新功能验证：群昵称 / 人物规则 / 群聊隔离 / 提示词预设

分两部分：
  [真实数据] 用本机微信库验证群昵称解析与 @我 检测（自动挑选测试对象）
  [合成数据] 用注入花名册验证人物规则、隔离、冷却分键（无需微信在线）

用法：
    python tools/verify_features.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import (bootstrap, cache_dir, find_test_group,  # noqa: E402
                        self_wxid)

bootstrap()

from wx_ai_bot import (BotEngine, DEFAULT_PROMPT_PRESETS, parse_group_members,
                       split_group_sender)
from wechatauto.db import WeChatDB

FAILS = []


def check(name, cond, extra=""):
    print("  [%s] %s%s" % ("√" if cond else "×", name,
                           ("  → %s" % extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ===========================================================================
print("=" * 78)
print("[一] 真实数据：群昵称（群名片）解析")
print("=" * 78)


def run_real_data_tests():
    """用本机微信库验证群昵称 / @我 检测 / _sender_label。

    自动挑选测试对象（公开仓库不能写死某个人的群与 wxid）；
    库里没有可用的群、或微信未登录时整体跳过，不影响合成数据部分。
    """
    try:
        db = WeChatDB(workdir=cache_dir())
    except Exception as exc:
        print("  [跳过] 无法打开微信库（微信未登录/未运行？）：%s" % exc)
        return

    # 自动挑一个「有群昵称」的群
    try:
        GROUP, GNAME, roster, NAMED = find_test_group(db, need=1)
    except Exception as exc:
        print("  [跳过] 读取群花名册失败：%s" % exc)
        return
    if not GROUP or not NAMED:
        print("  [跳过] 本地库里没有「设了群昵称」的群")
        print("        要跑这部分：先在微信里给某个群成员设置群名片")
        return
    named = {k: v for k, v in roster.items() if v}
    WX = NAMED[0][0]
    print("  测试群: %s   [%s]" % (GNAME, GROUP))
    check("解析出成员花名册（%d 人）" % len(roster), len(roster) >= 1)
    check("其中设了群名片的有 %d 人" % len(named), len(named) >= 1)

    diff = [k for k, v in named.items() if db.get_nickname(k) != v]
    check("群昵称与全局昵称不同的有 %d 人（证明必须用群昵称）" % len(diff),
          len(diff) >= 1)

    eng = BotEngine({"person_rules": []}, lambda k, p: None, db=db)
    eng.self_wxid = self_wxid(db)
    _info = db.get_self_info() or {}
    eng.self_names = {n for n in (_info.get("nick_name"), _info.get("remark")) if n}

    expect_group_nick = named[WX]
    expect_global = db.get_nickname(WX)
    got = eng._group_display(WX, GROUP)
    check("_group_display 返回群昵称 %r（而不是全局 %r）"
          % (expect_group_nick, expect_global),
          got == expect_group_nick, got)

    # 「@我」必须认得**机器人在该群的显示名**（群里 @ 出来的是这个）
    my_disp = eng._group_display(eng.self_wxid, GROUP)
    my_names = eng._my_names_in(GROUP)
    check("机器人在该群的显示名 = %r" % my_disp, my_disp in my_names)
    check("@我 检测认得自己的群显示名（%r）" % my_disp,
          eng._mentioned_me("@%s 你好" % my_disp, GROUP))
    for n in eng.self_names:
        check("@我 检测认得全局昵称 %r" % n,
              eng._mentioned_me("@%s 你好" % n, GROUP))
    check("无关 @ 不误判（@别的成员不算叫我）",
          not eng._mentioned_me("@%s 你好" % expect_group_nick, GROUP))
    check("完全无 @ 不误判", not eng._mentioned_me("你好", GROUP))

    # 另一个群：验证「没设群名片时回落全局昵称」
    other = None
    for cand in list_groups_with_nicknames(db, limit=200):
        if cand[0] != GROUP:
            other = cand
            break
    if other:
        g2, _g2name, roster2 = other
        no_nick = [k for k, v in roster2.items() if not v]
        if no_nick:
            w = no_nick[0]
            g2_disp = eng._group_display(w, g2)
            check("无群名片时回落全局昵称（%s…）" % w[:14],
                  g2_disp == eng._name_of(w), g2_disp)

    # ---- 集成：用真实群消息跑完整的 _sender_label 解析 ----
    print("\n-- 集成验证：真实群消息 → _sender_label（界面与人物规则用的就是它）--")
    chats = [GROUP] + ([other[0]] if other else [])
    msgs = []
    for g in chats:
        try:
            msgs += [(g, m) for m in db.get_messages(g, limit=25)
                     if m.get("sender_id") != 2 and m.get("type") == "文本"]
        except Exception as exc:
            print("    读取 %s 消息失败：%s" % (g, exc))
    if not msgs:
        print("    [跳过] 这些群里没有近期文本消息")
        return
    check("读到真实群消息（%d 条）" % len(msgs), len(msgs) >= 1)

    numeric_labels = hit_nick = fallback = 0
    samples = []
    for g, m in msgs:
        raw = (m.get("content") or "").strip()
        pw, _clean = split_group_sender(raw)
        label = eng._sender_label(m, g, True, pw)
        if label == "群成员":
            continue
        wxid = eng._sender_wxid(m, pw, True, g)
        nick = {k: v for k, v in eng._group_roster(g).items() if v}
        if label.isdigit():
            numeric_labels += 1
        elif wxid and nick.get(wxid) == label:
            hit_nick += 1
        elif wxid and eng._name_of(wxid) == label:
            fallback += 1
        if len(samples) < 6:
            samples.append((g, wxid, label, raw[:24].replace("\n", " ")))

    check("没有任何消息被解析成数字 ID（旧 bug）", numeric_labels == 0,
          "有 %d 条" % numeric_labels)
    check("结果都来自群昵称或全局昵称（命中群昵称 %d / 回落 %d）"
          % (hit_nick, fallback), (hit_nick + fallback) > 0)
    if samples:
        print("\n    样例（群 → wxid → 显示名 → 正文）：")
        for g, wxid, label, body in samples:
            print("      %-18s %-22s %-12s %s"
                  % (eng._name_of(g)[:18], (wxid or "-")[:22], label[:12], body))


run_real_data_tests()

# ===========================================================================
print("\n" + "=" * 78)
print("[二] 合成数据：人物规则 / 群聊隔离 / 冷却分键")
print("=" * 78)


class StubDB:
    """受控的假数据库：只提供花名册与全局昵称。"""

    def __init__(self, names):
        self.names = names

    def _contact_conn(self):
        return None

    def get_nickname(self, u):
        return self.names.get(u, u)

    def group_id_to_name(self, u):
        return self.names.get(u, u)


GRP_A = "111@chatroom"
GRP_B = "222@chatroom"
NAMES = {
    GRP_A: "群A", GRP_B: "群B",
    "wxid_alice1": "Alice全名", "wxid_bob222": "Bob全名",
    "wxid_carol3": "Carol全名", "wxid_me00000": "小助手全名",
    "wxid_priv111": "私聊对象",
}
ROSTER_A = {"wxid_alice1": "爱丽丝", "wxid_bob222": "",      # Bob 没设群名片
            "wxid_carol3": "卡罗", "wxid_me00000": "群A小助手"}
ROSTER_B = {"wxid_alice1": "A同学", "wxid_bob222": "B同学",
            "wxid_me00000": ""}


def new_engine(cfg_extra=None):
    cfg = {"person_rules": [], "context_isolation": "chat",
           "cooldown_per_person": True, "cooldown": 3.0}
    if cfg_extra:
        cfg.update(cfg_extra)
    e = BotEngine(cfg, lambda k, p: None, db=StubDB(NAMES))
    e.self_wxid = "wxid_me00000"
    e.self_names = {"小助手全名"}
    now = time.time()
    e._roster_cache[GRP_A] = (now, dict(ROSTER_A))
    e._roster_cache[GRP_B] = (now, dict(ROSTER_B))
    return e


e = new_engine()
check("群A 里显示群昵称", e._group_display("wxid_alice1", GRP_A) == "爱丽丝")
check("群B 里同一个人显示另一个群昵称（群间不串）",
      e._group_display("wxid_alice1", GRP_B) == "A同学")
check("同一个人在不同群 → 不同显示名",
      e._group_display("wxid_alice1", GRP_A) != e._group_display("wxid_alice1", GRP_B))
check("无群名片者回落全局昵称",
      e._group_display("wxid_bob222", GRP_A) == "Bob全名",
      e._group_display("wxid_bob222", GRP_A))

print("\n-- @我 检测（机器人在群A的群昵称是「群A小助手」）--")
check("认得群A的群昵称", e._mentioned_me("@群A小助手 在吗", GRP_A))
check("认得全局昵称", e._mentioned_me("@小助手全名 在吗", GRP_A))
check("在群B里「群A小助手」不算叫我", not e._mentioned_me("@群A小助手 在吗", GRP_B))
check("群B里认得全局昵称（群B无群名片）",
      e._mentioned_me("@小助手全名 在吗", GRP_B))

print("\n-- 人物识别（按群昵称匹配）--")
e2 = new_engine({"person_rules": [
    {"who": "爱丽丝", "scope": "*", "action": "ignore"},
]})
r = e2._match_person_rule(GRP_A, "wxid_alice1", "爱丽丝")
check("按群昵称「爱丽丝」命中规则", r is not None and r["action"] == "ignore")
r = e2._match_person_rule(GRP_B, "wxid_alice1", "A同学")
check("同一人在别的群（群昵称不同）不命中", r is None)

e3 = new_engine({"person_rules": [
    {"who": "Bob全名", "scope": "*", "action": "ignore"},
]})
r = e3._match_person_rule(GRP_A, "wxid_bob222", "Bob全名")
check("按全局昵称命中（无群名片者）", r is not None)
r = e3._match_person_rule(GRP_A, "wxid_alice1", "爱丽丝")
check("不匹配的人不命中", r is None)

e4 = new_engine({"person_rules": [
    {"who": "wxid_carol3", "scope": "group", "action": "keyword"},
    {"who": "*", "scope": "*", "action": "reply"},
]})
r = e4._match_person_rule(GRP_A, "wxid_carol3", "卡罗")
check("按 wxid 命中 + 范围 group 命中", r is not None and r["action"] == "keyword")
r = e4._match_person_rule("wxid_priv111", "wxid_priv111", "私聊对象")
check("范围 group 在私聊不生效，落到「*」规则", r is not None and r["action"] == "reply")

e5 = new_engine({"person_rules": [
    {"who": "A同学", "scope": GRP_B, "action": "ignore"},
]})
check("指定群范围（直接填群 wxid）：目标群命中",
      e5._match_person_rule(GRP_B, "wxid_alice1", "A同学") is not None)
check("指定群范围：其他群不命中",
      e5._match_person_rule(GRP_A, "wxid_alice1", "A同学") is None)
e5b = new_engine({"person_rules": [
    {"who": "爱丽丝", "scope": "group:" + GRP_A, "action": "ignore"},
]})
check("指定群范围（group: 前缀）：目标群命中",
      e5b._match_person_rule(GRP_A, "wxid_alice1", "爱丽丝") is not None)
check("指定群范围（group: 前缀）：其他群不命中",
      e5b._match_person_rule(GRP_B, "wxid_alice1", "爱丽丝") is None)
e5c = new_engine({"person_rules": [
    {"who": "爱丽丝", "scope": "群A", "action": "ignore"},      # 按群名指定
]})
check("指定群范围（填群名）：目标群命中",
      e5c._match_person_rule(GRP_A, "wxid_alice1", "爱丽丝") is not None)
check("指定群范围（填群名）：其他群不命中",
      e5c._match_person_rule(GRP_B, "wxid_alice1", "爱丽丝") is None)

e6 = new_engine({"person_rules": [
    {"who": "Alice全名, 卡罗", "scope": "*", "action": "ignore"},
]})
check("who 支持逗号分隔多值",
      e6._match_person_rule(GRP_A, "wxid_alice1", "爱丽丝") is not None
      and e6._match_person_rule(GRP_A, "wxid_carol3", "卡罗") is not None)

print("\n-- 群聊隔离（上下文键）--")
e7 = new_engine({"context_isolation": "chat"})
k = [e7._context_key(g, w, True) for g, w in
     ((GRP_A, "wxid_alice1"), (GRP_B, "wxid_alice1"),
      (GRP_A, "wxid_bob222"), (GRP_B, "wxid_carol3"))]
check("默认：不同群之间键不同", k[0] != k[1])
check("默认：同群不同人共享同一上下文（群内共享）", k[0] == k[2])
check("默认：私聊独立", e7._context_key("wxid_priv111", "wxid_priv111", False)
      == "wxid_priv111")

e8 = new_engine({"context_isolation": "person"})
kp = [e8._context_key(g, w, True) for g, w in
      ((GRP_A, "wxid_alice1"), (GRP_A, "wxid_bob222"), (GRP_B, "wxid_alice1"))]
check("按人隔离：同群不同人键不同", kp[0] != kp[1])
check("按人隔离：不同群键不同", kp[0] != kp[2])

print("\n-- 冷却分键 --")
e9 = new_engine({"cooldown_per_person": True})
c = [e9._cooldown_key(g, w, True) for g, w in
     ((GRP_A, "wxid_alice1"), (GRP_A, "wxid_bob222"), (GRP_B, "wxid_alice1"))]
check("按人冷却：同群不同人分开计时（B 不被 A 连坐）", c[0] != c[1])
check("按人冷却：不同群分开", c[0] != c[2])
e10 = new_engine({"cooldown_per_person": False})
check("关闭时按会话统一计时",
      e10._cooldown_key(GRP_A, "wxid_alice1", True)
      == e10._cooldown_key(GRP_A, "wxid_bob222", True))

print("\n-- 离开规则时不应越界 --")
e11 = new_engine({"person_rules": [{"who": "", "scope": "*", "action": "ignore"}]})
check("who 为空不命中", e11._match_person_rule(GRP_A, "wxid_alice1", "爱丽丝") is None)
e12 = new_engine({"person_rules": "不是列表"})
check("person_rules 类型错误不崩", e12._match_person_rule(GRP_A, "x", "y") is None)

# ===========================================================================
print("\n" + "=" * 78)
print("[三] 提示词预设")
print("=" * 78)
check("内置预设数量 >= 5（%d）" % len(DEFAULT_PROMPT_PRESETS),
      len(DEFAULT_PROMPT_PRESETS) >= 5)
check("含「中性助手（默认）」", "中性助手（默认）" in DEFAULT_PROMPT_PRESETS)
for name, text in DEFAULT_PROMPT_PRESETS.items():
    check("预设「%s」非空且为字符串" % name, isinstance(text, str) and len(text) > 20)

print("\n" + "=" * 78)
if FAILS:
    print("结论：%d 项未通过：" % len(FAILS))
    for f in FAILS:
        print("   -", f)
else:
    print("结论：全部通过")
print("=" * 78)
sys.exit(1 if FAILS else 0)
