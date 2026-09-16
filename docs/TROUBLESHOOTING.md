# 踩坑记录（实测，非推测）

这份文档记录的都是**实测**发现的坑，每条都写清：症状 → 假设 → 实测证据 → 根因 → 修法。

有些坑的价值不在于它本身，而在于**错误的排查方向有多贵** ——
下面好几条，我第一版诊断都是错的，是实测把结论纠正过来的。

---

## 1. @ 群成员从来就没成功过：弹层压根没出现

**症状**：群里回复时 @ 不生效，日志报 `未在成员列表中找到：XXX`。

**我的第一个假设（错）**：备注和昵称大小写不一致
（`备注='Xiaoming'` vs `昵称='XiaoMing'`），而 `at_member` 的匹配是
`member in t` 大小写敏感。

**实测证据**：写了个诊断，真的在群里敲 `@`，再 OCR 弹层该出现的区域 ——
**读到的全是聊天气泡**，没有任何成员名。说明**弹层根本没打开**，
跟名字匹配毫无关系。

**再实测**：dump UIA 树，对比两种输入方式：

```
type_unicode('@')  →  弹层不出现，OCR 只读到聊天气泡
SendKeys('@')      →  立刻出现：
    WindowControl    aid='MentionPopover'     class='mmui::XPopover'
      GroupControl   class='mmui::ChatMentionList'
        ListControl  aid='chat_mention_list'  class='mmui::XTableView'
          ListItemControl  name='阿明'
          ListItemControl  name='Ming_2024'
    （输入框 ValuePattern = '@'）
```

**根因**：上游 `WeChatGUI.at_member()` 用 `type_unicode('@')` 注入 **Unicode 字符**，
微信不把它当作 @ 提及触发键，成员弹层不会出现 → OCR 在聊天区里怎么找都找不到。

**修法**：改用 UIA 路径 —— `SendKeys('@')` 触发弹层 → 从 `chat_mention_list`
读成员名（UIA 直接给出 `Name`，不需要 OCR）→ UIA 点击 → 校验 → 追加正文 → 回车。
`tools/verify_at.py` 现在可以在 dry-run 下验证每一步。

**顺带发现**：微信把提及渲染成内联「芯片」，输入框 `ValuePattern` 的值是
**U+FFFC**（OBJECT REPLACEMENT CHARACTER）而不是 `@名字`。
所以校验条件必须是 `"@" in v or "\ufffc" in v`，只判断 `"@"` 会永远失败。

**另一个坑**：`at_member` 失败时**已经往输入框敲了 `@`，且弹层还开着**。
不清理就回退普通发送，回复会变成「@正文」甚至被弹层吃掉。
现在回退前先 `Esc` 关弹层 + `Ctrl+A` / `Delete` 清空输入框。

---

## 2. 群里 @ 人必须用群昵称

**症状**：即使弹层正常，用微信昵称去 @ 也找不到人。

**实测数据**（一个 55 人的群）：

| wxid | 群昵称 | 全局昵称 |
|---|---|---|
| `wxid_aaaa…` | **阿明** | Ming_2024 |
| `wxid_bbbb…` | **老王** | WangSir |
| `wxid_cccc…` | **小李** | li_xiao |

该群 45 人设了群名片，**45 人的群昵称与全局昵称 100% 不同**。

**根因**：微信 @ 出来的名字是**群昵称（群名片）**，不是微信昵称、也不是你设的备注。
而库里 `contact.nick_name` 存的是全局昵称。

**群昵称存在哪**：`contact.db` 的 `chat_room.ext_buffer`，是个 protobuf：

```
repeated f1 { f1: wxid, f2: 群昵称(可缺省), f3: flag, f4: 邀请人 }
```

解析要点：
- 条目**可能没有 f2**（该成员没设群名片）—— 第一版解析器跳过这些条目，
  导致「55 人的群只解出 45 条」，误以为解析不完整。应当保留为空串。
- 上游 `get_group_members()` 只返回全局 `nick_name` / `remark`，**没有群昵称**。

**显示名规则**：`群昵称 or 备注 or 昵称 or wxid`（群昵称为空时微信显示的就是全局昵称）。

**连带问题**：**「@我」检测也必须用群昵称**。实测机器人在某个群的群昵称与全局昵称
不同，只用全局昵称判断会**漏掉全部 @**。

---

## 3. 群内消息的发送者只是「群内序号」

**症状**：日志里出现 `未在成员列表中找到：8` / `：10` / `：7`。

**根因**：微信 4.x 群聊消息的 `real_sender_id` 是**群内序号**，
查 contact 表查不到时会退化成数字串（`"8"`），而代码把它当昵称去 @。

**修法**：真实 wxid 就藏在正文前缀里，格式是 `wxid_xxxx:\n真正的正文`。
从正文解出 wxid，再把前缀从正文剥掉（喂给模型的上下文也更干净）。

```
修复前：10 / 8 / 7   （永远找不到）
修复后：群昵称或全局昵称
```

---

## 4. disabled 的 Listbox 会静默吞掉 `insert`

**症状**：「指定会话」列表**永远是空的**，怎么点都选不了，且**毫无报错**。

**实测**：

```python
lb.configure(state='disabled')
lb.insert('end', 'A'); lb.insert('end', 'B')
lb.size()      # → 0   两条都没进去
lb.configure(state='normal')
lb.insert('end', 'C')
lb.size()      # → 1
```

**根因**：Tk 的 `Listbox` 在 `state="disabled"` 时，`insert` / `delete`
**不报错、不生效**。而「全部会话」模式会把列表禁用 ——
于是点「载入会话列表」看着成功了，实际一条会话都没进列表；
切回「指定会话」后是个空列表，自然什么都选不了。

`selection_set` 同理，在 disabled 时也无效。

**修法**：填充前先置 `normal`，填完再按当前模式恢复禁用状态；
并**校验实际条目数**，数量不符时报错（而不是继续假装成功）。

**教训**：一个「不报错的失败」比报错危险得多。凡是程序化写入 UI 控件之后，
都应该断言写入是否真的生效。

---

## 5. 我猜错的第一个诊断：`exportselection`

**症状**：用户反馈「指定会话无法多选」。

**我的假设（错）**：Tk 的 `Listbox` 默认 `exportselection=1`，会把选中「导出」
为选区，Windows 上焦点一移到别的控件（点输入框）选中就被清空。

**实测（对照实验）**：

```
exportselection=True   初始=3  焦点→Entry=3  焦点→Text=3  点击Entry=3  → 保持
exportselection=False  初始=3  焦点→Entry=3  焦点→Text=3  点击Entry=3  → 保持
```

Tk 8.6 上两者**没有区别**。假设不成立。

**真正的两个原因**：
1. 第 4 条（disabled 吞 insert）—— 列表压根是空的
2. `selectmode="extended"` 下**单击会替换整个选中**，必须按 Ctrl 才能多选，
   而界面**一个字都没说**

**修法**：改成**单击即切换选中**（Shift 点击保留原生连选），
加「全选 / 清空选择」按钮、实时「已选 N 个」计数、模式联动禁用、启动时拦截并给操作提示。

**教训**：`exportselection` 是个「经典答案」，很容易先入为主。
**先做对照实验，再下结论。**

---

## 6. 工作目录不可写 → 整个库瘫痪（报错还伪装成别的问题）

**症状**：

```
微信窗口不可用，无法发送：[WinError 5] 拒绝访问。: 'wechatauto_logs'
```

看起来像窗口问题，其实是**文件权限问题**。

**根因链**：

1. 上游 `logger.py` 里日志目录是硬编码相对路径：`Path("wechatauto_logs")`
2. 所以它取决于**启动时的工作目录**；从别处（不可写目录）启动就建不了目录
3. 更糟的是 FileHandler 是**惰性创建**的：`_ensure_file_logger()` 挂在
   **每一个** `wxlog.xxx()` 调用上；创建失败后 `file_handler` 仍是 `None`，
   于是**每次日志调用都重抛**同一个异常
4. 结果：`WeChatGUI()` 一构造就抛（因为构造里有日志调用），
   而调用方把它当成「窗口不可用」报了出来

**复现**（从 `C:\Windows\System32` 运行）：

```
当前工作目录: C:\Windows\System32
[×] PermissionError: [WinError 5] 拒绝访问。: 'wechatauto_logs'
```

**修法**：
- 启动时 `os.chdir` 到脚本目录，且**在导入 wechatauto 之前**
  （`WxParam.DEFAULT_SAVE_PATH` 是导入时用 cwd 算出来的）
- 预探一次文件日志，失败就自动 `WxParam.ENABLE_FILE_LOGGER = False`，
  保证只丢文件日志、功能不受影响
- 报错文案区分「真的没窗口」/「权限失败」/「其他」

**教训**：**报错措辞不准比没有报错更糟** —— 它会把排查引向完全错误的方向
（这次把用户和我都引到了 @ 上）。

---

## 7. 导入 wechatauto 前必须 flush

**症状**：脚本输出重定向到文件时，**开头几行消失**；交互式运行却正常。

**排查**：对比 `python x.py` 和 `python -u x.py` 的输出行数 —— 差 11 行。
差距恰好是「导入 wechatauto 之前打印的内容」。

**根因**：wechatauto 会导入 colorama，colorama 在 Windows 上用 `AnsiToWin32`
**重新包装 `sys.stdout` / `sys.stderr`**，此前**未 flush** 的输出会连同旧包装一起被丢弃。

**修法**：在所有 `import wechatauto` 之前 `sys.stdout.flush(); sys.stderr.flush()`。

**这个坑的元级教训**：我自己的测试脚本也中了同一招 ——
`verify_memory.py` 在第 10 节才首次导入 wechatauto，于是前 9 节的报告全被丢掉，
**测试报告只剩下最后几行**。有一次失败（"过期记忆不会被载入"）就因为看不到上下文，
多花了一轮才定位。

> **测试报告丢输出，比没有测试更危险。** 现在所有工具脚本统一走
> `tools/_bootstrap.py` 的 `bootstrap()`，它会在导入前 flush。

---

## 8. `comtypes` 需要写代码缓存目录

**症状**：`import wechatauto` 报
`PermissionError: [WinError 5] 拒绝访问。: 'C:\Users\...\AppData\Roaming\Python'`

**根因**：`uiautomation → comtypes` 需要写生成的 COM 模块。
它先试 `site-packages\comtypes\gen`，不可写就回落到 `%APPDATA%\Python\...\comtypes_cache`。
受限/只读/沙箱环境下两处都可能被拒。

**修法**：程序把这三种导入失败分别翻译成可读提示（缺依赖 / 权限 / 其他），
见 `explain_import_error()`。

---

## 9. 微信「关到托盘」时无法发送，而且报错与事实不符

**症状**：`未找到微信主窗口，请确认微信已登录并运行` —— 但微信明明在运行。

**实测窗口状态**：

```
hwnd   visible  iconic  title   class
131840 False    False   '微信'   Qt51514QWindowIcon
```

`visible=False` 且 `iconic=False` → 不是最小化，是**被隐藏**（微信 4.x 点关闭 = 收进托盘）。

**根因**：上游 `_find_main_window()` 只在**可见的顶层窗口**里评分查找，
隐藏窗口直接被跳过。而且 `ShowWindow` 对这个 Qt 窗口**无效**
（可见性由微信自己管理），实测 `SW_SHOW` / `SW_RESTORE` / `SW_SHOWNORMAL` 都试过。

**修法**：
- **最小化** → 自动 `SW_RESTORE` 恢复
- **关到托盘** → 只能**手动点托盘图标**；程序会明确告诉用户是这种情况
  （`describe_wechat_window()`），而不是含糊地说「请确认微信已登录并运行」

**结论**：GUI 自动化有一个硬边界 —— **窗口不可见就什么也做不了**。

---

## 10. 上下文轮数填大了无效（静默截断）

**症状**：界面「上下文轮数」填 10（期望带 20 条历史），实际只带 12 条，且**毫无提示**。

**根因**：历史缓冲区硬编码 `deque(maxlen=12)`，12 条 = 6 轮。

**修法**：上限提到 200 条（100 轮），超限时明确警告。

**顺带**：`deque(maxlen=12)` 这种「魔法数字」应该提成命名常量
（现在是 `HISTORY_MAX_MESSAGES`），否则没人知道它对界面行为有约束。

---

## 11. 启动回填取样太窄

**症状**：明明有近期消息，回填却报「没有找到可用的近期消息」。

**根因**：回填按 `get_messages(limit=N)` 取 N 条**原始**消息，再过滤成文本。
群里图片/表情/链接多的时候，30 条原始消息里可能只有 3 条文本 —— 等于没回填。

**修法**：扩大到 `N*4` 的取样池再筛，按「回填 N 条**文本**消息」为目标。

```
修复前：30 条原始消息 → 筛出 3 条
修复后：30 条原始消息 → 筛出 12 条
```

---

## 12. 人物档案会无限增长 / 抽取被重复排队

**两个 bug**（都是写测试时逼出来的）：

1. `note_message` / `merge` 都没有执行人数上限淘汰 → 档案库只增不减
2. `should_extract` 只在**抽取完成后**才记录时间戳 → 消息密集时同一个人
   会被反复排队、**重复调用大模型**

**修法**：两处都补上淘汰逻辑；改成「决定排队时先占位」（`mark_extracting`），
并配时间间隔双重节流。

---

## 排查方法小结

这些坑有共同的排查套路，值得复用：

1. **先做对照实验，别信「经典答案」**
   （第 5 条 `exportselection` 就是被经典答案带偏的）
2. **不要猜 OCR 看到了什么，把原始结果打出来**
   （第 1 条：打印 OCR 文本，一眼看出弹层没开）
3. **控件名/路径失效时 dump 结构**
   （第 1 条 dump UIA 树，直接看到 `MentionPopover`）
4. **对比「有缓存 vs 无缓存」/「缓冲 vs 无缓冲」的输出差异**
   （第 7 条靠 `-u` 与不加 `-u` 的行数差定位）
5. **程序化写 UI 之后要断言是否真的生效**
   （第 4 条：`insert` 静默失败）
6. **报错文案要能区分不同原因**
   （第 6 和第 9 条都是「报错措辞把排查引向错误方向」）
