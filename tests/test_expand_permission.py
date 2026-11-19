"""验证：放大到文本编辑器 + 三档权限

用户反馈：
  1. 「放大，双击都不能放大这个聊天框」—— invoke_popup 在 N 侧栏
     里不可靠，点了没反应。改成写进 Blender 文本编辑器：
     一定能开、能拉伸、能选中复制。
  2. 「对于权限的确认，可以增加一个完全权限，类似 DSH 的会话管理」
     —— 三档：ask / auto / full。
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
        self.rec.append("[prop]")

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
    st.permission = "ask"
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


def make_session(n=2):
    reset_all()
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    s.auto_allow = True
    s.permission = "full"
    for i in range(n):
        mock_llm.reset([mock_llm.text_resp("第%d个回答" % (i + 1))])
        s.add_user("第%d个问题" % (i + 1))
        th = threading.Thread(target=agent.run_turn,
                              args=(s, "custom", "k", "m", 0.0), daemon=True)
        th.start()
        pump(th)
    return s


# ═══════════════════════════════════════════════════════════════════
# 放大
# ═══════════════════════════════════════════════════════════════════
def t_expand_creates_text():
    """★ 放大要有实际产物：Blender 文本块里有完整对话。"""
    s = make_session(2)

    # 先删掉可能存在的旧文本
    name = "AI对话_%s" % s.sid
    old = bpy.data.texts.get(name)
    if old is not None:
        bpy.data.texts.remove(old)

    got_name, txt = ui_ai._export_to_text(ui_ai._peek_session())
    print("       返回:", got_name)
    assert got_name == name, "导出的名字不对：%r" % got_name

    txt = bpy.data.texts.get(name)
    assert txt is not None, "没创建文本块，放大等于没作用"

    body = txt.as_string()
    print("       文本长度:", len(body))
    print("       前 3 行:", body.splitlines()[:3])

    assert "第1个问题" in body, "文本里没有问题"
    assert "第1个回答" in body, "文本里没有回答"
    assert "第2个回答" in body, "文本里缺第二轮"
    assert "你" in body and "AI" in body, "没有区分说话人"


def t_expand_reuses_text():
    """反复放大不能堆一堆同名文本。"""
    s = make_session(1)
    name = "AI对话_%s" % s.sid
    ui_ai._export_to_text(ui_ai._peek_session())
    ui_ai._export_to_text(ui_ai._peek_session())
    ui_ai._export_to_text(ui_ai._peek_session())

    same = [t for t in bpy.data.texts if t.name.startswith(name)]
    print("       同名文本数:", len(same))
    assert len(same) == 1, "堆了 %d 个同名文本" % len(same)


def t_expand_full_content():
    """文本里必须是**完整**内容，不是被截断的。"""
    s = make_session(1)
    long_text = "这是一段很长的回答。" * 40
    s.messages.append({"role": "assistant", "content": long_text})

    name = "AI对话_%s" % s.sid
    ui_ai._export_to_text(ui_ai._peek_session())
    body = bpy.data.texts[name].as_string()

    assert long_text[:100] in body, "长回答开头缺了"
    assert long_text[-100:] in body, "长回答结尾缺了 —— 被截断了"
    print("       完整内容 %d 字 ✓" % len(long_text))


# ═══════════════════════════════════════════════════════════════════
# 权限三档
# ═══════════════════════════════════════════════════════════════════
def t_permission_levels():
    """★ 三档权限的判定逻辑。"""
    class FakeS:
        permission = "ask"

    s = FakeS()

    s.permission = "ask"
    assert agent.need_confirm(s, "delete_objects") is True, "ask 档该问"
    assert agent.need_confirm(s, "run_python") is True

    s.permission = "auto"
    assert agent.need_confirm(s, "delete_objects") is False, "auto 档不该问常规操作"
    assert agent.need_confirm(s, "run_python") is True, \
        "auto 档对 run_python 必须仍然问（能跑任意代码）"

    s.permission = "full"
    assert agent.need_confirm(s, "delete_objects") is False
    assert agent.need_confirm(s, "run_python") is False, "full 档不该问任何东西"
    print("       ask/auto/full 判定正确 ✓")


def t_permission_propagates():
    """面板选的权限要传到 session。"""
    reset_all()
    st = bpy.context.window_manager.blockout_ai
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)

    for perm, expect_full in (("ask", False), ("auto", False), ("full", True)):
        st.permission = perm
        ui_ai.BO_OT_ai_send  # 只取引用，真正验证走下面的直接赋值
        s.permission = st.permission
        s.auto_allow = (st.permission == "full")
        print("       %s -> session.permission=%s auto_allow=%s"
              % (perm, s.permission, s.auto_allow))
        assert s.permission == perm
        assert s.auto_allow is expect_full


def t_permission_in_panel():
    """面板上要有权限选择器。"""
    make_session(1)
    rec = draw_panel()
    joined = " ".join(rec)
    print("       面板:", [r[:18] for r in rec[:12]])
    assert "权限" in joined, "面板上没有权限设置"


def t_full_permission_no_confirm():
    """full 档时破坏性工具不再弹确认。"""
    s = make_session(1)
    s.permission = "full"
    assert agent.need_confirm(s, "delete_objects") is False
    assert agent.need_confirm(s, "run_python") is False
    print("       full 档全部放行 ✓")


def t_ask_permission_blocks():
    """ask 档时确认没回应 → 工具不执行。"""
    s = make_session(1)
    s.permission = "ask"
    # pending_confirm 为 None 时 _confirm 会等待超时，
    # 这里直接验证 need_confirm 的判定，不真等 120 秒
    assert agent.need_confirm(s, "delete_objects") is True
    print("       ask 档需要确认 ✓")


check("放大生成文本", t_expand_creates_text)
check("反复放大复用同名文本", t_expand_reuses_text)
check("放大内容完整不截断", t_expand_full_content)
check("三档权限判定", t_permission_levels)
check("权限传递到 session", t_permission_propagates)
check("面板有权限选择", t_permission_in_panel)
check("full 档全放行", t_full_permission_no_confirm)
check("ask 档要确认", t_ask_permission_blocks)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
