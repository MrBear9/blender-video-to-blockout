"""debuglog.py — 把 AI 链路的每一步写到固定日志文件

为什么需要：UI 上显示的内容用户要手动转述，容易丢信息。
写文件后，排查时直接读文件，不依赖截图或复述。

路径：<Blender 用户配置目录>/blockout_helper/ai_debug.log
Windows 一般是：
    C:\\Users\\<你>\\AppData\\Roaming\\Blender Foundation\\Blender\\4.5\\config\\blockout_helper\\ai_debug.log

安全：**绝不写 API Key**（写的话只写前 4 位 + 长度）。
"""

import os
import time

import bpy

_MAX_BYTES = 2 * 1024 * 1024        # 2 MB 后轮转


def log_dir():
    try:
        base = bpy.utils.user_resource("CONFIG")   # .../config
    except Exception:                              # noqa: BLE001
        base = os.path.expanduser("~")
    d = os.path.join(base, "blockout_helper")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.join(os.path.expanduser("~"), "blockout_helper")
        os.makedirs(d, exist_ok=True)
    return d


def log_path():
    return os.path.join(log_dir(), "ai_debug.log")


def redact_key(key):
    """脱敏：只留前 4 位和长度。"""
    k = (key or "").strip()
    if not k:
        return "(空)"
    if len(k) <= 8:
        return "(太短:%d)" % len(k)
    return "%s...(%d位)" % (k[:4], len(k))


def write(msg):
    """追加一行。任何异常都不能影响主流程。"""
    try:
        p = log_path()
        if os.path.exists(p) and os.path.getsize(p) > _MAX_BYTES:
            try:
                os.replace(p, p + ".1")
            except OSError:
                pass
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except Exception:                      # noqa: BLE001
        pass


def banner(text):
    write("=" * 60)
    write(text)


def clear():
    try:
        p = log_path()
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass
