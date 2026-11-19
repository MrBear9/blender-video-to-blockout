"""流式输出测试：mock 一个 SSE 服务，验证逐块接收与解析"""
import sys
import os
import json
import time
import threading
import importlib.util

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import blockout_helper as bh
bh.register()

from blockout_helper import api_client, agent, debuglog, tools

fails = []


def check(name, fn):
    try:
        fn()
        print("  ✓", name)
    except Exception as e:
        import traceback
        print("  ✗", name, ":", e)
        traceback.print_exc()
        fails.append(name)


# ═══════ 极简 SSE mock 服务 ═══════
from http.server import BaseHTTPRequestHandler, HTTPServer

CHUNKS = []          # 每个元素是一个 SSE data 行（dict）
RECEIVED = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        RECEIVED.append(json.loads(self.rfile.read(n).decode("utf-8")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in CHUNKS:
            self.wfile.write(("data: %s\n\n" % json.dumps(c)).encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


srv = HTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("PORT", PORT)

api_client.PROVIDERS["custom"] = {
    "label": "MockSSE", "format": "openai",
    "base_url": "http://127.0.0.1:%d/v1" % PORT,
    "models": ["m"], "docs": "",
}


def delta(content=None, reasoning=None, tool=None, finish=None):
    d = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    if tool is not None:
        d["tool_calls"] = tool
    ch = {"index": 0, "delta": d}
    if finish:
        ch["finish_reason"] = finish
    return {"choices": [ch]}


def t_stream_text():
    CHUNKS.clear()
    RECEIVED.clear()
    CHUNKS.extend([
        delta(content="你好"),
        delta(content="，这是"),
        delta(content="流式输出。"),
        delta(finish="stop"),
    ])
    got = []
    r = api_client.stream_chat("custom", "k", "m",
                               [{"role": "user", "content": "hi"}],
                               on_delta=lambda s: got.append(s), timeout=20)
    assert r["text"] == "你好，这是流式输出。", repr(r["text"])
    assert len(got) == 3, got
    assert r["stream_chunks"] == 4, r["stream_chunks"]
    # 确认请求里 stream=true
    assert RECEIVED[0]["stream"] is True, RECEIVED[0]["stream"]


def t_stream_reasoning_only():
    """只吐思考内容时不能算空回复。"""
    CHUNKS.clear()
    CHUNKS.extend([
        delta(reasoning="我在想"),
        delta(reasoning="……想好了"),
        delta(finish="stop"),
    ])
    r = api_client.stream_chat("custom", "k", "m",
                               [{"role": "user", "content": "hi"}], timeout=20)
    assert "我在想" in r["text"], r
    assert r["reasoning_chars"] > 0


def t_stream_tool_calls():
    """工具调用的参数是分块拼起来的。"""
    CHUNKS.clear()
    CHUNKS.extend([
        delta(tool=[{"index": 0, "id": "c1",
                     "function": {"name": "get_scene_info", "arguments": ""}}]),
        delta(tool=[{"index": 0, "function": {"arguments": '{"a"'}}]),
        delta(tool=[{"index": 0, "function": {"arguments": ": 1}"}}]),
        delta(finish="tool_calls"),
    ])
    r = api_client.stream_chat("custom", "k", "m",
                               [{"role": "user", "content": "hi"}], timeout=20)
    assert len(r["tool_calls"]) == 1, r["tool_calls"]
    c = r["tool_calls"][0]
    assert c["name"] == "get_scene_info", c
    assert c["arguments"] == {"a": 1}, c
    assert c["id"] == "c1", c


def t_agent_stream_events():
    CHUNKS.clear()
    CHUNKS.extend([
        delta(content="我"),
        delta(content="收到"),
        delta(content="了。"),
        delta(finish="stop"),
    ])
    s = agent.Session()
    evs = []
    s.on_event = lambda lv, tx: evs.append((lv, str(tx)))
    s.add_user("test")
    r = agent.run_turn(s, "custom", "k", "m", 0.0)
    assert r["ok"], r
    streams = [e for e in evs if e[0] == "stream"]
    assert streams, evs
    joined = "".join(e[1] for e in streams)
    assert "我收到了。" == joined, repr(joined)


def t_debuglog():
    debuglog.clear()
    debuglog.write("hello")
    p = debuglog.log_path()
    assert os.path.exists(p), p
    body = open(p, encoding="utf-8").read()
    assert "hello" in body
    # key 脱敏
    assert debuglog.redact_key("sk-1234567890abcdef") == "sk-1...(19位)"
    assert "(空)" in debuglog.redact_key("")
    print("       日志路径：", p)


check("流式文本", t_stream_text)
check("只有思考内容也不算空", t_stream_reasoning_only)
check("流式工具调用拼接", t_stream_tool_calls)
check("agent 产生 stream 事件", t_agent_stream_events)
check("调试日志写入", t_debuglog)

srv.shutdown()
bh.unregister()
print()
print("=" * 56)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
