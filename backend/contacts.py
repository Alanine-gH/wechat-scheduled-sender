# -*- coding: utf-8 -*-
"""账号与联系人枚举：自动发现本机所有微信账号，并列出该账号下的群聊与好友。

关键实现要点（均经实测确认）：
- 群聊用 ``db.get_groups()``（读 chat_room 表，含已退出的群，比 contact 表更全）
- 好友用 ``db._contact_conn()`` 直接查 contact.db，**必须带 local_type=3**
  （仅按 username 后缀过滤会混入系统号/公众号）
- 群与好友靠 local_type（2=群 / 3=好友）+ username 后缀双信号区分，两者零冲突
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

# 好友查询：微信「通讯录」里的真实好友 = local_type=1，排除群/公众号/企微/自己。
#
# 实测依据（务必保留这段说明，避免后人再踩坑）：
#   * local_type=3 是「群成员」——从群里能看到但**没有加好友**的人。本机 8089 行里
#     有 8088 行同时出现在 chatroom_member 表中。把这些列成好友是错的。
#   * 真实好友在 local_type=1：big / s_big / 齐点 / 收粮人的财税课 / 晚风叙旧 都在这里，
#     与微信「通讯录」界面显示的分组「联系人 8」一一对应。
#   * 需要再排除：自己的账号、以及只在会话里出现但不在通讯录的系统通知号
#     （fmessage 朋友推荐消息、medianote 语音记事本）。
_FRIEND_SQL = """
SELECT username, nick_name, remark, alias
FROM contact
WHERE local_type = 1
  AND username IS NOT NULL AND username != ''
  AND username NOT LIKE '%@chatroom'
  AND username NOT LIKE '%@openim'
  AND username NOT LIKE 'gh_%'
  AND delete_flag = 0
"""

# 不出现在通讯录的系统通知号（会话类，不是好友）
_SYSTEM_ACCOUNTS = {
    "fmessage",           # 朋友推荐消息
    "medianote",          # 语音记事本
    "notifymessage",      # 服务通知
    "qqmail", "qqsafe", "floatbottle", "newsapp",
    "masssend", "voipnotify", "voipinvitemsg", "lockmicromsg",
    "brandsessionholder", "brandservicesessionholder",
    "opencustomerservicemsg", "tmessage", "qmessage", "weapp",
    "xwechat", "weclaw",
}

_lock = threading.Lock()
_db_cache: Dict[str, Any] = {}


def _import_wechatauto():
    try:
        from wechatauto import WeChatDB, list_accounts  # noqa: WPS433
        return WeChatDB, list_accounts
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少依赖 wechatauto-replica，请先运行 install.bat 安装。原始错误: %s" % exc
        )


def list_local_accounts() -> List[Dict[str, Any]]:
    """枚举本机所有存在数据目录的微信账号。

    返回每项：account(目录名) / wxid / path / last_activity / is_current
    """
    _, list_accounts = _import_wechatauto()
    raw = list_accounts()
    out: List[Dict[str, Any]] = []
    for item in raw:
        out.append({
            "account": item.get("account"),
            "wxid": item.get("wxid"),
            "path": item.get("path"),
            "last_activity": item.get("last_activity"),
        })
    if out:
        newest = max(out, key=lambda x: x.get("last_activity") or 0)
        for item in out:
            item["is_current"] = item["account"] == newest["account"]
    return out


def get_db(account: Optional[str] = None):
    """取得（并缓存）某账号的 WeChatDB 实例。"""
    WeChatDB, _ = _import_wechatauto()
    key = account or "__current__"
    with _lock:
        if key in _db_cache:
            return _db_cache[key]
        db = WeChatDB(account=account) if account else WeChatDB()
        _db_cache[key] = db
        return db


def clear_cache() -> None:
    with _lock:
        _db_cache.clear()


def list_groups(account: Optional[str] = None, with_members: bool = False) -> List[Dict[str, Any]]:
    """列出该账号的全部群聊（读 chat_room 表，含很久没说话的群）。"""
    db = get_db(account)
    groups = db.get_groups()
    out: List[Dict[str, Any]] = []
    for g in groups:
        name = (g.get("name") or "").strip() or g.get("username") or ""
        item = {
            "kind": "group",
            "username": g.get("username"),
            "name": name,
            "owner": g.get("owner"),
            "member_count": g.get("member_count") or 0,
        }
        if with_members:
            item["members"] = g.get("members") or []
        out.append(item)
    out.sort(key=lambda x: (-(x.get("member_count") or 0), x.get("name") or ""))
    return out


def list_friends(account: Optional[str] = None, limit: int = 0) -> List[Dict[str, Any]]:
    """列出该账号的全部好友（local_type=3，已排除群/公众号/企微/系统号）。"""
    db = get_db(account)
    conn = db._contact_conn()
    if conn is None:
        raise RuntimeError("无法打开 contact.db，请确认微信已登录且密钥可获取")
    try:
        rows = conn.execute(_FRIEND_SQL).fetchall()
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    seen = set()
    # 自己的账号不列入好友
    try:
        self_wxid = (getattr(db, "wxid", "") or "").strip()
    except Exception:
        self_wxid = ""
    if not self_wxid:
        try:
            self_wxid = (db.get_self_info() or {}).get("username", "") or ""
        except Exception:
            self_wxid = ""

    for r in rows:
        username = (r["username"] or "").strip()
        if not username or username in seen:
            continue
        low = username.lower()
        if low in _SYSTEM_ACCOUNTS:
            continue
        if self_wxid and username == self_wxid:
            continue
        seen.add(username)
        name = (r["remark"] or "").strip() or (r["nick_name"] or "").strip() or username
        out.append({
            "kind": "friend",
            "username": username,
            "name": name,
            "nick_name": r["nick_name"] or "",
            "remark": r["remark"] or "",
            "alias": r["alias"] or "",
        })
    out.sort(key=lambda x: x["name"])
    if limit and limit > 0:
        return out[:limit]
    return out


def get_contacts(account: Optional[str] = None) -> Dict[str, Any]:
    """一次性返回该账号的群聊 + 好友，供前端下拉框使用。"""
    groups = list_groups(account)
    friends = list_friends(account)
    db = get_db(account)
    try:
        self_info = db.get_self_info()
    except Exception:
        self_info = {}

    # 所选账号是否就是当前登录窗口对应的账号（决定能否真正发送）
    selected = getattr(db, "account", account)
    current = None
    try:
        accs = list_local_accounts()
        cur = next((a for a in accs if a.get("is_current")), None)
        current = cur.get("account") if cur else None
    except Exception:
        pass
    is_current = bool(selected and current and selected == current)

    return {
        "account": selected,
        "current_account": current,
        "is_current_login": is_current,
        "self": self_info,
        "groups": groups,
        "friends": friends,
        "group_count": len(groups),
        "friend_count": len(friends),
    }
