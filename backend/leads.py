# -*- coding: utf-8 -*-
"""线索表：登记主动私聊的群友、跟进状态，并导出 CSV。

对应方案 3.1 节的「登记线索表：昵称、需求类型、跟进状态、来源群」
和第四节组员C的职责。

存储用单个 JSON（``data/leads.json``），不引数据库：
线索量级是每周几十条，JSON 足够，也方便用户直接看、直接备份。
（该文件含真实昵称与会话内容，已在 .gitignore 里，绝不提交。）

跟进状态按方案里的流程定义：
    new       —— 刚收到，待筛选
    material  —— 已发资料（低意向，靠群内内容持续触达）
    escalated —— 已转组长（高意向，当天转）
    won       —— 已成交 / 谈成合作
    nurture   —— 长期培育（暂无客户，不投入太多精力）
    invalid   —— 无效（无关、广告、重复）
"""
from __future__ import annotations

import csv
import json
import os
import time
from typing import Any, Dict, List, Optional

LEADS_FILE = "leads.json"
MAX_LEADS = 3000            # 上限，防止文件无限增长

STATUSES = ("new", "material", "escalated", "won", "nurture", "invalid")
STATUS_CN = {
    "new": "待筛选",
    "material": "已发资料",
    "escalated": "已转组长",
    "won": "已成交",
    "nurture": "长期培育",
    "invalid": "无效",
}
INTENT_CN = {"need": "有具体需求", "hook": "仅领资料", "other": "其它"}


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, LEADS_FILE)


def load(data_dir: str) -> Dict[str, Any]:
    try:
        with open(_path(data_dir), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        d = {}
    d.setdefault("leads", {})
    return d


def save(data_dir: str, d: Dict[str, Any]) -> bool:
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def make_id(username: str, sort_seq: int) -> str:
    """线索唯一键：同一会话 + 同一条消息 = 一条线索，天然去重。

    这样重复轮询、重跑历史都不会产生重复线索。
    """
    return "%s:%d" % (username or "", int(sort_seq or 0))


def add_items(items: List[Dict[str, Any]], data_dir: str,
              source_group: str = "") -> int:
    """把 inbox 产出的条目登记成线索，返回新增条数（已存在的跳过）。"""
    d = load(data_dir)
    leads = d["leads"]
    added = 0
    for it in items or []:
        lid = make_id(it.get("username") or "", it.get("sort_seq") or 0)
        if lid in leads:
            # 已登记：更新一下关键词/意图（消息内容可能被补全），不改状态
            rec = leads[lid]
            if it.get("keywords"):
                rec["keywords"] = it["keywords"]
            continue
        leads[lid] = {
            "id": lid,
            "username": it.get("username") or "",
            "name": it.get("name") or "",
            "at": int(it.get("at") or 0),
            "sort_seq": int(it.get("sort_seq") or 0),
            "text": it.get("text") or "",
            "type": it.get("type") or "",
            "intent": it.get("intent") or "other",
            "keywords": it.get("keywords") or [],
            "status": "new",
            "source_group": source_group or "",
            "note": "",
            "created": int(time.time()),
            "updated": int(time.time()),
            # 是否已弹过桌面通知。只对高意向线索用，避免同一人反复弹窗
            "notified": False,
        }
        added += 1

    # 超过上限时去掉最旧的
    if len(leads) > MAX_LEADS:
        ordered = sorted(leads.values(), key=lambda x: x.get("at") or 0)
        for rec in ordered[:len(leads) - MAX_LEADS]:
            leads.pop(rec["id"], None)
    save(data_dir, d)
    return added


def list_leads(data_dir: str,
               status: Optional[str] = None,
               intent: Optional[str] = None,
               limit: int = 200) -> List[Dict[str, Any]]:
    """列出线索，按时间倒序。"""
    d = load(data_dir)
    out = list(d["leads"].values())
    if status:
        out = [x for x in out if x.get("status") == status]
    if intent:
        out = [x for x in out if x.get("intent") == intent]
    out.sort(key=lambda x: x.get("at") or 0, reverse=True)
    for x in out:
        x["status_cn"] = STATUS_CN.get(x.get("status") or "", x.get("status") or "")
        x["intent_cn"] = INTENT_CN.get(x.get("intent") or "", x.get("intent") or "")
    return out[:limit] if limit > 0 else out


def pending_notify(data_dir: str, limit: int = 5) -> List[Dict[str, Any]]:
    """还没弹过通知的「高意向」线索（intent=need）。

    为什么只通知高意向：方案里「仅领资料」是低意向，天天弹窗只会让人关掉通知；
    「有具体客户」才是当天要转组长的，值得打断一下。
    """
    d = load(data_dir)
    out = [x for x in d["leads"].values()
           if x.get("intent") == "need" and not x.get("notified")]
    out.sort(key=lambda x: x.get("at") or 0)
    for x in out:
        x["at_text"] = time.strftime("%m-%d %H:%M",
                                     time.localtime(x.get("at") or 0))
    return out[:limit] if limit > 0 else out


def mark_notified(data_dir: str, lead_ids: List[str]) -> int:
    """标记为已通知，避免同一线索反复弹窗。"""
    d = load(data_dir)
    n = 0
    for lid in lead_ids or []:
        rec = d["leads"].get(lid)
        if rec and not rec.get("notified"):
            rec["notified"] = True
            n += 1
    if n:
        save(data_dir, d)
    return n


def get_lead(data_dir: str, lead_id: str) -> Optional[Dict[str, Any]]:
    """取单条线索（带上中文标签），不存在返回 None。"""
    d = load(data_dir)
    rec = d["leads"].get(lead_id)
    if not rec:
        return None
    out = dict(rec)
    out["status_cn"] = STATUS_CN.get(out.get("status") or "", out.get("status") or "")
    out["intent_cn"] = INTENT_CN.get(out.get("intent") or "", out.get("intent") or "")
    return out


def update_lead(data_dir: str, lead_id: str, **fields) -> bool:
    """更新跟进状态 / 备注 / 来源群。只接受白名单字段。"""
    d = load(data_dir)
    rec = d["leads"].get(lead_id)
    if not rec:
        return False
    if "status" in fields:
        st = str(fields["status"] or "")
        if st not in STATUSES:
            return False
        rec["status"] = st
    if "note" in fields:
        rec["note"] = str(fields["note"] or "")[:500]
    if "source_group" in fields:
        rec["source_group"] = str(fields["source_group"] or "")[:120]
    if "name" in fields and fields["name"]:
        rec["name"] = str(fields["name"])[:80]
    rec["updated"] = int(time.time())
    save(data_dir, d)
    return True


def stats(data_dir: str, since_ts: int = 0) -> Dict[str, Any]:
    """线索统计（供周报用）。``since_ts`` 只统计该时间之后的。"""
    d = load(data_dir)
    leads = [x for x in d["leads"].values()
             if (x.get("at") or 0) >= (since_ts or 0)]
    by_status: Dict[str, int] = {k: 0 for k in STATUSES}
    by_intent: Dict[str, int] = {"need": 0, "hook": 0, "other": 0}
    people = set()
    for x in leads:
        by_status[x.get("status") or "new"] = by_status.get(
            x.get("status") or "new", 0) + 1
        by_intent[x.get("intent") or "other"] = by_intent.get(
            x.get("intent") or "other", 0) + 1
        people.add(x.get("username") or "")
    return {
        "total": len(leads),
        "people": len(people),              # 去重后的人数（KPI 用这个）
        "by_status": by_status,
        "by_intent": by_intent,
        "need_people": len({x.get("username") for x in leads
                            if x.get("intent") == "need"}),
    }


CSV_HEADER = ["时间", "昵称", "微信号", "意图", "关键词", "内容",
              "跟进状态", "来源群", "备注"]


def export_csv(data_dir: str, out_path: str,
               status: Optional[str] = None) -> Dict[str, Any]:
    """导出 CSV。写 UTF-8 BOM，Excel 直接双击不乱码。"""
    rows = list_leads(data_dir, status=status, limit=0)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for r in rows:
                w.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(r.get("at") or 0)),
                    r.get("name") or "",
                    r.get("username") or "",
                    r.get("intent_cn") or "",
                    "、".join(r.get("keywords") or []),
                    (r.get("text") or "").replace("\n", " "),
                    r.get("status_cn") or "",
                    r.get("source_group") or "",
                    r.get("note") or "",
                ])
        return {"ok": True, "path": out_path, "rows": len(rows),
                "bytes": os.path.getsize(out_path)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}