# -*- coding: utf-8 -*-
"""自动清理：按保留天数回收截图与投放日志。

为什么需要：这套工具是「每天自动发」的长期常驻程序，
截图按每个目标每次发送一张累积（30 个群 × 每天 2 张 × 365 天 ≈ 2 万张），
投放日志按事件追加。不回收的话磁盘迟早被自己撑满。

两条原则：
1. **只删自己产生的东西** —— 严格限定在 ``shots/`` 下的日期目录与
   ``delivery_log.jsonl``，绝不碰用户的其它文件。
2. **删了什么要能说清** —— 返回清单（目录名/文件数/字节数）并写运行日志，
   而不是默默删掉，否则用户某天发现截图没了会以为是 bug。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List

# 截图目录名形如 20261103
DATE_DIR_RE = re.compile(r"^\d{8}$")


def _day_dir_date(name: str) -> float:
    try:
        return time.mktime(time.strptime(name, "%Y%m%d"))
    except Exception:
        return 0.0


def _tree_size(path: str) -> tuple:
    """返回 (文件数, 字节数)。"""
    n = 0
    size = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                size += os.path.getsize(fp)
                n += 1
            except Exception:
                pass
    return n, size


def clean_shots(shot_dir: str, keep_days: int,
                now: float = 0.0) -> Dict[str, Any]:
    """删掉超过保留天数的截图日期目录。keep_days<=0 视为不清理。"""
    out: Dict[str, Any] = {"removed": [], "files": 0, "bytes": 0}
    if keep_days <= 0 or not shot_dir or not os.path.isdir(shot_dir):
        return out
    cutoff = (now or time.time()) - keep_days * 86400
    for name in os.listdir(shot_dir):
        full = os.path.join(shot_dir, name)
        if not (os.path.isdir(full) and DATE_DIR_RE.match(name)):
            continue          # 只认日期目录，其它一概不碰
        if _day_dir_date(name) >= cutoff:
            continue
        n, size = _tree_size(full)
        try:
            for root, dirs, files in os.walk(full, topdown=False):
                for f in files:
                    os.remove(os.path.join(root, f))
                for d in dirs:
                    os.rmdir(os.path.join(root, d))
            os.rmdir(full)
        except Exception:
            continue
        out["removed"].append({"dir": name, "files": n, "bytes": size})
        out["files"] += n
        out["bytes"] += size
    return out


def clean_logs(data_dir: str, keep_days: int,
               now: float = 0.0) -> Dict[str, Any]:
    """把投放日志里超过保留天数的记录丢掉（重写文件，保留近期的）。"""
    out: Dict[str, Any] = {"kept": 0, "dropped": 0, "bytes_before": 0,
                           "bytes_after": 0}
    path = os.path.join(data_dir, "delivery_log.jsonl")
    if keep_days <= 0 or not os.path.isfile(path):
        return out
    cutoff = (now or time.time()) - keep_days * 86400
    try:
        out["bytes_before"] = os.path.getsize(path)
        kept: List[str] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    at = int((json.loads(s) or {}).get("at") or 0)
                except Exception:
                    continue
                if at >= cutoff:
                    kept.append(s)
                else:
                    out["dropped"] += 1
        out["kept"] = len(kept)
        with open(path, "w", encoding="utf-8") as f:
            if kept:
                f.write("\n".join(kept) + "\n")
        out["bytes_after"] = os.path.getsize(path)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def clean_inbox_state(data_dir: str, keep_days: int = 180,
                      now: float = 0.0) -> Dict[str, Any]:
    """清掉很久没轮询过的会话游标（对方早就不联系了，没必要一直留着）。"""
    out: Dict[str, Any] = {"dropped": 0}
    path = os.path.join(data_dir, "inbox_state.json")
    if not os.path.isfile(path):
        return out
    cutoff = (now or time.time()) - keep_days * 86400
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f) or {}
        chats = st.get("chats") or {}
        keep = {k: v for k, v in chats.items()
                if int((v or {}).get("last_poll") or 0) >= cutoff}
        out["dropped"] = len(chats) - len(keep)
        if out["dropped"] > 0:
            st["chats"] = keep
            with open(path, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def run_all(data_dir: str, shot_dir: str,
            log_keep_days: int, shot_keep_days: int,
            now: float = 0.0) -> Dict[str, Any]:
    """执行一次完整清理，返回可直接展示的清单。"""
    r = {
        "shots": clean_shots(shot_dir, shot_keep_days, now),
        "logs": clean_logs(data_dir, log_keep_days, now),
        "inbox": clean_inbox_state(data_dir, 180, now),
    }
    r["total_files"] = r["shots"].get("files", 0)
    r["total_bytes"] = r["shots"].get("bytes", 0)
    r["ok"] = True
    return r