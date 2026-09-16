# -*- coding: utf-8 -*-
"""读取链路诊断：验证「定位数据库 → 提取密钥 → 解密 → 读会话/消息」

用途：在启动机器人之前，先确认 wechatauto-replica 的读取链路在你的机器上可用。
只读，不发送任何消息，不修改微信数据。

用法：
    python verify_read.py                # 账号自动选择
    python verify_read.py --account wxid_xxx_abcd
    python verify_read.py --messages 5   # 额外打印最近 5 条消息（默认 0，不打印）

解密缓存默认写到脚本目录下的 .wxdb_cache（避免系统临时目录权限问题）。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT, bootstrap, cache_dir  # noqa: E402

bootstrap()


def setup_console() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=None)
    ap.add_argument("--messages", type=int, default=0)
    ap.add_argument("--limit-sessions", type=int, default=20)
    args = ap.parse_args()

    print("=" * 70)
    print("读取链路诊断")
    print("=" * 70)

    try:
        # wechatauto 会导入 colorama（在 Windows 上重新包装 sys.stdout），
        # 先 flush 否则前面已打印的表头会被丢弃
        sys.stdout.flush()
        sys.stderr.flush()
        import wechatauto
        from wechatauto.db import WeChatDB, auto_detect_db_dir, list_accounts
    except Exception as exc:
        print("[×] 导入 wechatauto 失败：%s" % exc)
        traceback.print_exc()
        return 1
    print("[√] wechatauto %s" % wechatauto.__version__)

    db_dir = auto_detect_db_dir()
    print("[%s] 数据目录：%s" % ("√" if db_dir else "×", db_dir))
    if not db_dir:
        print("    微信 4.x 数据目录未找到（需微信已登录过）")
        return 1

    try:
        accts = list_accounts(db_dir)
    except Exception as exc:
        print("[×] 枚举账号失败：%s" % exc)
        return 1
    print("[√] 发现 %d 个账号：" % len(accts))
    for a in accts:
        print("      %-28s wxid=%-24s 最近活动=%s"
              % (a.get("account"), a.get("wxid"),
                 __import__("time").strftime(
                     "%Y-%m-%d %H:%M", __import__("time").localtime(
                         a.get("last_activity") or 0))))

    workdir = cache_dir()
    if args.account:
        workdir = os.path.join(workdir, args.account)
    print("\n解密缓存目录：%s" % workdir)

    print("\n正在提取密钥并解密数据库（首次约 6 秒，仅内存只读扫描）…")
    try:
        db = WeChatDB(db_dir=db_dir, workdir=workdir, account=args.account)
    except Exception as exc:
        print("[×] 初始化失败：%s" % exc)
        print("\n可能原因：")
        print("  1. 微信 4.x 未登录（密钥只存在于已登录进程的内存中）")
        print("  2. Python 与微信位数不一致（必须同为 64 位）")
        print("  3. 多账号时选错账号，请加 --account 指定")
        print("\n可运行项目自带诊断获取细节：python -m wechatauto.diagnose_keys")
        traceback.print_exc()
        return 1

    print("[√] 数据库就绪，账号目录：%s" % db.account)
    print("    wxid：%s" % db.wxid)
    try:
        info = db.get_self_info() or {}
        print("    昵称：%s" % (info.get("nick_name") or "-"))
        print("    备注：%s" % (info.get("remark") or "-"))
    except Exception as exc:
        print("[!] 读取自身信息失败：%s" % exc)

    print("\n" + "-" * 70)
    print("会话列表（前 %d 个，供选择监听目标）" % args.limit_sessions)
    print("-" * 70)
    try:
        sessions = db.get_sessions(limit=args.limit_sessions)
    except Exception as exc:
        print("[×] 读取会话失败：%s" % exc)
        traceback.print_exc()
        return 1

    print("%-34s %-22s %6s  %s" % ("username", "显示名", "未读", "最近消息摘要"))
    for s in sessions:
        u = s.get("username") or ""
        try:
            disp = db.get_nickname(u)
        except Exception:
            disp = u
        if not disp or disp == u:
            if u.endswith("@chatroom"):
                try:
                    disp = db.group_id_to_name(u) or u
                except Exception:
                    disp = u
        kind = "群" if u.endswith("@chatroom") else "私"
        print("%-34s %-22s %6s  %s"
              % (u, "%s[%s]" % (disp[:20], kind), s.get("unread"),
                 (s.get("summary") or "")[:28].replace("\n", " ")))

    if args.messages:
        print("\n" + "-" * 70)
        print("最近消息")
        print("-" * 70)
        for s in sessions[:3]:
            u = s.get("username") or ""
            try:
                msgs = db.get_messages(u, limit=args.messages)
            except Exception as exc:
                print("  [%s] 读取失败：%s" % (u, exc))
                continue
            print("\n  ── %s ──" % u)
            for m in msgs:
                import time as _t
                ts = _t.strftime("%m-%d %H:%M",
                                 _t.localtime(m.get("create_time") or 0))
                who = "我" if m.get("sender_id") == 2 else (
                    m.get("sender_username") or m.get("sender_id"))
                print("    [%s] %s (%s) %s"
                      % (ts, who, m.get("type"),
                         (m.get("content") or "")[:50].replace("\n", " ")))

    print("\n" + "=" * 70)
    print("结论：读取链路可用。可启动图形界面：python wx_ai_bot.py")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
