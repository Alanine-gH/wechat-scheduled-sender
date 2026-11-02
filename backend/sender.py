# -*- coding: utf-8 -*-
"""发送引擎：基于 UIA 语义控件驱动微信客户端（不使用鼠标坐标 / 不依赖窗口位置）。

核心机制：
1. 热激活微信 Qt accessibility gate（Weixin.dll 里的 active byte），让 mmui:: 控件树
   实体化。微信重启/升级/重登后该字节会重置为 0，因此每次运行前都必须重写并校验。
2. 通过 AutomationId 定位控件（session_list / chat_input_field / 发送按钮），
   用 InvokePattern / ValuePattern 语义调用 —— 窗口移动、缩放完全不影响。
3. 目标优先用 wxid（全局唯一；昵称存在重名，实测 169 个），必要时降级到显示名。
4. 发送后可选做数据库回读校验（真实落库才算成功），并截图存档。
"""
from __future__ import annotations

import ctypes
import os
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


def find_wechat_window() -> Optional[int]:
    if win32gui is None:
        return None
    found: List[tuple] = []

    def _cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            if WECHAT_MAIN_CLASS not in cls:
                return True
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if (r - l) < 300 or (b - t) < 300:
                return True
            title = win32gui.GetWindowText(hwnd)
            if not any(x in title for x in WECHAT_TITLE_HINTS):
                return True
            found.append((hwnd, (r - l) * (b - t)))
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
    """检查控件树是否已实体化。

    判定依据：树里出现 >= need 个 mmui:: 控件。
    不用 session_list / MainView 这类页面相关控件 —— 界面停在通讯录、
    朋友圈等页面时它们不存在，会误判成「gate 未生效」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            root = auto.ControlFromHandle(hwnd)
            if root is not None and _count_mmui(root, 4) >= need:
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
        self._ensure_uia()

    def _ensure_uia(self) -> None:
        ensure_window_visible(self.hwnd)
        if not wake_accessibility(self.hwnd):
            raise RuntimeError(
                "微信控件树未能实体化（accessibility gate 未生效）。"
                "请确认微信已登录；若刚重启过微信，重试本程序即可。"
            )
        self.root = auto.ControlFromHandle(self.hwnd)
        if self.root is None:
            raise RuntimeError("无法附加到微信窗口")

    # -- 控件查找 ----------------------------------------------------------
    def _search_box(self):
        return self.root.EditControl(searchDepth=30, ClassName="mmui::XValidatorTextEdit")

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

    def _dismiss_popover(self) -> None:
        for _ in range(3):
            if self._search_box().Exists(0.2):
                return
            try:
                auto.SendKeys("{Esc}", waitTime=0.2)
            except Exception:
                pass
            time.sleep(0.5)

    # -- 打开会话 ----------------------------------------------------------
    def open_chat(self, target: str, display: Optional[str] = None,
                  timeout: float = 12.0) -> bool:
        """打开会话。target 优先按 wxid 搜索，失败则用显示名。"""
        self._dismiss_popover()
        keywords: List[str] = [k for k in (target, display) if k]
        # 去重
        uniq: List[str] = []
        for k in keywords:
            if k not in uniq:
                uniq.append(k)

        # 先试会话列表直接命中（用显示名）
        for kw in uniq:
            if self._click_session_item(kw):
                if self._verify_chat(kw, display, timeout):
                    return True

        box = self._search_box()
        if not box.Exists(0.5):
            raise RuntimeError("未找到搜索框，控件树可能不完整")

        for kw in uniq:
            try:
                box.SetFocus()
                time.sleep(0.2)
                box.SendKeys("{Ctrl}a", waitTime=0.05)
                box.SendKeys("{Delete}", waitTime=0.05)
                time.sleep(0.25)
                box.SendKeys(kw, waitTime=0.05)
                time.sleep(1.5)
            except Exception:
                continue

            lst = self._search_list()
            if lst is not None:
                items = lst.GetChildren()
                # 精确匹配优先（避免重名命中错人）
                exact = None
                first_chat = None
                for it in items:
                    try:
                        nm = (it.Name or "").strip()
                        if nm and nm != "搜索网络结果":
                            if first_chat is None:
                                first_chat = it
                            if display and nm == display:
                                exact = it
                                break
                            if nm == kw:
                                exact = it
                                break
                    except Exception:
                        continue
                pick = exact or first_chat
                if pick is not None:
                    try:
                        pick.Click()
                    except Exception:
                        pass
                    time.sleep(1.3)
                    if self._verify_chat(kw, display, timeout):
                        return True

            # 退化：回车进入
            try:
                box.SetFocus()
                box.SendKeys("{Enter}", waitTime=0.1)
                time.sleep(1.5)
            except Exception:
                pass
            if self._verify_chat(kw, display, timeout):
                return True

        return False

    def _click_session_item(self, target: str) -> bool:
        lst = self._session_list()
        if lst is None:
            return False
        try:
            items = lst.GetChildren()
        except Exception:
            return False
        for it in items:
            try:
                aid = it.AutomationId or ""
                name = (it.Name or "").split("\n")[0].strip()
                if aid == "session_item_%s" % target or name == target:
                    try:
                        it.GetInvokePattern().Invoke()
                    except Exception:
                        it.Click()
                    time.sleep(1.2)
                    return True
            except Exception:
                continue
        return False

    def _verify_chat(self, kw: str, display: Optional[str], timeout: float = 12.0) -> bool:
        """校验当前会话是否为目标（防止重名发错）。"""
        names = [n for n in (kw, display) if n]
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self.current_chat_name()
            if cur:
                for n in names:
                    if n in cur or cur in n:
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
                res["error"] = "打开会话失败或未能校验目标（可能重名或名称变更）"
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
