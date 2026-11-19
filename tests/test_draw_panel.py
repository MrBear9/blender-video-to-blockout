"""验证：面板 draw() 真的把答案画出来了

之前的测试都只检查 st.last_answer 这个属性，没检查 draw()。
如果 draw() 中途抛异常，Blender 会静默中断绘制 —— 属性有值，
面板上却什么都没有。这正是「日志里有、UI 里没有」的另一种可能。
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


# ═══════════════════════════════════════════════════════════════════
# 假 layout：记录所有 label/box 文字
# ═══════════════════════════════════════════════════════════════════

class FakeRow:
    def __init__(self, rec):
        self.rec = rec

    def label(self, text="", icon=None):
        self.rec.append(text)

    def operator(self, *a, **k):
        # 记录按钮文字，才能断言「去看」这类按钮真的画了
        if k.get("text"):
            self.rec.append("[按钮]" + k["text"])
        return FakeOp()

    def prop(self, *a, **k):
        pass

    def row(self, align=False):
        return FakeRow(self.rec)

    def column(self):
        return FakeRow(self.rec)

    def template_list(self, *a, **k):
        pass

    def separator(self):
        pass


class FakeOp:
    def __getattr__(self, name):
        return self


class FakeLayout(FakeRow):
    def box(self):
        return FakeRow(self.rec)

    def column(self):
        return FakeRow(self.rec)

    def row(self, align=False):
        return FakeRow(self.rec)

    def separator(self):
        pass

    def template_list(self, *a, **k):
        pass


def draw_panel():
    """真正调用面板 draw()，返回收集到的所有文字。"""
    rec = []
    layout = FakeLayout(rec)

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
    fake.layout = layout
    fake.draw(bpy.context)
    return rec


PORT = mock_llm.start()
api_client.PROVIDERS["custom"] = {
    "label": "Mock", "format": "openai",
    "base_url": "http://127.0.0.1:%d/v1" % PORT,
    "models": ["m"], "docs": "",
}

ANSWER = "你好。需要做什么？建模、白膜、视频转骨骼动画都可以说。"


def pump(thread, timeout=40):
    from blockout_helper.dispatch import _TIMER_TICK
    deadline = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        _TIMER_TICK()
        ui_ai._poll_body()
        time.sleep(0.005)
    thread.join(timeout=5)
    for _ in range(8):
        _TIMER_TICK()
        ui_ai._poll_body()
        time.sleep(0.01)
        if not ui_ai._PENDING:
            break


def reset_all():
    st = bpy.context.window_manager.blockout_ai
    st.active_sid = ""
    st.lines.clear()
    st.last_answer = ""
    st.last_error = ""
    st.status = "就绪"
    ui_ai._SESSIONS.clear()
    bpy.app.driver_namespace["_bo_session"] = None


def t_draw_shows_answer():
    """核心：发完一轮后，draw() 的文字里必须出现答案。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai

    s = ui_ai._new_session()
    ui_ai._switch_to(s.sid)

    mock_llm.reset([mock_llm.text_resp(ANSWER)])
    s.auto_allow = True
    s.add_user("你好")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    assert st.last_answer == ANSWER, "属性没拿到答案：%r" % st.last_answer

    rec = draw_panel()
    joined = " ".join(rec)
    print("       draw 文字块数:", len(rec))
    head = ANSWER[:20]
    assert any(head in t for t in rec), \
        "面板没画出答案内容。draw 前 12 条：%r" % rec[:12]
    print("       面板包含答案 ✓")


def t_draw_after_new_session():
    """新建会话后 draw() 不能崩（且答案框消失是正常的）。"""
    st = bpy.context.window_manager.blockout_ai
    s2 = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s2.sid)

    rec = draw_panel()
    joined = " ".join(rec)
    assert "AI 回答" not in joined or st.last_answer == "", \
        "空会话却画了答案"
    # 但必须给出提示，让用户知道答案在别的会话里
    assert any("切换" in t or "无回答" in t or "历史" in t for t in rec), \
        "空会话没有任何提示，用户会以为答案丢了。draw：%r" % rec[:14]
    print("       空会话有提示 ✓")


def t_draw_switch_back():
    """切回有答案的会话，draw() 要重新画出答案。"""
    st = bpy.context.window_manager.blockout_ai
    sid_with_answer = None
    for sid, s in ui_ai._SESSIONS.items():
        if ui_ai._session_last_answer(s) == ANSWER:
            sid_with_answer = sid
            break
    assert sid_with_answer, "找不到有答案的会话"

    ui_ai._switch_to(sid_with_answer)
    rec = draw_panel()
    assert any(ANSWER[:20] in t for t in rec), "切回后没画答案内容"
    print("       切回后重新画出 ✓")


def t_draw_no_exception_on_empty():
    """全新状态 draw() 不能抛异常。"""
    reset_all()
    rec = draw_panel()
    assert len(rec) > 0, "draw 没产生任何文字"


def t_draw_no_ghost_session():
    """★ draw() 绝不能创建会话。

    之前 draw 里调 _get_session()，每重绘一次就可能造一个幽灵会话：
    列表里冒出「会话 2 / 会话 3」，答案却留在真正的会话里。
    """
    reset_all()
    st = bpy.context.window_manager.blockout_ai
    assert len(ui_ai._SESSIONS) == 0

    for _ in range(20):
        draw_panel()

    n = len(ui_ai._SESSIONS)
    print("       20 次重绘后会话数:", n)
    assert n == 0, "draw() 造出了 %d 个幽灵会话" % n
    assert st.active_sid == "", "draw() 改了 active_sid：%r" % st.active_sid


def t_switch_syncs_highlight():
    """切会话后，列表高亮必须指向同一个会话。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai

    sa = ui_ai._new_session()
    sb = ui_ai._new_session()
    ui_ai._sync_session_list()

    ui_ai._switch_to(sa.sid)
    hi = st.sessions[st.session_index].sid
    assert hi == sa.sid, "高亮=%s 但 active=%s" % (hi, sa.sid)

    ui_ai._switch_to(sb.sid)
    hi = st.sessions[st.session_index].sid
    assert hi == sb.sid, "高亮=%s 但 active=%s" % (hi, sb.sid)
    print("       高亮与 active 一致 ✓")


def t_other_session_event_dropped():
    """后台旧会话的迟到事件不能写进当前面板。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai

    sa = ui_ai._new_session()
    sb = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(sb.sid)          # 当前看 B

    # A 那边迟到的 final
    ui_ai.post_event("final", "这是A的答案", sa.sid)
    ui_ai._poll_body()

    assert st.last_answer != "这是A的答案", \
        "A 的答案串到 B 的面板了：%r" % st.last_answer
    assert len(st.lines) == 0, "A 的事件在 B 上建了行"
    print("       跨会话事件已隔离 ✓")


def t_other_session_hint():
    """当前会话没答案、别的会话有时，面板要给提示和跳转按钮。"""
    st = bpy.context.window_manager.blockout_ai
    sa = [s for s in ui_ai._SESSIONS.values()
          if s.sid == st.last_answer_sid or True][0]

    # 造一个「A 有答案、当前在 B」的状态
    reset_all()
    a = ui_ai._new_session()
    b = ui_ai._new_session()
    a.messages.append({"role": "assistant", "content": ANSWER})
    ui_ai._sync_session_list()
    ui_ai._switch_to(b.sid)

    # 模拟 A 的答案还挂在面板状态上（切会话前写入的）
    st.last_answer = ANSWER
    st.last_answer_sid = a.sid

    rec = draw_panel()
    assert any("另一会话" in t for t in rec), \
        "没有跨会话提示，用户会以为回复丢了：%r" % rec[:16]
    assert any("去看" in t for t in rec), "没有跳转按钮"

    # 现在允许显示别的会话的答案，但**必须标注来源**，
    # 否则用户会以为这是当前会话的回复（切过去又看不到，二次困惑）。
    joined = " ".join(rec)
    if ANSWER[:20] in joined:
        assert any("另一会话" in t for t in rec), \
            "显示了 A 的答案却没标明来自别的会话"
        assert any(a.title[:6] in t for t in rec), "没写明是哪个会话的答案"
    print("       跨会话提示 + 跳转 ✓")


check("draw 画出答案", t_draw_shows_answer)
check("新建会话后 draw 不崩且有提示", t_draw_after_new_session)
check("切回会话重新画出", t_draw_switch_back)
check("空状态 draw 不抛异常", t_draw_no_exception_on_empty)
check("draw 不造幽灵会话", t_draw_no_ghost_session)
check("切会话同步列表高亮", t_switch_syncs_highlight)
check("跨会话事件隔离", t_other_session_event_dropped)
check("跨会话答案给提示", t_other_session_hint)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
