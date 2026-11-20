"""dsh_client.py —— 走 DSH 的模型，不再自己填 API Key

为什么要它
----------
    插件原来自己 POST api.deepseek.com，key 得单独填、单独存。
    但 DSH 里早就配好了模型（还有 34 family / 228 个建模 op）。
    这个模块把「请求」交给 DSH，插件不碰任何凭据。

数据流
------
    Blender 面板 → POST /blender-chat/send  → DSH 插件 → agents.create/followup
    Blender 面板 ← GET  /blender-chat/poll  ← 事件邮箱（stream/final）

关键约束
--------
* 只依赖标准库（urllib / json），不引入 requests —— Blender 环境干净。
* **必须跑在后台线程**：send/poll 都会阻塞。bpy 只在主线程碰。
* 端口可配：DSH Web 默认 53064，但会变，所以允许覆盖。
"""
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

#: DSH Web GUI 默认端口（**每次启动都会变**，所以下面有自动探测）
DEFAULT_PORT = 0

#: 自动探测时要扫的端口范围（本机实测出现过的）
SCAN_PORTS = (63524, 64944, 53064, 58169, 8326)

#: 探测结果缓存：端口 -> (是否可用, 时间戳)
_PROBE_CACHE = {}
_PROBE_TTL = 30.0

#: 最近一次探测到的可用端口（优先复用，避免每轮都扫）
_LAST_GOOD = 0


#: 单次 HTTP 超时
TIMEOUT = 30


class DshError(Exception):
    """DSH 通道错误。message 必须能直接显示给用户。"""


class CancelToken:
    """中断令牌。与 api_client.CancelToken 同接口，方便共用取消逻辑。"""

    def __init__(self):
        import threading
        self._ev = threading.Event()

    def cancel(self):
        self._ev.set()

    def is_set(self):
        return self._ev.is_set()

    def raise_if_cancelled(self):
        if self._ev.is_set():
            raise DshError("已取消")


def _base(port):
    return "http://127.0.0.1:%d" % int(port or DEFAULT_PORT)


def _tcp_alive(port, timeout=0.12):
    """快速探一下端口开没开（比发 HTTP 便宜得多）。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)),
                                      timeout=timeout):
            return True
    except OSError:
        return False


def _looks_like_dsh(port, timeout=3.0):
    """这个端口是不是 DSH 的 **HTTP 服务**？

    ⚠ 两个坑（都真踩过）：
      1. 58169 端口接受 TCP 连接后**立刻 RST** —— 只看"连得上"会误判。
      2. 同一进程开了多个端口（8326 / 58169 / 63524），
         其中 8326 对所有路径都回 401。

    所以：必须**读到 HTTP 响应行**才算；连不上/被重置/超时都不算。
    """
    raw = _raw_http(port, "/blender-chat/status", timeout)
    if not raw:
        return False
    return raw.startswith("HTTP/")


def _raw_http(port, path, timeout=3.0):
    """用裸 TCP 发一个最小 GET，返回原始响应文本；拿不到返回 ""。"""
    import socket
    s = None
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
        s.settimeout(timeout)
        req = ("GET %s HTTP/1.0\r\nHost: 127.0.0.1\r\n"
               "Connection: close\r\n\r\n" % path)
        s.sendall(req.encode("latin1"))
        chunks = []
        total = 0
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > 8192:
                break
        return b"".join(chunks).decode("latin1", "replace")
    except OSError:
        return ""
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _http_code(raw):
    """从原始响应里取状态码；取不到返回 0。"""
    if not raw or not raw.startswith("HTTP/"):
        return 0
    try:
        return int(raw.split(None, 2)[1])
    except (IndexError, ValueError):
        return 0


def _listening_ports(hint_procs=("deepseek", "dsh")):
    """枚举本机正在监听的端口。

    ⚠ 为什么必须动态枚举：DSH 每次启动都换端口，
      实测见过 53064 → 64944 → 58169 → 63524 → 49648。
      写死候选单必然跟不上。

    做法：优先按进程名过滤（Windows / Linux 各一套），
    拿不到就退回"所有本地监听端口"。
    """
    import os
    import subprocess

    ports = []
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                timeout=8, encoding="utf-8", errors="replace")
            rows = []
            for line in (out.stdout or "").splitlines():
                parts = line.split()
                # TCP  127.0.0.1:49648  0.0.0.0:0  LISTENING  38200
                if len(parts) < 5 or parts[3].upper() != "LISTENING":
                    continue
                if not parts[0].upper().startswith("TCP"):
                    continue
                try:
                    port = int(parts[1].rsplit(":", 1)[1])
                    pid = int(parts[4])
                except (IndexError, ValueError):
                    continue
                rows.append((port, pid))

            # 找 DSH 进程的 PID
            hit_pids = set()
            try:
                tl = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=8,
                    encoding="utf-8", errors="replace")
                for line in (tl.stdout or "").splitlines():
                    cols = [c.strip('"') for c in line.split('","')]
                    if len(cols) < 2:
                        continue
                    name = cols[0].strip('"').lower()
                    try:
                        pid = int(cols[1].strip('"'))
                    except ValueError:
                        continue
                    if any(h in name for h in hint_procs):
                        hit_pids.add(pid)
            except (OSError, subprocess.SubprocessError):
                pass

            for port, pid in rows:
                if not hit_pids or pid in hit_pids:
                    if port not in ports:
                        ports.append(port)
        else:
            out = subprocess.run(
                ["sh", "-c", "ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null"],
                capture_output=True, text=True, timeout=8)
            for line in (out.stdout or "").splitlines():
                for tok in line.split():
                    if ":" in tok:
                        try:
                            p = int(tok.rsplit(":", 1)[1])
                            if p not in ports:
                                ports.append(p)
                        except ValueError:
                            pass
    except (OSError, subprocess.SubprocessError):
        return []

    return ports


def _candidate_ports(extra=None):
    """候选端口：显式给的 → 动态发现 → 兜底列表。"""
    out = []
    for p in (extra or []):
        if p and int(p) not in out:
            out.append(int(p))
    for p in _listening_ports():
        if p not in out:
            out.append(p)
    for p in SCAN_PORTS:                    # 兜底：实测出现过的
        if p not in out:
            out.append(p)
    return out


def detect_port(extra=None, use_cache=True):
    """自动找 DSH **Web** 的端口。

    ⚠ 为什么不写死端口：DSH **每次启动都换端口**
      （实测 53064 → 64944 → 58169 → 63524 → 49648）。

    ⚠ 为什么要"打分"而不是"第一个命中就用"：
      同一进程会开好几个端口（8326 对所有路径回 401，
      58169 直接 RST）。按能不能问到 /blender-chat 排序最可靠：

        200 → 插件已装（最好）
        405 → 路径在，方法不对
        404 → 是真 DSH，但路由没注册（= 插件没装）
        401/403 → 可能只是别的服务在统一鉴权（最低）

    ⚠ 还有一个坑：本机的 9877（dsh-blender-plugin 后端）也会回 404，
      但它回的是**带 JSON body 的 404**（{"ok":false,...}），
      而 DSH Web 回的是**空 body 的 404**。靠 body 区分：
      有 JSON "error":"not found" 的，是别的服务，降权。

    返回端口号，找不到返回 0。
    """
    global _LAST_GOOD
    now = time.time()

    # 1. 上次成功的
    if use_cache and _LAST_GOOD:
        hit = _PROBE_CACHE.get(_LAST_GOOD)
        if hit and now - hit[1] < _PROBE_TTL and hit[0]:
            if _tcp_alive(_LAST_GOOD):
                return _LAST_GOOD

    scored = []
    for p in _candidate_ports(extra):
        if not _tcp_alive(p, 0.12):
            _PROBE_CACHE[p] = (False, now)
            continue
        raw = _raw_http(p, "/blender-chat/status", 2.5)
        code = _http_code(raw)
        if code == 0:
            _PROBE_CACHE[p] = (False, now)      # 不是 HTTP，跳过
            continue
        rank = (4 if code == 200 else
                3 if code == 405 else
                2 if code == 404 else
                1 if code in (401, 403) else 0)

        # ★ 404 还要看 body：带 {"error":"not found"} 的是别的服务
        #   （实测 9877 就是这样），真 DSH Web 回的是空 body。
        if code == 404:
            body = raw.split("\r\n\r\n", 1)
            body = body[1] if len(body) > 1 else ""
            if '"not found"' in body or '"error"' in body:
                rank = 1                    # 降级：不是 DSH Web

        scored.append((rank, p, code))
        _PROBE_CACHE[p] = (code == 200, now)

    if not scored:
        return 0

    scored.sort(key=lambda t: (-t[0], t[1]))
    rank, best, code = scored[0]
    _LAST_GOOD = best if code in (200, 404) else 0
    return best


def resolve_port(configured=0):
    """把「用户填的端口」变成「真正能用的端口」。

    填了就先用它（只要是真 HTTP 服务就认）；不通就自动探测。
    """
    if configured:
        c = int(configured)
        # 只要能问到 HTTP 就算数（404 = 真 DSH 但插件没装，
        # 那种情况也要让后续报「插件没装」，而不是「端口不对」）
        raw = _raw_http(c, "/blender-chat/status", 3.0)
        if _http_code(raw) in (200, 404, 405):
            return c
    return detect_port()


def _request(port, path, payload=None, method="GET", timeout=TIMEOUT):
    # ★ 端口无效（0 / 空 / 负数）时自动探测。
    #   以前 DEFAULT_PORT = 0，_base() 拼出 http://127.0.0.1:0，
    #   Windows 上直接抛 WinError 10049（地址无效），
    #   面板上就显示成"拉取失败"，看不出是端口没解析。
    try:
        p = int(port or 0)
    except (TypeError, ValueError):
        p = 0
    if p <= 0 or p > 65535:
        p = resolve_port(0)
        if not p:
            raise DshError("没找到在跑的 DSH。确认 DSH 已启动，"
                           "或在上方手动填写端口号。")
    url = _base(p) + path
    data = None
    headers = {"accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:                       # noqa: BLE001
            pass
        if e.code in (401, 403):
            raise DshError(
                "DSH 需要鉴权（HTTP %d）。请在 DSH 设置里把本地端口访问"
                "打开，或改用带 Cookie 的方式。" % e.code)
        if e.code == 404:
            raise DshError(
                "DSH 上没有 /blender-chat 路由（HTTP 404）。"
                "先装 dsh-blender-chat 插件并重启 DSH。")
        if e.code in (502, 503, 504):
            # 网关层错误：DSH 上游模型服务暂时不可用（常出现在
            # 代理/nginx 前面），通常等几秒重发就好，不是插件的问题。
            raise DshError(
                "网关暂时不可用（HTTP %d）—— 上游模型服务没响应。"
                "这通常是临时的，等几秒点「重发上一条」即可。" % e.code)
        raise DshError("DSH 返回 HTTP %d：%s" % (e.code, body or e.reason))
    except urllib.error.URLError as e:
        raise DshError(
            "连不上 DSH（%s）：%s。确认 DSH 在运行，端口填对了没。"
            % (_base(p), e.reason))
    except json.JSONDecodeError:
        raise DshError("DSH 返回的不是 JSON，端口可能填错了。")


def status(port):
    """探一次通道。返回 dict；不通就抛 DshError（消息可直接显示）。"""
    return _request(port, "/blender-chat/status")


def send(port, sid, text, cwd="", attach=""):
    """把一句话交给 DSH。返回 sessionId。

    attach: 要接上的已有会话 id（或其唯一前缀）。留空则用/新建
            blender-chat-xxx 会话。绑上之后该 sid 一直复用那条会话。
    """
    if not (text or "").strip():
        raise DshError("消息为空")
    body = {"sid": sid, "text": text, "cwd": cwd}
    if attach:
        body["attach"] = attach
    r = _request(port, "/blender-chat/send", body, method="POST")
    if not r.get("ok"):
        raise DshError(r.get("error") or "DSH 拒绝了这条消息")
    return r.get("sessionId") or ""


def list_sessions(port, limit=100, live_only=True):
    """列出 DSH 上已有的会话，供面板下拉选择。

    live_only=True 时只返回「工作区里登记着」的会话 —— 归档的、
    孤儿残留、测试壳子都不会出现，列表跟网页左侧对得上。

    返回 (sessions, can_list)。sessions 是
    [{sessionId, title, cwd, createdAt}, ...]，按时间倒序。
    can_list 为 False 时说明列不出来，只能手填完整 id。
    """
    params = {"limit": int(limit)}
    if live_only:
        params["liveOnly"] = "1"
    q = urllib.parse.urlencode(params)
    r = _request(port, "/blender-chat/sessions?" + q)
    return (r.get("sessions") or []), bool(r.get("canList"))


def poll(port, sid, since=0):
    """取一次增量事件。返回 (events, running, next_since)。"""
    q = urllib.parse.urlencode({"sid": sid, "since": int(since)})
    r = _request(port, "/blender-chat/poll?" + q)
    events = r.get("events") or []
    nxt = since
    for ev in events:
        seq = ev.get("seq")
        if isinstance(seq, int) and seq > nxt:
            nxt = seq
    return events, bool(r.get("running")), nxt


def reset(port, sid):
    """丢掉该 sid 的 DSH 会话（下次重新建）。"""
    q = urllib.parse.urlencode({"sid": sid})
    return _request(port, "/blender-chat/reset?" + q, method="POST")


def run_turn(port, sid, text, cwd="", on_delta=None, on_final=None,
             on_progress=None, cancel=None, max_wait=300, attach=""):
    """完整跑一轮：发送 → 轮询 → 回调。**在后台线程里调用。**

    返回 {"ok": bool, "text": str, "error": str, "port": int, "sessionId": str}

    port=0 时自动探测（DSH 每次启动都换端口，别指望填对）。
    attach: 接上已有会话（完整 id 或唯一前缀），留空则新建。
    cancel: 任何带 .is_set() 的对象；置位后尽快返回。
    """
    t0 = time.time()

    # ★ 先解析端口：探测本身也要花时间，放进 max_wait 的预算里
    try:
        real = resolve_port(port)
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "text": "", "port": 0,
                "error": "探测 DSH 端口失败：%s" % e}
    if not real:
        return {"ok": False, "text": "", "port": 0,
                "error": "没找到在跑的 DSH。确认 DSH 已启动。"}

    session_id = ""
    try:
        session_id = send(real, sid, text, cwd=cwd, attach=attach)
    except DshError as e:
        return {"ok": False, "text": "", "error": str(e), "port": real}

    if on_progress:
        try:
            on_progress("已交给 DSH，等待模型…")
        except Exception:                       # noqa: BLE001
            pass

    since = 0
    final = ""
    idle_polls = 0
    while time.time() - t0 < max_wait:
        if cancel is not None:
            try:
                if cancel.is_set():
                    return {"ok": bool(final), "text": final,
                            "error": "" if final else "已取消"}
            except Exception:                   # noqa: BLE001
                pass

        try:
            events, running, since = poll(real, sid, since)
        except DshError as e:
            return {"ok": bool(final), "text": final,
                    "error": str(e), "port": real}

        for ev in events:
            kind = ev.get("kind")
            t = ev.get("text") or ""
            if kind == "stream":
                if on_delta:
                    try:
                        on_delta(t)
                    except Exception:           # noqa: BLE001
                        pass
            elif kind == "final":
                final += t
                if on_final:
                    try:
                        on_final(t)
                    except Exception:           # noqa: BLE001
                        pass

        if final:
            return {"ok": True, "text": final, "error": "", "port": real,
                    "sessionId": session_id}

        if not running and not events:
            idle_polls += 1
            # 连续几次都空闲且没有任何事件 → 认为这一轮没产出
            if idle_polls >= 8:
                return {"ok": False, "text": "", "port": real,
                        "sessionId": session_id,
                        "error": "DSH 没有返回内容（agent 已空闲）"}
        else:
            idle_polls = 0

        time.sleep(0.35)

    return {"ok": bool(final), "text": final, "port": real,
            "sessionId": session_id,
            "error": "" if final else "等待 DSH 超时（%d 秒）" % max_wait}


def self_test(port=0):
    """给界面用的自检：返回 (ok, message)。

    port=0 时自动探测，消息里会带上实际用的端口。
    """
    try:
        real = resolve_port(port)
    except Exception as e:                      # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)

    if not real:
        return False, ("没找到在跑的 DSH。"
                       "确认 DSH 已启动（端口每次都不一样，已自动扫描）。")

    try:
        st = status(real)
    except DshError as e:
        msg = str(e)
        if "404" in msg:
            return False, ("找到 DSH（端口 %d），但没有 /blender-chat 路由。"
                           "先装 dsh-blender-chat 插件并重启 DSH。" % real)
        return False, msg
    except Exception as e:                      # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)

    n = len(st.get("sessions") or [])
    return True, "DSH 通道正常（端口 %d，已有 %d 个会话）" % (real, n)
