# -*- coding: utf-8 -*-
"""内容编排：按星期取引导语、与正文拼成一条、按星期投钩子。

对应运营方案（粮食反向开票业务·财税广告群营销方案）：

* **主投放**：每天 1 条，固定时间发。内容 = 当日引导语 + 原文案，
  原文案一字不改。引导语按周一~周日轮换，不重复。
* **资料钩子**：文案发出间隔约 30 分钟后补发一条，给群友一个低门槛的
  主动私聊理由。频率每周 2-3 次，固定在周二 / 周四 / 周六，避免刷屏被踢。

内容文件格式（``data/campaign.txt``，用户可直接编辑）::

    # === 引导语 ===
    [mon] 周一那句
    [tue] 周二那句
    ...
    [sun] 周日那句

    # === 正文 ===
    原文案，可以多行
    整块原样使用，不做任何改写

    # === 钩子 ===
    钩子文案，可以多段，空行分隔

设计取舍：内容用「章节 + 星期标签」的纯文本格式，而不是 JSON/YAML ——
运营要天天改文案，纯文本最不容易改坏，也不引入额外依赖。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 星期键（与 datetime.weekday() 顺序一致：0=周一）
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 钩子默认只在周二/周四/周六投（方案 2.2 节）
DEFAULT_HOOK_DAYS = ("tue", "thu", "sat")

SECTION_RE = re.compile(r"^\s*#\s*===\s*(.+?)\s*===\s*$")
WEEKDAY_LINE_RE = re.compile(r"^\s*\[(\w+)\]\s*(.*)$")

SECTION_GUIDE = "引导语"
SECTION_BODY = "正文"
SECTION_HOOK = "钩子"

# 章节名的几种常见写法都认，免得用户改个小标题就解析不到
SECTION_ALIASES = {
    "引导语": SECTION_GUIDE, "引导词": SECTION_GUIDE, "guide": SECTION_GUIDE,
    "话题": SECTION_GUIDE, "话题引导语": SECTION_GUIDE,
    "正文": SECTION_BODY, "原文案": SECTION_BODY, "body": SECTION_BODY,
    "主文案": SECTION_BODY, "文案": SECTION_BODY,
    "钩子": SECTION_HOOK, "资料钩子": SECTION_HOOK, "hook": SECTION_HOOK,
}


def weekday_key(date: Optional[_dt.date] = None) -> str:
    """返回日期对应的星期键（mon..sun）。"""
    date = date or _dt.date.today()
    return WEEKDAY_KEYS[date.weekday()]


def weekday_cn(key: str) -> str:
    try:
        return WEEKDAY_CN[WEEKDAY_KEYS.index(key)]
    except Exception:
        return key


@dataclass
class Campaign:
    """一份内容编排。"""

    guides: Dict[str, str] = field(default_factory=dict)   # mon..sun -> 引导语
    body: str = ""                                         # 原文案（整块）
    hooks: List[str] = field(default_factory=list)         # 钩子文案
    hook_days: List[str] = field(default_factory=lambda: list(DEFAULT_HOOK_DAYS))

    def compose(self, date: Optional[_dt.date] = None,
                with_guide: bool = True) -> str:
        """拼出当天要发的主文案：引导语 + 原文案。

        ``with_guide=False`` 时只返回原文案（方案要求原文案一字不改，
        引导语是「上方加一句」，所以这里用换行拼接，不改动原文案本身）。
        """
        date = date or _dt.date.today()
        parts: List[str] = []
        if with_guide:
            g = (self.guides.get(weekday_key(date)) or "").strip()
            if g:
                parts.append(g)
        b = (self.body or "").strip("\n")
        if b:
            parts.append(b)
        return "\n".join(parts)

    def hook(self, date: Optional[_dt.date] = None,
             index: Optional[int] = None) -> Optional[str]:
        """取当天的钩子文案；当天不在投放日或没有钩子时返回 None。

        ``index`` 用于同一天多次投钩子时轮换不同的钩子（默认按年内第几周轮换）。
        """
        date = date or _dt.date.today()
        if weekday_key(date) not in self.hook_days:
            return None
        if not self.hooks:
            return None
        if index is None:
            index = date.isocalendar()[1]      # 按周轮换，同一天结果稳定
        return self.hooks[index % len(self.hooks)]

    def guide_of(self, date: Optional[_dt.date] = None) -> str:
        return (self.guides.get(weekday_key(date or _dt.date.today())) or "").strip()

    def to_dict(self) -> Dict[str, object]:
        return {
            "guides": dict(self.guides),
            "guide_count": len([v for v in self.guides.values() if v.strip()]),
            "body_lines": len([x for x in self.body.split("\n") if x.strip()]),
            "hooks": len(self.hooks),
            "hook_days": list(self.hook_days),
        }


def parse_campaign(text: str) -> Campaign:
    """解析内容文件。章节按 ``# === 名称 ===`` 切分。"""
    camp = Campaign()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    section: Optional[str] = None
    hook_buf: List[str] = []
    body_buf: List[str] = []

    def flush_hook():
        block = "\n".join(hook_buf).strip("\n")
        if block.strip():
            camp.hooks.append(block)
        hook_buf.clear()

    for ln in lines:
        m = SECTION_RE.match(ln)
        if m:
            # 切换章节前先把上一段钩子收尾
            if section == SECTION_HOOK:
                flush_hook()
            raw = m.group(1).strip()
            section = SECTION_ALIASES.get(raw.lower(), SECTION_ALIASES.get(raw))
            continue

        if section == SECTION_GUIDE:
            wm = WEEKDAY_LINE_RE.match(ln)
            if wm:
                key = wm.group(1).lower()
                if key in WEEKDAY_KEYS:
                    camp.guides[key] = wm.group(2).strip()
            # 章节内的其它行（说明/注释）忽略
        elif section == SECTION_BODY:
            body_buf.append(ln)
        elif section == SECTION_HOOK:
            # 空行分段；--- 也可分段
            if not ln.strip() or ln.strip() == "---":
                flush_hook()
            else:
                hook_buf.append(ln)

    if section == SECTION_HOOK:
        flush_hook()

    # 正文去掉首尾空行；中间空行保留（原文案可能自带分段）
    camp.body = "\n".join(body_buf).strip("\n")
    return camp


def load_campaign(path: str) -> Campaign:
    """从文件读取；文件不存在时返回空 Campaign（不抛异常）。"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return parse_campaign(f.read())
    except FileNotFoundError:
        return Campaign()
    except Exception:
        return Campaign()


def load_campaign_detail(path: str) -> Dict[str, object]:
    """给前端：内容概览 + 当天拼出来的效果预览。"""
    camp = load_campaign(path)
    today = _dt.date.today()
    return {
        "campaign": camp.to_dict(),
        "today": weekday_key(today),
        "today_cn": weekday_cn(weekday_key(today)),
        "main_preview": camp.compose(today),
        "hook_preview": camp.hook(today) or "",
        "hook_today": weekday_key(today) in camp.hook_days,
    }