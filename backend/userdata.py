# -*- coding: utf-8 -*-
r"""把第三方库的用户文件重定向到安装目录，避免写入 ~ / %TEMP%。

背景
----
``wechatauto-replica`` 会在用户目录和系统临时目录下创建若干文件：

    ~/.wechatauto/gate_cache.json          UIA gate RVA 缓存
    ~/.wechatauto/layout-<机器>.json        布局校准结果
    ~/.wechatauto/rhythm.json               发送节流状态
    %TEMP%/wechatauto_db/<账号>/            解密出的临时数据库与 keys.json
    %LOCALAPPDATA%/wechatauto_keys/         密钥稳定副本
    <cwd>/wechatauto_logs/                  库自带的文件日志
    ~/pane_diag_raw.png                     表情截图诊断图
    ~/Documents/wechatauto_media            媒体下载默认目录
    ~/Documents/wechatauto_moments          朋友圈导出默认目录
    ~/Documents/wechatauto_recall           防撤回备份默认目录

本项目要求**所有用户文件都在安装目录下**，所以这里把它们全部改道。

关键约束
--------
1. **不要改** ``USERPROFILE`` / ``APPDATA`` / ``LOCALAPPDATA``。
   微信数据目录的**发现**逻辑依赖这些环境变量（外加注册表），改了会找不到数据库。

2. ``os.path.expanduser`` **会被重定向**（见 ``_install_expanduser_redirect``），
   但只针对 ``~`` 与 ``~/...`` / ``~\...`` 前缀；其它入参（含 ``~otheruser``）
   一律透传给真实的 ``expanduser``。
   这样做是安全的：库里 16 处 ``expanduser`` 用法**全部**是输出/缓存/演示路径，
   而 ``db.py`` **一处都没用**（已 grep 确认为 0），所以不影响数据库发现。

3. 需要真实家目录时，用 ``_REAL_EXPANDUSER``（模块导入时就保存下来的那个），
   例如 ``migrate_legacy`` 找旧目录。
"""
from __future__ import annotations

import os
import sys

# 子目录布局（都在安装目录下）
DIR_DATA = "data"
DIR_LOGS = "logs"
DIR_SHOTS = "shots"
DIR_CACHE = "cache"
DIR_TMP = "tmp"
DIR_RUNTIME = "runtime"

_configured_root = None

# 保存「真实的」expanduser，安装重定向后自己还要用它去找旧目录
_REAL_EXPANDUSER = os.path.expanduser


def _install_expanduser_redirect(root: str) -> None:
    """把 ``os.path.expanduser('~')`` 重定向到安装目录。

    这是**兜底**措施。库里除了三个模块级常量（已单独覆盖）之外，还有若干
    行内调用会写用户主目录，例如：

        msgs/mtype.py:261    ~/pane_diag_raw.png          （表情截图诊断图）
        media.py:54          ~/Documents/wechatauto_media
        moment.py:3655/3756/3833  ~/Documents/wechatauto_moments
        recall.py:44         ~/Documents/wechatauto_recall

    逐个打补丁容易漏（而且行内调用改不了模块常量），所以统一重定向。

    为什么这样做是安全的（已全量核对 wechatauto 内全部 16 处 expanduser 用法）：
        * 它们全部是**输出/缓存/演示**路径；
        * 微信数据目录的**发现**逻辑压根不用 expanduser ——
          db.py 走的是 APPDATA / LOCALAPPDATA / 注册表 / USERPROFILE 环境变量
          （``grep expanduser db.py`` 结果为空），因此不受影响。

    只处理 ``~`` / ``~/...`` / ``~\\...``；``~otheruser`` 之类一律透传真实实现。

    已知的唯一副作用：``media.py:1586`` 的 ``_save_once()`` 会用
    ``expanduser("~")`` 拼出 ``Downloads`` / ``Pictures`` /
    ``Documents\\WeChat Files`` 作为**只读扫描**目录去找刚保存的图片；
    重定向后这三个目录在安装目录下不存在 → 该兜底分支不生效。
    主路径（账号目录 ``acc``）仍在扫描列表里，功能不受影响；
    本项目的后端也不走这条路径。
    """
    def _redirected(path):
        if not path:
            return _REAL_EXPANDUSER(path)
        if path == "~":
            return root
        if path.startswith("~/") or path.startswith("~\\"):
            return os.path.join(root, path[2:])
        return _REAL_EXPANDUSER(path)

    os.path.expanduser = _redirected


def project_root() -> str:
    """未指定数据根目录时的默认值：项目/安装目录。"""
    # <root>/backend/userdata.py -> <root>
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_root() -> str:
    """当前生效的数据根目录（安装目录）。"""
    return _configured_root or os.environ.get("WCS_DATA_ROOT") or project_root()


def sub(name: str) -> str:
    p = os.path.join(data_root(), name)
    os.makedirs(p, exist_ok=True)
    return p


def ensure_writable(root: str) -> None:
    """确认安装目录可写；不可写时给出可操作的提示而不是默默失败。"""
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".write-test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as exc:
        raise RuntimeError(
            "安装目录不可写：%s\n"
            "本程序要求所有用户文件都放在安装目录下，因此需要把程序安装到可写位置，\n"
            "例如 D:\\WeChatSender（不要装在 C:\\Program Files 下，除非以管理员身份运行）。\n"
            "原始错误：%s" % (root, exc)
        )


def configure(root: str | None = None) -> str:
    """执行重定向。必须在任何 WeChatDB / WeChat 调用之前执行。

    返回生效的数据根目录。
    """
    global _configured_root

    root = os.path.abspath(root or os.environ.get("WCS_DATA_ROOT") or project_root())
    _configured_root = root
    ensure_writable(root)

    tmp_dir = sub(DIR_TMP)
    cache_dir = sub(DIR_CACHE)
    sub(DIR_DATA)
    sub(DIR_LOGS)
    sub(DIR_SHOTS)
    sub(DIR_RUNTIME)

    # 1) 临时目录：db.py 用 tempfile.gettempdir() 解密数据库、写 keys.json。
    #    只影响本进程，不影响微信数据目录的发现（那用的是 USERPROFILE/APPDATA）。
    os.environ["TEMP"] = tmp_dir
    os.environ["TMP"] = tmp_dir
    os.environ["TMPDIR"] = tmp_dir
    try:
        import tempfile
        tempfile.tempdir = tmp_dir      # 清掉 import 时可能已缓存的值
    except Exception:
        pass

    # 2) 密钥稳定副本目录（库支持该环境变量）
    keys_dir = os.path.join(cache_dir, "keys")
    os.makedirs(keys_dir, exist_ok=True)
    os.environ["WECHATAUTO_KEYS_DIR"] = keys_dir

    # 2.5) 把历史上落在用户目录里的缓存搬进来（移动而非删除，可逆）
    #      注意：必须在安装 expanduser 重定向**之前**做，否则会去找新位置。
    migrate_legacy(cache_dir, keys_dir)

    # 2.6) 兜底：重定向 os.path.expanduser，堵住库里剩余的行内写点。
    #      必须放在 import wechatauto 之前 —— 这样那些模块级常量
    #      （如 recall.DEFAULT_RECALL_DIR）在 import 时就算到安装目录下。
    _install_expanduser_redirect(root)

    # 3) 覆盖库里的模块级路径常量（它们在 import 时就用 expanduser 算好了）
    _patch_library(cache_dir)

    # 4) 把工作目录也钉在安装目录，兜住任何相对路径写法
    try:
        os.chdir(root)
    except Exception:
        pass

    return root


def migrate_legacy(cache_dir: str, keys_dir: str) -> list:
    """把过去写在用户目录里的缓存搬到安装目录下（移动，不删除）。

    只搬已知的缓存文件，不碰其它任何东西；目标已存在时保留现有文件、不覆盖。
    返回搬动的条目描述列表，供日志展示。
    """
    moved = []

    def _move(src: str, dst_dir: str) -> None:
        try:
            if not os.path.isfile(src):
                return
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(src))
            if os.path.exists(dst):
                # 目标已有，说明已经搬过了；删掉遗留副本即可（内容等价）
                try:
                    os.remove(src)
                    moved.append("删除重复遗留: %s" % src)
                except OSError:
                    pass
                return
            import shutil
            shutil.move(src, dst)
            moved.append("迁移: %s -> %s" % (src, dst))
        except Exception as exc:
            sys.stderr.write("[userdata] 迁移失败 %s: %s\n" % (src, exc))

    # 1) ~/.wechatauto/*.json
    try:
        # 用真实的 expanduser，避免被上面的重定向带偏
        old_dir = os.path.join(_REAL_EXPANDUSER("~"), ".wechatauto")
        if os.path.isdir(old_dir):
            for name in os.listdir(old_dir):
                if name.endswith(".json") or name.endswith(".json.bak"):
                    _move(os.path.join(old_dir, name), cache_dir)
            # 空的遗留目录顺手清掉
            try:
                if os.path.isdir(old_dir) and not os.listdir(old_dir):
                    os.rmdir(old_dir)
                    moved.append("删除空目录: %s" % old_dir)
            except OSError:
                pass
    except Exception as exc:
        sys.stderr.write("[userdata] 检查旧缓存目录失败：%s\n" % exc)

    # 2) %LOCALAPPDATA%/wechatauto_keys/*.json
    try:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")
        if base:
            old_keys = os.path.join(base, "wechatauto_keys")
            if os.path.isdir(old_keys):
                for name in os.listdir(old_keys):
                    if name.endswith(".json") or name.endswith(".json.bak"):
                        _move(os.path.join(old_keys, name), keys_dir)
                try:
                    if not os.listdir(old_keys):
                        os.rmdir(old_keys)
                        moved.append("删除空目录: %s" % old_keys)
                except OSError:
                    pass
    except Exception as exc:
        sys.stderr.write("[userdata] 检查旧密钥目录失败：%s\n" % exc)

    return moved


def _patch_library(cache_dir: str) -> None:
    """导入库并改写它的缓存路径常量。"""
    try:
        import wechatauto  # noqa: F401
    except Exception as exc:  # 库没装好时不要在这里炸，交给上层报错
        sys.stderr.write("[userdata] 无法导入 wechatauto：%s\n" % exc)
        return

    # accessibility.GATE_CACHE_FILE = ~/.wechatauto/gate_cache.json
    try:
        from wechatauto import accessibility as _acc
        gate = os.path.join(cache_dir, "gate_cache.json")
        _acc.GATE_CACHE_FILE = gate
        try:
            _acc._GATE_CACHE = None      # 让落盘缓存重新从新路径读
        except Exception:
            pass
    except Exception as exc:
        sys.stderr.write("[userdata] 改写 gate 缓存路径失败：%s\n" % exc)

    # guia.LAYOUT_CONFIG_DIR = ~/.wechatauto
    try:
        from wechatauto import guia as _guia
        _guia.LAYOUT_CONFIG_DIR = cache_dir
    except Exception as exc:
        sys.stderr.write("[userdata] 改写布局配置路径失败：%s\n" % exc)

    # rhythm.STATE_DIR / STATE_FILE = ~/.wechatauto/rhythm.json（发送节流状态）
    try:
        from wechatauto import rhythm as _rhythm
        _rhythm.STATE_DIR = cache_dir
        _rhythm.STATE_FILE = os.path.join(cache_dir, "rhythm.json")
    except Exception as exc:
        sys.stderr.write("[userdata] 改写节流状态路径失败：%s\n" % exc)

    # 库自带的文件日志默认写在 <cwd>/wechatauto_logs，本项目自己记日志，关掉它
    try:
        from wechatauto.param import WxParam
        WxParam.ENABLE_FILE_LOGGER = False
    except Exception:
        pass

    # 媒体 / 朋友圈 / 撤回备份的默认落盘目录，也指到安装目录下
    base_root = data_root()
    try:
        from wechatauto import media as _media
        media_dir = os.path.join(base_root, DIR_DATA, "media")
        os.makedirs(media_dir, exist_ok=True)
        _media.DEFAULT_SAVE_PATH = media_dir
    except Exception as exc:
        sys.stderr.write("[userdata] 改写媒体目录失败：%s\n" % exc)

    try:
        from wechatauto import recall as _recall
        recall_dir = os.path.join(base_root, DIR_DATA, "recall")
        os.makedirs(recall_dir, exist_ok=True)
        _recall.DEFAULT_RECALL_DIR = recall_dir
    except Exception as exc:
        sys.stderr.write("[userdata] 改写撤回备份目录失败：%s\n" % exc)


def describe() -> dict:
    """返回各目录，供日志/前端展示。"""
    root = data_root()
    return {
        "root": root,
        "data": os.path.join(root, DIR_DATA),
        "logs": os.path.join(root, DIR_LOGS),
        "shots": os.path.join(root, DIR_SHOTS),
        "cache": os.path.join(root, DIR_CACHE),
        "tmp": os.path.join(root, DIR_TMP),
        "runtime": os.path.join(root, DIR_RUNTIME),
    }