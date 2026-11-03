# Agents 文档索引

> AI 在执行任务前先查本索引，快速定位所需文档。
> 维护要求：新增/修改文档后同步更新本表。

| 文档名称 | 用途说明 | 存储路径 | 最近修改时间 |
|---|---|---|---|
| README.md | 项目总说明：快速开始、目录结构、用户文件策略、功能、技术原理、环境坑、打包、验证记录、FAQ | `README.md` | 2026-11-03 |
| 设计文档-运营方案自动化.md | v1.2.0 内容编排/定时调度/线索接收/周报/合规校验的模块划分、设计取舍、踩过的坑、隐私与局限 | `docs/` | 2026-11-03 |
| 排查记录-微信4.2.0.11控件树失效.md | 微信自动升级到 4.2.0.11 后控件树无法实体化的根因、证据与处置 | `docs/` | 2026-11-02 |
| 制作说明.md | 宣传片规格、9 屏结构、设计取舍、工程复现方式 | `宣传视频/` | 2026-11-02 |
| 发布文案.md | 宣传片配套标题、短视频版/图文版正文、话题标签 | `宣传视频/` | 2026-11-02 |
| 定稿配音与时间轴.json | 各屏时长、标题、副标题、配音稿 | `宣传视频/` | 2026-11-02 |

## 代码结构速查

| 路径 | 职责 |
|---|---|
| `backend/campaign.py` | 内容编排：按星期引导语 + 原文案拼接 + 资料钩子 |
| `backend/scheduler.py` | 定时：绝对时间事件表、钩子延迟、每群每周上限 |
| `backend/inbox.py` | 接收：轮询数据库找出「谁主动私聊了我」，意图分类 |
| `backend/leads.py` | 线索表：登记去重、跟进状态、CSV 导出 |
| `backend/report.py` | 周报：投放日志、6 项 KPI 达成、砍群建议 |
| `backend/guard.py` | 合规：违规开票暗示拦截（带语境判断） |
| `backend/media.py` | 素材：三种来源收集 + 按目标分配 |
| `backend/clipboard.py` | 剪贴板：图片 CF_DIB / 文件 CF_HDROP，供微信粘贴 |
| `electron/main.js` | 主进程：安装目录重定向、拉起/回收 Python 边车、建窗口 |
| `electron/preload.js` | 通过 contextBridge 把服务端口与 token 交给界面；暴露文件选择 |
| `backend/server.py` | 本地 HTTP 服务（token 鉴权 + CORS） |
| `backend/contacts.py` | 账号 / 群聊 / 好友 枚举 |
| `backend/sender.py` | 发送引擎（UIA 语义控件 + 截图 + 回读校验） |
| `backend/planner.py` | 发送计划生成（日期种子随机） |
| `backend/userdata.py` | 把第三方库的写入重定向到安装目录 |
| `frontend/index.html` | 控制台界面（单文件） |
| `launch.js` / `build.js` | Electron 启动器 / electron-builder 启动器（绕开 .bin shim） |

## 约定

- 临时文件放 `temp/`，不散落在项目其他位置
- `temp/`、`AI对话/` 已在 `.gitignore` 中，禁止提交
- 修改代码必须同步更新对应文档（至少更新本索引的「最近修改时间」）