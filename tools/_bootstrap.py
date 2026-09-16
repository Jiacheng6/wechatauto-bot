# -*- coding: utf-8 -*-
"""工具脚本的公共引导。

集中处理三件每脚本都要做的事：

1. **定位项目根目录** —— 工具脚本在 ``tools/`` 下，而配置/日志/缓存都在根目录。
2. **修好两个已知的环境坑**：
   - wechatauto 的日志目录是**相对当前工作目录**的，从别处启动会
     ``PermissionError: [WinError 5] ... 'wechatauto_logs'``，所以要 chdir 到根目录。
   - 导入 wechatauto 时 colorama 会重新包装 ``sys.stdout``，此前未 flush 的输出
     会被丢弃 —— 导入前必须先 flush。
3. **提供测试用的取样工具** —— 公开仓库里不能写死某个人的群/wxid，
   所以改成从本地库里自动挑一个可用的测试对象。

用法::

    from _bootstrap import ROOT, cache_dir, bootstrap, find_test_group, self_wxid
    bootstrap()                    # 控制台编码 + chdir + sys.path
    from wechatauto.db import WeChatDB
"""
from __future__ import annotations

import os
import sys
import time

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)

__all__ = ["ROOT", "TOOLS_DIR", "setup_console", "ensure_root_cwd", "flush",
           "bootstrap", "cache_dir", "find_test_group", "self_wxid",
           "list_groups_with_nicknames"]


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

def setup_console() -> None:
    """Windows 控制台切 UTF-8，避免中文输出乱码。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def ensure_root_cwd() -> None:
    """把工作目录切到项目根目录（wechatauto 的相对路径以 cwd 为准）。"""
    try:
        if os.path.abspath(os.getcwd()) != os.path.abspath(ROOT):
            os.chdir(ROOT)
    except Exception:
        pass


def flush() -> None:
    """导入 wechatauto 之前调用（colorama 会重新包装 stdout，未 flush 的输出会丢）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def bootstrap(console: bool = True) -> str:
    """一次性完成：控制台编码 → sys.path → chdir → flush。

    返回项目根目录。
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    # 未 pip 安装时，兼容仓库里直接放一份 wechatauto 源码
    repo = os.path.join(ROOT, "repo")
    if os.path.isdir(os.path.join(repo, "wechatauto")) and repo not in sys.path:
        sys.path.insert(0, repo)
    if console:
        setup_console()
    ensure_root_cwd()
    flush()
    return ROOT


def cache_dir(name: str = ".wxdb_cache") -> str:
    """解密缓存目录（放在项目根目录下，避免依赖系统临时目录）。"""
    path = os.path.join(ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 测试取样：公开仓库不能写死某人的群/wxid，改为自动挑选
# ---------------------------------------------------------------------------

def self_wxid(db) -> str:
    """当前账号的 wxid。"""
    try:
        info = db.get_self_info() or {}
        return info.get("username") or getattr(db, "wxid", "") or ""
    except Exception:
        return ""


def list_groups_with_nicknames(db, limit: int = 200) -> list:
    """列出「有群昵称（群名片）的群」，按设有群昵称的人数降序。

    Returns:
        [(chatroom_wxid, 群名, {wxid: 群昵称}), ...]
    """
    import wx_ai_bot

    out = []
    try:
        sessions = db.get_sessions(limit=limit)
    except Exception:
        return out
    for s in sessions:
        u = s.get("username") or ""
        if not u.endswith("@chatroom"):
            continue
        roster = {}
        try:
            conn = db._contact_conn()
            if conn:
                try:
                    row = conn.execute(
                        "SELECT ext_buffer FROM chat_room WHERE username=? LIMIT 1",
                        (u,)).fetchone()
                finally:
                    conn.close()
                if row is not None:
                    try:
                        roster = wx_ai_bot.parse_group_members(row["ext_buffer"])
                    except Exception:
                        roster = {}
        except Exception:
            roster = {}
        named = {k: v for k, v in roster.items() if v}
        if named:
            try:
                gname = db.group_id_to_name(u) or u
            except Exception:
                gname = u
            out.append((u, gname, roster))
    out.sort(key=lambda x: -len([v for v in x[2].values() if v]))
    return out


def find_test_group(db, need: int = 1):
    """挑一个可用于测试的群。

    Returns:
        (chatroom_wxid, 群名, {wxid: 群昵称}, [(成员wxid, 群昵称), ...])
        找不到返回 (None, None, {}, [])
    """
    groups = list_groups_with_nicknames(db)
    for chatroom, gname, roster in groups:
        named = [(k, v) for k, v in roster.items() if v]
        if len(named) >= need:
            return chatroom, gname, roster, named
    if groups:                      # 退路：只要是个群就行
        chatroom, gname, roster = groups[0]
        return chatroom, gname, roster, [(k, v) for k, v in roster.items() if v]
    return None, None, {}, []


def find_test_group_any(db, limit: int = 200):
    """挑任意一个群（不要求有群昵称）。

    Returns: (chatroom_wxid, 群名) 或 (None, None)
    """
    try:
        for s in db.get_sessions(limit=limit):
            u = s.get("username") or ""
            if u.endswith("@chatroom"):
                try:
                    return u, (db.group_id_to_name(u) or u)
                except Exception:
                    return u, u
    except Exception:
        pass
    return None, None


def wait(seconds: float) -> None:
    time.sleep(seconds)
