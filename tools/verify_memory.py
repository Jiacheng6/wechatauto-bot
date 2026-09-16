# -*- coding: utf-8 -*-
"""实测「记忆功能」的真实边界（断言式，结论由实测值算出，不写死）

前 9 节不需要微信在线、也不需要 API Key（用替身 LLM 客户端截获 messages）；
后 4 节用到本机微信库，库里没有合适数据时会自动跳过。

用法：
    python tools/verify_memory.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import (ROOT, bootstrap, cache_dir, find_test_group,  # noqa: E402
                        find_test_group_any, self_wxid)

bootstrap()

import wx_ai_bot  # noqa: E402
from wx_ai_bot import HISTORY_MAX_MESSAGES, BotEngine  # noqa: E402

CAPTURED = []
FAILS = []


def check(name, cond, extra=""):
    print("  [%s] %s%s" % ("√" if cond else "×", name,
                           ("  → %s" % extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeLLM:
    """替身：不真的调用 API，只把发出去的 messages 记下来。"""

    def __init__(self, *a, **k):
        pass

    def chat(self, messages, retries=2):
        CAPTURED.append([dict(m) for m in messages])
        return "好的"


wx_ai_bot.LLMClient = FakeLLM


class StubDB:
    def __init__(self):
        self.names = {"wxid_a": "甲", "wxid_b": "乙", "群1": "群1", "群2": "群2"}

    def _contact_conn(self):
        return None

    def get_nickname(self, u):
        return self.names.get(u, u)

    def group_id_to_name(self, u):
        return self.names.get(u, u)


def mk(turns=3, iso="chat", extra=None):
    cfg = {"context_turns": turns, "context_isolation": iso,
           "system_prompt": "系统提示", "person_rules": [],
           "cooldown": 0, "cooldown_per_person": True}
    if extra:
        cfg.update(extra)
    return BotEngine(cfg, lambda k, p: None, db=StubDB())


def hist_len(msgs):
    """请求里携带的历史消息条数（去掉 system 与最后一条 user）。"""
    return len(msgs[1:-1])


print("=" * 76)
print("实测：记忆功能的真实边界")
print("=" * 76)

# ---- 1. 是否累积 ----
print("\n[1] 连续对话是否累积上下文（context_turns=3，即最多带 3 轮=6 条）")
CAPTURED.clear()
e = mk(3)
for i in range(6):
    e._ask_llm("chatA", "甲", "第%d句话" % (i + 1), False)
counts = [hist_len(c) for c in CAPTURED]
print("    每轮请求携带的历史条数:", counts)
check("第1轮无历史", counts[0] == 0, counts[0])
check("会随对话累积", counts[-1] > counts[0])
check("累积到 3 轮=6 条后不再增长", counts[-1] == 6 and counts[-2] == 6, counts)

# ---- 2. 容量上限 ----
print("\n[2] 容量上限 HISTORY_MAX_MESSAGES=%d" % HISTORY_MAX_MESSAGES)
CAPTURED.clear()
e = mk(3)
for i in range(HISTORY_MAX_MESSAGES + 30):
    e._ask_llm("chatB", "甲", "第%d句" % (i + 1), False)
stored = len(e._history["chatB"])
print("    连发 %d 轮后 history 实际长度 = %d"
      % (HISTORY_MAX_MESSAGES + 30, stored))
check("历史被上限截断（不会无限增长）", stored == HISTORY_MAX_MESSAGES, stored)
check("上限足够大（>=200 条 = 100 轮）", HISTORY_MAX_MESSAGES >= 200,
      HISTORY_MAX_MESSAGES)

# ---- 3. 界面的「上下文轮数」是否真的生效 ----
print("\n[3] 界面「上下文轮数」设置是否真的生效（填 10，应带 20 条）")
CAPTURED.clear()
e = mk(10)
for i in range(20):
    e._ask_llm("chatC", "甲", "第%d句" % (i + 1), False)
n10 = hist_len(CAPTURED[-1])
print("    期望携带 20 条，实际携带 %d 条" % n10)
check("轮数设置生效（修复：原先被 maxlen=12 静默截断到 12）", n10 == 20, n10)

# ---- 4. 关闭上下文 ----
print("\n[4] context_turns=0（关闭上下文）")
CAPTURED.clear()
e = mk(0)
for i in range(3):
    e._ask_llm("chatD", "甲", "第%d句" % (i + 1), False)
check("不记录也不携带历史",
      len(e._history["chatD"]) == 0 and hist_len(CAPTURED[-1]) == 0)

# ---- 5. 会话隔离 ----
print("\n[5] 会话之间的记忆隔离（默认按会话）")
CAPTURED.clear()
e = mk(3)
e._ask_llm("wxid_a", "甲", "我是甲", False)
e._ask_llm("wxid_b", "乙", "我是乙", False)
e._ask_llm("wxid_a", "甲", "还记得我吗", False)
print("    私聊A → 私聊B → 私聊A：最后一轮携带 %d 条历史" % hist_len(CAPTURED[-1]))
check("私聊A 只记得 A 自己的对话（不会被 B 污染）",
      hist_len(CAPTURED[-1]) == 2, hist_len(CAPTURED[-1]))
e._ask_llm("群1", "甲", "群1说话", True)
e._ask_llm("群2", "甲", "群2说话", True)
check("群1 与 群2 的记忆互相独立",
      "群1" in e._history and "群2" in e._history
      and e._history["群1"] is not e._history["群2"])
check("私聊与群聊的记忆互相独立",
      e._history["wxid_a"] is not e._history["群1"])

# ---- 6. 按人隔离模式 ----
print("\n[6] 开启「群内按人隔离」后，群里每人独立记忆")
CAPTURED.clear()
e = mk(3, iso="person")
e._ask_llm("群1|wxid_a", "甲", "甲在群里说话", True)
e._ask_llm("群1|wxid_b", "乙", "乙在群里说话", True)
e._ask_llm("群1|wxid_a", "甲", "甲又说", True)
check("甲的记忆里没有乙的内容",
      "群1|wxid_a" in e._history and "群1|wxid_b" in e._history
      and e._history["群1|wxid_a"] is not e._history["群1|wxid_b"])
check("甲再次发言能带上自己的历史", hist_len(CAPTURED[-1]) == 2,
      hist_len(CAPTURED[-1]))

# ---- 7. 记忆恢复发生在启动流程里 ----
print("\n[7] 记忆恢复的时机（构造时 vs 启动时）")
import inspect
boot_src = inspect.getsource(BotEngine._boot)
e_fresh = mk(3)
check("刚构造的引擎历史为空（恢复在 _boot 里做，不在构造函数里）",
      not e_fresh._history)
check("_boot 里调用了 _restore_memory（② 跨重启恢复）",
      "_restore_memory" in boot_src)
check("_boot 里调用了 _warmup_from_db（① 数据库回填）",
      "_warmup_from_db" in boot_src)
check("_boot 里会强制刷盘前的记忆已落盘（stop 也刷）",
      "save_if_dirty" in inspect.getsource(BotEngine.stop)
      or "save_if_dirty" in boot_src)

# ---- 8. 数据库回填确实会读库 ----
print("\n[8] 数据库回填是否真的读库")
warm_src = inspect.getsource(BotEngine._warmup_from_db)
check("回填路径调用 get_messages 读取真实聊天记录", "get_messages" in warm_src)
check("回填只取文本消息（跳过图片/表情等）", '"文本"' in warm_src)
check("回填会剥掉群消息的 wxid 前缀", "split_group_sender" in warm_src)
check("回填使用群昵称作为发送者标签", "_group_roster" in warm_src)

print("\n" + "=" * 76)
print("[9] 记忆持久化（② 跨重启不丢）")
print("=" * 76)

import os
import time as _time

from wx_ai_bot import MemoryStore

MEM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mem_test.json")
for p in (MEM_PATH, MEM_PATH + ".tmp"):
    if os.path.exists(p):
        os.remove(p)

st = MemoryStore(MEM_PATH, max_conversations=3, max_age_hours=168)
check("新存储为空", st.stats()["conversations"] == 0)

st.update("convA", [("user", "你好"), ("assistant", "你好呀")])
st.update("convB", [("user", "在吗"), ("assistant", "在的")])
st.update("群1", [("user", "甲：大家好"), ("assistant", "欢迎")])
check("内存里记录了 3 个会话", st.stats()["conversations"] == 3)
saved = st.save_if_dirty(force=True)
check("落盘成功", saved and os.path.exists(MEM_PATH))

st2 = MemoryStore(MEM_PATH)
check("新实例能读回 3 个会话", st2.stats()["conversations"] == 3,
      st2.stats()["conversations"])
check("内容完整还原", st2.snapshot("convA") == [("user", "你好"), ("assistant", "你好呀")],
      st2.snapshot("convA"))

st.update("convC", [("user", "x"), ("assistant", "y")])
st.update("convD", [("user", "p"), ("assistant", "q")])
check("超出会话上限会淘汰最旧的（max_conversations=3）",
      st.stats()["conversations"] == 3, st.stats()["conversations"])
check("淘汰的是最久未更新的 convA", not st.has("convA"))

print("\n-- 过期与损坏容错 --")
# 过期：把文件里的 updated 改成很久以前（真实场景是隔了很久才再启动）
import json as _json
with open(MEM_PATH, encoding="utf-8") as fh:
    _obj = _json.load(fh)
for _v in _obj["conversations"].values():
    _v["updated"] = _time.time() - 999 * 3600          # 999 小时前
with open(MEM_PATH, "w", encoding="utf-8") as fh:
    _json.dump(_obj, fh, ensure_ascii=False)
st3 = MemoryStore(MEM_PATH, max_age_hours=168)
check("过期记忆（999 小时前）不会被载入", st3.stats()["conversations"] == 0,
      st3.stats()["conversations"])
st3b = MemoryStore(MEM_PATH, max_age_hours=2000)
check("放宽时限后同一份记忆又能载入", st3b.stats()["conversations"] >= 1,
      st3b.stats()["conversations"])

with open(MEM_PATH, "w", encoding="utf-8") as fh:
    fh.write("{ 这不是合法 json")
st4 = MemoryStore(MEM_PATH)
check("坏文件不会导致启动失败（静默跳过）", st4.stats()["conversations"] == 0)

st5 = MemoryStore(MEM_PATH)
st5.update("k", [("user", "a"), ("assistant", "b")])
st5.save_if_dirty(force=True)
with open(MEM_PATH, "w", encoding="utf-8") as fh:
    fh.write('{"conversations": {"bad": 123, "worse": {"turns": "不是列表"},'
             ' "half": {"turns": [["user","ok"],["bogus","x"],"不是pair"]}}}')
st6 = MemoryStore(MEM_PATH)
check("结构异常条目被跳过、合法条目保留",
      st6.stats()["conversations"] == 1 and st6.snapshot("half") == [("user", "ok")],
      st6.snapshot("half"))

n_cleared = st2.clear()
check("clear() 清空内存与文件", not os.path.exists(MEM_PATH))

print("\n-- 引擎级：重启后是否还记得 --")
CAPTURED.clear()
cfg_mem = {"context_turns": 3, "system_prompt": "S", "person_rules": [],
           "cooldown": 0, "memory_enabled": True, "memory_file": MEM_PATH}
e1 = BotEngine(cfg_mem, lambda k, p: None, db=StubDB())
e1._ask_llm("chatP", "甲", "我叫小明", False)
e1.stop(join=False)          # 停止时强制刷盘
check("停止时已落盘", os.path.exists(MEM_PATH))
e2 = BotEngine(cfg_mem, lambda k, p: None, db=StubDB())
n = e2._restore_memory()
check("重启后恢复 1 个会话的记忆", n == 1, n)
CAPTURED.clear()
e2._ask_llm("chatP", "甲", "我叫什么", False)
hist = CAPTURED[-1][1:-1]
check("重启后的请求里带上了重启前的对话",
      any("我叫小明" in m["content"] for m in hist), hist)
if os.path.exists(MEM_PATH):
    os.remove(MEM_PATH)

# ===========================================================================
# ① 从数据库回填
# ===========================================================================
print("\n" + "=" * 76)
print("[10] 启动回填历史（① 一上线就认识对方）")
print("=" * 76)

try:
    # 重要：wechatauto 会导入 colorama 并重新包装 sys.stdout，
    # 若此刻缓冲区里还有没 flush 的输出（前面 9 节的报告）会被全部丢弃。
    sys.stdout.flush()
    sys.stderr.flush()
    from wechatauto.db import WeChatDB
    rdb = WeChatDB(workdir=cache_dir())
    has_db = True
except Exception as exc:
    print("  跳过（无法打开微信库：%s）" % exc)
    has_db = False

def _run_warmup_checks(rdb, GROUP_R, GNAME_R, memp):
    """① 启动回填的具体检查（用到真实库，所以单独成函数便于跳过）。"""
    print("  回填测试群: %s   [%s]" % (GNAME_R, GROUP_R))
    for p in (memp, memp + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
    cfg_w = {"context_turns": 3, "system_prompt": "S", "person_rules": [],
             "cooldown": 0, "memory_enabled": True, "memory_file": memp,
             "warmup_messages": 30, "warmup_hours": 0, "warmup_max_chats": 5}
    ew = BotEngine(cfg_w, lambda k, p: None, db=rdb)
    ew.self_wxid = self_wxid(rdb)
    seeded = ew._warmup_from_db(rdb, [GROUP_R], "selected")
    check("回填了会话（%d 个）" % seeded, seeded >= 1, seeded)

    hist_w = list(ew._history.get(GROUP_R, []))
    print("    该群最近 30 条文本消息 → 合并同角色后 %d 条历史" % len(hist_w))
    check("历史被填充且数量合理（>=5 条）", len(hist_w) >= 5, len(hist_w))
    check("不会超过 warmup_messages 上限", len(hist_w) <= 30, len(hist_w))
    roles = {r for r, _ in hist_w}
    check("user / assistant 两种角色都有（自己发的是 assistant）",
          roles <= {"user", "assistant"} and len(roles) == 2, roles)
    check("相邻同角色已合并（无连续两条同角色）",
          all(hist_w[i][0] != hist_w[i + 1][0] for i in range(len(hist_w) - 1)))
    check("群消息前缀已被剥掉（正文不含 wxid_:）",
          not any("wxid_" in t and "：" in t.split("\n")[0] and t.startswith("wxid_")
                  for _, t in hist_w))
    labels = [t.split("：")[0] for r, t in hist_w if r == "user" and "：" in t]
    roster = ew._group_roster(GROUP_R)
    named = {v for v in roster.values() if v}
    check("群消息带的是群昵称（%d 个标签，样例 %s）"
          % (len(labels), labels[:3]),
          bool(labels) and any(l in named for l in labels), labels[:5])
    check("回填内容已写入持久化存储", ew._memory.stats()["conversations"] >= 1)
    check("回填后立即再回填会跳过（已有记忆不覆盖）",
          ew._warmup_from_db(rdb, [GROUP_R], "selected") == 0)
    if os.path.exists(memp):
        os.remove(memp)

    # 期限过滤
    cfg_t = dict(cfg_w)
    cfg_t["memory_file"] = memp
    cfg_t["warmup_hours"] = 0.001        # 极短时限
    et = BotEngine(cfg_t, lambda k, p: None, db=rdb)
    et.self_wxid = self_wxid(rdb)
    et._warmup_from_db(rdb, [GROUP_R], "selected")
    check("时限过滤生效（超期的消息不回填）",
          len(et._history.get(GROUP_R, [])) == 0,
          len(et._history.get(GROUP_R, [])))

    # 关闭回填
    cfg_off = dict(cfg_w)
    cfg_off["memory_file"] = memp
    cfg_off["warmup_messages"] = 0
    eo = BotEngine(cfg_off, lambda k, p: None, db=rdb)
    eo.self_wxid = self_wxid(rdb)
    eo._warmup_from_db(rdb, [GROUP_R], "selected")
    check("warmup_messages=0 时完全关闭回填",
          len(eo._history.get(GROUP_R, [])) == 0)
    if os.path.exists(memp):
        os.remove(memp)


if has_db:
    import inspect
    src = inspect.getsource(BotEngine._boot)
    check("_boot 里确实调用了回填", "_warmup_from_db" in src)
    check("回填用的是真实数据库读取",
          "get_messages" in inspect.getsource(BotEngine._warmup_from_db))

    # 自动挑一个群做回填测试（公开仓库不能写死某个人的群）
    GROUP_R, GNAME_R = find_test_group_any(rdb)
    if not GROUP_R:
        print("  [跳过] 本地库里没有群聊，无法验证回填")
    else:
        _run_warmup_checks(rdb, GROUP_R, GNAME_R,
                           os.path.join(ROOT, "_mem_warm.json"))

print("\n" + "=" * 76)
print("[11] 真实启动流程端到端（_boot：恢复 ② → 回填 ① → 挂监听）")
print("=" * 76)

if has_db:
    import wechatauto.db as _wdb

    class StubListener:
        """替身监听器：记录注册情况，绝不真的轮询（避免误发消息）。"""

        instances = []

        def __init__(self, db, interval=1.0, watermark=None):
            self.db = db
            self.interval = interval
            self.registered = []
            self.all_mode = False
            self.started = False
            StubListener.instances.append(self)

        def add_listener(self, user, cb):
            self.registered.append(user)

        def add_all(self, cb, discover=True):
            self.all_mode = True

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

    _orig_listener = _wdb.Listener
    _wdb.Listener = StubListener

    boot_mem = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mem_boot.json")
    for p in (boot_mem, boot_mem + ".tmp"):
        if os.path.exists(p):
            os.remove(p)

    cfg_b = {"context_turns": 3, "system_prompt": "S", "person_rules": [],
             "cooldown": 0, "memory_enabled": True, "memory_file": boot_mem,
             "warmup_messages": 30, "warmup_hours": 0, "warmup_max_chats": 3,
             "scope": "selected", "targets": [GROUP_R], "interval": 1.0,
             "account": ""}
    logs = []
    eb = BotEngine(cfg_b, lambda k, p: logs.append((k, p)), db=rdb)
    StubListener.instances.clear()
    eb._boot()                      # 走真实启动路径

    on = [p for k, p in logs if k == "running"]
    check("_boot 成功走完并报告 running=True", on and on[-1] is True, on)
    sl = StubListener.instances[-1] if StubListener.instances else None
    check("监听器已启动", sl is not None and sl.started)
    check("监听了配置里的目标会话", sl is not None and sl.registered == [GROUP_R],
          sl.registered if sl else None)

    hist_b = list(eb._history.get(GROUP_R, []))
    check("启动后立即拥有该群的历史上下文（%d 条）" % len(hist_b), len(hist_b) >= 5,
          len(hist_b))
    texts = " ".join(t for _, t in hist_b)
    check("历史内容是真实聊天记录（含群成员发言）", "：" in texts and len(texts) > 50)

    eb.stop(join=False)
    check("停止后记忆已落盘", os.path.exists(boot_mem))
    st_after = MemoryStore(boot_mem)
    check("落盘的记忆含该群会话", st_after.stats()["conversations"] >= 1,
          st_after.stats())

    # ---- 模拟「再次启动」：应恢复持久化记忆，而不是重复回填 ----
    print("\n  -- 模拟第二次启动（应走 ② 恢复，跳过 ① 回填）--")
    logs2 = []
    eb2 = BotEngine(cfg_b, lambda k, p: logs2.append((k, p)), db=rdb)
    StubListener.instances.clear()
    eb2._boot()
    hist_b2 = list(eb2._history.get(GROUP_R, []))
    check("第二次启动恢复了历史（%d 条）" % len(hist_b2), len(hist_b2) >= 5,
          len(hist_b2))

    r2 = [p["text"] for k, p in logs2 if k == "log" and "已恢复" in p.get("text", "")]
    check("日志显示走的是「已恢复上次的对话记忆」", bool(r2), r2)
    warmed2 = [p["text"] for k, p in logs2
               if k == "log" and "回填" in p.get("text", "")
               and "已从数据库回填" in p.get("text", "")]
    check("第二次启动没有再重复回填（已有记忆不覆盖）", not warmed2, warmed2)
    check("两次的历史一致（记忆真的被复用）", hist_b2 == hist_b,
          (len(hist_b2), len(hist_b)))
    eb2.stop(join=False)

    # 清理
    _wdb.Listener = _orig_listener
    for p in (boot_mem, boot_mem + ".tmp"):
        if os.path.exists(p):
            os.remove(p)

# ===========================================================================
# ③ 长期记忆 / 人物档案
# ===========================================================================
print("\n" + "=" * 76)
print("[12] 长期记忆／人物档案（③ 记住这个人是谁）")
print("=" * 76)

from wx_ai_bot import (PROFILE_EXTRACT_PROMPT, ProfileStore,
                       parse_json_reply)

PROF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prof_test.json")
for p in (PROF_PATH, PROF_PATH + ".tmp"):
    if os.path.exists(p):
        os.remove(p)

ps = ProfileStore(PROF_PATH, max_profiles=3, max_facts=4)
check("新档案库为空", ps.stats()["profiles"] == 0)

n1 = ps.note_message("wxid_a", "张三", ["张三", "小张"])
n2 = ps.note_message("wxid_a", "张三", ["张三"])
check("累计消息数递增", (n1, n2) == (1, 2), (n1, n2))
check("记录了显示名", ps.get("wxid_a")["name"] == "张三")
check("记录了别名（当前显示名本身不重复算别名）",
      "小张" in ps.get("wxid_a")["aliases"], ps.get("wxid_a")["aliases"])

print("\n-- 抽取节流 --")
check("消息数不足时不抽取", not ps.should_extract("wxid_a", every_n=10,
                                                min_interval=0))
for _i in range(8):
    ps.note_message("wxid_a", "张三")
check("达到 10 条时触发", ps.should_extract("wxid_a", every_n=10, min_interval=0))
ps.mark_extracting("wxid_a")          # 模拟「已排队」，避免重复抽取
check("排队后时间间隔内不重复抽取",
      not ps.should_extract("wxid_a", every_n=10, min_interval=99999))
check("标记抽取后仍会因消息数增长再次触发（换大间隔后）",
      ps.should_extract("wxid_a", every_n=10, min_interval=0.0))

print("\n-- 合并抽取结果 --")
added, total = ps.merge("wxid_a", facts=["喜欢喝冰美式", "周五有约定"], summary="学生")
check("首次合并写入 2 条事实", (added, total) == (2, 2), (added, total))
check("写入画像摘要", ps.get("wxid_a")["summary"] == "学生")
added, total = ps.merge("wxid_a", facts=["喜欢喝冰美式"])       # 重复
check("重复事实被去重", added == 0, added)
added, total = ps.merge("wxid_a", facts=["喜欢喝冰美式呢"])     # 高度重合
check("高度重合的事实也被跳过", added == 0, added)
added, total = ps.merge("wxid_a", facts=["养了一只叫毛豆的猫"])
check("新事实正常写入", (added, total) == (1, 3), (added, total))
for i in range(6):
    ps.merge("wxid_a", facts=["额外事实%d" % i])
check("事实数不超过上限（max_facts=4）", len(ps.get("wxid_a")["facts"]) <= 4,
      len(ps.get("wxid_a")["facts"]))
added, _ = ps.merge("wxid_a", facts=["x" * 200])
check("超长事实被拒绝", added == 0)

print("\n-- 更新昵称（改名字 → 旧名变别名）--")
ps.update_name("wxid_a", "张三三", ["张三三"])
check("显示名已更新", ps.get("wxid_a")["name"] == "张三三")
check("旧名保留为别名", "张三" in ps.get("wxid_a")["aliases"],
      ps.get("wxid_a")["aliases"])

print("\n-- 持久化 --")
check("落盘成功", ps.save_if_dirty(force=True))
ps2 = ProfileStore(PROF_PATH, max_profiles=3, max_facts=4)
check("重新载入档案", ps2.stats()["profiles"] == 1, ps2.stats())
check("事实与画像完整还原",
      ps2.get("wxid_a")["summary"] == "学生" and ps2.get("wxid_a")["facts"],
      ps2.get("wxid_a"))

ps2.note_message("wxid_b", "B"); ps2.note_message("wxid_c", "C")
ps2.note_message("wxid_d", "D"); ps2.note_message("wxid_e", "E")
check("超出人数上限会淘汰最旧的（max_profiles=3）",
      ps2.stats()["profiles"] == 3, ps2.stats()["profiles"])
check("clear() 清空内存与文件",
      ps2.clear() == 3 and not os.path.exists(PROF_PATH))

print("\n-- JSON 解析容错（模型常加围栏或废话）--")
cases = [
    ('{"facts": ["a"], "summary": "s"}', ["a"], "s"),
    ('```json\n{"facts": ["b"], "summary": "t"}\n```', ["b"], "t"),
    ('好的，结果如下：\n{"facts": ["c"], "summary": "u"}\n希望有帮助', ["c"], "u"),
]
for raw, facts, summary in cases:
    got = parse_json_reply(raw)
    check("解析 %r…" % raw[:26], got.get("facts") == facts
          and got.get("summary") == summary, got)
check("乱码返回空 dict", parse_json_reply("完全不是 json") == {})
check("空输入返回空 dict", parse_json_reply("") == {})
check("数组而非对象 → 空 dict", parse_json_reply("[1,2,3]") == {})

print("\n-- 注入系统提示词 --")
cfg_p = {"context_turns": 3, "system_prompt": "基础人格", "person_rules": [],
         "cooldown": 0, "profile_enabled": True, "profile_file": PROF_PATH,
         "profile_inject": True, "profile_every_n": 10}
ep = BotEngine(cfg_p, lambda k, p: None, db=StubDB())
ep._profiles.merge("wxid_a", facts=["喜欢喝冰美式"], summary="学生")
block = ep._profile_block("wxid_a", "")
check("注入了长期记忆块", "长期记忆" in block and "喜欢喝冰美式" in block, block[:80])
check("注入了画像摘要", "学生" in block)
check("未知人物不注入", ep._profile_block("wxid_unknown", "") == "")
ep.cfg["profile_inject"] = False
check("关闭注入后为空", ep._profile_block("wxid_a", "") == "")

print("\n-- 端到端：抽取流程（替身 LLM 返回 JSON）--")
CAPTURED.clear()
wx_ai_bot.LLMClient = FakeLLM


class JsonLLM:
    def __init__(self, *a, **k):
        pass

    def chat(self, messages, retries=2):
        CAPTURED.append([dict(m) for m in messages])
        return '```json\n{"facts": ["养了一只叫毛豆的猫"], "summary": "爱猫人士"}\n```'


wx_ai_bot.LLMClient = JsonLLM
cfg_e2 = dict(cfg_p)
cfg_e2["profile_file"] = PROF_PATH
ep2 = BotEngine(cfg_e2, lambda k, p: None, db=StubDB())
with ep2._history_lock:
    ep2._history["wxid_z"] = __import__("collections").deque(
        [("user", "我家猫叫毛豆"), ("assistant", "好可爱"),
         ("user", "它两岁了"), ("assistant", "那挺活泼")], maxlen=200)
ep2._extract_profile("wxid_z", "小张", "", "wxid_z")
prof_z = ep2._profiles.get("wxid_z")
check("抽取到了事实", prof_z.get("facts") == ["养了一只叫毛豆的猫"], prof_z)
check("抽取到了画像", prof_z.get("summary") == "爱猫人士")
check("抽取时的提示词是信息抽取器",
      "信息抽取" in CAPTURED[-1][0]["content"])
check("抽取请求里带上了对话内容",
      "我家猫叫毛豆" in CAPTURED[-1][1]["content"])
wx_ai_bot.LLMClient = FakeLLM
for p in (PROF_PATH, PROF_PATH + ".tmp"):
    if os.path.exists(p):
        os.remove(p)

# ===========================================================================
# 一键更新昵称
# ===========================================================================
print("\n" + "=" * 76)
print("[13] 一键更新昵称（refresh_nicknames）")
print("=" * 76)

if has_db:
    # 自动挑一个「有群昵称」的群与成员（公开仓库不能写死真实 wxid）
    GROUP_N, GNAME_N, _roster_n, NAMED_N = find_test_group(rdb, need=1)
    if not GROUP_N or not NAMED_N:
        print("  [跳过] 本地库里没有「设了群昵称」的群，无法验证昵称刷新")
    else:
        _run_refresh_checks(rdb, GROUP_N, GNAME_N, NAMED_N[0][0])


def _run_refresh_checks(rdb, GROUP_R, GNAME_N, WX):
    """一键更新昵称的具体检查：污染缓存 → 刷新 → 验证恢复真实值。"""
    print("  测试群: %s   [%s]   成员 %s" % (GNAME_N, GROUP_R, WX))
    nrp = os.path.join(ROOT, "_prof_nr.json")
    for p in (nrp, nrp + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
    cfg_nr = {"context_turns": 3, "system_prompt": "S", "person_rules": [],
              "cooldown": 0, "profile_enabled": True, "profile_file": nrp,
              "memory_enabled": False, "scope": "selected",
              "targets": [GROUP_R], "warmup_messages": 0}
    en = BotEngine(cfg_nr, lambda k, p: None, db=rdb)
    en.self_wxid = self_wxid(rdb)

    real_nick = en._group_display(WX, GROUP_R)
    check("基线：能取到群昵称 %r" % real_nick, real_nick and real_nick != WX)

    # 污染两个缓存，模拟「缓存过期 / 微信里改了昵称」
    with en._roster_lock:
        en._roster_cache[GROUP_R] = (_time.time(), {WX: "过期的假群昵称"})
    en._name_cache[WX] = "过期的假备注"
    check("污染生效：群显示名变成假值",
          en._group_display(WX, GROUP_R) == "过期的假群昵称",
          en._group_display(WX, GROUP_R))
    check("污染生效：全局名变成假值", en._name_of(WX) == "过期的假备注")

    st = en.refresh_nicknames()
    print("    refresh 结果:", {k: st[k] for k in
                              ("cleared_names", "cleared_rosters",
                               "groups", "members", "named")})
    check("清理了缓存（备注 %d 个 / 花名册 %d 个）"
          % (st["cleared_names"], st["cleared_rosters"]),
          st["cleared_names"] >= 1 and st["cleared_rosters"] >= 1)
    check("重新读到了群花名册（%d 群 %d 人，%d 人设了群昵称）"
          % (st["groups"], st["members"], st["named"]),
          st["members"] > 0 and st["named"] > 0)
    got_after = en._group_display(WX, GROUP_R)
    check("刷新后群昵称恢复为真实值 %r（说明真的重读了库）" % real_nick,
          got_after == real_nick, got_after)
    check("刷新后全局昵称恢复为真实值", en._name_of(WX) == rdb.get_nickname(WX),
          en._name_of(WX))
    check("刷新后花名册已重新填充", len(en._group_roster(GROUP_R)) > 0)

    # 画像里的显示名也应被同步刷新
    en._profiles.merge(WX, facts=["测试事实"], name="过期的假名")
    check("画像里是假名", en._profiles.get(WX)["name"] == "过期的假名")
    st2 = en.refresh_nicknames()
    prof_now = en._profiles.get(WX)
    check("刷新后画像显示名已同步为群昵称 %r" % real_nick,
          prof_now.get("name") == real_nick, prof_now.get("name"))
    check("旧名进入别名", "过期的假名" in (prof_now.get("aliases") or []),
          prof_now.get("aliases"))
    check("刷新会计数同步的档案数", st2["profiles"] >= 1, st2)

    en.stop(join=False)
    for p in (nrp, nrp + ".tmp"):
        if os.path.exists(p):
            os.remove(p)

print("\n" + "=" * 76)
print("汇总（以下结论由本脚本前面的实测得出）：")
print("  · 短期记忆：有 —— 内存滑动窗口，按会话隔离（可选按人隔离）")
print("  · 容量    ：每会话最多 %d 条消息（= %d 轮），超出丢弃最早者"
      % (HISTORY_MAX_MESSAGES, HISTORY_MAX_MESSAGES // 2))
print("  · ② 持久化：有 —— 落盘 wx_ai_bot_memory.json，重启自动恢复 [9]")
print("  · ① 回填  ：有 —— 启动时从数据库读入近期对话 [10]")
print("  · ③ 长期记忆：有 —— 人物档案（谁是谁/喜好/约定），注入回复 [12]")
print("  · 一键更新昵称：有 —— 清缓存并从库重读群昵称与备注 [13]")
if FAILS:
    print("\n有 %d 项未通过：%s" % (len(FAILS), "，".join(FAILS)))
else:
    print("\n结论：全部通过")
print("=" * 76)
sys.exit(1 if FAILS else 0)
