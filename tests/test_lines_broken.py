"""复现「发送后 2 条，但对话记录一行都没有」

用户现场（v1.7.4）：
    会话 会话 1 · 2 条 · 对话记录空白 · status「就绪」（不是"已发送…"）

2 条 = user + assistant，说明 agent 跑完了。但：
  - 自己发的那条 user 没画出来
  - status 回落到「就绪」

说明 **st.lines 这一层整个不可靠**：发送时 append 的行、回复时
append 的行，全都不见了。而 session.messages 是 2 条 —— 好好的。

结论：面板不能依赖 st.lines 显示主要内容。历史才是权威。
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
    ui_ai._LAST_ANSWER_SID = ""
    bpy.app.driver_namespace["_bo_session"] = None


ANSWER = "这是回复内容，必须显示出来。"


def run_one(s, q="你好"):
    mock_llm.reset([mock_llm.text_resp(ANSWER)])
    s.auto_allow = True
    s.add_user(q)
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)


# ═══════════════════════════════════════════════════════════════════
def t_history_rows_basic():
    """_history_rows 能把历史转成可画的行。"""
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    run_one(s, "你好啊")

    rows = ui_ai._history_rows(s)
    print("       行:", [(lv, tx[:14]) for lv, tx in rows])
    assert any(lv == "user" for lv, _ in rows), "没有 user 行"
    assert any(lv == "say" for lv, _ in rows), "没有 say 行"


def t_draws_from_history_when_lines_empty():
    """★ 核心：把 st.lines 清空（模拟它被重建），面板仍要显示对话。"""
    st = bpy.context.window_manager.blockout_ai
    s = next(iter(ui_ai._SESSIONS.values()))
    assert s.message_count() == 2, s.message_count()

    # ★ 模拟现场：st.lines 空了
    st.lines.clear()
    st.last_answer = ""
    st.last_answer_sid = ""

    print("       st.lines 清空后:", len(st.lines))
    rec = draw_panel()
    joined = " ".join(rec)

    print("       面板前 12 条:", rec[:12])
    assert "你好啊" in joined, \
        "用户自己发的话没画出来。rec=%r" % rec[:14]
    assert ANSWER[:10] in joined, \
        "AI 回复没画出来。rec=%r" % rec[:14]


def t_status_not_stale():
    """发送后 status 不能瞬间回落成「就绪」让人以为没发出去。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)

    mock_llm.reset([mock_llm.text_resp(ANSWER)])
    s.auto_allow = True
    s.add_user("测试")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    # 跑完之后 status 应该是「已回复」而不是「就绪」
    print("       status:", repr(st.status))
    assert st.status in ("已回复", "接收中", "已发送"), \
        "status 回落到 %r，用户会以为消息没发出去" % st.status


def t_answer_box_after_lines_cleared():
    """st.lines 清空后，回答框也要能从历史重建。"""
    st = bpy.context.window_manager.blockout_ai
    st.last_answer = ""
    st.last_answer_sid = ""
    st.lines.clear()
    ui_ai._ANSWERS.clear()

    rec = draw_panel()
    joined = " ".join(rec)
    assert any(x in joined for x in ("AI 回答", "你：", "AI：")), "清空后没画回答框"
    assert ANSWER[:10] in joined, "回答内容没恢复"


def t_every_history_message_visible():
    """历史里每条 user/assistant 都该在面板上找得到。"""
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)

    # 两轮
    run_one(s, "第一句话")
    mock_llm.reset([mock_llm.text_resp("第二轮回答")])
    s.add_user("第二句话")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    st = bpy.context.window_manager.blockout_ai
    st.lines.clear()
    rec = draw_panel()
    joined = " ".join(rec)

    print("       条数:", s.message_count())
    for need in ("第一句话", "第二句话"):
        assert need in joined, "历史里的 %r 没画出来" % need
    print("       两轮内容都在 ✓")


check("历史转行", t_history_rows_basic)
check("lines 空了仍从历史画", t_draws_from_history_when_lines_empty)
check("status 不回落", t_status_not_stale)
check("lines 空了回答框仍在", t_answer_box_after_lines_cleared)
check("每条历史都可见", t_every_history_message_visible)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
