# -*- coding: utf-8 -*-
"""发送引擎：基于 UIA 语义控件驱动微信客户端（不使用鼠标坐标 / 不依赖窗口位置）。

核心机制：
1. 热激活微信 Qt accessibility gate（Weixin.dll 里的 active byte），让 mmui:: 控件树
   实体化。微信重启/升级/重登后该字节会重置为 0，因此每次运行前都必须重写并校验。
2. 通过 AutomationId 定位控件（session_list / chat_input_field / 发送按钮），
   用 InvokePattern / ValuePattern 语义调用 —— 窗口移动、缩放完全不影响。
3. 打开会话**只用显示名**，不用 wxid：微信搜索框不认 wxid，输入它只会命中
   「搜索网络结果」并打开搜一搜面板。wxid 仅用于发送后的数据库回读校验。
4. 发送后可选做数据库回读校验（真实落库才算成功），并截图存档。
"""
from __future__ import annotations

import ctypes
import difflib
import os
import re
import time
from typing import Any, Dict, List, Optional

import uiautomation as auto

try:
    import win32gui
    import win32con
except Exception:  # pragma: no cover
    win32gui = None

WECHAT_MAIN_CLASS = "Qt51514QWindowIcon"
WECHAT_TITLE_HINTS = ("微信", "Weixin", "WeChat")
PW_RENDERFULLCONTENT = 0x00000002


# ---------------------------------------------------------------------------
# 名称归一化：微信搜索框对长名字 / emoji / 特殊符号很不友好
# ---------------------------------------------------------------------------
# 去掉空白、零宽字符、emoji、以及各种中英文装饰符号。
# 群名里常见的 【】& 1️⃣ 之类会让「整名搜索」搜不到，
# 所以改成「用归一化后的短关键词检索，再用模糊匹配认行」。
_STRIP_RE = re.compile(
    r"[\s\u200b-\u200f\u2028-\u202f\u20d0-\u20ff\u2600-\u27bf\ufe0e\ufe0f"
    r"\U0001F000-\U0001FAFF"
    r"【】\[\]（）()<>《》「」『』·、，,。.：:；;！!？?…\-—_=+*#@/\\|~^&%$'\"`^]+"
)


def normalize_name(s: Optional[str]) -> str:
    """把名称压成「只剩实义字符」的形式，用于搜索与比较。"""
    return _STRIP_RE.sub("", s or "")


def search_keys(name: str, max_len: int = 6) -> List[str]:
    """给一个显示名，产出若干个检索词（按可用性排序）。

    优先用归一化后的前几个字（区分度通常最高）；不行再试尾部和原文。
    """
    norm = normalize_name(name)
    out: List[str] = []
    cands = []
    if norm:
        cands.append(norm[:max_len])
        if len(norm) > max_len:
            cands.append(norm[-max_len:])
        cands.append(norm)
    cands.append((name or "")[:max_len])
    for k in cands:
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------
def init_com() -> None:
    """在当前线程初始化 COM。

    uiautomation 依赖 COM；在后台线程里创建控件对象前必须调用，
    否则会拿不到控件树（表现为「gate 未生效」）。可重复调用。
    """
    try:
        ctypes.windll.ole32.CoInitializeEx(0, 0x2)  # COINIT_APARTMENTTHREADED
    except Exception:
        pass


def find_wechat_window(restore: bool = True) -> Optional[int]:
    """找到微信主窗口句柄。

    注意最小化的情况：窗口最小化时 GetWindowRect 会返回图标尺寸（实测 160x28），
    如果按「尺寸必须 > 300」直接筛掉，就会得出「没找到微信」的错误结论 ——
    而恢复逻辑在查找之后才执行，永远没机会跑。
    所以这里遇到尺寸过小的候选时，先判断是否最小化，是就恢复再测一次。
    """
    if win32gui is None:
        return None
    found: List[tuple] = []

    def _size(hwnd):
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return r - l, b - t

    def _cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            if WECHAT_MAIN_CLASS not in cls:
                return True
            title = win32gui.GetWindowText(hwnd)
            if not any(x in title for x in WECHAT_TITLE_HINTS):
                return True

            w, h = _size(hwnd)
            if (w < 300 or h < 300) and restore and win32gui.IsIconic(hwnd):
                # 最小化 → 先恢复，再重新量尺寸
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.35)
                except Exception:
                    pass
                w, h = _size(hwnd)

            if w < 300 or h < 300:
                return True
            found.append((hwnd, w * h))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return max(found, key=lambda x: x[1])[0] if found else None


def ensure_window_visible(hwnd: int) -> None:
    if win32gui is None:
        return
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.8)


# 本程序完整验证过的微信版本。微信升级会改 Weixin.dll，
# accessibility gate（控件树开关）的位置随之变化，新版本可能扫不准。
VERIFIED_WECHAT_VERSIONS = ("4.1.15.13", "4.1.15.53")


_VERSION_DIR_RE = re.compile(r"^\d+\.\d+(\.\d+)*$")


def detect_wechat_version() -> Optional[str]:
    """推断正在运行的微信版本（形如 ``4.2.0.11``）。

    微信 4.x 有两种安装结构，两种都要认：
      * ``<安装目录>\\<版本>\\Weixin.exe``     —— exe 就在版本目录里
      * ``<安装目录>\\Weixin.exe`` + ``<安装目录>\\<版本>\\Weixin.dll``
        —— exe 是启动器，真正带版本的是子目录里的 Weixin.dll
        （本机就是这种：``C:\\soft\\Weixin\\Weixin.exe`` 与 ``4.2.0.11\\``）
    """
    try:
        import psutil
    except Exception:
        return None

    exe = None
    try:
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if (p.info.get("name") or "").lower().startswith("weixin"):
                    e = p.info.get("exe")
                    if e:
                        exe = e
                        break
            except Exception:
                continue
    except Exception:
        return None
    if not exe:
        return None

    base = os.path.dirname(exe)
    # 情况 A：exe 自身就在版本目录里
    if _VERSION_DIR_RE.match(os.path.basename(base)):
        return os.path.basename(base)

    # 情况 B：启动器旁边按版本命名的子目录，取 Weixin.dll 最新的那个
    try:
        cands = []
        for n in os.listdir(base):
            full = os.path.join(base, n)
            if not (os.path.isdir(full) and _VERSION_DIR_RE.match(n)):
                continue
            dll = os.path.join(full, "Weixin.dll")
            cands.append((os.path.getmtime(dll) if os.path.exists(dll) else 0, n))
        if cands:
            cands.sort(reverse=True)
            return cands[0][1]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# UIA gate 热激活
# ---------------------------------------------------------------------------
def wake_accessibility(hwnd: int, retries: int = 3) -> bool:
    """热激活 accessibility gate 并校验控件树是否实体化。

    微信重启/升级/重登后 gate byte 会重置为 0，控件树退回空壳；
    这里做多次重试，每次重新写入并校验。
    """
    for i in range(retries):
        try:
            from wechatauto import accessibility as _acc
            try:
                _acc.wake([hwnd], set_flag=True, verify=True)
            except Exception:
                pass
        except Exception:
            pass

        if _uia_ready(hwnd, timeout=4.0):
            return True
        # 窗口可能被最小化/遮挡，恢复后再试
        try:
            ensure_window_visible(hwnd)
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _count_mmui(root, max_depth: int = 4) -> int:
    """统计树里出现多少个 mmui:: 控件（gate 生效的直接证据）。"""
    n = 0
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for ch in children:
            try:
                if (ch.ClassName or "").startswith("mmui::"):
                    n += 1
            except Exception:
                pass
            stack.append((ch, depth + 1))
    return n


def _uia_ready(hwnd: int, timeout: float = 5.0, need: int = 3) -> bool:
    """检查控件树是否已实体化（gate 是否生效）。

    判定用两个信号，命中任一即可：

    1. 能按 AutomationId 找到主容器 ``MainView``。它是各页面（聊天/通讯录/
       朋友圈）共同的根容器，gate 生效就必然存在 —— 最可靠。
    2. 树里出现 >= need 个 ``mmui::`` 控件。

    注意第 2 条必须数到第 6 层：``mmui::MainView`` 本身就在第 4 层
    （窗口 → Pane → QWidget → QStackedWidget → MainView），
    只数 4 层会得到很少的计数，从而把「已就绪」误判成「gate 未生效」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            root = auto.ControlFromHandle(hwnd)
            if root is not None:
                if root.GroupControl(searchDepth=30, AutomationId="MainView").Exists(0.4):
                    return True
                if _count_mmui(root, 6) >= need:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
def capture_window(hwnd: int, out_path: str) -> bool:
    """PrintWindow 全内容渲染截图（不依赖窗口是否在前台/被遮挡）。"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        from PIL import Image
        import win32ui
    except Exception:
        return False
    if win32gui is None:
        return False

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return False

    hwnd_dc = mfc_dc = save_dc = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)

        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not ok:
            ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)

        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits,
                               "raw", "BGRX", 0, 1)
        img.save(out_path, "PNG")
        return True
    except Exception:
        return False
    finally:
        try:
            if save_dc:
                save_dc.DeleteDC()
            if mfc_dc:
                mfc_dc.DeleteDC()
            if hwnd_dc:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 发送引擎
# ---------------------------------------------------------------------------
class SendEngine:
    def __init__(self, hwnd: Optional[int] = None):
        # 必须先在当前线程初始化 COM，否则后台线程拿不到控件树
        init_com()
        self.hwnd = hwnd or find_wechat_window()
        if not self.hwnd:
            raise RuntimeError("未找到微信主窗口，请先启动并登录微信")
        self.root = None
        # 最近一次 open_chat 失败的原因（供日志展示，便于定位）
        self.last_open_error: Optional[str] = None
        self._ensure_uia()

    def _ensure_uia(self) -> None:
        """确保控件树可用。

        实测：gate 的唤醒**首次经常失败、隔几秒重试才成功**
        （用户日志里就是 16:13:57 失败、16:14:57 成功）。
        所以这里分多轮尝试，每轮之间给微信一点时间，
        而不是一次失败就把整个任务判死。
        """
        ensure_window_visible(self.hwnd)
        for attempt in range(1, 5):
            if wake_accessibility(self.hwnd, retries=1):
                self.root = auto.ControlFromHandle(self.hwnd)
                if self.root is not None:
                    return
            if attempt < 4:
                time.sleep(2.5)
        self.root = None

        ver = detect_wechat_version()
        hint = ""
        if ver and ver not in VERIFIED_WECHAT_VERSIONS:
            hint = ("\n检测到微信版本 %s；本程序只在 %s 上完整验证过。"
                    "微信升级会改变 accessibility gate 的位置，"
                    "可能导致控件树无法实体化。"
                    "建议关闭微信自动更新并退回已验证版本。"
                    % (ver, " / ".join(VERIFIED_WECHAT_VERSIONS)))
        elif ver:
            hint = "\n当前微信版本 %s（已验证版本）。" % ver
        raise RuntimeError(
            "微信控件树未能实体化（accessibility gate 未生效）。"
            "请确认微信已登录；若刚重启过微信，重试本程序即可。" + hint
        )

    # -- 控件查找 ----------------------------------------------------------
    def _search_box(self, min_width: int = 24):
        """取搜索输入框；不可用时返回 None（不抛异常）。

        微信 4.2.x 起搜索框控件可能换了类名，所以**不认死一个类名**，
        按可靠性依次尝试多种定位方式。

        另外搜索框会「缩起来」：窗口变窄、或拖动分隔条把会话列表压窄时，
        会折叠成只剩一个放大镜图标。这种控件宽度接近 0，
        SendKeys 打不进字却会让调用方以为「已经输入了关键词」，所以要拒绝。
        """
        cands = []
        try:
            cands.append(self.root.EditControl(
                searchDepth=30, ClassName="mmui::XValidatorTextEdit"))
            # 4.2.x 备选：按 AutomationId 找
            for aid in ("search_input", "search_field", "search_edit"):
                cands.append(self.root.EditControl(searchDepth=30, AutomationId=aid))
            # 再备选：搜索容器里的任意 EditControl
            sf = self.root.GroupControl(searchDepth=30, ClassName="mmui::XSearchField")
            if sf.Exists(0.2):
                cands.append(sf.EditControl(searchDepth=6))
        except Exception:
            return None

        for ed in cands:
            try:
                if ed is None or not ed.Exists(0.25):
                    continue
                r = ed.BoundingRectangle
                if (r.right - r.left) < min_width or (r.bottom - r.top) < 10:
                    continue
                return ed
            except Exception:
                continue
        return None

    def _ensure_search_box(self, timeout: float = 6.0):
        """尽最大努力拿到可用的搜索框，失败返回 None。

        依次尝试：
          1. 直接找（搜索框正常展开时命中）
          2. Ctrl+F —— 微信的「搜索」快捷键，会把搜索框唤出并聚焦
          3. 点 ``mmui::XSearchField`` 容器（把折叠状态展开）
          4. 点容器里的放大镜图标按钮
        每一步之后都重新检查一次，全部失败才放弃。
        """
        deadline = time.time() + timeout

        def ok():
            box = self._search_box()
            return box if box is not None and box.Exists(0.2) else None

        box = ok()
        if box is not None:
            return box

        attempts = (
            ("Ctrl+F 唤出搜索", lambda: auto.SendKeys("{Ctrl}f", waitTime=0.15)),
            ("点击搜索框容器", lambda: self._click_search_field()),
            ("点击放大镜图标", lambda: self._click_search_icon()),
        )
        for name, act in attempts:
            if time.time() >= deadline:
                break
            try:
                act()
            except Exception:
                pass
            time.sleep(0.9)
            box = ok()
            if box is not None:
                return box

        # 最后再等一等（有些布局要等动画结束才把 EditControl 渲染出来）
        while time.time() < deadline:
            box = ok()
            if box is not None:
                return box
            time.sleep(0.3)
        return None

    def _search_field_container(self):
        c = self.root.GroupControl(searchDepth=30, ClassName="mmui::XSearchField")
        return c if c.Exists(0.3) else None

    def _click_search_field(self) -> None:
        c = self._search_field_container()
        if c is not None:
            try:
                c.Click()
            except Exception:
                pass

    def _click_search_icon(self) -> None:
        c = self._search_field_container()
        if c is None:
            return
        try:
            for ch in c.GetChildren():
                if ch.ControlTypeName == "ButtonControl":
                    ch.Click()
                    return
        except Exception:
            pass

    def _search_list(self):
        lst = self.root.ListControl(searchDepth=30, AutomationId="search_list")
        return lst if lst.Exists(0.3) else None

    def _session_list(self):
        lst = self.root.ListControl(searchDepth=30, AutomationId="session_list")
        return lst if lst.Exists(0.3) else None

    def _input_field(self):
        ed = self.root.EditControl(searchDepth=30, ClassName="mmui::ChatInputField")
        return ed if ed.Exists(0.3) else None

    def current_chat_name(self) -> Optional[str]:
        """输入框的 Name 会反映当前会话名，用作发送前校验。"""
        try:
            f = self._input_field()
            if f is not None:
                return (f.Name or "").strip() or None
        except Exception:
            pass
        return None

    # 搜索结果里属于「网络搜索」的入口行。点中它们会打开搜一搜面板
    # （返回公众号文章），而不是打开会话。必须跳过。
    NETWORK_ROW_HINTS = ("搜索网络结果", "搜一搜", "网络结果", "搜索文章", "查找")

    def _escape_panels(self, times: int = 3) -> None:
        """按 Esc 关掉可能打开的浮层/搜一搜面板，把界面还原到可搜索状态。"""
        for _ in range(times):
            try:
                auto.SendKeys("{Esc}", waitTime=0.2)
            except Exception:
                pass
            time.sleep(0.45)

    def _dismiss_popover(self) -> None:
        """确保搜索浮层是干净的。

        先 Esc 收干净，再确认能拿到搜索框；拿不到就切回「微信」标签页
        （停在通讯录/朋友圈等页面时本来就没有搜索框）。
        """
        self._escape_panels(2)
        if self._search_box() is None:
            try:
                tab = self.root.ButtonControl(searchDepth=30, Name="微信")
                if tab.Exists(0.4):
                    tab.Click()
                    time.sleep(1.0)
            except Exception:
                pass
        return

    # -- 打开会话 ----------------------------------------------------------
    def _search_row_split(self, lst):
        """把搜索结果分成「聊天行」与「是否存在网络搜索入口」。

        返回 (list[(name, control)], 是否有网络入口)。
        """
        chats, has_network = [], False
        try:
            items = lst.GetChildren()
        except Exception:
            return chats, has_network
        for it in items:
            try:
                nm = (it.Name or "").strip()
            except Exception:
                continue
            if not nm:
                continue
            if any(h in nm for h in self.NETWORK_ROW_HINTS):
                has_network = True
                continue
            # 会话行会带换行分段（名称\n时间/预览），取首行做名称比较
            chats.append((nm.split("\n")[0].strip(), it))
        return chats, has_network

    def open_chat(self, target: str, display: Optional[str] = None,
                  timeout: float = 12.0) -> bool:
        """打开会话。

        **只用显示名搜索，绝不用 wxid**：微信搜索框不认 wxid，
        输入 wxid 只会命中「搜索网络结果」，点进去就是搜一搜面板
        （公众号文章列表），而不是会话 —— 这正是实际踩到的 bug。

        也不再用「回车进入」作为兜底：在搜索框里按回车同样会触发网络搜索。
        """
        kw = (display or target or "").strip()
        if not kw:
            return False

        # 只有 wxid / 群 id、没有显示名时，不要往搜索框里输 ——
        # 微信搜索框不认这类 id，只会弹出「搜索网络结果」。
        if kw.endswith("@chatroom") or kw.startswith(("wxid_", "gh_")):
            self._dismiss_popover()
            if self._click_session_item(kw) and self._verify_chat(kw, None, timeout):
                return True
            return False

        self._dismiss_popover()

        # 1) 会话列表里直接命中（最快，也最不容易出错）
        if self._click_session_item(kw) and self._verify_chat(kw, display, timeout):
            return True

        # 2) 本地搜索：逐个候选关键词试。
        #    微信搜索框对长名字 / emoji / 特殊符号很不友好 —— 群名里带
        #    【】&1️⃣ 时整名经常搜不到，所以先拿「归一化后的短关键词」检索，
        #    再对结果行做模糊匹配。短词没命中就换尾部、再换完整名。
        last_err = None
        for key in search_keys(kw):
            try:
                box = self._ensure_search_box()
                if box is None:
                    last_err = "搜索框不可用（可能被折叠）"
                    break
                box.SetFocus()
                time.sleep(0.2)
                box.SendKeys("{Ctrl}a", waitTime=0.05)
                box.SendKeys("{Delete}", waitTime=0.05)
                time.sleep(0.25)
                box.SendKeys(key, waitTime=0.05)
                time.sleep(1.5)
            except Exception as exc:
                last_err = "输入关键词失败: %s" % exc
                continue

            lst = self._search_list()
            if lst is None:
                last_err = "搜索结果面板未出现"
                continue

            chats, _has_network = self._search_row_split(lst)
            pick = self._pick_best_row(chats, kw, key)
            if pick is None:
                last_err = "关键词 %r 的结果里没有可靠匹配" % key
                continue

            try:
                pick.Click()
            except Exception:
                pass
            time.sleep(1.3)
            if self._verify_chat(key, display, timeout):
                return True
            last_err = "点了 %r 对应行，但会话标题校验没过" % key

        # 失败：清场，避免把搜一搜面板留在界面上影响下一条
        self._escape_panels()
        self.last_open_error = last_err
        return False

    def _pick_best_row(self, chats, display: str, key: str):
        """从搜索结果里挑出与目标最匹配的一行；不确定时返回 None。

        **宁可失败也不点错** —— 点错群的代价是把消息发到别人的群里。
        所以：归一化后完全相同 → 直接选；否则取相似度最高且明显领先的一行；
        若有两行分数接近（典型的「一堆相似群名」），放弃。
        """
        dn = normalize_name(display)
        scored = []
        for nm, it in chats:
            rn = normalize_name(nm)
            if not rn:
                continue
            if dn and rn == dn:
                score = 1.0
            elif dn and (dn in rn or rn in dn):
                score = 0.92
            else:
                score = difflib.SequenceMatcher(None, dn or key, rn).ratio()
            scored.append((score, nm, it))

        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        if best[0] < 0.60:
            return None
        if len(scored) > 1 and (best[0] - scored[1][0]) < 0.06:
            return None
        return best[2]

    def _scroll_session_list(self, lst) -> bool:
        """把会话列表往下翻一屏（语义滚动优先，不依赖坐标）。"""
        try:
            sp = lst.GetScrollPattern()
            if sp is not None:
                sp.Scroll(auto.ScrollAmount.NoAmount,
                          auto.ScrollAmount.LargeIncrement)
                return True
        except Exception:
            pass
        # 退化：把鼠标移到列表自身的中心再滚轮（位置取自控件矩形，不是写死的坐标）
        try:
            r = lst.BoundingRectangle
            if (r.right - r.left) > 10 and (r.bottom - r.top) > 10:
                lst.SetFocus()
                auto.WheelDown(wheelTimes=5, waitTime=0.1)
                return True
        except Exception:
            pass
        return False

    def _click_session_item(self, target: str, max_scroll: int = 4) -> bool:
        """在会话列表里找目标并点开。

        会话列表只渲染可见的若干行，目标可能被压在下面；搜索框又可能
        被折叠/换类名拿不到，所以这里支持**滚动若干屏**继续找，
        避免「搜索不可用就整条失败」。

        名称比较同时用原文与归一化形式，兼容群名里的 emoji / 符号。
        """
        lst = self._session_list()
        if lst is None:
            return False
        want = normalize_name(target)

        for _round in range(max_scroll + 1):
            try:
                items = lst.GetChildren()
            except Exception:
                return False
            for it in items:
                try:
                    aid = it.AutomationId or ""
                    name = (it.Name or "").split("\n")[0].strip()
                    hit = (aid == "session_item_%s" % target or name == target)
                    if not hit and want and len(want) >= 4:
                        hit = normalize_name(name) == want
                    if not hit:
                        continue
                    try:
                        it.GetInvokePattern().Invoke()
                    except Exception:
                        it.Click()
                    time.sleep(1.2)
                    return True
                except Exception:
                    continue
            if not self._scroll_session_list(lst):
                break
            time.sleep(0.6)
        return False

    def _verify_chat(self, kw: str, display: Optional[str], timeout: float = 12.0) -> bool:
        """校验当前会话是否为目标（防止重名发错）。

        搜索关键词往往是「去掉符号后的短片段」（例如用「代博会浦飞出海」去找
        群名「【代博会】浦飞出海&会计工厂1️⃣」），所以除了原文比较，
        还要把两边都归一化后再比一次，否则命中了也认不出来。
        """
        names = [n for n in (kw, display) if n]
        norms = [normalize_name(n) for n in names]
        norms = [d for d in norms if d]
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self.current_chat_name()
            if cur:
                for n in names:
                    if n in cur or cur in n:
                        return True
                cn = normalize_name(cur)
                if cn:
                    for d in norms:
                        if d == cn:
                            return True
                        # 片段匹配要求至少 4 个字，避免过短关键词误判
                        if len(d) >= 4 and (d in cn or cn in d):
                            return True
            time.sleep(0.4)
        return False

    # -- 发送 --------------------------------------------------------------
    def _send_button(self):
        """定位发送按钮。

        注意：该按钮（mmui::XOutlineButton, Name='发送'）只在输入框有内容时
        才会被渲染出来，因此必须在写入文本之后再查找。
        """
        iv = self.root.GroupControl(searchDepth=30, ClassName="mmui::ChatInputView")
        if not iv.Exists(0.3):
            return None
        btn = iv.ButtonControl(searchDepth=6, Name="发送")
        if btn.Exists(0.4):
            return btn
        # 退化：按类名找
        btn = iv.ButtonControl(searchDepth=6, ClassName="mmui::XOutlineButton")
        return btn if btn.Exists(0.3) else None

    def send_text(self, text: str) -> bool:
        """写入输入框并触发发送。返回 True 表示已触发（不代表已落库）。"""
        field = self._input_field()
        if field is None:
            raise RuntimeError("未找到输入框 chat_input_field")
        try:
            field.Click()
        except Exception:
            pass
        time.sleep(0.2)

        # 清空
        try:
            field.GetValuePattern().SetValue("")
        except Exception:
            try:
                field.SendKeys("{Ctrl}a", waitTime=0.05)
                field.SendKeys("{Delete}", waitTime=0.05)
            except Exception:
                pass

        # 写入（ValuePattern 避开剪贴板与输入法）
        try:
            field.GetValuePattern().SetValue(text)
        except Exception:
            try:
                field.SendKeys(text, waitTime=0.02)
            except Exception:
                return False

        # 等待按钮渲染出来
        time.sleep(0.6)
        btn = self._send_button()

        # 实测结论：该按钮的 InvokePattern 无效（输入框不会清空），
        # 必须用 Click（控件级点击，仍由 UIA 定位，不依赖屏幕坐标）或回车。
        if btn is not None:
            try:
                btn.Click()
                time.sleep(0.5)
                if self.input_is_empty():
                    return True
            except Exception:
                pass

        # 退化：回车发送
        try:
            field.SetFocus()
            field.SendKeys("{Enter}", waitTime=0.1)
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def input_is_empty(self) -> bool:
        """检查输入框是否已清空（发送成功的强信号）。"""
        try:
            f = self._input_field()
            if f is None:
                return False
            vp = f.GetValuePattern()
            # uiautomation 的 ValuePattern 用 Value 属性
            try:
                val = vp.Value
            except Exception:
                val = ""
            return not (val or "").strip()
        except Exception:
            return False

    def verify_in_db(self, username: Optional[str], text: str,
                     timeout: float = 8.0) -> bool:
        """数据库回读校验：确认消息真的落库（即真的发出去了）。"""
        if not username:
            return False
        probe = (text or "")[:16]
        if not probe:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                from wechatauto import WeChatDB
                db = WeChatDB()
                for m in db.get_messages(username, limit=6):
                    if probe in (m.get("content") or ""):
                        return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def send_to(self, target: str, text: str, display: Optional[str] = None,
                username: Optional[str] = None,
                shot_path: Optional[str] = None,
                verify: bool = True) -> Dict[str, Any]:
        """打开会话 -> 校验目标 -> 发送 -> 回读校验 -> 截图。

        判定成功的条件：打开成功 + 已触发发送 + (输入框已清空 或 回读命中)
        """
        res: Dict[str, Any] = {"target": display or target, "ok": False}
        try:
            if not self.open_chat(target, display=display):
                res["error"] = "打开会话失败：%s" % (
                    self.last_open_error or "未能校验目标（可能重名或名称变更）")
                return res

            triggered = self.send_text(text)
            if not triggered:
                res["error"] = "写入输入框或触发发送失败"
                return res

            time.sleep(0.8)
            cleared = self.input_is_empty()
            res["cleared"] = cleared

            verified = False
            if verify and username:
                verified = self.verify_in_db(username, text)
            res["verified"] = verified

            if shot_path:
                res["shot"] = shot_path if capture_window(self.hwnd, shot_path) else None

            if cleared or verified:
                res["ok"] = True
            else:
                res["error"] = "已触发发送，但输入框仍有内容且数据库未回读（可能发送失败）"
            return res
        except Exception as exc:
            res["error"] = str(exc)
            return res
