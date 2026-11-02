# -*- coding: utf-8 -*-
"""启动器：检查依赖 -> 打印中文横幅 -> 启动 HTTP 服务。

为什么要有这个文件：
    Windows 的 cmd.exe 按系统 ANSI 代码页（中文系统为 936/GBK）逐字节读取 .bat，
    如果 .bat 里含 UTF-8 中文，某些尾字节会落在 GBK 前导字节区间并吃掉后面的换行，
    导致批处理行错位、把中文碎片当成命令执行。
    因此 .bat 保持纯 ASCII，所有中文提示都由本脚本输出
    （Python 在 Windows 控制台走 WriteConsoleW，不受代码页影响）。
"""
from __future__ import annotations

import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(BASE, "backend")
REQUIREMENTS = os.path.join(BASE, "requirements.txt")

# 导入名 -> 便于提示的包名
REQUIRED = [
    ("wechatauto", "wechatauto-replica"),
    ("uiautomation", "uiautomation"),
    ("win32gui", "pywin32"),
    ("PIL", "Pillow"),
    ("psutil", "psutil"),
]


def _safe_console():
    """避免个别不可编码字符直接抛 UnicodeEncodeError 把启动打断。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def say(msg: str = "") -> None:
    """print 并立即 flush —— 管道/重定向下也能实时看到提示。"""
    print(msg, flush=True)


def set_title(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def missing() -> list:
    miss = []
    for mod, pkg in REQUIRED:
        try:
            __import__(mod)
        except Exception:
            miss.append(pkg)
    return miss


def main() -> int:
    _safe_console()
    set_title("微信定时群发控制台")

    if not os.path.isdir(BACKEND):
        say("[错误] 找不到 backend 目录，请确认文件完整：%s" % BACKEND)
        return 1

    say("=" * 64)
    say("  微信定时群发控制台  ·  浏览器模式（调试用）")
    say("=" * 64)
    say()
    say("  说明：正式使用请双击「启动.bat」运行 Electron 桌面版。")
    say("        本模式不启动 Electron，直接在浏览器里打开界面。")
    say()

    miss = missing()
    if miss:
        say("[提示] 缺少依赖：%s" % "、".join(miss))
        say("       正在安装，请稍候…")
        say()
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS])
        if rc != 0:
            say()
            say("[错误] 依赖安装失败。请手动执行：")
            say("       python -m pip install -r requirements.txt")
            return 1
        miss = missing()
        if miss:
            say()
            say("[错误] 仍然缺少：%s" % "、".join(miss))
            return 1
        say()
        say("[完成] 依赖已就绪")

    port = 8791
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            say("[提示] 端口参数无效，改用默认端口 8791")

    os.chdir(BACKEND)
    sys.path.insert(0, BACKEND)

    try:
        import server
    except Exception as exc:
        say()
        say("[错误] 加载服务失败：%s" % exc)
        import traceback
        traceback.print_exc()
        return 1

    try:
        server.main(port=port)
    except OSError as exc:
        say()
        say("[错误] 端口 %d 无法使用：%s" % (port, exc))
        say("       可换端口启动：启动.bat 8792")
        return 1
    say()
    say("服务已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())