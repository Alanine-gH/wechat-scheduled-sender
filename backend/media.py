# -*- coding: utf-8 -*-
"""素材管理：图片 / 任意文件 的三种来源。

三种来源（对应界面上三选一，也可混用）：

1. **文件夹轮转**：扫一个目录里的图片（或任意文件），按发送顺序轮流取。
2. **路径列表**：文本文件里每行一个路径，按行顺序取。
3. **界面选图**：用户在界面上直接挑文件，前端把绝对路径传上来。

统一产出 `List[MediaItem]`，由 planner 分配到每个目标。
"""
from __future__ import annotations

import os
import random
from typing import Dict, List, Optional

# 图片扩展名（微信能直接当图片粘贴的）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
# 允许当「任意文件」发送的扩展名。刻意不含 exe/msi/bat 等可执行文件：
# 微信对可执行文件有额外拦截，且误发风险高。
FILE_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".mp3", ".wav", ".mp4", ".mov", ".avi",
    ".psd", ".ai", ".svg", ".tif", ".tiff",
    # 字幕与代码类，经常需要连素材一起发
    ".srt", ".ass", ".vtt", ".lrc",
    ".py", ".js", ".ts", ".html", ".css", ".java", ".sql", ".sh",
    ".log", ".ini", ".cfg", ".conf",
}

MAX_FILE_BYTES = 100 * 1024 * 1024   # 微信单文件上限约 100 MB


class MediaItem:
    """一条待发素材。"""

    __slots__ = ("path", "kind", "size", "name")

    def __init__(self, path: str, kind: str, size: int):
        self.path = path
        self.kind = kind          # "image" | "file"
        self.size = size
        self.name = os.path.basename(path)

    def as_dict(self) -> Dict[str, object]:
        return {"path": self.path, "kind": self.kind,
                "size": self.size, "name": self.name}


def classify(path: str) -> Optional[str]:
    """判断一个文件能不能发、按什么发。返回 "image" / "file" / None。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in FILE_EXTS:
        return "file"
    return None


def _make(path: str) -> Optional[MediaItem]:
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_FILE_BYTES:
            return None
        kind = classify(path)
        if not kind:
            return None
        return MediaItem(os.path.abspath(path), kind, size)
    except Exception:
        return None


def scan_folder(folder: str, only_images: bool = True) -> List[MediaItem]:
    """扫目录，返回可用素材（按文件名排序，保证可复现）。"""
    out: List[MediaItem] = []
    try:
        if not os.path.isdir(folder):
            return out
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if not os.path.isfile(full):
                continue
            item = _make(full)
            if not item:
                continue
            if only_images and item.kind != "image":
                continue
            out.append(item)
    except Exception:
        pass
    return out


def read_path_list(text_file: str) -> List[MediaItem]:
    """读「每行一个路径」的文本文件。支持相对路径（相对该文件所在目录）。"""
    out: List[MediaItem] = []
    try:
        base = os.path.dirname(os.path.abspath(text_file))
        with open(text_file, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                p = line.strip()
                if not p or p.startswith("#"):
                    continue
                if not os.path.isabs(p):
                    p = os.path.join(base, p)
                item = _make(p)
                if item:
                    out.append(item)
    except Exception:
        pass
    return out


def from_paths(paths) -> List[MediaItem]:
    """界面选的文件（绝对路径列表）。"""
    out: List[MediaItem] = []
    for p in paths or []:
        item = _make(p)
        if item:
            out.append(item)
    return out


def collect(spec: Dict[str, object]) -> List[MediaItem]:
    """按界面传来的配置收集素材。

    spec 形如：
      {"source": "folder"|"list"|"picked"|"mixed",
       "folder": "D:/...", "only_images": true,
       "list_file": "D:/...paths.txt",
       "paths": ["D:/a.png", "D:/b.pdf"]}
    """
    spec = spec or {}
    items: List[MediaItem] = []
    seen = set()

    def add(batch):
        for it in batch:
            if it.path not in seen:
                seen.add(it.path)
                items.append(it)

    src = (spec.get("source") or "mixed").lower()
    if src in ("folder", "mixed"):
        add(scan_folder(spec.get("folder") or "",
                        bool(spec.get("only_images", True))))
    if src in ("list", "mixed"):
        add(read_path_list(spec.get("list_file") or ""))
    if src in ("picked", "mixed"):
        add(from_paths(spec.get("paths")))
    return items


def assign(items: List[MediaItem], targets: List[str],
           mode: str = "rotate", seed: Optional[int] = None) -> Dict[str, List[MediaItem]]:
    """把素材分配到各目标。

    mode:
      * "rotate"（默认）—— 轮流取，保证尽量每个目标拿到的都不一样；
      * "shuffle"  —— 先按种子打乱再轮流取，同一天可复现；
      * "same"     —— 每个目标都发同一批（即全部素材各发一遍）。
    """
    out: Dict[str, List[MediaItem]] = {}
    if not items or not targets:
        return {t: [] for t in targets}

    if mode == "same":
        for t in targets:
            out[t] = list(items)
        return out

    pool = list(items)
    if mode == "shuffle":
        rnd = random.Random(seed)
        rnd.shuffle(pool)

    for i, t in enumerate(targets):
        out[t] = [pool[i % len(pool)]]
    return out


def describe(items: List[MediaItem]) -> Dict[str, object]:
    """给前端的概览。"""
    imgs = sum(1 for x in items if x.kind == "image")
    files = len(items) - imgs
    total = sum(x.size for x in items)
    return {"count": len(items), "images": imgs, "files": files,
            "total_bytes": total,
            "preview": [x.as_dict() for x in items[:8]]}