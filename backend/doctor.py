# -*- coding: utf-8 -*-
"""健康自检：一键诊断「为什么发不出去」。

为什么需要它：
这套工具依赖微信客户端、控件树、本地数据库解密三件事同时成立，
任何一环出问题都表现为「发送失败」。之前只能靠人工来回排查
（微信一升级控件树就失效，那次花了很久才定位）。
把已知的检查点固化成清单，出问题时能先自己看一眼。

设计要点：
* **每项检查独立容错** —— 一项抛异常不能拖垮整个诊断，
  否则最需要它的场景（环境已经坏了）恰恰用不了。
* 每项都给 ``hint``：不只是说「哪里不对」，还要说「该怎么办」。
* 分三档：``ok`` / ``warn``（能跑但有问题） / ``error``（跑不了）。
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Any, Callable, Dict, List, Optional

# 单次检查的说明与修复建议都写在这里，避免散落在流程代码里
OK = "ok"
WARN = "warn"
ERROR = "error"


def _check(name: str, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """跑一项检查，任何异常都变成 error 结果而不是往上抛。"""
    t0 = time.time()
    try:
        r = fn() or {}
    except Exception as exc:
        r = {"status": ERROR, "detail": "检查本身出错：%s" % exc}
    out = {
        "name": name,
        "status": r.get("status", OK),
        "detail": r.get("detail", ""),
        "hint": r.get("hint", ""),
        "ms": int((time.time() - t0) * 1000),
    }
    for k, v in r.items():
        if k not in out:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------
def _check_datadir(data_dir: str) -> Dict[str, Any]:
    if not os.path.isdir(data_dir):
        return {"status": ERROR, "detail": "数据目录不存在：%s" % data_dir,
                "hint": "首次运行请先启动一次程序，它会自动创建"}
    probe = os.path.join(data_dir, ".doctor_probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as exc:
        return {"status": ERROR, "detail": "数据目录不可写：%s" % exc,
                "hint": "检查目录权限，或把程序装到当前用户可写的位置"}
    return {"status": OK, "detail": data_dir}


def _check_disk(data_dir: str) -> Dict[str, Any]:
    try:
        use = shutil.disk_usage(data_dir if os.path.isdir(data_dir)
                                else os.path.dirname(data_dir) or ".")
    except Exception as exc:
        return {"status": WARN, "detail": "无法读取磁盘信息：%s" % exc}
    free_gb = use.free / 1073741824.0
    if free_gb < 1:
        return {"status": ERROR, "detail": "剩余空间 %.2f GB" % free_gb,
                "hint": "磁盘快满了，截图和日志会写不进去；先清理磁盘"}
    if free_gb < 5:
        return {"status": WARN, "detail": "剩余空间 %.2f GB" % free_gb,
                "hint": "建议在设置里调小「截图保留天数」"}
    return {"status": OK, "detail": "剩余 %.1f GB" % free_gb}


def _check_wechat_window() -> Dict[str, Any]:
    import sender as S
    # UIA / win32gui 都依赖 COM，而 COM 是**按线程**初始化的。
    # HTTP 处理线程没做过 CoInitialize，不显式初始化的话
    # 在这个线程上读控件树必然失败（自检一开始就报假警）。
    try:
        S.init_com()
    except Exception:
        pass
    hwnd = S.find_wechat_window()
    if not hwnd:
        return {"status": ERROR, "detail": "没找到微信窗口",
                "hint": "确认微信已登录且窗口没有最小化到托盘；"
                        "若用的是新版本微信，可能是控件树变化导致识别失败"}
    try:
        import win32gui
        rect = win32gui.GetWindowRect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
    except Exception:
        w = h = 0
    return {"status": OK, "detail": "hwnd=%s 尺寸=%dx%d" % (hwnd, w, h)}


def _check_wechat_version() -> Dict[str, Any]:
    import sender as S
    ver = S.detect_wechat_version()
    if not ver:
        return {"status": WARN, "detail": "未能识别微信版本",
                "hint": "不影响使用，但版本变化是「突然发不出去」最常见的原因"}
    verified = ver in getattr(S, "VERIFIED_WECHAT_VERSIONS", ())
    if verified:
        return {"status": OK, "detail": "微信 %s（已验证）" % ver}
    return {"status": WARN, "detail": "微信 %s（未在已验证列表里）" % ver,
            "hint": "大概率仍可用，但若控件树检查失败，请把版本号反馈以便适配"}


def _check_uia() -> Dict[str, Any]:
    import sender as S
    # 同上：控件树读取依赖 COM，本线程必须先初始化
    try:
        S.init_com()
    except Exception:
        pass
    hwnd = S.find_wechat_window()
    if not hwnd:
        return {"status": ERROR, "detail": "没有窗口可检查（见上一项）"}
    ok = False
    try:
        S.wake_accessibility(hwnd)
        ok = S._uia_ready(hwnd, timeout=6.0)
    except Exception as exc:
        return {"status": ERROR, "detail": "控件树检查出错：%s" % exc,
                "hint": "见 docs/排查记录-微信4.2.0.11控件树失效.md"}
    if ok:
        return {"status": OK, "detail": "微信控件树可正常读取"}
    return {"status": ERROR, "detail": "控件树无法实体化（读不到界面元素）",
            "hint": "这是「打开会话失败」最常见的原因。"
                    "重启微信后重试；仍不行请把微信版本与本文档一起反馈"}


def _check_wxdir(data_dir: str = "") -> Dict[str, Any]:
    """微信数据目录定位（「读不到登录的微信」最常见的原因就在这一项）。"""
    import wxdir
    import settings as S

    manual = ""
    try:
        manual = str(S.get(data_dir, "wechat_db_dir") or "") if data_dir else ""
    except Exception:
        manual = ""
    d = wxdir.diagnose(manual)
    if not d.get("db_dir"):
        hint = "在「设置 → 微信连接」里填微信的文件目录（微信：设置 → 文件管理 可看到路径）"
        if manual:
            hint = ("手填的目录没找到账号数据，确认路径正确（填到哪一级都行，"
                    "但目录里要有 xxx\\db_storage）；" + hint)
        return {"status": ERROR, "detail": "未找到微信数据目录", "hint": hint}

    accs = d.get("accounts") or []
    if not accs:
        return {"status": WARN,
                "detail": "找到目录 %s（%s），但里面没有账号数据"
                          % (d["db_dir"], d.get("source_cn") or ""),
                "hint": "可能微信刚登录还没建库；或目录指到了空壳位置"}
    return {"status": OK,
            "detail": "%s（%s）· %d 个账号"
                      % (d["db_dir"], d.get("source_cn") or "", len(accs)),
            "cue": {"db_dir": d["db_dir"], "source": d.get("source"),
                    "accounts": [a["account"] for a in accs]}}


def _check_database(account: Optional[str] = None) -> Dict[str, Any]:
    import contacts as C
    db = C.get_db(account)
    groups = C.list_groups(account)
    friends = C.list_friends(account)
    if not groups and not friends:
        return {"status": WARN, "detail": "数据库可读，但没读到任何群/好友",
                "hint": "确认当前登录账号；刚登录的账号可能要等微信同步完通讯录"}
    return {"status": OK,
            "detail": "账号 %s：%d 个群 / %d 个好友"
                      % (getattr(db, "account", "?"), len(groups), len(friends))}


def _check_gate_cache() -> Dict[str, Any]:
    """控件树「热激活」的缓存文件是否已生成。"""
    import sender as S
    path = getattr(S, "GATE_CACHE_FILE", "") or ""
    if not path:
        for cand in ("cache/gate_cache.json", "cache/keys/gate_cache.json"):
            if os.path.isfile(cand):
                path = cand
                break
    if not path or not os.path.isfile(path):
        return {"status": WARN, "detail": "还没有控件树缓存（首次发送时自动生成）",
                "hint": "属正常现象；若反复发送失败，删掉 cache/ 后重试可强制重新扫描"}
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    return {"status": OK, "detail": "%s（%d 字节）" % (path, size)}


def _check_settings(data_dir: str) -> Dict[str, Any]:
    import settings as ST
    s = ST.load(data_dir)
    problems: List[str] = []
    if s["interval_min"] > s["interval_max"]:
        problems.append("间隔下限大于上限")
    if s["send_window_start"] == s["send_window_end"] and s["enforce_window"]:
        problems.append("发送时段的起止相同（会被当成全天开放）")
    if not (s["hook_days"] or []):
        problems.append("没勾任何钩子投放日（钩子永远不会发）")
    if s["auto_enabled"] and not s["auto_check_seconds"]:
        problems.append("自动运行没设检查间隔")
    if problems:
        return {"status": WARN, "detail": "；".join(problems),
                "hint": "到「6 · 设置」里改正"}
    return {"status": OK, "detail": "配置可读且关键值合理"}


def _check_campaign(data_dir: str) -> Dict[str, Any]:
    import campaign as CM
    path = os.path.join(data_dir, "campaign.txt")
    if not os.path.isfile(path):
        return {"status": WARN, "detail": "没有 campaign.txt（内容编排不可用）",
                "hint": "从 data/campaign.example.txt 复制一份再填内容"}
    c = CM.load_campaign(path)
    if not c.guides and not c.body:
        return {"status": ERROR, "detail": "campaign.txt 解析不出内容",
                "hint": "检查章节标记是否为 '# === 引导语/正文/钩子 ==='"}
    missing = [k for k in CM.WEEKDAY_KEYS if k not in c.guides]
    if missing:
        return {"status": WARN,
                "detail": "正文 %d 行、钩子 %d 条，但缺 %s 的引导语"
                          % (len(c.body.split("\n")), len(c.hooks),
                             "、".join(missing)),
                "hint": "缺的那几天会只发正文"}
    return {"status": OK,
            "detail": "正文 %d 行、引导语 %d 条、钩子 %d 条"
                      % (len(c.body.split("\n")), len(c.guides), len(c.hooks))}


def _check_shots(data_dir: str, shot_dir: str,
                 shot_keep_days: int) -> Dict[str, Any]:
    if not os.path.isdir(shot_dir):
        return {"status": OK, "detail": "还没有截图目录（首次发送后生成）"}
    n = 0
    size = 0
    for root, _d, files in os.walk(shot_dir):
        for f in files:
            n += 1
            try:
                size += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    mb = size / 1048576.0
    if shot_keep_days <= 0:
        return {"status": WARN, "detail": "%d 张截图 / %.1f MB，但保留天数 <=0（不清理）"
                                          % (n, mb),
                "hint": "长期运行会一直涨，建议设成 30 天左右"}
    return {"status": OK, "detail": "%d 张截图 / %.1f MB（保留 %d 天）"
                                    % (n, mb, shot_keep_days)}


def _check_timezone() -> Dict[str, Any]:
    now = time.localtime()
    if not now.tm_isdst and time.strftime("%z") not in ("+0800",):
        pass
    return {"status": OK,
            "detail": "本机时间 %s（%s）"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), time.strftime("%z"))}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
CHECK_ORDER = (
    "数据目录", "磁盘空间", "微信窗口", "微信版本", "控件树",
    "本地数据库", "控件树缓存", "内容配置", "计划配置", "截图占用", "本机时间",
)


def run(data_dir: str, shot_dir: str, account: Optional[str] = None,
        shot_keep_days: int = 30) -> Dict[str, Any]:
    """跑完整诊断，返回 ``{checks, summary, ok}``。

    ``ok=False`` 表示存在 error 级问题（发不出去），
    warn 只表示「能跑但有隐患」。
    """
    checks: List[Dict[str, Any]] = [
        _check("数据目录", lambda: _check_datadir(data_dir)),
        _check("磁盘空间", lambda: _check_disk(data_dir)),
        _check("微信窗口", _check_wechat_window),
        _check("微信版本", _check_wechat_version),
        _check("微信数据目录", lambda: _check_wxdir(data_dir)),
        _check("控件树", _check_uia),
        _check("本地数据库", lambda: _check_database(account)),
        _check("控件树缓存", _check_gate_cache),
        _check("内容配置", lambda: _check_campaign(data_dir)),
        _check("计划配置", lambda: _check_settings(data_dir)),
        _check("截图占用", lambda: _check_shots(data_dir, shot_dir,
                                               shot_keep_days)),
        _check("本机时间", _check_timezone),
    ]
    errs = [c for c in checks if c["status"] == ERROR]
    warns = [c for c in checks if c["status"] == WARN]
    if errs:
        summary = "%d 项异常、%d 项提醒 —— 有阻塞性问题，先处理异常项" % (
            len(errs), len(warns))
    elif warns:
        summary = "全部可用，但有 %d 项提醒值得看一眼" % len(warns)
    else:
        summary = "全部正常，可以发送"
    return {
        "checks": checks,
        "ok": not errs,
        "errors": len(errs),
        "warnings": len(warns),
        "summary": summary,
        "at": int(time.time()),
    }