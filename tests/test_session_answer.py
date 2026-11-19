"""验证：切换会话后答案不丢

复现用户截图的问题：s3 有答案，新建 s4 后切过去，答案消失。
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

ANSWER_A = "你好。需要做什么？建模、白膜、视频转骨骼动画都可以说。"


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


def run_round(session, answer):
    mock_llm.reset([mock_llm.text_resp(answer)])
    session.auto_allow = True
    session.add_user("hi")

    th = threading.Thread(
        target=agent.run_turn,
        args=(session, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)


def t_switch_keeps_answer():
    """核心：在 s1 拿到答案，切到 s2 再切回 s1，答案必须还在。"""
    st = bpy.context.window_manager.blockout_ai
    st.active_sid = ""
    ui_ai._SESSIONS.clear()
    st.lines.clear()
    st.last_answer = ""

    # 会话 A：拿到答案
    bpy.app.driver_namespace["_bo_session"] = None
    sa = ui_ai._new_session()
    ui_ai._switch_to(sa.sid)
    run_round(sa, ANSWER_A)

    assert st.last_answer == ANSWER_A, "会话A 没拿到答案：%r" % st.last_answer
    print("       A 答案:", st.last_answer[:30])

    # 新建会话 B（模拟你点"新建"）
    sb = ui_ai._new_session()
    ui_ui = ui_ai._sync_session_list()
    ui_ai._switch_to(sb.sid)

    # B 是空的，答案应该消失（这是对的）
    assert st.last_answer == "", "空会话不该有答案，实际 %r" % st.last_answer
    print("       切到 B: 答案为空 ✓")

    # ★ 切回 A —— 答案必须回来
    ui_ai._switch_to(sa.sid)
    assert st.last_answer == ANSWER_A, \
        "切回 A 后答案丢了：%r" % st.last_answer
    print("       切回 A: 答案恢复 ✓")


def t_switch_replays_lines():
    """切会话要把历史重放出来。"""
    st = bpy.context.window_manager.blockout_ai
    sa = list(ui_ai._SESSIONS.values())[0]
    ui_ai._switch_to(sa.sid)

    levels = [st.lines[i].level for i in range(len(st.lines))]
    texts = [st.lines[i].text for i in range(len(st.lines))]
    print("       重放行数:", len(texts), levels)
    assert "user" in levels, "没有 user 行"
    assert "say" in levels, "没有 say 行"
    assert any(ANSWER_A in t for t in texts), "答案没在历史里"


def t_poll_recovers_from_history():
    """即使 _last_final_text 丢了，poll 也要能从历史里找回答案。"""
    st = bpy.context.window_manager.blockout_ai
    sa = list(ui_ai._SESSIONS.values())[0]
    ui_ai._switch_to(sa.sid)

    # 清掉面板答案 + 清掉 _last_final_text
    st.last_answer = ""
    sa._last_final_text = ""

    ui_ai._poll_body()
    print("       从历史恢复:", st.last_answer[:30])
    assert st.last_answer == ANSWER_A, \
        "poll 没能从历史恢复：%r" % st.last_answer


def t_new_session_empty():
    """新建会话必须是干净的。"""
    st = bpy.context.window_manager.blockout_ai
    sc = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(sc.sid)

    assert st.last_answer == "", "新会话有残留答案"
    assert len(st.lines) == 0, "新会话有残留行：%d" % len(st.lines)
    assert sc.message_count() == 0


check("切会话保住答案", t_switch_keeps_answer)
check("切会话重放历史", t_switch_replays_lines)
check("poll 从历史恢复", t_poll_recovers_from_history)
check("新建会话是干净的", t_new_session_empty)

mock_llm.stop()
bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
