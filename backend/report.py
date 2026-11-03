# -*- coding: utf-8 -*-
"""投放日志与周报：按方案的 6 项 KPI 出统计，并给出「砍群」建议。

对应方案：
* 六、首月关键指标（单群）
    原文案+引导语投放   7 条/周
    资料钩子投放        2-3 条/周
    群友主动私聊人数     ≥5 人/周
    筛出「有粮食客户」同行 ≥2 人/周
    组长深度沟通         ≥3 人/周
    合作意向/试单        ≥1 条/月
* 五、风险控制 4：**每周砍群** —— 连续两周主动私聊 <3 人的群，
  降低频率或退出。

为什么单独记投放日志（而不是只看 schedule_state 的计数）：
那个计数只记了「某个群本周发过几次」，分不出主文案和钩子，
也看不出哪天断投了。周报要能回答「这周的 7 条主文案、3 条钩子
到底发出去没有」，所以按事件逐条记 jsonl。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from typing import Any, Dict, List, Optional

LOG_FILE = "delivery_log.jsonl"
MAX_LINES = 20000            # 日志上限，超出时重写最近的部分

# 方案六、首月关键指标（单群）
KPI_TARGETS = {
    "main_per_week": 7,
    "hook_per_week_min": 2,
    "hook_per_week_max": 3,
    "chat_people_per_week": 5,
    "need_people_per_week": 2,
    "deep_talk_per_week": 3,
    "deal_per_month": 1,
}

# 「砍群」阈值：连续两周主动私聊少于这个人数就建议降频/退出
CUT_THRESHOLD_PEOPLE = 3


def _log_path(data_dir: str) -> str:
    return os.path.join(data_dir, LOG_FILE)


def log_delivery(data_dir: str, target: str, kind: str, ok: bool,
                 at: Optional[int] = None, note: str = "") -> None:
    """记一条投放。``kind`` 是 main / hook。"""
    rec = {
        "at": int(at if at is not None else time.time()),
        "target": target or "",
        "kind": kind or "main",
        "ok": bool(ok),
    }
    if note:
        rec["note"] = note
    try:
        os.makedirs(data_dir, exist_ok=True)
        p = _log_path(data_dir)
        # 先看行数，超上限就截断重写，避免 jsonl 无限长
        if os.path.exists(p) and _count_lines(p) > MAX_LINES:
            _truncate(p, MAX_LINES // 2)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _count_lines(path: str) -> int:
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in f:
                n += 1
    except Exception:
        pass
    return n


def _truncate(path: str, keep: int) -> None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-keep:])
    except Exception:
        pass


def load_deliveries(data_dir: str, since_ts: int = 0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(_log_path(data_dir), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if int(rec.get("at") or 0) >= (since_ts or 0):
                    out.append(rec)
    except FileNotFoundError:
        return []
    except Exception:
        return out
    return out


# ---------------------------------------------------------------------------
# 周范围
# ---------------------------------------------------------------------------
def week_range(anchor: Optional[_dt.date] = None) -> tuple:
    """返回本周的 [周一 00:00, 下周一 00:00) 两个时间戳。"""
    anchor = anchor or _dt.date.today()
    monday = anchor - _dt.timedelta(days=anchor.weekday())
    start = _dt.datetime.combine(monday, _dt.time.min)
    end = start + _dt.timedelta(days=7)
    return int(start.timestamp()), int(end.timestamp())


def last_weeks(anchor: Optional[_dt.date] = None, n: int = 2) -> List[tuple]:
    """返回最近 n 周的 (start, end, 周一日期) 列表，近的在后。"""
    anchor = anchor or _dt.date.today()
    out = []
    for i in range(n - 1, -1, -1):
        day = anchor - _dt.timedelta(days=7 * i)
        s, e = week_range(day)
        out.append((s, e, day - _dt.timedelta(days=day.weekday())))
    return out


# ---------------------------------------------------------------------------
# 周报
# ---------------------------------------------------------------------------
def weekly_report(data_dir: str,
                  anchor: Optional[_dt.date] = None,
                  group_hint: str = "") -> Dict[str, Any]:
    """出一份周报：投放量、线索量、6 项 KPI 达成、砍群建议。

    ``group_hint``：投放日志里的 target 是群名，可用于把线索按来源群归拢
    （线索的来源群由人工登记，没有就归到「未标注」）。
    """
    try:
        import leads as L
    except Exception:
        L = None

    anchor = anchor or _dt.date.today()
    start, end = week_range(anchor)
    wk = "%s ~ %s" % (_dt.date.fromtimestamp(start).isoformat(),
                      (_dt.date.fromtimestamp(end) - _dt.timedelta(days=1)).isoformat())

    # ---- 投放侧 ----
    dels = load_deliveries(data_dir, since_ts=start)
    dels = [d for d in dels if int(d.get("at") or 0) < end]
    main_ok = [d for d in dels if d.get("kind") == "main" and d.get("ok")]
    hook_ok = [d for d in dels if d.get("kind") == "hook" and d.get("ok")]
    main_fail = [d for d in dels if d.get("kind") == "main" and not d.get("ok")]
    hook_fail = [d for d in dels if d.get("kind") == "hook" and not d.get("ok")]
    main_targets = sorted({d.get("target") for d in main_ok if d.get("target")})
    hook_targets = sorted({d.get("target") for d in hook_ok if d.get("target")})

    # ---- 线索侧 ----
    lead_stats = L.stats(data_dir, since_ts=start) if L else {}
    leads = [x for x in (L.list_leads(data_dir, limit=0) if L else [])
             if start <= int(x.get("at") or 0) < end]
    # 把「已转组长 / 已成交」视为已深度沟通
    deep = [x for x in leads
            if x.get("status") in ("escalated", "won")]
    won = [x for x in leads if x.get("status") == "won"]

    # 筹码：每群投放次数（方案指标是「单群」口径）
    per_target: Dict[str, Dict[str, int]] = {}
    for d in dels:
        t = d.get("target") or "(未知)"
        b = per_target.setdefault(t, {"main": 0, "hook": 0,
                                      "main_fail": 0, "hook_fail": 0})
        key = ("main" if d.get("kind") == "main" else "hook")
        b[key if d.get("ok") else key + "_fail"] = \
            b.get(key if d.get("ok") else key + "_fail", 0) + 1

    # ---- KPI 达成（单群口径取平均）----
    n_main_targets = max(1, len(main_targets))
    avg_main = round(len(main_ok) / n_main_targets, 2) if main_ok else 0
    avg_hook = round(len(hook_ok) / max(1, len(hook_targets)), 2) if hook_ok else 0

    kpi = [
        {"key": "main_per_week", "name": "原文案+引导语投放（单群）",
         "value": avg_main, "target": "≥ %d 条/周" % KPI_TARGETS["main_per_week"],
         "ok": avg_main >= KPI_TARGETS["main_per_week"]},
        {"key": "hook_per_week", "name": "资料钩子投放（单群）",
         "value": avg_hook,
         "target": "%d-%d 条/周" % (KPI_TARGETS["hook_per_week_min"],
                                    KPI_TARGETS["hook_per_week_max"]),
         "ok": KPI_TARGETS["hook_per_week_min"] <= avg_hook <= KPI_TARGETS["hook_per_week_max"]},
        {"key": "chat_people", "name": "群友主动私聊人数",
         "value": lead_stats.get("people", 0),
         "target": "≥ %d 人/周" % KPI_TARGETS["chat_people_per_week"],
         "ok": lead_stats.get("people", 0) >= KPI_TARGETS["chat_people_per_week"]},
        {"key": "need_people", "name": "筛出「有粮食客户」的同行",
         "value": lead_stats.get("need_people", 0),
         "target": "≥ %d 人/周" % KPI_TARGETS["need_people_per_week"],
         "ok": lead_stats.get("need_people", 0) >= KPI_TARGETS["need_people_per_week"]},
        {"key": "deep_talk", "name": "组长深度沟通（已转组长）",
         "value": len({x.get("username") for x in deep}),
         "target": "≥ %d 人/周" % KPI_TARGETS["deep_talk_per_week"],
         "ok": len({x.get("username") for x in deep}) >= KPI_TARGETS["deep_talk_per_week"]},
        {"key": "deal", "name": "合作意向 / 试单",
         "value": len({x.get("username") for x in won}),
         "target": "≥ %d 条/月" % KPI_TARGETS["deal_per_month"],
         "ok": len({x.get("username") for x in won}) >= KPI_TARGETS["deal_per_month"],
         "monthly": True},
    ]

    # ---- 砍群建议：连续两周主动私聊 < 3 人的群 ----
    # 前提：该群在**两周里都真有投放**。否则刚启用时数据天然稀疏，
    # 会把每个群都判成「该砍」——那是误导，不是洞察。
    weeks = last_weeks(anchor, 2)
    all_leads = L.list_leads(data_dir, limit=0) if L else []
    cut_candidates = []
    for t in main_targets:
        per_week = []
        delivered_weeks = 0
        for (ws, we, _monday) in weeks:
            cnt = len({x.get("username") for x in all_leads
                       if ws <= int(x.get("at") or 0) < we
                       and (x.get("source_group") or "") == t})
            per_week.append(cnt)
            if any(ws <= int(d.get("at") or 0) < we
                   for d in load_deliveries(data_dir, since_ts=ws)
                   if d.get("target") == t):
                delivered_weeks += 1
        if delivered_weeks < 2:
            continue            # 数据还不够两周，不给结论
        if all(c < CUT_THRESHOLD_PEOPLE for c in per_week):
            cut_candidates.append({
                "target": t, "per_week": per_week,
                "hint": "连续两周主动私聊 < %d 人，建议降频或退出"
                        % CUT_THRESHOLD_PEOPLE,
            })

    return {
        "week": wk,
        "week_start": start,
        "week_end": end,
        "delivery": {
            "main_ok": len(main_ok), "main_fail": len(main_fail),
            "hook_ok": len(hook_ok), "hook_fail": len(hook_fail),
            "main_targets": len(main_targets), "hook_targets": len(hook_targets),
            "per_target": per_target,
        },
        "leads": lead_stats,
        "leads_detail": {
            "need": lead_stats.get("by_intent", {}).get("need", 0),
            "hook": lead_stats.get("by_intent", {}).get("hook", 0),
            "escalated": lead_stats.get("by_status", {}).get("escalated", 0),
            "won": lead_stats.get("by_status", {}).get("won", 0),
        },
        "kpi": kpi,
        "kpi_passed": sum(1 for k in kpi if k["ok"]),
        "kpi_total": len(kpi),
        "cut_candidates": cut_candidates,
        "note": (
            "「群友主动私聊人数」按人去重统计；"
            "「组长深度沟通」用线索状态=已转组长/已成交 近似，"
            "实际沟通发生在微信里，建议同时人工核对；"
            "砍群建议依赖线索的「来源群」字段，未登记时无法归因。"
        ),
    }