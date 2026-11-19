"""在 Blender 里实测 DeepSeek 真实对话（带 tools）

需要 DEEPSEEK_API_KEY 环境变量。
只跑一次最简请求，验证"测试连接成功但对话卡住"是否已修。
"""
import sys
import os
import time
import json
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import blockout_helper as bh
bh.register()

from blockout_helper import agent, api_client, dispatch, tools

KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
if not KEY:
    print("NO_KEY 跳过（设置 DEEPSEEK_API_KEY 后重跑）")
    sys.exit(0)

MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
print("MODEL", MODEL, "KEY_LEN", len(KEY))

# ── A. 不带 tools（等价于"测试连接"）──
t0 = time.time()
try:
    r = api_client.chat_once("deepseek", KEY, MODEL,
                             [{"role": "user", "content": "只回复两个字：收到"}],
                             max_tokens=32, timeout=60)
    print("A_NO_TOOLS ok=%.1fs text=%r" % (time.time() - t0, (r.get("text") or "")[:60]))
except api_client.ApiError as e:
    print("A_NO_TOOLS FAIL %.1fs %s" % (time.time() - t0, e))

# ── B. 带 tools（真实对话路径）──
schemas = tools.schemas()
print("TOOLS", len(schemas), "bytes",
      len(json.dumps(schemas, ensure_ascii=False).encode("utf-8")))
t0 = time.time()
try:
    r = api_client.chat_once("deepseek", KEY, MODEL,
                             [{"role": "system", "content": agent.SYSTEM_PROMPT},
                              {"role": "user", "content": "现在场景里有什么？"}],
                             tools=schemas, max_tokens=1024, timeout=90)
    dt = time.time() - t0
    print("B_WITH_TOOLS ok=%.1fs text=%r calls=%d"
          % (dt, (r.get("text") or "")[:80], len(r.get("tool_calls") or [])))
    for c in (r.get("tool_calls") or []):
        print("   tool:", c["name"], json.dumps(c["arguments"], ensure_ascii=False)[:120])
except api_client.ApiError as e:
    print("B_WITH_TOOLS FAIL %.1fs %s" % (time.time() - t0, e))

# ── C. 完整 agent 一轮（后台线程 + dispatch）──
S = agent.Session()
evs = []
S.on_event = lambda lv, tx: evs.append((lv, str(tx)[:120]))
S.auto_allow = True
S.add_user("列出所有网格对象的名称")

res = {}
def worker():
    res["r"] = agent.run_turn(S, "deepseek", KEY, MODEL, 0.3)

t0 = time.time()
th = threading.Thread(target=worker, daemon=True)
th.start()
from blockout_helper.dispatch import _TIMER_TICK
deadline = time.time() + 150
while th.is_alive() and time.time() < deadline:
    _TIMER_TICK()
    time.sleep(0.01)
th.join(timeout=10)
print("C_AGENT done=%s %.1fs" % (not th.is_alive(), time.time() - t0))
print("   result:", res.get("r"))
for lv, tx in evs[-6:]:
    print("   ev[%s] %s" % (lv, tx))

bh.unregister()
print("REAL_TEST_DONE")
