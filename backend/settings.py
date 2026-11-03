# -*- coding: utf-8 -*-
"""统一配置中心：所有可调行为集中在这里，带默认值、范围校验与前端表单描述。

为什么单独做一层（而不是继续往 settings.json 里随手塞）：
配置一多就会失控 —— 有的键在 server.py 里读、有的在 scheduler.py 里读，
默认值分散、校验各写一遍、改了不知道影响谁。
这里把「有什么设置、默认多少、合法范围、界面上叫什么」全部声明在一处，
其余模块只按 key 取值。

分层：``DEFAULTS`` 给代码用（取值快），``SCHEMA`` 给界面用（渲染表单 + 校验）。
两者用同一份 key，测试里会校验它们不漂移。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

SETTINGS_FILE = "settings.json"

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# ---------------------------------------------------------------------------
# 默认值（代码取值用）
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    # 内容
    "message_mode": "line",              # line | separator
    "use_campaign": False,

    # 调度
    "start_at": "20:00",
    "hook_delay_minutes": 30.0,
    "hook_days": ["tue", "thu", "sat"],
    "run_days": [],                      # 空 = 每天都允许

    # 发送时段：超出这个区间不发（半夜发消息最容易被投诉）
    "send_window_start": "08:00",
    "send_window_end": "22:00",
    "enforce_window": False,

    # 限额
    "weekly_cap": 0,                     # 每群每周上限，0=不限
    "daily_cap": 0,                      # 每群每日上限，0=不限
    "run_cap": 0,                        # 单次运行总条数上限，0=不限
    "max_fail_streak": 3,                # 连续失败 N 条即中止，0=不中止

    # 节奏
    "interval_min": 3.5,                 # 相邻两条间隔下限（分钟）
    "interval_max": 6.5,                 # 相邻两条间隔上限（分钟）
    "cooldown_every": 0,                 # 每发 N 条强制休息一次，0=关
    "cooldown_seconds": 60,              # 休息时长（秒）

    # 可靠性
    "retry_times": 1,                    # 单条失败重试次数，0=不重试
    "retry_delay_seconds": 3,
    "verify_timeout": 8,                 # 回读校验最长等待（秒）
    "clear_input_before": True,          # 发送前清空输入框

    # 自动运行
    "auto_enabled": False,               # 到点自动执行
    "auto_check_seconds": 60,            # 守护线程检查间隔

    # 接收
    "inbox_auto_poll": False,            # 自动轮询私聊
    "inbox_poll_seconds": 300,
    "keywords_need": ["客户", "收购", "发票", "台账", "备案", "自产", "承包",
                      "合作社", "税负", "个体户", "一般纳税人", "反向开票",
                      "收储", "经纪人", "账务", "企业", "公司", "加工厂",
                      "贸易", "代账", "事务所", "分润", "合作"],
    "keywords_hook": ["资料", "清单", "模板", "表格", "pdf", "发我", "要一份",
                      "来一份", "怎么领", "领取"],

    # 清理
    "log_keep_days": 30,
    "shot_keep_days": 30,
}

# ---------------------------------------------------------------------------
# 表单描述（界面渲染 + 校验用）
# 每项：key / 分组 / 标签 / 类型 / 最小 / 最大 / 说明
# ---------------------------------------------------------------------------
SCHEMA: List[Dict[str, Any]] = [
    # ── 调度 ──
    {"key": "start_at", "group": "调度", "label": "每天起始时间", "type": "time",
     "help": "内容编排模式的第一个事件时间"},
    {"key": "hook_delay_minutes", "group": "调度", "label": "钩子延迟（分钟）",
     "type": "number", "min": 0, "max": 1440, "step": 5,
     "help": "主文案发出后多久补发资料钩子"},
    {"key": "hook_days", "group": "调度", "label": "钩子投放日", "type": "weekdays",
     "help": "只在选中的星期投钩子，其余日子只发主文案"},
    {"key": "run_days", "group": "调度", "label": "允许投放的星期", "type": "weekdays",
     "help": "留空表示每天都允许；选中后不在名单里的日子会跳过"},

    # ── 发送时段 ──
    {"key": "enforce_window", "group": "发送时段", "label": "启用发送时段限制",
     "type": "bool", "help": "开启后只在下面的时间段内发送"},
    {"key": "send_window_start", "group": "发送时段", "label": "时段起", "type": "time"},
    {"key": "send_window_end", "group": "发送时段", "label": "时段止", "type": "time",
     "help": "跨零点也支持（如 22:00 ~ 06:00）"},

    # ── 限额 ──
    {"key": "weekly_cap", "group": "限额", "label": "每群每周上限", "type": "number",
     "min": 0, "max": 999, "help": "0 = 不限；达到上限的群本轮自动跳过"},
    {"key": "daily_cap", "group": "限额", "label": "每群每日上限", "type": "number",
     "min": 0, "max": 999, "help": "0 = 不限；防手滑重复跑"},
    {"key": "run_cap", "group": "限额", "label": "单次运行上限（条）", "type": "number",
     "min": 0, "max": 9999, "help": "0 = 不限；一次最多发多少条"},
    {"key": "max_fail_streak", "group": "限额", "label": "连续失败即中止", "type": "number",
     "min": 0, "max": 99, "help": "连续失败达到该条数就停下来（0 = 不中止）"},

    # ── 节奏 ──
    {"key": "interval_min", "group": "节奏", "label": "间隔下限（分钟）", "type": "number",
     "min": 0.1, "max": 600, "step": 0.1, "help": "相邻两条之间随机等待的下限"},
    {"key": "interval_max", "group": "节奏", "label": "间隔上限（分钟）", "type": "number",
     "min": 0.1, "max": 600, "step": 0.1},
    {"key": "cooldown_every", "group": "节奏", "label": "每 N 条休息一次", "type": "number",
     "min": 0, "max": 999, "help": "一口气连发几十条最像机器人，0 = 关闭"},
    {"key": "cooldown_seconds", "group": "节奏", "label": "休息时长（秒）", "type": "number",
     "min": 0, "max": 3600},

    # ── 可靠性 ──
    {"key": "retry_times", "group": "可靠性", "label": "单条重试次数", "type": "number",
     "min": 0, "max": 5, "help": "打开会话失败时重试几次，0 = 不重试"},
    {"key": "retry_delay_seconds", "group": "可靠性", "label": "重试间隔（秒）",
     "type": "number", "min": 1, "max": 60},
    {"key": "verify_timeout", "group": "可靠性", "label": "回读校验超时（秒）",
     "type": "number", "min": 1, "max": 60, "help": "发送后等数据库落库的最长时间"},
    {"key": "clear_input_before", "group": "可靠性", "label": "发送前清空输入框",
     "type": "bool", "help": "避免上一条的残留和这一条混在一起"},

    # ── 自动运行 ──
    {"key": "auto_enabled", "group": "自动运行", "label": "启用自动运行",
     "type": "bool", "help": "程序常驻时，到点自动按计划执行，不用每天手动点"},
    {"key": "auto_check_seconds", "group": "自动运行", "label": "检查间隔（秒）",
     "type": "number", "min": 10, "max": 600},

    # ── 接收 ──
    {"key": "inbox_auto_poll", "group": "接收", "label": "自动轮询私聊",
     "type": "bool", "help": "定时读取数据库里新收到的私聊消息"},
    {"key": "inbox_poll_seconds", "group": "接收", "label": "轮询间隔（秒）",
     "type": "number", "min": 30, "max": 3600},
    {"key": "keywords_need", "group": "接收", "label": "高意向关键词", "type": "list",
     "help": "命中即判为「有具体需求」，当天该转组长"},
    {"key": "keywords_hook", "group": "接收", "label": "低意向关键词", "type": "list",
     "help": "命中即判为「仅领资料」"},

    # ── 清理 ──
    {"key": "log_keep_days", "group": "清理", "label": "日志保留天数", "type": "number",
     "min": 1, "max": 3650},
    {"key": "shot_keep_days", "group": "清理", "label": "截图保留天数", "type": "number",
     "min": 1, "max": 3650},
]

SCHEMA_KEYS = {s["key"] for s in SCHEMA}


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _path(data_dir: str) -> str:
    return os.path.join(data_dir, SETTINGS_FILE)


def load(data_dir: str) -> Dict[str, Any]:
    """读配置：以默认值为底，用文件里的值覆盖。

    这样新增设置项时老配置文件不会缺键，取值处也不必到处写 `or 默认值`。
    """
    out = dict(DEFAULTS)
    try:
        with open(_path(data_dir), "r", encoding="utf-8") as f:
            saved = json.load(f) or {}
    except Exception:
        saved = {}
    for k, v in saved.items():
        if k in DEFAULTS:
            out[k] = v
    return out


def get(data_dir: str, key: str, default: Any = None) -> Any:
    return load(data_dir).get(key, DEFAULTS.get(key, default))


def coerce(key: str, raw: Any) -> Tuple[bool, Any, str]:
    """把一个原始输入转成合法值。返回 (是否合法, 值, 原因)。"""
    spec = next((s for s in SCHEMA if s["key"] == key), None)
    if spec is None:
        return False, None, "未知设置项 %s" % key
    t = spec.get("type")
    if t == "bool":
        if isinstance(raw, bool):
            return True, raw, ""
        return True, str(raw).lower() in ("1", "true", "yes", "on"), ""
    if t == "number":
        try:
            v = float(raw)
        except Exception:
            return False, None, "%s 需要是数字" % spec.get("label", key)
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:
            return False, None, "%s 不能小于 %s" % (spec.get("label", key), lo)
        if hi is not None and v > hi:
            return False, None, "%s 不能大于 %s" % (spec.get("label", key), hi)
        # 整数字段存整数，别把 30 存成 30.0
        if spec.get("step") is None and v == int(v):
            return True, int(v), ""
        return True, v, ""
    if t == "weekdays":
        vals = raw if isinstance(raw, (list, tuple)) else [raw]
        days = [str(x).lower() for x in vals if str(x).lower() in WEEKDAY_KEYS]
        return True, days, ""
    if t == "list":
        if isinstance(raw, (list, tuple)):
            vals = [str(x).strip() for x in raw]
        else:
            vals = [x.strip() for x in str(raw or "").split("\n")]
        return True, [x for x in vals if x], ""
    # time 与其它字符串型
    s = str(raw or "").strip()
    if t == "time":
        parts = s.split(":")
        try:
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            return False, None, "%s 需要是 HH:MM 格式" % spec.get("label", key)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return False, None, "%s 不是合法时间" % spec.get("label", key)
        return True, "%02d:%02d" % (h, m), ""
    return True, s, ""


def save(data_dir: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并保存，逐项校验。返回 ``{ok, saved, errors, settings}``。

    只接受 SCHEMA 里声明过的键 —— 防止拼错的键悄悄写进配置文件
    （写进去不会报错，但永远不生效，最难查）。
    """
    cur = load(data_dir)
    saved: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for k, v in (patch or {}).items():
        if k not in DEFAULTS:
            errors[k] = "未知设置项"
            continue
        if k not in SCHEMA_KEYS:
            # 有默认值但没进表单的（如 message_mode），直接原样接受
            cur[k] = v
            saved[k] = v
            continue
        ok, val, why = coerce(k, v)
        if not ok:
            errors[k] = why
            continue
        cur[k] = val
        saved[k] = val

    os.makedirs(data_dir, exist_ok=True)
    try:
        with open(_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        return {"ok": False, "saved": {}, "errors": {"_io": str(exc)}, "settings": cur}
    return {"ok": not errors, "saved": saved, "errors": errors, "settings": cur}


def reset(data_dir: str, keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """恢复默认值；``keys`` 为空则全部恢复。"""
    cur = load(data_dir)
    if not keys:
        cur = dict(DEFAULTS)
    else:
        for k in keys:
            if k in DEFAULTS:
                cur[k] = DEFAULTS[k]
    os.makedirs(data_dir, exist_ok=True)
    with open(_path(data_dir), "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return cur


def describe(data_dir: str) -> Dict[str, Any]:
    """给前端：当前值 + 表单描述（按组分好）。"""
    cur = load(data_dir)
    groups: List[Dict[str, Any]] = []
    for s in SCHEMA:
        g = s.get("group") or "其它"
        bucket = next((x for x in groups if x["name"] == g), None)
        if bucket is None:
            bucket = {"name": g, "items": []}
            groups.append(bucket)
        item = dict(s)
        item["value"] = cur.get(s["key"], DEFAULTS.get(s["key"]))
        bucket["items"].append(item)
    return {"settings": cur, "groups": groups, "defaults": dict(DEFAULTS)}