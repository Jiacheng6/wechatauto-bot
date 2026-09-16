# -*- coding: utf-8 -*-
"""提交前防泄漏预检：扫描将要提交的文件里有没有密钥 / 个人信息。

**在 git push 之前跑一遍**：

    python tools/check_secrets.py            # 扫描工作区
    python tools/check_secrets.py --staged   # 只扫描 git 暂存区（更贴近 push 内容）
    python tools/check_secrets.py --names "张三,某群名"   # 临时加人名黑名单

退出码 0 = 干净，1 = 发现可疑内容（**不要 push**）。

它检查：
    · 各类 API Key 形态（sk- / AIza / ghp_ / xoxb- …）
    · 微信 wxid / @chatroom 群 ID（会暴露账号与所在群）
    · 中国手机号、邮箱
    · 本机绝对路径（D:\\... / C:\\Users\\...）
    · 被 .gitignore 遗漏的敏感文件（配置/日志/记忆/解密缓存）
    · **人名/昵称黑名单**

关于人名 —— **正则查不出人名**，这是本工具的结构性盲区：
密钥有格式（`sk-` 开头），而「张三」「阿明」没有任何格式特征。
所以人名只能靠**显式清单**来查：

    1) 建一个 `.name_blocklist.txt`（已在 .gitignore 里，不会被提交），
       每行一个不想出现在仓库里的名字/昵称（自己的、联系人的、群名都行）
    2) 或临时用 `--names "张三,李四,某群名"`

名单里的词一旦在任何文件里出现就会报出来。
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT, setup_console  # noqa: E402

setup_console()

#: 人名黑名单文件（与 .gitignore 配套：该文件本身绝不能被提交）
BLOCKLIST_FILE = os.path.join(ROOT, ".name_blocklist.txt")


def load_name_blocklist(extra: str = "") -> list:
    """读取人名黑名单：文件里的 + 命令行 --names 的。"""
    names = []
    try:
        with open(BLOCKLIST_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print("读取人名黑名单失败：%s" % exc)
    for piece in (extra or "").replace("，", ",").split(","):
        piece = piece.strip()
        if piece:
            names.append(piece)
    out = []
    for n in names:
        if len(n) >= 2 and n not in out:      # 单字会大量误报，忽略
            out.append(n)
    return out

#: 绝不允许进入仓库的文件（相对路径或文件名）
FORBIDDEN_FILES = {
    "wx_ai_bot_config.json",
    "wx_ai_bot.log",
    "wx_ai_bot_memory.json",
    "wx_ai_bot_profiles.json",
}
FORBIDDEN_DIRS = {
    ".wxdb_cache", ".wxdb_fresh", "wechatauto_logs", "repo", "_shots",
    "__pycache__", ".venv", "venv",
}

#: (名称, 正则, 说明)
RULES = [
    ("API Key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
     "OpenAI/DeepSeek/硅基流动等密钥"),
    ("API Key (其他形态)",
     re.compile(r"\b(AIza[0-9A-Za-z_\-]{30,}|ghp_[A-Za-z0-9]{30,}|"
                r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,}|"
                r"AKIA[0-9A-Z]{16})\b"),
     "Google/GitHub/Slack/AWS 凭证"),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥"),
    ("微信 wxid", re.compile(r"\bwxid_[0-9a-zA-Z]{8,}"), "会暴露账号/联系人身份"),
    ("微信群 ID", re.compile(r"\b\d{8,}@chatroom\b"), "会暴露所在群"),
    ("中国手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "个人手机号"),
    ("邮箱", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
     "邮箱（排除示例域）"),
    ("Windows 绝对路径", re.compile(r"[A-Za-z]:\\\\?[Uu]sers\\\\?|[A-Za-z]:\\\\?[Ww]echat"),
     "本机路径"),
]

#: 允许出现的白名单（避免误报）
ALLOW_PATTERNS = [
    re.compile(r"wxid_xxxxxxxxxxxx22"),          # 文档里的占位符
    # 测试用的假 wxid（见 tools/verify_*.py）
    re.compile(r"wxid_(abc12345|def67890|test\w*|alice\d*|bob\d*|carol\d*|"
               r"me0+|priv\d+|member\d*|unknown|x)\b"),
    re.compile(r"@chatroom"),                    # 仅示例后缀
    re.compile(r"example\.com|example\.org|@example"),
    # 通用路径前缀（本身不含个人信息：C:\Users\ / C:\Users\... / C:\Users\<name>）
    re.compile(r"^[A-Za-z]:\\+[Uu]sers\\*"),
    re.compile(r"AppData\\+Roaming\\+Python"),   # 通用路径说明
    re.compile(r"[A-Za-z]:\\+[Ww]echatai"),      # 文档的历史示例路径
]

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "repo",
             ".wxdb_cache", ".wxdb_fresh", "wechatauto_logs", "_shots"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz",
            ".whl", ".exe", ".dll", ".pyd", ".pyc", ".db", ".sqlite", ".so"}
MAX_BYTES = 2 * 1024 * 1024


def iter_files(staged_only: bool):
    """staged_only=True 时，列出**提交后仓库里会有的全部文件**。

    注意不能用 `git diff --cached --name-only` —— 那只列出**本次改动**的文件。
    首次提交时它恰好等于全量，所以看不出问题；一旦已有历史提交，
    未改动的文件就会被漏掉，而它们同样会被 push 上去。
    正确来源是 `git ls-files`（索引内容 = 即将提交的内容）。
    """
    if staged_only:
        import subprocess
        try:
            out = subprocess.run(["git", "ls-files"],
                                 cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout
        except Exception as exc:
            print("读取 git 索引失败（是不是还没 git init？）：%s" % exc)
            return
        for line in out.splitlines():
            line = line.strip()
            if line and os.path.exists(os.path.join(ROOT, line)):
                yield os.path.join(ROOT, line), line
        return
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT)
            yield full, rel


def allowed(text: str, match: str) -> bool:
    for p in ALLOW_PATTERNS:
        if p.search(match):
            return True
    return False


def scan_text(rel: str, text: str, names: list) -> list:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat, why in RULES:
            for m in pat.finditer(line):
                frag = m.group(0)
                if allowed(text, frag):
                    continue
                hits.append((name, rel, lineno, frag[:60], why))
        # 人名黑名单：正则查不出人名，只能靠显式清单
        for n in names:
            if n in line:
                hits.append(("人名黑名单", rel, lineno, n, "命中了黑名单里的名字/昵称"))
    return hits


def is_gitignored(rel: str) -> bool:
    """粗判该文件是否已被 .gitignore 覆盖（按文件名/目录名匹配）。

    目的是区分两种情况：
      · 本地存在但已被忽略 → 安全，只需提示
      · 会被提交进去     → **危险，必须拦下**
    """
    try:
        with open(os.path.join(ROOT, ".gitignore"), "r", encoding="utf-8") as fh:
            patterns = [ln.strip() for ln in fh
                        if ln.strip() and not ln.strip().startswith("#")]
    except Exception:
        return False
    base = os.path.basename(rel).lower()
    parts = [p.lower() for p in rel.replace("\\", "/").split("/")]
    for pat in patterns:
        p = pat.strip("/").lower().rstrip("*")
        if not p:
            continue
        if p == base or fnmatch.fnmatch(base, p):
            return True
        if p in parts:
            return True
        if rel.replace("\\", "/").lower().startswith(p):
            return True
    return False


def main() -> int:
    staged = "--staged" in sys.argv
    print("=" * 76)
    print("提交前防泄漏预检   （%s）" % ("git 暂存区" if staged else "工作区"))
    print("=" * 76)

    # --names "a,b,c"
    extra_names = ""
    for i, a in enumerate(sys.argv):
        if a == "--names" and i + 1 < len(sys.argv):
            extra_names = sys.argv[i + 1]
        elif a.startswith("--names="):
            extra_names = a.split("=", 1)[1]
    names = load_name_blocklist(extra_names)
    if names:
        # 只报数量，**不要把名单本身打印出来** —— 否则输出一旦被重定向到
        # 仓库内的文件（例如 `... > audit.txt`），名单就随文件一起泄漏了。
        print("人名黑名单：已加载 %d 项（内容不显示，避免输出本身泄漏）" % len(names))
    else:
        print("人名黑名单：未配置（要查人名请建 .name_blocklist.txt，"
              "或加 --names \"张三,某群名\"）")

    problems = []
    ignored = []
    checked = 0
    BLOCK_BASE = os.path.basename(BLOCKLIST_FILE).lower()

    for full, rel in iter_files(staged):
        base = os.path.basename(rel)
        parts = set(rel.replace("\\", "/").split("/"))
        if base.lower() == BLOCK_BASE:
            problems.append(("黑名单文件本身", rel, 0,
                             "人名黑名单含联系人姓名，**绝不能提交**", ""))
            continue
        if base in FORBIDDEN_FILES or (parts & FORBIDDEN_DIRS):
            if staged or not is_gitignored(rel):
                problems.append(("敏感文件", rel, 0,
                                 "含密钥/聊天记录，且**不会被 .gitignore 拦下**", ""))
            else:
                ignored.append(rel)
            continue
        if os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        try:
            if os.path.getsize(full) > MAX_BYTES:
                continue
            with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            continue
        checked += 1
        # 只看内容，跳过本预检脚本自身（它内含用于匹配的正则字面量）
        if rel.replace("\\", "/").endswith("tools/check_secrets.py"):
            continue
        problems.extend(scan_text(rel, text, names))

    print("已扫描 %d 个文本文件" % checked)
    if ignored:
        print("\n本地存在但已被 .gitignore 忽略（安全，不会提交）：")
        for rel in sorted(set(ignored)):
            print("  · %s" % rel)
    if not problems:
        print("\n[√] 未发现会被提交的密钥或个人身份信息，可以 push")
        print("=" * 76)
        return 0

    print("\n[×] 发现 %d 处可疑内容 —— **先别 push**\n" % len(problems))
    seen = set()
    for kind, rel, lineno, frag, why in problems:
        key = (kind, rel, lineno, frag)
        if key in seen:
            continue
        seen.add(key)
        loc = ("%s:%d" % (rel, lineno)) if lineno else rel
        print("  [%s] %s" % (kind, loc))
        print("        %s   %s" % (frag, ("— " + why) if why else ""))
    print("\n处理建议：")
    print("  · 密钥泄露 → **立刻去服务商后台吊销并重新生成**，改完再重试")
    print("    （已 push 过的密钥即使删掉也仍留在 git 历史里，必须吊销）")
    print("  · 私人数据 → 加进 .gitignore，再用 git rm --cached <文件> 移除跟踪")
    print("  · 文档里的示例 → 换成占位符，如 wxid_xxxxxxxxxxxx22")
    print("=" * 76)
    return 1


if __name__ == "__main__":
    sys.exit(main())
