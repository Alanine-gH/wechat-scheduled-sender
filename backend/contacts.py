# -*- coding: utf-8 -*-
"""账号与联系人枚举：自动发现本机所有微信账号，并列出该账号下的群聊与好友。

关键实现要点（均经实测确认）：
- 群聊**自己查** chat_room / chatroom_member / contact（见 list_groups）。
  为什么不走第三方库的 ``db.get_groups()``：它内部用
  ``WHERE room_id IN (…)``（占位符数 = 群数）和 ``WHERE id IN (…)``
  （占位符数 = **全部群成员总数**）。成员总数一旦超过 SQLite 参数上限
  （老版本 999 / 新版 32766），直接报 ``too many SQL variables``，
  界面表现就是「读取失败：too many SQL variables」。
  测试机 29 个群约 1.3 万成员没触发；用户机器群多、成员多就炸了。
- 好友用 ``db._contact_conn()`` 直接查 contact.db，**必须带 local_type=1**
  （仅按 username 后缀过滤会混入系统号/公众号）。
- 群与好友靠 local_type（2=群 / 3=好友成员）+ username 后缀双信号区分，两者零冲突。
  （注意：群里能看到但没加好友的人 local_type=3；真实好友是 local_type=1，
  与微信「通讯录」界面一致 —— 见下方 _FRIEND_SQL 的注释。）
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional

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

# SQLite 参数上限：老版本 999，3.32+ 是 32766。按 900 分批，两头都安全。
_CHUNK = 900


def _query_in(conn: sqlite3.Connection, sql: str, ids: Iterable[int]) -> list:
    """分批执行 ``… WHERE id IN (%s)`` 查询。

    ids 数量再多也不会触发 ``too many SQL variables``。
    sql 里必须正好有一个 ``%s`` 占位符用来填 ``?,?…``。
    """
    out: list = []
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        part = ids[i:i + _CHUNK]
        if not part:
            continue
        ph = ",".join("?" * len(part))
        out.extend(conn.execute(sql % ph, tuple(part)).fetchall())
    return out


def _enumerate_groups(conn: sqlite3.Connection, with_members: bool = False) -> List[Dict[str, Any]]:
    """枚举全部群聊。conn 是已解密的 contact.db 连接（row_factory=Row）。

    设计：
    - 成员**计数**用 GROUP BY，一次查询拿所有群的成员数，完全不碰 IN；
    - 成员**详情**（只有 with_members=True 才查）先拉全量 room_id/member_id
      映射（无 IN），再对去重后的成员 id 按 900 一批分片查 contact。
    这样群再多、成员再多都不会撞参数上限。
    """
    rooms = conn.execute("SELECT id, username, owner FROM chat_room").fetchall()
    room_names = {
        r["username"]: (r["nick_name"] or r["username"])
        for r in conn.execute(
            "SELECT username, nick_name FROM contact "
            "WHERE username LIKE '%@chatroom'").fetchall()
    }
    counts = {
        int(r["room_id"]): int(r["n"])
        for r in conn.execute(
            "SELECT room_id, COUNT(*) AS n FROM chatroom_member GROUP BY room_id")
    }

    members_by_room: Dict[int, List[Dict[str, Any]]] = {}
    if with_members:
        all_members = conn.execute(
            "SELECT room_id, member_id FROM chatroom_member").fetchall()
        by_room: Dict[int, List[int]] = {}
        for m in all_members:
            by_room.setdefault(int(m["room_id"]), []).append(int(m["member_id"]))
        contact = {
            int(r["id"]): r
            for r in _query_in(
                conn,
                "SELECT id, username, nick_name, remark FROM contact "
                "WHERE id IN (%s)",
                sorted({int(m["member_id"]) for m in all_members}),
            )
        }
        room_owner = {int(r["id"]): (r["owner"] or "") for r in rooms}
        for rid, ids in by_room.items():
            owner = room_owner.get(rid, "")
            lst: List[Dict[str, Any]] = []
            for mid in ids:
                c = contact.get(mid)
                if c is None:
                    continue
                lst.append({
                    "username": c["username"],
                    "nick_name": c["nick_name"],
                    "remark": c["remark"],
                    "is_owner": c["username"] == owner,
                })
            members_by_room[rid] = lst

    out: List[Dict[str, Any]] = []
    for room in rooms:
        rid = int(room["id"])
        uname = room["username"] or ""
        item: Dict[str, Any] = {
            "kind": "group",
            "username": uname,
            "name": room_names.get(uname, uname),
            "owner": room["owner"],
            # 没要详情时用 GROUP BY 的真实成员数；要详情时以能对上 contact 的为准，
            # 与旧实现（只数查得到资料的成员）保持一致。
            "member_count": counts.get(rid, 0) if not with_members else len(
                members_by_room.get(rid) or []),
        }
        if with_members:
            item["members"] = members_by_room.get(rid) or []
        out.append(item)
    out.sort(key=lambda x: (-(x.get("member_count") or 0), x.get("name") or ""))
    return out


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
    """列出该账号的全部群聊（读 chat_room 表，含很久没说话的群）。

    实现走 ``_enumerate_groups``，不用第三方库的 ``get_groups()``，
    原因见模块头部说明（成员总数超 SQLite 参数上限会炸）。
    """
    db = get_db(account)
    conn = db._contact_conn()
    if conn is None:
        raise RuntimeError("无法打开 contact.db，请确认微信已登录且密钥可获取")
    try:
        return _enumerate_groups(conn, with_members=with_members)
    finally:
        conn.close()


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
