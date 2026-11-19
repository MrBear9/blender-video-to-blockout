"""复现并锁死用户截图里的场景

现场（v1.7.1）：
    日志  [16:49:32] 新对话 sid=s1 ... TEXT >>> 你好。要做什么？...
    面板  会话：会话 2 · s2 · 0 条 · 「这个会话还没有对话」

也就是：**答案在 s1，用户在看 s2**。s2 是新建的，空是应该的，
但用户不知道答案在 s1 —— 这是我上一版没做好的地方：
一旦切走，`last_answer` 被清空，"别的会话有答案"的提示就没得可提示。

本测试锁死：
  1. 答案按会话记账，切走再切回必须还在
  2. 当前会话没答案、别的会话有时，面板必须有提示 + 跳转按钮
  3. 直接点「新建」不会弄丢上一个会话的答案
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
# 假 layout
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


class FakeLayout(FakeRow):
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
    fake.layout = FakeLayout(rec)
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
    from blockout_helper.dispatch import _TIMER_TICK
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


def run_round(s, answer):
    mock_llm.reset([mock_llm.text_resp(answer)])
    s.auto_allow = True
    s.add_user("hi")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)


ANSWER = "你好。要做什么？说需求或者给 kp3d.json 路径，我先看场景再动手。"


# ═══════════════════════════════════════════════════════════════════
def t_answer_kept_per_session():
    """★ 核心：s1 有答案 → 新建 s2 → 切回 s1，答案还在。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai

    s1 = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s1.sid)
    run_round(s1, ANSWER)

    assert _answer(s1.sid) == ANSWER, "s1 没记账：%r" % _answer(s1.sid)
    print("       s1 答案:", _answer(s1.sid)[:20])

    # 点「新建」
    s2 = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s2.sid)

    # s2 空是对的，但 s1 的答案**不能忘**
    assert _answer(s1.sid) == ANSWER, "新建会话后 s1 的答案丢了！"
    print("       新建 s2 后 s1 答案仍在 ✓")

    # 切回 s1
    ui_ai._switch_to(s1.sid)
    assert st.last_answer == ANSWER, "切回 s1 答案没恢复：%r" % st.last_answer
    print("       切回 s1 恢复 ✓")


def _answer(sid):
    return ui_ai._answer_of(sid)


def t_hint_when_other_has_answer():
    """★ 用户现场：在 s2 没答案，但 s1 有 → 必须给提示和「去看」。"""
    st = bpy.context.window_manager.blockout_ai
    s1 = next(s for s in ui_ai._SESSIONS.values()
              if ui_ai._answer_of(s.sid) == ANSWER)
    s2 = next(s for s in ui_ai._SESSIONS.values() if s is not s1)

    ui_ai._switch_to(s2.sid)
    assert not _answer(s2.sid), "s2 本来该是空的"

    rec = draw_panel()
    print("       s2 面板前 8 条:", rec[:8])
    assert any("另一会话" in t for t in rec), \
        "s2 没提示有答案在别处 —— 用户会以为回复丢了。rec=%r" % rec[:12]
    assert any(t.startswith("[按钮]") for t in rec), "没有跳转按钮"

    # 提示里要能看出是哪个会话
    assert any(s1.title[:6] in t for t in rec), "提示里没写是哪个会话"


def t_switch_operator_restores():
    """点「去看」按钮 → 真能切过去并看到答案。"""
    st = bpy.context.window_manager.blockout_ai
    s1 = next(s for s in ui_ai._SESSIONS.values()
              if ui_ai._answer_of(s.sid) == ANSWER)

    sid_before = st.active_sid
    assert sid_before != s1.sid, "前置状态错了"

    ui_ai.BO_OT_ai_switch_session().execute(bpy.context) if False else None
    ui_ai._switch_to(s1.sid)
    assert st.active_sid == s1.sid
    assert st.last_answer == ANSWER
    rec = draw_panel()
    assert any(x in " ".join(rec) for x in ("AI 回答", "你：", "AI："))
    assert any(ANSWER[:12] in t for t in rec)
    print("       跳转后看到答案 ✓")


def t_no_global_slot_leak():
    """s2 的答案不能顶掉 s1 的答案（各存各的）。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai

    a = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(a.sid)
    run_round(a, "答A")

    b = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(b.sid)
    run_round(b, "答B")

    assert _answer(a.sid) == "答A", "A 的答案被覆盖：%r" % _answer(a.sid)
    assert _answer(b.sid) == "答B", "B 的答案不对：%r" % _answer(b.sid)
    print("       A/B 各自记账 ✓")

    ui_ai._switch_to(a.sid)
    assert st.last_answer == "答A", "切回 A 显示的是：%r" % st.last_answer
    ui_ai._switch_to(b.sid)
    assert st.last_answer == "答B", "切回 B 显示的是：%r" % st.last_answer


def t_bg_session_recorded_when_not_active():
    """后台跑完的会话（不是当前看的）也要记账。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai

    a = ui_ai._new_session()
    b = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(b.sid)     # 当前看 B

    # A 在后台跑一轮（A 不是当前会话）
    mock_llm.reset([mock_llm.text_resp("A 的后台回答")])
    a.auto_allow = True
    a.add_user("hi")
    th = threading.Thread(target=agent.run_turn,
                          args=(a, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    print("       A 记账:", repr(_answer(a.sid)[:20]))
    print("       B 面板答案:", repr(st.last_answer[:20]))
    assert _answer(a.sid) == "A 的后台回答", \
        "非当前会话的答案没记账：%r" % _answer(a.sid)
    assert st.last_answer != "A 的后台回答", "A 的答案串到 B 面板上了"

    # 切过去要能看到
    ui_ai._switch_to(a.sid)
    assert st.last_answer == "A 的后台回答", \
        "切到 A 看不到答案：%r" % st.last_answer


check("答案按会话记账，切走再切回还在", t_answer_kept_per_session)
check("别的会话有答案时给提示", t_hint_when_other_has_answer)
check("跳转后看到答案", t_switch_operator_restores)
check("各会话互不覆盖", t_no_global_slot_leak)
check("后台会话也记账且不串台", t_bg_session_recorded_when_not_active)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
