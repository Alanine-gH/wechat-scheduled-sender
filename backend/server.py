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

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 必须在导入 sender / contacts 之前完成路径重定向
import userdata

userdata.configure(os.environ.get("WCS_DATA_ROOT"))

import contacts as contacts_mod  # noqa: E402
import planner  # noqa: E402
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


def _run_plan(plan: dict) -> None:
    RT.running = True
    RT.started_at = time.time()
    try:
        engine = SendEngine()
        RT.log("INFO", "已连接微信窗口 hwnd=%s" % engine.hwnd)
    except Exception as exc:
        RT.log("ERROR", "初始化发送引擎失败: %s" % exc)
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
            shot_path = os.path.join(
                SHOT_DIR, time.strftime("%Y%m%d"),
                "r%02d_%s_%s.png" % (rnd["index"], time.strftime("%H%M%S"), _safe(display)),
            )
            res = engine.send_to(wxid or display, text, display=display,
                                 username=wxid, shot_path=shot_path)
            if res.get("ok"):
                RT.progress["sent"] += 1
                tag = "已校验" if res.get("verified") else ("未回读" if wxid else "无wxid")
                RT.log("OK", "已发送 -> %s [%s] | %s" % (display, tag, text[:24].replace("\n", " ")))
            else:
                RT.progress["failed"] += 1
                RT.log("ERROR", "发送失败 -> %s : %s" % (display, res.get("error")))
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
                plan = planner.build_plan(
                    messages=msgs, targets=targets,
                    rounds=int(body.get("rounds") or planner.DEFAULT_ROUNDS),
                    per_round=int(body.get("per_round") or planner.DEFAULT_PER_ROUND),
                    interval_min=float(body.get("interval_min") or planner.DEFAULT_INTERVAL_MIN),
                    interval_max=float(body.get("interval_max") or planner.DEFAULT_INTERVAL_MAX),
                )
                RT.plan = plan
                return self._json({"ok": True, "plan": plan})

            if p == "/api/start":
                if RT.running:
                    return self._json({"ok": False, "error": "任务已在运行中"}, 409)
                plan = body.get("plan") or RT.plan
                if not plan or not plan.get("assignments"):
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
