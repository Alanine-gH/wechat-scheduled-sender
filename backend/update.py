# -*- coding: utf-8 -*-
"""检查更新 / 下载新版本安装包。

为什么不引入 electron-updater：
  1) 项目没有 `publish` 配置，electron-updater 依赖 `app-update.yml`，要动打包配置；
  2) 安装包**没有代码签名**，Windows 上静默自动更新的失败面很大
     （SmartScreen 拦截、静默参数被 NSIS 助手式安装器忽略等），
     一旦坏了，用户拿到的是一个「点了更新没反应」的程序，比没有这功能更糟。
  3) 用户规则里也明确：不擅自引入新依赖。

所以这里做的是**可靠的「检查 + 下载 + 交给用户点安装」**：
  - 查 GitHub Releases（公开仓库，无需 token）
  - 版本号自己比较，不依赖接口返回顺序
  - 下载到安装目录的 temp/，校验大小，再让用户手动运行安装程序

设计约束：
  - **不接受前端传 URL**。下载地址由服务端重新从 Release 里取，
    避免这个接口被当成 SSRF 跳板。
  - 查询结果缓存 5 分钟，避开 GitHub 未认证 60 次/小时 的限流。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

REPO = "Alanine-gH/wechat-scheduled-sender"
API_URL = "https://api.github.com/repos/%s/releases?per_page=20" % REPO
RELEASES_PAGE = "https://github.com/%s/releases/latest" % REPO

_CACHE_TTL = 300          # 秒；5 分钟内复用上一次的查询结果
_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()

_UA = "WeChatSender-Updater/1.0 (+https://%s)" % "github.com/" + REPO


# ---------------------------------------------------------------- 版本比较

def parse_version(s: str) -> tuple:
    """把 'v1.8.0' / '1.8.0' / '1.8.0-beta.1' 解析成可比较的元组。

    只取前三段数字；预发布标记一律视为**小于**同版本正式版
    （1.8.0-beta < 1.8.0），这是发布语义的常规做法。
    """
    if not s:
        return (0, 0, 0, 0)
    s = str(s).strip().lstrip("vV")
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", s)
    if not m:
        return (0, 0, 0, 0)
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    rest = (m.group(4) or "").strip()
    # 有预发布尾巴 -> 0（更小）；正式版 -> 1
    stable = 0 if rest else 1
    return (major, minor, patch, stable)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------- 查询

def _fetch_releases(timeout: float = 12.0) -> List[Dict[str, Any]]:
    req = urllib.request.Request(API_URL, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, list):
        raise ValueError("GitHub 返回的不是发布列表")
    return data


def _pick_best_asset(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """优先挑 Windows x64 的安装包 .exe。

    排除 .blockmap 与 .yml（那些是更新器元数据，不是给人装的包）。
    """
    exes = [a for a in assets
            if str(a.get("name", "")).lower().endswith(".exe")
            and "uninstall" not in str(a.get("name", "")).lower()]
    if not exes:
        return None
    # 名字里带 x64 的优先
    exes.sort(key=lambda a: (0 if "x64" in a.get("name", "").lower() else 1,
                             -int(a.get("size") or 0)))
    return exes[0]


def _summarise(rel: Dict[str, Any], current: str) -> Dict[str, Any]:
    tag = rel.get("tag_name") or rel.get("name") or ""
    latest = str(tag).lstrip("vV")
    asset = _pick_best_asset(rel.get("assets") or [])
    # 去掉 Markdown 记号再给界面 —— 前端是纯文本渲染，留着 ## 和 ** 很难看
    body = latest_notes_for_display(rel.get("body") or "")
    if len(body) > 4000:
        body = body[:4000] + "\n…（说明过长，已截断）"
    return {
        "current": current,
        "latest": latest,
        "tag": tag,
        "has_update": is_newer(latest, current),
        "name": rel.get("name") or tag,
        "published_at": rel.get("published_at") or "",
        "url": rel.get("html_url") or RELEASES_PAGE,
        "notes": body,
        "asset_name": (asset or {}).get("name") or "",
        "asset_size": int((asset or {}).get("size") or 0),
        "has_asset": bool(asset),
        "prerelease": bool(rel.get("prerelease")),
    }


def check(current: str, use_cache: bool = True) -> Dict[str, Any]:
    """查询最新正式版并与 current 比较。

    永不抛异常：网络问题一律转成 ok=False + 中文说明，
    因为这是「点一下看看」的功能，不该让界面炸掉。
    """
    global _cache
    now = time.time()

    if use_cache:
        with _cache_lock:
            if _cache["data"] and now - _cache["at"] < _CACHE_TTL:
                out = dict(_cache["data"])
                out["current"] = current
                out["has_update"] = is_newer(out.get("latest") or "", current)
                out["cached"] = True
                return out

    try:
        rels = _fetch_releases()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            msg = "已被 GitHub 限流（未登录每小时 60 次），请过一会儿再试"
        elif e.code == 404:
            msg = "仓库或发布页不存在（404）"
        else:
            msg = "GitHub 返回 HTTP %s" % e.code
        return {"ok": False, "error": msg, "url": RELEASES_PAGE}
    except urllib.error.URLError as e:
        return {"ok": False,
                "error": "连不上 GitHub：%s" % (getattr(e, "reason", e),),
                "url": RELEASES_PAGE}
    except Exception as e:                      # 超时 / JSON 异常等
        return {"ok": False, "error": "检查更新失败：%s" % e, "url": RELEASES_PAGE}

    # 只考虑正式版；再按版本号取最大（不轻信接口顺序）
    stable = [r for r in rels
              if not r.get("draft") and not r.get("prerelease")]
    if not stable:
        return {"ok": False, "error": "还没有正式发布的版本", "url": RELEASES_PAGE}

    best = max(stable, key=lambda r: parse_version(
        str(r.get("tag_name") or r.get("name") or "")))
    info = _summarise(best, current)
    info["ok"] = True
    info["cached"] = False
    info["checked_at"] = int(now)

    with _cache_lock:
        _cache["at"] = now
        _cache["data"] = dict(info)

    return info


# ---------------------------------------------------------------- 下载

class _Download:
    """一个下载任务的状态机。同一时间只允许一个任务，
    避免用户连点几下并发拉 126MB × N。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"      # idle / running / done / error
        self.got = 0
        self.total = 0
        self.name = ""
        self.path = ""
        self.error = ""
        self.started = 0.0

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "got": self.got,
                "total": self.total,
                "name": self.name,
                "path": self.path,
                "error": self.error,
                "elapsed": round(time.time() - self.started, 1) if self.started else 0,
            }


_dl = _Download()


def download_status() -> Dict[str, Any]:
    return _dl.snapshot()


def start_download(data_dir: str, asset_name: str = "") -> Dict[str, Any]:
    """开始下载。asset_name 可为空（自动选 x64 exe）；
    传了就**必须是该 Release 里真实存在的资产名** —— 不接受任意 URL。"""
    with _dl.lock:
        if _dl.state == "running":
            return {"ok": False, "error": "已有下载任务在进行中"}
        _dl.state = "running"
        _dl.got = 0
        _dl.total = 0
        _dl.name = asset_name
        _dl.path = ""
        _dl.error = ""
        _dl.started = time.time()

    t = threading.Thread(target=_worker, args=(data_dir, asset_name), daemon=True)
    t.start()
    return {"ok": True, "started": True}


def _worker(data_dir: str, want_name: str) -> None:
    try:
        info = check("", use_cache=True)
        if not info.get("ok"):
            raise RuntimeError(info.get("error") or "无法获取发布信息")

        # 重新从 Release 里取资产：绝不信任外部传入的 URL
        rels = _fetch_releases()
        stable = [r for r in rels if not r.get("draft") and not r.get("prerelease")]
        best = max(stable, key=lambda r: parse_version(
            str(r.get("tag_name") or r.get("name") or "")))
        assets = best.get("assets") or []

        if want_name:
            target = next((a for a in assets
                           if a.get("name") == want_name), None)
            if target is None:
                raise RuntimeError("该版本里没有名为 %s 的资产" % want_name)
        else:
            target = _pick_best_asset(assets)
            if target is None:
                raise RuntimeError("该版本没有可下载的安装包")

        url = target.get("browser_download_url") or ""
        name = target.get("name") or "update.exe"
        total = int(target.get("size") or 0)
        if not url:
            raise RuntimeError("资产缺少下载地址")

        outdir = os.path.join(data_dir, "temp", "update")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, name)
        part = out + ".part"

        with _dl.lock:
            _dl.total = total
            _dl.name = name
            _dl.path = out

        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                with _dl.lock:
                    _dl.got += len(chunk)

        got = os.path.getsize(part)
        # 大小校验：下了一半也算失败，不能让用户点开一个坏安装包
        if total and got != total:
            raise RuntimeError("下载不完整：%d / %d 字节" % (got, total))
        os.replace(part, out)          # 原子替换，避免半截文件被当成成品

        with _dl.lock:
            _dl.state = "done"
            _dl.path = out
            _dl.got = got
    except Exception as e:
        with _dl.lock:
            _dl.state = "error"
            _dl.error = str(e)
        try:
            p = _dl.path + ".part"
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def latest_notes_for_display(body: str) -> str:
    """把 Release 说明里的 Markdown 记号去掉，界面直接显示纯文本。

    前端是纯文本渲染，留着一堆 ## / ** / |---| 很难看。
    """
    s = body or ""
    lines = []
    for ln in s.split("\n"):
        # 丢掉表格分隔行（|---|---|），保留有内容的行
        if re.fullmatch(r"\s*\|[\s:\-|]+\|\s*", ln):
            continue
        # 表格行转成「列 列」的可读形式
        if ln.strip().startswith("|") and ln.strip().endswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            ln = "  ".join(c for c in cells if c)
        lines.append(ln)
    s = "\n".join(lines)
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)     # 标题井号
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)           # 粗体
    s = re.sub(r"`([^`]+)`", r"\1", s)               # 行内代码
    s = re.sub(r"^\s*[-*]\s+", "· ", s, flags=re.M)  # 列表符号
    s = re.sub(r"\n{3,}", "\n\n", s)                 # 压缩空行
    return s.strip()
