"""验证：DSH 通道（不填 API Key，借 DSH 的模型）

用户需求：
    「我现在不是直接使用的插件的配置api key吗，其实我并不一定非得
      在插件里面去配置模型然后去请求模型……可以直接通过本地的 DSH
      的插件会话的方式去修改 blender」

本测试锁死：
  1. dsh_client 的错误信息能直接指导用户（不通时知道该干什么）
  2. backend=dsh 时不需要 API Key 就能发
  3. run_turn_via_dsh 能把 stream/final 事件正确 emit 出去
  4. 中断能生效
"""
import sys
import os
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import addon_utils
import blockout_helper as bh
bh.register()
addon_utils.enable("blockout_helper", default_set=True, persistent=True)

from blockout_helper import agent, dsh_client, ui_ai

fails = []


def check(name, fn):
    try:
        fn()
        print("  ✓", name)
    except AssertionError as e:
        print("  ✗", name, ":", e)
        fails.append(name)
    except Exception as e:
        import traceback
        print("  ✗", name, ":", type(e).__name__, e)
        traceback.print_exc()
        fails.append(name)


# ═══════════════════════════════════════════════════════════════════
# 假 DSH 后端（/blender-chat/*）
# ═══════════════════════════════════════════════════════════════════

STATE = {
    "sent": [],          # 收到的 send
    "events": [],        # 待 poll 取走的事件
    "seq": 0,
    "mode": "ok",        # ok | 404 | 401 | empty
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"      # 每次请求一个连接，避免 keep-alive 串状态

    def log_message(self, *a):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if STATE["mode"] == "404":
            self._json(404, {"error": "not found"})
            return
        if STATE["mode"] == "401":
            self._json(401, {"error": "unauthorized"})
            return
        if self.path.startswith("/blender-chat/status"):
            self._json(200, {"ok": True, "sessions": [
                {"sid": s.get("sid"), "sessionId": "x", "running": False}
                for s in STATE["sent"]
            ]})
            return
        if self.path.startswith("/blender-chat/poll"):
            evs = STATE["events"]
            STATE["events"] = []
            self._json(200, {"events": evs, "running": False})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if STATE["mode"] == "404":
            self._json(404, {"error": "not found"})
            return
        if STATE["mode"] == "401":
            self._json(401, {"error": "unauthorized"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        if self.path.startswith("/blender-chat/send"):
            STATE["sent"].append(body)
            if STATE["mode"] == "empty":
                self._json(200, {"ok": True, "sessionId": "s"})
                return
            for piece in ("你", "好"):
                STATE["seq"] += 1
                STATE["events"].append(
                    {"seq": STATE["seq"], "kind": "stream", "text": piece})
            STATE["seq"] += 1
            STATE["events"].append(
                {"seq": STATE["seq"], "kind": "final", "text": "你好"})
            self._json(200, {"ok": True, "sessionId": "s"})
            return
        if self.path.startswith("/blender-chat/reset"):
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})


PORT = 0
SERVER = None


def start_server():
    global SERVER, PORT
    SERVER = HTTPServer(("127.0.0.1", 0), Handler)
    PORT = SERVER.server_address[1]
    t = threading.Thread(target=SERVER.serve_forever, daemon=True)
    t.start()
    return PORT


# ⚠ 必须先起服务再跑用例：忘了这行会让 PORT 一直是 0，
#   所有请求打到 127.0.0.1:0 —— 表现为一堆莫名其妙的 405/404。
start_server()
print("[假 DSH 后端] 端口 %d" % PORT)


def reset_state(mode="ok"):
    STATE["sent"] = []
    STATE["events"] = []
    STATE["seq"] = 0
    STATE["mode"] = mode


# ⚠ 这台机器上真有一个 DSH 在跑（63524 等）。
#   自动探测会优先抓到它，绕过我们起的假服务器。
#   测 self_test(port) 时给它显式端口就够了，但 detect 的候选要清掉。
_REAL_SCAN = list(dsh_client.SCAN_PORTS)
_REAL_LISTENING = dsh_client._listening_ports
dsh_client._listening_ports = lambda *a, **k: []
dsh_client.SCAN_PORTS = ()


def restore_scan():
    dsh_client.SCAN_PORTS = tuple(_REAL_SCAN)
    dsh_client._listening_ports = _REAL_LISTENING


def t_client_error_messages():
    """★ 通道不通时，错误信息必须能指导用户。"""
    # 404（现在消息里会带实际端口，断言放宽）
    reset_state("404")
    ok, msg = dsh_client.self_test(PORT)
    print("       404 →", msg[:70])
    assert not ok
    assert "404" in msg or "没装" in msg or "blender-chat" in msg, \
        "404 提示不清楚：%r" % msg
    assert str(PORT) in msg, "提示里没带端口：%r" % msg

    # 401：resolve_port 会跳过 401（它不能用来发请求），
    # 所以消息是"没找到"而不是"要鉴权" —— 这也是可接受的告知。
    reset_state("401")
    ok, msg = dsh_client.self_test(PORT)
    print("       401 →", msg[:70])
    assert not ok
    assert ("鉴权" in msg or "401" in msg or "没找到" in msg), \
        "401 提示不清楚：%r" % msg

    # 坏端口：现在会自动探测，可能找到真实 DSH（报"没装插件"），
    # 也可能什么都没找到。两种都算"没通过且信息可用"。
    dsh_client._LAST_GOOD = 0
    dsh_client._PROBE_CACHE.clear()
    ok, msg = dsh_client.self_test(19999)
    print("       坏端口 →", msg[:70])
    assert not ok, "坏端口不该通过"
    assert ("连不上" in msg or "没找到" in msg or "blender-chat" in msg), \
        "坏端口提示不清楚：%r" % msg


def t_self_test_ok():
    """通道正常时自检要过。"""
    reset_state()
    ok, msg = dsh_client.self_test(PORT)
    print("       →", msg)
    assert ok, "自检该通过：%r" % msg


def t_send_and_poll():
    """send + poll 能拿到事件。"""
    reset_state()
    sid = dsh_client.send(PORT, "t1", "测试")
    assert sid == "s"
    assert STATE["sent"][0]["text"] == "测试"

    events, running, since = dsh_client.poll(PORT, "t1", 0)
    print("       events:", [(e["kind"], e["text"]) for e in events])
    assert len(events) == 3
    assert since == 3, "since 没推进：%d" % since


def t_run_turn_callbacks():
    """★ run_turn 要把 stream/final 都回调出去。"""
    reset_state()
    deltas, finals = [], []
    res = dsh_client.run_turn(
        PORT, "t2", "问一句",
        on_delta=deltas.append,
        on_final=finals.append,
        max_wait=10,
    )
    print("       deltas:", deltas, "finals:", finals)
    print("       result:", res)
    assert res["ok"], "run_turn 失败：%r" % res
    assert res["text"] == "你好"
    assert deltas == ["你", "好"], "增量没回调：%r" % deltas
    assert finals == ["你好"], "final 没回调：%r" % finals


def t_run_turn_empty():
    """DSH 无产出时要给出明确错误，不能一直挂。"""
    reset_state("empty")
    t0 = time.time()
    res = dsh_client.run_turn(PORT, "t3", "问", max_wait=15)
    dt = time.time() - t0
    print("       耗时 %.1fs →" % dt, res)
    assert not res["ok"]
    assert res["error"], "没有错误信息"
    assert dt < 14, "没及时返回（%.1fs）" % dt


def t_cancel():
    """中断令牌要生效。"""
    reset_state("empty")
    tok = dsh_client.CancelToken()
    tok.cancel()
    res = dsh_client.run_turn(PORT, "t4", "问", cancel=tok, max_wait=10)
    print("       →", res)
    assert not res["ok"]


# ═══════════════════════════════════════════════════════════════════
# Blender 侧接入
# ═══════════════════════════════════════════════════════════════════
def t_backend_dsh_no_key_needed():
    """★ backend=dsh 时，没填 key 也能发出去。"""
    p = bpy.context.preferences.addons["blockout_helper"].preferences
    old_backend, old_port, old_key = p.backend, p.dsh_port, p.api_key
    try:
        p.backend = "dsh"
        p.dsh_port = PORT
        p.api_key = ""     # 故意清空

        reset_state()
        st = bpy.context.window_manager.blockout_ai
        st.input_text = "走 DSH 的问题"
        ui_ai._SESSIONS.clear()
        st.active_sid = ""

        r = bpy.ops.blockout.ai_send()
        print("       send 返回:", r)
        assert "FINISHED" in r, "没填 key 却不让发：%r" % r

        # 等后台线程
        for _ in range(40):
            time.sleep(0.1)
            if not bpy.app.driver_namespace.get("_bo_session") or \
                    not bpy.app.driver_namespace["_bo_session"].running:
                break
        print("       DSH 收到:", STATE["sent"])
        assert STATE["sent"], "DSH 没收到消息"
        assert STATE["sent"][0]["text"] == "走 DSH 的问题"
    finally:
        p.backend, p.dsh_port, p.api_key = old_backend, old_port, old_key


def t_via_dsh_emits_events():
    """★ run_turn_via_dsh 要 emit final 给面板。"""
    reset_state()
    s = agent.Session()
    s.on_event = ui_ai.post_event
    ui_ai._PENDING.clear()

    th = threading.Thread(
        target=agent.run_turn_via_dsh, args=(s, PORT, "问题"), daemon=True)
    th.start()
    th.join(timeout=20)
    assert not th.is_alive(), "后台线程没结束"

    levels = [(lv, tx) for lv, tx, _ in ui_ai._PENDING]
    print("       事件:", [(lv, tx[:12]) for lv, tx in levels])
    kinds = [lv for lv, _ in levels]
    assert "stream" in kinds, "没 emit stream"
    assert "final" in kinds, "没 emit final"
    finals = [tx for lv, tx in levels if lv == "final"]
    assert finals and finals[0] == "你好"


def t_ui_has_backend_switch():
    """偏好设置里要有请求方式切换。"""
    src = open(os.path.join(HERE, "addon", "blockout_helper", "ui_ai.py"),
               encoding="utf-8").read()
    assert '"dsh"' in src and "走 DSH" in src, "没有 DSH 选项"
    assert "dsh_port" in src, "没有端口配置"
    assert "blockout.ai_dsh_test" in src, "没有 DSH 自检按钮"


check("错误信息可指导", t_client_error_messages)
check("自检通过", t_self_test_ok)
check("send + poll", t_send_and_poll)
check("run_turn 回调", t_run_turn_callbacks)
check("无产出及时返回", t_run_turn_empty)
check("中断生效", t_cancel)
check("dsh 模式免 key", t_backend_dsh_no_key_needed)
check("via_dsh 发事件", t_via_dsh_emits_events)
check("UI 有切换", t_ui_has_backend_switch)

if SERVER:
    SERVER.shutdown()
restore_scan()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
