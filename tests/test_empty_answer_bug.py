"""复现「4 条但面板空白」

用户现场（v1.7.2）：会话 s1，4 条消息，状态「就绪」，回答框全空。

4 条的典型构成（用了工具的一轮）：
    user                "你好"
    assistant  content=""  tool_calls=[...]   ← 空！
    tool       {结果}
    assistant  content=""  tool_calls=[...]   ← 空！

旧代码两个漏洞：
  1. _session_last_answer 只看「最后一条 assistant」，那条 content 是空
     字符串 → 返回 ""，历史兜底失效。
  2. 答案靠 emit("final") 进事件队列；事件一丢（切会话 / timer 没跑），
     面板就永远没有内容。

本测试锁死：
  1. 带 tool_calls 的空 assistant 不能顶掉真实答案
  2. 事件全丢时，面板仍能从 session 同步字段恢复答案
  3. 多个空 assistant 之后仍然找得到文本
"""
import sys
import os
import time
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "addon"))
sys.path.insert(0, os.path.join(HERE, "tools"))   # mock_llm 在这

import bpy
import blockout_helper as bh
bh.register()

import mock_llm
from blockout_helper import agent, api_client, ui_ai
from blockout_helper.dispatch import _TIMER_TICK

fails = []


def check(name, fn):
    try:
        fn()
        print("  ✓", name)
    except Exception as e:
        import traceback
        print("  ✗", name, ":", type(e).__name__, e)
        traceback.print_exc()
        fails.append(name)


PORT = mock_llm.start()
api_client.PROVIDERS["custom"] = {
    "label": "Mock", "format": "openai",
    "base_url": "http://127.0.0.1:%d/v1" % PORT,
    "models": ["m"], "docs": "",
}


# ═══════════════════════════════════════════════════════════════════
class FakeOp:
    def __getattr__(self, name):
        return self


class FakeRow:
    def __init__(self, rec):
        self.rec = rec

    def label(self, text="", icon=None):
        self.rec.append(text)

    def operator(self, *a, **k):
        if k.get("text"):
            self.rec.append("[按钮]" + k["text"])
        return FakeOp()

    def prop(self, *a, **k):
        pass

    def row(self, align=False):
        return FakeRow(self.rec)

    def column(self):
        return FakeRow(self.rec)

    def box(self):
        return FakeRow(self.rec)

    def separator(self):
        pass

    def template_list(self, *a, **k):
        pass


def draw_panel():
    rec = []

    class FakeSelf:
        layout = None
        # v1.7.6：draw() 拆成五个区，假 self 必须都绑上
        _draw_sessions = ui_ai.VIEW3D_PT_blockout_ai._draw_sessions
        _draw_answer = ui_ai.VIEW3D_PT_blockout_ai._draw_answer
        _draw_transcript = ui_ai.VIEW3D_PT_blockout_ai._draw_transcript
        _draw_input = ui_ai.VIEW3D_PT_blockout_ai._draw_input
        _draw_debug = ui_ai.VIEW3D_PT_blockout_ai._draw_debug

        def draw(self, context):
            ui_ai.VIEW3D_PT_blockout_ai.draw(self, context)

    fake = FakeSelf()
    fake.layout = FakeRow(rec)
    fake.draw(bpy.context)
    return rec


def reset_all():
    st = bpy.context.window_manager.blockout_ai
    st.active_sid = ""
    st.lines.clear()
    st.last_answer = ""
    st.last_answer_sid = ""
    st.last_error = ""
    st.status = "就绪"
    ui_ai._SESSIONS.clear()
    ui_ai._ANSWERS.clear()
    ui_ai._PENDING.clear()
    ui_ai._LAST_ANSWER_SID = ""
    bpy.app.driver_namespace["_bo_session"] = None


def pump(thread, timeout=40):
    deadline = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        _TIMER_TICK()
        ui_ai._poll_body()
        time.sleep(0.005)
    thread.join(timeout=5)
    for _ in range(10):
        _TIMER_TICK()
        ui_ai._poll_body()
        time.sleep(0.01)
        if not ui_ai._PENDING:
            break


def fresh():
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    s.auto_allow = True
    return s


# ═══════════════════════════════════════════════════════════════════
# 1. 纯构造：历史里有空 assistant，也要找到文本
# ═══════════════════════════════════════════════════════════════════

def t_history_skips_empty_assistant():
    """最后几条 assistant 的 content 是空，也必须找到前面的文本。"""
    s = fresh()
    s.messages.append({"role": "user", "content": "你好"})
    s.messages.append({"role": "assistant", "content": "",
                       "tool_calls": [{"id": "c1", "type": "function",
                                       "function": {"name": "get_scene_info",
                                                    "arguments": "{}"}}]})
    s.messages.append({"role": "tool", "tool_call_id": "c1",
                       "content": "{}"})
    s.messages.append({"role": "assistant", "content": "",
                       "tool_calls": [{"id": "c2", "type": "function",
                                       "function": {"name": "list_objects",
                                                    "arguments": "{}"}}]})
    print("       历史条数:", s.message_count())

    got = ui_ai._session_last_answer(s)
    print("       取到:", repr(got))
    # 这一轮确实没有文本回答 → 空是对的，但不能崩
    assert got == "", "不该凭空造出答案"

    # 现在补一条真正的文本回答
    s.messages.append({"role": "assistant", "content": "场景里有 27 个对象。"})
    got = ui_ai._session_last_answer(s)
    assert got == "场景里有 27 个对象。", "找不到文本回答：%r" % got
    print("       补文本后取到 ✓")


def t_toolcall_does_not_clobber_answer():
    """★ add_assistant 带空文本 + tool_calls 时，不能顶掉之前记录的答案。"""
    s = fresh()
    s.add_assistant("第一轮的真实回答")
    assert s._last_final_text == "第一轮的真实回答"

    # 后面又调了好几次工具（每次都是空文本 + tool_calls）
    for i in range(3):
        s.add_assistant("", [{"id": "c%d" % i, "name": "get_scene_info",
                              "arguments": {}}])
    print("       _last_final_text:", repr(s._last_final_text))
    assert s._last_final_text == "第一轮的真实回答", \
        "空 assistant 顶掉了真答案：%r" % s._last_final_text


# ═══════════════════════════════════════════════════════════════════
# 2. 事件全丢，面板仍要出答案
# ═══════════════════════════════════════════════════════════════════

def t_recovers_when_events_dropped():
    """★ 核心：跑完一轮后把事件队列清空，面板仍要显示答案。

    这正是「日志有、UI 没有」的机制 —— 事件丢了，但 session 里的
    同步字段还在，poll 兜底要能从那儿捞回来。
    """
    s = fresh()
    st = bpy.context.window_manager.blockout_ai

    ANSWER = "场景里 27 个对象，12 栋楼，15 个人。"
    mock_llm.reset([mock_llm.text_resp(ANSWER)])
    s.add_user("场景里有什么")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    # ★ 模拟事件全丢
    ui_ai._PENDING.clear()
    st.last_answer = ""
    st.last_answer_sid = ""
    ui_ai._ANSWERS.clear()

    ui_ai._poll_body()

    print("       恢复后:", repr(st.last_answer[:36]))
    assert st.last_answer == ANSWER, \
        "事件丢了之后没能从 session 恢复：%r" % st.last_answer

    rec = draw_panel()
    assert any(x in " ".join(rec) for x in ("AI 回答", "你：", "AI：")), "面板没有任何内容"
    assert any(ANSWER[:12] in t for t in rec), "没画答案内容"


def t_recovers_tool_round():
    """用了一轮工具（4 条历史）后，事件丢失也要能恢复。"""
    s = fresh()
    st = bpy.context.window_manager.blockout_ai

    # 第一次请求 → 调工具；第二次 → 给文本
    mock_llm.reset([
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "get_scene_info",
                                         "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}],
         "usage": {"total_tokens": 20}},
        mock_llm.text_resp("场景里有 3 个立方体。"),
    ])

    s.add_user("场景里有什么")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    print("       历史条数:", s.message_count())
    assert s.message_count() >= 4, "应该用了工具（≥4 条）"

    # 事件全丢
    ui_ai._PENDING.clear()
    st.last_answer = ""
    ui_ai._ANSWERS.clear()
    ui_ai._poll_body()

    print("       恢复后:", repr(st.last_answer[:36]))
    assert st.last_answer == "场景里有 3 个立方体。", \
        "用了工具的那轮，事件丢失后恢复失败：%r" % st.last_answer


def t_switch_back_after_tools():
    """用了工具的一轮，切走再切回，答案要在。"""
    st = bpy.context.window_manager.blockout_ai
    s1 = next(s for s in ui_ai._SESSIONS.values() if s.message_count() >= 4)
    ui_ai._switch_to(s1.sid)
    assert st.last_answer, "切回用了工具的会话，面板还空着"

    s2 = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s2.sid)
    ui_ai._switch_to(s1.sid)
    assert st.last_answer, "切走再切回后答案没了"
    print("       切回后:", repr(st.last_answer[:30]))


def t_pure_tool_round_still_shows():
    """★ 用户现场的极端形态：模型只调工具，从头到尾没给文本。

    旧行为：面板「就绪」+ 空框，用户完全不知道发生了什么。
    新行为：必须给出可读说明。
    """
    s = fresh()
    st = bpy.context.window_manager.blockout_ai

    # 每一轮都只调工具（永远不给文本），直到 step 上限
    tool_resp = {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "get_scene_info",
                                     "arguments": "{}"}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"total_tokens": 20}}
    mock_llm.reset([tool_resp] * 30)

    s.add_user("场景里有什么")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th, timeout=90)

    print("       历史条数:", s.message_count())
    print("       last_answer:", repr(st.last_answer[:60]))
    assert st.last_answer, "整轮无文本时面板完全空白"
    assert ("没有生成回答" in st.last_answer
            or "步数上限" in st.last_answer), \
        "空白说明不明确：%r" % st.last_answer

    rec = draw_panel()
    assert any(x in " ".join(rec) for x in ("AI 回答", "你：", "AI：")), "面板没有任何内容"


check("历史里跳过空 assistant", t_history_skips_empty_assistant)
check("工具消息不顶掉答案", t_toolcall_does_not_clobber_answer)
check("事件全丢仍能恢复", t_recovers_when_events_dropped)
check("用过工具的一轮也能恢复", t_recovers_tool_round)
check("切走再切回答案还在", t_switch_back_after_tools)
check("整轮无文本也有说明", t_pure_tool_round_still_shows)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
