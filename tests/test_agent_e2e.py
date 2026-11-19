"""端到端验证 agent tool-use 循环（走真实 HTTP，不花 token）

流程：
    mock 服务 → agent.run_turn（后台线程）→ api_client urllib → 解析
    → dispatch 回主线程 → tools.call 改场景 → 结果回填 → 再请求 → 文本收尾

判据：
    * 场景里真的多出了物体
    * 请求体里带上了 tools 定义
    * 第二轮的 messages 里带了 tool 消息
"""
import sys
import os
import time
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)                                  # mock_llm 在这
sys.path.insert(0, os.path.join(HERE, "addon"))
sys.path.insert(0, os.path.join(HERE, "tools"))   # mock_llm 在这           # blockout_helper 在这

import bpy

import blockout_helper as bh
bh.register()

import mock_llm
from blockout_helper import agent, api_client, dispatch, tools

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


# 起 mock 服务
PORT = mock_llm.start()
print("MOCK_PORT", PORT)

# 指向 mock
api_client.PROVIDERS["custom"] = {
    "label": "Mock", "format": "openai",
    "base_url": "http://127.0.0.1:%d/v1" % PORT,
    "models": ["mock-model"], "docs": "",
}

# 清场
for o in list(bpy.data.objects):
    if o.name.startswith("E2E_"):
        bpy.data.objects.remove(o, do_unlink=True)

S = agent.Session()
events = []
S.on_event = lambda lv, tx: events.append((lv, tx))


def pump_until(thread, timeout=30):
    """后台线程跑着时，主线程必须持续 tick，否则 dispatch 会超时。"""
    from blockout_helper.dispatch import _TIMER_TICK
    deadline = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        _TIMER_TICK()
        time.sleep(0.005)
    thread.join(timeout=5)


# ═══════════════════════════════════════════════════════════════════
# 场景 1：单次工具调用 → 文本收尾
# ═══════════════════════════════════════════════════════════════════
def t_single_tool_call():
    mock_llm.reset([
        mock_llm.tool_resp("add_primitive",
                           {"kind": "cube", "name": "E2E_Cube", "size": 0.4,
                            "location": [0, 0, 0.4]}, "c1"),
        mock_llm.text_resp("已经加好了一个立方体。"),
    ])
    S.reset()
    S.auto_allow = True
    S.add_user("加一个立方体")

    th = threading.Thread(target=agent.run_turn,
                          args=(S, "custom", "test-key", "mock-model", 0.0),
                          daemon=True)
    th.start()
    pump_until(th)

    assert "E2E_Cube" in bpy.data.objects, "场景里没多出物体"
    assert len(mock_llm.CALLS) == 2, "应该请求两次（工具后还要一次）"

    # 第一次请求要带 tools
    first = mock_llm.CALLS[0]
    assert first.get("tools"), "请求里没有 tools 定义"
    assert any(t["function"]["name"] == "add_primitive" for t in first["tools"])
    assert first["stream"] is True, "agent 应走流式"

    # 第二次请求要带 tool 结果
    second = mock_llm.CALLS[1]
    roles = [m["role"] for m in second["messages"]]
    assert "tool" in roles, roles
    tool_msg = next(m for m in second["messages"] if m["role"] == "tool")
    assert "E2E_Cube" in tool_msg["content"], tool_msg["content"][:200]

    # 最终回答落进历史
    last = S.messages[-1]
    assert last["role"] == "assistant"
    assert "立方体" in last["content"], last["content"]


# ═══════════════════════════════════════════════════════════════════
# 场景 2：多工具链（加物体 → 改变换 → 收尾）
# ═══════════════════════════════════════════════════════════════════
def t_multi_step():
    mock_llm.reset([
        mock_llm.tool_resp("add_primitive",
                           {"kind": "sphere", "name": "E2E_Sphere",
                            "size": 0.3, "location": [2, 0, 0]}, "s1"),
        mock_llm.tool_resp("set_transform",
                           {"name": "E2E_Sphere", "location": [2, 0, 1.5]}, "s2"),
        mock_llm.text_resp("球加好了，并且抬到了 1.5 米。"),
    ])
    S.reset()
    S.auto_allow = True
    S.add_user("加个球再抬到 1.5 米")

    th = threading.Thread(target=agent.run_turn,
                          args=(S, "custom", "test-key", "mock-model", 0.0),
                          daemon=True)
    th.start()
    pump_until(th)

    o = bpy.data.objects.get("E2E_Sphere")
    assert o is not None, "球没建出来"
    assert abs(o.location.z - 1.5) < 1e-4, o.location
    assert len(mock_llm.CALLS) == 3, len(mock_llm.CALLS)


# ═══════════════════════════════════════════════════════════════════
# 场景 3：破坏性工具需要确认（拒绝就不执行）
# ═══════════════════════════════════════════════════════════════════
def t_destructive_rejected():
    target = bpy.data.objects.get("E2E_Cube")
    assert target is not None, "前置：E2E_Cube 应该存在"

    mock_llm.reset([
        mock_llm.tool_resp("delete_objects", {"names": ["E2E_Cube"]}, "d1"),
        mock_llm.text_resp("好的，不删了。"),
    ])
    S.reset()
    S.auto_allow = False          # 关键：需要确认
    S.add_user("把 E2E_Cube 删掉")

    def auto_reject():
        """等 pending_confirm 出现，然后拒绝。"""
        deadline = time.time() + 15
        while time.time() < deadline:
            pc = S.pending_confirm
            if pc:
                name, args, ev, box = pc
                box["ok"] = False
                ev.set()
                return
            time.sleep(0.01)

    th_r = threading.Thread(target=auto_reject, daemon=True)
    th_r.start()

    th = threading.Thread(target=agent.run_turn,
                          args=(S, "custom", "test-key", "mock-model", 0.0),
                          daemon=True)
    th.start()
    pump_until(th)

    assert "E2E_Cube" in bpy.data.objects, "拒绝后物体不该被删"
    # 找到 delete_objects 那条 tool 结果（第一轮），确认内容是"拒绝"
    tool_msgs = [m for m in S.messages if m["role"] == "tool"]
    assert tool_msgs, "没有 tool 消息"
    first_tool = tool_msgs[0]
    assert "拒绝" in first_tool["content"], first_tool["content"]
    print("       拒绝消息：", first_tool["content"][:80])


# ═══════════════════════════════════════════════════════════════════
# 场景 4：API 报错要如实回传到会话
# ═══════════════════════════════════════════════════════════════════
def t_api_error():
    mock_llm.reset([])            # 队列空 → mock 返回 stop，不报错
    # 改成指向一个不存在的端口，制造网络错误
    saved = api_client.PROVIDERS["custom"]["base_url"]
    api_client.PROVIDERS["custom"]["base_url"] = "http://127.0.0.1:1/v1"
    try:
        S.reset()
        S.add_user("hi")
        r = agent.run_turn(S, "custom", "k", "m", 0.0)
        assert r["ok"] is False, r
        assert "网络不可达" in (r.get("error") or ""), r
    finally:
        api_client.PROVIDERS["custom"]["base_url"] = saved


# ═══════════════════════════════════════════════════════════════════
# 场景 5：超步保护
# ═══════════════════════════════════════════════════════════════════
def t_max_steps():
    script = [mock_llm.tool_resp("get_scene_info", {}, "m%d" % i)
              for i in range(agent.MAX_STEPS + 3)]
    mock_llm.reset(script)
    S.reset()
    S.auto_allow = True
    S.add_user("无限循环试试")
    r = agent.run_turn(S, "custom", "k", "m", 0.0)
    assert r["ok"] is False, r
    assert "步" in (r.get("error") or ""), r
    assert r["steps"] <= agent.MAX_STEPS, r


check("单次工具调用 → 文本收尾", t_single_tool_call)
check("多工具链（加 + 改）", t_multi_step)
check("破坏性工具拒绝执行", t_destructive_rejected)
check("API 错误如实回传", t_api_error)
check("超步保护", t_max_steps)

# 清场
for o in list(bpy.data.objects):
    if o.name.startswith("E2E_"):
        bpy.data.objects.remove(o, do_unlink=True)

mock_llm.stop()
bh.unregister()

print()
print("=" * 56)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
