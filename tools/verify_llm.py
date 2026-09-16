# -*- coding: utf-8 -*-
"""大模型客户端诊断：不依赖真实 API Key，用本地 mock 服务验证请求/解析逻辑

验证点：
    1. 请求是否发到正确的 /chat/completions 端点（含 base_url 补全规则）
    2. 请求体字段（model / messages / temperature / max_tokens）是否正确
    3. 正常响应能否正确取出 choices[0].message.content
    4. 错误响应（401 / 业务 error 字段）能否被翻译成可读报错

用法：
    python verify_llm.py            # 跑全部 mock 用例
    python verify_llm.py --real     # 用界面保存的配置真实调用一次（需已填 API Key）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT, bootstrap  # noqa: E402

bootstrap()

from wx_ai_bot import LLMClient, LLMError, load_config  # noqa: E402

RECEIVED = []


class MockHandler(BaseHTTPRequestHandler):
    """模拟 OpenAI 兼容接口。"""
    mode = "ok"

    def log_message(self, *a):        # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        auth = self.headers.get("Authorization", "")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        RECEIVED.append({"path": self.path, "auth": auth, "payload": payload})

        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"message": "unknown endpoint"}})
            return

        if MockHandler.mode == "401":
            self._send(401, {"error": {"message": "Invalid API key provided"}})
            return
        if MockHandler.mode == "biz_error":
            self._send(200, {"error": {"message": "余额不足，请充值"}})
            return
        if MockHandler.mode == "bad_json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"not-a-json")
            return
        if MockHandler.mode == "empty":
            self._send(200, {"choices": [{"message": {"content": ""}}]})
            return

        self._send(200, {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": payload.get("model", "mock"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": "这是 mock 模型的回复"},
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 42},
        })

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run_mock_tests(port: int) -> int:
    server = HTTPServer(("127.0.0.1", port), MockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d/v1" % port
    failures = []

    def check(name, cond, extra=""):
        print("    [%s] %s%s" % ("√" if cond else "×", name,
                                 ("  → %s" % extra) if extra and not cond else ""))
        if not cond:
            failures.append(name)

    # --- 用例 1：正常调用 ---
    print("\n[1] 正常调用")
    MockHandler.mode = "ok"
    RECEIVED.clear()
    client = LLMClient(base, "sk-test-123", "mock-model", 0.3, 256, 20)
    try:
        reply = client.chat([{"role": "user", "content": "你好"}], retries=0)
        check("返回内容正确", reply == "这是 mock 模型的回复", repr(reply))
    except Exception as exc:
        check("调用成功", False, "%s: %s" % (type(exc).__name__, exc))
        reply = ""

    if RECEIVED:
        r = RECEIVED[-1]
        check("端点路径 = /v1/chat/completions", r["path"] == "/v1/chat/completions",
              r["path"])
        check("Authorization 头带 Bearer",
              r["auth"] == "Bearer sk-test-123", r["auth"])
        p = r["payload"]
        check("model 字段正确", p.get("model") == "mock-model", str(p.get("model")))
        check("messages 正确传递", p.get("messages") == [{"role": "user", "content": "你好"}],
              str(p.get("messages")))
        check("temperature / max_tokens 传递",
              p.get("temperature") == 0.3 and p.get("max_tokens") == 256, str(p))
        check("stream=False", p.get("stream") is False, str(p.get("stream")))
    else:
        check("收到请求", False, "mock 服务没收到任何请求")

    # --- 用例 2：base_url 补全规则 ---
    print("\n[2] base_url 补全规则")
    cases = [
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        ("https://x.com/v1/", "https://x.com/v1/chat/completions"),
        ("https://x.com/v1/chat/completions", "https://x.com/v1/chat/completions"),
    ]
    for given, want in cases:
        got = LLMClient(given, "k", "m")._endpoint()
        check("%-42s → %s" % (given, want), got == want, got)

    # --- 用例 3：错误处理 ---
    print("\n[3] 错误处理")
    for mode, keyword, desc in (
        ("401", "401", "HTTP 401 鉴权失败"),
        ("biz_error", "余额不足", "业务 error 字段"),
        ("bad_json", "JSON", "非法 JSON"),
        ("empty", "空", "空内容"),
    ):
        MockHandler.mode = mode
        c = LLMClient(base, "sk-test", "mock-model", timeout=10)
        try:
            c.chat([{"role": "user", "content": "x"}], retries=0)
            check(desc + " 被识别为错误", False, "居然成功返回了")
        except LLMError as exc:
            check(desc, keyword in str(exc), str(exc))
        except Exception as exc:
            check(desc, False, "%s: %s" % (type(exc).__name__, exc))

    # --- 用例 4：网络不可达 ---
    print("\n[4] 网络不可达")
    c = LLMClient("http://127.0.0.1:9/v1", "k", "m", timeout=3)
    try:
        c.chat([{"role": "user", "content": "x"}], retries=0)
        check("连接失败被捕获", False, "居然成功了")
    except LLMError as exc:
        check("连接失败被捕获", True)
        print("        报错信息：%s" % str(exc)[:90])
    except Exception as exc:
        check("连接失败被捕获", False, "%s: %s" % (type(exc).__name__, exc))

    server.shutdown()
    print("\n" + "=" * 66)
    if failures:
        print("结论：%d 项未通过：%s" % (len(failures), "，".join(failures)))
        return 1
    print("结论：大模型客户端逻辑全部通过（端点、字段、解析、错误处理）")
    print("=" * 66)
    return 0


def run_real() -> int:
    cfg = load_config()
    if not cfg.get("api_key"):
        print("[×] 配置里没有 API Key。请先在界面填写，或直接编辑 %s"
              % os.path.join(ROOT, "wx_ai_bot_config.json"))
        return 1
    print("真实调用：%s @ %s" % (cfg.get("model"), cfg.get("base_url")))
    client = LLMClient(cfg["base_url"], cfg["api_key"], cfg["model"],
                       float(cfg.get("temperature", 0.7)),
                       int(cfg.get("max_tokens", 800)),
                       float(cfg.get("timeout", 60)))
    try:
        print("[√] 回复：%s" % client.test())
        return 0
    except Exception as exc:
        print("[×] 失败：%s" % exc)
        return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--port", type=int, default=8791)
    a = ap.parse_args()
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=" * 66)
    print("大模型客户端诊断")
    print("=" * 66)
    sys.exit(run_real() if a.real else run_mock_tests(a.port))
