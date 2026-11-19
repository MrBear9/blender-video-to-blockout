"""用真实 DeepSeek 返回格式，端到端验证面板能显示答案

模拟你截图里的场景：日志有输出（text=27字），面板什么都看不到。
这次直接检查 st.last_answer —— 那是面板上「AI 回答」框的数据源。
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

# 你的真实答案
REAL_ANSWER = "你好。需要做什么？建模、白膜、视频转骨骼动画都可以说。"


def pump(thread, timeout=40):
    """模拟真实 GUI：线程跑时持续 poll，**结束后还要再 poll 几轮**。

    ⚠ 关键：agent 线程退出前会 emit 最后一批事件（final / done）。
    如果 poll 循环因为 th.is_alive() 为 False 就退出，
    这批事件永远停在 _PENDING 里 —— 面板上就是空白。
    真实 GUI 里 timer 每 0.2s 跑一次，总会处理掉；测试必须模拟这一点。
    """
    from blockout_helper.dispatch import _TIMER_TICK
    deadline = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        _TIMER_TICK()
        ui_ai._poll_body()
        time.sleep(0.005)
    thread.join(timeout=5)
    # 排空最后一批事件（真实 timer 会做这件事）
    for _ in range(5):
        _TIMER_TICK()
        ui_ai._poll_body()
        time.sleep(0.01)
        if not ui_ai._PENDING:
            break


def t_real_scenario():
    """完整链路：发消息 → 模型返回 → 面板上必须有答案。"""
    st = bpy.context.window_manager.blockout_ai
    st.lines.clear()
    st.last_answer = ""
    st.last_error = ""
    ui_ai._PENDING.clear()

    mock_llm.reset([mock_llm.text_resp(REAL_ANSWER)])

    s = ui_ai._get_session()
    s.reset()
    s.auto_allow = True
    s.add_user("你好")
    ui_ai.post_event("user", "你好")

    th = threading.Thread(
        target=agent.run_turn,
        args=(s, "custom", "k", "m", 0.0), daemon=True)
    th.start()
    pump(th)

    print("       last_answer = %r" % st.last_answer[:60])
    print("       status      = %r" % st.status)

    assert st.last_answer == REAL_ANSWER, \
        "面板答案不对：%r" % st.last_answer

    # 历史里也要有一行 say
    says = [st.lines[i].text for i in range(len(st.lines))
            if st.lines[i].level == "say"]
    assert REAL_ANSWER in says, "历史里没有 say 行：%s" % says


def t_error_also_shows():
    """API 失败时面板也要有明确文本，不能空白。"""
    st = bpy.context.window_manager.blockout_ai
    st.lines.clear()
    st.last_answer = ""
    st.last_error = ""
    ui_ai._PENDING.clear()

    saved = api_client.PROVIDERS["custom"]["base_url"]
    api_client.PROVIDERS["custom"]["base_url"] = "http://127.0.0.1:1/v1"
    try:
        s = ui_ai._get_session()
        s.reset()
        s.auto_allow = True
        s.add_user("测试")
        ui_ai.post_event("user", "测试")

        th = threading.Thread(
            target=agent.run_turn,
            args=(s, "custom", "k", "m", 0.0), daemon=True)
        th.start()
        pump(th)

        print("       失败后 last_answer = %r" % st.last_answer[:60])
        assert st.last_answer, "失败时面板也是空白！"
        assert "失败" in st.last_answer or "错误" in st.last_answer, \
            st.last_answer
    finally:
        api_client.PROVIDERS["custom"]["base_url"] = saved


def t_heartbeat():
    """心跳要递增，证明 timer 在跑。"""
    st = bpy.context.window_manager.blockout_ai
    h0 = st.heartbeat
    ui_ai._poll_body()
    ui_ai._poll_body()
    assert st.heartbeat != h0, "心跳没动：%d -> %d" % (h0, st.heartbeat)
    assert st.tick_time, "没有时间戳"


def t_no_remove_crash():
    """大量事件不能因集合操作崩掉。"""
    st = bpy.context.window_manager.blockout_ai
    st.lines.clear()
    ui_ai._PENDING.clear()

    for i in range(400):
        ui_ai.post_event("info", "事件 %d" % i)
    ui_ai._poll_body()

    assert len(st.lines) > 0, "400 个事件后集合空了"
    print("       400 个事件后行数：", len(st.lines))
    # 最后一条要在
    last = st.lines[len(st.lines) - 1].text
    assert "399" in last, last


def t_final_after_error():
    """错误路径必须也发 final（面板一定有文本）。"""
    st = bpy.context.window_manager.blockout_ai
    st.lines.clear()
    st.last_answer = ""
    ui_ai._PENDING.clear()

    saved = api_client.PROVIDERS["custom"]["base_url"]
    api_client.PROVIDERS["custom"]["base_url"] = "http://127.0.0.1:1/v1"
    try:
        s = agent.Session()
        evs = []
        s.on_event = lambda lv, tx: evs.append((lv, str(tx)))
        s.add_user("x")
        agent.run_turn(s, "custom", "k", "m", 0.0)
        finals = [e for e in evs if e[0] == "final"]
        assert finals, "错误路径没有 final 事件"
    finally:
        api_client.PROVIDERS["custom"]["base_url"] = saved


def t_fallback_after_thread_ends():
    """关键：agent 线程结束后，即使事件没被消费，poll 也要把答案补上。

    模拟真实最坏时序：
      1. 跑完 agent（不 poll）
      2. 清空 _PENDING（模拟"事件丢了"）
      3. 再 poll 一次
    答案仍必须出现 —— 靠的是 poll 从 session._last_final_text 兜底。
    """
    st = bpy.context.window_manager.blockout_ai
    st.lines.clear()
    st.last_answer = ""
    st.last_error = ""
    ui_ai._PENDING.clear()

    mock_llm.reset([mock_llm.text_resp("线程结束后兜底")])

    s = ui_ai._get_session()
    s.reset()
    s.auto_allow = True
    s.add_user("hi")

    from blockout_helper.dispatch import _TIMER_TICK
    result = {}

    def run():
        result["r"] = agent.run_turn(s, "custom", "k", "m", 0.0)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    deadline = time.time() + 40
    while th.is_alive() and time.time() < deadline:
        _TIMER_TICK()
        time.sleep(0.005)
    th.join(timeout=5)

    # ★ 故意丢弃所有待处理事件，模拟队列丢失
    ui_ai._PENDING.clear()
    assert st.last_answer == "", "前置：此时面板应该还是空的"

    # 只跑 poll（真实 GUI 每 0.2s 跑一次）
    ui_ai._poll_body()

    print("       poll 兜底后 last_answer = %r" % st.last_answer[:50])
    assert st.last_answer == "线程结束后兜底", \
        "poll 兜底没生效：%r" % st.last_answer


check("真实场景：面板显示答案", t_real_scenario)
check("失败也有文本", t_error_also_shows)
check("心跳递增", t_heartbeat)
check("400 事件不崩", t_no_remove_crash)
check("错误路径有 final", t_final_after_error)
check("线程结束后 poll 兜底", t_fallback_after_thread_ends)

mock_llm.stop()
bh.unregister()
print()
print("=" * 56)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
