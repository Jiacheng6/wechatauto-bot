# -*- coding: utf-8 -*-
"""微信 AI 自动回复机器人（可视化界面）

基于 wechatauto-replica：
    * 读消息 —— 本地 SQLCipher 数据库解密 + Listener 增量轮询（无需微信在前台）
    * 发消息 —— UIA / 坐标-OCR 混合驱动（需要微信窗口可见且桌面未锁定）

闭环：监听新消息 → 调用大模型 API → 自动回复。

特性：
    * tkinter 可视化界面：全部配置在界面上完成，配置持久化到 JSON
    * 任意 OpenAI 兼容接口（DeepSeek / OpenAI / 通义 / Kimi / Ollama / vLLM…）
    * 私聊与群聊分别设定触发规则（全部 / 仅关键词 / 仅 @我 / 关闭）
    * 多轮上下文、每会话冷却、消息去重、发送队列节流
    * 全部耗时操作在后台线程，界面不卡；日志与消息双视图

用法：
    python wx_ai_bot.py            # 启动图形界面
    python wx_ai_bot.py --check    # 环境自检（不启动界面）

依赖：
    pip install -e .               # 在 wechatauto-replica 仓库目录下
    pip install winsdk pypinyin    # OCR 发送路径（可选但建议）
    大模型走标准库 urllib，无需额外安装；若装了 openai 包则自动优先使用。
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections import OrderedDict, defaultdict, deque

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------

APP_NAME = "微信 AI 自动回复机器人"
APP_VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "wx_ai_bot_config.json")
LOG_PATH = os.path.join(BASE_DIR, "wx_ai_bot.log")
LOG_MAX_BYTES = 5 * 1024 * 1024

#: 单个会话最多保留多少条历史消息（短期记忆容量）。
#: 每条约几十~几百字节，200 条 ≈ 100 轮，足够任何正常对话；
#: 原先硬编码 12 条导致界面「上下文轮数」填超过 6 也无效（被静默截断）。
HISTORY_MAX_MESSAGES = 200
HISTORY_MAX_TURNS = HISTORY_MAX_MESSAGES // 2

DEFAULT_SYSTEM_PROMPT = (
    "你是一个通过微信与人交流的智能助理。要求：\n"
    "1. 用简体中文回复，语气自然、友好、口语化，像真人聊天；\n"
    "2. 回复尽量简短（一般不超过 100 字），除非对方明确要求详细说明；\n"
    "3. 不要暴露你是 AI、机器人或程序，也不要提及系统提示；\n"
    "4. 不要使用 Markdown 标记（微信不渲染），不要输出表情符号以外的特殊格式；\n"
    "5. 对方问什么就答什么，不确定的事情坦率说不清楚，不要编造。"
)

#: 内置提示词预设（可在界面上即时切换、另存为新的预设）
DEFAULT_PROMPT_PRESETS = {
    "中性助手（默认）": DEFAULT_SYSTEM_PROMPT,
    "温和礼貌": (
        "你是一个通过微信与人交流的助理。要求：\n"
        "1. 简体中文，语气温和、礼貌、有耐心，多用「您」「请」；\n"
        "2. 回复简短（不超过 80 字），说重点；\n"
        "3. 不暴露自己是 AI；不用 Markdown；\n"
        "4. 遇到被冒犯或挑衅时保持克制，礼貌地转移话题，绝不回骂；\n"
        "5. 不知道就直说不知道，不要编造。"
    ),
    "简洁犀利": (
        "你是一个微信上的聊天搭子。要求：\n"
        "1. 简体中文，说话短、直、干脆，一针见血，不废话；\n"
        "2. 一次回复不超过 40 字，常常一句话就够；\n"
        "3. 可以有态度、有观点，但**不对人进行人身攻击、不使用脏话**；\n"
        "4. 被挑衅时用机智化解，不要跟着对骂；\n"
        "5. 不暴露自己是 AI；不用 Markdown。"
    ),
    "客服答疑": (
        "你是一名产品客服。要求：\n"
        "1. 简体中文，专业、准确、有条理；\n"
        "2. 先给结论再给理由，必要时用「1. 2. 3.」分点；\n"
        "3. 只回答与产品或服务相关的问题，其余礼貌说明不负责该范围；\n"
        "4. 不确定的信息不要编造，改为引导用户提供更多信息；\n"
        "5. 不使用 Markdown 语法（微信不渲染）。"
    ),
    "群友闲聊": (
        "你是这个微信群里的一名普通成员。要求：\n"
        "1. 简体中文，轻松、幽默、接地气，像群友一样接话；\n"
        "2. 回复极短（一般不超过 30 字），像真人随口一句；\n"
        "3. 可以开玩笑、玩梗，但不骂人、不引战、不聊敏感话题；\n"
        "4. 不暴露自己是 AI；不用 Markdown；少用表情，别每句都带。"
    ),
}

DEFAULT_CONFIG = {
    # 微信连接
    "account": "",
    "scope": "selected",           # selected = 指定会话；all = 全部会话
    "targets": [],                 # 指定会话的 username 列表
    "interval": 1.0,               # 轮询间隔（秒）

    # 大模型
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "prompt_presets": dict(DEFAULT_PROMPT_PRESETS),
    "temperature": 0.7,
    "max_tokens": 800,
    "timeout": 60,
    "llm_concurrency": 2,          # 同时进行的大模型请求数上限

    # 回复规则
    "private_mode": "all",         # all | keyword | off
    "group_mode": "at",            # at | all | keyword | off
    "keywords": "",                # 逗号分隔
    "extra_types": False,          # 是否也处理「文件/链接/卡片」类文本
    "context_turns": 3,            # 携带多少轮历史上下文（0 = 不带）
    "cooldown": 3.0,               # 同一会话两次回复的最小间隔（秒）
    "cooldown_per_person": True,   # 群聊冷却按人分别计时（防连坐）
    "context_isolation": "chat",   # chat = 按会话隔离（群之间天然隔离）| person = 群内每人独立
    "max_reply_chars": 500,        # 回复超长截断
    "mention_back": False,         # 群聊回复时 @ 对方（默认关闭，OCR 定位）
    "verify_send": False,          # 发送后回读数据库确认
    "send_interval": 1.5,          # 两条发送之间的最小间隔（秒）

    # 人物规则：识别特定人物 → 特定回答
    # [{"who": "群昵称或备注或wxid（逗号分隔多个，* 表示任何人）",
    #   "scope": "*" | "group" | "private" | "group:<群wxid或群名>",
    #   "action": "reply" | "ignore" | "keyword" | "at_only",
    #   "keywords": "action=keyword 时的关键词",
    #   "persona": "该人物的专属提示词",
    #   "persona_mode": "append" 追加 | "replace" 替换全局提示词",
    #   "note": "备注"}]
    "person_rules": [],

    # 记忆
    "memory_enabled": True,        # ② 对话记忆持久化（重启不丢）
    "memory_file": "",             # 空 = 脚本目录下 wx_ai_bot_memory.json
    "memory_max_conversations": 200,
    "memory_max_age_hours": 168,   # 超过此时长的记忆视为过期（7 天）
    "memory_save_interval": 30,    # 写盘节流间隔（秒）
    "warmup_messages": 20,         # ① 启动时从数据库回填最近多少条消息（0 = 关闭）
    "warmup_hours": 72,            # 只回填这个时间内的消息（0 = 不限）
    "warmup_max_chats": 20,        # 最多回填多少个会话

    # ③ 长期记忆／用户画像
    "profile_enabled": True,       # 让模型从对话里抽取「关于某人的稳定事实」
    "profile_file": "",            # 空 = 脚本目录下 wx_ai_bot_profiles.json
    "profile_every_n": 10,         # 每累计多少条消息抽取一次画像
    "profile_min_interval": 1800,  # 同一人两次抽取的最小间隔（秒）
    "profile_max_facts": 20,       # 每人最多保留多少条事实
    "profile_inject": True,        # 回复时把画像注入系统提示词

    "save_api_key": True,
    "log_to_file": True,
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%H:%M:%S")


#: 清空输入框用的虚拟键码（与 wechatauto.guia 内的常量一致）
VK_ESCAPE = 0x1B
VK_DELETE = 0x2E
VK_A = 0x41


def ensure_workdir() -> None:
    """把当前工作目录切到脚本所在目录。

    ⚠️ wechatauto 的**日志目录** ``wechatauto_logs`` 与默认下载目录都是
    **相对当前工作目录**的路径（``Path("wechatauto_logs").mkdir(...)``）。
    若从别处（尤其不可写的目录）启动，logger 会在建目录时抛
    ``PermissionError: [WinError 5] ... 'wechatauto_logs'``。

    更麻烦的是：它的 FileHandler 是**惰性创建**的，创建失败后 ``file_handler``
    仍为 None，于是**每一次日志调用都会重抛**同一个异常 —— 整个 wechatauto 层
    都会因为「日志目录不可写」而瘫痪，报错还常常伪装成别的功能故障
    （例如 ``WeChatGUI()`` 一构造就抛，看起来像"微信窗口不可用"）。
    """
    try:
        if os.path.abspath(os.getcwd()) != os.path.abspath(BASE_DIR):
            os.chdir(BASE_DIR)
    except Exception:
        pass


def preflight_wxlog() -> str:
    """提前触发 wechatauto 的文件日志初始化；失败就关掉它。

    返回空串表示正常，否则返回给用户看的说明。这样即便工作目录只读，
    也只会丢文件日志，功能不受影响。
    """
    try:
        from wechatauto.logger import wxlog
    except Exception:
        return ""
    try:
        wxlog._ensure_file_logger()
        return ""
    except Exception as exc:
        try:
            from wechatauto.param import WxParam
            WxParam.ENABLE_FILE_LOGGER = False
        except Exception:
            pass
        return ("wechatauto 的文件日志目录不可写（%s）。已自动关闭它的文件日志，"
                "收发功能不受影响。\n"
                "        建议从脚本所在目录启动：cd /d %s" % (exc, BASE_DIR))


def describe_wechat_window() -> str:
    """描述微信主窗口当前状态，用于给出**可操作**的报错（而不是"请确认微信已运行"）。"""
    try:
        u = ctypes.windll.user32
    except Exception:
        return ""
    found_hidden = False
    found_any = False
    for title in ("微信", "Weixin"):
        try:
            h = u.FindWindowW(None, title)
        except Exception:
            h = 0
        if not h:
            continue
        found_any = True
        try:
            if u.IsWindowVisible(h):
                return ""
            if u.IsIconic(h):
                return "微信主窗口处于「最小化」状态"
            found_hidden = True
        except Exception:
            pass
    if found_hidden:
        # 微信 4.x 点关闭是隐藏主窗口（收进托盘）：窗口句柄还在、IsWindow 为真，
        # 但 IsWindowVisible 为假，且 ShowWindow 对它无效（由微信自己管理可见性）。
        return ("微信主窗口被「隐藏」了（多半是关到了托盘）——窗口句柄还在、"
                "进程也在运行，但 GUI 自动化看不见它")
    if not found_any:
        return "没有找到微信主窗口（微信可能未运行、未登录，或窗口已被关闭）"
    return ""


def restore_wechat_window() -> bool:
    """尝试把微信主窗口从「最小化 / 隐藏」恢复出来。

    库的 ``_find_main_window`` **只在可见的顶层窗口里**评分查找，所以微信一旦
    最小化或被收进托盘，``WeChatGUI()`` 就会直接抛
    「未找到微信主窗口，请确认微信已登录并运行」——措辞会让人以为微信没开。

    注意：**最小化**（``IsIconic``）可以用 ``SW_RESTORE`` 恢复；
    但微信 4.x 的「关到托盘」是**隐藏**（``IsWindowVisible`` 假而 ``IsIconic``
    也假），``ShowWindow`` 通常无效 —— 这种情况只能请用户点托盘图标。
    """
    try:
        u = ctypes.windll.user32
    except Exception:
        return False
    for title in ("微信", "Weixin"):
        try:
            h = u.FindWindowW(None, title)
        except Exception:
            h = 0
        if not h:
            continue
        for mode in (9, 1, 5):      # SW_RESTORE（治最小化）→ SHOWNORMAL → SHOW
            try:
                u.ShowWindow(h, mode)
            except Exception:
                pass
            time.sleep(0.3)
            try:
                if u.IsWindowVisible(h):
                    try:
                        u.SetForegroundWindow(h)
                    except Exception:
                        pass
                    time.sleep(0.6)
                    return True
            except Exception:
                pass
    return False


def clear_leftover_input(gui) -> None:
    """清掉上一次失败操作残留在输入框里的内容。

    ``at_member`` 在「找不到成员」时直接返回失败，**但此时它已经往输入框敲了
    `@` 且成员弹层还开着**。若不清理就回退普通发送，回复会变成「@正文」，
    甚至被弹层吃掉导致发送失败。
    """
    inp = getattr(gui, "_input", None)
    if inp is None:
        return
    try:
        inp.key(VK_ESCAPE)              # 关掉 @ 成员选择弹层
        time.sleep(0.25)
    except Exception:
        pass
    try:
        if gui.focus_input():
            inp.key(VK_A, ctrl=True)    # 全选
            time.sleep(0.15)
            inp.key(VK_DELETE)          # 删除
            time.sleep(0.15)
    except Exception:
        pass


#: 微信 @ 成员弹层的 UIA 标识（实测：mmui::XPopover / MentionPopover 下）
MENTION_LIST_AID = "chat_mention_list"


def uia_mention_send(gui, member_names, text: str, who: str = None,
                     dry_run: bool = False) -> dict:
    """用 UIA 发送一条带 **真实 @提及** 的消息（不依赖 OCR / 坐标）。

    为什么必须自己实现：微信的成员弹层**只由真实按键触发**。库里的
    ``at_member`` 用 ``type_unicode('@')`` 注入 Unicode 字符，微信不认为那是
    @ 提及，弹层从不出现 → OCR 在聊天区里怎么找都找不到成员名。实测
    （``_diag_uia_at.py``）：``SendKeys("@")`` 会立刻弹出
    ``mmui::XPopover / MentionPopover``，其中
    ``ListControl(AutomationId='chat_mention_list')`` 的子项 ``Name``
    就是成员显示名，可直接读取与点击。

    Args:
        member_names: 候选名字（群昵称 / 备注 / 昵称 …），按顺序尝试匹配。
        dry_run: 只把 @ 提及插进输入框并校验，不发出去（用于自检）。

    Returns:
        dict: {"ok": bool, "stage": str, "detail": str, "matched": str}
    """
    res = {"ok": False, "stage": "init", "detail": "", "matched": ""}
    try:
        import uiautomation as auto
    except Exception as exc:
        res["detail"] = "uiautomation 不可用：%s" % exc
        return res

    uia = None
    try:
        uia = gui._get_uia()
    except Exception as exc:
        res["detail"] = "UIA 引擎不可用：%s" % exc
        return res
    if uia is None:
        res["detail"] = "UIA 引擎未启用"
        return res

    try:
        if not uia.ensure_window():
            res["detail"] = "微信窗口不可用"
            return res
        if who:
            res["stage"] = "open_chat"
            try:
                if uia.current_chat() != who and not uia.open_chat(who):
                    res["detail"] = "打开会话失败：%s" % who
                    return res
            except Exception as exc:
                res["detail"] = "打开会话异常：%s" % exc
                return res
            time.sleep(0.5)

        e = uia._chat_input()
        if e is None:
            res["stage"] = "input"
            res["detail"] = "找不到输入框控件（chat_input_field）"
            return res

        # 1) 清空输入框，并用**真实按键**敲 '@' 触发成员弹层
        res["stage"] = "type_at"
        e.Click()
        time.sleep(0.2)
        e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
        time.sleep(0.2)
        e.SendKeys("@", waitTime=0.1)
        time.sleep(0.9)

        # 2) 在 UIA 树里拿到成员列表
        res["stage"] = "find_popup"
        lst = None
        deadline = time.time() + 3.0
        while time.time() < deadline:
            lst = auto.ListControl(searchDepth=0xFFFFFFFF,
                                   AutomationId=MENTION_LIST_AID)
            if lst.Exists(0.2, 0.1):
                break
            lst = None
            time.sleep(0.2)
        if lst is None:
            res["detail"] = ("成员弹层未出现（chat_mention_list 未找到）——"
                             "可能输入框未真正聚焦")
            try:
                e.SendKeys("{Esc}", waitTime=0.05)
                e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            except Exception:
                pass
            return res

        items = []
        try:
            for child in lst.GetChildren():
                try:
                    items.append((child, (child.Name or "").strip()))
                except Exception:
                    continue
        except Exception as exc:
            res["detail"] = "读取成员列表失败：%s" % exc
            return res
        res["detail"] = "候选：%s" % "、".join(n for _c, n in items[:8])

        # 3) 匹配成员名（精确 → 忽略大小写 → 包含 → 前两字）
        want = [str(w).strip() for w in (member_names or []) if str(w).strip()]
        target = None
        matched = ""
        for w in want:
            wl = w.lower()
            for ctrl, name in items:
                if name and name == w:
                    target, matched = ctrl, name
                    break
            if target is None:
                for ctrl, name in items:
                    if name and name.lower() == wl:
                        target, matched = ctrl, name
                        break
            if target is None:
                for ctrl, name in items:
                    if name and (wl in name.lower() or name.lower() in wl):
                        target, matched = ctrl, name
                        break
            if target is None and len(w) >= 2:
                frag = w[:2]
                for ctrl, name in items:
                    if name and name.startswith(frag):
                        target, matched = ctrl, name
                        break
            if target is not None:
                break
        if target is None:
            res["stage"] = "match"
            res["detail"] = ("未匹配到成员 %s；弹层候选：%s"
                             % (want, "、".join(n for _c, n in items[:8])))
            try:
                e.SendKeys("{Esc}", waitTime=0.05)
                e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            except Exception:
                pass
            return res
        res["matched"] = matched

        # 4) 点选成员（UIA 点击，不用坐标）
        res["stage"] = "click"
        clicked = False
        try:
            target.Click()
            clicked = True
        except Exception:
            try:
                target.GetInvokePattern().Invoke()
                clicked = True
            except Exception as exc:
                res["detail"] = "点选成员失败：%s" % exc
                return res
        if not clicked:
            res["detail"] = "点选成员失败"
            return res
        time.sleep(0.6)

        # 5) 校验 @提及 真的进了输入框
        #    注意：微信把提及渲染成内联「芯片」，ValuePattern 里是 **U+FFFC
        #    (OBJECT REPLACEMENT CHARACTER)** 而不是字面量 "@名字"。
        res["stage"] = "verify_mention"
        try:
            v = e.GetValuePattern().Value or ""
        except Exception:
            v = ""
        if ("@" not in v) and ("\ufffc" not in v):
            res["detail"] = ("点选后输入框里没有出现提及（实际内容 %r）" % v[:40])
            try:
                e.SendKeys("{Esc}", waitTime=0.05)
                e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            except Exception:
                pass
            return res
        if dry_run:
            res["ok"] = True
            res["stage"] = "dry_run"
            res["detail"] = ("已插入提及（输入框内容 %r，含 %d 个内联芯片）"
                             % (v[:40], v.count("\ufffc")))
            try:
                e.SendKeys("{Esc}", waitTime=0.05)
                e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            except Exception:
                pass
            return res

        # 6) 追加正文（**不能清空**，否则 @提及 会一起没掉）再发送
        res["stage"] = "type_text"
        try:
            uia._paste_into(e, text, clear=False)
        except Exception:
            e.Click()
            time.sleep(0.1)
            uia._clip_set(text)
            e.SendKeys("{Ctrl}v", waitTime=0.05)
        time.sleep(0.3)
        res["stage"] = "send"
        e.SendKeys("{Enter}", waitTime=0.05)
        res["ok"] = True
        res["detail"] = "已 @%s 并发送" % matched
        return res
    except Exception as exc:
        res["detail"] = "%s: %s" % (type(exc).__name__, exc)
        return res


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "-"


def _to_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _to_int(value, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _clip(text: str, limit: int = 90) -> str:
    text = (text or "").replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# 微信 4.x 群聊消息的 message_content 形如：
#     "wxid_xxxxxxxxxxxx22:\n真正的正文"
# 库里 real_sender_id 是群内序号，contact 表查不到时会被反查成 "8" / "10"
# 这类数字 ID —— 用内容前缀里的 wxid 才能拿到真实昵称。
_GROUP_SENDER_RE = re.compile(r"^(wxid_[0-9A-Za-z_\-]{4,})\s*[:：][ \t]*\n?")


def split_group_sender(content: str) -> tuple:
    """从群聊消息正文里剥出发送者 wxid，返回 (wxid, 正文)。

    非群格式（或无前缀）返回 ("", 原文)。
    """
    if not content:
        return "", content
    m = _GROUP_SENDER_RE.match(content)
    if not m:
        return "", content
    return m.group(1), content[m.end():].strip()


def looks_like_wxid(value: str) -> bool:
    """判断一个标识是否是可用的 username（而不是 '8' / '10' 这类群内序号）。"""
    v = (value or "").strip()
    if not v:
        return False
    if v.startswith("wxid_"):
        return True
    if v.endswith("@chatroom") or v.endswith("@openim"):
        return True
    if v == "filehelper" or v == "weixin":
        return True
    # 纯数字（群内序号）或过短的一串都不可靠
    if v.isdigit():
        return False
    return False


# ---------------------------------------------------------------------------
# 群昵称（群名片）解析
# ---------------------------------------------------------------------------
# 微信 4.x 把「每个群成员的群昵称」存在 contact.db 的 chat_room.ext_buffer 里，
# 是 protobuf：repeated f1 { f1: wxid, f2: 群昵称(可缺省), f3: flag, f4: 邀请人 }
#
# 这一点极其关键：**群里 @ 人时插入的是「群昵称」，不是全局昵称/备注**。
# 实测某群 45 名成员，群昵称与全局昵称 100% 不同（阿明↔Ming_2024、
# 老王↔WangSir…），用全局昵称去 @ 永远找不到人；连「@我」检测也会失效
# —— 机器人在某群的群昵称可能与它在别处的昵称完全不同。
#
# 群昵称为空时微信显示的是全局昵称（备注优先），故
# 显示名 = 群昵称 or 备注 or 昵称 or wxid。

def _pb_varint(buf: bytes, i: int):
    """读一个 protobuf varint，返回 (值, 新下标)。"""
    result = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 越界")


def _pb_fields(buf: bytes):
    """遍历 protobuf 字段，产出 (字段号, wire_type, 值)。"""
    i, n = 0, len(buf)
    while i < n:
        tag, i = _pb_varint(buf, i)
        fno, wt = tag >> 3, tag & 7
        if wt == 0:
            val, i = _pb_varint(buf, i)
            yield fno, wt, val
        elif wt == 2:
            ln, i = _pb_varint(buf, i)
            if i + ln > n:
                raise ValueError("字段被截断")
            yield fno, wt, buf[i:i + ln]
            i += ln
        elif wt == 5:
            yield fno, wt, buf[i:i + 4]
            i += 4
        elif wt == 1:
            yield fno, wt, buf[i:i + 8]
            i += 8
        else:
            raise ValueError("不支持的 wire_type=%d" % wt)


def parse_group_members(blob) -> dict:
    """解析 chat_room.ext_buffer → {wxid: 群昵称}（群昵称可能为空字符串）。

    解析失败一律返回已解出的部分，绝不抛异常。
    """
    out = {}
    if not blob:
        return out
    try:
        for _fno, wt, val in _pb_fields(bytes(blob)):
            if wt != 2 or not isinstance(val, (bytes, bytearray)):
                continue
            wxid = nick = ""
            try:
                for f2, w2, v2 in _pb_fields(bytes(val)):
                    if w2 != 2:
                        continue
                    text = bytes(v2).decode("utf-8", "replace")
                    if f2 == 1 and not wxid:
                        wxid = text
                    elif f2 == 2 and not nick:
                        nick = text
            except Exception:
                continue
            if wxid and looks_like_wxid(wxid):
                out[wxid] = nick
    except Exception:
        pass
    return out


def _setup_console() -> None:
    """Windows 控制台切到 UTF-8，避免 --check 中文输出乱码。

    注意：仅设置编码是安全的；真正会丢输出的是「导入 wechatauto 时 colorama
    重新包装 sys.stdout」——那由 :func:`_flush_streams` 在导入前 flush 解决。
    """
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


def _flush_streams() -> None:
    """在导入可能重新包装 sys.stdout 的第三方库之前刷新缓冲。

    wechatauto 会导入 colorama，colorama 在 Windows 上会用 AnsiToWin32 重新
    包装 sys.stdout/sys.stderr；若此前有未 flush 的输出，会被连同旧包装一起
    丢弃（表现为「重定向时开头若干行消失」，加 -u 或交互式运行则正常）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def explain_import_error(exc: BaseException) -> str:
    """把 wechatauto 导入失败翻译成可操作的提示。"""
    msg = "%s" % exc
    if ("WinError 5" in msg or "PermissionError" in msg or "拒绝访问" in msg
            or "Access is denied" in msg):
        return ("依赖读写被系统拒绝（uiautomation → comtypes 需要写 COM 代码缓存目录）。\n"
                "        处理办法：给当前用户对 Python 安装目录下 "
                "site-packages\\comtypes\\gen 与 %APPDATA%\\Python 的写权限；\n"
                "        或改用普通用户终端运行（受限/沙箱/只读环境会拦截这类写入）。")
    if "No module named" in msg:
        name = msg.split("No module named")[-1].strip().strip("'\"")
        return ("缺少依赖：%s\n"
                "        在 wechatauto-replica 仓库目录执行：pip install -e ." % name)
    return "请在该仓库目录执行 pip install -e . 后重试。"


def wechatauto_on_path() -> bool:
    """wechatauto 包目录是否已在 sys.path 上（不代表依赖齐全）。"""
    for p in sys.path:
        try:
            if p and os.path.isdir(os.path.join(p, "wechatauto")):
                return True
        except Exception:
            continue
    return False


def ensure_wechatauto_on_path() -> None:
    """把本地的 wechatauto-replica 仓库加入 sys.path（未 pip 安装时也能跑）。"""
    try:
        _flush_streams()          # colorama 会包装 stdout，先冲掉缓冲
        import wechatauto  # noqa: F401
        return
    except Exception:
        pass
    candidates = [
        BASE_DIR,
        os.path.join(BASE_DIR, "repo"),
        os.path.join(BASE_DIR, "wechatauto-replica"),
        os.path.join(os.path.dirname(BASE_DIR), "repo"),
    ]
    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "wechatauto")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            cfg.update(saved)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    data = dict(cfg)
    if not data.get("save_api_key"):
        data["api_key"] = ""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------------------
# 大模型客户端（OpenAI 兼容接口）
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# JSON 持久化小工具（MemoryStore / ProfileStore 共用）
# ---------------------------------------------------------------------------

def _read_json_file(path: str) -> dict:
    """读 JSON，任何异常都返回空 dict（坏文件绝不阻断启动）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: str, obj) -> bool:
    """原子写 JSON（临时文件 + 改名），失败不抛异常。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# 长期记忆／用户画像（③ 认识某个人）
# ---------------------------------------------------------------------------

class ProfileStore:
    """人物档案：把「关于某个人的稳定事实」长期记下来。

    与 ①②（对话历史）的区别：
        · ①② 记的是「最近聊了什么」—— 会过期、会被滑动窗口挤掉
        · ③ 记的是「这个人是谁、喜欢什么、有什么约定」—— 长期保留，
          由大模型从对话里抽取，注入到每次回复的系统提示词中

    以 **wxid 为键**（昵称会变，wxid 不变），同时记录见过的各种名字
    （群昵称/备注/昵称）作为 aliases，配合「更新昵称」刷新。
    """

    VERSION = 1

    def __init__(self, path: str, max_profiles: int = 500, max_facts: int = 20):
        self.path = path
        self.max_profiles = max(1, int(max_profiles))
        self.max_facts = max(1, int(max_facts))
        self._data = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0
        self.loaded_count = 0
        self.load()

    # -- 读 ---------------------------------------------------------------
    def load(self) -> int:
        obj = _read_json_file(self.path)
        profs = obj.get("profiles") if isinstance(obj, dict) else None
        if not isinstance(profs, dict):
            return 0
        fresh = {}
        for wxid, item in profs.items():
            if not isinstance(item, dict):
                continue
            facts = [str(f) for f in (item.get("facts") or [])
                     if isinstance(f, str) and f.strip()]
            aliases = [str(a) for a in (item.get("aliases") or [])
                       if isinstance(a, str) and a.strip()]
            fresh[str(wxid)] = {
                "name": str(item.get("name") or ""),
                "aliases": aliases[:10],
                "summary": str(item.get("summary") or ""),
                "facts": facts[-self.max_facts:],
                "msg_count": int(item.get("msg_count") or 0),
                "updated": float(item.get("updated") or 0) or time.time(),
                "extracted_at": float(item.get("extracted_at") or 0),
            }
        with self._lock:
            self._data = fresh
            self._evict_locked()
            self.loaded_count = len(fresh)
        return len(fresh)

    # -- 写 ---------------------------------------------------------------
    def get(self, wxid: str) -> dict:
        with self._lock:
            item = self._data.get(wxid)
            return dict(item) if item else {}

    def note_message(self, wxid: str, name: str = "", aliases=None) -> int:
        """记一条来自此人的消息；返回其累计消息数。"""
        if not wxid:
            return 0
        with self._lock:
            item = self._data.setdefault(wxid, {
                "name": "", "aliases": [], "summary": "", "facts": [],
                "msg_count": 0, "updated": time.time(), "extracted_at": 0.0,
            })
            item["msg_count"] = int(item.get("msg_count") or 0) + 1
            item["updated"] = time.time()
            self._touch_name_locked(item, name, aliases)
            self._evict_locked()
            self._dirty = True
            return item["msg_count"]

    def mark_extracting(self, wxid: str) -> None:
        """在「决定排队抽取」时先打上时间戳。

        否则消息密集时 should_extract 会在上一次抽取完成前反复返回 True，
        把同一个人重复排队、重复消耗 API 调用。
        """
        if not wxid:
            return
        with self._lock:
            item = self._data.get(wxid)
            if item is not None:
                item["extracted_at"] = time.time()
                self._dirty = True

    def update_name(self, wxid: str, name: str = "", aliases=None) -> bool:
        """「更新昵称」用：刷新档案里记录的显示名与别名。"""
        if not wxid:
            return False
        with self._lock:
            item = self._data.get(wxid)
            if not item:
                return False
            changed = self._touch_name_locked(item, name, aliases)
            if changed:
                self._dirty = True
            return changed

    def _touch_name_locked(self, item: dict, name: str, aliases) -> bool:
        changed = False
        name = (name or "").strip()
        if name and name != item.get("name"):
            old = item.get("name")
            item["name"] = name
            changed = True
            if old:                                   # 旧名字留作别名
                item.setdefault("aliases", [])
                if old not in item["aliases"]:
                    item["aliases"] = ([old] + item["aliases"])[:10]
        for a in (aliases or []):
            a = (a or "").strip()
            if not a or a == item.get("name"):
                continue
            item.setdefault("aliases", [])
            if a not in item["aliases"]:
                item["aliases"] = ([a] + item["aliases"])[:10]
                changed = True
        return changed

    def should_extract(self, wxid: str, every_n: int, min_interval: float) -> bool:
        """是否该为这个人跑一次画像抽取（按消息数 + 时间双重节流）。"""
        if not wxid or every_n <= 0:
            return False
        with self._lock:
            item = self._data.get(wxid)
            if not item:
                return False
            count = int(item.get("msg_count") or 0)
            last = float(item.get("extracted_at") or 0)
        if count < every_n:
            return False
        if last and (time.time() - last) < min_interval:
            return False
        return (count % every_n == 0) or last == 0

    def merge(self, wxid: str, facts=None, summary: str = "",
              name: str = "", aliases=None) -> tuple:
        """合并一次抽取结果，返回 (新增事实数, 事实总数)。"""
        if not wxid:
            return 0, 0
        added = 0
        with self._lock:
            item = self._data.setdefault(wxid, {
                "name": "", "aliases": [], "summary": "", "facts": [],
                "msg_count": 0, "updated": time.time(), "extracted_at": 0.0,
            })
            existing = {self._norm_fact(f) for f in item.get("facts") or []}
            for f in (facts or []):
                f = (f or "").strip()
                if not f or len(f) > 120:
                    continue
                key = self._norm_fact(f)
                if not key or key in existing:
                    continue
                # 与已有事实高度重合也跳过（简单包含判定）
                if any(key in e or e in key for e in existing if e):
                    continue
                item.setdefault("facts", []).append(f)
                existing.add(key)
                added += 1
            if added:
                item["facts"] = item["facts"][-self.max_facts:]
            summary = (summary or "").strip()
            if summary:
                item["summary"] = summary[:300]
            self._touch_name_locked(item, name, aliases)
            item["extracted_at"] = time.time()
            item["updated"] = time.time()
            self._evict_locked()
            self._dirty = True
            total = len(item.get("facts") or [])
        return added, total

    @staticmethod
    def _norm_fact(text: str) -> str:
        return re.sub(r"[\s，。！？、：；,.!?:;\"'「」【】()（）]+", "", (text or "")).lower()

    def forget(self, wxid: str) -> bool:
        with self._lock:
            gone = self._data.pop(wxid, None) is not None
            if gone:
                self._dirty = True
        return gone

    def clear(self) -> int:
        with self._lock:
            n = len(self._data)
            self._data = {}
            self._dirty = False
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass
        return n

    def _evict_locked(self) -> None:
        over = len(self._data) - self.max_profiles
        if over <= 0:
            return
        order = sorted(self._data.items(), key=lambda kv: kv[1].get("updated", 0))
        for key, _ in order[:over]:
            self._data.pop(key, None)

    def keys(self) -> list:
        with self._lock:
            return list(self._data.keys())

    def stats(self) -> dict:
        with self._lock:
            n = len(self._data)
            facts = sum(len(v.get("facts") or []) for v in self._data.values())
        return {"profiles": n, "facts": facts, "path": self.path}

    def save_if_dirty(self, force: bool = False, min_interval: float = 30.0) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            now = time.time()
            if not force and now - self._last_save < min_interval:
                return False
            payload = {"version": self.VERSION, "saved_at": now,
                       "profiles": {k: dict(v) for k, v in self._data.items()}}
            self._dirty = False
            self._last_save = now
        if _atomic_write_json(self.path, payload):
            return True
        with self._lock:
            self._dirty = True
        return False


#: 画像抽取用的提示词：只要稳定事实，拒绝一次性寒暄与敏感信息
PROFILE_EXTRACT_PROMPT = (
    "你是一个信息抽取器。请从给定的微信聊天记录中，抽取「关于对方」的"
    "**稳定、长期有用**的信息，用于让助手长期记住这个人。\n"
    "只抽取这类信息：称呼/姓名、身份或与我的关系、明确表达的喜好与厌恶、"
    "重要的约定或时间节点、工作或学习背景、需要长期注意的事项。\n"
    "不要抽取：一次性的寒暄与情绪、临时状态、闲聊中的玩笑、"
    "以及任何敏感信息（证件号、密码、住址门牌、银行卡、精确生日）。\n"
    "每条事实用一句简短的陈述句，不要主语重复（写「喜欢喝冰美式」而不是"
    "「他说他喜欢喝冰美式」）。\n"
    "只输出 JSON，不要任何解释或代码块标记，格式："
    '{"facts": ["事实1", "事实2"], "summary": "一句话概括这个人"}'
    "\n如果确实没有值得长期记住的信息，输出："
    '{"facts": [], "summary": ""}'
)


def parse_json_reply(text: str) -> dict:
    """从模型回复里抠出 JSON（容忍 ```json 围栏与前后废话）。"""
    if not text:
        return {}
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# 对话记忆持久化（② 跨重启不丢）
# ---------------------------------------------------------------------------

class MemoryStore:
    """把每个会话的对话历史落盘，重启后恢复。

    - JSON 格式、原子写（临时文件 + os.replace），坏文件不会导致启动失败
    - 按会话数上限淘汰最久未更新的
    - 超过 max_age_hours 的记忆视为过期（避免拿一周前的上下文硬接话）
    - 写盘节流：内存里打脏标记，后台线程按间隔刷盘；停止时强制刷

    注意：该文件包含**聊天内容明文**，请勿泄露。
    """

    VERSION = 1

    def __init__(self, path: str, max_conversations: int = 200,
                 max_age_hours: float = 168.0):
        self.path = path
        self.max_conversations = max(1, int(max_conversations))
        self.max_age_hours = max(0.0, float(max_age_hours))
        self._data = {}                 # key -> {"updated": ts, "turns": [[role, text]]}
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0
        self.loaded_count = 0
        self.load()

    # -- 读 ---------------------------------------------------------------
    def load(self) -> int:
        obj = _read_json_file(self.path)
        if not obj:
            return 0
        convs = obj.get("conversations")
        if not isinstance(convs, dict):
            return 0
        now = time.time()
        fresh = {}
        for key, item in convs.items():
            if not isinstance(item, dict):
                continue
            raw = item.get("turns")
            if not isinstance(raw, list):
                continue
            try:
                updated = float(item.get("updated") or 0) or now
            except Exception:
                updated = now
            if self.max_age_hours and now - updated > self.max_age_hours * 3600:
                continue
            turns = []
            for pair in raw:
                if (isinstance(pair, (list, tuple)) and len(pair) == 2
                        and str(pair[0]) in ("user", "assistant")
                        and isinstance(pair[1], str) and pair[1]):
                    turns.append([str(pair[0]), pair[1]])
            turns = turns[-HISTORY_MAX_MESSAGES:]
            if turns:
                fresh[str(key)] = {"updated": updated, "turns": turns}
        with self._lock:
            self._data = fresh
            self._evict_locked()
            self.loaded_count = len(fresh)
        return len(fresh)

    # -- 写 ---------------------------------------------------------------
    def snapshot(self, key: str) -> list:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return []
            return [(r, t) for r, t in item["turns"]]

    def has(self, key: str) -> bool:
        with self._lock:
            return bool(self._data.get(key, {}).get("turns"))

    def keys(self) -> list:
        with self._lock:
            return list(self._data.keys())

    def update(self, key: str, turns) -> None:
        """记录某会话的最新历史（turns 为 (role, text) 序列）。"""
        rows = [[str(r), str(t)] for r, t in turns if t]
        rows = rows[-HISTORY_MAX_MESSAGES:]
        if not rows:
            return
        with self._lock:
            self._data[key] = {"updated": time.time(), "turns": rows}
            self._evict_locked()
            self._dirty = True

    def clear(self) -> int:
        """清空全部记忆（含文件）。返回清掉的会话数。"""
        with self._lock:
            n = len(self._data)
            self._data = {}
            self._dirty = False
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass
        return n

    def forget(self, key: str) -> bool:
        with self._lock:
            gone = self._data.pop(key, None) is not None
            if gone:
                self._dirty = True
        return gone

    def _evict_locked(self) -> None:
        over = len(self._data) - self.max_conversations
        if over <= 0:
            return
        order = sorted(self._data.items(), key=lambda kv: kv[1].get("updated", 0))
        for key, _ in order[:over]:
            self._data.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            convs = len(self._data)
            turns = sum(len(v["turns"]) for v in self._data.values())
        return {"conversations": convs, "messages": turns, "path": self.path,
                "dirty": self._dirty}

    def save_if_dirty(self, force: bool = False, min_interval: float = 30.0) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            now = time.time()
            if not force and now - self._last_save < min_interval:
                return False
            payload = {
                "version": self.VERSION,
                "saved_at": now,
                "conversations": {k: {"updated": v["updated"], "turns": v["turns"]}
                                  for k, v in self._data.items()},
            }
            self._dirty = False
            self._last_save = now
        if _atomic_write_json(self.path, payload):
            return True
        with self._lock:
            self._dirty = True              # 写失败，下次再试
        return False


class LLMClient:
    """OpenAI 兼容 /chat/completions 客户端。

    优先使用已安装的 openai SDK；否则回退到标准库 urllib（零额外依赖）。
    """

    #: 部分服务端对 max_tokens 上限很敏感（填过大会直接报错），统一做一次收敛
    MAX_TOKENS_CAP = 32768

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.7, max_tokens: int = 800,
                 timeout: float = 60):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.temperature = temperature
        try:
            mt = int(max_tokens)
        except Exception:
            mt = 800
        self.max_tokens = max(1, min(mt, self.MAX_TOKENS_CAP))
        self.timeout = timeout
        self._sdk = None
        self._sdk_tried = False
        self._lock = threading.Lock()

    # -- 端点 ---------------------------------------------------------------
    def _endpoint(self) -> str:
        url = self.base_url
        if not url:
            raise LLMError("未填写 API 地址（Base URL）")
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1") or "/v1/" in url:
            return url + "/chat/completions"
        # 裸域名（如 https://api.deepseek.com）补 /v1
        return url + "/v1/chat/completions"

    def _get_sdk(self):
        if self._sdk_tried:
            return self._sdk
        self._sdk_tried = True
        try:
            from openai import OpenAI  # type: ignore
            base = self.base_url
            if base.endswith("/chat/completions"):
                base = base[: -len("/chat/completions")]
            self._sdk = OpenAI(api_key=self.api_key, base_url=base or None,
                               timeout=self.timeout)
        except Exception:
            self._sdk = None
        return self._sdk

    # -- 调用 ---------------------------------------------------------------
    def chat(self, messages: list, retries: int = 2) -> str:
        if not self.model:
            raise LLMError("未填写模型名称")
        last_err = None
        for attempt in range(retries + 1):
            try:
                return self._chat_once(messages)
            except LLMError as exc:
                last_err = exc
                # 鉴权 / 参数错误重试无意义
                msg = str(exc)
                if any(k in msg for k in ("401", "403", "Unauthorized",
                                          "invalid_api_key", "not found",
                                          "404", "余额", "insufficient")):
                    break
            except Exception as exc:                       # 网络抖动等
                last_err = LLMError("%s: %s" % (type(exc).__name__, exc))
            if attempt < retries:
                time.sleep(1.0 + attempt)
        raise last_err or LLMError("大模型调用失败")

    def _chat_once(self, messages: list) -> str:
        sdk = self._get_sdk()
        if sdk is not None:
            try:
                resp = sdk.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                content = (resp.choices[0].message.content or "").strip()
                if content:
                    return content
                raise LLMError("大模型返回空内容")
            except LLMError:
                raise
            except Exception as exc:
                # SDK 失败后不再回退，把真实错误抛给用户
                raise LLMError("openai SDK 调用失败：%s" % exc)
        return self._chat_urllib(messages)

    def _chat_urllib(self, messages: list) -> str:
        import urllib.error
        import urllib.request

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
                "User-Agent": "wx-ai-bot/%s" % APP_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise LLMError("HTTP %s %s %s" % (exc.code, exc.reason, detail))
        except urllib.error.URLError as exc:
            raise LLMError("网络错误：%s" % exc.reason)

        try:
            obj = json.loads(raw)
        except Exception:
            raise LLMError("返回内容不是合法 JSON：%s" % raw[:200])

        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError("接口返回错误：%s" % detail)
        try:
            content = obj["choices"][0]["message"]["content"]
        except Exception:
            raise LLMError("无法解析返回结构：%s" % raw[:200])
        content = (content or "").strip()
        if not content:
            raise LLMError("大模型返回空内容")
        return content

    def test(self) -> str:
        """连通性测试，返回模型的回复文本。"""
        return self.chat([{"role": "user", "content": "你好，请只回复：连接成功"}],
                         retries=0)


# ---------------------------------------------------------------------------
# 机器人引擎（全部在后台线程运行）
# ---------------------------------------------------------------------------

class BotEngine:
    """监听微信新消息 → 调大模型 → 自动回复。"""

    SEEN_CAP = 3000

    def __init__(self, cfg: dict, emit, db=None):
        # 注意：这里**持有引用而非拷贝**。界面保存配置时会 update 同一个 dict，
        # 于是回复规则/开关类设置（@ 对方、冷却、触发模式、提示词、模型…）
        # 能在运行中即时生效，不需要重启机器人。
        # 仅「监听范围 / 轮询间隔 / 账号」在建立 Listener 时读取一次，需重启。
        self.cfg = cfg
        self.emit = emit                     # emit(kind:str, payload) 线程安全
        self._db = db                        # 可复用界面已初始化的 WeChatDB
        self._listener = None
        self._gui = None                     # WeChatGUI，惰性创建
        self._gui_lock = threading.Lock()
        self._stop = threading.Event()
        self._seen = OrderedDict()
        self._seen_lock = threading.Lock()
        self._last_reply_at = {}
        self._history = defaultdict(lambda: deque(maxlen=HISTORY_MAX_MESSAGES))
        self._history_lock = threading.RLock()
        self._send_q: "queue.Queue" = queue.Queue()
        self._llm_sem = threading.Semaphore(max(1, _to_int(cfg.get("llm_concurrency"), 2)))
        self._sender_thread = None
        self.self_names = set()
        self.self_wxid = ""
        self._name_cache = {}
        # 群昵称花名册：{chatroom: (读取时间, {wxid: 群昵称})}
        self._roster_cache = {}
        self._roster_lock = threading.Lock()
        # 对话记忆持久化
        self._memory = self._make_memory_store()
        self._memory_thread = None
        # ③ 长期记忆／用户画像
        self._profiles = self._make_profile_store()
        self._profile_q: "queue.Queue" = queue.Queue()
        self._profile_thread = None

        self.stats = {"in": 0, "out": 0, "sent": 0, "skip": 0, "err": 0,
                      "profile": 0}

    def _make_profile_store(self) -> "ProfileStore":
        cfg = self.cfg
        if not cfg.get("profile_enabled", True):
            return ProfileStore(os.devnull, 1, 1)
        path = (cfg.get("profile_file") or "").strip() or os.path.join(
            BASE_DIR, "wx_ai_bot_profiles.json")
        return ProfileStore(
            path,
            max_profiles=_to_int(cfg.get("memory_max_conversations"), 200),
            max_facts=_to_int(cfg.get("profile_max_facts"), 20),
        )

    def profile_stats(self) -> dict:
        return self._profiles.stats()

    def clear_profiles(self) -> int:
        return self._profiles.clear()

    # -- 一键更新昵称 -------------------------------------------------------
    def refresh_nicknames(self) -> dict:
        """清空所有昵称缓存并从数据库重读（群昵称 + 全局昵称 + 画像里的名字）。

        群昵称花名册平时缓存 %d 秒；改了群名片后想立刻生效就用这个。
        """ % int(self.ROSTER_TTL)
        with self._roster_lock:
            n_roster = len(self._roster_cache)
            self._roster_cache.clear()
        n_names = len(self._name_cache)
        self._name_cache.clear()

        db = self._db
        chats = []
        if db is not None:
            try:
                if (self.cfg.get("scope") or "selected").strip() == "all":
                    chats = [s["username"] for s in db.get_sessions(limit=50)
                             if s.get("username")]
                else:
                    chats = [t for t in (self.cfg.get("targets") or []) if t]
            except Exception as exc:
                self.log("更新昵称：读取会话失败（%s）" % exc, "warn")

        # 重读群昵称花名册
        groups = members = named = 0
        for chat in chats:
            if not chat.endswith("@chatroom"):
                continue
            roster = self._group_roster(chat)
            if roster:
                groups += 1
                members += len(roster)
                named += sum(1 for v in roster.values() if v)

        # 顺带把画像里记录的显示名刷新一遍（昵称会变，wxid 不会）
        updated = 0
        with self._roster_lock:
            rosters = {c: dict(r) for c, (_t, r) in self._roster_cache.items()}
        for wxid in self._profiles.keys():
            names = []
            for roster in rosters.values():
                nick = (roster.get(wxid) or "").strip()
                if nick and nick not in names:
                    names.append(nick)
            global_name = self._name_of(wxid)
            if global_name and global_name != wxid and global_name not in names:
                names.append(global_name)
            if not names:
                continue
            if self._profiles.update_name(wxid, names[0], names):
                updated += 1
        self._profiles.save_if_dirty(force=True)

        return {"cleared_names": n_names, "cleared_rosters": n_roster,
                "groups": groups, "members": members, "named": named,
                "profiles": updated}

    def _make_memory_store(self) -> "MemoryStore":
        cfg = self.cfg
        if not cfg.get("memory_enabled", True):
            # 关闭持久化：给一个「假路径」的内存存储，不会落盘
            return MemoryStore(os.devnull, 1, 0)
        path = (cfg.get("memory_file") or "").strip() or os.path.join(
            BASE_DIR, "wx_ai_bot_memory.json")
        return MemoryStore(
            path,
            max_conversations=_to_int(cfg.get("memory_max_conversations"), 200),
            max_age_hours=_to_float(cfg.get("memory_max_age_hours"), 168.0),
        )

    def memory_stats(self) -> dict:
        return self._memory.stats()

    def clear_memory(self) -> int:
        """清空记忆（界面「清除记忆」按钮）。"""
        with self._history_lock:
            self._history.clear()
        return self._memory.clear()

    def _restore_memory(self) -> int:
        """把落盘的历史装回内存（② 跨重启）。"""
        n = 0
        for key in self._memory.keys():
            turns = self._memory.snapshot(key)
            if not turns:
                continue
            with self._history_lock:
                buf = self._history[key]
                buf.clear()
                for role, text in turns[-HISTORY_MAX_MESSAGES:]:
                    buf.append((role, text))
            n += 1
        return n

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._sender_thread = threading.Thread(target=self._sender_loop,
                                               name="bot-sender", daemon=True)
        self._sender_thread.start()
        if self.cfg.get("memory_enabled", True):
            self._memory_thread = threading.Thread(target=self._memory_loop,
                                                  name="bot-memory", daemon=True)
            self._memory_thread.start()
        if self.cfg.get("profile_enabled", True):
            self._profile_thread = threading.Thread(target=self._profile_loop,
                                                   name="bot-profile", daemon=True)
            self._profile_thread.start()
        threading.Thread(target=self._boot, name="bot-boot", daemon=True).start()

    # -- 画像抽取（独立线程，绝不阻塞回复）---------------------------------
    def _profile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._profile_q.get(timeout=0.5)
            except queue.Empty:
                self._memory.save_if_dirty(
                    min_interval=max(5.0, _to_float(
                        self.cfg.get("memory_save_interval"), 30.0)))
                self._profiles.save_if_dirty(
                    min_interval=max(5.0, _to_float(
                        self.cfg.get("memory_save_interval"), 30.0)))
                continue
            if task is None:
                break
            try:
                self._extract_profile(*task)
            except Exception as exc:
                self._err("抽取用户画像", exc)
        try:
            self._profiles.save_if_dirty(force=True)
        except Exception:
            pass

    def _extract_profile(self, wxid: str, name: str, chatroom: str,
                         ctx_key: str) -> None:
        """让模型从该会话最近的对话里抽取「关于对方」的稳定事实。"""
        cfg = self.cfg
        with self._history_lock:
            recent = list(self._history.get(ctx_key, []))[-12:]
        if len(recent) < 4:
            return
        convo = "\n".join(
            ("我：%s" % t) if r == "assistant" else t for r, t in recent)
        label = name or self._name_of(wxid) or wxid
        messages = [
            {"role": "system", "content": PROFILE_EXTRACT_PROMPT},
            {"role": "user", "content":
                "对方的名字显示为「%s」。以下是最近的聊天记录：\n\n%s"
                % (label, _clip(convo, 3000))},
        ]
        client = LLMClient(
            base_url=cfg.get("base_url", ""), api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""), temperature=0.2,
            max_tokens=500, timeout=_to_float(cfg.get("timeout"), 60),
        )
        acquired = self._llm_sem.acquire(timeout=120)
        try:
            reply = client.chat(messages, retries=1)
        finally:
            if acquired:
                self._llm_sem.release()

        data = parse_json_reply(reply)
        facts = data.get("facts") if isinstance(data.get("facts"), list) else []
        summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
        added, total = self._profiles.merge(wxid, facts=facts, summary=summary,
                                           name=label)
        self.stats["profile"] += 1
        self.emit("stats", dict(self.stats))
        if added:
            self.log("更新人物档案「%s」：新增 %d 条事实（共 %d 条）"
                     % (label, added, total), "llm")
            for f in facts[:3]:
                self.log("    · %s" % _clip(str(f), 50), "llm")
        else:
            self.log("人物档案「%s」没有新增事实（共 %d 条）" % (label, total))
        self.emit("profiles", self._profiles.stats())

    def _maybe_queue_profile(self, wxid: str, name: str, chatroom: str,
                             ctx_key: str) -> None:
        if not self.cfg.get("profile_enabled", True) or not wxid:
            return
        every_n = max(1, _to_int(self.cfg.get("profile_every_n"), 10))
        min_iv = max(60.0, _to_float(self.cfg.get("profile_min_interval"), 1800))
        if not self._profiles.should_extract(wxid, every_n, min_iv):
            return
        self.log("排队抽取人物档案：%s（累计 %d 条消息，每 %d 条抽一次）"
                 % (name or wxid, self._profiles.get(wxid).get("msg_count", 0),
                    every_n))
        self._profiles.mark_extracting(wxid)      # 先占位，避免重复排队
        try:
            self._profile_q.put_nowait((wxid, name, chatroom, ctx_key))
        except Exception:
            pass

    # -- 画像注入 -----------------------------------------------------------
    def _profile_block(self, wxid: str, chatroom: str) -> str:
        """把长期记忆拼成一段注入系统提示词的文本。"""
        if not self.cfg.get("profile_inject", True) or not wxid:
            return ""
        prof = self._profiles.get(wxid)
        if not prof:
            return ""
        facts = prof.get("facts") or []
        summary = (prof.get("summary") or "").strip()
        if not facts and not summary:
            return ""
        lines = ["", "【关于对方的长期记忆】"]
        if summary:
            lines.append("整体印象：%s" % summary)
        if facts:
            lines.append("已知事实：")
            for f in facts[-int(self.cfg.get("profile_max_facts") or 20):]:
                lines.append("- %s" % f)
        if prof.get("aliases"):
            lines.append("见过的称呼：%s" % "、".join(prof["aliases"][:5]))
        lines.append("（以上为历史积累，仅供参考；与当前对话冲突时以当前对话为准。）")
        return "\n".join(lines)

    def _memory_loop(self) -> None:
        """定期把记忆刷盘（节流写，避免每轮对话都写文件）。"""
        interval = max(5.0, _to_float(self.cfg.get("memory_save_interval"), 30.0))
        while not self._stop.is_set():
            self._stop.wait(5.0)
            try:
                self._memory.save_if_dirty(min_interval=interval)
            except Exception:
                pass

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        # 停止前把记忆落盘，避免丢掉最后几轮对话
        try:
            self._memory.save_if_dirty(force=True)
            self._profiles.save_if_dirty(force=True)
        except Exception:
            pass
        try:
            self._profile_q.put_nowait(None)
        except Exception:
            pass
        lst = self._listener
        if lst is not None:
            try:
                lst.stop()
            except Exception:
                pass
            self._listener = None
        try:
            self._send_q.put_nowait(None)
        except Exception:
            pass
        if join and self._sender_thread is not None:
            self._sender_thread.join(timeout=3)
            self._sender_thread = None

    def is_stopping(self) -> bool:
        return self._stop.is_set()

    # -- 日志 ---------------------------------------------------------------
    def log(self, text: str, level: str = "info") -> None:
        self.emit("log", {"level": level, "text": text})

    def _err(self, where: str, exc: BaseException) -> None:
        self.stats["err"] += 1
        self.log("%s 失败：%s: %s" % (where, type(exc).__name__, exc), "error")
        self.emit("log", {"level": "debug", "text": traceback.format_exc()})
        self.emit("stats", dict(self.stats))

    # -- 启动流程 -----------------------------------------------------------
    def _boot(self) -> None:
        ensure_workdir()
        try:
            ensure_wechatauto_on_path()
            from wechatauto.db import Listener, WeChatDB
        except Exception as exc:
            self.log("无法导入 wechatauto：%s" % exc, "error")
            self.log(explain_import_error(exc), "warn")
            self.emit("running", False)
            return
        # wechatauto 的文件日志是惰性创建、失败会每次都重抛，这里先探一次
        note = preflight_wxlog()
        if note:
            self.log(note, "warn")

        # 1) 数据库（首次解密约 6 秒）
        try:
            db = self._db
            if db is None:
                self.log("正在读取微信数据库（首次需解密，约 6 秒）…")
                account = (self.cfg.get("account") or "").strip() or None
                db = WeChatDB(account=account)
                self._db = db
            info = db.get_self_info()
            self.self_wxid = (info or {}).get("username") or getattr(db, "wxid", "")
            self.self_names = {n for n in ((info or {}).get("nick_name"),
                                          (info or {}).get("remark")) if n}
            label = (info or {}).get("nick_name") or (info or {}).get("remark") or self.self_wxid
            self.emit("account", {"account": db.account, "nick": label,
                                  "wxid": self.self_wxid})
            self.log("数据库就绪，当前账号：%s" % label, "ok")
        except Exception as exc:
            self.log("读取微信数据库失败：%s" % exc, "error")
            self.log("请确认：① 微信 4.x 已登录；② Python 与微信同为 64 位；"
                     "③ 多账号时在界面选择正确账号。", "warn")
            self.emit("running", False)
            return

        if self._stop.is_set():
            self.emit("running", False)
            return

        # 2) 恢复记忆：先装回落盘的历史（②），再回填数据库里的近期对话（①）
        try:
            n_restored = self._restore_memory()
            if n_restored:
                self.log("已恢复上次的对话记忆：%d 个会话（重启不失忆）" % n_restored, "ok")
            elif self.cfg.get("memory_enabled", True):
                st = self._memory.stats()
                if st["conversations"] == 0:
                    self.log("没有历史记忆文件（首次运行，或记忆已过期/已清除）")
        except Exception as exc:
            self._err("恢复持久化记忆", exc)

        if self._stop.is_set():
            self.emit("running", False)
            return

        try:
            scope_now = (self.cfg.get("scope") or "selected").strip()
            targets_now = [t for t in (self.cfg.get("targets") or []) if t]
            self._warmup_from_db(db, targets_now, scope_now)
        except Exception as exc:
            self._err("启动回填历史", exc)

        # 3) 注册监听
        try:
            lst = Listener(db, interval=max(0.3, _to_float(self.cfg.get("interval"), 1.0)))
            scope = (self.cfg.get("scope") or "selected").strip()
            if scope == "all":
                lst.add_all(self._on_msg, discover=True)
                self.log("监听范围：全部会话（自动发现新会话）", "ok")
            else:
                targets = [t for t in (self.cfg.get("targets") or []) if t]
                if not targets:
                    self.log("未选择任何会话，已停止。请在「监听范围」里勾选会话，"
                             "或改用「全部会话」。", "error")
                    self.emit("running", False)
                    return
                for user in targets:
                    lst.add_listener(user, self._on_msg)
                names = "、".join(self._name_of(u) for u in targets[:5])
                self.log("监听 %d 个会话：%s%s"
                         % (len(targets), names, " …" if len(targets) > 5 else ""), "ok")
            lst.start()
            self._listener = lst
        except Exception as exc:
            self._err("启动监听", exc)
            self.emit("running", False)
            return

        self.log("机器人已启动，正在等待新消息…", "ok")
        self.emit("running", True)

    # -- 会话名解析 ---------------------------------------------------------
    def _name_of(self, username: str) -> str:
        """username → 可发送的会话显示名（发送依赖搜索框，必须用昵称）。"""
        if username in self._name_cache:
            return self._name_cache[username]
        name = username
        db = self._db
        if db is not None:
            try:
                got = db.get_nickname(username)
                if got and got != username:
                    name = got
                elif username.endswith("@chatroom"):
                    try:
                        gname = db.group_id_to_name(username)
                        if gname:
                            name = gname
                    except Exception:
                        pass
            except Exception:
                pass
        self._name_cache[username] = name
        return name

    # -- 群昵称（群名片）----------------------------------------------------
    ROSTER_TTL = 120.0          # 花名册缓存秒数（群名片会变，过期重读）

    def _group_roster(self, chatroom: str) -> dict:
        """读取并缓存群成员花名册 {wxid: 群昵称}（群昵称为空串表示未设群名片）。"""
        if not chatroom:
            return {}
        now = time.time()
        with self._roster_lock:
            cached = self._roster_cache.get(chatroom)
            if cached and now - cached[0] < self.ROSTER_TTL:
                return cached[1]
        roster = {}
        db = self._db
        if db is not None:
            try:
                conn = db._contact_conn()
                if conn:
                    try:
                        row = conn.execute(
                            "SELECT ext_buffer FROM chat_room WHERE username=? LIMIT 1",
                            (chatroom,)).fetchone()
                    finally:
                        conn.close()
                    if row is not None:
                        try:
                            blob = row["ext_buffer"]
                        except Exception:
                            blob = row[0]
                        roster = parse_group_members(blob)
            except Exception as exc:
                self.log("读取群昵称花名册失败（%s）：%s" % (chatroom, exc), "warn")
        with self._roster_lock:
            self._roster_cache[chatroom] = (now, roster)
        if roster:
            named = sum(1 for v in roster.values() if v)
            self.log("群 %s：读到 %d 名成员，其中 %d 人设了群昵称"
                     % (self._name_of(chatroom), len(roster), named))
        return roster

    def _group_display(self, wxid: str, chatroom: str) -> str:
        """某成员在指定群里的**实际显示名**（群昵称优先，回落全局昵称）。

        @ 人时必须用这个值 —— 群里 @ 出来的是群昵称。
        """
        wxid = (wxid or "").strip()
        if not wxid:
            return ""
        if chatroom:
            roster = self._group_roster(chatroom)
            nick = (roster.get(wxid) or "").strip()
            if nick:
                return nick
        return self._name_of(wxid)

    def _my_names_in(self, chatroom: str) -> set:
        """微信里「@我」可能出现的所有写法（群昵称 + 全局昵称/备注）。"""
        names = {n for n in self.self_names if n}
        if self.self_wxid:
            g = self._group_display(self.self_wxid, chatroom)
            if g and g != self.self_wxid:
                names.add(g)
        return names

    def _find_member(self, query: str, chatroom: str):
        """在群里按「群昵称 / 全局昵称 / 备注 / wxid」反查 wxid（供人物规则用）。"""
        q = (query or "").strip()
        if not q or not chatroom:
            return None
        roster = self._group_roster(chatroom)
        ql = q.lower()
        for wxid, nick in roster.items():
            if (nick or "").strip().lower() == ql:
                return wxid
        for wxid in roster:
            if self._name_of(wxid).lower() == ql:
                return wxid
        for wxid, nick in roster.items():
            if ql and (ql in (nick or "").lower() or ql in self._name_of(wxid).lower()):
                return wxid
        return None

    # -- 人物规则（识别特定人物 → 特定回答）--------------------------------
    def _rule_scope_ok(self, rule: dict, chatroom: str, is_group: bool) -> bool:
        """判断规则是否作用于当前会话。

        scope 支持：``*``/``all`` 全部、``group`` 仅群聊、``private`` 仅私聊、
        或**直接填群 wxid / 群名**（等价于 group:xxx）。
        """
        scope = (rule.get("scope") or "*").strip()
        if scope in ("*", "", "all", "全部", "any"):
            return True
        if scope in ("group", "群聊"):
            return is_group
        if scope in ("private", "私聊"):
            return not is_group
        if scope.startswith("group:") or scope.startswith("群:"):
            target = scope.split(":", 1)[1].strip()
        else:
            # 允许直接填群 wxid（xxx@chatroom）或群名
            target = scope
        if not target:
            return True
        if not is_group:
            return False
        return target == chatroom or target == self._name_of(chatroom)

    def _rule_who_matches(self, rule: dict, wxid: str, display: str,
                          chatroom: str) -> bool:
        """who 支持逗号分隔多个，任一等值/子串命中即可；'*' 表示任何人。"""
        who = (rule.get("who") or "").strip()
        if not who:
            return False
        if who == "*":
            return True
        # 允许直接把用户输入的「群昵称」当 wxid 之外的写法
        global_name = self._name_of(wxid) if wxid else ""
        pool = {x.lower() for x in
                (display or "", global_name or "", (wxid or "").strip()) if x}
        for part in re.split(r"[,，;；]", who):
            p = part.strip().lower()
            if not p:
                continue
            if p in pool:
                return True
            if any(p in x for x in pool):
                return True
        return False

    def _match_person_rule(self, chatroom: str, wxid: str, display: str):
        """返回第一条命中的规则（列表顺序即优先级），无则 None。"""
        rules = self.cfg.get("person_rules") or []
        if not isinstance(rules, list):
            return None
        is_group = (chatroom or "").endswith("@chatroom")
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if not self._rule_scope_ok(rule, chatroom, is_group):
                continue
            if self._rule_who_matches(rule, wxid, display, chatroom):
                return rule
        return None

    # -- 消息回调（运行在 Listener 的每会话工作线程里）-----------------------
    def _on_msg(self, msg: dict, _listener=None) -> None:
        try:
            self._handle(msg)
        except Exception as exc:
            self._err("处理消息", exc)

    def _handle(self, msg: dict) -> None:
        if self._stop.is_set():
            return

        username = msg.get("username") or ""
        content = msg.get("content") or ""
        mtype = msg.get("type")
        sender_id = msg.get("sender_id")

        # 去重（轮询可能重复投递）
        key = (username, msg.get("local_id"))
        with self._seen_lock:
            if key in self._seen:
                return
            self._seen[key] = time.time()
            while len(self._seen) > self.SEEN_CAP:
                self._seen.popitem(last=False)

        # 自己发出的消息
        if sender_id == 2:
            self.stats["skip"] += 1
            return

        # 消息类型过滤
        allowed = {"文本"}
        if self.cfg.get("extra_types"):
            allowed.add("文件/链接/卡片")
        if mtype not in allowed:
            self.stats["skip"] += 1
            return

        content = (content or "").strip()
        if not content or (content.startswith("[") and content.endswith("]")):
            self.stats["skip"] += 1
            return

        is_group = username.endswith("@chatroom")

        # 群聊：从正文前缀里取出真实发送者 wxid，并把前缀从正文里剥掉
        # （库里 real_sender_id 只给出群内序号，反查常得到 "8"/"10" 这类数字）
        prefix_wxid = ""
        if is_group:
            prefix_wxid, cleaned = split_group_sender(content)
            if prefix_wxid and cleaned:
                content = cleaned

        sender_label = self._sender_label(msg, username, is_group, prefix_wxid)
        sender_wxid = self._sender_wxid(msg, prefix_wxid, is_group, username)

        self.stats["in"] += 1
        self.emit("msg", {
            "time": _now(), "dir": "收到", "chat": self._name_of(username),
            "sender": sender_label, "content": _clip(content),
            "status": mtype,
        })
        self.emit("stats", dict(self.stats))

        # 人物规则：识别到特定人物时，可忽略 / 仅关键词 / 仅 @ / 换专属人设
        rule = self._match_person_rule(username, sender_wxid, sender_label)
        action = (rule.get("action") or "reply").strip() if rule else "reply"
        if rule:
            self.log("命中人物规则「%s」→ %s | %s | %s"
                     % (rule.get("who", ""), action,
                        self._name_of(username), sender_label), "llm")

        if action in ("ignore", "忽略", "不回复"):
            self.stats["skip"] += 1
            self.log("跳过（人物规则：忽略 %s）| %s" % (sender_label,
                                                     self._name_of(username)), "skip")
            self.emit("stats", dict(self.stats))
            return

        # 触发规则（人物规则可覆盖：keyword / at_only）
        if rule and action in ("keyword", "仅关键词"):
            kws = [k.strip() for k in (rule.get("keywords") or "").split(",") if k.strip()]
            if not kws or not any(k in content for k in kws):
                self.stats["skip"] += 1
                self.log("跳过（人物规则关键词未命中 %s）| %s | %s"
                         % (rule.get("keywords", ""), self._name_of(username),
                            sender_label), "skip")
                self.emit("stats", dict(self.stats))
                return
        elif rule and action in ("at_only", "仅@"):
            if is_group and not self._mentioned_me(content, username):
                self.stats["skip"] += 1
                self.log("跳过（人物规则：未被 @）| %s | %s"
                         % (self._name_of(username), sender_label), "skip")
                self.emit("stats", dict(self.stats))
                return
        else:
            ok, reason = self._should_reply(content, is_group, username)
            if not ok:
                self.stats["skip"] += 1
                self.log("跳过（%s）| %s | %s: %s"
                         % (reason, self._name_of(username), sender_label,
                            _clip(content, 40)), "skip")
                self.emit("stats", dict(self.stats))
                return

        # 冷却：群里默认按「每个人」分别计时，避免 A 说话后 B 被连坐丢弃
        cooldown = max(0.0, _to_float(self.cfg.get("cooldown"), 0.0))
        now = time.time()
        cool_key = self._cooldown_key(username, sender_wxid, is_group)
        last = self._last_reply_at.get(cool_key, 0.0)
        if cooldown and now - last < cooldown:
            self.stats["skip"] += 1
            self.log("跳过（冷却中 %.1fs）| %s | %s"
                     % (cooldown - (now - last), self._name_of(username),
                        sender_label), "skip")
            self.emit("stats", dict(self.stats))
            return

        # 上下文隔离键：默认整个会话共享；开启「按人隔离」后群内每人独立
        ctx_key = self._context_key(username, sender_wxid, is_group)

        # ③ 长期记忆：累计该人的消息数（达到阈值后排队抽取画像）
        if sender_wxid:
            names = []
            if is_group:
                nick = (self._group_roster(username).get(sender_wxid) or "").strip()
                if nick:
                    names.append(nick)
            gname = self._name_of(sender_wxid)
            if gname and gname != sender_wxid:
                names.append(gname)
            self._profiles.note_message(sender_wxid, names[0] if names else sender_label,
                                        names)
            self._maybe_queue_profile(sender_wxid, sender_label, username, ctx_key)

        # 调用大模型（人物规则可带专属人设 + 长期记忆注入）
        reply = self._ask_llm(ctx_key, sender_label, content, is_group,
                              rule=rule, person_wxid=sender_wxid,
                              chatroom=username)
        if not reply:
            return
        self._last_reply_at[cool_key] = time.time()

        # 入队发送：@ 的目标用「群昵称」（群里 @ 出来的是群昵称）
        mention_target = ""
        if is_group and sender_wxid:
            mention_target = self._group_display(sender_wxid, username)
        self._send_q.put({
            "username": username,
            "who": self._name_of(username),
            "sender": mention_target or sender_label,
            "sender_wxid": sender_wxid,
            "text": reply,
            "is_group": is_group,
        })
        self.log("已排队回复 → %s（队列 %d）"
                 % (self._name_of(username), self._send_q.qsize()))

    def _cooldown_key(self, username: str, sender_wxid: str, is_group: bool) -> str:
        """冷却计时键。群聊默认按人分开计时（可在界面关闭）。"""
        if is_group and self.cfg.get("cooldown_per_person", True) and sender_wxid:
            return "%s|%s" % (username, sender_wxid)
        return username

    def _context_key(self, username: str, sender_wxid: str, is_group: bool) -> str:
        """上下文（历史）隔离键。

        默认按「会话」隔离 —— 群与群之间、群与私聊之间天然互不串味。
        设为 person 时，同一群里每个成员各自独立上下文。
        """
        mode = (self.cfg.get("context_isolation") or "chat").strip()
        if mode == "person" and is_group and sender_wxid:
            return "%s|%s" % (username, sender_wxid)
        return username

    def _sender_wxid(self, msg: dict, prefix_wxid: str, is_group: bool,
                     username: str) -> str:
        """尽力解析发送者 wxid（用于规则匹配 / 群昵称查询 / 冷却与上下文分键）。"""
        if not is_group:
            return username
        if prefix_wxid:
            return prefix_wxid
        su = (msg.get("sender_username") or "").strip()
        return su if looks_like_wxid(su) else ""

    def _sender_label(self, msg: dict, username: str, is_group: bool,
                      prefix_wxid: str = "") -> str:
        """解析「谁发的」，**群聊里返回群昵称**（群里 @ 出来、成员看到的就是它）。

        群昵称为空则回落全局昵称；若只能拿到群内序号（"8"/"10"）则退化为
        「群成员」——绝不把数字 ID 当作昵称。
        """
        if not is_group:
            return self._name_of(username)

        wxid = self._sender_wxid(msg, prefix_wxid, is_group, username)
        if not wxid:
            return "群成员"
        roster = self._group_roster(username)
        nick = (roster.get(wxid) or "").strip()
        if nick:
            return nick
        if looks_like_wxid(wxid):
            name = self._name_of(wxid)
            if name and name != wxid:
                return name
        return "群成员"

    def _should_reply(self, content: str, is_group: bool,
                      chatroom: str = "") -> tuple:
        mode = (self.cfg.get("group_mode" if is_group else "private_mode") or "off").strip()
        if mode == "off":
            return False, "该类型会话已关闭回复"
        if mode == "keyword":
            kws = [k.strip() for k in (self.cfg.get("keywords") or "").split(",") if k.strip()]
            if not kws:
                return False, "未设置关键词"
            hit = next((k for k in kws if k in content), None)
            return (True, "") if hit else (False, "未命中关键词")
        if mode == "at" and is_group:
            return (True, "") if self._mentioned_me(content, chatroom) else (False, "未被 @")
        if mode == "at" and not is_group:
            return True, ""
        return True, ""

    def _mentioned_me(self, content: str, chatroom: str = "") -> bool:
        """是否被 @ 了。

        关键：群里 @ 出来的是**该群的群昵称**。机器人在某群的群昵称可能
        与它在别处的昵称完全不同（实测差异很大），
        只用全局昵称判断会漏掉所有 @。
        """
        if "@" not in content:
            return False
        names = self._my_names_in(chatroom) if chatroom else set(self.self_names)
        if names:
            for name in names:
                if name and ("@" + name) in content:
                    return True
            return False
        # 拿不到自己昵称时放宽：出现 @ 即视为在叫我
        return True

    def _ask_llm(self, ctx_key: str, sender_label: str, content: str,
                 is_group: bool, rule: dict = None, person_wxid: str = "",
                 chatroom: str = "") -> str:
        cfg = self.cfg
        client = LLMClient(
            base_url=cfg.get("base_url", ""),
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""),
            temperature=_to_float(cfg.get("temperature"), 0.7),
            max_tokens=_to_int(cfg.get("max_tokens"), 800),
            timeout=_to_float(cfg.get("timeout"), 60),
        )

        # 系统提示词：人物规则可「替换」或「追加」专属人设
        system = cfg.get("system_prompt") or ""
        if rule:
            persona = (rule.get("persona") or "").strip()
            if persona:
                pmode = (rule.get("persona_mode") or "append").strip()
                if pmode == "replace":
                    system = persona
                else:
                    system = (system + "\n\n" + persona).strip()
                self.log("使用专属人设（%s）：%s" % (pmode, _clip(persona, 40)))

        # ③ 长期记忆注入
        block = self._profile_block(person_wxid, chatroom)
        if block:
            system = (system + "\n" + block).strip()
            self.log("已注入人物档案（%s）" % (sender_label or person_wxid))

        turns = max(0, _to_int(cfg.get("context_turns"), 0))
        if turns > HISTORY_MAX_TURNS:
            self.log("上下文轮数 %d 超出上限，按 %d 轮处理（每轮 2 条消息，"
                     "总量上限 %d 条）" % (turns, HISTORY_MAX_TURNS,
                                        HISTORY_MAX_MESSAGES), "warn")
            turns = HISTORY_MAX_TURNS
        messages = [{"role": "system", "content": system}]
        if turns:
            with self._history_lock:
                recent = list(self._history[ctx_key])[-turns * 2:]
            for role, text in recent:
                messages.append({"role": role, "content": text})
        user_text = content if not is_group else "%s：%s" % (sender_label, content)
        messages.append({"role": "user", "content": user_text})

        t0 = time.time()
        self.log("请求大模型（%s）…" % cfg.get("model", ""))
        acquired = self._llm_sem.acquire(timeout=90)
        try:
            reply = client.chat(messages)
        except Exception as exc:
            self._err("调用大模型", exc)
            self.log("提示：检查 API 地址 / 密钥 / 模型名是否正确（可用「测试大模型」按钮）",
                     "warn")
            return ""
        finally:
            if acquired:
                self._llm_sem.release()

        reply = (reply or "").strip()
        limit = max(0, _to_int(cfg.get("max_reply_chars"), 0))
        if limit and len(reply) > limit:
            reply = reply[:limit].rstrip() + "…"
        if not reply:
            return ""

        if turns:
            with self._history_lock:
                self._history[ctx_key].append(("user", user_text))
                self._history[ctx_key].append(("assistant", reply))
                snapshot = list(self._history[ctx_key])
            # 标记进持久化存储（落盘由后台线程节流执行）
            self._memory.update(ctx_key, snapshot)

        self.stats["out"] += 1
        self.emit("stats", dict(self.stats))
        self.log("大模型回复（%.1fs）：%s" % (time.time() - t0, _clip(reply, 60)), "llm")
        return reply

    # -- ① 启动回填：从数据库读入「启动之前」的聊天记录 --------------------
    def _warmup_from_db(self, db, targets, scope: str) -> int:
        """把数据库里已有的最近消息回填成对话历史，让机器人一上线就认识对方。

        只有「没有可用持久化记忆」的会话才回填（持久化记忆更新鲜）。
        返回回填的会话数。
        """
        n = _to_int(self.cfg.get("warmup_messages"), 20)
        if n <= 0:
            return 0
        hours = _to_float(self.cfg.get("warmup_hours"), 72.0)
        max_chats = max(1, _to_int(self.cfg.get("warmup_max_chats"), 20))
        cutoff = (time.time() - hours * 3600) if hours > 0 else 0.0

        if scope == "all":
            try:
                chats = [s["username"] for s in db.get_sessions(limit=max_chats)
                         if s.get("username")]
            except Exception as exc:
                self.log("回填：读取会话列表失败（%s）" % exc, "warn")
                return 0
        else:
            chats = [t for t in (targets or []) if t][:max_chats]

        person_mode = (self.cfg.get("context_isolation") or "chat") == "person"
        seeded_chats = 0
        seeded_msgs = 0
        for chat in chats:
            is_group = chat.endswith("@chatroom")
            # 该会话已有持久化记忆 → 以它为准，不覆盖
            if not person_mode and self._memory.has(self._context_key(chat, "", is_group)):
                continue
            # 多取一些再筛：群里图片/表情/链接很多，若只取 n 条，
            # 过滤成纯文本后往往只剩两三条，等于没回填。
            # 这里按「回填 n 条**文本**消息」为目标扩大取样池。
            pool = min(max(n * 4, n + 10), 500)
            try:
                msgs = db.get_messages(chat, limit=pool)
            except Exception as exc:
                self.log("回填 %s 失败：%s" % (self._name_of(chat), exc), "warn")
                continue
            if not msgs:
                continue

            # 先过滤出可用的文本消息，再只保留最近 n 条
            # （n 的语义是「回填多少条文本消息」，而非「读多少条原始消息」）
            items = []
            for m in msgs:
                if m.get("type") != "文本":
                    continue
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                try:
                    ts = float(m.get("create_time") or 0)
                except Exception:
                    ts = 0.0
                if cutoff and ts and ts < cutoff:
                    continue

                prefix = ""
                if is_group:
                    prefix, cleaned = split_group_sender(content)
                    if prefix and cleaned:
                        content = cleaned
                mine = (m.get("sender_id") == 2)
                wxid = "" if mine else self._sender_wxid(m, prefix, is_group, chat)

                if mine:
                    role, text = "assistant", content
                else:
                    if is_group:
                        label = ""
                        if wxid:
                            label = (self._group_roster(chat).get(wxid) or "").strip() \
                                or self._name_of(wxid)
                        text = ("%s：%s" % (label, content)) if label and label != wxid \
                            else content
                    else:
                        text = content
                    role = "user"

                items.append((self._context_key(chat, wxid, is_group), role, text))

            items = items[-n:]

            # 按上下文键分组：chat 模式全归一个键；person 模式群内每人一个键
            buckets = defaultdict(list)
            for key, role, text in items:
                # 相邻同角色消息合并，减少 token 且更接近模型预期
                buf = buckets[key]
                if buf and buf[-1][0] == role:
                    buf[-1] = [role, buf[-1][1] + "\n" + text]
                else:
                    buf.append([role, text])

            for key, turns in buckets.items():
                turns = turns[-HISTORY_MAX_MESSAGES:]
                if not turns:
                    continue
                with self._history_lock:
                    existing = self._history.get(key)
                    if existing:            # 已有（例如另一路径已填充）不覆盖
                        continue
                    buf = self._history[key]
                    for role, text in turns:
                        buf.append((role, text))
                self._memory.update(key, turns)
                seeded_msgs += len(turns)
            if buckets:
                seeded_chats += 1

        if seeded_msgs:
            self.log("已从数据库回填 %d 个会话的最近对话（共 %d 条消息）——"
                     "机器人现在知道启动前的聊天内容了"
                     % (seeded_chats, seeded_msgs), "ok")
        elif n > 0:
            self.log("回填：没有找到可用的近期消息（时限 %.0f 小时内）" % hours)
        return seeded_chats

    # -- 发送线程（串行 + 节流，避免 GUI 操作打架）--------------------------
    def _get_gui(self):
        ensure_workdir()          # wechatauto 的日志/下载目录都是相对 cwd 的
        with self._gui_lock:
            if self._gui is not None:
                return self._gui
            from wechatauto.guia import WeChatGUI
            try:
                self._gui = WeChatGUI()
            except RuntimeError as exc:
                # 微信最小化/收进托盘时库会找不到主窗口 —— 先尝试恢复再重试一次
                if "未找到微信主窗口" not in str(exc):
                    raise
                self.log("微信主窗口不可见，尝试恢复…", "warn")
                if restore_wechat_window():
                    self._gui = WeChatGUI()
                else:
                    state = describe_wechat_window()
                    if state:
                        raise RuntimeError(
                            "%s。\nGUI 发送必须能看到微信窗口 —— "
                            "请点任务栏托盘里的微信图标把主窗口显示出来"
                            "（不要最小化、也不要关到托盘）。" % state)
                    raise
            return self._gui

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._send_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._do_send(item)
            except Exception as exc:
                self._err("发送消息", exc)
                self.emit("msg", {
                    "time": _now(), "dir": "发送", "chat": item.get("who", ""),
                    "sender": "机器人", "content": _clip(item.get("text", "")),
                    "status": "异常",
                })
            gap = max(0.0, _to_float(self.cfg.get("send_interval"), 0.0))
            if gap and not self._stop.is_set():
                self._stop.wait(gap)

    def _do_send(self, item: dict) -> None:
        text, who = item["text"], item["who"]
        verify = bool(self.cfg.get("verify_send"))
        t0 = time.time()

        try:
            gui = self._get_gui()
        except Exception as exc:
            msg = "%s" % exc
            # 区分「真的没窗口」和「初始化因别的原因失败」——
            # 以前一律报「微信窗口不可用」，会把文件权限之类的故障误导成窗口问题。
            if ("WinError 5" in msg or "拒绝访问" in msg or "Access is denied" in msg
                    or isinstance(exc, PermissionError)):
                self.log("初始化微信驱动失败（文件系统权限）：%s" % exc, "error")
                self.log("原因：wechatauto 的日志目录/下载目录是相对「当前工作目录」的，"
                         "从不可写的目录启动就会失败。", "warn")
                self.log("本程序已自动切到脚本目录：%s" % BASE_DIR, "warn")
                status_txt = "初始化失败(权限)"
            elif "未找到微信主窗口" in msg or "微信主窗口" in msg or "被「隐藏」" in msg:
                self.log("微信窗口不可用：%s" % msg, "error")
                status_txt = "微信窗口不可见"
            else:
                self.log("初始化微信驱动失败：%s" % exc, "error")
                status_txt = "初始化失败"
            self.log("发送依赖 GUI 操作：微信需已登录、窗口未最小化、桌面未锁定。", "warn")
            self.emit("msg", {
                "time": _now(), "dir": "发送", "chat": who, "sender": "机器人",
                "content": _clip(text), "status": status_txt,
            })
            return

        # 「@ 对方」现在走 **UIA 路径**（真实按键触发弹层 + UIA 点成员）。
        # 库里的 gui.at_member 走 OCR + 坐标，实测**弹层根本不会出现**
        # （它用 type_unicode('@') 注入 Unicode 字符，微信不认作 @ 触发），
        # 那条路必然失败，所以主路径换成 UIA，失败再退回普通发送。
        outcome = None            # (ok, status, detail)
        if item.get("is_group") and self.cfg.get("mention_back"):
            member = (item.get("sender") or "").strip()
            if not member or member == "群成员" or member.isdigit():
                self.log("群成员名不可用（%r），跳过 @ 直接普通发送" % member, "warn")
            else:
                cands = [member]
                wxid = (item.get("sender_wxid") or "").strip()
                if wxid:
                    for fn in (lambda: self._group_display(wxid, item.get("username") or ""),
                               lambda: self._name_of(wxid)):
                        try:
                            got = fn()
                            if got:
                                cands.append(got)
                        except Exception:
                            pass
                cands = [c for c in dict.fromkeys(cands) if c]
                try:
                    r = uia_mention_send(gui, cands, text, who=who)
                except Exception as exc:
                    r = {"ok": False, "detail": "@ 发送异常：%s: %s"
                         % (type(exc).__name__, exc)}
                if r.get("ok"):
                    outcome = (True, "成功", r.get("detail") or "已发送")
                    self.log("@ 提及发送成功：%s（候选 %s）"
                             % (r.get("matched"), cands))
                else:
                    self.log("@ 成员发送未成功（%s），清理输入框后回退普通发送"
                             % r.get("detail"), "warn")
                    clear_leftover_input(gui)

        if outcome is None:
            ok, status, detail = self._read_response(gui.send_msg(text, who, verify=verify))
        else:
            ok, status, detail = outcome
        if ok:
            self.stats["sent"] += 1
            self.log("回复已发送 → %s（%.1fs）%s"
                     % (who, time.time() - t0, detail), "ok")
        else:
            # 关键：失败必须说「失败」。旧版无论成败都打印「已发送」，
            # 会把「@ 成员定位失败导致消息被丢弃」伪装成发送成功。
            self.stats["err"] += 1
            self.log("回复发送失败 → %s（%.1fs）状态=%s 详情=%s"
                     % (who, time.time() - t0, status, detail or "(无)"), "error")
            self.log("  排查：① 微信窗口是否可见、桌面是否锁定；"
                     "② 目标会话名是否与微信里一致；"
                     "③ 若开了「群聊回复时 @ 对方」，可先关掉它。", "warn")
            self.emit("hint", "发送失败：%s" % (detail or status))
        self.emit("stats", dict(self.stats))
        self.emit("msg", {
            "time": _now(), "dir": "发送", "chat": who, "sender": "机器人",
            "content": _clip(text), "status": status if ok else "失败",
        })

    @staticmethod
    def _read_response(result) -> tuple:
        """把 WxResponse 解析成 (是否成功, 状态文本, 详细说明)。

        wechatauto 的 WxResponse 是 dict 子类，status ∈ {成功, 失败, 错误}，
        并带 is_success 属性与 __bool__。
        """
        if result is None:
            return False, "无返回", ""
        try:
            status = str(result["status"])
            detail = str(result.get("message") or "")
        except Exception:
            return bool(result), str(result)[:40], ""
        try:
            ok = bool(result.is_success)
        except Exception:
            ok = status == "成功"
        return ok, status, detail

    # -- 供界面调用的辅助查询 ----------------------------------------------
    def get_db(self):
        return self._db


# ---------------------------------------------------------------------------
# 环境自检（--check）
# ---------------------------------------------------------------------------

def run_check() -> int:
    _setup_console()
    ensure_workdir()
    print("=" * 68)
    print("%s v%s —— 环境自检" % (APP_NAME, APP_VERSION))
    print("=" * 68)
    print("Python      : %s" % sys.version.split()[0])
    print("解释器位数  : %s" % (64 if sys.maxsize > 2 ** 32 else 32))
    print("工作目录    : %s" % BASE_DIR)

    ok = True
    print("\n[1] 图形界面")
    try:
        import tkinter  # noqa: F401
        print("    tkinter     OK")
    except Exception as exc:
        ok = False
        print("    tkinter     缺失（%s）—— 需安装带 Tk 的 Python" % exc)

    print("\n[2] wechatauto-replica")
    ensure_wechatauto_on_path()
    _flush_streams()
    try:
        import wechatauto
        print("    wechatauto  OK  版本 %s" % wechatauto.__version__)
        print("    位置        %s" % os.path.dirname(wechatauto.__file__))
    except Exception as exc:
        ok = False
        print("    wechatauto  导入失败：%s" % exc)
        print("    → %s" % explain_import_error(exc))
        if not wechatauto_on_path():
            print("    （既未 pip 安装，sys.path 上也没找到该仓库目录）")

    print("\n[3] 第三方依赖")
    deps = [
        ("uiautomation", "UIA 控件树（发送主路径）", True),
        ("win32gui", "pywin32，窗口/剪贴板操作", True),
        ("pyperclip", "剪贴板输入", True),
        ("PIL", "Pillow，截图/OCR 前置", True),
        ("psutil", "进程内存扫描（提取数据库密钥）", True),
        ("colorama", "wechatauto 日志着色", True),
        ("cryptography", "SQLCipher 解密", True),
        ("zstandard", "长文本 zstd 解压", True),
        ("pyautogui", "鼠标操作（朋友圈/引用消息）", True),
        ("cv2", "opencv，模板匹配与 OCR 预处理", True),
        ("winsdk", "Windows OCR（发送兜底路径）", True),
        ("imageio_ffmpeg", "语音/视频转码", True),
        ("pypinyin", "会话名拼音搜索", False),
        ("openai", "OpenAI SDK（可选，不装走标准库）", False),
    ]
    missing_required = []
    for mod, desc, required in deps:
        _flush_streams()
        try:
            __import__(mod)
            print("    %-14s OK    %s" % (mod, desc))
        except Exception:
            flag = "必需" if required else "可选"
            print("    %-14s 缺失  %s（%s）" % (mod, flag, desc))
            if required:
                missing_required.append(mod)
    if missing_required:
        ok = False
        print("\n    推荐一步到位（在 wechatauto-replica 仓库目录执行）：")
        print("      pip install -e .")
        print("    或只补缺失项：")
        print("      pip install %s" % " ".join(
            {"PIL": "Pillow", "cv2": "opencv-python", "win32gui": "pywin32",
             "imageio_ffmpeg": "imageio-ffmpeg"}.get(m, m)
            for m in missing_required))

    print("\n[4] 微信客户端")
    try:
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW(None, "微信")
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW(None, "Weixin")
        print("    主窗口      %s" % ("已找到 hwnd=%s" % hwnd if hwnd
                                    else "未找到（微信未运行或未登录）"))
    except Exception as exc:
        print("    主窗口      检测失败：%s" % exc)

    print("\n[5] 配置文件")
    print("    配置        %s%s" % (CONFIG_PATH,
                                   "" if os.path.exists(CONFIG_PATH) else "（尚未生成）"))
    print("    日志        %s" % LOG_PATH)

    print("\n" + "=" * 68)
    print("结论：%s" % ("环境就绪，可运行 python wx_ai_bot.py" if ok
                       else "存在缺失项，请按上面提示补齐后再运行"))
    print("=" * 68)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------

def main() -> int:
    ensure_workdir()      # 必须在导入 wechatauto 之前，否则它的相对路径会指向别处
    ensure_wechatauto_on_path()
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, simpledialog, ttk
    except Exception as exc:
        print("无法加载 tkinter：%s" % exc)
        return 1

    # 高 DPI 适配（避免界面模糊）
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    cfg = load_config()

    # ---------------- 事件队列（后台线程 → 界面线程）----------------------
    events: "queue.Queue" = queue.Queue()

    def emit(kind: str, payload) -> None:
        try:
            events.put_nowait((kind, payload))
        except Exception:
            pass

    # ---------------- 主窗口 ---------------------------------------------
    root = tk.Tk()
    root.title("%s  v%s" % (APP_NAME, APP_VERSION))
    root.geometry("1180x780")
    root.minsize(980, 660)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    base_font = ("Microsoft YaHei UI", 9)
    root.option_add("*Font", base_font)
    style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Treeview", rowheight=22, font=base_font)
    style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Run.TButton", font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("Ok.TLabel", foreground="#0a7d28")
    style.configure("Bad.TLabel", foreground="#b32020")
    style.configure("Muted.TLabel", foreground="#666666")

    engine_holder = {"engine": None, "db": None}

    # ---------------- 顶部状态条 -----------------------------------------
    top = ttk.Frame(root, padding=(10, 8, 10, 4))
    top.pack(fill="x")

    ttk.Label(top, text="微信：").pack(side="left")
    lbl_wx = ttk.Label(top, text="未检测", style="Muted.TLabel")
    lbl_wx.pack(side="left", padx=(0, 14))

    ttk.Label(top, text="账号：").pack(side="left")
    lbl_acct = ttk.Label(top, text="-", style="Muted.TLabel")
    lbl_acct.pack(side="left", padx=(0, 14))

    ttk.Label(top, text="状态：").pack(side="left")
    lbl_run = ttk.Label(top, text="已停止", style="Bad.TLabel")
    lbl_run.pack(side="left", padx=(0, 14))

    lbl_stats = ttk.Label(top, text="收到 0 | 回复 0 | 已发送 0 | 跳过 0 | 错误 0",
                          style="Muted.TLabel")
    lbl_stats.pack(side="right")

    # ---------------- 主体：左设置 / 右视图 ------------------------------
    body = ttk.Frame(root, padding=(10, 0, 10, 0))
    body.pack(fill="both", expand=True)

    left_outer = ttk.Frame(body, width=430)
    left_outer.pack(side="left", fill="y", padx=(0, 8))
    left_outer.pack_propagate(False)

    # 可滚动的设置面板
    canvas = tk.Canvas(left_outer, borderwidth=0, highlightthickness=0, width=410)
    vsb = ttk.Scrollbar(left_outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    left = ttk.Frame(canvas)
    canvas.create_window((0, 0), window=left, anchor="nw")
    left.bind("<Configure>",
              lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfigure(canvas.find_all()[0], width=e.width))

    def _wheel(event):
        canvas.yview_scroll(int(-event.delta / 120), "units")

    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

    def entry(parent, key, width=16, show=None):
        var = tk.StringVar(value=str(cfg.get(key, "")))
        widget = ttk.Entry(parent, textvariable=var, width=width,
                           show=show if show else "")
        return var, widget

    # ===== 分组 1：微信连接 =====
    g1 = ttk.LabelFrame(left, text=" ① 微信连接 ", padding=8)
    g1.pack(fill="x", pady=(2, 8))

    row = ttk.Frame(g1)
    row.pack(fill="x", pady=2)
    ttk.Label(row, text="账号", width=10).pack(side="left")
    var_account = tk.StringVar(value=cfg.get("account", ""))
    cmb_account = ttk.Combobox(row, textvariable=var_account, width=24)
    cmb_account.pack(side="left", fill="x", expand=True)
    ttk.Label(g1, text="留空 = 自动选择最近使用的账号",
              style="Muted.TLabel").pack(anchor="w")

    row = ttk.Frame(g1)
    row.pack(fill="x", pady=(4, 2))
    btn_load = ttk.Button(row, text="载入会话列表")
    btn_load.pack(side="left")
    btn_refresh_names = ttk.Button(row, text="更新昵称")
    btn_refresh_names.pack(side="left", padx=6)
    lbl_load = ttk.Label(row, text="", style="Muted.TLabel")
    lbl_load.pack(side="left", padx=6)
    ttk.Label(g1, text="「更新昵称」= 立刻重读群昵称与备注（平时缓存 120 秒），\n"
                       "微信里刚改的群名片/备注可立即生效。",
              style="Muted.TLabel", justify="left").pack(anchor="w")

    # ===== 分组 2：监听范围 =====
    g2 = ttk.LabelFrame(left, text=" ② 监听范围 ", padding=8)
    g2.pack(fill="x", pady=(2, 8))

    var_scope = tk.StringVar(value=cfg.get("scope", "selected"))
    row = ttk.Frame(g2)
    row.pack(fill="x")
    ttk.Radiobutton(row, text="全部会话", value="all",
                    variable=var_scope).pack(side="left", padx=(0, 12))
    ttk.Radiobutton(row, text="指定会话（下方多选）", value="selected",
                    variable=var_scope).pack(side="left")

    list_wrap = ttk.Frame(g2)
    list_wrap.pack(fill="both", expand=True, pady=(4, 2))
    # exportselection=False：避免选中被系统"导出"为选区（防御性设置）
    lb_sessions = tk.Listbox(list_wrap, selectmode="extended", height=8,
                             exportselection=False, activestyle="none",
                             font=("Microsoft YaHei UI", 9),
                             selectbackground="#3d7fd6", selectforeground="#ffffff")
    lb_sessions.pack(side="left", fill="both", expand=True)
    sb2 = ttk.Scrollbar(list_wrap, orient="vertical", command=lb_sessions.yview)
    sb2.pack(side="right", fill="y")
    lb_sessions.configure(yscrollcommand=sb2.set)

    session_map = []          # [(username, display)]
    lb_populated = {"done": False}   # 列表是否已被真实会话填充过

    row = ttk.Frame(g2)
    row.pack(fill="x", pady=(2, 0))
    lbl_sel = ttk.Label(row, text="", style="Muted.TLabel")
    lbl_sel.pack(side="left")
    btn_sel_all = ttk.Button(row, text="全选", width=6)
    btn_sel_all.pack(side="right", padx=2)
    btn_sel_none = ttk.Button(row, text="清空选择", width=9)
    btn_sel_none.pack(side="right", padx=2)

    ttk.Label(g2, text="多选：直接点击会话即可选中/取消（可连续点多个）；\n"
                       "Shift+点击 连选一段；也可用下方「全选」。「全部会话」模式下列表不可用。",
              style="Muted.TLabel", justify="left").pack(anchor="w")

    row = ttk.Frame(g2)
    row.pack(fill="x", pady=(4, 0))
    ttk.Label(row, text="轮询间隔(秒)", width=12).pack(side="left")
    var_interval, ent_interval = entry(row, "interval", width=8)
    ent_interval.pack(side="left")

    def populate_lb(items) -> None:
        """把会话填进列表。

        ⚠️ 必须先置为 normal：Tk 的 Listbox 在 disabled 状态下
        ``insert`` / ``delete`` 会被**静默忽略**（不报错、不生效）。
        而「全部会话」模式会把列表禁用 —— 于是「载入会话列表」看起来成功了，
        实际一条都没进去，切回「指定会话」后是个空列表，什么也选不了。
        """
        try:
            lb_sessions.configure(state="normal")
        except Exception:
            pass
        lb_sessions.delete(0, "end")
        for u, disp in items:
            lb_sessions.insert("end", "%s    [%s]" % (disp, u))
        got = lb_sessions.size()
        if items and got != len(items):
            log("会话列表填充异常：应有 %d 条，实际 %d 条" % (len(items), got), "error")
        update_scope_ui()          # 按当前模式恢复禁用状态

    def update_scope_ui(*_a) -> None:
        """按当前模式刷新列表可用状态与「已选 N 个」提示。"""
        is_sel = var_scope.get() == "selected"
        try:
            lb_sessions.configure(state="normal" if is_sel else "disabled")
        except Exception:
            pass
        n = len(lb_sessions.curselection())
        if not is_sel:
            lbl_sel.configure(text="当前监听全部会话（列表不生效）",
                              style="Muted.TLabel")
        elif n == 0:
            lbl_sel.configure(text="尚未选择任何会话 —— 启动会被拦下",
                              style="Bad.TLabel")
        else:
            lbl_sel.configure(text="已选 %d 个会话" % n, style="Ok.TLabel")

    def on_lb_select(_e=None) -> None:
        update_scope_ui()

    def on_lb_click(event):
        """单击即切换选中 —— 让「多选」不需要知道要按 Ctrl。

        Tk 的 extended 模式默认「单击替换整个选中」，必须按 Ctrl 才能多选，
        这里几乎没人猜得到。改成：单击 = 选中/取消该项；Shift 单击保留原生连选。
        """
        if event.state & 0x0001:            # Shift：交给原生连选
            root.after_idle(update_scope_ui)
            return None
        idx = lb_sessions.nearest(event.y)
        if idx < 0 or idx >= lb_sessions.size():
            return "break"
        box = lb_sessions.bbox(idx)
        if not box or not (box[1] <= event.y <= box[1] + box[3]):
            return "break"                  # 点在条目之间的空白处
        try:
            lb_sessions.focus_set()
        except Exception:
            pass
        if idx in lb_sessions.curselection():
            lb_sessions.selection_clear(idx)
        else:
            lb_sessions.selection_set(idx)
        update_scope_ui()
        return "break"                      # 阻止默认的「清掉其它选中」

    lb_sessions.bind("<Button-1>", on_lb_click)
    lb_sessions.bind("<<ListboxSelect>>", on_lb_select)
    var_scope.trace_add("write", lambda *a: update_scope_ui())
    update_scope_ui()

    # ===== 分组 3：大模型 API =====
    g3 = ttk.LabelFrame(left, text=" ③ 大模型 API ", padding=8)
    g3.pack(fill="x", pady=(2, 8))

    def labeled_row(parent, label, key, width=30, show=None):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=12).pack(side="left")
        v, w = entry(r, key, width=width, show=show)
        w.pack(side="left", fill="x", expand=True)
        return v, w

    var_base, _ = labeled_row(g3, "API 地址", "base_url")
    var_key, ent_key = labeled_row(g3, "API Key", "api_key", show="*")

    r = ttk.Frame(g3)
    r.pack(fill="x", pady=2)
    var_showkey = tk.BooleanVar(value=False)

    def toggle_key():
        ent_key.configure(show="" if var_showkey.get() else "*")

    ttk.Checkbutton(r, text="显示密钥", variable=var_showkey,
                    command=toggle_key).pack(side="left", padx=(112, 0))

    var_model, _ = labeled_row(g3, "模型名称", "model")
    var_savekey = tk.BooleanVar(value=bool(cfg.get("save_api_key", True)))
    ttk.Checkbutton(g3, text="保存密钥到本地配置文件（明文）",
                    variable=var_savekey).pack(anchor="w")

    # ---- 提示词预设：快速切换 ----
    presets = dict(DEFAULT_PROMPT_PRESETS)
    saved_presets = cfg.get("prompt_presets")
    if isinstance(saved_presets, dict):
        presets.update({k: v for k, v in saved_presets.items() if isinstance(v, str)})

    r = ttk.Frame(g3)
    r.pack(fill="x", pady=(6, 2))
    ttk.Label(r, text="提示词预设", width=12).pack(side="left")
    var_preset = tk.StringVar(value="")
    cmb_preset = ttk.Combobox(r, textvariable=var_preset, values=list(presets),
                              state="readonly", width=18)
    cmb_preset.pack(side="left", padx=(0, 6))
    btn_preset_load = ttk.Button(r, text="载入")
    btn_preset_load.pack(side="left", padx=2)
    btn_preset_save = ttk.Button(r, text="另存为…")
    btn_preset_save.pack(side="left", padx=2)
    btn_preset_del = ttk.Button(r, text="删除")
    btn_preset_del.pack(side="left", padx=2)

    r = ttk.Frame(g3)
    r.pack(fill="x", pady=(4, 2))
    ttk.Label(r, text="系统提示词", width=12).pack(side="left", anchor="n")
    txt_prompt = tk.Text(r, height=7, wrap="word", font=("Microsoft YaHei UI", 9))
    txt_prompt.pack(side="left", fill="both", expand=True)
    txt_prompt.insert("1.0", cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)

    r = ttk.Frame(g3)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="温度", width=8).pack(side="left")
    var_temp, ent_temp = entry(r, "temperature", width=6)
    ent_temp.pack(side="left", padx=(0, 10))
    ttk.Label(r, text="最大 tokens", width=12).pack(side="left")
    var_maxtok, ent_maxtok = entry(r, "max_tokens", width=7)
    ent_maxtok.pack(side="left", padx=(0, 10))
    ttk.Label(r, text="超时(秒)").pack(side="left")
    var_timeout, ent_timeout = entry(r, "timeout", width=6)
    ent_timeout.pack(side="left")

    # ===== 分组 4：回复规则 =====
    g4 = ttk.LabelFrame(left, text=" ④ 回复规则 ", padding=8)
    g4.pack(fill="x", pady=(2, 8))

    MODES_PRIVATE = {"全部回复": "all", "仅关键词": "keyword", "不回复": "off"}
    MODES_GROUP = {"仅 @我 时回复": "at", "全部回复": "all",
                   "仅关键词": "keyword", "不回复": "off"}

    def rev(d, val):
        for k, v in d.items():
            if v == val:
                return k
        return list(d)[0]

    r = ttk.Frame(g4)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="私聊", width=12).pack(side="left")
    var_pm = tk.StringVar(value=rev(MODES_PRIVATE, cfg.get("private_mode", "all")))
    ttk.Combobox(r, textvariable=var_pm, values=list(MODES_PRIVATE),
                 state="readonly", width=14).pack(side="left")

    r = ttk.Frame(g4)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="群聊", width=12).pack(side="left")
    var_gm = tk.StringVar(value=rev(MODES_GROUP, cfg.get("group_mode", "at")))
    ttk.Combobox(r, textvariable=var_gm, values=list(MODES_GROUP),
                 state="readonly", width=14).pack(side="left")

    var_kw, _ = labeled_row(g4, "关键词", "keywords", width=30)
    ttk.Label(g4, text="仅「仅关键词」模式生效；多个用英文逗号分隔",
              style="Muted.TLabel").pack(anchor="w", padx=(112, 0))

    r = ttk.Frame(g4)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="上下文轮数", width=12).pack(side="left")
    var_ctx, ent_ctx = entry(r, "context_turns", width=6)
    ent_ctx.pack(side="left", padx=(0, 12))
    ttk.Label(r, text="冷却(秒)").pack(side="left")
    var_cool, ent_cool = entry(r, "cooldown", width=6)
    ent_cool.pack(side="left")

    r = ttk.Frame(g4)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="回复上限(字)", width=12).pack(side="left")
    var_maxc, ent_maxc = entry(r, "max_reply_chars", width=6)
    ent_maxc.pack(side="left", padx=(0, 12))
    ttk.Label(r, text="发送间隔(秒)").pack(side="left")
    var_sendi, ent_sendi = entry(r, "send_interval", width=6)
    ent_sendi.pack(side="left")

    var_extra = tk.BooleanVar(value=bool(cfg.get("extra_types")))
    var_mention = tk.BooleanVar(value=bool(cfg.get("mention_back")))
    var_verify = tk.BooleanVar(value=bool(cfg.get("verify_send")))
    var_logfile = tk.BooleanVar(value=bool(cfg.get("log_to_file", True)))
    ttk.Checkbutton(g4, text="同时处理链接/卡片类消息",
                    variable=var_extra).pack(anchor="w", pady=(4, 0))
    ttk.Checkbutton(g4, text="群聊回复时 @ 对方（OCR 定位，较慢；失败会自动退回普通发送）",
                    variable=var_mention).pack(anchor="w")
    ttk.Checkbutton(g4, text="发送后回读数据库确认（更准但更慢）",
                    variable=var_verify).pack(anchor="w")
    ttk.Checkbutton(g4, text="日志写入文件", variable=var_logfile).pack(anchor="w")

    # ---- 隔离与冷却 ----
    r = ttk.Frame(g4)
    r.pack(fill="x", pady=(4, 2))
    ttk.Label(r, text="上下文隔离", width=12).pack(side="left")
    ISOLATION = {"按会话隔离（群之间互不影响）": "chat",
                 "群内按人隔离（每人独立记忆）": "person"}
    var_iso = tk.StringVar(value=rev(ISOLATION, cfg.get("context_isolation", "chat")))
    ttk.Combobox(r, textvariable=var_iso, values=list(ISOLATION),
                 state="readonly", width=26).pack(side="left")
    var_coolper = tk.BooleanVar(value=bool(cfg.get("cooldown_per_person", True)))
    ttk.Checkbutton(g4, text="群聊冷却按人分别计时（同一人被限速，不影响别人）",
                    variable=var_coolper).pack(anchor="w", pady=(2, 0))

    # ---- 记忆 ----
    r = ttk.Frame(g4)
    r.pack(fill="x", pady=(6, 2))
    ttk.Label(r, text="启动回填", width=12).pack(side="left")
    var_warm, ent_warm = entry(r, "warmup_messages", width=6)
    ent_warm.pack(side="left")
    ttk.Label(r, text="条（0=关）").pack(side="left", padx=(2, 10))
    ttk.Label(r, text="时限").pack(side="left")
    var_warmh, ent_warmh = entry(r, "warmup_hours", width=6)
    ent_warmh.pack(side="left")
    ttk.Label(r, text="小时内").pack(side="left")

    r = ttk.Frame(g4)
    r.pack(fill="x", pady=2)
    var_mem = tk.BooleanVar(value=bool(cfg.get("memory_enabled", True)))
    ttk.Checkbutton(r, text="记忆持久化（重启不丢对话）",
                    variable=var_mem).pack(side="left")
    btn_mem_show = ttk.Button(r, text="查看", width=6)
    btn_mem_show.pack(side="left", padx=(8, 2))
    btn_mem_clear = ttk.Button(r, text="清除记忆")
    btn_mem_clear.pack(side="left", padx=2)
    lbl_mem = ttk.Label(r, text="", style="Muted.TLabel")
    lbl_mem.pack(side="left", padx=6)

    # ---- ③ 长期记忆／用户画像 ----
    r = ttk.Frame(g4)
    r.pack(fill="x", pady=(4, 2))
    var_prof = tk.BooleanVar(value=bool(cfg.get("profile_enabled", True)))
    ttk.Checkbutton(r, text="长期记忆（记住这个人是谁）",
                    variable=var_prof).pack(side="left")
    ttk.Label(r, text="每").pack(side="left", padx=(8, 2))
    var_prof_n, ent_prof_n = entry(r, "profile_every_n", width=4)
    ent_prof_n.pack(side="left")
    ttk.Label(r, text="条抽取").pack(side="left", padx=(2, 8))
    var_prof_inj = tk.BooleanVar(value=bool(cfg.get("profile_inject", True)))
    ttk.Checkbutton(r, text="回复时注入", variable=var_prof_inj).pack(side="left")
    btn_prof_show = ttk.Button(r, text="查看档案", width=9)
    btn_prof_show.pack(side="left", padx=(8, 2))
    btn_prof_clear = ttk.Button(r, text="清除")
    btn_prof_clear.pack(side="left", padx=2)
    lbl_prof = ttk.Label(r, text="", style="Muted.TLabel")
    lbl_prof.pack(side="left", padx=6)

    # ===== 分组 5：人物规则（识别特定人物 → 特定回答）=====
    g5 = ttk.LabelFrame(left, text=" ⑤ 人物规则（认人 → 特定回答）", padding=8)
    g5.pack(fill="x", pady=(2, 8))

    ttk.Label(g5, text="匹配优先级从上到下；姓名可填群昵称/备注/微信昵称/wxid，\n"
                       "多个用逗号分隔，* 表示任何人。留空则不启用。",
              style="Muted.TLabel", justify="left").pack(anchor="w")

    tv_rules = ttk.Treeview(g5, columns=("who", "scope", "action", "persona"),
                            show="headings", height=5)
    for c, t, w in (("who", "对象", 110), ("scope", "范围", 70),
                    ("action", "处理", 70), ("persona", "专属提示词", 130)):
        tv_rules.heading(c, text=t)
        tv_rules.column(c, width=w, anchor="w")
    tv_rules.pack(fill="x", pady=4)

    ACTION_LABELS = {"reply": "正常回复", "ignore": "忽略不回复",
                     "keyword": "仅关键词", "at_only": "仅被@"}
    SCOPE_LABELS = {"*": "全部", "group": "仅群聊",
                    "private": "仅私聊", "custom": "指定群"}

    def rule_row_text(rule):
        scope = (rule.get("scope") or "*").strip()
        scope_txt = SCOPE_LABELS.get(scope, scope)
        return (rule.get("who", ""), scope_txt,
                ACTION_LABELS.get((rule.get("action") or "reply").strip(), "正常回复"),
                _clip(rule.get("persona") or "", 30))

    person_rules = [r for r in (cfg.get("person_rules") or []) if isinstance(r, dict)]

    def refresh_rules():
        for i in tv_rules.get_children():
            tv_rules.delete(i)
        for rule in person_rules:
            tv_rules.insert("", "end", values=rule_row_text(rule))
        lbl_rule_count.configure(text="共 %d 条" % len(person_rules))

    # 规则编辑行
    r = ttk.Frame(g5)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="对象", width=5).pack(side="left")
    var_r_who = tk.StringVar()
    ttk.Entry(r, textvariable=var_r_who, width=16).pack(side="left", padx=(0, 6))
    ttk.Label(r, text="范围").pack(side="left")
    var_r_scope = tk.StringVar(value="全部")
    cmb_r_scope = ttk.Combobox(r, textvariable=var_r_scope,
                               values=["全部", "仅群聊", "仅私聊", "指定群"],
                               state="readonly", width=7)
    cmb_r_scope.pack(side="left", padx=(0, 6))
    ttk.Label(r, text="处理").pack(side="left")
    var_r_action = tk.StringVar(value="正常回复")
    ttk.Combobox(r, textvariable=var_r_action, values=list(ACTION_LABELS.values()),
                 state="readonly", width=9).pack(side="left")

    r = ttk.Frame(g5)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="指定群").pack(side="left")
    var_r_group = tk.StringVar()
    ttk.Entry(r, textvariable=var_r_group, width=18).pack(side="left", padx=(0, 6))
    ttk.Label(r, text="关键词").pack(side="left")
    var_r_kw = tk.StringVar()
    ttk.Entry(r, textvariable=var_r_kw, width=18).pack(side="left")

    r = ttk.Frame(g5)
    r.pack(fill="x", pady=2)
    ttk.Label(r, text="专属提示词").pack(side="left")
    var_r_persona_mode = tk.StringVar(value="追加")
    ttk.Combobox(r, textvariable=var_r_persona_mode, values=["追加", "替换"],
                 state="readonly", width=6).pack(side="left", padx=(0, 6))
    txt_r_persona = tk.Text(r, height=3, wrap="word", font=("Microsoft YaHei UI", 9))
    txt_r_persona.pack(side="left", fill="both", expand=True)

    r = ttk.Frame(g5)
    r.pack(fill="x", pady=2)
    btn_rule_add = ttk.Button(r, text="添加/更新为末条")
    btn_rule_add.pack(side="left", padx=(0, 4))
    btn_rule_update = ttk.Button(r, text="更新选中")
    btn_rule_update.pack(side="left", padx=4)
    btn_rule_del = ttk.Button(r, text="删除选中")
    btn_rule_del.pack(side="left", padx=4)
    btn_rule_up = ttk.Button(r, text="上移")
    btn_rule_up.pack(side="left", padx=4)
    btn_rule_down = ttk.Button(r, text="下移")
    btn_rule_down.pack(side="left", padx=4)
    lbl_rule_count = ttk.Label(r, text="", style="Muted.TLabel")
    lbl_rule_count.pack(side="left", padx=8)

    refresh_rules()

    # ---------------- 右侧视图 -------------------------------------------
    right = ttk.Frame(body)
    right.pack(side="left", fill="both", expand=True)

    nb = ttk.Notebook(right)
    nb.pack(fill="both", expand=True)

    tab_msg = ttk.Frame(nb)
    tab_log = ttk.Frame(nb)
    nb.add(tab_msg, text="  消息记录  ")
    nb.add(tab_log, text="  运行日志  ")

    cols = ("time", "dir", "chat", "sender", "content", "status")
    tv = ttk.Treeview(tab_msg, columns=cols, show="headings")
    for c, t, w, anchor in (
        ("time", "时间", 80, "center"),
        ("dir", "方向", 56, "center"),
        ("chat", "会话", 150, "w"),
        ("sender", "发送者", 110, "w"),
        ("content", "内容", 420, "w"),
        ("status", "状态", 110, "center"),
    ):
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor=anchor, stretch=(c == "content"))
    tv.tag_configure("in", foreground="#1a4f9c")
    tv.tag_configure("out", foreground="#0a7d28")
    tv.tag_configure("bad", foreground="#b32020")
    tv.pack(side="left", fill="both", expand=True)
    sb3 = ttk.Scrollbar(tab_msg, orient="vertical", command=tv.yview)
    sb3.pack(side="right", fill="y")
    tv.configure(yscrollcommand=sb3.set)

    txt_log = scrolledtext.ScrolledText(tab_log, wrap="word", state="disabled",
                                        font=("Consolas", 9))
    txt_log.pack(fill="both", expand=True)
    txt_log.tag_configure("info", foreground="#222222")
    txt_log.tag_configure("ok", foreground="#0a7d28")
    txt_log.tag_configure("warn", foreground="#b06a00")
    txt_log.tag_configure("error", foreground="#b32020")
    txt_log.tag_configure("llm", foreground="#6a1b9a")
    txt_log.tag_configure("skip", foreground="#888888")
    txt_log.tag_configure("debug", foreground="#aaaaaa")

    # ---------------- 底部按钮 -------------------------------------------
    bottom = ttk.Frame(root, padding=(10, 6, 10, 10))
    bottom.pack(fill="x")

    btn_start = ttk.Button(bottom, text="▶  启动机器人", style="Run.TButton")
    btn_start.pack(side="left")
    btn_stop = ttk.Button(bottom, text="■  停止", state="disabled")
    btn_stop.pack(side="left", padx=6)
    btn_test_llm = ttk.Button(bottom, text="测试大模型")
    btn_test_llm.pack(side="left", padx=(14, 6))
    btn_test_wx = ttk.Button(bottom, text="测试微信")
    btn_test_wx.pack(side="left", padx=6)
    btn_save = ttk.Button(bottom, text="保存配置")
    btn_save.pack(side="left", padx=6)

    lbl_hint = ttk.Label(bottom, text="", style="Muted.TLabel")
    lbl_hint.pack(side="left", padx=12)

    btn_clear = ttk.Button(bottom, text="清空视图")
    btn_clear.pack(side="right")

    # ---------------- 日志写入文件 ---------------------------------------
    log_lock = threading.Lock()

    def write_file(line: str) -> None:
        if not var_logfile.get():
            return
        with log_lock:
            try:
                if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass

    def log(text: str, level: str = "info") -> None:
        emit("log", {"level": level, "text": text})

    # ---------------- 配置收集 -------------------------------------------
    def collect_config() -> dict:
        selected = [session_map[i][0] for i in lb_sessions.curselection()
                    if i < len(session_map)]
        # 只在「列表还没载入过」时才回落到配置里的旧 targets（首次启动的场景）。
        # 一旦列表载入过，就以用户的勾选为准 —— 否则用户清空选择后，
        # 程序会悄悄拿旧 targets 去监听，与界面显示不符。
        if var_scope.get() == "selected" and not selected and not lb_populated["done"]:
            selected = list(cfg.get("targets") or [])
        return {
            "account": var_account.get().strip(),
            "scope": var_scope.get(),
            "targets": selected,
            "interval": _to_float(var_interval.get(), 1.0),
            "base_url": var_base.get().strip(),
            "api_key": var_key.get().strip(),
            "model": var_model.get().strip(),
            "system_prompt": txt_prompt.get("1.0", "end").strip(),
            "prompt_presets": dict(presets),
            "person_rules": [dict(r) for r in person_rules],
            "temperature": _to_float(var_temp.get(), 0.7),
            "max_tokens": _to_int(var_maxtok.get(), 800),
            "timeout": _to_float(var_timeout.get(), 60),
            "llm_concurrency": _to_int(cfg.get("llm_concurrency"), 2),
            "private_mode": MODES_PRIVATE.get(var_pm.get(), "all"),
            "group_mode": MODES_GROUP.get(var_gm.get(), "at"),
            "keywords": var_kw.get().strip(),
            "extra_types": bool(var_extra.get()),
            "context_turns": _to_int(var_ctx.get(), 3),
            "cooldown": _to_float(var_cool.get(), 3.0),
            "cooldown_per_person": bool(var_coolper.get()),
            "context_isolation": ISOLATION.get(var_iso.get(), "chat"),
            "memory_enabled": bool(var_mem.get()),
            "warmup_messages": _to_int(var_warm.get(), 20),
            "warmup_hours": _to_float(var_warmh.get(), 72.0),
            "warmup_max_chats": _to_int(cfg.get("warmup_max_chats"), 20),
            "memory_max_conversations": _to_int(cfg.get("memory_max_conversations"), 200),
            "memory_max_age_hours": _to_float(cfg.get("memory_max_age_hours"), 168.0),
            "memory_save_interval": _to_float(cfg.get("memory_save_interval"), 30.0),
            "memory_file": cfg.get("memory_file", ""),
            "profile_enabled": bool(var_prof.get()),
            "profile_every_n": _to_int(var_prof_n.get(), 10),
            "profile_inject": bool(var_prof_inj.get()),
            "profile_file": cfg.get("profile_file", ""),
            "profile_min_interval": _to_float(cfg.get("profile_min_interval"), 1800.0),
            "profile_max_facts": _to_int(cfg.get("profile_max_facts"), 20),
            "max_reply_chars": _to_int(var_maxc.get(), 500),
            "mention_back": bool(var_mention.get()),
            "verify_send": bool(var_verify.get()),
            "send_interval": _to_float(var_sendi.get(), 1.5),
            "save_api_key": bool(var_savekey.get()),
            "log_to_file": bool(var_logfile.get()),
        }

    def apply_config_to_ui(new: dict) -> None:
        """把（外部载入的）配置回填界面。"""
        nonlocal session_map
        var_account.set(new.get("account", ""))
        var_scope.set(new.get("scope", "selected"))
        for k, v in (("interval", var_interval), ("base_url", var_base),
                     ("api_key", var_key), ("model", var_model),
                     ("temperature", var_temp), ("max_tokens", var_maxtok),
                     ("timeout", var_timeout), ("keywords", var_kw),
                     ("context_turns", var_ctx), ("cooldown", var_cool),
                     ("max_reply_chars", var_maxc), ("send_interval", var_sendi)):
            v.set(str(new.get(k, "")))
        var_pm.set(rev(MODES_PRIVATE, new.get("private_mode", "all")))
        var_gm.set(rev(MODES_GROUP, new.get("group_mode", "at")))
        var_extra.set(bool(new.get("extra_types")))
        var_mention.set(bool(new.get("mention_back")))
        var_verify.set(bool(new.get("verify_send")))
        var_savekey.set(bool(new.get("save_api_key", True)))
        var_logfile.set(bool(new.get("log_to_file", True)))
        var_coolper.set(bool(new.get("cooldown_per_person", True)))
        var_iso.set(rev(ISOLATION, new.get("context_isolation", "chat")))
        var_mem.set(bool(new.get("memory_enabled", True)))
        var_prof.set(bool(new.get("profile_enabled", True)))
        var_prof_inj.set(bool(new.get("profile_inject", True)))
        for k, v in (("warmup_messages", var_warm), ("warmup_hours", var_warmh),
                     ("profile_every_n", var_prof_n)):
            v.set(str(new.get(k, "")))
        txt_prompt.delete("1.0", "end")
        txt_prompt.insert("1.0", new.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
        # 勾选已保存的监听目标
        targets = set(new.get("targets") or [])
        if targets:
            lb_sessions.selection_clear(0, "end")
            for i, (u, _) in enumerate(session_map):
                if u in targets:
                    lb_sessions.selection_set(i)
        update_scope_ui()      # 程序化改选中不会触发 <<ListboxSelect>>，手动刷一次

    # ---------------- 事件泵 ---------------------------------------------
    def pump() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()

                if kind == "log":
                    lvl = payload.get("level", "info")
                    line = "[%s] %s" % (_now(), payload.get("text", ""))
                    txt_log.configure(state="normal")
                    txt_log.insert("end", line + "\n", lvl)
                    txt_log.see("end")
                    txt_log.configure(state="disabled")
                    if lvl != "debug":
                        write_file(line)

                elif kind == "msg":
                    if payload.get("dir") == "收到":
                        tag = "in"
                    elif str(payload.get("status", "")) == "成功":
                        tag = "out"
                    else:
                        tag = "bad"
                    tv.insert("", 0, values=(
                        payload.get("time", ""), payload.get("dir", ""),
                        payload.get("chat", ""), payload.get("sender", ""),
                        payload.get("content", ""), payload.get("status", ""),
                    ), tags=(tag,))
                    # 限制行数，避免长时间运行内存膨胀
                    children = tv.get_children()
                    for extra in children[600:]:
                        tv.delete(extra)

                elif kind == "profiles":
                    refresh_prof_label()

                elif kind == "stats":
                    lbl_stats.configure(
                        text="收到 %d | 回复 %d | 已发送 %d | 跳过 %d | 错误 %d | 档案 %d"
                             % (payload.get("in", 0), payload.get("out", 0),
                                payload.get("sent", 0), payload.get("skip", 0),
                                payload.get("err", 0), payload.get("profile", 0)))

                elif kind == "account":
                    lbl_acct.configure(text="%s (%s)" % (payload.get("nick", "-"),
                                                        payload.get("wxid", "-")),
                                       style="Ok.TLabel")
                    lbl_wx.configure(text="已连接", style="Ok.TLabel")
                    acct = payload.get("account", "")
                    if acct and not var_account.get().strip():
                        var_account.set(acct)

                elif kind == "sessions":
                    items = payload.get("items") or []
                    session_map.clear()
                    session_map.extend(items)
                    populate_lb(session_map)
                    lb_populated["done"] = True
                    apply_config_to_ui(collect_config())
                    update_scope_ui()
                    lbl_load.configure(text="共 %d 个会话" % len(session_map))
                    if not session_map:
                        log("会话列表为空：确认微信 4.x 已登录，"
                            "且「账号」选的是当前在用那个", "warn")

                elif kind == "accounts":
                    cmb_account.configure(values=payload or [])

                elif kind == "running":
                    on = bool(payload)
                    btn_start.configure(state="disabled" if on else "normal")
                    btn_stop.configure(state="normal" if on else "disabled")
                    lbl_run.configure(text="运行中" if on else "已停止",
                                      style="Ok.TLabel" if on else "Bad.TLabel")
                    refresh_mem_label()
                    refresh_prof_label()
                    if not on:
                        holder = engine_holder.get("engine")
                        if holder is not None:
                            threading.Thread(target=holder.stop, daemon=True).start()
                            engine_holder["engine"] = None

                elif kind == "hint":
                    lbl_hint.configure(text=payload)

        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            root.after(120, pump)

    # ---------------- 交互动作 -------------------------------------------
    def do_save_config(verbose: bool = True) -> None:
        new = collect_config()
        cfg.update(new)
        try:
            save_config(cfg)
            if verbose:
                log("配置已保存：%s" % CONFIG_PATH, "ok")
                if engine_holder.get("engine") is not None:
                    log("机器人正在运行：回复规则 / @ 对方 / 冷却 / 提示词 / 模型等"
                        "已即时生效；", "warn")
                    log("「监听范围 / 轮询间隔 / 账号」需停止后重新启动才会生效。",
                        "warn")
        except Exception as exc:
            log("保存配置失败：%s" % exc, "error")

    def do_load_sessions() -> None:
        btn_load.configure(state="disabled")

        def work():
            try:
                from wechatauto.db import WeChatDB, list_accounts
            except Exception as exc:
                log("无法导入 wechatauto：%s" % exc, "error")
                return
            log("正在读取会话列表（首次需解密数据库，约 6 秒）…")
            try:
                try:
                    accts = list_accounts()
                    names = [a.get("account") or a.get("name") or str(a) for a in accts]
                    emit("accounts", names)
                except Exception:
                    pass
                account = var_account.get().strip() or None
                db = WeChatDB(account=account)
                engine_holder["db"] = db
                info = db.get_self_info() or {}
                emit("account", {"account": db.account,
                                 "nick": info.get("nick_name") or info.get("remark") or "",
                                 "wxid": info.get("username") or getattr(db, "wxid", "")})
                sessions = db.get_sessions(limit=300)
                items = []
                for s in sessions:
                    u = s.get("username") or ""
                    if not u:
                        continue
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
                    items.append((u, disp))
                emit("sessions", {"items": items})
                log("会话列表载入完成，共 %d 个会话" % len(items), "ok")
            except Exception as exc:
                log("载入会话失败：%s" % exc, "error")
                log("请确认微信 4.x 已登录；若为多账号，请在上方选择账号。", "warn")
            finally:
                root.after(0, lambda: btn_load.configure(state="normal"))

        threading.Thread(target=work, name="load-sessions", daemon=True).start()

    def do_test_llm() -> None:
        btn_test_llm.configure(state="disabled")
        c = collect_config()

        def work():
            log("测试大模型连通性：%s @ %s" % (c.get("model"), c.get("base_url")))
            try:
                client = LLMClient(c["base_url"], c["api_key"], c["model"],
                                   _to_float(c["temperature"], 0.7),
                                   _to_int(c["max_tokens"], 800),
                                   _to_float(c["timeout"], 60))
                t0 = time.time()
                reply = client.test()
                log("大模型连通正常（%.1fs），返回：%s" % (time.time() - t0, reply), "ok")
                emit("hint", "大模型连接成功")
            except Exception as exc:
                log("大模型连接失败：%s" % exc, "error")
                log("常见原因：API 地址缺少 /v1、密钥错误、模型名不存在、余额不足、"
                    "网络需代理。", "warn")
                emit("hint", "大模型连接失败，详见日志")
            finally:
                root.after(0, lambda: btn_test_llm.configure(state="normal"))

        threading.Thread(target=work, name="test-llm", daemon=True).start()

    def do_test_wx() -> None:
        btn_test_wx.configure(state="disabled")

        def work():
            try:
                import ctypes
                hwnd = (ctypes.windll.user32.FindWindowW(None, "微信")
                        or ctypes.windll.user32.FindWindowW(None, "Weixin"))
                if hwnd:
                    log("微信主窗口已找到（hwnd=%s）" % hwnd, "ok")
                    emit("hint", "微信窗口正常")
                else:
                    log("未找到微信主窗口，请确认微信 4.x 已登录并运行", "error")
                    emit("hint", "未找到微信窗口")
            except Exception as exc:
                log("检测微信窗口失败：%s" % exc, "error")
            finally:
                root.after(0, lambda: btn_test_wx.configure(state="normal"))

        threading.Thread(target=work, name="test-wx", daemon=True).start()

    def do_start() -> None:
        if engine_holder.get("engine") is not None:
            log("机器人已在运行", "warn")
            return
        new = collect_config()
        if not new["api_key"]:
            if not messagebox.askyesno(
                    "未填写 API Key",
                    "尚未填写大模型 API Key，机器人将无法生成回复。\n\n仍要启动吗？"):
                return
        if not new["model"]:
            messagebox.showwarning("缺少模型名称", "请先填写模型名称。")
            return
        if new["scope"] == "selected" and not new["targets"]:
            tip = ("当前是「指定会话」模式，但没有选中任何会话，无法启动。\n\n"
                   "请按下面任一种方式处理：\n"
                   "  · 在上方列表里直接点击会话（点一下选中，再点一下取消）\n"
                   "  · 或点列表下方的「全选」\n"
                   "  · 或把监听范围改成「全部会话」\n\n")
            if not lb_populated["done"]:
                tip += "提示：列表还是空的，请先点「载入会话列表」。"
            else:
                tip += "提示：也可以在「全部会话」模式下先跑通，再改成指定会话。"
            messagebox.showwarning("未选择会话", tip)
            log("启动被拦下：指定会话模式下没有勾选任何会话", "warn")
            return

        cfg.update(new)
        do_save_config(verbose=False)

        # 复用已初始化的数据库（省去重新解密的几秒），但账号变了就必须重建
        db_reuse = engine_holder.get("db")
        want = (new.get("account") or "").strip()
        if db_reuse is not None and want and getattr(db_reuse, "account", None) != want:
            log("账号已切换为「%s」，将重新初始化数据库" % want)
            db_reuse = None
            engine_holder["db"] = None

        eng = BotEngine(cfg, emit, db=db_reuse)
        engine_holder["engine"] = eng
        log("=" * 52)
        log("正在启动机器人…")
        lbl_run.configure(text="启动中…", style="Muted.TLabel")
        btn_start.configure(state="disabled")
        eng.start()

    def do_stop() -> None:
        eng = engine_holder.get("engine")
        if eng is None:
            return
        log("正在停止机器人…")
        lbl_run.configure(text="停止中…", style="Muted.TLabel")
        engine_holder["engine"] = None

        def work():
            try:
                eng.stop()
            except Exception as exc:
                log("停止时出错：%s" % exc, "warn")
            emit("running", False)
            log("机器人已停止", "ok")

        threading.Thread(target=work, name="stop-bot", daemon=True).start()

    def do_clear() -> None:
        for item in tv.get_children():
            tv.delete(item)
        txt_log.configure(state="normal")
        txt_log.delete("1.0", "end")
        txt_log.configure(state="disabled")

    # ---------------- 提示词预设 -----------------------------------------
    def do_preset_load() -> None:
        name = var_preset.get().strip()
        if not name or name not in presets:
            log("请先在下拉框里选择一个提示词预设", "warn")
            return
        txt_prompt.delete("1.0", "end")
        txt_prompt.insert("1.0", presets[name])
        cfg["system_prompt"] = presets[name]
        log("已载入提示词预设「%s」（运行中即时生效）" % name, "ok")

    def do_preset_save() -> None:
        text = txt_prompt.get("1.0", "end").strip()
        if not text:
            log("提示词为空，无法保存为预设", "warn")
            return
        name = simpledialog.askstring("另存为预设", "预设名称：",
                                      initialvalue=var_preset.get().strip() or "我的预设")
        if not name:
            return
        name = name.strip()
        presets[name] = text
        cmb_preset.configure(values=list(presets))
        var_preset.set(name)
        cfg["prompt_presets"] = dict(presets)
        do_save_config(verbose=False)
        log("已保存提示词预设「%s」" % name, "ok")

    def do_preset_del() -> None:
        name = var_preset.get().strip()
        if not name or name not in presets:
            return
        if not messagebox.askyesno("删除预设", "确定删除提示词预设「%s」？" % name):
            return
        presets.pop(name, None)
        cmb_preset.configure(values=list(presets))
        var_preset.set("")
        cfg["prompt_presets"] = dict(presets)
        do_save_config(verbose=False)
        log("已删除提示词预设「%s」" % name, "ok")

    # ---------------- 人物规则编辑 ---------------------------------------
    def read_rule_form() -> dict:
        scope_label = var_r_scope.get().strip()
        scope = {"全部": "*", "仅群聊": "group", "仅私聊": "private",
                 "指定群": (var_r_group.get().strip() or "*")}.get(scope_label, "*")
        action = {v: k for k, v in ACTION_LABELS.items()}.get(
            var_r_action.get().strip(), "reply")
        return {
            "who": var_r_who.get().strip(),
            "scope": scope,
            "action": action,
            "keywords": var_r_kw.get().strip(),
            "persona": txt_r_persona.get("1.0", "end").strip(),
            "persona_mode": "replace" if var_r_persona_mode.get().strip() == "替换"
                            else "append",
        }

    def load_rule_into_form(rule: dict) -> None:
        var_r_who.set(rule.get("who", ""))
        scope = (rule.get("scope") or "*").strip()
        if scope in SCOPE_LABELS:
            var_r_scope.set(SCOPE_LABELS[scope])
            var_r_group.set("")
        else:
            var_r_scope.set("指定群")
            var_r_group.set(scope)
        var_r_action.set(ACTION_LABELS.get((rule.get("action") or "reply").strip(),
                                          "正常回复"))
        var_r_kw.set(rule.get("keywords", ""))
        txt_r_persona.delete("1.0", "end")
        txt_r_persona.insert("1.0", rule.get("persona", ""))
        var_r_persona_mode.set("替换" if (rule.get("persona_mode") == "replace") else "追加")

    def do_rule_add() -> None:
        rule = read_rule_form()
        if not rule["who"]:
            messagebox.showwarning("缺少对象", "请填写要匹配的人：群昵称 / 备注 / "
                                             "微信昵称 / wxid，多个用逗号分隔。")
            return
        person_rules.append(rule)
        refresh_rules()
        do_save_config(verbose=False)
        log("已添加人物规则：%s → %s（运行中即时生效）"
            % (rule["who"], ACTION_LABELS.get(rule["action"], rule["action"])), "ok")

    def do_rule_update() -> None:
        sel = tv_rules.selection()
        if not sel:
            log("请先在列表里选中要更新的规则", "warn")
            return
        idx = tv_rules.index(sel[0])
        rule = read_rule_form()
        if not rule["who"]:
            messagebox.showwarning("缺少对象", "请填写要匹配的人。")
            return
        person_rules[idx] = rule
        refresh_rules()
        tv_rules.selection_set(tv_rules.get_children()[idx])
        do_save_config(verbose=False)
        log("已更新第 %d 条人物规则" % (idx + 1), "ok")

    def do_rule_del() -> None:
        sel = tv_rules.selection()
        if not sel:
            log("请先在列表里选中要删除的规则", "warn")
            return
        for item in reversed(sel):
            idx = tv_rules.index(item)
            removed = person_rules.pop(idx)
            log("已删除人物规则：%s" % removed.get("who", ""), "ok")
        refresh_rules()
        do_save_config(verbose=False)

    def do_rule_move(delta: int) -> None:
        sel = tv_rules.selection()
        if not sel:
            return
        idx = tv_rules.index(sel[0])
        new_idx = idx + delta
        if not (0 <= new_idx < len(person_rules)):
            return
        person_rules[idx], person_rules[new_idx] = (
            person_rules[new_idx], person_rules[idx])
        refresh_rules()
        tv_rules.selection_set(tv_rules.get_children()[new_idx])
        do_save_config(verbose=False)

    def on_rule_select(_event=None) -> None:
        sel = tv_rules.selection()
        if not sel:
            return
        idx = tv_rules.index(sel[0])
        if 0 <= idx < len(person_rules):
            load_rule_into_form(person_rules[idx])

    tv_rules.bind("<<TreeviewSelect>>", on_rule_select)

    # ---------------- 记忆 ------------------------------------------------
    def mem_file_path() -> str:
        return (cfg.get("memory_file") or "").strip() or os.path.join(
            BASE_DIR, "wx_ai_bot_memory.json")

    def refresh_mem_label() -> None:
        eng = engine_holder.get("engine")
        try:
            if eng is not None:
                st = eng.memory_stats()
                lbl_mem.configure(text="%d 会话 / %d 条"
                                       % (st["conversations"], st["messages"]))
                return
            path = mem_file_path()
            if os.path.exists(path):
                lbl_mem.configure(text="文件 %.1f KB" % (os.path.getsize(path) / 1024.0))
            else:
                lbl_mem.configure(text="暂无记忆")
        except Exception:
            lbl_mem.configure(text="")

    def do_mem_clear() -> None:
        eng = engine_holder.get("engine")
        n = None
        if eng is not None:
            n = eng.clear_memory()
        else:
            # 未运行：直接删文件
            path = mem_file_path()
            try:
                if os.path.exists(path):
                    os.remove(path)
                    n = -1
            except Exception as exc:
                log("删除记忆文件失败：%s" % exc, "error")
        if n is not None:
            log("已清除对话记忆%s（机器人之前的上下文已忘记）"
                % ("（运行中，内存也已清空）" if n >= 0 else ""), "ok")
        else:
            log("没有可清除的记忆", "warn")
        refresh_mem_label()

    def do_mem_show() -> None:
        path = mem_file_path()
        if not os.path.exists(path):
            messagebox.showinfo("记忆", "暂无记忆文件：\n%s" % path)
            return
        try:
            with open(path, encoding="utf-8") as fh:
                obj = json.load(fh)
            convs = obj.get("conversations") or {}
            lines = ["记忆文件：%s" % path,
                     "会话数：%d" % len(convs), ""]
            for k, v in list(convs.items())[:15]:
                turns = v.get("turns") or []
                upd = time.strftime("%m-%d %H:%M",
                                    time.localtime(v.get("updated") or 0))
                first = turns[0][1][:28].replace("\n", " ") if turns else ""
                lines.append("%-28s %2d 条  最后更新 %s  %s"
                             % (k[:28], len(turns), upd, first))
            if len(convs) > 15:
                lines.append("… 共 %d 个会话" % len(convs))
            messagebox.showinfo("记忆内容", "\n".join(lines))
        except Exception as exc:
            messagebox.showerror("记忆", "读取失败：%s" % exc)

    # ---------------- 监听范围：全选 / 清空 -------------------------------
    def do_select_all() -> None:
        if var_scope.get() != "selected":
            var_scope.set("selected")
        try:
            lb_sessions.configure(state="normal")   # disabled 时 selection_set 同样无效
        except Exception:
            pass
        lb_sessions.selection_set(0, "end")
        update_scope_ui()
        log("已全选 %d 个会话" % lb_sessions.size(), "ok")

    def do_select_none() -> None:
        try:
            lb_sessions.configure(state="normal")
        except Exception:
            pass
        lb_sessions.selection_clear(0, "end")
        update_scope_ui()
        log("已清空会话选择")

    # ---------------- 一键更新昵称 ----------------------------------------
    def do_refresh_names() -> None:
        btn_refresh_names.configure(state="disabled")
        eng = engine_holder.get("engine")

        def work():
            try:
                if eng is not None:
                    log("正在重读昵称（群昵称 + 备注 + 微信昵称）…")
                    st = eng.refresh_nicknames()
                    log("昵称已更新：清理 %d 个备注缓存 / %d 个群花名册；"
                        "重读 %d 个群、%d 名成员（其中 %d 人设了群昵称），"
                        "同步 %d 份人物档案"
                        % (st["cleared_names"], st["cleared_rosters"],
                           st["groups"], st["members"], st["named"],
                           st["profiles"]), "ok")
                    if eng.self_wxid:
                        g = eng._group_display(eng.self_wxid, "")
                        log("我在该会话的显示名：%s" % g)
                else:
                    # 未运行：重新载入会话列表即可刷新昵称显示
                    log("机器人未运行，改为刷新会话列表中的昵称…")
                    do_load_sessions()
                    return
                emit("hint", "昵称已更新")
            except Exception as exc:
                log("更新昵称失败：%s" % exc, "error")
            finally:
                root.after(0, lambda: btn_refresh_names.configure(state="normal"))

        threading.Thread(target=work, name="refresh-names", daemon=True).start()

    # ---------------- 长期记忆／画像 --------------------------------------
    def prof_file_path() -> str:
        return (cfg.get("profile_file") or "").strip() or os.path.join(
            BASE_DIR, "wx_ai_bot_profiles.json")

    def refresh_prof_label() -> None:
        eng = engine_holder.get("engine")
        try:
            if eng is not None:
                st = eng.profile_stats()
                lbl_prof.configure(text="%d 人 / %d 条"
                                        % (st["profiles"], st["facts"]))
                return
            path = prof_file_path()
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    n = len((json.load(fh).get("profiles") or {}))
                lbl_prof.configure(text="%d 人" % n)
            else:
                lbl_prof.configure(text="暂无")
        except Exception:
            lbl_prof.configure(text="")

    def do_prof_show() -> None:
        path = prof_file_path()
        eng = engine_holder.get("engine")
        data = {}
        if eng is not None:
            for k in eng._profiles.keys():
                data[k] = eng._profiles.get(k)
        elif os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = (json.load(fh).get("profiles") or {})
            except Exception as exc:
                messagebox.showerror("人物档案", "读取失败：%s" % exc)
                return
        if not data:
            messagebox.showinfo("人物档案", "暂无档案。\n\n"
                                "开启「长期记忆」后，机器人会在对话中自动积累"
                                "关于每个人的稳定信息。\n文件：%s" % path)
            return
        lines = ["人物档案文件：%s" % path, "共 %d 人" % len(data), ""]
        for wxid, p in list(data.items())[:12]:
            lines.append("─" * 54)
            lines.append("%s   [%s]" % (p.get("name") or "(未命名)", wxid))
            if p.get("aliases"):
                lines.append("  别名：%s" % "、".join(p["aliases"][:6]))
            if p.get("summary"):
                lines.append("  画像：%s" % p["summary"])
            for f in (p.get("facts") or [])[:8]:
                lines.append("   · %s" % f)
            lines.append("  累计消息：%s" % p.get("msg_count", 0))
        if len(data) > 12:
            lines.append("")
            lines.append("… 其余 %d 人略" % (len(data) - 12))
        messagebox.showinfo("人物档案", "\n".join(lines))

    def do_prof_clear() -> None:
        if not messagebox.askyesno("清除长期记忆",
                                   "确定清空所有人物档案？\n"
                                   "（对话上下文记忆不受影响）"):
            return
        eng = engine_holder.get("engine")
        if eng is not None:
            n = eng.clear_profiles()
        else:
            path = prof_file_path()
            n = 0
            try:
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as fh:
                        n = len((json.load(fh).get("profiles") or {}))
                    os.remove(path)
            except Exception as exc:
                log("删除档案文件失败：%s" % exc, "error")
        log("已清除 %d 份人物档案" % n, "ok")
        refresh_prof_label()

    btn_start.configure(command=do_start)
    btn_stop.configure(command=do_stop)
    btn_load.configure(command=do_load_sessions)
    btn_test_llm.configure(command=do_test_llm)
    btn_test_wx.configure(command=do_test_wx)
    btn_save.configure(command=lambda: do_save_config())
    btn_preset_load.configure(command=do_preset_load)
    btn_preset_save.configure(command=do_preset_save)
    btn_preset_del.configure(command=do_preset_del)
    btn_rule_add.configure(command=do_rule_add)
    btn_rule_update.configure(command=do_rule_update)
    btn_rule_del.configure(command=do_rule_del)
    btn_rule_up.configure(command=lambda: do_rule_move(-1))
    btn_rule_down.configure(command=lambda: do_rule_move(1))
    btn_mem_clear.configure(command=do_mem_clear)
    btn_mem_show.configure(command=do_mem_show)
    btn_refresh_names.configure(command=do_refresh_names)
    btn_sel_all.configure(command=do_select_all)
    btn_sel_none.configure(command=do_select_none)
    btn_prof_show.configure(command=do_prof_show)
    btn_prof_clear.configure(command=do_prof_clear)
    btn_clear.configure(command=do_clear)

    def on_close() -> None:
        eng = engine_holder.get("engine")
        if eng is not None:
            if not messagebox.askyesno("退出", "机器人正在运行，确定要退出吗？"):
                return
            try:
                eng.stop(join=False)
            except Exception:
                pass
        try:
            do_save_config(verbose=False)
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    root.after(120, pump)

    # 启动欢迎信息
    log("%s v%s 已就绪" % (APP_NAME, APP_VERSION), "ok")
    log("步骤：① 测试微信 → ② 载入会话列表 → ③ 填写大模型 API → ④ 测试大模型 → ⑤ 启动机器人")
    log("配置文件：%s" % CONFIG_PATH)
    # 记忆状态（② 持久化 / ① 回填）
    try:
        mp = mem_file_path()
        if os.path.exists(mp):
            with open(mp, encoding="utf-8") as fh:
                _mo = json.load(fh)
            _mc = len((_mo.get("conversations") or {}))
            log("记忆文件：%s（%d 个会话）—— 启动后会自动恢复" % (mp, _mc), "ok")
        else:
            log("记忆文件：%s（暂无；启动时会从数据库回填最近 %s 条对话）"
                % (mp, cfg.get("warmup_messages")), "ok")
    except Exception as exc:
        log("读取记忆文件失败：%s" % exc, "warn")
    refresh_mem_label()
    try:
        pp = prof_file_path()
        if os.path.exists(pp):
            with open(pp, encoding="utf-8") as fh:
                _pc = len((json.load(fh).get("profiles") or {}))
            log("人物档案：%s（%d 人）—— 长期记忆会注入回复" % (pp, _pc), "ok")
        else:
            log("人物档案：%s（暂无；开启「长期记忆」后会随对话自动积累）" % pp)
    except Exception as exc:
        log("读取人物档案失败：%s" % exc, "warn")
    refresh_prof_label()
    if not os.path.exists(os.path.join(BASE_DIR, "repo", "wechatauto")):
        try:
            import wechatauto  # noqa: F401
        except Exception:
            log("未检测到 wechatauto-replica，请先执行 pip install -e .（详见 README_使用说明.md）",
                "warn")

    if "--smoke" in sys.argv:
        # 冒烟自测：构建完整界面后自动关闭，用于验证 UI 构建无异常
        log("冒烟自测：界面构建成功，2 秒后自动关闭", "ok")
        root.after(2000, root.destroy)

    root.mainloop()
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(run_check())
    sys.exit(main())
