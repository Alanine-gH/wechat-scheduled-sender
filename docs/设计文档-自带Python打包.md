# 设计文档：自带 Python 打包（v1.5.0）

- **日期**：2026-11-03
- **背景**：v1.4.0 验证打包产物启动时发现，`resources/python/` 里没有解释器，
  程序回落到系统 `python`。这意味着**发给别人用，对方电脑没装 Python 就起不来**，
  会弹「无法启动后端服务」。

## 一、路线选择

| 方案 | 体积 | 改动 | 风险 |
|---|---|---|---|
| **embeddable Python**（选用） | +163 MB | **零代码改动**（`pythonCandidates()` 已优先找 `resources/python/python.exe`） | 依赖要手工装、`. _pth` 有坑 |
| PyInstaller 单文件 | +30~50 MB | 要改 main.js 走 `server.exe` | 首次启动慢（解压到 temp）、易被杀软误报 |
| 维持现状 | 0 | 无 | **对方没 Python 就用不了** |

选 embeddable 的决定性理由：`pythonCandidates()` 早就写好了「优先找内置 python」，
所以这条路**不用改一行代码**，只是把解释器放进打包资源。风险更小的方案优先。

## 二、实现步骤

1. 从 npmmirror 取 `python-3.14.7-embed-amd64.zip`（12 MB，7 秒下完）
2. 解压到 `vendor/python/`
3. 改 `python314._pth`：加 `Lib\site-packages` 和 `import site`
4. 用**系统 pip** `--target` 把 5 个依赖装进 `vendor/python/Lib/site-packages`
   （同 minor 版本，wheel 二进制兼容）
5. 精简掉不用的包
6. `package.json` 加 `extraResources: {"from":"vendor/python","to":"python"}`
7. `vendor/` 进 `.gitignore`（163 MB 的构建产物不该进仓库）

## 三、踩的四个坑

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | `Fatal Python error: Failed to import encodings module` | PowerShell 5.1 的 `Set-Content -Encoding UTF8` **会写 BOM**，Python 的 `._pth` 不剥 BOM，首行变成 `\ufeffpython314.zip`，找不到标准库 | 用 `UTF8Encoding($false)` 无 BOM 写 |
| 2 | `ModuleNotFoundError: No module named 'userdata'` | embeddable 的 `._pth` 是**隔离模式**，不会自动把脚本目录加进 `sys.path` | server.py 顶部显式 `sys.path.insert(0, 脚本目录)` |
| 3 | pip 报 exit 1 | 系统 Python 里 `angr` 与 `pycparser` 版本冲突的 stderr 警告，PowerShell 把 stderr 当失败 | 实际 `Successfully installed` 23 个包，是**误判** |
| 4 | site-packages 376 MB | 装了 opencv(112) + imageio_ffmpeg(100) + numpy(52) 等 | 见下节精简 |

第 1 条特别值得记：**BOM 这种不可见字符造成的故障最难查** ——
错误信息「找不到 encodings」和真实原因（路径首字符被污染）之间隔着十万八千里。
以后凡是给 Python 写 `._pth`、`.pth`、配置文件，一律用无 BOM UTF-8。

第 2 条是 embeddable Python 的**固有行为**，不是 bug：它就是要「不污染 sys.path」，
所以任何依赖「脚本目录自动在 sys.path」的项目搬过来都会栽。

## 四、精简：376 MB -> 163 MB

查了依赖链的实际用法再删，不是凭感觉：

| 包 | 体积 | 谁在用 | 处置 |
|---|---|---|---|
| `cv2`（opencv） | 112 MB | 只有 `moment.py`（**朋友圈**），本工具不碰朋友圈 | **删** |
| `numpy` | 52 MB | `accessibility.py`、`uia_driver.py` —— **正是 UIA 核心** | **保留** |
| `imageio_ffmpeg` | 100 MB | 只有 `media.py` 转码微信图片，且**不可用时有 .wxgf 兜底** | **删** |
| `winsdk` | 53 MB | `wechatauto-replica` 依赖 | 保留 |
| `PIL` / `cryptography` / `pywin32` | ~41 MB | 剪贴板、解密、窗口操作 | 保留 |

关键判断：**numpy 不能删**。看起来它像是 cv2 的附属，实际 `accessibility.py`
和 `uia_driver.py` 都在用 —— 那是整个控权树方案的根基。只看「谁依赖它」会误删。

验证方式：删完跑导入测试 + 用内嵌 python 起后端 + 读 `/api/contacts`，
确认 29 群 / 7 好友 都读得出来。

## 五、验证记录

| 项 | 结果 |
|---|---|
| 内嵌 python 版本 | 3.14.7 amd64，`encodings` 可导入 |
| 8 个依赖导入 | 全部 OK（PIL/psutil/uiautomation/win32clipboard/win32con/win32gui/win32ui/wechatauto） |
| 起后端 | `PORT=50486` 正常输出 |
| `/api/contacts` | 群 29 / 好友 7（读微信库 OK） |
| `/api/doctor` | 全部可用（2 项提醒均属预期） |
| 体积 | 376.2 MB -> 163.0 MB（省 213.2 MB） |

## 六、代价与剩余风险

1. **安装包从 78 MB 涨到约 150 MB**。这是「自包含」的必然代价。
2. **删了 cv2，朋友圈相关功能不可用**。本工具不需要；若将来要做朋友圈，
   把 `cv2` 装回去即可。
3. **删了 imageio_ffmpeg，微信图片消息会落成 .wxgf 原始文件**而不是 jpg。
   文本线索不受影响。
4. **内嵌 Python 版本锁死 3.14.7**。微信库若要更高版本，需重新下载新 embed 包。
