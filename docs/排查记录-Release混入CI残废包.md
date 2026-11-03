# 排查记录：Release 里多出一个版本号错误的安装包

- **发现时间**：2026-11-04
- **影响版本**：v1.5.0 / v1.6.0 / v1.7.0（三个 Release 全部中招）
- **严重程度**：中高 —— 多出的包**不能独立运行**，但看起来像正常安装包

## 一、现象

发布 v1.7.0 后核对资产，发现 Release 里除了我上传的两个文件，还有：

```
WeChatSender-1.0.0-x64.exe           81,825,969 字节
WeChatSender-1.0.0-x64.exe.blockmap      85,889 字节
```

而且：

- 本地 `dist/` 里**从来没有** 1.0.0 的 exe
- v1.5.0、v1.6.0 里也有同名文件，但**大小各不相同**
  （81822379 / 81823602 / 81825969）—— 说明是**每次构建重新产出的**
- 手写的发布说明被换成了通用文案，且文案里还写着
  「需要已安装 Python 3.9+」（v1.5.0 起早就自带 Python 了）

## 二、定位过程

1. 查 `dist/`：没有 1.0.0 的 exe → 不是本地残留
2. 查 `package.json`：`version` 恒为 `1.0.0`，但 `artifactName` 是
   `${productName}-${version}-${arch}.${ext}` → 谁会产出 1.0.0 的包？
3. 查 API 原始 JSON 的 `uploader` 字段 —— **决定性证据**：

   | 资产 | uploader |
   |---|---|
   | `WeChatSender-1.7.0-x64.exe` | `Alanine-gH`（我） |
   | `WeChatSender-1.0.0-x64.exe` | **`github-actions[bot]`** |

4. 查 `gh run list`：每次我 `gh release create` 之后，都有一条
   `build-release` workflow 运行成功，耗时约 1m50s，event=`push`，ref=`v1.7.0`

## 三、根因

仓库里有个 `.github/workflows/build-release.yml`，触发条件是：

```yaml
on:
  push:
    tags:
      - 'v*'
```

而 `gh release create v1.7.0 ...` 会**创建并推送 `v1.7.0` tag**，
于是 CI 被触发。CI 里跑的是 `npm run dist`，此时 `package.json` 的
`version` 是 **1.0.0**（我的版本号只在本地 `build.js` 构建期间临时改写），
所以产出 `WeChatSender-1.0.0-x64.exe`，再由 `softprops/action-gh-release@v2`
的 `files: dist/*.exe` **自动追加到同一个 Release**。

三个连锁后果：

1. **Release 里多了一个版本号错误的包**
2. **更严重：那个包是残废的** —— `vendor/python` 在 `.gitignore` 里，
   CI 里没有内置 Python，它产出的安装包需要目标机自己装 Python。
   别人下载这个 81MB 的包，装完发现启动失败
3. **手写的发布说明被覆盖** —— `action-gh-release` 的 `body:` 是通用文案，
   还带着「需要 Python 3.9+」这种已经过时的说法

## 四、修法

### 1. CI 改为只手动触发

```yaml
on:
  workflow_dispatch:        # 去掉 push.tags
    inputs:
      version:
        required: true      # 强制填版本号，不再从 package.json 猜
```

发布步骤也从 `if: github.event_name == 'push' || inputs.publish_release`
改成 `if: inputs.publish_release`，避免手动跑一次就意外发布。

并在 workflow 顶部用注释写清**为什么不能监听 tag**，防止以后有人改回去。

### 2. 清理已有错误资产

按 `uploader == github-actions[bot]` 判定，删除三个 Release 里的残废包
（这是最可靠的判据 —— 比按文件名猜安全）。

### 3. 恢复被覆盖的发布说明

用仓库里保存的 notes 重写 body。

## 五、经验

1. **CI 与本地打包同时存在时必须明确谁负责发布**。
   两边都发 → 同一 tag 上出现两个来源的产物，且版本号、内容都可能不同。

2. **判断产物来源看 `uploader` 字段**，比看文件名或时间戳可靠。

3. **`.gitignore` 排除的东西，CI 里也没有**。
   本项目 `vendor/python` 被排除（163MB 不该进仓库），
   于是 CI 天然构建不出自包含包 —— 这点必须在 workflow 注释里写死。

4. **`gh release create` 会建 tag**。用 `--target` 或先手动建 tag 都无法避免；
   如果仓库有 tag 触发的 CI，这就是个必然的连锁反应。

## 六、当前状态

| Release | 资产 | 说明 |
|---|---|---|
| v1.5.0 | 1.5.0 exe + blockmap | 已清理、说明已恢复 |
| v1.6.0 | 1.6.0 exe + blockmap | 已清理、说明已恢复 |
| v1.7.0 | 1.7.0 exe + blockmap + UserManual(docx/pdf) | 已清理、说明已恢复 |

发布流程固定为：

```
准备内嵌Python.bat  →  打包.bat <版本>  →  gh release create ...
```

CI 只在需要「验证云端能否构建」时手动跑，且明确知道它产出的包**不含内置 Python**。
