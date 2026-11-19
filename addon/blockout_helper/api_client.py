"""api_client.py —— 多提供商 LLM 客户端

只依赖标准库 urllib（Blender 的 Python 有时缺 requests，且不该为插件加依赖）。

支持的提供商形态
----------------
    openai     OpenAI Chat Completions 格式（DeepSeek / OpenAI / 兼容端点都走这条）
    anthropic  Anthropic Messages 格式

DeepSeek 用 OpenAI 兼容格式：
    base_url = https://api.deepseek.com
    model    = deepseek-flash
详见 https://api-docs.deepseek.com/zh-cn/

设计要点
--------
* **不阻塞**：所有网络请求在调用方给的后台线程里跑，本模块不碰 bpy
* **可取消**：每个请求带一个 CancelToken，UI 能中途取消
* **错误可读**：HTTP 错误把响应体前 500 字带出来（API 常把真正原因放 body 里）
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request


# ═══════════════════════════════════════════════════════════════════
# 提供商预设
# ═══════════════════════════════════════════════════════════════════

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "format": "openai",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "docs": "https://api-docs.deepseek.com/zh-cn/",
    },
    "openai": {
        "label": "OpenAI",
        "format": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "docs": "https://platform.openai.com/docs",
    },
    "anthropic": {
        "label": "Anthropic",
        "format": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-20250514"],
        "docs": "https://docs.anthropic.com",
    },
    "ollama": {
        "label": "Ollama（本地）",
        "format": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "docs": "https://ollama.com",
    },
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "format": "openai",
        "base_url": "",
        "models": [],
        "docs": "",
    },
}


class ApiError(Exception):
    """API 层错误（网络/鉴权/格式）。message 要能直接给用户看。"""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class CancelToken:
    """取消标志。UI 点"停止"时 set()，请求循环会主动退出。"""

    def __init__(self):
        self._ev = threading.Event()

    def cancel(self):
        self._ev.set()

    @property
    def cancelled(self):
        return self._ev.is_set()

    def raise_if_cancelled(self):
        if self._ev.is_set():
            raise ApiError("已取消")


# ═══════════════════════════════════════════════════════════════════
# 请求构造 / 解析
# ═══════════════════════════════════════════════════════════════════

def build_request(provider_key, api_key, model, messages, tools=None,
                  temperature=0.3, max_tokens=4096, stream=False):
    """→ (url, headers, payload_dict)"""
    prov = PROVIDERS.get(provider_key) or PROVIDERS["custom"]
    base = (prov["base_url"] or "").rstrip("/")
    fmt = prov["format"]

    if fmt == "anthropic":
        url = base + "/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [m for m in messages if m.get("role") != "system"],
        }
        sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
        if sys_parts:
            payload["system"] = "\n\n".join(sys_parts)
        if tools:
            payload["tools"] = [_anthropic_tool(t) for t in tools]
        if stream:
            payload["stream"] = True
        return url, headers, payload

    # OpenAI 兼容
    url = base + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": bool(stream),
    }
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
        payload["tool_choice"] = "auto"

    # ⚠ DeepSeek 特有：**默认开启思考模式**（官方文档原文），
    # 思考模式默认输出上限 64K tokens —— 表现就是界面一直显示
    # "正在请求模型…"，等很久才回来。工具调用场景不需要思考，显式关掉。
    if provider_key == "deepseek":
        payload["thinking"] = {"type": "disabled"}

    return url, headers, payload


def _anthropic_tool(t):
    """OpenAI function 格式 → Anthropic tool 格式。"""
    return {
        "name": t["name"],
        "description": t.get("description", ""),
        "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
    }


def parse_response(provider_key, raw):
    """把 HTTP 响应体解析成统一结构。

    返回:
        {"text": str, "tool_calls": [{"id","name","arguments"(dict)}],
         "usage": {...}, "raw_finish": str}
    """
    prov = PROVIDERS.get(provider_key) or PROVIDERS["custom"]
    if prov["format"] == "anthropic":
        return _parse_anthropic(raw)
    return _parse_openai(raw)


def _parse_openai(raw):
    choices = raw.get("choices") or []
    if not choices:
        raise ApiError("响应里没有 choices", body=json.dumps(raw)[:500])
    msg = choices[0].get("message") or {}
    text = msg.get("content") or ""

    # ⚠ 兼容思考模式：DeepSeek / 部分兼容端点在思考模式下把内容放
    # message.reasoning_content（或 reasoning），content 为空。
    # 不取这个字段的话，界面就会"请求成功但什么都不显示"。
    if not text:
        for alt in ("reasoning_content", "reasoning", "thinking"):
            altv = msg.get(alt)
            if isinstance(altv, str) and altv.strip():
                text = altv
                break
            if isinstance(altv, dict):
                inner = altv.get("content") or altv.get("text")
                if isinstance(inner, str) and inner.strip():
                    text = inner
                    break

    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError:
            args = {"__raw__": args_raw}
        # tool_call_id 必须 strip：部分模型（实测 DeepSeek/Qwen）会把 id 包上
        # 不可见空格，回填 tool 消息时匹配不上，API 会静默卡住或直接拒绝。
        cid = str(tc.get("id") or "").strip()
        if not cid:
            cid = "call_%d" % len(calls)
        calls.append({"id": cid,
                      "name": str(fn.get("name") or "").strip(),
                      "arguments": args})

    return {"text": text, "tool_calls": calls,
            "usage": raw.get("usage") or {},
            "raw_finish": choices[0].get("finish_reason"),
            # 原始响应留一份，空回复时能直接显示给用户看
            "raw_message": json.dumps(msg, ensure_ascii=False)[:1200],
            "raw_all": json.dumps(raw, ensure_ascii=False)[:2000]}


def _parse_anthropic(raw):
    text_parts = []
    calls = []
    for block in raw.get("content") or []:
        t = block.get("type")
        if t == "text":
            text_parts.append(block.get("text") or "")
        elif t == "tool_use":
            calls.append({"id": block.get("id") or ("call_%d" % len(calls)),
                          "name": block.get("name") or "",
                          "arguments": block.get("input") or {}})
    return {"text": "".join(text_parts), "tool_calls": calls,
            "usage": raw.get("usage") or {},
            "raw_finish": raw.get("stop_reason")}


# ═══════════════════════════════════════════════════════════════════
# 网络
# ═══════════════════════════════════════════════════════════════════

def post_json(url, headers, payload, timeout=120, cancel=None, debug=None):
    """发一个 JSON POST。返回解析后的 dict。

    debug：可选回调 debug(str)，把请求/响应的关键信息吐出去。
           插件里接到 UI 的【原始】行，用户能看到到底发生了什么。
    """
    if cancel is not None:
        cancel.raise_if_cancelled()

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    if debug:
        try:
            debug("POST %s | %d bytes | model=%s | tools=%d"
                  % (url, len(data), payload.get("model"),
                     len(payload.get("tools") or [])))
        except Exception:                       # noqa: BLE001
            pass

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:                       # noqa: BLE001
            pass
        hint = ""
        if e.code == 401:
            hint = "（API Key 无效或未配置）"
        elif e.code == 402:
            hint = "（余额不足）"
        elif e.code == 404:
            hint = "（路径不存在：检查 base_url，DeepSeek 应填 https://api.deepseek.com）"
        elif e.code == 422:
            hint = "（参数不合法：可能是 tools schema 或 max_tokens）"
        elif e.code == 429:
            hint = "（触发限流，稍后再试）"
        elif e.code >= 500:
            hint = "（服务端错误，可重试）"
        msg = "HTTP %d %s%s\n%s" % (e.code, e.reason, hint, detail[:800])
        if debug:
            try:
                debug(msg)
            except Exception:                   # noqa: BLE001
                pass
        raise ApiError(msg, status=e.code, body=detail) from e
    except urllib.error.URLError as e:
        msg = "网络不可达：%s\n检查 base_url 与网络/代理设置" % (e.reason,)
        if debug:
            try:
                debug(msg)
            except Exception:                   # noqa: BLE001
                pass
        raise ApiError(msg) from e
    except socket.timeout:
        msg = "请求超时（%ds）" % timeout
        if debug:
            try:
                debug(msg)
            except Exception:                   # noqa: BLE001
                pass
        raise ApiError(msg) from None
    except Exception as e:                      # noqa: BLE001
        msg = "%s: %s" % (type(e).__name__, e)
        if debug:
            try:
                debug(msg)
            except Exception:                   # noqa: BLE001
                pass
        raise ApiError(msg) from e

    if debug:
        try:
            debug("HTTP %s | 响应 %d 字节 | 前 400 字：%s"
                  % (status, len(body), body[:400].replace("\n", " ")))
        except Exception:                       # noqa: BLE001
            pass

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise ApiError("响应不是合法 JSON：%s\n%s" % (e, body[:500])) from e


def chat_once(provider_key, api_key, model, messages, tools=None,
              temperature=0.3, max_tokens=2048, timeout=90, cancel=None,
              thinking=None, debug=None):
    """一次完整往返（非流式）。返回 parse_response 的结构。

    thinking：DeepSeek 专用。None=插件默认关掉（工具调用不需要思考，
              开着会让"请求中"持续几十秒到几分钟）；
              "enabled"/"high" 等可显式开启。
    debug：回调 debug(str)，把 HTTP 层的关键信息吐给 UI。
    """
    url, headers, payload = build_request(
        provider_key, api_key, model, messages, tools,
        temperature, max_tokens, stream=False)
    if thinking is not None and provider_key == "deepseek":
        payload["thinking"] = {"type": "enabled" if thinking is True else thinking}
        if "reasoning_effort" not in payload:
            payload["reasoning_effort"] = "high"
    raw = post_json(url, headers, payload, timeout=timeout, cancel=cancel,
                    debug=debug)
    return parse_response(provider_key, raw)


def stream_chat(provider_key, api_key, model, messages, tools=None,
                temperature=0.3, max_tokens=2048, timeout=180, cancel=None,
                on_delta=None, on_reason=None, on_progress=None, debug=None):
    """**流式**请求：逐块回调 on_delta(text)，最后返回完整结果。

    为什么需要流式：非流式要等模型把整段答完才返回一整块。
    DeepSeek 关掉思考后仍可能先吐 reasoning、再吐 content，
    客户端如果只看非流式的 message.content，很容易拿到空串。
    流式能一边收一边显示，且能分别捕获 reasoning / content。

    返回结构与 parse_response 一致：
        {"text", "tool_calls", "usage", "raw_finish", ...}
    """
    if cancel is not None:
        cancel.raise_if_cancelled()

    url, headers, payload = build_request(
        provider_key, api_key, model, messages, tools,
        temperature, max_tokens, stream=True)
    if provider_key == "deepseek":
        payload["thinking"] = {"type": "disabled"}

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    if debug:
        _safe(debug, "STREAM POST %s | %d bytes | tools=%d"
              % (url, len(data), len(payload.get("tools") or [])))

    text_parts = []
    reason_parts = []
    tool_acc = {}          # index -> {"id","name","args"}
    finish = None
    usage = {}
    raw_chunks = 0
    bytes_read = 0

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:800]
        except Exception:                       # noqa: BLE001
            pass
        _safe(debug, "STREAM HTTP %d %s\n%s" % (e.code, e.reason, detail))
        raise ApiError("HTTP %d %s\n%s" % (e.code, e.reason, detail),
                       status=e.code, body=detail) from e
    except urllib.error.URLError as e:
        _safe(debug, "STREAM 网络不可达 %s" % (e.reason,))
        raise ApiError("网络不可达：%s" % (e.reason,)) from e
    except socket.timeout:
        _safe(debug, "STREAM 连接超时")
        raise ApiError("流式连接超时（%ds）" % timeout) from None

    t_start = time.time()
    last_report = 0.0

    try:
        for raw_line in resp:
            if cancel is not None and cancel.cancelled:
                break
            bytes_read += len(raw_line)
            line = raw_line.decode("utf-8", errors="replace").strip()

            # 每 3 秒报一次进展，避免"界面看起来死了"
            now = time.time()
            if on_progress and now - last_report >= 3.0:
                last_report = now
                _safe(on_progress, "已接收 %d 块 / %d 字节 / %.0fs"
                      % (raw_chunks, bytes_read, now - t_start))

            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_chunks += 1

            if obj.get("usage"):
                usage = obj["usage"]

            for ch in (obj.get("choices") or []):
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                d = ch.get("delta") or ch.get("message") or {}

                # 思考内容（DeepSeek/兼容端）
                rc = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(rc, str) and rc:
                    reason_parts.append(rc)
                    if on_reason:
                        _safe(on_reason, rc)

                c = d.get("content")
                if isinstance(c, str) and c:
                    text_parts.append(c)
                    if on_delta:
                        _safe(on_delta, c)

                for tc in (d.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = str(tc["id"]).strip()
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
    except socket.timeout:
        _safe(debug, "STREAM 读取超时（已收 %d 块）" % raw_chunks)
    finally:
        try:
            resp.close()
        except Exception:                       # noqa: BLE001
            pass

    text = "".join(text_parts)
    calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        try:
            args = json.loads(slot["args"]) if slot["args"] else {}
        except json.JSONDecodeError:
            args = {"__raw__": slot["args"]}
        calls.append({"id": slot["id"] or ("call_%d" % idx),
                      "name": slot["name"], "arguments": args})

    _safe(debug, "STREAM 完成：%d 块 | text=%d字 | reasoning=%d字 | calls=%d | finish=%s"
          % (raw_chunks, len(text), len("".join(reason_parts)), len(calls), finish))

    # 模型只吐了思考内容、没吐正文 → 把思考内容当正文显示，
    # 否则界面又是空的（这是"有回复但不显示"的一种成因）
    display = text
    if not display and reason_parts:
        display = "\n".join(reason_parts)

    return {"text": display, "tool_calls": calls, "usage": usage,
            "raw_finish": finish,
            "reasoning_chars": len("".join(reason_parts)),
            "stream_chunks": raw_chunks,
            "raw_message": (display[:600] if display else "(流式无内容)"),
            "raw_all": ""}


def _safe(fn, arg):
    try:
        fn(arg)
    except Exception:                           # noqa: BLE001
        pass


def test_connection(provider_key, api_key, model, base_url="", timeout=25):
    """连通性自检：发一句最短的问候。返回 (ok, message)。"""
    prov = PROVIDERS.get(provider_key) or PROVIDERS["custom"]
    saved = None
    if base_url:
        saved = PROVIDERS.get(provider_key)
        tmp = dict(prov)
        tmp["base_url"] = base_url
        PROVIDERS[provider_key] = tmp
    try:
        r = chat_once(provider_key, api_key, model,
                      [{"role": "user", "content": "回复：ok"}],
                      max_tokens=16, timeout=timeout)
        return True, (r.get("text") or "(空回复)")[:120]
    except ApiError as e:
        return False, str(e)
    finally:
        if saved is not None:
            PROVIDERS[provider_key] = saved
