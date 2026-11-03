# -*- coding: utf-8 -*-
"""调度器：按「日期种子」生成每日随机发送计划，并按间隔执行。

计划规则（用户需求）：
- 消息内容来自 data/messages.txt，一行一条
- 每天 30 条 × 5 轮 ≈ 150 条，约 2.5 小时发完
- 轮与轮之间随机间隔，均值约 5 分钟
- 随机数种子 = 当天日期（YYYY-MM-DD），保证同一天的计划可复现
"""
from __future__ import annotations

import datetime as _dt
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import campaign as _campaign
import scheduler as _scheduler

DEFAULT_ROUNDS = 1          # 轮数：默认 1 轮
DEFAULT_PER_ROUND = 0       # 每轮条数：0 = 跟随已选目标数（每个目标恰好 1 条）
DEFAULT_INTERVAL_MIN = 3.5  # 轮间隔下限（分钟）
DEFAULT_INTERVAL_MAX = 6.5  # 轮间隔上限（分钟）


@dataclass
class PlanRound:
    index: int
    delay_seconds: float          # 相对上一轮开始的等待秒数
    messages: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "delay_seconds": round(self.delay_seconds, 2),
            "delay_minutes": round(self.delay_seconds / 60.0, 2),
            "messages": self.messages,
            "count": len(self.messages),
        }


# 多行消息的分隔符：一条消息 = 两个 --- 之间的内容（块内换行会被完整保留）
MESSAGE_SEPARATOR = "---"

MODE_LINE = "line"            # 每行一条消息
MODE_SEPARATOR = "separator"  # --- 分隔，块内换行保留


def parse_messages(text: str, mode: Optional[str] = None) -> tuple:
    """解析消息池文本，返回 (messages, mode)。

    由 ``mode`` 显式指定（推荐，行为确定）：

    * ``"line"``：每行一条消息，忽略空行与 ``#`` 开头的注释行。
    * ``"separator"``：以独立的 ``---`` 行分隔消息，**块内换行原样保留**，
      所以「一条多行消息」不会被拆开。

    ``mode`` 为 None 时自动判断：文件里出现 ``---`` 就按 separator，否则按 line。
    """
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    if mode not in (MODE_LINE, MODE_SEPARATOR):
        mode = MODE_SEPARATOR if any(
            ln.strip() == MESSAGE_SEPARATOR for ln in raw_lines
        ) else MODE_LINE

    if mode == MODE_LINE:
        out = []
        for ln in raw_lines:
            if not ln.strip():
                continue
            if ln.lstrip().startswith("#"):
                continue
            out.append(ln)
        return out, MODE_LINE

    messages: List[str] = []
    buf: List[str] = []
    for ln in raw_lines:
        if ln.strip() == MESSAGE_SEPARATOR:
            block = "\n".join(buf).strip("\n")
            if block.strip():
                messages.append(block)
            buf = []
            continue
        buf.append(ln)
    tail = "\n".join(buf).strip("\n")
    if tail.strip():
        messages.append(tail)
    return messages, MODE_SEPARATOR


def load_messages(path: str, mode: Optional[str] = None) -> List[str]:
    """从 txt 读取消息池。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return []
    return parse_messages(text, mode)[0]


def load_messages_detail(path: str, mode: Optional[str] = None) -> Dict[str, Any]:
    """读取消息池并附带解析模式，供前端展示。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {"text": "", "messages": [], "count": 0, "mode": mode or MODE_LINE}
    msgs, used = parse_messages(text, mode)
    return {"text": text, "messages": msgs, "count": len(msgs), "mode": used}


def build_plan(
    messages: List[str],
    targets: List[str],
    seed_date: Optional[str] = None,
    rounds: int = DEFAULT_ROUNDS,
    per_round: int = DEFAULT_PER_ROUND,
    interval_min: float = DEFAULT_INTERVAL_MIN,
    interval_max: float = DEFAULT_INTERVAL_MAX,
    media_by_target: Optional[Dict[str, List[str]]] = None,
    media_first: bool = True,
) -> Dict[str, Any]:
    """生成当日发送计划。

    ``per_round`` 传 0（默认）时自动取「已选目标数」，即**每个目标恰好发 1 条**，
    不会把同一条消息对同一个群反复刷。

    每轮从 messages 里随机抽 per_round 条（不足则循环取），
    再轮转分配给 targets；轮间隔用日期种子随机生成，当天可复现。

    ``media_by_target``：{目标名: [素材路径, ...]}。每条消息按目标取一条素材，
    第 r 轮取 pool[(r-1) % len(pool)]，这样多轮时素材会轮换。
    ``media_first`` 决定素材与文字的先后。
    """
    seed_date = seed_date or _dt.date.today().isoformat()
    rng = random.Random(seed_date)  # 种子 = 日期，稳定可复现

    per_round = int(per_round or 0)
    if per_round <= 0:
        per_round = max(1, len(targets))
    rounds = max(1, int(rounds or 1))
    media_by_target = media_by_target or {}

    # 间隔是「相邻两次发送之间」的等待时间（不是轮与轮之间）：
    # 30 个群 × 约 5 分钟 ≈ 150 分钟 ≈ 2.5 小时，符合原始需求。
    delay_rng = random.Random("%s|delay" % seed_date)

    plan_rounds: List[PlanRound] = []
    for r in range(rounds):
        if not messages:
            # 消息池为空时不能直接产出 0 条 —— 否则「只发素材」模式发不出去。
            # 有素材就按 per_round 补齐空文字占位，真正的内容由 media_path 承担。
            if media_by_target and per_round > 0:
                picked = [""] * per_round
            else:
                picked = []
        elif len(messages) >= per_round:
            picked = rng.sample(messages, per_round)
        else:
            picked = [messages[rng.randrange(len(messages))] for _ in range(per_round)]
        plan_rounds.append(PlanRound(index=r + 1, delay_seconds=0.0, messages=picked))

    # 把每轮消息轮转分配给目标（随机起点，保证每个目标都被覆盖），
    # 并给每一条消息分配随机等待时间。
    assignments: List[Dict[str, Any]] = []
    tcount = max(1, len(targets))
    first_send = True
    media_count = 0
    for pr in plan_rounds:
        rng2 = random.Random("%s|%d" % (seed_date, pr.index))
        start = rng2.randrange(tcount)
        pairs = []
        for i, msg in enumerate(pr.messages):
            tgt = targets[(start + i) % tcount]
            if first_send:
                wait = 0.0            # 第一条立即发
                first_send = False
            else:
                wait = delay_rng.uniform(interval_min, interval_max) * 60.0
            pool = media_by_target.get(tgt) or []
            media_path = ""
            if pool:
                media_path = pool[(pr.index - 1) % len(pool)]
                media_count += 1
            pairs.append({
                "target": tgt,
                "text": msg,
                "media_path": media_path,
                "media_first": bool(media_first),
                "delay_seconds": round(wait, 2),
            })
        pr.delay_seconds = sum(p["delay_seconds"] for p in pairs)
        assignments.append({"index": pr.index, "pairs": pairs})

    total = sum(len(a["pairs"]) for a in assignments)
    total_seconds = sum(pp["delay_seconds"] for a in assignments for pp in a["pairs"])

    return {
        "seed_date": seed_date,
        "rounds": rounds,
        "per_round": per_round,
        "targets": targets,
        "target_count": len(targets),
        "message_pool": len(messages),
        "total_messages": total,
        "media_count": media_count,
        "estimated_seconds": round(total_seconds, 1),
        "estimated_minutes": round(total_seconds / 60.0, 1),
        "round_plans": [pr.to_dict() for pr in plan_rounds],
        "assignments": assignments,
    }


# ---------------------------------------------------------------------------
# 内容编排计划（绝对时间 + 双波次剧本）
# ---------------------------------------------------------------------------
def build_campaign_plan(targets: List[str],
                        campaign_obj,
                        day: Optional[_dt.date] = None,
                        start_at: str = _scheduler.DEFAULT_START_AT,
                        interval_min: float = DEFAULT_INTERVAL_MIN,
                        interval_max: float = DEFAULT_INTERVAL_MAX,
                        hook_delay_min: float = _scheduler.DEFAULT_HOOK_DELAY_MIN,
                        hook_days: Optional[List[str]] = None,
                        media_by_target: Optional[Dict[str, List[str]]] = None,
                        weekly_cap: int = _scheduler.DEFAULT_WEEKLY_CAP,
                        data_dir: str = "",
                        now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """生成「按星期内容 + 固定时间 + 双波次」的投放计划。

    与 `build_plan` 的区别：这里产出的是**绝对时间事件表**
    （主文案 + 各自的钩子），而不是「两条之间的相对等待」。
    方案要求固定晚 8 点发、30 分钟后补钩子，相对等待做不到这一点。

    ``weekly_cap`` > 0 时按「每群每周上限」过滤目标（状态记在 data_dir）。
    """
    day = day or (now or _dt.datetime.now()).date()
    media_by_target = media_by_target or {}

    # 频次过滤：达到本周上限的群本轮跳过
    skipped: List[str] = []
    if weekly_cap and weekly_cap > 0 and data_dir:
        allowed = _scheduler.filter_by_cap(targets, data_dir, weekly_cap, day)
        skipped = [t for t in targets if t not in allowed]
        targets = allowed

    events = _scheduler.build_events(
        targets, campaign_obj, day=day, start_at=start_at,
        interval_min=interval_min, interval_max=interval_max,
        hook_delay_min=hook_delay_min, hook_days=hook_days,
        media_by_target=media_by_target,
        seed=day.isoformat(), now=now,
    )

    # datetime 不能直接 JSON 序列化，统一转成 ISO 字符串
    out_events: List[Dict[str, Any]] = []
    for e in events:
        out_events.append({
            "at": e["at"].isoformat(timespec="seconds"),
            "target": e["target"],
            "kind": e["kind"],
            "text": e["text"],
            "media_path": e.get("media_path") or "",
            "media_first": True,
        })

    main_count = sum(1 for e in out_events if e["kind"] == "main")
    hook_count = sum(1 for e in out_events if e["kind"] == "hook")
    span = 0.0
    if out_events:
        first = _dt.datetime.fromisoformat(out_events[0]["at"])
        last = _dt.datetime.fromisoformat(out_events[-1]["at"])
        span = (last - first).total_seconds()

    camp = campaign_obj
    return {
        "mode": "campaign",
        "seed_date": day.isoformat(),
        "weekday": _campaign.weekday_key(day),
        "weekday_cn": _campaign.weekday_cn(_campaign.weekday_key(day)),
        "start_at": start_at,
        "hook_delay_minutes": hook_delay_min,
        "hook_today": bool(hook_count),
        "targets": targets,
        "target_count": len(targets),
        "skipped_by_cap": skipped,
        "weekly_cap": weekly_cap,
        "total_messages": len(out_events),
        "main_count": main_count,
        "hook_count": hook_count,
        "span_minutes": round(span / 60.0, 1),
        "estimated_minutes": round(span / 60.0, 1),
        "campaign": camp.to_dict() if camp is not None else {},
        "main_preview": camp.compose(day) if camp is not None else "",
        "hook_preview": (camp.hook(day) or "") if camp is not None else "",
        "events": out_events,
    }
