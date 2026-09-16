# -*- coding: utf-8 -*-
"""界面级验证：直接构建真实界面，对真实控件做断言与交互。

做法：把 Tk.mainloop 换掉，在界面构建完成后对真实控件树做检查，
跑完即销毁窗口（不会留下界面）。
"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
sys.stdout.flush()

import wx_ai_bot

FAILS = []


def check(name, cond, extra=""):
    print("  [%s] %s%s" % ("√" if cond else "×", name,
                           ("  → %s" % extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def find_all(widget, cls):
    out = []
    for child in widget.winfo_children():
        if isinstance(child, cls):
            out.append(child)
        out.extend(find_all(child, cls))
    return out


def find_button(root, text):
    for b in find_all(root, tk.Button):
        try:
            if str(b.cget("text")).strip() == text:
                return b
        except Exception:
            pass
    for b in find_all(root, getattr(tk, "ttk", tk).Button) if hasattr(tk, "ttk") else []:
        pass
    return None


# tkinter.ttk 的 Button 不是 tk.Button 子类，单独处理
from tkinter import ttk  # noqa: E402


def find_button2(root, text):
    b = find_button(root, text)
    if b:
        return b
    for b in find_all(root, ttk.Button):
        try:
            if str(b.cget("text")).strip() == text:
                return b
        except Exception:
            pass
    return None


def find_entries(root):
    return find_all(root, tk.Entry) + find_all(root, ttk.Entry)


def find_labels(root):
    return find_all(root, tk.Label) + find_all(root, ttk.Label)


def all_label_text(root):
    texts = []
    for lb in find_labels(root):
        try:
            texts.append(str(lb.cget("text")))
        except Exception:
            pass
    return texts


print("=" * 74)
print("界面级验证：指定会话多选")
print("=" * 74)

RESULT = {}
orig_mainloop = tk.Tk.mainloop


def fake_mainloop(self):
    root = self
    try:
        root.update_idletasks()
        root.update()
    except Exception:
        pass

    lbs = find_all(root, tk.Listbox)
    check("界面上有且仅有一个会话列表", len(lbs) == 1, len(lbs))
    if not lbs:
        root.destroy()
        return
    lb = lbs[0]

    print("\n-- 列表控件本身的配置 --")
    check("selectmode=%s（支持多选）" % lb.cget("selectmode"),
          str(lb.cget("selectmode")) == "extended", lb.cget("selectmode"))
    check("exportselection=%s（防选中被系统选区机制影响）"
          % lb.cget("exportselection"),
          not bool(int(lb.cget("exportselection"))), lb.cget("exportselection"))

    print("\n-- 单击是否即切换选中（关键：不需要按 Ctrl）--")
    for i, t in enumerate(("会话甲 [wxid_a]", "会话乙 [wxid_b]", "会话丙 [wxid_c]")):
        lb.insert("end", t)
    root.update()

    def click_row(idx):
        """在指定条目上合成一次真实鼠标单击。"""
        box = lb.bbox(idx)
        if not box:
            return
        x = box[0] + max(4, box[2] // 2)
        y = box[1] + box[3] // 2
        lb.event_generate("<Button-1>", x=x, y=y)
        try:
            lb.event_generate("<ButtonRelease-1>", x=x, y=y)
        except Exception:
            pass
        root.update()

    click_row(0)
    n1 = len(lb.curselection())
    check("点第 1 条 → 选中 1 项", n1 == 1, n1)
    click_row(1)
    n2 = len(lb.curselection())
    check("再点第 2 条 → 变成 2 项（多选生效，无需 Ctrl）", n2 == 2, n2)
    click_row(2)
    n3 = len(lb.curselection())
    check("再点第 3 条 → 3 项全选", n3 == 3, n3)
    click_row(1)
    n4 = len(lb.curselection())
    check("再点第 2 条（已选）→ 取消该项，剩 2 项", n4 == 2, n4)

    print("\n-- 选中后焦点移走是否保持 --")
    ents = find_entries(root)
    if ents:
        ents[0].focus_set()
        root.update()
        check("焦点移到输入框后选中仍保持（%d 项）" % len(lb.curselection()),
              len(lb.curselection()) == 2, len(lb.curselection()))
    else:
        check("找到输入框用于焦点测试", False)

    print("\n-- 「全选」「清空选择」按钮 --")
    b_all = find_button2(root, "全选")
    b_none = find_button2(root, "清空选择")
    check("界面存在「全选」按钮", b_all is not None)
    check("界面存在「清空选择」按钮", b_none is not None)
    if b_all:
        lb.selection_clear(0, "end")
        root.update()
        b_all.invoke()
        root.update()
        check("点「全选」→ 全部 %d 项被选中" % lb.size(),
              len(lb.curselection()) == lb.size(), len(lb.curselection()))
    if b_none:
        b_none.invoke()
        root.update()
        check("点「清空选择」→ 选中归零", len(lb.curselection()) == 0,
              len(lb.curselection()))

    print("\n-- 界面上的文字提示 --")
    texts = all_label_text(root)
    joined = "\n".join(texts)
    check("有「多选」操作说明", "多选" in joined)
    check("说明里写了「点击…选中/取消」", "选中/取消" in joined)
    check("说明了「全部会话」模式下列表不可用",
          "全部会话" in joined and "不可用" in joined)
    check("有「尚未选择任何会话」的状态提示",
          any("尚未选择" in t or "已选" in t for t in texts),
          [t for t in texts if "选" in t][:4])

    print("\n-- 单选/多选状态联动 --")
    scope_vars = [v for v in (root.tk.globalgetvar(n) for n in ())]
    # 通过单选按钮切换模式，验证列表可用性与提示
    radios = find_all(root, ttk.Radiobutton) + find_all(root, tk.Radiobutton)
    r_all = [r for r in radios if "全部会话" in str(r.cget("text"))]
    r_sel = [r for r in radios if "指定会话" in str(r.cget("text"))]
    check("存在「全部会话」「指定会话」两个单选", bool(r_all) and bool(r_sel))
    if r_all and r_sel:
        r_all[0].invoke()
        root.update()
        st_all = str(lb.cget("state"))
        check("切到「全部会话」→ 列表被禁用（state=%s）" % st_all,
              st_all == "disabled", st_all)
        check("切到「全部会话」→ 提示写明列表不生效",
              any("全部会话" in t and ("不生效" in t or "不可用" in t)
                  for t in all_label_text(root)),
              [t for t in all_label_text(root) if "全部" in t][:3])
        r_sel[0].invoke()
        root.update()
        st_sel = str(lb.cget("state"))
        check("切回「指定会话」→ 列表恢复可用（state=%s）" % st_sel,
              st_sel == "normal", st_sel)
        check("未选中任何会话时有红色警示提示",
              any("尚未选择" in t for t in all_label_text(root)),
              [t for t in all_label_text(root) if "选" in t][:4])
        # 用真实点击（而不是程序化 selection_set）验证计数提示会刷新
        click_row(0)
        check("点击选中后有「已选 N 个会话」计数提示",
              any("已选" in t and "会话" in t for t in all_label_text(root)),
              [t for t in all_label_text(root) if "选" in t][:4])
        click_row(1)
        check("再点一条后计数变成 2",
              any("已选 2 个会话" in t for t in all_label_text(root)),
              [t for t in all_label_text(root) if "已选" in t][:4])
        click_row(0)
        click_row(1)
        check("全部取消后回到「尚未选择」警示",
              any("尚未选择" in t for t in all_label_text(root)),
              [t for t in all_label_text(root) if "选" in t][:4])

    RESULT["ok"] = True
    root.destroy()


tk.Tk.mainloop = fake_mainloop
sys.argv = [a for a in sys.argv if a not in ("--smoke",)]
try:
    wx_ai_bot.main()
except SystemExit:
    pass
except Exception as exc:
    import traceback
    traceback.print_exc()
    check("界面构建未抛异常", False, exc)
finally:
    tk.Tk.mainloop = orig_mainloop

print("\n" + "=" * 74)
if FAILS:
    print("有 %d 项未通过：" % len(FAILS))
    for f in FAILS:
        print("   -", f)
else:
    print("结论：全部通过")
print("=" * 74)
sys.exit(1 if FAILS else 0)
