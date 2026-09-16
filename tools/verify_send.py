# -*- coding: utf-8 -*-
"""发送链路诊断：验证 WeChatGUI（UIA / 坐标-OCR）能否真的把消息发出去

⚠️ 这是**会真的发送微信消息**的操作。默认 dry-run，必须显式加 --yes 才发送。
默认目标是「文件传输助手」（发给自己，最安全），用 --who 指定其他会话。

用法：
    python verify_send.py                    # 仅检查窗口与布局，不发送
    python verify_send.py --yes              # 给文件传输助手发一条测试消息
    python verify_send.py --who 某昵称 --yes   # 发给指定会话

注意：发送是 GUI 操作，运行期间会抢占鼠标键盘并切换微信窗口。
      请勿在运行期间操作电脑；桌面锁定 / 微信最小化会导致失败。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
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
    ap.add_argument("--who", default="filehelper", help="目标会话（昵称或 filehelper）")
    ap.add_argument("--text", default=None, help="消息内容")
    ap.add_argument("--yes", action="store_true", help="确认真的发送")
    ap.add_argument("--verify", action="store_true", help="发送后回读数据库确认")
    args = ap.parse_args()

    if os.path.isdir(os.path.join(ROOT, "repo", "wechatauto")):
        sys.path.insert(0, os.path.join(ROOT, "repo"))

    # wechatauto 的日志目录 wechatauto_logs 是相对 cwd 的；从别处启动会在
    # logger 里抛 PermissionError（表现为「微信窗口不可用」这类误导性报错）
    try:
        if os.path.abspath(os.getcwd()) != os.path.abspath(ROOT):
            os.chdir(ROOT)
    except Exception:
        pass

    print("=" * 70)
    print("发送链路诊断")
    print("=" * 70)

    try:
        # 同 verify_read.py：导入 wechatauto 前先 flush，避免表头被 colorama 丢弃
        sys.stdout.flush()
        sys.stderr.flush()
        from wechatauto.guia import WeChatGUI
    except Exception as exc:
        print("[×] 导入失败：%s" % exc)
        traceback.print_exc()
        return 1

    text = args.text or "【链路测试】wx_ai_bot 发送验证 %s" % time.strftime("%H:%M:%S")

    # ---- 1. 构造 WeChatGUI（首次会自动做布局校准，可能耗时较久）----
    print("正在初始化微信 GUI 驱动（首次需 OCR 布局校准，请勿操作鼠标键盘）…")
    t0 = time.time()
    try:
        gui = WeChatGUI()
    except Exception as exc:
        print("[×] 初始化失败：%s" % exc)
        print("\n可能原因：微信未运行 / 未登录 / 主窗口被隐藏。")
        return 1
    print("[√] 初始化成功（%.1fs）" % (time.time() - t0))
    print("    主窗口 hwnd : %s" % gui.main_hwnd)
    print("    渲染窗口    : %s" % gui.render_hwnd)
    print("    窗口矩形    : %s" % (gui.render_rect,))
    print("    侧栏比例    : %.3f" % gui._sidebar_ratio)
    print("    发送键比例  : %s" % (tuple(round(v, 3) for v in gui._send_button_ratio),))

    # ---- 2. UIA 引擎可用性（发送首选路径）----
    try:
        uia = gui._get_uia()
        print("[%s] UIA 引擎（首选发送路径）" % ("√" if uia else "×"))
        if uia:
            try:
                cur = uia.current_chat()
                print("    当前会话：%s" % (cur or "(未打开)"))
            except Exception as exc:
                print("    读取当前会话失败：%s" % exc)
    except Exception as exc:
        print("[!] UIA 引擎探测异常：%s" % exc)

    # ---- 3. 窗口可见性 ----
    try:
        vis = gui.ensure_visible()
        print("[%s] 窗口可见性检查" % ("√" if vis else "×"))
        if not vis:
            print("    微信窗口不可见（最小化 / 锁屏）——发送会失败")
    except Exception as exc:
        print("[!] 可见性检查异常：%s" % exc)

    if not args.yes:
        print("\n" + "-" * 70)
        print("dry-run：未发送任何消息。")
        print("确认微信窗口正常后，加 --yes 实际发送：")
        print("    python verify_send.py --yes")
        print("-" * 70)
        return 0

    # ---- 4. 实际发送 ----
    print("\n" + "-" * 70)
    print("即将发送到「%s」：" % args.who)
    print("    %s" % text)
    print("-" * 70)
    t0 = time.time()
    try:
        resp = gui.send_msg(text, args.who, verify=args.verify)
    except Exception as exc:
        print("[×] 发送抛异常：%s" % exc)
        traceback.print_exc()
        return 1

    ok = bool(getattr(resp, "is_success", False))
    print("[%s] 发送结果（%.1fs）：status=%s message=%s"
          % ("√" if ok else "×", time.time() - t0,
             resp.get("status"), resp.get("message")))

    print("\n" + "=" * 70)
    if ok:
        print("结论：发送链路可用，收→AI→回 的闭环具备条件。")
    else:
        print("结论：发送未成功。请查看上面的 message 定位原因。")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
