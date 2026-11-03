# -*- coding: utf-8 -*-
"""定时调度：绝对时间触发 + 多波次剧本 + 每群每周频次上限。

对应运营方案：
* 主投放**固定晚 8 点发**（不是「两条之间随机等 N 分钟」）
* 文案发出**间隔 30 分钟**后补发资料钩子
* 钩子**每周 2-3 次**，固定在周二/周四/周六

为什么要把「相对间隔」升级成「绝对时间 + 事件队列」：
方案要求的是「固定时间点」，而多个群依次发完往往要一两个小时。
如果按相对间隔串行等待，钩子会被推到很晚，和方案说的「30 分钟后」不符。
所以这里生成一张**按时间排序的事件表**（主文案事件 + 对应的钩子事件），
执行时按事件时间推进 —— 群 1 的钩子可以插在群 5 的主文案之前，各不相干。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
from typing import Any, Dict, List, Optional

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULT_START_AT = "20:00"        # 方案：固定晚 8 点
DEFAULT_HOOK_DELAY_MIN = 30.0     # 方案：文案发出 30 分钟后补钩子
DEFAULT_WEEKLY_CAP = 0            # 每群每周投放上限；0 = 不限

STATE_FILE = "schedule_state.json"


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------
def parse_hhmm(s: Optional[str], default: str = DEFAULT_START_AT) -> tuple:
    """把 ``"20:00"`` / ``"20:00:00"`` 解析成 (时, 分)。非法值回退到默认。"""
    raw = (s or default or "").strip()
    parts = raw.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    try:
        h, m = (int(x) for x in str(default).split(":")[:2])
        return h, m
    except Exception:
        return 20, 0


def at_datetime(day: _dt.date, hhmm: Optional[str]) -> _dt.datetime:
    """把「某天的某个时刻」变成 datetime。"""
    h, m = parse_hhmm(hhmm)
    return _dt.datetime.combine(day, _dt.time(hour=h, minute=m))


def weekday_key(date: _dt.date) -> str:
    return WEEKDAY_KEYS[date.weekday()]


def in_days(date: _dt.date, days: Optional[List[str]]) -> bool:
    """当天是否在允许执行的星期里。days 为空表示每天都允许。"""
    if not days:
        return True
    return weekday_key(date) in [d.lower() for d in days]


# ---------------------------------------------------------------------------
# 发送时段窗口
# ---------------------------------------------------------------------------
def in_window(when: _dt.datetime,
              start: Optional[str] = "08:00",
              end: Optional[str] = "22:00") -> bool:
    """判断某时刻是否落在允许发送的时段内。

    支持跨零点（如 22:00 ~ 06:00）：把时段看成环上的一段，
    而不是简单地比较 start <= t <= end —— 后者对跨零点会永远为假。
    起止相同视为全天开放。
    """
    if not start or not end or start == end:
        return True
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)
    cur = when.hour * 60 + when.minute
    lo, hi = sh * 60 + sm, eh * 60 + em
    if lo < hi:
        return lo <= cur < hi
    # 跨零点：cur 在 [lo, 1440) 或 [0, hi)
    return cur >= lo or cur < hi


def next_window_start(when: _dt.datetime,
                      start: Optional[str] = "08:00",
                      end: Optional[str] = "22:00") -> _dt.datetime:
    """返回从 when 起的下一个时段起点；已在窗口内则原样返回。"""
    if in_window(when, start, end):
        return when
    sh, sm = parse_hhmm(start)
    cand = _dt.datetime.combine(when.date(), _dt.time(hour=sh, minute=sm))
    if cand <= when:
        cand += _dt.timedelta(days=1)
    return cand


# ---------------------------------------------------------------------------
# 事件队列
# ---------------------------------------------------------------------------
def build_events(targets: List[str],
                 campaign,
                 day: Optional[_dt.date] = None,
                 start_at: Optional[str] = DEFAULT_START_AT,
                 interval_min: float = 4.0,
                 interval_max: float = 6.0,
                 hook_delay_min: float = DEFAULT_HOOK_DELAY_MIN,
                 hook_days: Optional[List[str]] = None,
                 media_by_target: Optional[Dict[str, List[str]]] = None,
                 seed: Optional[str] = None,
                 now: Optional[_dt.datetime] = None,
                 enforce_window: bool = False,
                 window_start: Optional[str] = "08:00",
                 window_end: Optional[str] = "22:00") -> List[Dict[str, Any]]:
    """生成按时间排序的事件表。

    每个目标产生两类事件：
      * ``main`` —— 主文案（= 当日引导语 + 原文案），起点是 ``start_at``，
        各目标之间按 ``interval_min..interval_max`` 随机错开；
      * ``hook`` —— 资料钩子，时间 = 该目标主文案 + ``hook_delay_min`` 分钟，
        且**仅当当天在 hook_days 里**才生成。

    启用 ``enforce_window`` 时：起点若落在时段外会顺延到下一个时段起点；
    任何落在时段外的事件带 ``skip_reason`` 返回（调用方应过滤掉），
    而不是静默发送 —— 半夜发消息是最容易被投诉的一种失误。

    返回的事件都带绝对时间 ``at``（datetime），调用方按序执行即可。
    """
    day = day or (now or _dt.datetime.now()).date()
    now = now or _dt.datetime.now()
    media_by_target = media_by_target or {}

    events: List[Dict[str, Any]] = []
    if not targets:
        return events

    # 已经过了当天起点就顺延到今天之后的同一时刻（避免一启动就狂发）
    cursor = at_datetime(day, start_at)
    if cursor <= now:
        cursor = now + _dt.timedelta(seconds=20)
    if enforce_window:
        cursor = next_window_start(cursor, window_start, window_end)

    def _mark(ev: Dict[str, Any]) -> Dict[str, Any]:
        if enforce_window and not in_window(ev["at"], window_start, window_end):
            ev["skip_reason"] = ("超出允许发送时段 %s~%s"
                                 % (window_start, window_end))
        return ev

    rnd = random.Random("%s|stagger" % (seed or day.isoformat()))
    main_text = campaign.compose(day) if campaign else ""
    hook_text = campaign.hook(day) if campaign else None
    if hook_days is not None:
        allowed_hook = weekday_key(day) in [d.lower() for d in hook_days]
    else:
        allowed_hook = bool(hook_text)

    for i, tgt in enumerate(targets):
        at_main = cursor
        events.append(_mark({
            "at": at_main,
            "target": tgt,
            "kind": "main",
            "text": main_text,
            "media_path": (media_by_target.get(tgt) or [""])[0],
        }))
        if hook_text and allowed_hook:
            events.append(_mark({
                "at": at_main + _dt.timedelta(minutes=float(hook_delay_min)),
                "target": tgt,
                "kind": "hook",
                "text": hook_text,
                "media_path": "",
            }))
        cursor = cursor + _dt.timedelta(
            seconds=rnd.uniform(interval_min, interval_max) * 60.0)

    events.sort(key=lambda e: e["at"])
    return events


# ---------------------------------------------------------------------------
# 每群每周频次上限
# ---------------------------------------------------------------------------
def _state_path(data_dir: str) -> str:
    return os.path.join(data_dir, STATE_FILE)


def load_state(data_dir: str) -> Dict[str, Any]:
    try:
        with open(_state_path(data_dir), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(data_dir: str, state: Dict[str, Any]) -> bool:
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(_state_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def week_key(date: Optional[_dt.date] = None) -> str:
    """周标识，用「年-周数」；跨年时 ISO 周会变，所以带上年份。"""
    date = date or _dt.date.today()
    iso = date.isocalendar()
    return "%d-W%02d" % (iso[0], iso[1])


def day_key(date: Optional[_dt.date] = None) -> str:
    return (date or _dt.date.today()).isoformat()


def filter_by_cap(targets: List[str], data_dir: str,
                  cap: int, date: Optional[_dt.date] = None) -> List[str]:
    """按「每群每周上限」过滤目标；cap<=0 表示不限。

    保留这个函数是为了兼容既有调用；新代码请用 filter_by_caps（同时管日/周）。
    """
    return filter_by_caps(targets, data_dir, cap, 0, date)


def filter_by_caps(targets: List[str], data_dir: str,
                   weekly_cap: int = 0, daily_cap: int = 0,
                   date: Optional[_dt.date] = None) -> List[str]:
    """同时按「每群每周」与「每群每日」上限过滤；0 表示不限。

    每日上限的意义：计划可以一天跑好几次，或手滑点了两遍开始 ——
    每周上限拦不住这种重复，日上限能。
    """
    if weekly_cap <= 0 and daily_cap <= 0:
        return list(targets)
    state = load_state(data_dir)
    wk, dk = week_key(date), day_key(date)
    w_counts = (state.get("weekly") or {}).get(wk) or {}
    d_counts = (state.get("daily") or {}).get(dk) or {}
    out = []
    for t in targets:
        if weekly_cap > 0 and int(w_counts.get(t) or 0) >= weekly_cap:
            continue
        if daily_cap > 0 and int(d_counts.get(t) or 0) >= daily_cap:
            continue
        out.append(t)
    return out


def skip_reasons(targets: List[str], data_dir: str,
                 weekly_cap: int = 0, daily_cap: int = 0,
                 date: Optional[_dt.date] = None) -> Dict[str, str]:
    """给出每个被跳过的目标的原因（前端展示用，避免只报「跳过了」却不说是为什么）。"""
    state = load_state(data_dir)
    wk, dk = week_key(date), day_key(date)
    w_counts = (state.get("weekly") or {}).get(wk) or {}
    d_counts = (state.get("daily") or {}).get(dk) or {}
    out: Dict[str, str] = {}
    for t in targets:
        w = int(w_counts.get(t) or 0)
        d = int(d_counts.get(t) or 0)
        if weekly_cap > 0 and w >= weekly_cap:
            out[t] = "本周已发 %d 次，达到每周上限 %d" % (w, weekly_cap)
        elif daily_cap > 0 and d >= daily_cap:
            out[t] = "今天已发 %d 次，达到每日上限 %d" % (d, daily_cap)
    return out


def record_sent(target: str, data_dir: str,
                date: Optional[_dt.date] = None,
                kind: str = "main") -> None:
    """记一次投放，供每日/每周上限统计。

    只统计 ``kind == "main"``：上限约束的是「对这个群发了几轮」，
    钩子是该轮的附属动作，把它也计入会让上限提前触发。
    """
    if kind != "main":
        return
    state = load_state(data_dir)
    wk, dk = week_key(date), day_key(date)
    weekly = state.setdefault("weekly", {})
    daily = state.setdefault("daily", {})
    bucket_w = weekly.setdefault(wk, {})
    bucket_d = daily.setdefault(dk, {})
    bucket_w[target] = int(bucket_w.get(target) or 0) + 1
    bucket_d[target] = int(bucket_d.get(target) or 0) + 1
    # 各自只保留最近 8 个桶，避免文件无限膨胀
    for box in (weekly, daily):
        if len(box) > 8:
            for k in sorted(box.keys())[:-8]:
                box.pop(k, None)
    save_state(data_dir, state)


def cap_report(targets: List[str], data_dir: str,
               date: Optional[_dt.date] = None) -> Dict[str, Any]:
    """本周/今日已发次数。"""
    state = load_state(data_dir)
    wk, dk = week_key(date), day_key(date)
    return {
        "week": wk,
        "day": dk,
        "weekly": {t: int((((state.get("weekly") or {}).get(wk)) or {}).get(t) or 0)
                   for t in targets},
        "daily": {t: int((((state.get("daily") or {}).get(dk)) or {}).get(t) or 0)
                  for t in targets},
    }


def reset_day(data_dir: str, date: Optional[_dt.date] = None) -> None:
    state = load_state(data_dir)
    (state.get("daily") or {}).pop(day_key(date), None)
    save_state(data_dir, state)