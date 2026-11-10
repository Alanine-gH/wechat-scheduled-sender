# -*- coding: utf-8 -*-
"""本人在群里发的消息：UIA 界面截屏流程 + 本地聊天库导出。

两条路（前端可选）：
  * ui  —— 按用户给的 8 步，用 UIA 控件树打开「查找聊天记录」窗口，
            筛选到本人，逐屏截屏并顺便把可见文本抓下来。
  * db  —— 直接读已解密的本地聊天库，按 sender_username 过滤出本人消息，
            全量导出 JSON/TXT。不动微信界面，更快更全。

为什么 UIA 这条路要「可探测 + 可配置」：
  微信把 accessibility gate 关掉时，UIA 树只剩一个 MainWindow 根节点；
  且「查找聊天记录」是**独立顶层窗口**，控件名随版本/语言变化。
  所以：
    - 每一步都用「候选名列表」匹配 Name / AutomationId / ClassName，
      不认死一个字符串，也不用任何鼠标坐标；
    - 设置里 `myhistory_names_extra` 可追加候选名，不用改代码；
    - `dump_trees()` 把真实控件树写到 logs/，据此就能精确调名。

严禁坐标点击：所有动作走 ControlFromHandle + 语义控件 Click/SetValue，
窗口移动、缩放、被遮挡都不影响（项目既有约定）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

import uiautomation as auto

try:
    import win32gui
    import win32con
except Exception:  # pragma: no cover
    win32gui = None
    win32con = None

# ---------------------------------------------------------------- 候选名
# 这些是「按用户截图 + 微信常见文案」给出的候选，命中任意一个即可。
MORE_NAMES = ("更多", "...", "···", "聊天信息", "群聊信息", "Chat Info")
FIND_NAMES = ("查找聊天内容", "查找聊天记录", "搜索聊天内容", "聊天记录")
MEMBER_TAB_NAMES = ("群成员",)
# 只认「搜索群成员」这一类，**绝不认裸「搜索」**。
# 聊天记录窗口顶部的全局搜索框 Name 恰好是「搜索」，
# 一旦拿它兜底，昵称就会被当成聊天内容关键词搜出全群消息 ——
# 这正是 2026-11-10 实测踩到的坑（47 张截图全是别人的消息）。
MEMBER_SEARCH_NAMES = ("搜索群成员", "搜索成员", "查找群成员",
                       "搜索群聊成员", "Search group members")
GLOBAL_SEARCH_MARK = "搜索"
MEMBER_ITEM_TYPES = ("ListItemControl", "ButtonControl", "TextControl",
                     "GroupControl", "ImageControl", "PaneControl")
# 「查找聊天记录」窗口标题实测形如："群名"的聊天记录(500)
HIST_TITLE_MARKS = ("的聊天记录", "聊天记录")

_MAX_NODES = 4000
_MAX_DEPTH = 26


def _extra_names() -> List[str]:
    """从设置里读用户追加的候选名（一行一个），失败就空。"""
    try:
        import settings as St
        import userdata
        v = St.get(userdata.sub(userdata.DIR_DATA),
                   "myhistory_names_extra") or []
        if isinstance(v, str):
            v = [x.strip() for x in re.split(r"[|\n]", v)]
        return [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        return []


def _names(base) -> List[str]:
    out = list(base) + _extra_names()
    # 去重保序
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


# ---------------------------------------------------------------- 窗口
def _wechat_pids() -> set:
    """当前微信进程的 PID 集合，用来按归属识别窗口，而不是认死类名。

    微信的浮层 / 子窗口类名可能是 Qt51514QWindowIcon、QWindowTool、
    Qt624QWindowIcon…… 认类名会漏。按 PID 认最稳：只要是微信进程
    创建的顶层窗口就收进来。取不到进程列表时返回空集，由调用方退回
    「类名 + 标题」的旧规则，不让功能直接失效。
    """
    pids = set()
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n in ("weixin.exe", "wechat.exe", "wechatapp.exe"):
                pids.add(p.info["pid"])
    except Exception:
        pass
    if not pids:
        try:
            import ctypes
            from ctypes import wintypes
            # 兜底：Toolhelp32 快照枚举进程名
            TH32CS_SNAPPROCESS = 0x2
            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD),
                            ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                            ("th32ModuleID", wintypes.DWORD),
                            ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_wchar * 260)]
            snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(
                TH32CS_SNAPPROCESS, 0)
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            k32 = ctypes.windll.kernel32
            if k32.Process32FirstW(snap, ctypes.byref(entry)):
                while True:
                    n = entry.szExeFile.lower()
                    if n in ("weixin.exe", "wechat.exe", "wechatapp.exe"):
                        pids.add(entry.th32ProcessID)
                    if not k32.Process32NextW(snap, ctypes.byref(entry)):
                        break
            k32.CloseHandle(snap)
        except Exception:
            pass
    return pids


def wechat_windows() -> List[Dict[str, Any]]:
    """枚举所有微信相关顶层窗口（含隐藏），只读。

    判定顺序：先按进程 PID 归属（覆盖所有 Qt 浮层/子窗口），
    PID 拿不到时退回「类名含 QWindowIcon + 标题含微信/聊天记录」。
    """
    if win32gui is None:
        return []
    out: List[Dict[str, Any]] = []
    pids = _wechat_pids()

    def cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd) or ""
            title = win32gui.GetWindowText(hwnd) or ""
            try:
                import win32process
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            is_ours = bool(pids) and pid in pids
            is_qt = "QWindowIcon" in cls or "QWindowTool" in cls
            is_hist = any(m in title for m in HIST_TITLE_MARKS)
            is_main = is_qt and any(x in title for x in ("微信", "Weixin", "WeChat"))
            if not (is_ours or is_qt or is_hist or is_main):
                return True
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            out.append({"hwnd": hwnd, "pid": pid, "cls": cls, "title": title,
                        "w": r - l, "h": b - t,
                        "visible": bool(win32gui.IsWindowVisible(hwnd)),
                        "is_hist": is_hist, "is_main": is_main})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def _find_history_window(baseline: set, group: str = "",
                         timeout: float = 10.0) -> Optional[int]:
    """轮询找「聊天记录」窗口。

    优先取新出现且标题带群名的窗口；用户可能已经手动开着这个窗口，
    所以也接受 baseline 里已存在、标题匹配该群的窗口。
    """
    from sender import normalize_name  # 局部导入，避免 import 期循环
    key = normalize_name(group)[:6]
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = wechat_windows()
        hist = [w for w in wins if w["is_hist"]]
        # 标题带群名的优先（避免抓到别的群的窗口）
        titled = [w for w in hist if key and key in normalize_name(w["title"])]
        fresh = [w for w in titled if w["hwnd"] not in baseline]
        pick = fresh or titled
        if not pick:
            fresh = [w for w in hist if w["hwnd"] not in baseline]
            pick = fresh
        if pick:
            return max(pick, key=lambda x: (x["visible"], x["w"] * x["h"]))["hwnd"]
        other = [w for w in wins if w["hwnd"] not in baseline and w["visible"]
                 and w["w"] > 300 and w["h"] > 300 and not w["is_main"]]
        if other:
            return max(other, key=lambda x: x["w"] * x["h"])["hwnd"]
        time.sleep(0.4)
    return None


# ---------------------------------------------------------------- 控件树
def _walk(root, max_depth=_MAX_DEPTH, max_nodes=_MAX_NODES):
    """深度优先遍历，产出 (control, depth)。带节点上限，避免微信大群卡死。"""
    stack = [(root, 0)]
    n = 0
    while stack and n < max_nodes:
        c, d = stack.pop()
        n += 1
        yield c, d
        if d >= max_depth:
            continue
        try:
            children = list(c.GetChildren())
        except Exception:
            continue
        for ch in reversed(children):
            stack.append((ch, d + 1))


def _field(c, attr: str) -> str:
    try:
        return str(getattr(c, attr) or "").strip()
    except Exception:
        return ""


def _match(c, names) -> bool:
    name, aid, cls = _field(c, "Name"), _field(c, "AutomationId"), _field(c, "ClassName")
    for n in names:
        if not n:
            continue
        if n in name or n.lower() in aid.lower() or n.lower() in cls.lower():
            return True
    return False


def find_control(root, names, types=None, exact_first=True):
    """按候选名找控件。先试精确 Name，再试子串；可限定 ControlTypeName。"""
    names = _names(names)
    candidates = []
    for c, _d in _walk(root):
        if types and c.ControlTypeName not in types:
            continue
        if _field(c, "Name") in names:
            candidates.insert(0, c)       # 精确匹配优先
        elif _match(c, names):
            candidates.append(c)
        if exact_first and candidates and _field(candidates[0], "Name") in names:
            break
    return candidates[0] if candidates else None


def _roots(prefer: Optional[int] = None):
    """所有微信窗口的 UIA 根，按「优先窗口 → 可见 → 面积大」排序。

    微信的「搜索群成员」结果是 Qt 浮层，常常是**独立顶层窗口**；
    只在聊天记录窗口的树里找会漏，所以必须跨窗口找（只读）。

    微信进程会带一堆隐藏的零尺寸 Qt 内部窗口（实测 18 个），
    逐个 ControlFromHandle 既慢又可能卡住，这里先过滤掉。
    """
    wins = wechat_windows()
    wins = [w for w in wins if w["visible"] and w["w"] >= 200 and w["h"] >= 150]
    wins.sort(key=lambda w: (0 if prefer and w["hwnd"] == prefer else 1,
                             -(w["w"] * w["h"])))
    out = []
    for w in wins:
        try:
            r = auto.ControlFromHandle(w["hwnd"])
            if r is not None:
                out.append((w["hwnd"], r))
        except Exception:
            continue
    return out


def find_anywhere(names, types=None, prefer=None):
    for _hwnd, r in _roots(prefer):
        c = find_control(r, names, types)
        if c is not None:
            return c
    return None


def _page_down(prefer: Optional[int] = None) -> None:
    """键盘翻页（不用滚轮坐标），让 Qt 虚拟列表把下一批项渲染进控件树。

    优先聚焦「含搜索群成员输入框」的那个浮层窗口 —— 用户步骤 6 的
    「下拉找到本人头像」发生在浮层里，滚错窗口等于没翻。
    """
    pairs = _roots(prefer)
    ordered = []
    for h, r in pairs:
        has_member_box = any(_is_member_search(c)
                             for c, _d in _walk(r, max_nodes=800))
        ordered.append((0 if has_member_box else 1, h, r))
    ordered.sort(key=lambda x: x[0])
    for _pri, _h, r in ordered:
        try:
            r.SetFocus()
            auto.SendKeys("{PageDown}", waitTime=0.03)
            return
        except Exception:
            continue


def find_scrolling(names, types=None, prefer=None, scrolls: int = 4):
    """找不到就翻页再找。

    用户实测：右上角面板里的「查找聊天内容」、以及群成员结果里的本人头像，
    都要**下拉**才出现。Qt 虚拟列表不会把屏幕外的项放进 UIA 树，
    所以只查一次必然漏。这里用键盘翻页逐批找，仍不碰鼠标坐标。
    """
    for i in range(max(1, scrolls)):
        c = find_anywhere(names, types, prefer)
        if c is not None:
            return c
        if i < scrolls - 1:
            _page_down(prefer)
            time.sleep(0.55)
    return None


def click_control(c) -> bool:
    """语义点击。微信 4.2 的 InvokePattern 常返回成功但不生效，所以先 Click。"""
    if c is None:
        return False
    try:
        c.Click()
        return True
    except Exception:
        pass
    try:
        p = c.GetInvokePattern()
        if p:
            p.Invoke()
            return True
    except Exception:
        pass
    return False


def set_text(c, text: str, prefer_keys: bool = False) -> bool:
    """语义输入：不用剪贴板、不用坐标。

    prefer_keys=True 时先走「Click + SendKeys」：微信搜索框靠真实输入事件
    触发筛选，ValuePattern.SetValue 有时只改值不发事件，结果列表不刷新。
    """
    if c is None:
        return False
    if prefer_keys:
        try:
            c.Click()
            auto.SendKeys(text, waitTime=0.02)
            return True
        except Exception:
            pass
    try:
        p = c.GetValuePattern()
        if p:
            p.SetValue(text)
            return True
    except Exception:
        pass
    try:
        c.Click()
        auto.SendKeys(text, waitTime=0.02)
        return True
    except Exception:
        return False


def dump_tree(root, limit=600) -> List[Dict[str, Any]]:
    """把控件树压成可诊断的字典列表（只读）。"""
    out: List[Dict[str, Any]] = []
    for c, d in _walk(root, max_nodes=limit):
        try:
            r = c.BoundingRectangle
            rect = [r.left, r.top, r.right, r.bottom]
        except Exception:
            rect = []
        out.append({"depth": d, "name": _field(c, "Name"),
                    "aid": _field(c, "AutomationId"),
                    "type": _field(c, "ControlTypeName"),
                    "cls": _field(c, "ClassName"), "rect": rect})
        if len(out) >= limit:
            break
    return out


def dump_trees(log_dir: str) -> Dict[str, Any]:
    """探测入口：把当前所有微信窗口的控件树写到 logs/。

    不点击、不输入；仅唤醒一次 gate 让控件树实体化（正常运行也需要这一步）。
    用户把生成的 JSON 发回来，就能精确调整候选名，不必改代码。
    """
    import sender as S
    S.init_com()
    wins = wechat_windows()
    main = next((w for w in wins if w["is_main"]), None)
    if main:
        try:
            S.wake_accessibility(main["hwnd"], retries=1)
        except Exception:
            pass
        time.sleep(0.6)
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "uia_trees_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    payload = []
    for w in wins:
        item = dict(w)
        try:
            root = auto.ControlFromHandle(w["hwnd"])
            item["tree"] = dump_tree(root) if root else []
        except Exception as e:
            item["tree"] = []
            item["error"] = str(e)
        payload.append(item)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return {"ok": True, "path": path,
            "windows": [{"hwnd": w["hwnd"], "title": w["title"],
                         "cls": w["cls"], "visible": w["visible"]} for w in wins],
            "nodes": sum(len(x.get("tree") or []) for x in payload)}


# ---------------------------------------------------------------- 文本抓取
def collect_texts(root, limit=400) -> List[str]:
    """从结果列表里抓可见文本（去重、保序），用于结构化导出。"""
    out, seen = [], set()
    for c, _d in _walk(root, max_nodes=1500):
        if c.ControlTypeName not in ("TextControl", "ListItemControl",
                                     "HyperlinkControl", "ButtonControl"):
            continue
        name = _field(c, "Name")
        if not name or len(name) > 2000 or name in seen:
            continue
        # 过滤掉纯标签/纯数字，保留像消息的正文
        if name in ("搜索", "群成员", "日期", "文件", "图片与视频", "链接"):
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def _digest(texts) -> str:
    return hashlib.sha1("\n".join(texts).encode("utf-8", "replace")).hexdigest()[:12]


# ------------------------------------------------------- 群成员筛选状态校验
def _edit_value(c) -> str:
    """读 EditControl 当前值。读不到返回空串。"""
    try:
        p = c.GetValuePattern()
        if p is not None:
            return str(p.CurrentValue or "").strip()
    except Exception:
        pass
    return ""


def _is_global_search(c) -> bool:
    """是不是聊天记录窗口顶部那个「全局搜索聊天内容」框。

    实测控件：EditControl / mmui::XValidatorTextEdit / Name='搜索'。
    往这里输昵称 = 搜全群消息，必须杜绝。
    """
    if c.ControlTypeName != "EditControl":
        return False
    name = _field(c, "Name")
    cls = _field(c, "ClassName").lower()
    return name == GLOBAL_SEARCH_MARK or (
        not name and "validator" in cls)


def _is_member_search(c) -> bool:
    """是不是「群成员」浮层里的「搜索群成员」框。"""
    if c.ControlTypeName != "EditControl":
        return False
    probe = " ".join((_field(c, "Name"), _field(c, "AutomationId"),
                      _field(c, "ClassName"))).lower()
    return any(k.lower() in probe for k in MEMBER_SEARCH_NAMES)


def search_state(roots) -> Dict[str, Any]:
    """扫描所有窗口的搜索框，返回当前处于哪种搜索状态。

    这是防止「假成功」的关键：只有出现成员搜索框、且全局框为空，
    才说明真的切到了「群成员」筛选页。
    """
    st = {"member_box": None, "global_boxes": [], "global_values": [],
          "edits": 0}
    for _h, r in roots:
        for c, _d in _walk(r, max_nodes=1200):
            if c.ControlTypeName != "EditControl":
                continue
            st["edits"] += 1
            if _is_member_search(c):
                st["member_box"] = c
            elif _is_global_search(c):
                st["global_boxes"].append(c)
                v = _edit_value(c)
                if v:
                    st["global_values"].append(v)
    st["member_open"] = st["member_box"] is not None
    st["global_dirty"] = bool(st["global_values"])
    return st


def member_panel_ready(roots) -> bool:
    """「群成员」筛选页是否真的打开了（有成员搜索框、全局框没被写脏）。"""
    st = search_state(roots)
    return st["member_open"] and not st["global_dirty"]


def find_member_search(roots):
    """只在真正的成员搜索框里找，绝不退回全局搜索框。"""
    st = search_state(roots)
    return st["member_box"]


def tab_strategies(tab):
    """激活 TabItem 的候选动作，按语义强度排序。

    实测：微信 ``mmui::XMouseEventView`` 的 ``Click()`` **返回成功但不切换 tab**
    （和 sender.py 里 InvokePattern 那个坑同源）。调用方必须每试一个策略
    就用 ``member_panel_ready`` 验证状态，绝不「点了就算成功」。
    """
    return (
        ("selection", lambda: tab.GetSelectionItemPattern().Select()),
        ("legacy", lambda: tab.GetLegacyIAccessiblePattern().DoDefaultAction()),
        ("click", lambda: click_control(tab)),
        ("focus+space", lambda: (tab.SetFocus(),
                                 auto.SendKeys("{Space}", waitTime=0.05))),
    )


def wait_member_panel(prefer, baseline=None, timeout=4.0) -> bool:
    """轮询等「群成员」浮层出现。浮层常是独立顶层窗口，会带自己的搜索框。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if member_panel_ready(_roots(prefer)):
            return True
        time.sleep(0.3)
    return False


def _clear_global_search(roots) -> None:
    """把误写进全局搜索框的关键词清掉，避免污染用户的微信界面。

    只清「聊天记录窗口顶部」那个框，不动成员搜索框。
    """
    for _h, r in roots:
        for c, _d in _walk(r, max_nodes=1200):
            if not _is_global_search(c) or not _edit_value(c):
                continue
            try:
                p = c.GetValuePattern()
                if p is not None:
                    p.SetValue("")
                    return
            except Exception:
                pass
            try:
                c.Click()
                auto.SendKeys("{Ctrl}a", waitTime=0.03)
                auto.SendKeys("{Delete}", waitTime=0.03)
            except Exception:
                pass
            return


def result_looks_filtered(roots) -> bool:
    """结果页是否仍像「按成员筛选」，而不是全局关键词搜索。

    全局搜索框里有值 = 昵称被当成聊天内容关键词 = 结果全是别人的消息。
    """
    return not search_state(roots)["global_dirty"]


# ---------------------------------------------------------------- 任务状态
class _Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"        # idle / running / done / error / cancelled
        self.stopped = False
        self.mode = "ui"
        self.total = 0
        self.done = 0
        self.current = ""
        self.message = ""
        self.results: List[Dict[str, Any]] = []
        self.started = 0.0

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {"state": self.state, "mode": self.mode,
                    "total": self.total, "done": self.done,
                    "current": self.current, "message": self.message,
                    "elapsed": round(time.time() - self.started, 1) if self.started else 0,
                    "results": list(self.results)}

    def log(self, msg: str) -> None:
        with self.lock:
            self.message = msg


JOB = _Job()


def _cancelled() -> bool:
    with JOB.lock:
        return JOB.stopped


# ---------------------------------------------------------------- UI 路
def _ui_one(engine, group: str, nicks: List[str], pages: int,
            shot_dir: str, log_dir: str) -> Dict[str, Any]:
    import sender as S

    out: Dict[str, Any] = {"group": group, "ok": False, "shots": [], "texts": []}
    baseline = {w["hwnd"] for w in wechat_windows()}

    engine.ensure_alive()
    if not engine.open_chat(group):
        out["error"] = engine.last_open_error or "打开会话失败"
        return out

    # 1) 右上角三个点
    more = find_anywhere(MORE_NAMES,
                         types=("ButtonControl", "ImageControl", "GroupControl"),
                         prefer=engine.hwnd)
    if more is None or not click_control(more):
        out["error"] = "没找到右上角「更多」按钮（用「探测控件树」看真实名字）"
        return out
    time.sleep(0.9)

    # 2) 面板里的「查找聊天内容」（要下拉，可能是浮层窗口）
    find = find_scrolling(FIND_NAMES, prefer=engine.hwnd, scrolls=4)
    if find is None or not click_control(find):
        out["error"] = "没找到「查找聊天内容」（用「探测控件树」看真实名字）"
        return out

    # 3) 等独立窗口出现
    hwnd = _find_history_window(baseline, group=group, timeout=10.0)
    if not hwnd:
        out["error"] = "没等到「聊天记录」窗口"
        return out
    out["hwnd"] = hwnd

    # 4) 新窗口出现后控件树会重建，必须重新唤醒 gate
    S.wake_accessibility(engine.hwnd, retries=1)
    S.ensure_window_visible(hwnd)
    time.sleep(0.8)
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        out["error"] = "聊天记录窗口控件树不可用"
        return out

    # 诊断：把关键节点的真实控件名落盘。
    # 之前只在打开窗口时 dump 一份，看不到「点群成员之后」的状态，
    # 导致 tab 假成功这个问题很难查。现在分阶段各存一份。
    diag: List[Dict[str, Any]] = []

    def snap(stage: str) -> None:
        diag.append({"stage": stage,
                     "windows": [{"hwnd": h, "tree": dump_tree(r)}
                                 for h, r in _roots(hwnd)]})

    try:
        os.makedirs(log_dir, exist_ok=True)
        out["diag_file"] = os.path.join(
            log_dir, "myhistory_tree_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    except Exception:
        pass

    def flush_diag() -> None:
        if not out.get("diag_file"):
            return
        try:
            with open(out["diag_file"], "w", encoding="utf-8") as f:
                json.dump({"group": group, "hwnd": hwnd, "stages": diag},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def fail(msg: str, stage: str = "失败"):
        out["error"] = msg
        snap(stage)
        flush_diag()
        return out

    snap("窗口刚打开")

    # 5) 群成员 tab：逐个策略试，每试一次都验证「成员面板真的开了」。
    #    微信 mmui::XMouseEventView 的 Click() 会假成功，不能点了就算完。
    tab = find_anywhere(MEMBER_TAB_NAMES,
                        types=("TabItemControl", "ButtonControl",
                               "RadioButtonControl", "TextControl"),
                        prefer=hwnd)
    if tab is None:
        return fail("没找到「群成员」标签（用「探测控件树」看真实名字）",
                    "没找到群成员标签")

    opened, used = False, ""
    for name, act in tab_strategies(tab):
        try:
            act()
        except Exception:
            continue
        if wait_member_panel(hwnd, timeout=2.5):
            opened, used = True, name
            break
        # 状态没变就撤销这一步可能造成的副作用，再试下一个策略
        _clear_global_search(_roots(hwnd))
    if not opened:
        _clear_global_search(_roots(hwnd))
        return fail(("点了「群成员」但筛选面板没打开（试过 %s）。"
                     "已停止，避免把昵称搜成全群消息。请用「探测控件树」"
                     "导出真实控件名，填到「设置 → 我的群消息 → 追加控件候选名」"
                     % " / ".join(n for n, _f in tab_strategies(tab))),
                    "群成员面板未打开")
    out["tab_strategy"] = used
    snap("群成员面板已打开")

    # 6) 只认成员搜索框，绝不退回全局搜索框
    roots_now = _roots(hwnd)
    box = find_member_search(roots_now)
    if box is None:
        return fail("「群成员」面板里没找到「搜索群成员」输入框，已停止",
                    "没找到成员搜索框")

    hit = False
    for nick in nicks:
        if not set_text(box, nick, prefer_keys=True):
            continue
        time.sleep(1.0)
        # 最后一道闸：昵称绝不能跑进全局搜索框
        if search_state(_roots(hwnd))["global_dirty"]:
            _clear_global_search(_roots(hwnd))
            break
        # 头像也要下拉才出现（用户步骤 6）
        item = find_scrolling([nick], types=MEMBER_ITEM_TYPES,
                              prefer=hwnd, scrolls=5)
        if item is not None:
            # 7) 点本人头像 / 结果项
            click_control(item)
            hit = True
            out["matched_nick"] = nick
            break
    if not hit:
        return fail(("按自动识别的昵称没匹配到本人头像；已停止，"
                     "避免把全群消息当成结果截图。可在「补充昵称」里"
                     "填本人在该群显示的名字"),
                    "没匹配到本人头像")

    time.sleep(1.2)
    # 点头像后仍要确认没有退化成全局关键词搜索
    roots_final = _roots(hwnd)
    if not result_looks_filtered(roots_final):
        _clear_global_search(roots_final)
        return fail(("点头像后界面退回了全局聊天记录搜索，结果不可信，"
                     "已停止截图"),
                    "点头像后退回全局搜索")

    # 8) 逐屏截屏 + 抓文本，内容不再变化时停止。
    #    文本跨所有微信窗口抓（筛选结果可能在浮层），截图只截聊天记录窗口。
    date = time.strftime("%Y%m%d")
    out_dir = os.path.join(shot_dir, "myhistory", date)
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", group)[:24] or "group"
    seen, alltexts = set(), []
    for i in range(max(1, pages)):
        if _cancelled():
            break
        roots_i = _roots(hwnd)
        # 每一屏截图前都复查，防止中途界面被微信重建回全局搜索
        if not result_looks_filtered(roots_i):
            out["error"] = ("第 %d 屏时界面不再是「按成员筛选」，"
                            "已停止，后续截图不生成" % (i + 1))
            break
        texts = []
        for _h, r in roots_i:
            for t in collect_texts(r):
                if t not in texts:
                    texts.append(t)
        dg = _digest(texts)
        if dg in seen and i > 0:
            break
        seen.add(dg)
        for t in texts:
            if t not in alltexts:
                alltexts.append(t)
        p = os.path.join(out_dir, "%s_%s_%02d.png"
                         % (safe, time.strftime("%H%M%S"), i + 1))
        if S.capture_window(hwnd, p):
            out["shots"].append(os.path.relpath(p, shot_dir).replace("\\", "/"))
        try:
            root_now = auto.ControlFromHandle(hwnd)
            if root_now is not None:
                root_now.SetFocus()
            auto.SendKeys("{PageDown}", waitTime=0.05)
            time.sleep(0.7)
        except Exception:
            break
    out["texts"] = alltexts
    out["ok"] = bool(out["shots"]) and "error" not in out
    if not out["ok"] and "error" not in out:
        out["error"] = "截屏失败（PrintWindow 未取到内容）"
    snap("截图完成" if out["ok"] else "截图中断")
    flush_diag()

    # 收尾：只关聊天记录窗口，不动主窗口
    try:
        if win32gui and win32gui.IsWindow(hwnd):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 自动识别本人
def _varint(b: bytes, i: int):
    v = shift = 0
    while i < len(b):
        x = b[i]
        i += 1
        v |= (x & 0x7F) << shift
        if not (x & 0x80):
            return v, i
        shift += 7
        if shift > 63:
            break
    return v, i


def _pb_fields(b: bytes):
    """最小 protobuf 解码：只认 varint / length-delimited / 32 / 64 位。

    ext_buffer 是 repeated ChatRoomMember，不引入任何第三方库。
    """
    out, i = [], 0
    while i < len(b):
        tag, i = _varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            break
        if wt == 0:
            v, i = _varint(b, i)
            out.append((fn, wt, v))
        elif wt == 2:
            n, i = _varint(b, i)
            if n > len(b) - i:
                break
            out.append((fn, wt, b[i:i + n]))
            i += n
        elif wt == 5:
            i += 4
            out.append((fn, wt, None))
        elif wt == 1:
            i += 8
            out.append((fn, wt, None))
        else:
            break
    return out


def _pb_text(v) -> str:
    try:
        s = bytes(v).decode("utf-8")
    except Exception:
        return ""
    return s if s and all(ord(c) >= 32 or c in "\t\n" for c in s) else ""


def group_display_names(db) -> Dict[str, List[str]]:
    """自动算出「本人在每个群里显示成什么名字」，完全不用用户填。

    数据源是微信自己的 contact.db：
      * ``chat_room.ext_buffer`` 是 repeated ChatRoomMember 的 protobuf，
        每条记录 field1=成员 wxid、field2=**该群里的群昵称**、field4=邀请人。
        逐群解析出本人那条记录的 field2 —— 这就是「本人在该群的昵称」，
        不同群不一样也能各自对上。
      * 没设群昵称的群（field2 缺失）退回微信全局昵称 / remark / alias，
        从 ``contact`` 表里本人那行取。

    返回 {群username: [候选名, ...]}，按优先级排序。解析失败返回空表，
    由调用方退回全局候选，绝不让功能挂死。
    """
    out: Dict[str, List[str]] = {}
    conn = None
    try:
        me = str(getattr(db, "wxid", "") or "").strip()
        if not me:
            return out
        conn = db._contact_conn()
        if conn is None:
            return out

        # 本人全局身份（微信昵称 / 备注 / 微信号）
        global_names: List[str] = []
        try:
            row = conn.execute(
                "SELECT nick_name, remark, alias FROM contact WHERE username = ?",
                (me,)).fetchone()
            if row is not None:
                for k in ("nick_name", "remark", "alias"):
                    v = str(row[k] or "").strip()
                    if v and v not in global_names:
                        global_names.append(v)
        except Exception:
            pass
        try:
            info = db.get_self_info() or {}
            for k in ("nick_name", "remark"):
                v = str(info.get(k) or "").strip()
                if v and v not in global_names:
                    global_names.append(v)
        except Exception:
            pass
        if me not in global_names:
            global_names.append(me)

        # 群 username -> room id，用于把 ext_buffer 对应到群
        for r in conn.execute("SELECT id, username, ext_buffer FROM chat_room"):
            uname = str(r["username"] or "").strip()
            if not uname:
                continue
            names: List[str] = []
            buf = r["ext_buffer"]
            if buf:
                try:
                    for fn, wt, val in _pb_fields(bytes(buf)):
                        if fn != 1 or wt != 2:
                            continue
                        sub = _pb_fields(val)
                        wx = next((_pb_text(v) for f, w, v in sub
                                   if f == 1 and w == 2), "")
                        if wx != me:
                            continue
                        nick = next((_pb_text(v) for f, w, v in sub
                                     if f == 2 and w == 2), "")
                        if nick and nick not in names:
                            names.append(nick)   # 群昵称优先
                        break
                except Exception:
                    pass
            for g in global_names:
                if g not in names:
                    names.append(g)
            if names:
                out[uname] = names
    except Exception:
        pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    return out


def global_self_names(db) -> List[str]:
    """拿不到群级信息时的兜底：本人全局昵称候选。"""
    out: List[str] = []
    try:
        info = db.get_self_info() or {}
        for k in ("nick_name", "remark", "username"):
            v = str(info.get(k) or "").strip()
            if v and v not in out:
                out.append(v)
        v = str(getattr(db, "wxid", "") or "").strip()
        if v and v not in out:
            out.append(v)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- DB 路
def _self_ids(db) -> List[str]:
    out = []
    try:
        info = db.get_self_info() or {}
        for k in ("username", "wxid", "nick_name", "remark"):
            v = str(info.get(k) or "").strip()
            if v:
                out.append(v)
    except Exception:
        pass
    try:
        v = str(getattr(db, "wxid", "") or "").strip()
        if v:
            out.append(v)
    except Exception:
        pass
    return list(dict.fromkeys(out))


def _db_one(db, group: str, username: str, ids: List[str],
            data_dir: str, cap: int = 3000) -> Dict[str, Any]:
    """直接读本地库，过滤 sender_username 为本人。不做任何 UI 操作。"""
    out: Dict[str, Any] = {"group": group, "ok": False, "count": 0, "file": ""}
    if not username:
        out["error"] = "没有该群的 wxid，无法读库"
        return out
    picked, offset, step = [], 0, 200
    while offset < cap and not _cancelled():
        try:
            rows = db.get_messages(username, limit=step, offset=offset)
        except Exception as e:
            out["error"] = "读库失败：%s" % e
            return out
        if not rows:
            break
        for m in rows:
            snd = str(m.get("sender_username") or "").strip()
            if snd and snd in ids:
                picked.append({
                    "time": m.get("create_time") or 0,
                    "type": m.get("local_type"),
                    "text": m.get("content") or "",
                    "sort_seq": m.get("sort_seq"),
                })
        if len(rows) < step:
            break
        offset += step
    picked.sort(key=lambda x: (x.get("sort_seq") or 0))
    date = time.strftime("%Y%m%d")
    d = os.path.join(data_dir, "myhistory", date)
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", group)[:24] or "group"
    p = os.path.join(d, safe + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"group": group, "username": username,
                   "self_ids": ids, "count": len(picked),
                   "messages": picked}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(d, safe + ".txt"), "w", encoding="utf-8") as f:
        for m in picked:
            ts = ""
            try:
                ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(int(m["time"])))
            except Exception:
                pass
            f.write("[%s] %s\n" % (ts, (m.get("text") or "").replace("\n", " ")))
    out.update(ok=True, count=len(picked), file=p)
    return out


# ---------------------------------------------------------------- 对外入口
def self_candidates(data_dir: str, nickname: str = "",
                    with_db: bool = True) -> List[str]:
    """全局兜底候选：手填（可选）> 设置 > 本地库 self_info。

    正常流程不用它——界面模式由 ``group_display_names`` 按群给名字。
    这里只在「库打不开」或前端想预览候选时用到。
    """
    out: List[str] = []
    raw = (nickname or "").strip()
    if raw:
        out += [x.strip() for x in re.split(r"[|\n]", raw) if x.strip()]
    try:
        import settings as St
        v = St.get(data_dir, "my_nickname") or ""
        out += [x.strip() for x in re.split(r"[|\n]", str(v)) if x.strip()]
    except Exception:
        pass
    if with_db:
        try:
            import contacts as C
            out += [x for x in global_self_names(C.get_db()) if x not in out]
        except Exception:
            pass
    return list(dict.fromkeys([x for x in out if x]))


def _worker(targets, nickname, mode, pages, shot_dir, data_dir, log_dir):
    import sender as S
    S.init_com()
    try:
        # 界面模式必须开库：不是为了发消息，而是为了自动读出
        # 「本人在每个群显示成什么名字」。用户不用填任何东西。
        db = None
        try:
            import contacts as C
            db = C.get_db()
        except Exception as e:
            JOB.log("读本地库失败：%s（界面模式退回全局昵称）" % e)

        per_group = group_display_names(db) if db is not None else {}
        fallback = self_candidates(data_dir, nickname,
                                   with_db=(db is None))
        if db is not None:
            fallback = list(dict.fromkeys(
                fallback + global_self_names(db)))
        JOB.log("自动识别 %d 个群的本人显示名" % len(per_group))

        with JOB.lock:
            JOB.state = "running"
            JOB.total = len(targets)
            JOB.message = "准备中…"

        # 界面模式只建一次 SendEngine（它会唤醒 gate，逐群重建太慢）
        engine = None
        if mode in ("ui", "both"):
            try:
                engine = S.SendEngine(should_stop=_cancelled)
            except Exception as e:
                with JOB.lock:
                    JOB.results.append({"group": "(引擎)",
                                        "ui": {"ok": False,
                                               "error": "控件树不可用：%s" % e}})
                engine = None

        for idx, t in enumerate(targets):
            if _cancelled():
                break
            name = t.get("name") or ""
            username = t.get("username") or ""
            # 每个群用自己的显示名；不同群昵称不一样也能各自对上
            nicks = list(per_group.get(username) or [])
            for g in fallback:
                if g not in nicks:
                    nicks.append(g)
            with JOB.lock:
                JOB.current = name
                JOB.message = "正在处理 %d/%d：%s" % (idx + 1, len(targets), name)
            rec: Dict[str, Any] = {"group": name, "names": nicks[:3]}
            if mode in ("ui", "both"):
                try:
                    if engine is None:
                        rec["ui"] = {"ok": False,
                                     "error": "微信控件树不可用，无法走界面截屏；"
                                              "可改用「本地库导出」"}
                    else:
                        rec["ui"] = _ui_one(engine, name, nicks, pages,
                                            shot_dir, log_dir)
                except Exception as e:
                    rec["ui"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
                    try:
                        with open(os.path.join(log_dir, "myhistory_error.log"),
                                  "a", encoding="utf-8") as f:
                            f.write(traceback.format_exc() + "\n")
                    except Exception:
                        pass
            if mode in ("db", "both") and db is not None:
                try:
                    rec["db"] = _db_one(db, name, username, _self_ids(db),
                                        data_dir)
                except Exception as e:
                    rec["db"] = {"ok": False, "error": str(e)}
            with JOB.lock:
                JOB.results.append(rec)
                JOB.done = idx + 1
        with JOB.lock:
            JOB.state = "cancelled" if JOB.stopped else "done"
            JOB.message = ("已取消" if JOB.stopped else
                           "完成 %d 个群" % JOB.done)
    except Exception as e:
        with JOB.lock:
            JOB.state = "error"
            JOB.message = str(e)
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "myhistory_error.log"),
                      "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass


def start(targets, nickname, mode, pages, shot_dir, data_dir, log_dir) -> Dict[str, Any]:
    with JOB.lock:
        if JOB.state == "running":
            return {"ok": False, "error": "已有任务在跑"}
        JOB.state = "running"
        JOB.stopped = False
        JOB.mode = mode
        JOB.total = len(targets or [])
        JOB.done = 0
        JOB.current = ""
        JOB.message = "启动中…"
        JOB.results = []
        JOB.started = time.time()
    th = threading.Thread(target=_worker,
                          args=(targets or [], nickname or "", mode, pages,
                                shot_dir, data_dir, log_dir), daemon=True)
    th.start()
    return {"ok": True, "started": True, "total": JOB.total}


def stop() -> Dict[str, Any]:
    with JOB.lock:
        JOB.stopped = True
        JOB.state = "cancelled"
    return {"ok": True}


def status() -> Dict[str, Any]:
    return JOB.snapshot()
