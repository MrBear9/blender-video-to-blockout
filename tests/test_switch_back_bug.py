"""复现「图2有提示、图3切回空白」

用户现场（v1.7.3）：
    图2  s2 面板：「另一会话有回答 你好：『当前场景：…』」  ← _answer_of("s1") 有值
    图3  切回 s1：面板空白                                 ← _answer_of("s1") 却是空

同一份数据在不同会话下给出矛盾结果 —— 说明 _answer_of 的返回
**依赖当前状态**，不是纯函数。本文件把所有可能的不纯处逐一钉住。

真凶嫌疑：
  A. _session_last_answer 取到的是「工具调用那条空 assistant」之后，
     又因为某种原因返回了 ""（比如 messages 被 trim 掉了）
  B. _ANSWERS.pop 在 _switch_to 里被误清
  C. _last_final_text 被 reset/新轮覆盖成空
  D. 循环引用：_answer_of 依赖 _peek_session 的副作用
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
    for _ in range(12):
        _TIMER_TICK()
        ui_ai._poll_body()
        time.sleep(0.01)
        if not ui_ai._PENDING:
            break


# 复刻用户日志里的两轮：第一轮纯文本，第二轮先调工具再给文本
def make_two_rounds(s):
    s.auto_allow = True

    # 第 1 轮：直接文本
    mock_llm.reset([mock_llm.text_resp("你好。要做什么的？建模、白膜、视频转骨骼动画都可以。")])
    s.add_user("你好")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    # 第 2 轮：先 tool_call，再文本
    mock_llm.reset([
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "get_scene_info",
                                         "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}],
         "usage": {"total_tokens": 45}},
        mock_llm.text_resp("当前场景：1386 个对象，相机 CAM_crowd。要做什么？"),
    ])
    s.add_user("现在是什么场景")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)


def fresh():
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    return s


# ═══════════════════════════════════════════════════════════════════
def t_answer_of_is_pure():
    """★ _answer_of(sid) 必须是纯函数：不管当前 active_sid 是谁，结果一致。"""
    s1 = fresh()
    make_two_rounds(s1)

    st = bpy.context.window_manager.blockout_ai
    a_while_active = ui_ai._answer_of(s1.sid)
    print("       s1 是当前会话时:", repr(a_while_active[:30]))
    assert a_while_active, "s1 作为当前会话时就没有答案"

    s2 = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s2.sid)

    a_while_inactive = ui_ai._answer_of(s1.sid)
    print("       s1 不是当前会话时:", repr(a_while_inactive[:30]))
    assert a_while_inactive == a_while_active, \
        "_answer_of 不纯！切走后返回变了：%r -> %r" % (
            a_while_active, a_while_inactive)


def t_switch_back_shows():
    """★ 图3 的复现：切回 s1，面板必须画出答案。"""
    st = bpy.context.window_manager.blockout_ai
    s1 = next(s for s in ui_ai._SESSIONS.values()
              if ui_ai._session_last_answer(s))
    print("       s1:", s1.sid, "历史里有文本:", bool(ui_ai._session_last_answer(s1)))

    ui_ai._switch_to(s1.sid)
    print("       切回后 st.last_answer:", repr(st.last_answer[:36]))
    assert st.last_answer, "切回后 st.last_answer 是空的"

    rec = draw_panel()
    print("       面板前 10 条:", rec[:10])
    assert any(x in " ".join(rec) for x in ("AI 回答", "你：", "AI：")), \
        "切回后面板没画回答框。rec=%r" % rec[:12]


def t_hint_matches_reality():
    """图2：提示里说别处有答案 —— 那切过去就**必须**真有。"""
    st = bpy.context.window_manager.blockout_ai
    s1 = next(s for s in ui_ai._SESSIONS.values()
              if ui_ai._answer_of(s.sid))
    s2 = next(s for s in ui_ai._SESSIONS.values() if s is not s1)

    ui_ai._switch_to(s2.sid)
    rec = draw_panel()
    has_hint = any("另一会话有回答" in t for t in rec)
    print("       s2 有提示:", has_hint)
    if has_hint:
        assert ui_ai._answer_of(s1.sid), \
            "提示说 s1 有答案，但 _answer_of(s1) 是空 —— 提示是假的"


def t_trim_keeps_text():
    """历史被 trim 后，答案不能因此消失。"""
    s = fresh()
    make_two_rounds(s)
    before = ui_ai._answer_of(s.sid)

    # 塞很多轮把历史挤出窗口
    for i in range(20):
        s.messages.append({"role": "user", "content": "填充 %d" % i})
        s.messages.append({"role": "assistant", "content": ""})
    s.trim()

    after = ui_ai._answer_of(s.sid)
    print("       trim 前:", repr(before[:24]))
    print("       trim 后:", repr(after[:24]))
    assert after, "trim 之后答案彻底没了 —— 需要 _last_final_text 兜住"


def t_last_final_survives_switch():
    """_last_final_text 不能被切会话动作清掉。"""
    st = bpy.context.window_manager.blockout_ai
    s = next(s for s in ui_ai._SESSIONS.values() if s._last_final_text)
    before = s._last_final_text

    ui_ai._switch_to(s.sid)
    _ = ui_ai._peek_session()
    ui_ai._poll_body()

    print("       切换前后一致:", s._last_final_text == before)
    assert s._last_final_text == before, "_last_final_text 被清掉了"


check("_answer_of 是纯函数", t_answer_of_is_pure)
check("切回后画出答案", t_switch_back_shows)
check("提示与实际一致", t_hint_matches_reality)
check("trim 不丢答案", t_trim_keeps_text)
check("切换不清 _last_final_text", t_last_final_survives_switch)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
