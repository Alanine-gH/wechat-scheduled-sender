# 微信定时群发控制台（Electron 桌面版）

基于 **UIA 语义控件**驱动微信 4.x 桌面客户端的定时群发工具。

- 不依赖鼠标坐标、不依赖剪贴板、不依赖窗口位置——窗口移动、缩放、被遮挡都不影响
- **所有用户文件都放在安装目录下**，不在 `~` / `%APPDATA%` / `%TEMP%` 里创建任何东西

---

## 一、快速开始

双击 **`启动.bat`**（首次会自动 `npm install`，约 100 MB，请耐心等待）。

也可以手动：

```bat
npm install
npm start
```

启动后是一个桌面窗口：**选择账号 → 勾选群/好友 → 编辑消息 → 生成计划 → 开始发送**。
关闭窗口即退出，Python 边车进程会一并回收。

> 调试用：`启动-浏览器模式.bat` 会跳过 Electron，直接用浏览器打开本地服务。

---

## 二、目录结构

```
12_创新课_发送脚本/               ← 安装目录 = 用户文件根目录
├─ 启动.bat                      启动入口（纯 ASCII，见「环境坑」第 1 条）
├─ 启动-浏览器模式.bat             调试用：不启动 Electron
├─ 打包.bat                       打包入口（NSIS 安装包）
├─ launch.js                      Electron 启动器（绕开 npm .bin shim）
├─ build.js                       electron-builder 启动器（绕开 shim + 缓存改道）
├─ package.json                   Electron 应用清单 / 打包配置
├─ requirements.txt               Python 依赖
├─ run.py                         浏览器模式启动器
├─ electron/
│   ├─ main.js                    主进程：目录重定向 + 拉起 Python + 建窗口
│   └─ preload.js                 通过 contextBridge 把端口/token 交给界面
├─ backend/                       Python 边车（自动化引擎）
│   ├─ userdata.py                ★ 把库的写入重定向到安装目录
│   ├─ server.py                  本地 HTTP 服务（token 鉴权 + CORS）
│   ├─ contacts.py                账号 / 群聊 / 好友 枚举
│   ├─ sender.py                  发送引擎（UIA + 截图）
│   └─ planner.py                 发送计划（日期种子随机）
├─ frontend/
│   └─ index.html                 控制台界面（单文件）
│
│  —— 以下都是运行期产生的用户文件，全部在安装目录内 ——
├─ data/
│   ├─ messages.txt               消息池
│   ├─ settings.json              所选的解析模式
│   ├─ media/                     媒体下载默认目录（库用）
│   └─ recall/                    防撤回备份默认目录（库用）
├─ logs/                          运行日志（electron.log / run-*.log / lib-*.log）
├─ shots/                         每次发送的截图（按日期分目录）
├─ cache/                         库的缓存
│   ├─ gate_cache.json            UIA gate RVA 缓存
│   ├─ layout-<机器>.json          布局校准结果
│   ├─ rhythm.json                发送节流状态
│   └─ keys/<账号>.json            数据库密钥缓存
├─ tmp/                           临时目录（解密出的数据库副本）
└─ runtime/                       Electron/Chromium 自己的数据
    ├─ electron/                  userData
    ├─ session/                   会话数据
    └─ crash/                     崩溃转储
```

---

## 三、用户文件策略（核心约定）

**要求：不在用户目录下创建任何东西。** 实现方式分三层：

### 3.1 Electron / Chromium 层

Electron 默认会把 `userData`、会话数据、临时文件、崩溃转储写到
`%APPDATA%\<应用名>` 和 `%TEMP%`。这些必须在 **app ready 之前**用
`app.setPath()` 改道，否则会先创建、再改道，为时已晚：

```js
app.setPath('userData',   <安装目录>/runtime/electron);
app.setPath('sessionData',<安装目录>/runtime/session);
app.setPath('temp',       <安装目录>/tmp);
app.setPath('crashDumps', <安装目录>/runtime/crash);
app.setPath('logs',       <安装目录>/logs);
```

> ⚠️ **光调 `app.setPath('temp')` 是不够的**（这个是实测踩到的坑）：
> 它只改浏览器进程的 PathService，Chromium 的**子进程**（renderer / GPU / network）
> 是用**继承来的 `TEMP`/`TMP` 环境变量**经 `GetTempPath()` 自己解析临时目录的。
> 不覆盖环境变量，它们仍会在真实 `%TEMP%` 里创建文件（实测每次启动留下一个
> 0 字节 GUID `.tmp`）。所以必须同时改自身进程的环境变量，子进程才会继承：
>
> ```js
> process.env.TEMP = DIRS.tmp;
> process.env.TMP  = DIRS.tmp;
> process.env.TMPDIR = DIRS.tmp;
> ```

### 3.2 Python 库层

`wechatauto-replica` 会往这些地方写：

| 库原本的写入位置 | 改道方式 |
|---|---|
| `~/.wechatauto/gate_cache.json` | 覆盖 `accessibility.GATE_CACHE_FILE` |
| `~/.wechatauto/layout-<机器>.json` | 覆盖 `guia.LAYOUT_CONFIG_DIR` |
| `~/.wechatauto/rhythm.json` | 覆盖 `rhythm.STATE_DIR/STATE_FILE` |
| `%TEMP%/wechatauto_db/<账号>/` | 改 `TEMP`/`TMP` 环境变量 + `tempfile.tempdir` |
| `%LOCALAPPDATA%/wechatauto_keys/` | 设 `WECHATAUTO_KEYS_DIR` |
| `<cwd>/wechatauto_logs/` | `WxParam.ENABLE_FILE_LOGGER = False` |
| `~/pane_diag_raw.png`（表情截图诊断） | 兜底重定向 `os.path.expanduser` |
| `~/Documents/wechatauto_media` | 覆盖 `media.DEFAULT_SAVE_PATH` → `data/media` |
| `~/Documents/wechatauto_recall` | 覆盖 `recall.DEFAULT_RECALL_DIR` → `data/recall` |
| `~/Documents/wechatauto_moments` | 兜底重定向 `os.path.expanduser` |

除了逐个覆盖，还加了一道**兜底**：把 `os.path.expanduser('~')` 整体重定向到安装目录。
这一步放在 `import wechatauto` **之前**，这样那些在 import 时就算好的模块级常量
（如 `recall.DEFAULT_RECALL_DIR`）也会落到安装目录下，行内调用也不再漏。
全部逻辑在 `backend/userdata.py`。

> **为什么重定向 `expanduser` 是安全的**（已全量核对库里 15 处用法）：
> 它们全部是**输出/缓存/演示**路径；微信数据目录的**发现**逻辑压根不用
> `expanduser` —— `db.py` 走的是 `APPDATA` / `LOCALAPPDATA` / 注册表 /
> `USERPROFILE` 环境变量（`grep expanduser db.py` 结果为空）。
>
> 已知的唯一副作用：`media.py` 预览窗「保存原图」的兜底扫描会去安装目录下找
> `Downloads`/`Pictures`，那几个目录不存在 → 该兜底分支不生效。本项目不走这条路径。

> **绝对不能碰的东西**：微信数据目录是靠 `APPDATA` / `LOCALAPPDATA` / 注册表 /
> `USERPROFILE\Documents` **发现**的，属于「输入路径」。所以本程序**不修改**
> `USERPROFILE`、`APPDATA`、`LOCALAPPDATA`。`migrate_legacy` 内部用的是保存下来的
> **真实** `_REAL_EXPANDUSER`，因此仍能正确定位旧目录。

### 3.3 历史遗留迁移

第一次启动时，`userdata.py` 会把过去已经落在用户目录里的缓存**移动**进来
（不删除、目标已存在则保留现有文件），然后清掉空的遗留目录：

- `~/.wechatauto/*` → `<安装目录>/cache/`
- `%LOCALAPPDATA%/wechatauto_keys/*` → `<安装目录>/cache/keys/`

### 3.4 安装位置要求

因为要往安装目录写，**不要把程序装在 `C:\Program Files` 下**（那里普通权限不可写）。
安装到 `D:\WeChatSender` 这类可写位置即可。若安装目录确实不可写，
启动时会弹出明确提示而不是静默失败。

用 NSIS 打包时安装器允许自选目录；也可以打 portable 版本。

---

## 四、功能说明

### 4.1 账号与目标

- **自动枚举本机所有微信账号**（读取 `xwechat_files` 下的账号数据目录）
- 选中账号后自动加载该账号的**全部群聊**和**全部好友**
- 群聊与好友**分组显示**，可多选；支持按名称搜索

> 群聊来自 `chat_room` 表（含已退出/长期不活跃的群，最全）。
> 好友来自 `contact` 表的 `local_type=1`，与微信「通讯录」界面显示的联系人一致。

**多账号提示**：读取某账号数据 ≠ 能给它发消息。发送只能操作**当前登录**的微信窗口。
所选账号不是当前登录账号时，页面顶部会出现橙色警告。

### 4.2 消息内容（两种解析模式）

| 模式 | 行为 |
|---|---|
| **每行一条**（默认） | 一行 = 一条消息；空行与 `#` 开头的行忽略 |
| **多行消息** | 用单独一行 `---` 分隔消息，**块内换行原样保留** |

多行模式适合发带换行的清单/通知：

```
第一行内容
第二行内容
第三行内容
---
另一条消息
```

发出 2 条：第 1 条是含 3 行的多行消息，第 2 条单行。

> ⚠️ 多行模式但没有 `---` 时，**整段会被当成 1 条消息**。
> 页面会实时显示条数（编辑时显示「N 条（未保存）」）并给出蓝色说明。

### 4.3 计划参数

| 参数 | 默认 | 说明 |
|---|---|---|
| 轮数 | 1 | 分几轮发送 |
| 每轮条数 | 0 | **0 = 跟随已选目标数**，每个群/好友恰好 1 条 |
| 每条间隔下限 | 4 分 | 相邻两条消息之间的最短等待 |
| 每条间隔上限 | 6 分 | 相邻两条消息之间的最长等待 |

**间隔是「相邻两条消息之间」**，不是轮与轮之间。所以勾选 30 个群
（1 轮 × 30 条，每条 4~6 分钟）总耗时约 **150 分钟 ≈ 2.5 小时**，每个群各收到 1 条。

**随机种子固定为当天日期**（如 `2026-11-02`），同一天重复生成计划结果完全一致。

### 4.4 内容编排（定时投放，v1.2.0）

针对「每天固定时间发引导语+原文案、30 分钟后补钩子」这类运营方案。

- **绝对时间**：按 `每天 20:00` 这类定点触发，而不是「两条之间随机等 N 分钟」
- **按星期内容**：`data/campaign.txt` 里用 `[mon]..[sun]` 写 7 条引导语，按当天星期取
- **原文案一字不改**：引导语拼在原文案上方，正文整块原样发送
- **双波次剧本**：主文案发出后按设定的延迟（默认 30 分钟）补发资料钩子
- **频次控制**：钩子只在指定星期投（默认周二/周四/周六）；可设每群每周上限
- 执行时生成**按时间排序的事件表**，群 1 的钩子可以插在群 5 的主文案之前

内容格式（`data/campaign.txt`，可从 `campaign.example.txt` 复制）：

```
# === 引导语 ===
[mon] 周一那句
[tue] 周二那句
...
# === 正文 ===
原文案，可以多行，一字不改
# === 钩子 ===
资料钩子文案
```

### 4.5 素材（图片 / 文件，v1.2.0）

- 支持**图片**和**任意文件**，通过剪贴板粘贴送进微信输入框
  （微信工具栏没有「发送图片」入口，粘贴是唯一不依赖鼠标坐标的路径）
- 三种来源：**文件夹轮转** / **路径列表 txt** / **界面选文件**
- 分配方式：轮流取 / 打乱后轮流 / 每个目标都发全部
- 可与文字组合，顺序可选「先素材后文字」或「先文字后素材」

### 4.6 线索接收与周报（v1.2.0）

运营方案的重心往往不在「发」而在「收」，这一块负责统计效果。

- **接收私聊**：轮询本地数据库找出「谁主动私聊了我」，无需 hook
  - 首次纳入轮询的会话只记游标、不产出历史消息，避免灌进旧记录
  - 按关键词分档：`有具体需求`（提到客户/业务）/ `仅领资料` / 其它
- **线索表**：昵称、意图、关键词、跟进状态、备注，可导出 CSV（UTF-8 BOM，Excel 不乱码）
- **周报**：投放量（主文案/钩子）、按人去重的私聊人数、6 项 KPI 达成、砍群建议
- 砍群建议要求该群**两周都真有投放**才给结论，避免刚启用时误判

### 4.7 合规校验（v1.2.0）

发送前扫描文案，拦住暗示「违规开票」的措辞：

- `block` 明确违规暗示（如「粮贩子也能开」「代开发票」「内部渠道」）→ **直接不发**
- `warn` 风险措辞（如「一定能办」「变通处理」）→ 发但在界面提示
- **带语境判断**：句子里含「违规/红线/预警/严禁」等标志时视为**指出其违规**，
  放行 —— 否则会把「向经纪人开票即违规」这种合规表态也拦掉
- 风险词用具体短语而非单个汉字（避免「土地承包合同」被误报）

### 4.8 设置中心与自动运行（v1.3.0）

**自动运行**（方案「每天晚 8 点发」的关键，不用每天手动点）：

- 把勾选的群/好友存成自动任务，程序常驻时到点自己跑
- 到点判断：是否启用 → 有无目标 → 今天是否已跑 → 是否允许的星期 →
  是否到点 → 是否超出补跑宽限 → 是否在发送时段
- **补跑宽限**（默认 30 分钟）：晚上 23 点才打开程序时不会立刻发一轮
- **按天去重**：同一天只跑一次；且先记「今天已跑」再执行 ——
  跑到一半被关掉，重启后也不补，重复发送比漏发严重
- 界面上会显示**当前「该不该跑」的判断结果和原因**，
  不然用户只看到「没跑」，不知道是配错了还是在等时间
- **开机自启**（v1.6.0）：登录 Windows 后自动运行本程序（写 HKCU Run 键）。
  自动运行要生效，程序得先在跑 —— 勾上它才算真正无人值守

**28 项设置**，按分组集中管理（表单从后端 schema 自动渲染）：

| 分组 | 关键项 |
|---|---|
| 调度 | 起始时间、钩子延迟、钩子投放日、允许投放的星期 |
| 发送时段 | 启用时段限制、时段起止（**支持跨零点**，如 22:00~06:00） |
| 限额 | 每群每周上限、每群每日上限、单次运行上限、连续失败中止 |
| 节奏 | 间隔上下限、每 N 条休息一次及休息时长 |
| 可靠性 | 单条重试次数与间隔、回读校验超时、发送前清空输入框 |
| 自动运行 | 启用、检查间隔、错过补跑宽限 |
| 接收 | 自动轮询私聊、轮询间隔、高/低意向关键词（可改） |
| 清理 | 日志保留天数、截图保留天数 |

设置写入 `data/settings.json`。**非法值会被拒绝并逐项说明原因**
（例如「休息时长不能大于 3600」），拒绝的值不会写进文件。

**自动清理**：按保留天数回收 `shots/` 下的日期目录与投放日志。
只删自己产生的东西（目录名必须匹配 `^\d{8}$`），并在运行日志里
如实报告回收了多少 —— 默默删掉的话，用户发现截图没了会以为是 bug。

**私聊自动轮询**：按设置间隔定时读库，把新线索自动登记进线索表；
关键词可在设置里改（业务词会变，写死在代码里等于不可调）。

### 4.9 自检、补发、备份与群管理（v1.4.0）

**健康自检**（「4 · 执行」区）：一键把依赖链上 11 个关键点全查一遍 ——
数据目录可写、磁盘空间、微信窗口、微信版本、控件树、本地数据库、
控件树缓存、内容配置、计划配置、截图占用、本机时间。
每项都给「现状 + 建议」，出问题时先点它，不必靠猜。
（每项独立容错：一项坏了不影响其它项。）

**失败补发**（「4 · 执行」区）：发送失败会自动落盘登记，
程序关了再开也还在。可以查看待补发列表并一键补发；
补发成功的条目自动销账，不会重复堆积。

**备份与恢复**（「6 · 设置」区）：把配置、文案、线索打成 zip。
**恢复前会自动快照当前状态**，恢复错了可以退回；
导入时对文件名做白名单校验，防路径穿越。

**群管理**（「5 · 线索与周报」区）：方案要求「连续两周没反馈的群降频或退出」。
每个目标可设三档状态 ——
`正常`（每天发）/ `降频`（隔天发）/ `暂停`（不发）。
周报给出的砍群建议可以一键落为「降频」，再逐个调整。
计划生成时直接生效，并在预览里说明「谁因为什么被跳过」。

### 4.10 自带 Python（v1.5.0）

安装包**已内置 Python 3.14.7**，目标电脑无需预装 Python 即可运行
（`resources/python/python.exe`，启动时优先使用，不依赖 PATH）。

- 内置环境只含运行所需依赖，已精简至约 163 MB
  （去掉只在「朋友圈」用的 opencv 与视频转码用的 imageio_ffmpeg；
   numpy 因 UIA 核心要用而保留）
- `vendor/python` 体积大、不进 git；从仓库构建前先跑一次
  **`准备内嵌Python.bat`**（等价于 `powershell -File tools/prepare-python.ps1`）
  把它造出来，然后再 `打包.bat`
- 若未准备，`打包.bat` 仍能打包，但会提示「安装包需要目标机装 Python」

### 4.11 执行与截图

- 点「开始发送」后按计划执行，实时显示进度与日志
- **每次发送后自动截图**，存到 `shots/<日期>/r<轮次>_<时间>_<目标名>.png`
- 截图用 `PrintWindow`（`PW_RENDERFULLCONTENT`）抓取，窗口被遮挡或不在前台也能截
- 每条消息做**数据库回读校验**，日志里标 `[已校验]` 才算真正发出

---

## 五、技术原理

### 5.1 为什么需要「热激活」

微信 4.x 界面是 Qt 自绘的。`QAccessible` 在响应 `WM_GETOBJECT` 前会检查 `Weixin.dll`
里一个运行时 active byte（accessibility gate）。冷启动未启用读屏时该字节为 `0`，
此时 UIA 只能拿到 Qt 外壳（约 2 个节点），会话列表、输入框全都看不到。

把这个字节热写为 `1` 后，`mmui::` 控件树**立即实体化**（实测从 2 个节点变为 90+ 个），
出现 `session_list`、`chat_input_field`、`发送` 按钮等真实控件，无需重启微信。

> **关键风险**：微信重启、升级、重新登录都会把这个字节重置为 `0`。
> 程序每次运行前都会重新写入并校验（`sender.py` 的 `wake_accessibility`，
> 失败会重试 3 轮），不会假设「昨天能用今天就能用」。
>
> **更要注意：微信一旦升级，gate 的 RVA 就变了，而且新版本可能扫不准。**
> 实测：微信从 `4.1.15.13` 自动更新到 `4.2.0.11` 后，gate RVA 从
> `0xb135c38` 变成 `0xb507c08`，库只是「扫到一个候选值就标记为已验证」，
> 结果控件树**时有时无** —— 表现为程序一会儿报「控件树未能实体化」，
> 一会儿又能连上却找不到搜索框。
> 出现这种飘忽不定的情况，**先把微信退回已验证的版本**：
> 关掉微信自动更新、重装 `4.1.15.13`，并删掉 `cache/gate_cache.json` 让它重扫。

### 5.2 发送路径（全程无坐标）

| 步骤 | 方式 |
|---|---|
| 打开会话 | 按 `AutomationId=session_item_<名称>` 定位并 Invoke；找不到则用搜索框 |
| 校验目标 | 读输入框 `Name`（该属性反映当前会话名），防止重名发错 |
| 写入内容 | `ValuePattern.SetValue()` —— 绕开剪贴板与输入法，**换行也能原样写入** |
| 触发发送 | 点击 `mmui::XOutlineButton`（Name=`发送`） |
| 结果校验 | 输入框清空 + 数据库回读命中 |

**实测踩过的坑（已修复）**：

1. 发送按钮的 `InvokePattern` **无效**（调用后输入框不清空、消息不发），必须用 `Click()` 或回车。
2. 发送按钮**只在输入框有内容时才渲染出来**，必须先写内容再查找按钮。
3. **多行消息**用 `ValuePattern.SetValue()` 可整体写入，微信渲染为**一个气泡**
   （实测一条含 2 个换行的消息显示为 3 行气泡）。早期按行拆分会把一条多行消息误发成多条。
4. **绝对不要拿 wxid 去搜索框搜**。微信搜索框不认 wxid / `xxx@chatroom`，
   输入它只会命中结果列表里的「搜索网络结果」，点进去打开的是**搜一搜面板**
   （公众号文章列表），而不是会话 —— 表现就是「搜群名却出来一堆文章」。
   同理，**搜索框里也不要按回车**：回车同样会触发网络搜索。
   现在改为只用显示名搜索，并显式跳过所有网络搜索入口行。
5. **微信最小化时找不到窗口**。窗口最小化时 `GetWindowRect` 返回图标尺寸
   （实测 160×28），按「尺寸必须 > 300」直接筛掉就会误判成「没找到微信」，
   而恢复逻辑在查找之后才执行、永远没机会跑。现在遇到尺寸过小的候选会先
   `IsIconic` 判断并 `SW_RESTORE` 恢复，再重新量尺寸。
6. **就绪检查不能只数 `mmui::` 控件且层级太浅**。`mmui::MainView` 本身就在第 4 层
   （窗口 → Pane → QWidget → QStackedWidget → MainView），只数到第 4 层计数过少，
   会把「已就绪」误判成「gate 未生效」。现在优先按 `AutomationId=MainView` 判定。
7. **搜索框会「缩起来」，此时不能当成致命错误**。微信窗口变窄、或拖动分隔条把
   会话列表压窄时，搜索输入框会折叠成只剩一个放大镜图标，
   `mmui::XValidatorTextEdit` 直接不存在；也可能还存在但宽度只剩几像素，
   `SendKeys` 打不进字却让调用方以为「已经输入了关键词」。
   早期实现遇到这种情况会 `raise RuntimeError`，整条目标直接失败。
   现在改为：`_search_box()` 不抛异常（并校验宽度 ≥ 40px），
   `_ensure_search_box()` 依次尝试「直接找 → `Ctrl+F` 唤出 → 点 `mmui::XSearchField`
   容器 → 点容器里的放大镜图标」，全部失败才放弃，且放弃时只让这一个目标失败，
   不会打断整轮任务。

### 5.3 好友 vs 群聊（重要）

| local_type | 含义 | 本机行数 |
|---|---|---|
| 1 | **真实好友** | 23（含群/系统号，需过滤） |
| 2 | 群聊 | 16 |
| 3 | **群成员**（群里能看到但**没加好友**） | 8089 |

> ⚠️ `local_type=3` **不是好友**！本机 8089 行里有 8088 行同时存在于
> `chatroom_member` 表，也就是「从群里能看到的人」。把它当好友会列出上万人。
> 正确做法：`local_type=1` 排除群（`@chatroom`）、公众号（`gh_`）、企微（`@openim`）、
> 自己的账号、系统通知号（`fmessage`/`medianote`），结果与微信「通讯录」完全一致。

### 5.4 Electron 与 Python 的分工

- **Electron 主进程**：目录重定向、拉起/回收 Python 边车、建窗口、系统集成
- **Python 边车**：WeChat 自动化（`wechatauto-replica` + `uiautomation`），
  对外提供本地 HTTP 接口
- **渲染进程**：控制台界面，通过 preload 暴露的端口 + token 访问本地服务

端口由系统分配（`--port 0`），主进程从子进程 stdout 读 `PORT=<n>`；
每次启动生成随机 token，`/api/*` 都校验 `X-Token`，静态页面不需要。
服务只监听 `127.0.0.1`。

---

## 六、环境相关的坑（本机实测）

### 6.1 `ELECTRON_RUN_AS_NODE=1` 会让 Electron 退化成 Node

本机环境里全局设置了 `ELECTRON_RUN_AS_NODE=1`。它会让 `electron.exe` 以纯 Node 运行，
此时 `require('electron')` 返回的是 npm 包的**路径字符串**而不是 Electron API，
报错为：

```
TypeError: Cannot read properties of undefined (reading 'isPackaged')
```

`launch.js` 在 spawn 时对子进程环境 `delete ELECTRON_RUN_AS_NODE`，已修复。

### 6.2 路径含 `&` 会打断 npm 生成的 `.bin` shim

项目路径若含 `&`（例如 `D:\some&dir\project`），npm 生成的
`node_modules\.bin\electron.cmd` 里路径没加引号，`cmd.exe` 把 `&` 当命令分隔符：

```
'...\node_modules\.bin\' is not recognized as an internal or external command
```

所以 `package.json` 里 `start` 指向 `node launch.js`，用 `child_process.spawn`
（不走 shell）拉起，`&`、空格、中文都不会有问题。

### 6.3 npm 11 默认拦截 postinstall，Electron 二进制不会下载

```
npm warn install-scripts  electron@33.4.11 (postinstall: node install.js)
```

npm 11 会跳过未批准的安装脚本，于是 `node_modules/electron/dist` 缺失，
`npm start` 报 `Cannot find module ...\electron\cli.js`。

**修复方式（二选一）**：

```bat
:: 方式一：批准后重装
npm install-scripts approve electron
npm install
```

```bat
:: 方式二：手工解压（本机采用，2 秒完成）
node -e "const z=require('child_process');console.log(require('electron'))"
:: 然后解压 electron 缓存里的 zip 到 node_modules\electron\dist，
:: 并在 node_modules\electron\path.txt 写入 electron.exe
```

`启动.bat` 会检查 `node_modules\electron\package.json` 是否存在并自动 `npm install`。

### 6.4 `.bat` 里不能写中文

`cmd.exe` 按系统 ANSI 代码页（中文系统 936/GBK）逐字节读取 `.bat`。
UTF-8 中文字符的尾字节会落在 GBK 前导字节区间并**吃掉后面的换行符**，
导致批处理行错位，表现是满屏 `'xxx' is not recognized as an internal or external command`。

因此 `启动.bat` 保持**纯 ASCII**（实测 0 个非 ASCII 字节、无 BOM），
中文提示全部由 `run.py` / Electron 输出。

> 如果你要改 `启动.bat`，**不要往里面加中文**；改提示文字请改 `run.py`。

### 6.5 构建期的 Electron 下载缓存

`npm install` 时 Electron 的安装脚本会把二进制下载缓存到
`%LOCALAPPDATA%\electron\Cache`（约 110 MB）。这属于**构建期**产物，不是运行期用户文件。

本机已把它移入 `<安装目录>\cache\electron\`，后续安装可用
`ELECTRON_CACHE=<安装目录>\cache\electron` 复用，不再写用户目录。

---

## 七、打包

双击 **`打包.bat`**，或：

```bat
npm run dist            :: NSIS 安装包（可选安装目录）
npm run dist:portable   :: 免安装便携版
```

产物在 `dist/`：`WeChatSender-<版本>-x64.exe`（约 78 MB）。

打包配置里 `extraResources` 会把 `backend/` 与 `frontend/` 放进 `resources/`，
主进程在打包模式下从那里加载。

> 打包后 `app.isPackaged` 为真，安装目录取 `path.dirname(app.getPath('exe'))`；
> 开发模式取项目根目录。

### 打包环节的三个坑（都已在 `build.js` 里处理）

1. **不能用 `electron-builder` 直接跑 npm script**
   路径含 `&` 时会打断 `.bin` shim，报
   `Cannot find module 'D:\静静\electron-builder\cli.js'`。
   `build.js` 直接定位 `node_modules/electron-builder/cli.js` 并用 `spawn` 拉起。

2. **`electron-builder` 默认下载 Electron 二进制**
   走 GitHub releases，国内经常超时。配置里加了
   `"electronDist": "node_modules/electron/dist"`，直接用本地已解压的 Electron，**完全离线**。

3. **`nsis` / `winCodeSign` 等构建二进制也走 GitHub**
   `build.js` 默认把它们指向 npmmirror 镜像：

   ```
   ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
   ```

   想用官方源就把它设成空字符串再跑。

另外，`build.js` 把构建缓存钉在 `<项目>\cache\electron-builder`，
Electron 二进制缓存钉在 `<项目>\cache\electron` —— 与「所有文件都在安装目录下」一致，
不写 `%LOCALAPPDATA%`。

> 提示：`dist\win-unpacked\` 本身就是**可直接运行的绿色版**，
> 不装安装包也能用（双击里面的 `WeChatSender.exe`）。

---

## 八、已知限制

1. **微信必须已登录**。数据库密钥存在于微信进程内存中，首次提取后会缓存到 `cache/keys/`。
2. **发送是 GUI 操作**，需要微信窗口可恢复（最小化/托盘状态会自动恢复）。
3. **多账号**：只能给当前登录的账号发消息；切换账号需先在微信里切换登录。
4. **微信升级后** gate 的 RVA 会变。程序会尝试自动扫描，失败需等库更新。
5. **高频群发有风控风险**。已用日期种子做随机抖动，建议不要长期满负荷运行。
6. **安装目录需可写**，不要装在 `C:\Program Files` 下。

---

## 九、验证记录

「不在用户目录创建任何东西」这条做过一次**独立验证**（另开一个进程，自己取基线、
自己冷启动、自己归因）。第一轮**不通过**，抓到一个真实反例：

> Electron 的 Chromium **渲染进程**每次启动都会在真实 `%TEMP%` 里创建一个
> 0 字节 GUID `.tmp`。用 Restart Manager 归因确认占用者是
> `--type=renderer --user-data-dir=<安装目录>\runtime\electron`。
> 单独跑 Python 服务完成同一次数据库读取则零新增 —— 反证不是 Python 侧。

根因就是 3.1 节说的那条：`app.setPath('temp')` 管不到 Chromium 子进程，
它们用继承的 `TEMP`/`TMP` 环境变量。补上 `process.env.TEMP/TMP/TMPDIR` 后复验：

| 复验项 | 结果 |
|---|---|
| 真实 `%TEMP%` GUID `.tmp`（3 次冷启动，含一次 78 秒） | 10 → 10，**零新增**（按文件名逐条比对） |
| 运行时那 1 个 `.tmp` 的位置 | 在 `<安装目录>\tmp` 内，归因到 renderer |
| 退出后 | 自动清理 |
| `auto_detect_db_dir()` | 仍返回真实 `D:\WeiChat\xwechat_files` |
| `/api/contacts` | 群=29 / 好友=7，与改前一致 |
| `expanduser('~otheruser')` | 未劫持，仍走真实实现 |
| 6 处用户目录 + 4 个潜在写点 | **全部 ABSENT** |

**结论：通过。** 另有 3 个非阻塞小问题（文档与实现自相矛盾、`expanduser` 计数写错、
`media._save_once` 兜底扫描失效）已一并修正。

> 说明：`%TEMP%` 里现存的 10 个 GUID `.tmp` **不是本程序产生的** ——
> 它们最晚的一个时间戳早于本程序 Electron 二进制存在的时间（13:55:50），
> 很可能来自本机其它 Electron 应用（VS Code / DSH）。程序未删除它们。

---

## 十、常见问题

**Q：提示「微信控件树未能实体化」？**
A：① 确认微信已登录且窗口没被完全关闭；② 若刚重启过微信，重试即可；③ 仍失败重启本程序。

**Q：双击 `启动.bat` 后满屏 `'xxx' is not recognized`？**
A：`.bat` 被存成 UTF-8（含中文）导致 cmd 解析错位。当前版本已改为纯 ASCII。
若你手动编辑过，请勿加入中文（见 6.4）。

**Q：`npm start` 报 `Cannot find module ...\electron\cli.js`？**
A：Electron 二进制没解压出来（npm 11 拦截了 postinstall，见 6.3）。
执行 `npm install-scripts approve electron && npm install`。

**Q：我想发一条带换行的消息，结果被拆成好几条？**
A：切到「多行消息」模式，在每条之间单独放一行 `---`。若整段就是一条，
多行模式下不需要任何 `---`，页面会显示「1 条」。

**Q：明明写了 8 行，为什么显示只有 1 条？**
A：当前是「多行消息」模式，连续的几行属于同一条。要分成多条请插入 `---`。

**Q：端口被占用？**
A：Electron 模式用系统分配端口，不会冲突。浏览器模式可 `启动-浏览器模式.bat 8792` 换端口。

**Q：好友列表和微信通讯录对不上？**
A：确认 `contacts.py` 里的 `_FRIEND_SQL` 用的是 `local_type=1`（不要用 3，那是群成员）。

**Q：怎么确认程序没有往用户目录写东西？**
A：看界面顶部「用户文件目录」一行，会显示实际生效的根目录；
也可调用 `GET /api/paths` 查看各目录。程序会拒绝使用不可写的安装目录。