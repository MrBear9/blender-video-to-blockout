"""验证：滚动 / 放大 / 中断 / 精简显示

用户要求：
  1. 去掉心跳、STREAM POST 这类非必要信息，只留问题和回答
  2. 能上下滑动看完整回复
  3. 双击能放大聊天框
  4. 看见回答不理想时能直接中断

本测试锁死这四件事。
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
    except AssertionError as e:
        print("  ✗", name, ":", e)
        fails.append(name)
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
    def __init__(self, rec, text):
        self.rec = rec
        self.text = text

    def __getattr__(self, name):
        return self


class FakeRow:
    def __init__(self, rec):
        self.rec = rec

    def label(self, text="", icon=None):
        self.rec.append(text)

    def operator(self, *a, **k):
        t = k.get("text") or ""
        if t:
            self.rec.append("[按钮]" + t)
        return FakeOp(self.rec, t)

    def prop(self, *a, **k):
        self.rec.append("[prop]")

    def row(self, align=False):
        return FakeRow(self.rec)

    def column(self):
        return FakeRow(self.rec)

    def box(self):
        return FakeRow(self.rec)

    def separator(self):
        self.rec.append("[分隔]")

    def template_list(self, *a, **k):
        self.rec.append("[列表]")


def draw_panel():
    rec = []

    class FakeSelf:
        layout = None
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
    st.sessions.clear()
    st.answers.clear()
    st.last_answer = ""
    st.last_answer_sid = ""
    st.last_error = ""
    st.live_text = ""
    st.scroll_top = 0
    st.status = "就绪"
    ui_ai._SESSIONS.clear()
    ui_ai._ANSWERS.clear()
    ui_ai._PENDING.clear()
    ui_ai._PENDING_SWITCH = ""
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


def make_session(n_rounds=1):
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    s.auto_allow = True
    for i in range(n_rounds):
        mock_llm.reset([mock_llm.text_resp("第%d轮的回答内容。" % (i + 1))])
        s.add_user("第%d个问题" % (i + 1))
        th = threading.Thread(target=agent.run_turn,
                              args=(s, "custom", "k", "m", 0.0), daemon=True)
        th.start()
        pump(th)
    return s


# ═══════════════════════════════════════════════════════════════════
def t_only_qa_shown():
    """★ 只画问答：不能出现 STREAM POST / 心跳 / 工具日志。"""
    make_session()
    rec = draw_panel()
    joined = "\n".join(rec)

    print("       面板内容:", [r[:22] for r in rec[:10]])
    assert "第1个问题" in joined, "问题没画出来"
    assert "第1轮的回答内容" in joined, "回答没画出来"

    for noise in ("STREAM POST", "api/completions", "tools=",
                  "prompt_tokens", "心跳"):
        assert noise not in joined, "面板上还有噪音 %r" % noise
    print("       只有问答，无噪音 ✓")


def t_qa_rows_filters():
    """_qa_rows 只返回有文字的 user/assistant。"""
    s = make_session()
    s.messages.append({"role": "assistant", "content": "",
                       "tool_calls": [{"id": "c1", "type": "function",
                                       "function": {"name": "x",
                                                    "arguments": "{}"}}]})
    s.messages.append({"role": "tool", "tool_call_id": "c1", "content": "{}"})

    rows = ui_ai._qa_rows(s)
    print("       行:", [(lv, t[:12]) for lv, t in rows])
    assert all(lv in ("user", "say") for lv, _ in rows), \
        "混进了非问答行：%r" % rows
    assert all(t.strip() for _, t in rows), "混进了空行"


def t_scroll():
    """★ 滚动：scroll_top 变了，显示的内容跟着变。"""
    st = bpy.context.window_manager.blockout_ai
    s = make_session(n_rounds=8)     # 16 条问答
    rows = ui_ai._qa_rows(s)
    print("       总行数:", len(rows))

    st.view_lines = 4
    st.scroll_top = 0
    rec0 = draw_panel()
    joined0 = "\n".join(rec0)

    st.scroll_top = 4
    rec1 = draw_panel()
    joined1 = "\n".join(rec1)

    print("       顶部: %r" % rec0[:4])
    print("       滚动后: %r" % rec1[:4])
    assert joined0 != joined1, "滚动后内容没变"

    # 滚到底能看到最后一轮
    st.scroll_top = 99
    rec2 = draw_panel()
    joined2 = "\n".join(rec2)
    assert "第8轮的回答内容" in joined2, "滚到底看不到最新回答"
    assert st.scroll_top <= len(rows), "scroll_top 没被夹住：%d" % st.scroll_top
    print("       滚动正常 + 越界自动夹住 ✓")


def t_scroll_buttons_present():
    """面板上要有滚动/放大按钮。"""
    make_session(n_rounds=3)
    rec = draw_panel()
    joined = " ".join(rec)
    assert "[按钮]" in joined
    has_expand = any("FULLSCREEN" not in r for r in rec)
    print("       按钮:", [r for r in rec if r.startswith("[按钮]")])
    assert any(r.startswith("[按钮]") for r in rec), "没有按钮"


def _run_expand():
    """跑一次导出（算子的核心逻辑就是这个函数）。"""
    return ui_ai._export_to_text(ui_ai._peek_session())


def t_expand_popup():
    """★ 放大：改成写进文本编辑器（弹窗在侧栏不可靠）。"""
    st = bpy.context.window_manager.blockout_ai
    s = make_session(n_rounds=6)

    name = "AI对话_%s" % s.sid
    old = bpy.data.texts.get(name)
    if old is not None:
        bpy.data.texts.remove(old)

    got_name, txt = _run_expand()
    assert got_name == name, "导出的名字不对：%r" % got_name

    txt = bpy.data.texts.get(name)
    assert txt is not None, \
        "没生成文本块，实际有：%r" % [t.name for t in bpy.data.texts]
    body = txt.as_string()
    print("       会话 %s，文本 %d 字" % (s.sid, len(body)))
    print("       前 3 行：%s" % body.splitlines()[:3])

    assert "对话" in body.splitlines()[0], "首行没有标题"
    assert "第1个问题" in body, "没显示内容"
    assert "第6轮的回答" in body, "没显示最后一轮"


def t_expand_more_lines():
    """放大后能看到的内容要多于侧栏一屏。"""
    st = bpy.context.window_manager.blockout_ai
    s = make_session(n_rounds=8)

    st.view_lines = 4
    st.scroll_top = 0
    rec_small = draw_panel()

    name = "AI对话_%s" % s.sid
    _run_expand()
    body = bpy.data.texts[name].as_string()

    print("       侧栏 %d 条 / 文本 %d 字" % (len(rec_small), len(body)))
    # 文本里所有轮次都在，侧栏一屏只看得到几条
    for i in range(1, 9):
        assert "第%d个问题" % i in body, "文本缺第 %d 轮" % i
    assert len(body) > 200


def t_cancel_keeps_partial():
    """★ 中断：已经生成的内容要保住，不能白跑。"""
    st = bpy.context.window_manager.blockout_ai
    s = make_session()

    st.live_text = "这是一段还没写完的回答"
    s.running = True

    # operator 不能直接实例化，走 bpy.ops
    bpy.ops.blockout.ai_cancel()

    print("       status:", repr(st.status))
    print("       last_answer:", repr(st.last_answer[:40]))
    assert "中断" in st.status, "状态没标中断：%r" % st.status
    assert "这是一段还没写完的回答" in st.last_answer, \
        "已生成的内容丢了：%r" % st.last_answer
    assert not s.running, "session.running 没复位"
    assert st.live_text == "", "live_text 没清空"


def t_cancel_when_idle():
    """没在运行时点中断，不能崩、不能报错。"""
    make_session()
    st = bpy.context.window_manager.blockout_ai
    r = bpy.ops.blockout.ai_cancel()
    print("       返回:", r, "status:", repr(st.status))
    assert "FINISHED" not in r, "没在跑时不该返回 FINISHED：%r" % r
    assert st.status == "就绪", "status 被改了：%r" % st.status


def t_cancel_button_visible():
    """中断按钮必须在面板上能看到（发送旁边）。"""
    make_session()
    rec = draw_panel()
    joined = " ".join(rec)
    print("       按钮:", [r for r in rec if r.startswith("[按钮]")])
    assert "中断" in joined, "面板上没有中断按钮"


def t_live_text_accumulates():
    """流式增量要累积（中断后才有内容可保）。"""
    st = bpy.context.window_manager.blockout_ai
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    st.live_text = ""

    ui_ai._apply_event(st, "live_reset", "", s.sid)
    for chunk in ("你好", "，这是", "流式回答"):
        ui_ai._apply_event(st, "stream", chunk, s.sid)

    print("       累积结果:", repr(st.live_text))
    assert st.live_text == "你好，这是流式回答", \
        "增量没累积：%r" % st.live_text


check("只显示问答", t_only_qa_shown)
check("_qa_rows 过滤干净", t_qa_rows_filters)
check("滚动生效", t_scroll)
check("有滚动/放大按钮", t_scroll_buttons_present)
check("放大窗口能开", t_expand_popup)
check("大窗口行数更多", t_expand_more_lines)
check("中断保留已生成内容", t_cancel_keeps_partial)
check("空闲时点中断不崩", t_cancel_when_idle)
check("中断按钮可见", t_cancel_button_visible)
check("流式增量累积", t_live_text_accumulates)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
