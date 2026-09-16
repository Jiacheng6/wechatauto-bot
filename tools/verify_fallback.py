# -*- coding: utf-8 -*-
"""发送回退逻辑回归测试（对应当前架构：@ 走 UIA 路径）

覆盖：
    · 不开 @ 时直接普通发送
    · @ 成功时不再重复普通发送
    · @ 失败（弹层没出现 / 匹配不到人 / 抛异常）时必须回退普通发送，绝不丢消息
    · 成员名是数字/占位符时跳过 @
    · 失败要清理输入框（否则回复变成「@正文」）
    · 最终失败时日志必须报「失败」，不得谎报「已发送」
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
sys.stdout.flush()

import wx_ai_bot
from wx_ai_bot import BotEngine

RESULTS = []
FAILS = []


def check(name, cond, extra=""):
    print("  [%s] %s%s" % ("√" if cond else "×", name,
                           ("  → %s" % extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeResponse(dict):
    def __init__(self, status, message):
        super().__init__(status=status, message=message, data=None)

    @property
    def is_success(self):
        return self["status"] == "成功"

    @classmethod
    def success(cls, m):
        return cls("成功", m)

    @classmethod
    def failure(cls, m):
        return cls("失败", m)


class FakeInput:
    def __init__(self, log):
        self.log = log

    def key(self, vk, ctrl=False, shift=False):
        self.log.append(("key", vk, ctrl))


class StubGUI:
    """replace gui.send_msg；@ 路径由 patch 掉的 uia_mention_send 控制。"""

    def __init__(self, send_result=None, keylog=None):
        self.send_result = (send_result if send_result is not None
                            else FakeResponse.success("消息已发送"))
        self.calls = []
        self._input = FakeInput(keylog if keylog is not None else [])

    def send_msg(self, text, who=None, verify=False):
        self.calls.append(("send_msg", who))
        if isinstance(self.send_result, Exception):
            raise self.send_result
        return self.send_result

    def focus_input(self):
        return True


def run_case(name, mention_back, sender, mention_result, expect_calls,
             send_result=None, keylog=None, is_group=True):
    """mention_result: None=不 patch（用真的，会因无 UIA 失败）或 dict"""
    cfg = {"mention_back": mention_back, "verify_send": False}
    eng = BotEngine(cfg, lambda k, p: RESULTS.append((k, p)))
    stub = StubGUI(send_result, keylog)
    eng._get_gui = lambda: stub

    orig = wx_ai_bot.uia_mention_send
    if mention_result is not None:
        if isinstance(mention_result, Exception):
            def fake(gui, names, text, who=None, dry_run=False, _e=mention_result):
                raise _e
        else:
            def fake(gui, names, text, who=None, dry_run=False, _r=mention_result):
                return dict(_r)
        wx_ai_bot.uia_mention_send = fake
    try:
        eng._do_send({
            "username": "123@chatroom" if is_group else "wxid_x",
            "who": "测试群" if is_group else "某人",
            "sender": sender, "sender_wxid": "wxid_member1",
            "text": "回复正文", "is_group": is_group,
        })
    finally:
        wx_ai_bot.uia_mention_send = orig

    got = [c[0] for c in stub.calls]
    ok = got == expect_calls
    check(name, ok, "调用链=%s 期望=%s" % (got, expect_calls))
    return ok, stub


print("=" * 76)
print("发送回退逻辑回归（@ 走 UIA 路径）")
print("=" * 76)

print("\n[1] 不开 @ 选项 → 直接普通发送")
run_case("关闭 @ 时不应走 @ 路径", False, "张三",
         {"ok": True, "matched": "张三", "detail": "不该被调用"},
         ["send_msg"])

print("\n[2] 开 @ 且 UIA 成功 → 不再重复普通发送")
cfg = {"mention_back": True, "verify_send": False}
eng = BotEngine(cfg, lambda k, p: RESULTS.append((k, p)))
stub = StubGUI()
eng._get_gui = lambda: stub
orig = wx_ai_bot.uia_mention_send
wx_ai_bot.uia_mention_send = lambda gui, n, t, who=None, dry_run=False: {
    "ok": True, "matched": "测试成员", "detail": "已 @测试成员 并发送"}
eng._do_send({"username": "123@chatroom", "who": "测试群", "sender": "测试成员",
              "sender_wxid": "wxid_testmember1", "text": "回复正文",
              "is_group": True})
wx_ai_bot.uia_mention_send = orig
check("成功路径完全不调用 send_msg（避免重复发送）",
      [c[0] for c in stub.calls] == [], [c[0] for c in stub.calls])

print("\n[3] 开 @ 但弹层没出现 / 匹配不到人 → 必须回退普通发送")
for detail in ("成员弹层未出现（chat_mention_list 未找到）",
               "未匹配到成员 ['测试成员']；弹层候选：某成员、另一个成员",
               "点选后输入框里没有出现 @"):
    run_case("失败(%s…) → 回退" % detail[:12], True, "测试成员",
             {"ok": False, "detail": detail}, ["send_msg"])

print("\n[4] @ 路径抛异常 → 也要回退")
run_case("抛异常 → 回退", True, "测试成员",
         RuntimeError("UIA 崩了"), ["send_msg"])

print("\n[5] 成员名不可用（数字 ID / 占位符）→ 跳过 @")
run_case("数字成员名 → 直接普通发送", True, "8",
         {"ok": True, "matched": "不该调用", "detail": ""}, ["send_msg"])
run_case("「群成员」占位 → 直接普通发送", True, "群成员",
         {"ok": True, "matched": "不该调用", "detail": ""}, ["send_msg"])

print("\n[6] 私聊不走 @ 路径")
run_case("私聊直接普通发送", True, "某人",
         {"ok": True, "matched": "不该调用", "detail": ""},
         ["send_msg"], is_group=False)

print("\n[7] @ 失败后是否清理输入框（含 Esc / Ctrl+A / Delete）")
keylog = []
run_case("@ 失败 → 清理残留", True, "测试成员",
         {"ok": False, "detail": "未匹配到成员"}, ["send_msg"], keylog=keylog)
keys = [(hex(v), c) for _t, v, c in keylog if _t == "key"]
check("按下了 Esc / Ctrl+A / Delete（%s）" % keys,
      0x1B in [v for _t, v, _c in keylog if _t == "key"]
      and any(v == 0x41 and c for _t, v, c in keylog if _t == "key")
      and 0x2E in [v for _t, v, _c in keylog if _t == "key"], keys)

keylog2 = []
run_case("@ 成功 → 不该有多余按键", True, "测试成员",
         {"ok": True, "matched": "测试成员", "detail": "ok"}, [], keylog=keylog2)
check("成功路径无多余按键", not [k for k in keylog2 if k[0] == "key"])

print("\n[8] 最终发送也失败时，日志必须报「发送失败」且不得谎报「已发送」")
RESULTS.clear()
run_case("@ 失败 + 普通发送也失败", True, "测试成员",
         {"ok": False, "detail": "未匹配到成员"}, ["send_msg"],
         send_result=FakeResponse.failure("发送失败：多次重试未完成"))
logs = [p["text"] for p in (p for _k, p in RESULTS) if isinstance(p, dict)
        and "text" in p]
said_fail = any("回复发送失败" in t for t in logs)
claimed = any("回复已发送" in t for t in logs)
check("报「回复发送失败」=%s，谎报「回复已发送」=%s" % (said_fail, claimed),
      said_fail and not claimed)
if not (said_fail and not claimed):
    for t in logs:
        print("        日志: %s" % t)

print("\n[9] 回退成功时日志应同时有「回退」提示与「已发送」")
RESULTS.clear()
run_case("@ 失败但回退成功", True, "测试成员",
         {"ok": False, "detail": "未匹配到成员"}, ["send_msg"])
logs = [p["text"] for p in (p for _k, p in RESULTS) if isinstance(p, dict)
        and "text" in p]
check("有回退提示且有成功断言",
      any("回退普通发送" in t for t in logs)
      and any("回复已发送" in t for t in logs))

print("\n" + "=" * 76)
if FAILS:
    print("有 %d 项未通过：" % len(FAILS))
    for f in FAILS:
        print("   -", f)
else:
    print("结论：全部通过 —— @ 失败绝不吞掉消息")
print("=" * 76)
sys.exit(1 if FAILS else 0)
