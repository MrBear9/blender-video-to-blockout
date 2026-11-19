"""dispatch.py —— 把 bpy 操作派发回主线程

为什么需要这个模块（**整个插件最关键的 20 行**）
------------------------------------------------
bpy 不是线程安全的。在后台线程里调用 bpy 的结果不是抛异常，而是**硬崩溃**
（Blender 直接退出，连 traceback 都没有）。

但 LLM 的 HTTP 请求必须放在后台线程，否则主线程被阻塞，界面完全冻死。

所以标准做法是：
    * HTTP / 解析 → 后台线程
    * 任何碰 bpy 的代码 → 用 bpy.app.timers 排回主线程

本模块只依赖 bpy 和 threading，**不 import 本包内任何东西**，
避免形成循环导入（实测症状是 "cannot import name X from partially
initialized module Y"，且报错模块名往往不是真正的起点）。

用法
----
    from . import dispatch

    # 后台线程里：
    result = dispatch.call_on_main(lambda: bpy.data.objects["Cube"].location)
    # 或
    result = dispatch.call_on_main(some_fn, arg1, arg2)

主线程里可直接调用（会自动识别，不排队，避免自己等自己）。
"""

import threading
import traceback

import bpy

#: 单次派发的默认超时（秒）。bpy 操作一般很快，超时多半是死锁。
DEFAULT_TIMEOUT = 60.0

#: 主线程标识。任何线程都能读，用来判断"我是不是已经在主线程"
_main_thread_id = None


def _capture_main_thread():
    global _main_thread_id
    _main_thread_id = threading.get_ident()


_capture_main_thread()


def is_main_thread():
    """当前是否运行在 Blender 主线程。"""
    return _main_thread_id is not None and threading.get_ident() == _main_thread_id


class DispatchResult:
    """一次派发的结果容器。"""

    __slots__ = ("value", "error", "traceback", "event")

    def __init__(self):
        self.value = None
        self.error = None
        self.traceback = None
        self.event = threading.Event()


def call_on_main(fn, *args, **kwargs):
    """在主线程执行 fn(*args, **kwargs) 并返回结果。

    * 已在主线程 → 直接执行
    * 在后台线程 → 通过 bpy.app.timers 排队，阻塞等待（带超时）

    超时会抛 TimeoutError，而不是永远挂住 —— 后台线程卡死比报错更难查。
    """
    timeout = kwargs.pop("_timeout", DEFAULT_TIMEOUT)

    if is_main_thread():
        return fn(*args, **kwargs)

    if not bpy.app.timers.is_registered(_TIMER_TICK) and _pending_empty():
        # 确保 tick 在跑（幂等注册）
        _ensure_tick()

    res = DispatchResult()

    def _run():
        try:
            res.value = fn(*args, **kwargs)
        except Exception as exc:               # noqa: BLE001 - 需要把异常带回后台线程
            res.error = exc
            res.traceback = traceback.format_exc()
        finally:
            res.event.set()
        return None                            # 返回 None = 不重复执行

    with _lock:
        _queue.append(_run)

    _ensure_tick()

    if not res.event.wait(timeout):
        raise TimeoutError(
            "主线程派发超时（%.1fs）：%s。通常是 Blender 正在执行长任务"
            "（渲染/模态操作），或主线程已被阻塞。" % (timeout, getattr(fn, "__name__", fn))
        )

    if res.error is not None:
        raise res.error
    return res.value


# ═══════════════════════════════════════════════════════════════════
# 内部：队列 + timer tick
# ═══════════════════════════════════════════════════════════════════

_queue = []
_lock = threading.Lock()


def _pending_empty():
    with _lock:
        return not _queue


def _ensure_tick():
    """注册 tick。已注册时重复注册会让 Blender 报错，所以先查。"""
    if not bpy.app.timers.is_registered(_TIMER_TICK):
        try:
            bpy.app.timers.register(_TIMER_TICK, first_interval=0.0,
                                    persistent=True)
        except (ValueError, RuntimeError):
            # Blender 关闭中 / 定时器系统不可用，不致命
            pass


def _TIMER_TICK():
    """主线程 tick：取出队列里的任务逐个执行。

    ⚠ 名字故意大写 + 下划线开头，避免与业务函数混淆。
    返回间隔秒数：还有活就尽快回来，没活就慢一点省 CPU。
    """
    ran = 0
    while ran < 8:                             # 一次 tick 最多跑 8 个，别卡住 UI
        with _lock:
            if not _queue:
                break
            job = _queue.pop(0)
        try:
            job()
        except Exception:                      # noqa: BLE001
            # 异常已存进 res，由后台线程重新抛出；这里只保证 tick 不断
            traceback.print_exc()
        ran += 1
    return 0.0 if not _pending_empty() else 0.05


def pending_count():
    """队列里还有多少任务（UI 可显示）。"""
    with _lock:
        return len(_queue)


def shutdown():
    """注销 tick（插件卸载时调用）。"""
    try:
        if bpy.app.timers.is_registered(_TIMER_TICK):
            bpy.app.timers.unregister(_TIMER_TICK)
    except (ValueError, RuntimeError):
        pass
    with _lock:
        _queue.clear()
