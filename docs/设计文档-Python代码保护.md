# 设计文档 · Python 代码保护（自有模块去明文）

> 版本：v2.0.3（与升级安装修复合并发布）
> 日期：2026-11-11
> 涉及文件：`tools/protect_backend.py`（新增）、`build.js`、`package.json`、
> `electron/main.js`

## 先说清楚：这不是「加密保护」

用户要求「加密混淆，不能让 Python 露在外面」。必须诚实说明边界：

| 事实 | 说明 |
|---|---|
| Python 是解释型语言 | 运行时**必须**有字节码，不可能真正「不露在外面」 |
| 密钥必然随包分发 | 攻击者能反编译 `_boot.pyc` 取出密钥，或从内存中 dump 出 code object |
| `.pyc` 可反编译 | `uncompyle6` / `decompyle3` 能还原大部分逻辑；`core.bin` 只是不落盘明文 |
| 第三方库仍是源码 | 用户明确选择保留第三方 `.py`（它们本来就是开源库，无保密价值） |

**本方案的实际效果**：安装目录里不再有任何自有 `.py` 明文，随手翻看/直接改业务代码
的门槛被显著提高；它**不是**防逆向的加密保护，不要对使用者这样宣传。
依据：[SO 2055355](https://stackoverflow.com/questions/2055355/how-to-protect-python-source-code)、
[shlomif: pyc 只是遮羞布](https://shlomif-tech.livejournal.com/39732.html)、
[dev.to 2026 逆向保护综述](https://dev.to/justmaniak/python-reverse-engineering-protection-what-actually-works-in-2026-3p59)。

## 现状（动手前核实）

| 项 | 值 |
|---|---|
| `resources/backend/` | 23 个自有 `.py`（344 KB）明文 |
| `resources/python/Lib/site-packages/` | 1643 个第三方 `.py`（19 MB）明文 + 622 `.pyi` + numpy 的 `.f90/.f/.h/.csv` 等 |
| 内嵌 Python | 3.14.7 embeddable；`python314._pth` = `python314.zip / . / Lib\site-packages / import site` |
| 现有 pyc | 1643 个**全在 `__pycache__`**，sourceless 模式需要 legacy 布局 |
| 启动方式 | `python.exe <resources>/backend/server.py` |
| `__file__` 依赖 | `server.py:41` 用它算 `sys.path`，`server.py:77` 算 `FRONTEND_DIR`，`userdata.py:97` 算 `project_root()`；第三方库另有 20 处 |
| 可用加密库 | `cryptography` / `Crypto` 都在，但解密发生在任何第三方 import 之前，不能用 |

## 方案（用户拍板的三项决策）

| 决策点 | 选择 |
|---|---|
| 目标层级 | 真·运行时解密（密钥随包，明确标注只是提高门槛） |
| 处理范围 | 自有 23 个去源码；第三方删掉无用 `.pyi/.f90/.f/.h/.c/.cpp/.pyx/.pxd`，**保留第三方 `.py`** |
| 版本 | 与上一轮「升级安装卡死 + 用户数据外置」合并发 v2.0.3 |

### 为什么不用现成工具

| 方案 | 放弃原因 |
|---|---|
| PyArmor | 商业依赖，违反「不擅自引入新依赖」 |
| Nuitka / PyInstaller 打单 exe | 引入新构建工具；`wechatauto` 依赖 `comtypes` 动态生成模块、pywin32 COM、`uiautomation`，冻结兼容性风险极大（[Nuitka](https://nuitka.net/) 关联度高但回归面大） |
| 单纯 `compileall` + 删 `.py` | 标准 `.pyc` 可直接反编译，不满足「真·加密混淆」 |
| 加密 zip + 运行时落盘 | 运行时会在磁盘留下明文，且用户文件策略要求所有临时文件在数据根 |

最终：**标准库构建的自有模块内存加载**。零新依赖，不动第三方运行逻辑。

### 实现链路

```
构建期（build.js → tools/protect_backend.py，用 vendor/python/python.exe 跑）
  backend/*.py ─compile→ code object ─marshal→ zlib ─HMAC-SHA256 流加密→ core.bin
  引导脚本 ─py_compile(optimize=2)→ _boot.pyc（sourceless，.py 不进包）
  产物落 temp/protected-backend/（.gitignore 覆盖），构建结束无论成败都清理

运行期
  python.exe <resources>/backend/_boot.pyc 0 --token ... --quiet --version ...
    → 校验 HMAC tag（防篡改）
    → 用 nonce + 计数器生成 keystream，XOR 解密 → zlib → marshal
    → 注册 MetaPathFinder，从内存 code object 提供 22 个模块
    → 以 __main__ 执行 server 的 code object
```

关键细节：

- **字节码版本绑定**：`marshal` / `py_compile` 产物与 Python 3.14.7 绑定，
  保护脚本必须用 `vendor/python/python.exe` 运行。`build.js` 会检查它存在，
  缺失就明确报错，不用系统 Python 兜底。
- **`__file__` 语义保留**：内存加载的模块被赋予
  `<resources>/backend/<name>.py` 的伪 `__file__`，`server.py` 算
  `sys.path` 与 `FRONTEND_DIR`、`userdata.py` 算 `project_root()` 都照旧。
- **完整性**：`core.bin` 带 HMAC-SHA256 tag，篡改会直接报
  `core.bin integrity check failed`，不会执行到一半崩。
- **每次构建随机密钥**：`os.urandom(32)`，不复用。
- **开发模式不受影响**：`app.isPackaged` 为 false 时仍直接跑 `backend/server.py`，
  改代码不需要重新生成保护产物。
- **第三方清理**：`package.json` 的 `extraResources` 加 `filter` 排除
  `.pyi/.f90/.f/.h/.c/.cpp/.pyx/.pxd` 与 `__pycache__`；**不动源目录**，
  不删任何 `.pyd` / `.py`（83 个 `.pyd` 是原生扩展，必须保留）。

## 验证记录

| 项 | 结果 |
|---|---|
| 保护脚本单测 | 7/7（23 模块、产物只有 `core.bin`+`_boot.pyc`、无 `.py`、密文非明文、可解密、真实 import 6 个深层依赖模块、`settings` 37 项完整） |
| **端到端启动** | 5/5：`_boot.pyc` 真实启动服务、上报 `PORT=xxxx`、token 鉴权通过、`/api/app` 返回 `version=2.0.3` / `data_root` 正确、进程可终止 |
| 构建配置 | `build.js` / `main.js` / `preload.js` `node --check` 通过；`package.json` 语法与 `extraResources` 映射正确；保护产物被 `.gitignore:71 temp/` 覆盖 |
| 打包 | 见下节「打包验证」 |

## 已知限制

1. **不是防逆向保护**（见开头）。密钥可从 `_boot.pyc` 提取，code object 可从内存 dump。
2. 第三方 1643 个 `.py` 仍明文 —— 用户明确选择保留；若以后要求「整个包看不到 Python
   源码」，需要对第三方做 sourceless 转换，但 20 处 `__file__` 依赖与 83 个 `.pyd`
   会带来回归风险，须单独评估。
3. `core.bin` 与 Python 3.14.7 绑定：升级内嵌 Python 版本必须重新打包（正常流程本就会重新构建）。
4. 异常时的 traceback 里 `co_filename` 是伪装的 `backend/xxx.py`（相对名），
   不泄露开发机绝对路径，但也看不到真实行号对应的源码文件。
5. 未做真机 NSIS 安装→启动→升级端到端（会改动使用者机器），已用隔离的
   临时目录 + stub electron + 真实 Python 子进程覆盖启动链路。
