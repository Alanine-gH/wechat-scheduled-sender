# -*- coding: utf-8 -*-
"""群/好友的投放状态：把「砍群建议」变成能执行的动作。

对应方案五、风控 4：
    连续两周主动私聊 <3 人的群，降低频率或退出。

之前 report.py 只给出 cut_candidates「建议」，用户还得自己记住哪个群该少发 ——
记不住的。这里给每个目标一个可持久化的状态，计划生成时直接生效。

三档：
* ``active``  —— 正常，每天 1 条主文案 + 钩子
* ``reduced`` —— 降频：**只在单数日发**（隔天），发量减半但仍保持存在感
* ``paused``  —— 暂停：完全不发（等于退出这个群）

为什么降频用「隔天」而不是「每周几天」：
方案里要的是「降低频率」，隔天是最小改动且用户一眼能懂；
按周几发会牵扯到与 run_days / hook_days 的叠加，条件一多就容易算错。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

STATE_FILE = "target_state.json"

MODE_ACTIVE = "active"
MODE_REDUCED = "reduced"
MODE_PAUSED = "paused"
MODES = (MODE_ACTIVE, MODE_REDUCED, MODE_PAUSED)
MODE_CN = {MODE_ACTIVE: "正常", MODE_REDUCED: "降频（隔天）", MODE_PAUSED: "暂停"}


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, STATE_FILE)


def load(data_dir: str) -> Dict[str, Any]:
    try:
        with open(_path(data_dir), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        d = {}
    d.setdefault("targets", {})
    return d


def save(data_dir: str, d: Dict[str, Any]) -> None:
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_mode(data_dir: str, target: str) -> str:
    rec = (load(data_dir)["targets"] or {}).get(target) or {}
    m = rec.get("mode") or MODE_ACTIVE
    return m if m in MODES else MODE_ACTIVE


def set_mode(data_dir: str, targets: List[str], mode: str,
             reason: str = "") -> int:
    """批量设置状态，返回被改的目标数。"""
    if mode not in MODES:
        return 0
    d = load(data_dir)
    box = d["targets"]
    n = 0
    for t in targets or []:
        if not t:
            continue
        if mode == MODE_ACTIVE:
            # 恢复正常 = 直接删掉记录，别留一堆 active 占地方
            if t in box:
                box.pop(t, None)
                n += 1
            continue
        box[t] = {"mode": mode, "reason": reason[:200],
                  "since": int(time.time())}
        n += 1
    save(data_dir, d)
    return n


def should_send(data_dir: str, target: str,
                date=None) -> Tuple[bool, str]:
    """该目标今天该不该发。返回 (是否发, 原因)。"""
    import datetime as _dt
    date = date or _dt.date.today()
    mode = get_mode(data_dir, target)
    if mode == MODE_PAUSED:
        return False, "已暂停（不再向该群投放）"
    if mode == MODE_REDUCED:
        # 单数日发、双数日跳过 —— 发量减半
        if date.day % 2 == 0:
            return False, "降频中：双数日不投放"
        return True, "降频中：单数日照常投放"
    return True, ""


def filter_targets(data_dir: str, targets: List[str],
                   date=None) -> Tuple[List[str], Dict[str, str]]:
    """过滤掉今天不该发的目标，并给出原因表。"""
    allowed: List[str] = []
    skipped: Dict[str, str] = {}
    for t in targets or []:
        ok, why = should_send(data_dir, t, date)
        if ok:
            allowed.append(t)
        else:
            skipped[t] = why
    return allowed, skipped


def describe(data_dir: str, targets: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """给前端：目标 + 状态（只列非 active 的，除非显式给了名单）。"""
    d = load(data_dir)
    box = d["targets"] or {}
    names = set(box.keys())
    if targets:
        names |= set(targets)
    out: List[Dict[str, Any]] = []
    for name in sorted(names):
        rec = box.get(name) or {}
        mode = rec.get("mode") or MODE_ACTIVE
        out.append({
            "target": name,
            "mode": mode,
            "mode_cn": MODE_CN.get(mode, mode),
            "reason": rec.get("reason") or "",
            "since": rec.get("since") or 0,
            "since_text": time.strftime("%Y-%m-%d", time.localtime(rec["since"]))
                          if rec.get("since") else "",
        })
    return out


def apply_cut_candidates(data_dir: str, candidates: List[Dict[str, Any]],
                         mode: str = MODE_REDUCED) -> int:
    """把报告里的「砍群建议」批量落成状态（默认降频，不直接暂停）。"""
    names = [c.get("target") for c in candidates or [] if c.get("target")]
    reasons = {c.get("target"): (c.get("hint") or "") for c in candidates or []}
    d = load(data_dir)
    box = d["targets"]
    n = 0
    for name in names:
        box[name] = {"mode": mode, "reason": reasons.get(name, "")[:200],
                     "since": int(time.time())}
        n += 1
    save(data_dir, d)
    return n