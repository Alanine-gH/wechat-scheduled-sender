# -*- coding: utf-8 -*-
"""微信数据目录定位（比第三方库的自动探测更宽容）。

为什么需要这一层：wechatauto 的 ``auto_detect_db_dir()`` 只按
「配置文件 → 注册表 → 用户目录」三处找，且只认 ``xwechat_files`` /
``WeChat Files`` 这两个名字。实测有不少设备踩不到：

  * 微信「设置 → 文件管理」把存储位置改到了别的盘（如 E:\\微信文件）；
  * 装的是绿色版/多开版，注册表里没有记录；
  * 目录名是中文「微信文件」，或者旧版 ``WeChat Files``；
  * 用户目录被重定向（企业环境把「文档」放到网络盘）。

这些机器上程序会表现为「读不到登录的微信」——其实数据就在，只是没找到。

本模块的策略（按可靠性从高到低）：
  1. 用户在设置里手动填的目录（``wechat_db_dir``）—— 一定最准；
  2. 第三方库的自动探测（配置文件 / 注册表 / 用户目录）；
  3. **自己再做一遍全盘常见目录扫描**（各盘符根目录、文档、AppData、Tencent）。

每层都要求「该目录下确实存在含 db_storage 的账号目录」才算命中，
避免把空壳目录当成微信数据目录。
"""
from __future__ import annotations

import os
import string
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 常见的「账号目录的父目录」名字。前两个是微信 4.x / 3.x 的默认，
# 后面几个是实测遇到过的改法（含中文与大小写差异）。
_ROOT_NAMES = (
    "xwechat_files",
    "WeChat Files",
    "xwechat_files_data",
    "微信文件",
    "wechat_files",
)

# 相对常见锚点再往下一层的目录名（腾讯/文档 下）
_NESTED = (
    "xwechat_files",
    "WeChat Files",
    "Tencent",
    os.path.join("Tencent", "xwechat_files"),
    os.path.join("Tencent", "WeChat Files"),
)

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "dir": None, "source": "", "tried": []}
_CACHE_TTL = 30.0          # 秒；扫盘有成本，短时间内复用


def has_account_dirs(db_dir: Optional[str]) -> bool:
    """该目录下是否存在「含 db_storage 的账号目录」。"""
    if not db_dir or not os.path.isdir(db_dir):
        return False
    try:
        names = os.listdir(db_dir)
    except OSError:
        return False
    for name in names:
        if name.startswith("."):
            continue
        if os.path.isdir(os.path.join(db_dir, name, "db_storage")):
            return True
    return False


def account_dirs(db_dir: Optional[str]) -> List[str]:
    if not db_dir or not os.path.isdir(db_dir):
        return []
    out = []
    try:
        for name in os.listdir(db_dir):
            p = os.path.join(db_dir, name)
            if os.path.isdir(os.path.join(p, "db_storage")):
                out.append(p)
    except OSError:
        return []
    return sorted(out)


def _drives() -> List[str]:
    """列出存在的盘符（A/B 跳过）。"""
    out = []
    for ch in string.ascii_uppercase:
        if ch in ("A", "B"):
            continue
        root = "%s:\\" % ch
        if os.path.isdir(root):
            out.append(root)
    return out


def _scan_candidates() -> List[str]:
    """构造「可能存放微信数据」的候选目录（去重、保持优先级）。"""
    seen = set()
    cands: List[str] = []

    def add(p: Optional[str]) -> None:
        if not p:
            return
        p = os.path.normpath(p)
        key = p.lower()
        if key not in seen:
            seen.add(key)
            cands.append(p)

    # 1) 用户目录与常见 AppData
    for env in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        add(os.path.join(base, "Documents"))
        add(base)
        for sub in _NESTED:
            add(os.path.join(base, sub))
        add(os.path.join(base, "Documents", "xwechat_files"))
        add(os.path.join(base, "Documents", "WeChat Files"))

    # 2) 每个盘符根目录及其一层常见子目录
    for drive in _drives():
        for name in _ROOT_NAMES:
            add(os.path.join(drive, name))
        for sub in _NESTED:
            add(os.path.join(drive, sub))
        add(os.path.join(drive, "Documents"))
        for name in _ROOT_NAMES:
            add(os.path.join(drive, "Documents", name))

    return cands


def _probe(path: Optional[str]) -> Optional[str]:
    """把用户给的路径规整成「账号目录的父目录」。

    用户可能填的是：
      * ``E:\\微信文件``                     （已经是父目录）
      * ``E:\\微信文件\\wxid_xxx_abcd``        （填到了具体账号目录）
      * ``E:\\微信文件\\wxid_xxx\\db_storage`` （填到了 db_storage）
    三种都要能用 —— 让用户自己判断该填到哪一级是不合理的。
    """
    if not path:
        return None
    p = os.path.normpath(path.strip().strip('"'))
    if not os.path.isdir(p):
        return None

    # 填到了 db_storage → 退回账号目录 → 再退一级
    base = os.path.basename(p).lower()
    if base == "db_storage":
        p = os.path.dirname(p)
    # 填到了账号目录（自身有 db_storage）
    if os.path.isdir(os.path.join(p, "db_storage")):
        p = os.path.dirname(p)

    if has_account_dirs(p):
        return p
    # 再往下探一层（用户可能填了 xwechat_files 的上一层）
    for name in _ROOT_NAMES + ("Tencent",):
        sub = os.path.join(p, name)
        if has_account_dirs(sub):
            return sub
    return None


def resolve(manual: str = "", use_cache: bool = True) -> Tuple[Optional[str], str]:
    """返回 ``(db_dir, source)``。source ∈ setting / auto / scan / none。"""
    global _cache
    now = time.time()

    if use_cache:
        with _cache_lock:
            if now - _cache["at"] < _CACHE_TTL and _cache["source"] != "setting":
                return _cache["dir"], _cache["source"]

    # 1) 手动指定
    hit = _probe(manual)
    if hit:
        with _cache_lock:
            _cache.update({"at": now, "dir": hit, "source": "setting"})
        return hit, "setting"

    # 2) 第三方库的自动探测
    try:
        from wechatauto import auto_detect_db_dir  # noqa: WPS433
        auto = auto_detect_db_dir()
        if has_account_dirs(auto):
            with _cache_lock:
                _cache.update({"at": now, "dir": auto, "source": "auto"})
            return auto, "auto"
    except Exception:
        pass

    # 3) 自己扫
    tried: List[str] = []
    for cand in _scan_candidates():
        tried.append(cand)
        hit = _probe(cand)
        if hit:
            with _cache_lock:
                _cache.update({"at": now, "dir": hit, "source": "scan",
                               "tried": tried[-40:]})
            return hit, "scan"

    with _cache_lock:
        _cache.update({"at": now, "dir": None, "source": "none",
                       "tried": tried[-40:]})
    return None, "none"


def clear_cache() -> None:
    with _cache_lock:
        _cache.update({"at": 0.0, "dir": None, "source": "", "tried": []})


def diagnose(manual: str = "") -> Dict[str, Any]:
    """给自检用：说明找没找到、在哪、有几个账号。"""
    db_dir, source = resolve(manual, use_cache=False)
    label = {"setting": "手动指定", "auto": "自动探测",
             "scan": "全盘扫描", "none": "未找到"}.get(source, source)
    out: Dict[str, Any] = {
        "db_dir": db_dir or "",
        "source": source,
        "source_cn": label,
        "accounts": [],
        "manual": manual or "",
    }
    if not db_dir:
        with _cache_lock:
            out["tried"] = list(_cache.get("tried") or [])[:12]
        return out

    for d in account_dirs(db_dir):
        recent = 0.0
        try:
            for root, _, files in os.walk(os.path.join(d, "db_storage")):
                for f in files:
                    if f.endswith(".db"):
                        recent = max(recent, os.path.getmtime(os.path.join(root, f)))
        except OSError:
            pass
        out["accounts"].append({
            "account": os.path.basename(d),
            "last_activity": recent,
        })
    out["accounts"].sort(key=lambda x: -x["last_activity"])
    return out
