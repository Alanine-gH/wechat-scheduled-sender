# -*- coding: utf-8 -*-
"""本地 HTTP 服务（标准库实现，零额外依赖）。

由 Electron 主进程拉起，也可单独运行（浏览器模式）。

接口：
  GET  /                     前端页面（浏览器模式）
  GET  /api/paths            各用户目录位置（都在安装目录下）
  GET  /api/accounts         本机所有微信账号
  GET  /api/contacts?account= 该账号的群聊 + 好友
  GET  /api/messages         当前消息池
  POST /api/messages         保存消息池
  POST /api/plan             生成当日计划（预览）
  POST /api/start            开始执行
  POST /api/stop             停止执行
  GET  /api/status           运行状态与日志
  GET  /api/wechat           微信窗口检测
  GET  /shots/<file>         截图

鉴权：Electron 模式会传入 --token，所有 /api/* 需带 X-Token 头；
      浏览器模式（无 token）不强制，方便本地调试。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List
from urllib.parse import parse_qs, urlparse

# 必须在导入 sender / contacts 之前完成路径重定向
import userdata

userdata.configure(os.environ.get("WCS_DATA_ROOT"))

import campaign as campaign_mod  # noqa: E402
import contacts as contacts_mod  # noqa: E402
import guard as guard_mod  # noqa: E402
import inbox as inbox_mod  # noqa: E402
import leads as leads_mod  # noqa: E402
import media as media_mod  # noqa: E402
import planner  # noqa: E402
import report as report_mod  # noqa: E402
import scheduler  # noqa: E402
from sender import SendEngine, find_wechat_window  # noqa: E402

ROOT = userdata.data_root()
DATA_DIR = userdata.sub(userdata.DIR_DATA)
LOG_DIR = userdata.sub(userdata.DIR_LOGS)
SHOT_DIR = userdata.sub(userdata.DIR_SHOTS)
CACHE_DIR = userdata.sub(userdata.DIR_CACHE)
FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)
MESSAGES_FILE = os.path.join(DATA_DIR, "messages.txt")
CAMPAIGN_FILE = os.path.join(DATA_DIR, "campaign.txt")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

AUTH_TOKEN = ""   # 由 main() 注入


def read_mode() -> str:
    """读取用户选择的解析模式（line / separator）。"""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            m = (json.load(f) or {}).get("message_mode")
        if m in (planner.MODE_LINE, planner.MODE_SEPARATOR):
            return m
    except Exception:
        pass
    return planner.MODE_LINE


def read_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def schedule_config() -> Dict[str, Any]:
    """调度配置：起始时刻 / 钩子延迟 / 每周上限 / 允许投放的星期。

    没配过就用 scheduler 的默认值（方案给的：20:00、30 分钟、周二四六）。
    """
    s = read_settings()
    return {
        "start_at": s.get("start_at") or scheduler.DEFAULT_START_AT,
        "hook_delay_minutes": float(s.get("hook_delay_minutes")
                                    or scheduler.DEFAULT_HOOK_DELAY_MIN),
        "weekly_cap": int(s.get("weekly_cap") or scheduler.DEFAULT_WEEKLY_CAP),
        "hook_days": s.get("hook_days") or list(campaign_mod.DEFAULT_HOOK_DAYS),
        "run_days": s.get("run_days") or [],
    }


def write_settings(**kw) -> None:
    data = {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    data.update(kw)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class Runtime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.logs: list = []
        self.progress = {"round": 0, "total_rounds": 0, "sent": 0, "total": 0, "failed": 0}
        self.plan = None
        self.started_at = None

    def log(self, level: str, msg: str) -> None:
        item = {"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
        with self.lock:
            self.logs.append(item)
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]
        try:
            with open(os.path.join(LOG_DIR, "run-%s.log" % time.strftime("%Y%m%d")),
                      "a", encoding="utf-8") as f:
                f.write("[%s] %-5s %s\n" % (item["time"], level, msg))
        except Exception:
            pass


RT = Runtime()


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if c in bad else c for c in str(name))
    return out[:40] or "target"


def _send_one(engine, display: str, wxid, text: str, shot_dir: str,
              pair: dict, kind: str = "main",
              kind_label: str = "") -> bool:
    """发一条并记账（同时写投放日志）。

    返回 True 表示应该继续，False 表示被取消（应终止）。
    ``kind`` 区分 main/hook，供周报统计投放量。
    """
    shot_path = os.path.join(
        shot_dir, time.strftime("%Y%m%d"),
        "r%02d_%s_%s.png" % (pair.get("index") or 1, time.strftime("%H%M%S"),
                             _safe(display)),
    )
    res = engine.send_to(wxid or display, text, display=display,
                         username=wxid, shot_path=shot_path,
                         media_path=pair.get("media_path") or None,
                         media_first=bool(pair.get("media_first", True)))
    if res.get("cancelled"):
        RT.log("WARN", "已停止：中断于 %s" % display)
        return False

    ok = bool(res.get("ok"))
    # 投放日志：周报要靠它算「单群 7 条主文案 / 2-3 条钩子」的达成率
    try:
        report_mod.log_delivery(DATA_DIR, display, kind, ok)
    except Exception:
        pass

    if ok:
        RT.progress["sent"] += 1
        t = "已校验" if res.get("verified") or res.get("media_verified") else "未回读"
        extra = ""
        mp = pair.get("media_path")
        if mp:
            extra = " [素材 %s]" % os.path.basename(mp)
        head = ("%s " % kind_label) if kind_label else ""
        RT.log("OK", "%s已发送 -> %s [%s]%s | %s"
               % (head, display, t, extra,
                  (text or "(仅素材)")[:24].replace("\n", " ")))
    else:
        RT.progress["failed"] += 1
        RT.log("ERROR", "发送失败 -> %s : %s" % (display, res.get("error")))
    return True


def _run_events(engine, plan: dict) -> None:
    """按「绝对时间事件表」执行（内容编排模式）。

    事件已按时间排好序，逐条等到点再发。这样群 1 的钩子可以插在
    群 5 的主文案之前 —— 而不是把所有钩子堆到全部主文案之后。
    """
    events = plan.get("events") or []
    RT.progress.update({"total": len(events), "sent": 0, "failed": 0,
                        "total_rounds": 1, "round": 1})
    RT.log("INFO", "===== 内容编排模式：%d 个事件，起始 %s ====="
           % (len(events), plan.get("start_at")))

    for i, ev in enumerate(events):
        if not RT.running:
            RT.log("WARN", "收到停止指令，终止执行")
            break
        # 等到事件时间点；等待期间可被停止打断
        try:
            at = datetime.fromisoformat(ev["at"])
        except Exception:
            at = None
        if at is not None:
            while RT.running:
                gap = (at - datetime.now()).total_seconds()
                if gap <= 0:
                    break
                if gap > 5:
                    RT.log("INFO", "等待 %.0f 秒后发送 %s -> %s"
                           % (gap, ev.get("kind"), ev.get("target")))
                time.sleep(min(2.0, max(0.2, gap)))
        if not RT.running:
            break

        pair = {
            "index": 1,
            "target": ev.get("target"),
            "media_path": ev.get("media_path") or "",
            "media_first": True,
        }
        wxid = (plan.get("username_of") or {}).get(ev.get("target") or "")
        kind = ev.get("kind") or "main"
        label = {"main": "[主文案]", "hook": "[钩子]"}.get(kind, "")
        if not _send_one(engine, ev.get("target") or "", wxid,
                         ev.get("text") or "", SHOT_DIR, pair,
                         kind=kind, kind_label=label):
            break
        # 计时用的进度
        RT.progress["round"] = 1
        if i + 1 >= len(events):
            break
        time.sleep(1.2)


def _run_plan(plan: dict) -> None:
    RT.running = True
    RT.started_at = time.time()
    try:
        # 把「是否已请求停止」透给引擎：一次发送最长几十秒，
        # 只在两条之间判停止会导致「点了停止还在发」（实测停了还在跑 42 秒）。
        engine = SendEngine(should_stop=lambda: not RT.running)
        RT.log("INFO", "已连接微信窗口 hwnd=%s" % engine.hwnd)
    except Exception as exc:
        RT.log("ERROR", "初始化发送引擎失败: %s" % exc)
        RT.running = False
        return

    # 内容编排模式：按绝对时间事件表跑
    if plan.get("mode") == "campaign" and plan.get("events"):
        try:
            _run_events(engine, plan)
        except Exception as exc:
            RT.log("ERROR", "执行异常: %s" % exc)
        RT.log("INFO", "任务结束。成功 %d / 失败 %d" %
               (RT.progress["sent"], RT.progress["failed"]))
        # 记账供每周上限统计
        try:
            for t in (plan.get("targets") or []):
                scheduler.record_sent(t, DATA_DIR)
        except Exception:
            pass
        RT.running = False
        return

    rounds = plan.get("assignments") or []
    total = sum(len(r["pairs"]) for r in rounds)
    RT.progress.update({"total": total, "sent": 0, "failed": 0,
                        "total_rounds": len(rounds), "round": 0})

    for idx, rnd in enumerate(rounds):
        if not RT.running:
            RT.log("WARN", "收到停止指令，终止执行")
            break
        RT.progress["round"] = rnd["index"]
        RT.log("INFO", "===== 第 %d 轮开始（%d 条）=====" % (rnd["index"], len(rnd["pairs"])))

        for pair in rnd["pairs"]:
            if not RT.running:
                break

            # 相邻两次发送之间的随机等待（种子=日期，当天可复现）
            wait_s = float(pair.get("delay_seconds") or 0)
            if wait_s > 0:
                nxt = pair.get("target")
                RT.log("INFO", "等待 %.1f 分钟后发送 -> %s" % (wait_s / 60.0, nxt))
                slept = 0.0
                while slept < wait_s and RT.running:
                    time.sleep(min(2.0, wait_s - slept))
                    slept += 2.0
                if not RT.running:
                    break

            display = pair.get("target")
            wxid = pair.get("username")
            text = pair.get("text")
            pair = dict(pair, index=rnd["index"])
            if not _send_one(engine, display, wxid, text, SHOT_DIR, pair):
                break
            time.sleep(1.2)

    RT.log("INFO", "任务结束。成功 %d / 失败 %d" %
           (RT.progress["sent"], RT.progress["failed"]))
    RT.running = False


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatSender/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Electron 以 file:// 加载界面，需要允许跨源访问本服务
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _authed(self) -> bool:
        """校验 token；未配置 token（浏览器模式）时放行。"""
        if not AUTH_TOKEN:
            return True
        got = self.headers.get("X-Token") or ""
        if not got:
            q = parse_qs(urlparse(self.path).query)
            got = (q.get("token") or [""])[0]
        return got == AUTH_TOKEN

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain; charset=utf-8")

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p.startswith("/api/") and not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            if p in ("/", "/index.html"):
                return self._file(os.path.join(FRONTEND_DIR, "index.html"),
                                  "text/html; charset=utf-8")
            if p == "/api/paths":
                return self._json(userdata.describe())
            if p == "/api/accounts":
                return self._json({"accounts": contacts_mod.list_local_accounts()})
            if p == "/api/contacts":
                acc = (q.get("account") or [None])[0]
                return self._json(contacts_mod.get_contacts(acc))
            if p == "/api/messages":
                return self._json(planner.load_messages_detail(MESSAGES_FILE, read_mode()))
            if p == "/api/status":
                with RT.lock:
                    return self._json({"running": RT.running, "progress": RT.progress,
                                       "logs": RT.logs[-120:]})
            if p == "/api/wechat":
                h = find_wechat_window()
                return self._json({"found": bool(h), "hwnd": h})
            if p == "/api/campaign":
                # 内容编排预览：各天引导语、正文、钩子 + 当前调度配置 + 本周计数
                detail = campaign_mod.load_campaign_detail(CAMPAIGN_FILE)
                detail.update(schedule_config())
                detail["week"] = scheduler.week_key()
                detail["state"] = scheduler.load_state(DATA_DIR).get(
                    "weekly", {}).get(scheduler.week_key(), {})
                return self._json({"ok": True, **detail})
            if p == "/api/schedule":
                # 读取当前调度配置
                return self._json({"ok": True, **schedule_config()})
            if p == "/api/report":
                # 周报：投放量 + 线索量 + 6 项 KPI + 砍群建议
                return self._json({"ok": True, **report_mod.weekly_report(DATA_DIR)})
            if p == "/api/inbox":
                # 接收状态：各会话游标 + 本周线索概览
                st = inbox_mod.load_state(DATA_DIR)
                chats = st.get("chats") or {}
                return self._json({
                    "ok": True,
                    "chats": len(chats),
                    "detail": {v.get("name") or k: v.get("cursor")
                               for k, v in chats.items()},
                })
            if p == "/api/leads":
                # 线索表：列表 + 统计（可按状态/意图过滤）
                status = (q.get("status") or [None])[0]
                intent = (q.get("intent") or [None])[0]
                limit = int((q.get("limit") or ["200"])[0] or 200)
                return self._json({
                    "ok": True,
                    "leads": leads_mod.list_leads(DATA_DIR, status=status,
                                                  intent=intent, limit=limit),
                    "stats": leads_mod.stats(DATA_DIR),
                    "statuses": [{"key": k, "cn": leads_mod.STATUS_CN[k]}
                                 for k in leads_mod.STATUSES],
                })
            if p.startswith("/shots/"):
                rel = os.path.normpath(p[len("/shots/"):]).replace("..", "")
                return self._file(os.path.join(SHOT_DIR, rel), "image/png")
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:
            return self._json({"error": str(exc), "trace": traceback.format_exc()}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._read_json()
        try:
            if p.startswith("/api/") and not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            if p == "/api/messages":
                # 优先按原始文本保存（保留多行消息的换行）；
                # 兼容旧的 {messages: [...]} 调用方式。
                os.makedirs(DATA_DIR, exist_ok=True)
                mode = body.get("mode")
                if mode in (planner.MODE_LINE, planner.MODE_SEPARATOR):
                    write_settings(message_mode=mode)
                else:
                    mode = read_mode()
                if "text" in body:
                    raw = str(body.get("text") or "")
                else:
                    msgs_in = body.get("messages") or []
                    # 用 --- 连接，避免把多行消息压成一行
                    raw = ("\n%s\n" % planner.MESSAGE_SEPARATOR).join(
                        str(m) for m in msgs_in
                    )
                    if raw:
                        raw += "\n"
                if raw and not raw.endswith("\n"):
                    raw += "\n"
                with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
                    f.write(raw)
                detail = planner.load_messages_detail(MESSAGES_FILE, mode)
                return self._json({"ok": True, "count": detail["count"],
                                   "mode": detail["mode"]})

            if p == "/api/plan":
                msgs = planner.load_messages(MESSAGES_FILE, read_mode())
                targets = body.get("targets") or []
                # 合规校验：方案风控 1 要求群内不得出现违规开票暗示。
                # 在生成计划时就拦，别等发出去才发现。
                # 编排模式还要带上引导语与钩子（它们也是要发出去的内容）。
                check_items = [("消息池", m) for m in msgs]
                if body.get("campaign"):
                    camp_chk = campaign_mod.load_campaign(
                        body.get("campaign_file") or CAMPAIGN_FILE)
                    for k in campaign_mod.WEEKDAY_KEYS:
                        g = camp_chk.guides.get(k) or ""
                        if g:
                            check_items.append(
                                ("引导语(%s)" % campaign_mod.weekday_cn(k), g))
                    for i, h in enumerate(camp_chk.hooks or [], 1):
                        check_items.append(("钩子%d" % i, h))
                guard_hits = guard_mod.check_many(check_items)
                if not guard_hits["ok"]:
                    return self._json({
                        "ok": False, "error": "内容命中违规措辞，已拦下",
                        "guard": guard_hits}, 400)
                # 素材：按界面配置收集，再按目标轮转分配
                media_spec = body.get("media") or {}
                media_items = []
                media_by_target: Dict[str, List[str]] = {}
                if media_spec and targets:
                    try:
                        media_items = media_mod.collect(media_spec)
                    except Exception:
                        media_items = []
                    if media_items:
                        mode = (media_spec.get("assign_mode") or "rotate").lower()
                        seed = None
                        if mode == "shuffle":
                            seed = int(time.strftime("%Y%m%d"))
                        assign = media_mod.assign(media_items, targets, mode=mode, seed=seed)
                        media_by_target = {
                            k: [x.path for x in v] for k, v in assign.items()
                        }
                # ── 内容编排模式：绝对时间 + 双波次剧本 ──
                # body.campaign = true 时走这条；否则仍走原来的「轮次 + 相对间隔」
                if body.get("campaign"):
                    camp_path = body.get("campaign_file") or CAMPAIGN_FILE
                    camp = campaign_mod.load_campaign(camp_path)
                    cfg = schedule_config()
                    # body 里的值优先，没给就用配置里的
                    start_at = body.get("start_at") or cfg["start_at"]
                    hook_delay = float(body.get("hook_delay_min")
                                       or cfg["hook_delay_minutes"])
                    hook_days = body.get("hook_days") or cfg["hook_days"]
                    weekly_cap = int(body.get("weekly_cap")
                                     if body.get("weekly_cap") is not None
                                     else cfg["weekly_cap"])
                    run_days = body.get("run_days") or cfg.get("run_days") or []
                    plan = planner.build_campaign_plan(
                        targets=targets,
                        campaign_obj=camp,
                        start_at=start_at,
                        interval_min=float(body.get("interval_min")
                                           or planner.DEFAULT_INTERVAL_MIN),
                        interval_max=float(body.get("interval_max")
                                           or planner.DEFAULT_INTERVAL_MAX),
                        hook_delay_min=hook_delay,
                        hook_days=hook_days,
                        media_by_target=media_by_target,
                        weekly_cap=weekly_cap,
                        data_dir=DATA_DIR,
                    )
                    # 「只在指定星期执行」：今天不在名单里就产出空计划并说明原因
                    plan["run_days"] = run_days
                    if run_days and plan.get("weekday") not in run_days:
                        plan["events"] = []
                        plan["total_messages"] = 0
                        plan["main_count"] = 0
                        plan["hook_count"] = 0
                        plan["blocked_reason"] = (
                            "今天是%s，不在允许投放的星期（%s）里"
                            % (plan.get("weekday_cn"),
                               "、".join(run_days)))
                    plan["media"] = media_mod.describe(media_items) if media_items else None
                    plan["campaign_file"] = camp_path
                    plan["guard"] = guard_hits
                    RT.plan = plan
                    return self._json({"ok": True, "plan": plan})

                plan = planner.build_plan(
                    messages=msgs, targets=targets,
                    rounds=int(body.get("rounds") or planner.DEFAULT_ROUNDS),
                    per_round=int(body.get("per_round") or planner.DEFAULT_PER_ROUND),
                    interval_min=float(body.get("interval_min") or planner.DEFAULT_INTERVAL_MIN),
                    interval_max=float(body.get("interval_max") or planner.DEFAULT_INTERVAL_MAX),
                    media_by_target=media_by_target,
                    media_first=bool(media_spec.get("media_first", True)),
                )
                plan["media"] = media_mod.describe(media_items) if media_items else None
                plan["guard"] = guard_hits
                RT.plan = plan
                return self._json({"ok": True, "plan": plan})

            if p == "/api/guard":
                # 单独校验一段文本（界面里"改完先查一下"用）
                body = body or {}
                text = body.get("text") or ""
                if not text:
                    return self._json({"ok": False, "error": "缺少 text"}, 400)
                return self._json({"ok": True, **guard_mod.check(text)})

            if p == "/api/media":
                # 预览素材：只收集不发送，让用户确认选对了
                spec = body.get("media") or body or {}
                items = media_mod.collect(spec)
                return self._json({"ok": True, "media": media_mod.describe(items)})

            if p == "/api/schedule":
                body = body or {}
                # 保存调度配置（只写认识的字段，避免脏数据）
                patch: Dict[str, Any] = {}
                if "start_at" in body:
                    h, m = scheduler.parse_hhmm(body.get("start_at"))
                    patch["start_at"] = "%02d:%02d" % (h, m)
                if "hook_delay_minutes" in body:
                    patch["hook_delay_minutes"] = max(
                        0.0, float(body.get("hook_delay_minutes") or 0))
                if "weekly_cap" in body:
                    patch["weekly_cap"] = max(0, int(body.get("weekly_cap") or 0))
                if "hook_days" in body:
                    days = [str(d).lower() for d in (body.get("hook_days") or [])]
                    patch["hook_days"] = [
                        d for d in days if d in scheduler.WEEKDAY_KEYS]
                if "run_days" in body:
                    days = [str(d).lower() for d in (body.get("run_days") or [])]
                    patch["run_days"] = [
                        d for d in days if d in scheduler.WEEKDAY_KEYS]
                if patch:
                    write_settings(**patch)
                cfg = schedule_config()
                return self._json({"ok": True, **cfg})

            if p == "/api/schedule/reset":
                # 清空本周投放计数（比如手动重跑当天）
                state = scheduler.load_state(DATA_DIR)
                wk = scheduler.week_key()
                (state.get("weekly") or {}).pop(wk, None)
                scheduler.save_state(DATA_DIR, state)
                return self._json({"ok": True, "week": wk})

            if p == "/api/inbox/poll":
                # 轮询私聊新消息（读库，不注入进程）
                body = body or {}
                r = inbox_mod.poll_new(
                    account=body.get("account"),
                    data_dir=DATA_DIR,
                    include_history=bool(body.get("include_history")),
                )
                if r.get("error"):
                    return self._json({"ok": False, "error": r["error"]}, 500)
                items = r.get("items") or []
                # 命中的线索登记进线索表
                try:
                    r["added"] = leads_mod.add_items(items, DATA_DIR)
                except Exception as exc:
                    r["leads_error"] = str(exc)
                    r["added"] = 0
                r.pop("state", None)
                return self._json({"ok": True, **r})

            if p == "/api/inbox/reset":
                n = inbox_mod.reset_cursors(DATA_DIR)
                return self._json({"ok": True, "cleared": n})

            if p == "/api/leads/update":
                body = body or {}
                lid = str(body.get("id") or "")
                if not lid:
                    return self._json({"ok": False, "error": "缺少 id"}, 400)
                patch = {k: body[k] for k in ("status", "note", "source_group", "name")
                         if k in body}
                if not leads_mod.update_lead(DATA_DIR, lid, **patch):
                    return self._json({"ok": False,
                                       "error": "线索不存在或状态非法"}, 400)
                return self._json({"ok": True,
                                   "lead": leads_mod.get_lead(DATA_DIR, lid)})

            if p == "/api/leads/export":
                body = body or {}
                # 默认导到安装目录下的 data/，用户可指定其它路径
                out = body.get("path") or os.path.join(DATA_DIR, "leads.csv")
                r = leads_mod.export_csv(DATA_DIR, out,
                                         status=body.get("status"))
                return self._json(r, 200 if r.get("ok") else 500)

            if p == "/api/start":
                if RT.running:
                    return self._json({"ok": False, "error": "任务已在运行中"}, 409)
                plan = body.get("plan") or RT.plan
                if not plan or not (plan.get("assignments") or plan.get("events")):
                    return self._json({"ok": False, "error": "计划为空"}, 400)
                if not find_wechat_window():
                    return self._json({"ok": False, "error": "未找到微信窗口，请先登录微信"}, 400)
                RT.logs = []
                t = threading.Thread(target=_run_plan, args=(plan,), daemon=True)
                RT.thread = t
                t.start()
                return self._json({"ok": True})

            if p == "/api/stop":
                RT.running = False
                RT.log("WARN", "已请求停止")
                return self._json({"ok": True})

            return self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:
            return self._json({"error": str(exc), "trace": traceback.format_exc()}, 500)


def main(host="127.0.0.1", port=8791, token="", quiet=False):
    """启动服务。port=0 表示由系统分配空闲端口（Electron 用这种模式）。"""
    global AUTH_TOKEN
    AUTH_TOKEN = token or ""

    srv = ThreadingHTTPServer((host, port), Handler)
    actual_port = srv.server_address[1]

    # Electron 主进程靠这一行拿到端口
    print("PORT=%d" % actual_port, flush=True)
    if not quiet:
        print("  浏览器打开:  http://%s:%d" % (host, actual_port), flush=True)
        print("  用户文件目录: %s" % userdata.data_root(), flush=True)
        print("  关闭此窗口即停止服务", flush=True)
        print("-" * 64, flush=True)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="微信定时群发控制台 - 本地服务")
    ap.add_argument("port", nargs="?", type=int, default=8791,
                    help="端口，0 表示自动分配（默认 8791）")
    ap.add_argument("--token", default="", help="API 鉴权 token（留空则不校验）")
    ap.add_argument("--data-root", default="", help="用户文件根目录（默认安装目录）")
    ap.add_argument("--quiet", action="store_true", help="只输出 PORT=")
    args = ap.parse_args()

    if args.data_root:
        os.environ["WCS_DATA_ROOT"] = args.data_root
        userdata.configure(args.data_root)

    main(port=args.port, token=args.token, quiet=args.quiet)
