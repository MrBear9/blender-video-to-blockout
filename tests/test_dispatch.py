"""验证 dispatch：后台线程操作 bpy 是否安全"""
import sys, os, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon"))

import bpy
from blockout_helper import dispatch

print("MAIN_THREAD", dispatch.is_main_thread())

# 1. 主线程直调
v = dispatch.call_on_main(lambda: bpy.app.version_string)
print("DIRECT", v)

# 2. 后台线程 → 派发回主线程
box = {}
def worker():
    t0 = time.time()
    try:
        box["name"] = dispatch.call_on_main(lambda: bpy.data.objects[0].name)
        box["count"] = dispatch.call_on_main(lambda: len(bpy.data.objects))
        box["ok"] = True
    except Exception as e:
        box["err"] = "%s: %s" % (type(e).__name__, e)
    box["ms"] = round((time.time() - t0) * 1000, 1)

t = threading.Thread(target=worker)
t.start()

# 主线程必须持续跑事件循环，timer 才会触发。
# ⚠ -b（后台模式）**没有事件循环**，timer 永远不触发。
#    真实插件跑在 GUI 里，这里用 bpy.app.timers.timer 的类型判断说明模式。
print("BACKGROUND_MODE", bpy.app.background)
from blockout_helper.dispatch import _TIMER_TICK
deadline = time.time() + 10
while t.is_alive() and time.time() < deadline:
    _TIMER_TICK()          # 无事件循环时手动 tick（headless 测试专用）
    time.sleep(0.01)
t.join(timeout=5)

print("WORKER", box)

# 3. 异常传播
def boom():
    raise ValueError("预期异常")
try:
    dispatch.call_on_main(boom)
except ValueError as e:
    print("EXC_OK", e)

# 4. 后台线程的异常也要能带回来
box2 = {}
def worker2():
    try:
        dispatch.call_on_main(boom)
    except ValueError as e:
        box2["err"] = str(e)

t2 = threading.Thread(target=worker2)
t2.start()
deadline = time.time() + 10
while t2.is_alive() and time.time() < deadline:
    _TIMER_TICK()
    time.sleep(0.01)
t2.join(timeout=5)
print("WORKER_EXC", box2)

dispatch.shutdown()
print("DISPATCH_TEST_DONE")
