# -*- coding: utf-8 -*-
"""@ 成员诊断（UIA 路径）

微信的成员弹层**只由真实按键触发**：库里的 `at_member` 用
`type_unicode('@')` 注入 Unicode 字符，弹层不会出现，OCR 自然找不到成员。
本工具验证新的 UIA 路径：真实按键敲 '@' → 从 UIA 树读成员列表 → 匹配 → 点选。

默认 **dry-run**：只把 @提及 插进输入框并校验，不发出去。
真发请加 --send（会往群里发一条测试消息）。

用法：
    python tools/verify_at.py                    # dry-run，用配置里监听的群
    python tools/verify_at.py "某群名"
    python tools/verify_at.py "某群名" --send "链路测试"
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT, bootstrap, cache_dir, self_wxid  # noqa: E402

bootstrap()

FAILS = []


def check(name, cond, extra=""):
    print("  [%s] %s%s" % ("√" if cond else "×", name,
                           ("  → %s" % extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    args = [a for a in sys.argv[1:]]
    do_send = "--send" in args
    rest = [a for a in args if not a.startswith("--")]
    group = rest[0] if rest else None
    text = rest[1] if len(rest) > 1 else "【链路测试】@ 提及验证"

    cfg = {}
    try:
        with open(os.path.join(ROOT, "wx_ai_bot_config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        pass
    if not group:
        tg = cfg.get("targets") or []
        group = tg[0] if tg else None

    print("=" * 78)
    print("@ 成员诊断（UIA 路径）     模式=%s" % ("真发" if do_send else "dry-run"))
    print("=" * 78)

    from wechatauto.db import WeChatDB
    from wechatauto.guia import WeChatGUI
    import wx_ai_bot

    # ---- 1) 库里能查到哪些成员、候选名字是什么 ----
    try:
        rdb = WeChatDB(workdir=cache_dir())
    except Exception as exc:
        print("[×] 无法打开微信数据库：%s" % exc)
        print("    请先登录微信 4.x（数据库密钥只存在于已登录进程的内存里）再运行本诊断。")
        return 1

    if not group:                       # 未指定：先用配置的监听目标，再退到自动挑
        tg = cfg.get("targets") or []
        group = tg[0] if tg else None
    chatroom = group if (group and group.endswith("@chatroom")) else None
    gname = group
    if not chatroom:
        for s in rdb.get_sessions(limit=300):
            u = s.get("username") or ""
            if not u.endswith("@chatroom"):
                continue
            try:
                n = rdb.group_id_to_name(u) or u
            except Exception:
                n = u
            if group and (n == group or group in n):
                chatroom, gname = u, n
                break
    if not chatroom:                    # 找不到就自动挑第一个群，便于首次使用
        for s in rdb.get_sessions(limit=300):
            u = s.get("username") or ""
            if u.endswith("@chatroom"):
                chatroom = u
                try:
                    gname = rdb.group_id_to_name(u) or u
                except Exception:
                    gname = u
                break
    if not chatroom:
        print("[×] 本地库里没有任何群聊，无法做 @ 诊断")
        return 1
    print("目标群: %s   [%s]" % (gname, chatroom))

    roster = {}
    if chatroom:
        conn = rdb._contact_conn()
        try:
            row = conn.execute("SELECT ext_buffer FROM chat_room WHERE username=? LIMIT 1",
                               (chatroom,)).fetchone()
            if row is not None:
                roster = wx_ai_bot.parse_group_members(row["ext_buffer"])
        finally:
            conn.close()

    me = self_wxid(rdb)          # 当前账号，用于把自己从候选里排除
    members = []
    print("\n--- 库里的群成员与候选名字 ---")
    for wxid, nick in roster.items():
        gn = (nick or "").strip()
        glob = rdb.get_nickname(wxid)
        hits = []
        try:
            hits = rdb.search_contact(wxid) or []
        except Exception:
            pass
        remark = (hits[0].get("remark") if hits else "") or ""
        nname = (hits[0].get("nick_name") if hits else "") or ""
        cands = [c for c in dict.fromkeys([gn, glob, remark, nname]) if c]
        is_me = bool(me) and wxid.startswith(me)
        tag = " ← 机器人自己" if is_me else ""
        print("  %-26s 群昵称=%-12r 备注=%-12r 昵称=%-12r%s"
              % (wxid, gn, remark, nname, tag))
        print("        候选匹配名: %s" % cands)
        if not is_me:
            members.append((wxid, cands))
    check("库里读到群成员（%d 人，排除机器人自己后 %d 人）"
          % (len(roster), len(members)), bool(members))

    # ---- 2) 真实 UIA 操作 ----
    print("\n" + "=" * 78)
    print("UIA 路径验证：真实按键敲 '@' → 读 UIA 成员列表 → 匹配 → 点选")
    print("=" * 78)
    try:
        gui = WeChatGUI()
    except RuntimeError as exc:
        print("[!] %s" % exc)
        print("正在尝试恢复微信窗口…")
        if wx_ai_bot.restore_wechat_window():
            gui = WeChatGUI()
        else:
            print("[×] 微信主窗口不可见且无法恢复。请把微信窗口打开（不要最小化）后重试。")
            return 1
    print("已连接微信窗口 hwnd=%s" % gui.main_hwnd)

    ok_any = False
    for wxid, cands in members:
        print("\n--- 尝试 @ %s（候选 %s）---" % (wxid, cands))
        r = wx_ai_bot.uia_mention_send(
            gui, cands, text, who=gname, dry_run=not do_send)
        for k in ("ok", "stage", "matched", "detail"):
            print("    %-8s = %r" % (k, r.get(k)))
        if do_send and r.get("ok"):
            print("    → 已真实发送（含 @提及）")
        ok_any = ok_any or bool(r.get("ok"))
        time.sleep(0.8)

    check("至少有一个成员能成功定位并插入 @提及", ok_any)
    try:
        wx_ai_bot.clear_leftover_input(gui)
    except Exception:
        pass

    print("\n" + "=" * 78)
    if FAILS:
        print("有 %d 项未通过：%s" % (len(FAILS), "，".join(FAILS)))
        print("\n排查提示：")
        print("  · 「成员弹层未出现」→ 输入框没真正聚焦；确认微信窗口可见、未锁屏")
        print("  · 「未匹配到成员」→ 弹层候选名与库里的名字不一致，按上面打印的候选调整")
    else:
        print("结论：@ 提及链路可用%s" % ("" if do_send else "（dry-run，未发送）"))
    print("=" * 78)
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(1)
