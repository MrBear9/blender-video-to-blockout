"""api_client 离线自检：不发网络请求，只验证请求构造与响应解析

注意：api_client 是**纯标准库**模块（不 import bpy），所以这里可以脱离
Blender 直接用系统 Python 跑，速度快得多。
"""
import importlib.util
import json
import sys
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "api_client", os.path.join(HERE, "addon", "blockout_helper", "api_client.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

fails = []


def check(name, fn):
    try:
        fn()
        print("  ✓", name)
    except Exception as e:
        print("  ✗", name, ":", e)
        fails.append(name)


TOOLS = [{
    "name": "get_scene_info",
    "description": "读取场景对象列表",
    "parameters": {"type": "object", "properties": {}, "required": []},
}]


def t_deepseek_request():
    url, h, p = A.build_request(
        "deepseek", "sk-test", "deepseek-flash",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=TOOLS)
    assert url == "https://api.deepseek.com/chat/completions", url
    assert h["Authorization"] == "Bearer sk-test", h
    assert p["model"] == "deepseek-flash"
    assert p["messages"][0]["role"] == "system"
    assert p["tools"][0]["type"] == "function"
    assert p["tools"][0]["function"]["name"] == "get_scene_info"
    assert p["stream"] is False


def t_custom_base():
    saved = A.PROVIDERS["custom"]["base_url"]
    A.PROVIDERS["custom"]["base_url"] = "http://127.0.0.1:8000/v1"
    try:
        url, h, p = A.build_request("custom", "k", "m", [{"role": "user", "content": "x"}])
        assert url == "http://127.0.0.1:8000/v1/chat/completions", url
    finally:
        A.PROVIDERS["custom"]["base_url"] = saved


def t_anthropic_request():
    url, h, p = A.build_request(
        "anthropic", "ak", "claude-x",
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        tools=TOOLS)
    assert url.endswith("/messages"), url
    assert h["x-api-key"] == "ak"
    assert h["anthropic-version"] == "2023-06-01"
    assert p["system"] == "S"
    assert all(m["role"] != "system" for m in p["messages"])
    ann = p["tools"][0]
    assert ann["name"] == "get_scene_info"
    assert "input_schema" in ann


def t_parse_openai_text():
    raw = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
           "usage": {"total_tokens": 12}}
    r = A.parse_response("deepseek", raw)
    assert r["text"] == "hello"
    assert r["tool_calls"] == []
    assert r["usage"]["total_tokens"] == 12


def t_parse_openai_tool_call():
    raw = {"choices": [{"message": {
        "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "get_scene_info",
                                     "arguments": '{"a": 1}'}}]},
        "finish_reason": "tool_calls"}]}
    r = A.parse_response("deepseek", raw)
    assert len(r["tool_calls"]) == 1
    assert r["tool_calls"][0]["name"] == "get_scene_info"
    assert r["tool_calls"][0]["arguments"] == {"a": 1}


def t_parse_bad_arguments():
    """模型偶尔吐非法 JSON —— 不能崩，要兜住。"""
    raw = {"choices": [{"message": {
        "tool_calls": [{"id": "c1", "function": {"name": "x",
                                                 "arguments": "{bad json"}}]}}]}
    r = A.parse_response("deepseek", raw)
    assert r["tool_calls"][0]["arguments"].get("__raw__") == "{bad json"


def t_parse_anthropic():
    raw = {"content": [{"type": "text", "text": "part1"},
                       {"type": "tool_use", "id": "t1", "name": "get_scene_info",
                        "input": {"k": 2}}],
           "stop_reason": "tool_use"}
    r = A.parse_response("anthropic", raw)
    assert r["text"] == "part1"
    assert r["tool_calls"][0]["arguments"] == {"k": 2}


def t_parse_empty_raises():
    try:
        A.parse_response("deepseek", {"choices": []})
    except A.ApiError:
        pass
    else:
        raise AssertionError("空 choices 应该抛 ApiError")


def t_cancel_token():
    c = A.CancelToken()
    c.raise_if_cancelled()           # 未取消，不抛
    c.cancel()
    try:
        c.raise_if_cancelled()
    except A.ApiError:
        pass
    else:
        raise AssertionError("取消后应抛 ApiError")


check("DeepSeek 请求构造", t_deepseek_request)
check("自定义 base_url", t_custom_base)
check("Anthropic 请求构造", t_anthropic_request)
check("解析纯文本响应", t_parse_openai_text)
check("解析 tool_call 响应", t_parse_openai_tool_call)
check("非法 arguments 不崩", t_parse_bad_arguments)
check("解析 Anthropic 响应", t_parse_anthropic)
check("空 choices 报错", t_parse_empty_raises)
check("取消令牌", t_cancel_token)

def t_deepseek_thinking_disabled():
    """DeepSeek 必须显式关掉思考模式。

    官方文档：省略 thinking 且不给 reasoning_effort 时**默认开启思考**，
    思考模式默认输出上限 64K tokens —— 实测表现就是界面一直显示
    "正在请求模型…"，几十秒到几分钟才回来。
    """
    url, h, p = A.build_request("deepseek", "k", "deepseek-flash",
                                [{"role": "user", "content": "hi"}])
    assert p.get("thinking") == {"type": "disabled"}, p.get("thinking")

    # 其他提供商不该被塞这个字段
    url2, h2, p2 = A.build_request("openai", "k", "gpt-4o-mini",
                                   [{"role": "user", "content": "hi"}])
    assert "thinking" not in p2, p2


def t_tool_call_id_stripped():
    """tool_call_id 前后的不可见空格必须清掉。

    实测 DeepSeek/Qwen 会把 id 包上空格，回填时匹配不上，
    请求会被拒或静默卡住（"tool response without matching tool call"）。
    """
    raw = {"choices": [{"message": {"tool_calls": [
        {"id": "  call_abc  ", "function": {"name": " f ", "arguments": "{}"}}]}}]}
    r = A.parse_response("deepseek", raw)
    assert r["tool_calls"][0]["id"] == "call_abc", repr(r["tool_calls"][0]["id"])
    assert r["tool_calls"][0]["name"] == "f", repr(r["tool_calls"][0]["name"])

    # 空 id 要兜一个
    raw2 = {"choices": [{"message": {"tool_calls": [
        {"id": "", "function": {"name": "f", "arguments": "{}"}}]}}]}
    r2 = A.parse_response("deepseek", raw2)
    assert r2["tool_calls"][0]["id"], "空 id 应兜一个默认值"


def t_reasoning_content_fallback():
    """思考模式下内容可能在 reasoning_content，不取就显示空白。"""
    raw = {"choices": [{"message": {"content": "",
                                    "reasoning_content": "思考内容"},
                        "finish_reason": "stop"}]}
    r = A.parse_response("deepseek", raw)
    assert r["text"] == "思考内容", repr(r["text"])

    # dict 形式
    raw2 = {"choices": [{"message": {"content": "",
                                     "thinking": {"content": "嵌套"}},
                         "finish_reason": "stop"}]}
    assert A.parse_response("deepseek", raw2)["text"] == "嵌套"

    # 全空时也要能拿到原始响应用于展示
    raw3 = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
    r3 = A.parse_response("deepseek", raw3)
    assert r3["text"] == ""
    assert r3["raw_message"], "空回复时必须留原始响应"
    assert "choices" in r3["raw_all"]


def t_raw_always_present():
    """任何响应都要带 raw_message，界面才能兜底显示。"""
    raw = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
    r = A.parse_response("deepseek", raw)
    assert r["raw_message"], r
    assert r["raw_all"], r


check("DeepSeek 关掉思考模式", t_deepseek_thinking_disabled)
check("tool_call_id 去空格", t_tool_call_id_stripped)
check("reasoning_content 兜底", t_reasoning_content_fallback)
check("始终保留原始响应", t_raw_always_present)

print()
print("=" * 56)
print("✗ %d 项失败" % len(fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
