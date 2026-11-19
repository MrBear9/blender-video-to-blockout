"""验证：DSH 端口自动探测

用户实际遇到的问题：
    昨天 DSH 在 53064，今天重启变成 64944
    → probe.mjs 报「连不上 53064」
    → 手动填端口等于每重启一次就失效一次

结论：**端口必须自动探测**。
"""
import sys
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import blockout_helper as bh
bh.register()

from blockout_helper import dsh_client

# ⚠ 这台机器上真有一个 DSH 在跑。
#   动态端口发现会抓到它，干扰所有"应该找不到"的用例。
#   统一在这些用例里屏蔽掉真实发现。
_REAL_LISTENING = dsh_client._listening_ports
dsh_client._listening_ports = lambda *a, **k: []

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
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _json(self, status, body):
        import json
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/blender-chat/status"):
            self._json(200, {"ok": True, "sessions": []})
        else:
            self._json(404, {"error": "not found"})


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def serve_plain():
    """一个不是 DSH 的服务（根路径返回别的）"""
    class Plain(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"hi")

    srv = HTTPServer(("127.0.0.1", 0), Plain)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ═══════════════════════════════════════════════════════════════════
def t_tcp_alive():
    """端口探活要准。"""
    srv, port = serve()
    try:
        assert dsh_client._tcp_alive(port), "自己起的服务都探不到"
        assert not dsh_client._tcp_alive(19998), "关着的端口探成了开着"
    finally:
        srv.shutdown()


def t_looks_like_dsh():
    """HTTP 服务要认得出；非 HTTP 的不认。"""
    srv, port = serve()
    try:
        assert dsh_client._looks_like_dsh(port), "自己的 DSH 服务没认出来"
    finally:
        srv.shutdown()

    # 非 HTTP 服务（一个裸 TCP）
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    p2 = s.getsockname()[1]
    try:
        assert not dsh_client._looks_like_dsh(p2, timeout=1), \
            "裸 TCP 被认成了 DSH"
    finally:
        s.close()


def t_detect_finds_extra_port():
    """★ 自动探测要能从 extra 候选里找到真实端口。"""
    srv, port = serve()
    try:
        dsh_client._LAST_GOOD = 0
        dsh_client._PROBE_CACHE.clear()
        found = dsh_client.detect_port(extra=[port])
        print("       目标 %d → 探到 %d" % (port, found))
        assert found == port, "没探到：%r" % found
    finally:
        srv.shutdown()


def t_resolve_prefers_configured():
    """填了且可用 → 优先用它。"""
    srv, port = serve()
    try:
        dsh_client._LAST_GOOD = 0
        dsh_client._PROBE_CACHE.clear()
        real = dsh_client.resolve_port(port)
        print("       configured=%d → %d" % (port, real))
        assert real == port
    finally:
        srv.shutdown()


def t_resolve_falls_back_on_bad_port():
    """★ 填的端口失效（DSH 重启换端口）→ 自动找新的。

    这正是用户昨天遇到的情况。
    """
    srv, port = serve()
    try:
        dsh_client._LAST_GOOD = 0
        dsh_client._PROBE_CACHE.clear()
        # 填一个不存在的端口，真实跑在 port
        real = dsh_client.resolve_port(19997)
        print("       configured=19997(死) → %d (真在 %d)" % (real, port))
        # detect_port 的候选里没有这个随机端口，所以找不到也算合理；
        # 这里验证的是「不会把死端口当成能用」
        assert real != 19997, "把死端口当成可用端口了"
    finally:
        srv.shutdown()


def t_detect_returns_zero_when_none():
    """一个都没有时返回 0，不抛异常。"""
    dsh_client._LAST_GOOD = 0
    dsh_client._PROBE_CACHE.clear()
    # 把候选改成全是死端口
    old = dsh_client.SCAN_PORTS
    dsh_client.SCAN_PORTS = (19990, 19991, 19992)
    try:
        got = dsh_client.detect_port()
        print("       →", got)
        assert got == 0, "该返回 0，实际 %r" % got
    finally:
        dsh_client.SCAN_PORTS = old


def t_self_test_reports_port():
    """★ 自检消息里要带上实际端口（用户才知道连的是哪个）。"""
    srv, port = serve()
    try:
        dsh_client._LAST_GOOD = 0
        dsh_client._PROBE_CACHE.clear()
        ok, msg = dsh_client.self_test(port)
        print("       →", msg)
        assert ok, msg
        assert str(port) in msg, "消息里没写端口：%r" % msg
    finally:
        srv.shutdown()


def t_no_dsh_message():
    """没找到 DSH 时，提示要能指导。"""
    dsh_client._LAST_GOOD = 0
    dsh_client._PROBE_CACHE.clear()
    old = dsh_client.SCAN_PORTS
    dsh_client.SCAN_PORTS = (19990,)
    try:
        ok, msg = dsh_client.self_test(0)
        print("       →", msg)
        assert not ok
        assert "没找到" in msg or "DSH" in msg, "提示不清楚：%r" % msg
    finally:
        dsh_client.SCAN_PORTS = old


def t_ui_default_zero():
    """Blender 面板的端口默认值必须是 0（自动探测）。"""
    entry = bpy.context.preferences.addons.get("blockout_helper")
    if entry is None:
        import addon_utils
        addon_utils.enable("blockout_helper", default_set=True, persistent=True)
        entry = bpy.context.preferences.addons.get("blockout_helper")
    p = entry.preferences
    bp = p.bl_rna.properties["dsh_port"]
    print("       默认值:", bp.default)
    assert bp.default == 0, "默认值该是 0（自动探测），实际 %r" % bp.default


def t_reject_rst_server():
    """★ 核心：接受 TCP 后立刻 RST 的端口，不能当成 DSH。

    实测 58169 就是这样 —— 旧实现只看"连得上"，选错了端口，
    后面 fetch 直接 ECONNRESET 崩掉。
    """
    import socket
    import threading as th

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    stop = {"v": False}

    def rst_loop():
        srv.settimeout(0.2)
        while not stop["v"]:
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            # 立刻 RST，不发任何数据
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                __import__("struct").pack("ii", 1, 0))
                conn.close()
            except OSError:
                pass

    th.Thread(target=rst_loop, daemon=True).start()
    try:
        assert dsh_client._tcp_alive(port), "TCP 层应该是通的"
        raw = dsh_client._raw_http(port, "/blender-chat/status", 2.0)
        print("       RST 端口 raw:", repr(raw[:40]))
        assert raw == "" or not raw.startswith("HTTP/"), \
            "RST 端口被当成了 HTTP 服务"
        assert not dsh_client._looks_like_dsh(port, 2.0), \
            "RST 端口被认成了 DSH"
    finally:
        stop["v"] = True
        srv.close()


def t_scoring_prefers_404_over_401():
    """★ 404（真 DSH 但没装插件）要优先于 401（别的服务统一鉴权）。"""
    # 造两个服务：一个 404，一个 401
    s404 = HTTPServer(("127.0.0.1", 0), type("H404", (BaseHTTPRequestHandler,), {
        "protocol_version": "HTTP/1.0",
        "log_message": lambda *a: None,
        "do_GET": lambda self: (
            self.send_response(404), self.send_header("Content-Length", "0"),
            self.end_headers()),
    }))
    p404 = s404.server_address[1]
    threading.Thread(target=s404.serve_forever, daemon=True).start()

    s401 = HTTPServer(("127.0.0.1", 0), type("H401", (BaseHTTPRequestHandler,), {
        "protocol_version": "HTTP/1.0",
        "log_message": lambda *a: None,
        "do_GET": lambda self: (
            self.send_response(401), self.send_header("Content-Length", "0"),
            self.end_headers()),
    }))
    p401 = s401.server_address[1]
    threading.Thread(target=s401.serve_forever, daemon=True).start()

    try:
        dsh_client._LAST_GOOD = 0
        dsh_client._PROBE_CACHE.clear()
        old = dsh_client.SCAN_PORTS
        # 故意把 401 放前面，验证会不会被"先到先得"骗走
        dsh_client.SCAN_PORTS = (p401, p404)
        try:
            got = dsh_client.detect_port()
            print("       401=%d, 404=%d → 选中 %d" % (p401, p404, got))
            assert got == p404, "选了 401 端口（%d），应该选 404（%d）" % (
                got, p404)
        finally:
            dsh_client.SCAN_PORTS = old
    finally:
        s404.shutdown()
        s401.shutdown()


check("端口探活", t_tcp_alive)
check("识别 DSH 服务", t_looks_like_dsh)
check("拒绝 RST 端口", t_reject_rst_server)
check("404 优于 401", t_scoring_prefers_404_over_401)
check("从候选里探测", t_detect_finds_extra_port)
check("优先用填的端口", t_resolve_prefers_configured)
check("死端口不被误用", t_resolve_falls_back_on_bad_port)
check("全没有返回 0", t_detect_returns_zero_when_none)
check("自检带端口号", t_self_test_reports_port)
check("找不到时提示清楚", t_no_dsh_message)
check("UI 默认自动探测", t_ui_default_zero)

dsh_client._listening_ports = _REAL_LISTENING
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
