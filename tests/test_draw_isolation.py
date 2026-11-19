"""验证：draw() 分段隔离 + 点列表自动切会话

用户现场（v1.7.5）：面板画到「就绪 2 条」就停了，下面（对话记录、
输入框、快捷、版本）全都没了。

Blender 对 draw() 里的异常**完全静默**：只画到出错那行，不报错、
不打印。所以只要某一段抛一次，用户看到的就是"面板少了一半"，
而且完全不知道原因。

本测试锁死：
  1. 任何一段抛异常，其它段照画
  2. 异常必须在面板上可见（不是静默）
  3. 列表选中即切换
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

        # 把 panel 的真实方法绑上来，冒充 self
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


def reset_all():
    st = bpy.context.window_manager.blockout_ai
    st.active_sid = ""
    st.lines.clear()
    st.sessions.clear()
    st.answers.clear()
    st.last_answer = ""
    st.last_answer_sid = ""
    st.last_error = ""
    st.status = "就绪"
    ui_ai._SESSIONS.clear()
    ui_ai._ANSWERS.clear()
    ui_ai._PENDING.clear()
    ui_ai._PENDING_SWITCH = ""
    ui_ai._LAST_ANSWER_SID = ""
    bpy.app.driver_namespace["_bo_session"] = None


ANSWER = "分段测试的回复内容。"


def make_session(q="你好"):
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    mock_llm.reset([mock_llm.text_resp(ANSWER)])
    s.auto_allow = True
    s.add_user(q)
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)
    return s


# ═══════════════════════════════════════════════════════════════════
def t_all_sections_drawn():
    """正常情况：五个区都要画到。"""
    make_session()
    rec = draw_panel()
    joined = " ".join(rec)
    print("       总条数:", len(rec))
    for need in ("会话", "发送", "快捷", "诊断"):
        assert need in joined, "面板缺少「%s」区。rec=%r" % (need, rec[-8:])
    print("       五个区都在 ✓")


def t_broken_section_does_not_kill_rest():
    """★ 核心：让「回答区」抛异常，其它区必须照画。"""
    make_session()

    orig = ui_ai.VIEW3D_PT_blockout_ai._draw_answer

    def boom(self, layout, st, session):
        raise RuntimeError("模拟回答区出错")

    ui_ai.VIEW3D_PT_blockout_ai._draw_answer = boom
    try:
        rec = draw_panel()
    finally:
        ui_ai.VIEW3D_PT_blockout_ai._draw_answer = orig

    joined = " ".join(rec)
    print("       回答区坏掉后的条数:", len(rec))
    # 关键：后面的区必须在
    assert "发送" in joined, "回答区坏了，输入框也没了"
    assert "快捷" in joined, "回答区坏了，快捷区也没了"
    assert "诊断" in joined, "回答区坏了，诊断区也没了"
    # 异常必须可见
    assert "面板绘制出错" in joined, "异常被静默吞掉了"
    print("       其它区照画 + 异常可见 ✓")


def t_broken_transcript_keeps_input():
    """对话记录区抛异常，输入框必须还能用（否则彻底没法自救）。"""
    make_session()

    orig = ui_ai.VIEW3D_PT_blockout_ai._draw_transcript

    def boom(self, layout, st, session):
        raise ValueError("模拟历史渲染出错")

    ui_ai.VIEW3D_PT_blockout_ai._draw_transcript = boom
    try:
        rec = draw_panel()
    finally:
        ui_ai.VIEW3D_PT_blockout_ai._draw_transcript = orig

    joined = " ".join(rec)
    assert "发送" in joined, "对话记录坏了，输入框也没了 —— 用户没法自救"
    assert "诊断" in joined, "对话记录坏了，诊断区也没了"
    print("       输入框仍可用 ✓")


def t_answers_collection_missing_ok():
    """st.answers 读不了时，draw 也不能整体崩。"""
    make_session()

    def boom(self, layout, st, session):
        raise RuntimeError("模拟答案区坏")

    orig = ui_ai.VIEW3D_PT_blockout_ai._draw_answer
    ui_ai.VIEW3D_PT_blockout_ai._draw_answer = boom
    try:
        rec = draw_panel()
    finally:
        ui_ai.VIEW3D_PT_blockout_ai._draw_answer = orig

    assert len(rec) > 6, len(rec)
    print("       st.answers 异常时面板仍完整 ✓")


def t_select_switches_session():
    """★ 点列表选中另一条，下一次 poll 要自动切过去。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai

    a = ui_ai._new_session()
    b = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(a.sid)
    assert st.active_sid == a.sid

    # 模拟用户点了列表第二行（b）
    idx_b = next(i for i in range(len(st.sessions))
                 if st.sessions[i].sid == b.sid)
    st.session_index = idx_b

    # draw 只记录意图
    draw_panel()
    print("       draw 后 pending:", ui_ai._PENDING_SWITCH)
    assert ui_ai._PENDING_SWITCH == b.sid, "draw 没记录切换意图"

    # timer 真正执行
    ui_ai._poll_body()
    print("       切换后 active:", st.active_sid)
    assert st.active_sid == b.sid, "选中没自动切换"


def t_switch_shows_that_history():
    """切过去要能看到**那条**会话的历史。"""
    st = bpy.context.window_manager.blockout_ai
    a, b = list(ui_ai._SESSIONS.values())[:2]
    a.add_user("A 的消息")
    a.messages.append({"role": "assistant", "content": "A 的回答"})
    b.add_user("B 的消息")
    b.messages.append({"role": "assistant", "content": "B 的回答"})

    ui_ai._switch_to(b.sid)
    rec = draw_panel()
    joined = " ".join(rec)
    assert "B 的消息" in joined, "切到 B 没显示 B 的历史"
    assert "A 的消息" not in joined, "切到 B 却显示了 A 的历史"
    print("       历史正确切换 ✓")


def t_no_switch_loop():
    """反复 draw 不能反复触发切换（会死循环）。"""
    st = bpy.context.window_manager.blockout_ai
    ui_ai._PENDING_SWITCH = ""
    before = st.active_sid

    for _ in range(10):
        draw_panel()
        ui_ai._poll_body()

    print("       active 前后:", before, "->", st.active_sid)
    assert st.active_sid == before, "反复 draw 触发了切换"


check("五个区都画", t_all_sections_drawn)
check("单区异常不拖垮全部", t_broken_section_does_not_kill_rest)
check("历史坏了输入框还在", t_broken_transcript_keeps_input)
check("答案区异常面板仍完整", t_answers_collection_missing_ok)
check("选中即切换", t_select_switches_session)
check("切换显示对应历史", t_switch_shows_that_history)
check("不会反复切换", t_no_switch_loop)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
