"""验证：AI 助手不通 vs 连通性自检通 —— 隔断到底在哪

用户现场：
    连通性自检 → 正常回复
    AI 助手   → 面板显示「2 条」，但没有任何回答框

"2 条" = user + assistant 都在会话里，所以请求**成功发出并返回了**，
只是 text 为空 → 旧代码在空回复时**不发 final** → 面板上什么框都没有。

本测试锁三件事：
  1. 空回复也必须发 final（面板要有明确提示，不能白屏）
  2. reasoning_content 里有内容时要显示出来
  3. draw() 不依赖 timer 也能把事件刷到面板上
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
# 假 layout（记录文字，用来断言面板真的画了）
# ═══════════════════════════════════════════════════════════════════

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


class FakeOp:
    def __getattr__(self, name):
        return self


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
    bpy.app.driver_namespace["_bo_session"] = None
    ui_ai._PENDING.clear()


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


def fresh_session():
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    s.auto_allow = True
    return s


# ═══════════════════════════════════════════════════════════════════
# 1. 空回复：必须发 final，面板必须有框
# ═══════════════════════════════════════════════════════════════════

def t_empty_reply_emits_final():
    s = fresh_session()
    # mock 返回官方那种空 content（thinking 模式吞掉正文的典型形态）
    # ⚠ 要给两份：流式返回空内容时 agent 会回落非流式，再发一次请求。
    empty = {
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": ""}}],
        "usage": {"total_tokens": 7},
    }
    mock_llm.reset([empty, empty])

    s.add_user("你好")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    st = bpy.context.window_manager.blockout_ai
    print("       会话条数:", s.message_count())
    print("       last_answer:", repr(st.last_answer[:50]))

    # 关键：2 条消息 + 空回复时，面板上**必须**有内容
    assert s.message_count() == 2, "应该是 user+assistant 两条"
    assert st.last_answer, "空回复时面板完全空白 —— 用户会以为回复丢了"
    assert "空回复" in st.last_answer or "没有" in st.last_answer, \
        "空回复的提示不明确：%r" % st.last_answer

    rec = draw_panel()
    assert any("空回复" in t or "没返回" in t for t in rec), \
        "空回复内容没画出来：%r" % rec[:12]


def t_reasoning_only_is_shown():
    """内容只在 reasoning_content 里时，要显示出来而不是当空回复。"""
    s = fresh_session()
    reasoning = {
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "",
                                 "reasoning_content": "场景里有 27 个对象，主要是楼和人群。"}}],
        "usage": {"total_tokens": 30},
    }
    mock_llm.reset([reasoning, reasoning])

    s.add_user("场景里有什么")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    st = bpy.context.window_manager.blockout_ai
    print("       last_answer:", repr(st.last_answer[:60]))
    assert st.last_answer, "只有 reasoning 内容时面板空白"
    assert "27 个对象" in st.last_answer, \
        "reasoning 内容没显示出来：%r" % st.last_answer


# ═══════════════════════════════════════════════════════════════════
# 2. 正常回复走通（对照组，证明链路本身没坏）
# ═══════════════════════════════════════════════════════════════════

ANSWER = "你好。需要做什么都可以说。"


def t_normal_reply_works():
    s = fresh_session()
    mock_llm.reset([mock_llm.text_resp(ANSWER)])
    s.add_user("你好")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    st = bpy.context.window_manager.blockout_ai
    assert st.last_answer == ANSWER, "正常回复没显示：%r" % st.last_answer
    rec = draw_panel()
    assert any(ANSWER[:12] in t for t in rec), \
        "回答内容没画出来：%r" % rec[:12]
    print("       正常链路通过 ✓")


# ═══════════════════════════════════════════════════════════════════
# 3. ★ draw 不依赖 timer
# ═══════════════════════════════════════════════════════════════════

def t_draw_drains_without_timer():
    """核心：timer 完全没跑，只靠 draw() 也要把事件刷出来。

    这正是用户现场的差别：连通性自检是同步写属性、不依赖 timer；
    AI 助手全靠 timer，一旦 timer 没在跑，日志有、UI 永远没有。
    """
    s = fresh_session()
    st = bpy.context.window_manager.blockout_ai

    # 停掉 timer，模拟「timer 没在跑」
    ui_ai._stop_poll()

    # 后台线程正常跑完（没有 timer 帮忙泵事件）
    mock_llm.reset([mock_llm.text_resp("定时器死了也要显示这句。")])
    s.add_user("测试")
    th = threading.Thread(target=agent.run_turn,
                          args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    th.join(timeout=30)
    assert not th.is_alive(), "后台线程没结束"

    print("       事件缓冲:", len(ui_ai._PENDING))
    assert ui_ai._PENDING, "后台没产生事件（测试本身有问题）"

    # ★ 只调 draw()，不调 _poll_body()，不跑 timer
    rec = draw_panel()

    assert st.last_answer, "timer 没跑时 draw 没能把答案刷出来"
    assert "定时器死了" in st.last_answer, \
        "draw 刷出的内容不对：%r" % st.last_answer
    assert any("定时器死了" in t for t in rec), "draw 没画内容"
    print("       仅靠 draw 恢复显示 ✓")

    ui_ai._ensure_poll()


def t_draw_reentry_safe():
    """draw 里调 _poll_body，_poll_body 又 tag_redraw —— 不能递归爆炸。"""
    fresh_session()
    st = bpy.context.window_manager.blockout_ai
    st.last_answer = "递归测试"
    st.last_answer_sid = st.active_sid

    ui_ai._PENDING.append(("final", "递归测试2", st.active_sid))

    # 打桩：让 tag_redraw 同步回调 draw（模拟最坏情况）
    calls = {"n": 0}
    orig = ui_ai._tag_redraw

    def reentrant_redraw():
        calls["n"] += 1
        if calls["n"] < 5:
            draw_panel()          # 模拟重绘立刻触发 draw

    ui_ai._tag_redraw = reentrant_redraw
    try:
        draw_panel()
    finally:
        ui_ai._tag_redraw = orig

    print("       重入次数:", calls["n"])
    assert calls["n"] <= 2, "tag_redraw 被反复触发（%d 次），有递归风险"


check("空回复也发 final", t_empty_reply_emits_final)
check("只有 reasoning 也显示", t_reasoning_only_is_shown)
check("正常回复走通", t_normal_reply_works)
check("draw 不依赖 timer", t_draw_drains_without_timer)
check("draw 重入安全", t_draw_reentry_safe)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
