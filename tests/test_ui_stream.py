"""验证流式事件真的能刷进 UI 面板属性

这是"日志有输出但面板空白"的直接检验点。
"""
import sys
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import blockout_helper as bh
bh.register()

from blockout_helper import ui_ai

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


def reset():
    st = bpy.context.window_manager.blockout_ai
    st.lines.clear()
    ui_ai._PENDING.clear()
    return st


def t_stream_accumulates():
    """流式块**不建行**，只更新 status。

    这是 v1.5 的设计：流式只负责"正在输入"的观感，
    真正的答案由 final 事件写入。好处是永远不会产生碎片行
    （之前 19 个块散成 19 行，互相覆盖，面板最后是空的）。
    """
    st = reset()
    for chunk in ["你好", "。需要", "做什么", "？"]:
        ui_ai.post_event("stream", chunk)
    ui_ai._poll_body()

    assert len(st.lines) == 0, "流式不该建行，实际 %d 行" % len(st.lines)
    assert "接收中" in st.status, st.status

    # final 才产生答案行
    ui_ai.post_event("final", "你好。需要做什么？")
    ui_ui_poll = ui_ai._poll_body()
    assert len(st.lines) == 1, len(st.lines)
    assert st.lines[0].text == "你好。需要做什么？"
    assert st.last_answer == "你好。需要做什么？"


def t_stream_across_polls():
    """跨多次 tick 的流式块也不该建行。"""
    st = reset()
    ui_ai.post_event("stream", "第一段")
    ui_ai._poll_body()
    ui_ai.post_event("stream", "第二段")
    ui_ai._poll_body()

    assert len(st.lines) == 0, "跨 tick 流式也不该建行，实际 %d" % len(st.lines)


def t_stream_then_info_breaks():
    """普通事件正常建行；final 产生答案行。"""
    st = reset()
    ui_ai.post_event("think", "第 1 轮")
    ui_ai._poll_body()
    ui_ai.post_event("stream", "流式片段")     # 不建行
    ui_ai._poll_body()
    ui_ai.post_event("info", "模型返回（耗时 0.7s）")
    ui_ai._poll_body()
    ui_ai.post_event("final", "AI 的回答")
    ui_ui = ui_ai._poll_body()

    texts = [(x.level, x.text) for x in st.lines]
    print("       行：", texts)
    says = [x.text for x in st.lines if x.level == "say"]
    assert says == ["AI 的回答"], says
    assert any("耗时" in x.text for x in st.lines if x.level == "info")


def t_reason_accumulates():
    """思考内容不建行，只更新状态。"""
    st = reset()
    for c in ["思考", "中…"]:
        ui_ai.post_event("reason", c)
    ui_ai._poll_body()

    assert len(st.lines) == 0, "reason 不该建行：%d" % len(st.lines)
    assert "思考" in st.status, st.status


def t_raw_shows():
    """raw 级别的诊断信息要能进面板。"""
    st = reset()
    ui_ui = ui_ai.post_event("raw", "STREAM POST https://api.deepseek.com | 8053 bytes")
    ui_ai._poll_body()
    assert len(st.lines) == 1
    assert st.lines[0].level == "raw"
    assert "deepseek" in st.lines[0].text


def t_full_conversation_flow():
    """模拟一次完整对话产生的事件序列。"""
    st = reset()
    seq = [
        ("think", "第 1 轮：正在请求模型…"),
        ("raw", "STREAM POST ... | tools=18"),
        ("stream", "你好"),
        ("stream", "。需要"),
        ("stream", "做什么？"),
        ("info", "模型返回（耗时 0.7s）"),
        ("final", "你好。需要做什么？"),
        ("done", "完成（1 轮，0.7s）"),
    ]
    for lv, tx in seq:
        ui_ai.post_event(lv, tx)
    ui_ai._poll_body()

    print("       最终面板：")
    for x in st.lines:
        print("         [%s] %s" % (x.level, x.text[:40]))

    says = [x.text for x in st.lines if x.level == "say"]
    assert says == ["你好。需要做什么？"], says
    assert st.last_answer == "你好。需要做什么？", st.last_answer


check("流式块合成一行", t_stream_accumulates)
check("跨 tick 仍是一行", t_stream_across_polls)
check("普通事件打断流式行", t_stream_then_info_breaks)
check("思考内容累积", t_reason_accumulates)
check("诊断信息进面板", t_raw_shows)
check("完整对话面板有回答", t_full_conversation_flow)

bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
