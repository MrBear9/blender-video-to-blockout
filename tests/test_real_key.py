"""从 Blender 偏好设置读出已保存的 API key，跑真实 DeepSeek 测试

安全：key 只在内存里用，不打印、不落盘。
"""
import sys
import os
import json
import time
import importlib.util

HERE = r"E:\Xiongwq\RobotChat\blender-video-to-blockout"
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import addon_utils

# 确保插件是启用状态，偏好设置才会被注册
addon_utils.enable("blockout_helper", default_set=True, persistent=True)

entry = bpy.context.preferences.addons.get("blockout_helper")
if entry is None:
    print("RESULT 插件未启用，读不到偏好设置")
    sys.exit(1)

p = entry.preferences
key = (p.api_key or "").strip()
model = p.model or "deepseek-flash"
provider = p.provider

print("PROVIDER", provider)
print("MODEL", model)
print("KEY_LEN", len(key))
print("KEY_PREFIX", key[:6] + "..." if len(key) > 10 else "(太短)")

if not key:
    print("RESULT 偏好设置里没有 key")
    sys.exit(1)

# 加载 api_client（纯标准库）
spec = importlib.util.spec_from_file_location(
    "api_client", os.path.join(HERE, "addon", "blockout_helper", "api_client.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

print("=" * 60)

# ── 测试 1：最简 ──
t0 = time.time()
try:
    url, headers, payload = A.build_request(
        provider, key, model, [{"role": "user", "content": "只回复两个字：收到"}],
        max_tokens=32)
    print("PAYLOAD_KEYS", sorted(payload.keys()))
    print("THINKING", payload.get("thinking"))
    raw = A.post_json(url, headers, payload, timeout=60)
    print("T1_OK %.2fs" % (time.time() - t0))
    print("T1_RAW", json.dumps(raw, ensure_ascii=False)[:800])
    parsed = A.parse_response(provider, raw)
    print("T1_TEXT", repr(parsed["text"][:100]))
    print("T1_CALLS", len(parsed["tool_calls"]))
    print("T1_FINISH", parsed["raw_finish"])
except A.ApiError as e:
    print("T1_FAIL %.2fs" % (time.time() - t0), str(e)[:600])
    sys.exit(1)

# ── 测试 2：带 tools（真实 schema）──
import blockout_helper
from blockout_helper import tools as T
schemas = T.schemas()
print("\nTOOLS", len(schemas), "bytes",
      len(json.dumps(schemas, ensure_ascii=False).encode()))

t0 = time.time()
try:
    url, headers, payload = A.build_request(
        provider, key, model,
        [{"role": "user", "content": "现在场景里有什么？"}],
        tools=schemas, max_tokens=512)
    raw = A.post_json(url, headers, payload, timeout=90)
    print("T2_OK %.2fs" % (time.time() - t0))
    parsed = A.parse_response(provider, raw)
    print("T2_TEXT", repr(parsed["text"][:200]))
    print("T2_CALLS", len(parsed["tool_calls"]))
    for c in parsed["tool_calls"]:
        print("   call:", c["name"], json.dumps(c["arguments"], ensure_ascii=False)[:100])
    if not parsed["text"] and not parsed["tool_calls"]:
        print("T2_EMPTY 完整响应：")
        print(json.dumps(raw, ensure_ascii=False)[:1200])
except A.ApiError as e:
    print("T2_FAIL %.2fs" % (time.time() - t0), str(e)[:600])

print("\n" + "=" * 60)
print("DONE")
