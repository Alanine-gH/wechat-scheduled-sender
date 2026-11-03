# -*- coding: utf-8 -*-
"""接收私聊消息：轮询本地数据库，找出「群友主动私聊我」的线索。

对应运营方案的命门：**这套方案的重心不在「发」，而在「收」**。
文档里 6 项 KPI 有 4 项（主动私聊人数、有客户同行数、深度沟通数、
合作意向数）全部依赖读取私聊消息，而之前的工具是完全单向的。

为什么不用 hook：
数据库里已经有全部消息（``get_new_messages(user, since_seq)`` 支持增量拉取），
解密读库就够，不需要注入进程 —— 与现有架构一致，也没有额外风控风险。

方向怎么判断：每条消息带 ``sender_username``，等于自己的 wxid 就是发出去的，
否则就是对方发来的。

首次运行的处理（关键）：
第一个把某会话纳入轮询时**不能**把历史消息当成新消息 —— 否则一开就灌进来
几百条旧记录。所以首次只记录「当前最大 sort_seq」作为游标，不产出线索。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

# 这些不是真实好友，不参与线索统计
IGNORE_USERNAMES = {"filehelper", "weixin", "notifymessage", "floatbottle",
                    "newsapp", "fmessage", "medianote"}

# 「资料钩子」类意图：只想要资料，按方案 3.1 属于低意向
HOOK_KEYWORDS = ("资料", "清单", "模板", "表格", "pdf", "发我", "要一份",
                 "来一份", "怎么领", "领取")

# 「有具体需求」类意图：提到客户或在处理具体业务，按方案 3.1 属于高意向，
# 当天就该转组长。
# 注意：这里刻意**不放「粮食」**这种领域词 —— 方案里的资料本身就叫
# 「粮食财税资料」，人人都会提到粮食，用它判高意向会把「仅领资料」误判。
NEED_KEYWORDS = ("客户", "收购", "发票", "台账", "备案", "自产", "承包",
                 "合作社", "税负", "个体户", "一般纳税人", "反向开票",
                 "收储", "经纪人", "账务", "企业", "公司", "加工厂",
                 "贸易", "代账", "事务所", "分润", "合作")

STATE_FILE = "inbox_state.json"


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


def classify_text(text: str,
                  need: Optional[List[str]] = None,
                  hook: Optional[List[str]] = None) -> str:
    """给来消息打意图标签。

    返回 ``"need"``（提到客户/业务，高意向）、``"hook"``（只是要资料）、
    ``"other"``。按方案 3.1 节的分类：仅领资料 = 低意向，有具体客户 = 当天转组长。

    关键词可由调用方传入（来自设置里的「高意向/低意向关键词」），
    不传就用本模块的默认表 —— 业务词会变，写死不可调。
    """
    t = (text or "").lower()
    need_kw = need if need else NEED_KEYWORDS
    hook_kw = hook if hook else HOOK_KEYWORDS
    if any(str(k).lower() in t for k in need_kw if str(k).strip()):
        return "need"
    if any(str(k).lower() in t for k in hook_kw if str(k).strip()):
        return "hook"
    return "other"


def match_keywords(text: str,
                   need: Optional[List[str]] = None,
                   hook: Optional[List[str]] = None) -> List[str]:
    """命中的关键词列表（用于线索表里显示「他提到了什么」）。"""
    t = (text or "").lower()
    hits: List[str] = []
    for k in list(need if need else NEED_KEYWORDS) \
            + list(hook if hook else HOOK_KEYWORDS):
        sk = str(k).strip()
        if sk and sk.lower() in t and sk not in hits:
            hits.append(sk)
    return hits


def _peer_name(friend: Dict[str, Any]) -> str:
    return friend.get("name") or friend.get("nick_name") or friend.get("username")


def poll_new(account: Optional[str] = None,
             data_dir: str = "",
             friends: Optional[List[Dict[str, Any]]] = None,
             per_chat_limit: int = 50,
             include_history: bool = False,
             need_keywords: Optional[List[str]] = None,
             hook_keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    """轮询所有私聊会话，返回新收到的消息。

    ``include_history=False``（默认）时，**首次纳入轮询的会话只记游标、
    不产出线索**，避免历史消息被当成新线索灌进来。

    返回 ``{"items": [...], "scanned": n, "bootstrapped": [...], "me": wxid}``
    """
    try:
        import contacts as C
    except Exception as exc:
        return {"error": "无法加载联系人模块：%s" % exc, "items": []}

    try:
        db = C.get_db(account)
    except Exception as exc:
        return {"error": "无法打开数据库：%s" % exc, "items": []}

    # 自己的 wxid：用来判断消息方向
    me = ""
    try:
        info = db.get_self_info() or {}
        me = info.get("username") or info.get("wxid") or ""
    except Exception:
        me = ""
    if not me:
        # 退路：用账号目录名去掉后缀（形如 wxid_xxx_abcd）
        acc = getattr(db, "account", "") or ""
        me = acc.rsplit("_", 1)[0] if acc else ""

    if friends is None:
        try:
            friends = C.list_friends(account)
        except Exception:
            friends = []

    state = load_state(data_dir)
    chats = state.setdefault("chats", {})

    items: List[Dict[str, Any]] = []
    bootstrapped: List[str] = []

    for f in friends or []:
        u = (f.get("username") or "").strip()
        if not u or u in IGNORE_USERNAMES:
            continue
        rec = chats.get(u)
        first_time = rec is None
        cursor = int((rec or {}).get("cursor") or 0)

        try:
            msgs = db.get_new_messages(u, since_seq=cursor, limit=per_chat_limit)
        except Exception:
            continue

        max_seq = cursor
        for m in msgs or []:
            try:
                seq = int(m.get("sort_seq") or 0)
            except Exception:
                seq = 0
            if seq > max_seq:
                max_seq = seq
            sender = (m.get("sender_username") or "").strip()
            if sender and me and sender == me:
                continue                      # 自己发的，不算线索
            if first_time and not include_history:
                continue                      # 首次只记游标，不产出
            text = (m.get("content") or "") if m.get("type") in ("文本", None) else ""
            items.append({
                "username": u,
                "name": _peer_name(f),
                "at": int(m.get("create_time") or 0),
                "sort_seq": seq,
                "type": m.get("type") or "",
                "text": text,
                "intent": classify_text(text, need_keywords, hook_keywords),
                "keywords": match_keywords(text, need_keywords, hook_keywords),
            })

        chats[u] = {
            "cursor": max_seq,
            "name": _peer_name(f),
            "last_poll": int(time.time()),
        }
        if first_time:
            bootstrapped.append(_peer_name(f))

    save_state(data_dir, state)
    return {"items": items, "scanned": len(friends or []),
            "bootstrapped": bootstrapped, "me": me,
            "state": {k: v.get("cursor") for k, v in chats.items()}}


def reset_cursors(data_dir: str) -> int:
    """清空所有游标（下次轮询会重新 bootstrap）。返回清掉的会话数。"""
    state = load_state(data_dir)
    n = len(state.get("chats") or {})
    state["chats"] = {}
    save_state(data_dir, state)
    return n