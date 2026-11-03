# -*- coding: utf-8 -*-
"""剪贴板操作：把图片放进剪贴板，供微信 Ctrl+V 粘贴。

为什么必须走剪贴板：
微信 4.x 输入区工具栏只有 表情/收藏/文件/截图/直播/语音 六个按钮，
**没有「发送图片」入口**。图片要么拖拽、要么粘贴。
拖拽需要精确坐标（本项目的红线），所以只剩剪贴板粘贴这一条语义路径。
"""
from __future__ import annotations

import io
import os
from typing import Optional

try:
    import win32clipboard
    import win32con
except Exception:  # pragma: no cover
    win32clipboard = None
    win32con = None

# 微信能直接当图片粘贴的格式
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def _image_to_dib(path: str) -> Optional[bytes]:
    """把图片转成 CF_DIB 需要的字节（BMP 去掉 14 字节文件头）。

    优先用 Pillow（能处理 png/jpg/webp 等）；没装 Pillow 时只支持 bmp。
    """
    try:
        from PIL import Image
    except Exception:
        if path.lower().endswith(".bmp"):
            with open(path, "rb") as f:
                return f.read()[14:]
        return None

    try:
        im = Image.open(path)
        # 剪贴板位图不带 alpha；带 alpha 的图直接转 RGB 会变黑，先铺白底
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "BMP")
        return buf.getvalue()[14:]
    except Exception:
        return None


def set_image(path: str) -> bool:
    """把图片文件放进剪贴板（CF_DIB）。成功返回 True。"""
    if win32clipboard is None:
        return False
    data = _image_to_dib(path)
    if not data:
        return False
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def set_files(paths) -> bool:
    """把一批文件放进剪贴板（CF_HDROP），相当于资源管理器里复制这些文件。

    微信收到 CF_HDROP 粘贴后，会当成「发送文件」处理。
    注意：DROPFILES 结构必须用 struct.pack 拼 —— ctypes 的标量对象
    不支持 `+` 运算（会抛 TypeError），早期版本就是在这里静默失败的。
    """
    if win32clipboard is None:
        return False
    try:
        import struct

        paths = [os.path.abspath(p) for p in paths if p]
        if not paths:
            return False

        # DROPFILES：pFiles(4) + pt.x(4) + pt.y(4) + fNC(4) + fWide(4) = 20 字节
        # pFiles 是文件列表相对本结构起点的偏移；fWide=1 表示宽字符
        header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
        body = b""
        for p in paths:
            body += p.encode("utf-16-le") + b"\x00\x00"
        body += b"\x00\x00"          # 列表结尾再补一个空字符
        blob = header + body

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_HDROP, blob)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def clear() -> None:
    """清空剪贴板（发送完把图片清掉，避免下次误粘贴旧内容）。"""
    if win32clipboard is None:
        return
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass