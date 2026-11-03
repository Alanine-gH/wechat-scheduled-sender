# -*- coding: utf-8 -*-
"""备份与恢复：把用户积累下来的配置、内容与线索打成一个包。

为什么需要：
``data/`` 里的东西是**一点点攒出来的** —— 线索表记了几周的私聊、
campaign.txt 是反复改出来的文案、settings.json 是调好的节奏参数。
误删一次、或换台电脑，就全白干。

两条安全约束：
1. **导入前先自动快照**当前 data/，恢复错了还能退回去
   （直接覆盖用户积累正是最不可原谅的操作）
2. **防路径穿越**：zip 里的文件名一律取 basename 并白名单校验，
   绝不 ``extractall`` —— 恶意的 ../../ 能往系统目录写文件
"""
from __future__ import annotations

import json
import os
import time
import zipfile
from typing import Any, Dict, List, Optional

# 只备份这些「用户积累」文件；缓存、密钥、截图不在其中
BACKUP_FILES = (
    "settings.json",      # 调好的节奏/限额/关键词
    "campaign.txt",       # 反复打磨的文案
    "messages.txt",       # 消息池
    "leads.json",         # 攒了几周的线索
    "auto_task.json",     # 自动任务的目标名单
    "schedule_state.json",  # 每日/每周已发次数
    "inbox_state.json",   # 私聊轮询游标
    "failures.json",      # 待补发记录
)

MANIFEST = "manifest.json"
SNAPSHOT_DIR = "backup"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % n


def export(data_dir: str, out_path: Optional[str] = None) -> Dict[str, Any]:
    """打包 data/ 下用户积累的文件。返回清单与实际大小。"""
    if not out_path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(data_dir, SNAPSHOT_DIR,
                                "backup_%s.zip" % stamp)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    included: List[Dict[str, Any]] = []
    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in BACKUP_FILES:
                src = os.path.join(data_dir, name)
                if not os.path.isfile(src):
                    continue
                size = os.path.getsize(src)
                z.write(src, arcname=name)
                included.append({"name": name, "bytes": size})
            z.writestr(MANIFEST, json.dumps({
                "created": int(time.time()),
                "created_text": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files": [x["name"] for x in included],
                "app": "wechat-scheduled-sender",
            }, ensure_ascii=False, indent=2))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    total = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
    return {"ok": True, "path": out_path, "files": included,
            "count": len(included), "bytes": total,
            "size_text": _human(total)}


def list_backups(data_dir: str) -> List[Dict[str, Any]]:
    """列出已有的备份包（新的在前）。"""
    d = os.path.join(data_dir, SNAPSHOT_DIR)
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if not name.lower().endswith(".zip"):
            continue
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except Exception:
            continue
        out.append({"name": name, "path": p, "bytes": st.st_size,
                    "size_text": _human(st.st_size),
                    "at_text": time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x.get("at_text") or "", reverse=True)
    return out


def verify(zip_path: str) -> Dict[str, Any]:
    """只读校验一个备份包：能不能打开、里面有哪些文件、是否安全。"""
    if not os.path.isfile(zip_path):
        return {"ok": False, "error": "文件不存在"}
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            names = z.namelist()
    except Exception as exc:
        return {"ok": False, "error": "不是有效的 zip：%s" % exc}

    # 安全检查：文件名必须就是白名单里的名字，不允许带路径
    bad = [n for n in names
           if n != MANIFEST and (os.path.basename(n) != n
                                 or n not in BACKUP_FILES)]
    if bad:
        return {"ok": False,
                "error": "包含可疑条目（可能是路径穿越）：%s" % ", ".join(bad[:5])}
    return {"ok": True, "names": names,
            "count": len([n for n in names if n != MANIFEST])}


def import_(data_dir: str, zip_path: str) -> Dict[str, Any]:
    """从备份恢复。**先把当前 data/ 快照一份**，恢复错了可以退回。"""
    v = verify(zip_path)
    if not v.get("ok"):
        return v

    # 1) 自动快照当前状态（即使恢复失败，用户的现状也还在）
    snap = export(data_dir, os.path.join(
        data_dir, SNAPSHOT_DIR, "before_restore_%s.zip"
        % time.strftime("%Y%m%d_%H%M%S")))

    restored: List[str] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            for name in z.namelist():
                if name == MANIFEST:
                    continue
                # 2) 再次确认目标就在 data/ 下，且名字合法
                if os.path.basename(name) != name or name not in BACKUP_FILES:
                    continue
                dst = os.path.join(data_dir, name)
                with z.open(name) as src, open(dst, "wb") as out:
                    out.write(src.read())
                restored.append(name)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "restored": restored,
                "snapshot": snap.get("path")}

    return {
        "ok": True,
        "restored": restored,
        "count": len(restored),
        "snapshot": snap.get("path") if snap.get("ok") else "",
        "note": "恢复前的现状已自动快照，若恢复错了可以用该快照退回",
    }