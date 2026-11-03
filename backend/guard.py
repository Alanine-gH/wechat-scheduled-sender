# -*- coding: utf-8 -*-
"""合规词校验：发送前拦住会暗示「违规开票」的措辞。

对应方案五、风控 1：

    群内任何内容不得出现「粮贩子也能开」「经纪人可以操作」等暗示。
    你的差异化恰恰是「只做合规」。违规暗示不仅吸引劣质客户，
    还可能被群内同行截图举报。

分三档：

* ``block`` —— 明确违规暗示，**直接不发**（宁可少发一条，也不能留把柄）
* ``warn``  —— 措辞有风险、可能被误读，发但在界面上提示
* ``pass``  —— 无命中

设计取舍：宁可多拦几个误报，也不能漏。拦下来的内容会带原因回给界面，
用户能看懂是哪个词触发的、要不要改；发不出去的损失远小于被举报的损失。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# 明确违规：一旦命中就不发
# ---------------------------------------------------------------------------
BLOCK_WORDS = (
    # 向非自产方开票（粮食场景的核心红线：仅限自产农户）
    "粮贩子也能开", "粮贩也能开", "贩子也能开", "经纪人可以操作",
    "经纪人能开", "给经纪人开", "开给经纪人", "向经纪人开票",
    "粮贩子可开", "谁都能开", "不看自产",
    # 票货分离 / 无真实交易
    "票货分离", "无票货", "空开", "只开票不发货", "纯开票", "光开票",
    # 虚开 / 代开 / 买卖发票
    "虚开", "代开发票", "发票代开", "买卖发票", "买发票", "卖发票",
    "过票", "洗票", "资金回流",
    # 保证结果 / 内部关系
    "包过", "保过", "保证通过", "一定能开", "百分百通过", "稳过",
    "内部关系", "找关系", "走后门", "内部渠道", "有门路",
    # 规避监管
    "不会被查", "查不到", "规避稽查", "躲过稽查", "不用备案", "无需备案",
    "不用建账", "无需台账", "绕开监管",
)

# ---------------------------------------------------------------------------
# 风险措辞：发但提示
# ---------------------------------------------------------------------------
# 刻意**不用单个汉字**（如「包」）：粮食业务里「土地承包合同」「分包」
# 都是正规术语，用单字判会把合规内容刷成一堆警告 ——
# 警告一旦误报到这种程度，用户就会开始忽略它，等于没有。
WARN_WORDS = (
    "一定可以", "一定能开", "一定能办", "绝对可以", "绝对没问题",
    "绝对能办", "百分百", "100%", "保过", "包过", "保证能",
    "内部价", "内部指标", "灵活处理", "变通处理", "特殊渠道",
    "特殊办法", "不查", "没人管", "放心开", "放心办", "稳妥办理",
    "特殊操作",
)
# 注意：不要在这里重复 BLOCK_WORDS 里的词（如「内部渠道」「找关系」）——
# 两边都有时以 BLOCK 为准，重复写只会让人误以为它是轻量的风险词。

# 词后面紧跟这些字时，属于正常业务用语而非承诺，降级为 warn
# （例如「一定注意」不是承诺，「台账规范」不是违规）
_SOFTENERS = ("注意", "记得", "要", "需要", "应该", "及时", "规范", "按照",
              "依据", "政策", "规定")

# 合规语境标志：命中这些说明句子里是在「指出某事违规/不可行」，
# 而不是在「承诺自己能办」。方案 2.3 节恰恰要求公开回复时带政策依据
# （原话举例：「向经纪人开票属于票货分离，金税四期资金穿透比对直接预警」）——
# 不区分语境就会把用户自己的合规话术拦下来，那是帮倒忙。
COMPLIANCE_MARKERS = (
    "违规", "违法", "不合法", "不得", "禁止", "严禁", "不能", "不可",
    "切勿", "不要", "不许", "红线", "即属于", "属于违", "行不通",
    "不可行", "风险极高", "预警", "稽查", "核查", "查处", "处罚",
    "定性", "留存", "留存期", "必须", "应当", "要求",
)

# 句子切分：中英文句读与换行
_SENTENCE_RE = re.compile(r"[。！？!?；;\n]")


def _sentence_of(text: str, pos: int) -> str:
    """取 pos 所在的句子（用于判断该词出现的语境）。"""
    start = 0
    for m in _SENTENCE_RE.finditer(text):
        if m.end() > pos:
            return text[start:m.start() + 1]
        start = m.end()
    return text[start:]


def _in_compliance_context(text: str, word: str) -> bool:
    """该违规词是否出现在「指出其违规」的语境里。"""
    pos = text.find(word)
    if pos < 0:
        return False
    sent = _sentence_of(text, pos)
    return any(mk in sent for mk in COMPLIANCE_MARKERS)


def _level_hits(text: str) -> Dict[str, List[str]]:
    t = text or ""
    block = [w for w in BLOCK_WORDS
             if w in t and not _in_compliance_context(t, w)]
    warn: List[str] = []
    for w in WARN_WORDS:
        if w not in t:
            continue
        # 看词后面 3 个字里有没有"软化词"，有就不算承诺
        idx = t.find(w)
        tail = t[idx + len(w): idx + len(w) + 3]
        if any(s in tail for s in _SOFTENERS):
            continue
        warn.append(w)
    return {"block": block, "warn": warn}


def check(text: str) -> Dict[str, Any]:
    """校验一段文本。返回 ``{ok, level, hits, reason}``。"""
    hits = _level_hits(text)
    if hits["block"]:
        return {
            "ok": False,
            "level": "block",
            "hits": hits["block"],
            "reason": "命中违规措辞：%s —— 方案风控要求群内不得出现此类暗示，已拦下不发"
                      % "、".join(hits["block"]),
        }
    if hits["warn"]:
        return {
            "ok": True,
            "level": "warn",
            "hits": hits["warn"],
            "reason": "措辞有风险（%s），建议改成更中性的说法" % "、".join(hits["warn"]),
        }
    return {"ok": True, "level": "pass", "hits": [], "reason": ""}


def check_many(items) -> Dict[str, Any]:
    """批量校验。

    ``items`` 可以是 ``[text, ...]`` 或 ``[(label, text), ...]``。
    返回 ``{ok, blocked: [...], warned: [...], checked: n}``；
    ``ok=False`` 表示存在被拦下的内容（调用方应阻止本次发送）。
    """
    blocked: List[Dict[str, Any]] = []
    warned: List[Dict[str, Any]] = []
    n = 0
    for it in items or []:
        if isinstance(it, (tuple, list)) and len(it) >= 2:
            label, text = it[0], it[1]
        else:
            label, text = "", it
        if not (text or "").strip():
            continue
        n += 1
        r = check(text)
        if r["level"] == "block":
            blocked.append({"label": label, "hits": r["hits"],
                            "reason": r["reason"],
                            "preview": (text or "")[:60].replace("\n", " ")})
        elif r["level"] == "warn":
            warned.append({"label": label, "hits": r["hits"],
                           "reason": r["reason"],
                           "preview": (text or "")[:60].replace("\n", " ")})
    return {"ok": not blocked, "blocked": blocked, "warned": warned, "checked": n}