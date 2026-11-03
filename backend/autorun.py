# -*- coding: utf-8 -*-
"""自动运行守护：到点自动按计划执行，不用每天手动点「开始发送」。

为什么这块是 P0：
运营方案要求「每天 1 条，固定晚 8 点发」——人不可能天天坐在电脑前点一次。
没有守护，这套方案在第三天就断了。

职责边界（单一职责，不碰发送实现）：
本模块只负责「**现在该不该跑**」和「**跑过没有**」，
真正的建计划与发送由调用方通过 ``run_fn`` 注入。
这样它不依赖 server.py，也不产生循环依赖，可以单独测试。

状态文件 ``data/auto_task.json``：保存自动任务的目标名单 + 最近一次执行日。
「按天去重」存**日期**而不是时间戳：判断的是「今天跑过没有」，
存日期最直观，也避免了时区/夏令时带来的边界问题。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

TASK_FILE = "auto_task.json"

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, TASK_FILE)


def load_task(data_dir: str) -> Dict[str, Any]:
    """自动任务状态：目标名单 + 最近执行日 + 最近一次结果。"""
    out: Dict[str, Any] = {"targets": [], "last_run_day": "", "last_run_at": 0,
                           "last_result": "", "last_error": ""}
    try:
        with open(_path(data_dir), "r", encoding="utf-8") as f:
            saved = json.load(f) or {}
        for k in out:
            if k in saved:
                out[k] = saved[k]
    except Exception:
        pass
    return out


def save_task(data_dir: str, **patch) -> Dict[str, Any]:
    cur = load_task(data_dir)
    cur.update({k: v for k, v in patch.items() if k in cur})
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cur


# ---------------------------------------------------------------------------
# 该不该跑
# ---------------------------------------------------------------------------
def _parse_hhmm(s: Optional[str], default: str = "20:00") -> Tuple[int, int]:
    try:
        parts = str(s or default).split(":")
        h, m = int(parts[0]), (int(parts[1]) if len(parts) > 1 else 0)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return 20, 0


def _in_window(when: _dt.datetime, start: str, end: str) -> bool:
    """与 scheduler.in_window 同一套语义（含跨零点）。

    这里独立实现一份，是为了让本模块不依赖 scheduler —— 它只判断
    「现在能不能发」，不需要计划生成能力。
    """
    if not start or not end or start == end:
        return True
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    lo, hi = sh * 60 + sm, eh * 60 + em
    cur = when.hour * 60 + when.minute
    if lo < hi:
        return lo <= cur < hi
    return cur >= lo or cur < hi


def due(now: Optional[_dt.datetime] = None,
        cfg: Optional[Dict[str, Any]] = None,
        task: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """判断此刻是否该自动执行。

    返回 ``(是否该跑, 原因)``。原因无论该不该跑都给 ——
    界面上要能回答「为什么它没跑」，否则用户只能干等。
    """
    now = now or _dt.datetime.now()
    cfg = cfg or {}
    task = task or {}

    if not cfg.get("auto_enabled"):
        return False, "未启用自动运行"
    if not (task.get("targets") or []):
        return False, "自动任务还没有保存目标名单"

    today = now.date().isoformat()
    if task.get("last_run_day") == today:
        return False, "今天已经自动跑过了"

    run_days = [str(d).lower() for d in (cfg.get("run_days") or [])]
    wk = WEEKDAY_KEYS[now.weekday()]
    if run_days and wk not in run_days:
        return False, "今天（%s）不在允许投放的星期" % wk

    h, m = _parse_hhmm(cfg.get("start_at"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < target:
        return False, "还没到 %02d:%02d" % (h, m)

    grace = int(cfg.get("auto_grace_minutes") or 0)
    late_min = (now - target).total_seconds() / 60.0
    if grace and late_min > grace:
        return False, ("已过 %02d:%02d 有 %.0f 分钟，超过补跑宽限 %d 分钟，"
                       "跳过今天" % (h, m, late_min, grace))

    if cfg.get("enforce_window") and not _in_window(
            now, cfg.get("send_window_start"), cfg.get("send_window_end")):
        return False, ("当前 %s 不在允许发送时段 %s~%s"
                       % (now.strftime("%H:%M"), cfg.get("send_window_start"),
                          cfg.get("send_window_end")))

    return True, "已到 %02d:%02d，可以执行" % (h, m)


# ---------------------------------------------------------------------------
# 守护线程
# ---------------------------------------------------------------------------
class AutoRunner:
    """常驻守护：按 ``auto_check_seconds`` 轮询，到点调用 ``run_fn``。

    ``run_fn`` 由调用方注入（通常是 server 的「建计划并执行」），
    返回 True 表示已触发。守护本身不做任何发送。
    """

    def __init__(self, data_dir: str, cfg_loader: Callable[[], Dict[str, Any]],
                 run_fn: Callable[[Dict[str, Any]], bool],
                 log: Optional[Callable[[str, str], None]] = None,
                 is_busy: Optional[Callable[[], bool]] = None) -> None:
        self.data_dir = data_dir
        self._cfg = cfg_loader
        self._run = run_fn
        self._log = log or (lambda lvl, msg: None)
        self._busy = is_busy or (lambda: False)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_reason = "尚未检查"
        self.last_check = 0.0
        self.fire_count = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="auto-runner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        # 启动先等一小会：让 server 把窗口/引擎准备好，避免开机瞬间抢资源
        if self._stop.wait(15):
            return
        while not self._stop.is_set():
            try:
                cfg = self._cfg() or {}
                task = load_task(self.data_dir)
                ok, reason = due(cfg=cfg, task=task)
                self.last_reason = reason
                self.last_check = time.time()
                if not ok:
                    self._stop.wait(5)
                    continue
                if self._busy():
                    self.last_reason = "上一次任务还在执行，本轮跳过"
                    self._stop.wait(5)
                    continue
                self._log("INFO", "自动运行：%s" % reason)
                # 先记执行日再真正跑：万一跑到一半程序被关掉，
                # 重启后不应该在同一天再补一轮（重复发送比漏发严重）
                save_task(self.data_dir, last_run_day=_dt.date.today().isoformat(),
                          last_run_at=int(time.time()), last_result="running")
                fired = False
                try:
                    fired = bool(self._run(task))
                except Exception as exc:
                    save_task(self.data_dir, last_result="error",
                              last_error=str(exc))
                    self._log("ERROR", "自动运行执行失败：%s" % exc)
                else:
                    save_task(self.data_dir,
                              last_result="ok" if fired else "skipped",
                              last_error="")
                if fired:
                    self.fire_count += 1
            except Exception as exc:
                self._log("ERROR", "自动运行守护异常：%s" % exc)

            # 用可中断的等待，保证 stop() 能及时生效
            try:
                wait_s = float((self._cfg() or {}).get("auto_check_seconds") or 60)
            except Exception:
                wait_s = 60.0
            self._stop.wait(max(5.0, min(600.0, wait_s)))

    def status(self) -> Dict[str, Any]:
        task = load_task(self.data_dir)
        cfg = self._cfg() or {}
        ok, reason = due(cfg=cfg, task=task)
        return {
            "thread_running": self.running,
            "enabled": bool(cfg.get("auto_enabled")),
            "due_now": ok,
            "reason": reason,
            "last_check": self.last_check,
            "fire_count": self.fire_count,
            "targets": task.get("targets") or [],
            "last_run_day": task.get("last_run_day") or "",
            "last_run_at": task.get("last_run_at") or 0,
            "last_result": task.get("last_result") or "",
            "last_error": task.get("last_error") or "",
            "start_at": cfg.get("start_at"),
            "grace_minutes": cfg.get("auto_grace_minutes"),
        }