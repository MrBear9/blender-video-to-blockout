"""本地 mock OpenAI 兼容服务 —— 验证 agent tool-use 循环

不花 token，但走**完全真实的 HTTP 路径**（urllib → JSON → 解析 → 工具执行）。
服务端按预设脚本依次返回：先要求调工具，再给出文本回答。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

RESPONSES = []          # 依次消费的响应队列
CALLS = []              # 记录收到的请求体


def reset(script):
    RESPONSES.clear()
    CALLS.clear()
    RESPONSES.extend(script)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8")
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            req = {"__raw__": body}
        CALLS.append(req)

        if RESPONSES:
            payload = RESPONSES.pop(0)
        else:
            payload = {"choices": [{"message": {"content": "（mock 队列空）"},
                                    "finish_reason": "stop"}]}

        # agent 默认走流式；请求里 stream=true 时必须回 SSE，
        # 否则流式解析器收不到任何块，会被当成空回复。
        if req.get("stream"):
            self._send_sse(payload)
            return

        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, payload):
        """把非流式 payload 拆成 SSE 块发出去。"""
        ch = (payload.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        finish = ch.get("finish_reason") or "stop"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj):
            line = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        # 正文
        text = msg.get("content") or ""
        if text:
            emit({"choices": [{"index": 0,
                               "delta": {"content": text},
                               "finish_reason": None}]})

        # 推理内容
        reason = msg.get("reasoning_content") or msg.get("reasoning")
        if reason:
            emit({"choices": [{"index": 0,
                               "delta": {"reasoning_content": reason},
                               "finish_reason": None}]})

        # 工具调用
        tcs = msg.get("tool_calls") or []
        for i, tc in enumerate(tcs):
            fn = tc.get("function") or {}
            emit({"choices": [{"index": 0,
                               "delta": {"tool_calls": [{
                                   "index": i,
                                   "id": tc.get("id") or ("call_%d" % i),
                                   "type": "function",
                                   "function": {
                                       "name": fn.get("name", ""),
                                       "arguments": fn.get("arguments", "{}"),
                                   },
                               }]},
                               "finish_reason": None}]})

        # 结束
        emit({"choices": [{"index": 0, "delta": {},
                           "finish_reason": finish}]})
        emit({"usage": payload.get("usage") or {"total_tokens": 10}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def text_resp(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 10}}


def tool_resp(name, args, call_id="call_1"):
    return {"choices": [{
        "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args,
                                                     ensure_ascii=False)},
            }],
        },
        "finish_reason": "tool_calls"}],
        "usage": {"total_tokens": 20}}


def start(port=0):
    """起服务，返回实际端口。"""
    srv = HTTPServer(("127.0.0.1", port), Handler)
    actual = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _SERVERS.append(srv)
    return actual


_SERVERS = []


def stop():
    for s in _SERVERS:
        try:
            s.shutdown()
        except Exception:                       # noqa: BLE001
            pass
    _SERVERS.clear()
