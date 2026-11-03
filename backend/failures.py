# -*- coding: utf-8 -*-
"""失败登记与重发：把「哪些没发出去」持久化，并支持补发。

为什么需要：
原来失败只写进内存里的运行日志，程序一关就查不到 ——
第二天想知道「昨天哪几个群没发成功」只能靠翻截图，也就无从补发。

核心设计：**按内容记账，而不是按流水**
每条失败用 ``target|kind|text|media`` 算出一把「钥匙」，同一个目标+同一条内容
视为同一条待办。于是：
* 失败时登记：已有同钥匙条目就累加次数、更新时间，**不重复堆一堆**
* 成功后销账：按同一把钥匙把待办标记为已解决，无需人工核对
重发因此是「拿待办当计划跑一遍」，成功自然销账、失败自然留下。

文件 ``data/failures.json``（含真实群名与消息内容，已在 .gitignore 里）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

FAIL_FILE = "failures.json"
MAX_ENTRIES = 500          # 上限，防止文件无限增长


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, FAIL_FILE)


def key_of(target: str, kind: str, text: str,
           media_path: str = "") -> str:
    """待办唯一键。用内容而不是时间：同一目标同一条内容就是同一个待办。"""
    raw = "|".join([target or "", kind or "", media_path or "",
                    (text or "").strip()])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def load(data_dir: str) -> Dict[str, Any]:
    try:
        with open(_path(data_dir), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        d = {}
    d.setdefault("items", {})
    return d


def save(data_dir: str, d: Dict[str, Any]) -> None:
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def add(data_dir: str, target: str, kind: str, text: str,
        media_path: str = "", username: str = "",
        error: str = "") -> None:
    """登记一次失败。同钥匙的已存在条目只累加，不重复堆。"""
    d = load(data_dir)
    items = d["items"]
    k = key_of(target, kind, text, media_path)
    now = int(time.time())
    rec = items.get(k)
    if rec:
        rec["attempts"] = int(rec.get("attempts") or 1) + 1
        rec["last_at"] = now
        rec["error"] = error or rec.get("error") or ""
        rec["status"] = "pending"
    else:
        items[k] = {
            "id": k,
            "target": target or "",
            "username": username or "",
            "kind": kind or "main",
            "text": text or "",
            "media_path": media_path or "",
            "error": error or "",
            "attempts": 1,
            "first_at": now,
            "last_at": now,
            "status": "pending",
        }
    if len(items) > MAX_ENTRIES:
        ordered = sorted(items.values(), key=lambda x: x.get("first_at") or 0)
        for r in ordered[:len(items) - MAX_ENTRIES]:
            items.pop(r["id"], None)
    save(data_dir, d)


def resolve(data_dir: str, target: str, kind: str, text: str,
            media_path: str = "") -> bool:
    """按内容销账（发送成功后调用）。返回是否确实销掉了一条。"""
    d = load(data_dir)
    k = key_of(target, kind, text, media_path)
    rec = d["items"].get(k)
    if not rec or rec.get("status") == "resolved":
        return False
    rec["status"] = "resolved"
    rec["resolved_at"] = int(time.time())
    save(data_dir, d)
    return True


def pending(data_dir: str) -> List[Dict[str, Any]]:
    """待补发的失败条目（新的在前）。"""
    d = load(data_dir)
    out = [x for x in d["items"].values() if x.get("status") != "resolved"]
    out.sort(key=lambda x: x.get("last_at") or 0, reverse=True)
    for x in out:
        x["at_text"] = time.strftime(
            "%m-%d %H:%M", time.localtime(x.get("last_at") or 0))
    return out


def stats(data_dir: str) -> Dict[str, Any]:
    d = load(data_dir)
    items = list(d["items"].values())
    pend = [x for x in items if x.get("status") != "resolved"]
    return {
        "pending": len(pend),
        "resolved": len(items) - len(pend),
        "total": len(items),
        # 涉及多少个不同的目标（用户更关心「几个群没发出去」而不是几条）
        "targets": len({x.get("target") for x in pend}),
    }


def clear(data_dir: str, only_resolved: bool = True) -> int:
    """清理：默认只删已解决的（留待办），``only_resolved=False`` 全清。"""
    d = load(data_dir)
    items = d["items"]
    before = len(items)
    if only_resolved:
        d["items"] = {k: v for k, v in items.items()
                      if v.get("status") != "resolved"}
    else:
        d["items"] = {}
    save(data_dir, d)
    return before - len(d["items"])


def build_plan_items(data_dir: str) -> List[Dict[str, Any]]:
    """把待办转成「可执行事件」列表（供重发直接用）。"""
    out = []
    now = time.time()
    for i, x in enumerate(pending(data_dir)):
        out.append({
            # 每条间隔 2 秒错开，避免瞬间连发
            "at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(now + i * 2)),
            "target": x.get("target") or "",
            "kind": x.get("kind") or "main",
            "text": x.get("text") or "",
            "media_path": x.get("media_path") or "",
            "media_first": True,
        })
    return out