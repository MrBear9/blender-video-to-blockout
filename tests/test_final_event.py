"""验证 final 事件能让面板显示完整回答

模拟真实事件序列：think → raw → progress 打断 → stream 块 → info → final
关键：progress/raw 穿插会让流式行散掉，final 必须能把答案补回来。
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


def t_final_recovers_after_interrupt():
    """真实故障复现：progress 事件打断流式，final 要能救回来。"""
    st = reset()
    ANS = "你好。需要做什么？建模、白膜、视频转骨骼动画都可以说。"

    # 真实顺序：think → raw → stream（被打断）→ think → stream → info → final
    seq = [
        ("think", "第 1 轮：正在请求模型…"),
        ("raw", "STREAM POST https://api.deepseek.com | 8053 bytes"),
        ("stream", "你好"),
        ("stream", "。需要"),
        ("think", "已接收 0 块 / 290 字节 / 0s"),   # ← 打断流式行
        ("stream", "做什么"),
        ("stream", "？"),
        ("raw", "STREAM 完成：18 块"),
        ("info", "模型返回（耗时 0.9s）"),
        ("final", ANS),
        ("done", "完成（1 轮，0.9s）"),
    ]
    for lv, tx in seq:
        ui_ai.post_event(lv, tx)
    ui_ai._poll_body()

    print("       面板内容：")
    for x in st.lines:
        print("         [%-6s] %s" % (x.level, x.text[:55]))

    # 关键断言：必须有一行 say，且内容是完整答案
    says = [x.text for x in st.lines if x.level == "say"]
    assert says, "面板上没有任何 say 行！"
    assert ANS in says, "完整答案没出现在 say 行里：%s" % says


def t_final_without_stream():
    """非流式路径：没有 stream 块，只有 final。"""
    st = reset()
    ui_ai.post_event("think", "第 1 轮")
    ui_ai.post_event("final", "这是非流式的完整回答")
    ui_ai.post_event("done", "完成")
    ui_ai._poll_body()

    says = [x.text for x in st.lines if x.level == "say"]
    assert says == ["这是非流式的完整回答"], says


def t_final_overwrites_fragmented():
    """流式不建行；final 只产生一行完整答案。"""
    st = reset()
    ui_ai.post_event("user", "我的问题")
    ui_ai._poll_body()

    for c in ["A", "B", "C"]:
        ui_ui = ui_ai.post_event("stream", c)
        ui_ai.post_event("info", "打断")
        ui_ai._poll_body()

    before = len([x for x in st.lines if x.level == "say"])
    assert before == 0, "流式不该产生 say 行，实际 %d" % before

    ui_ai.post_event("final", "ABC 完整")
    ui_ai._poll_body()

    says = [x.text for x in st.lines if x.level == "say"]
    assert says == ["ABC 完整"], "应只有一行完整答案：%s" % says
    assert st.last_answer == "ABC 完整", st.last_answer
    # user 行要保留
    assert any(x.level == "user" for x in st.lines), "user 行被误删了"


def t_zh_ui():
    """UI 文案要是中文。"""
    import blockout_helper
    src = open(blockout_helper.__file__, encoding="utf-8").read()
    assert "视频转白膜" in src, "面板标题不是中文"
    assert "导入 recon.json" in src
    assert "生成骨骼" in src
    assert "运镜" in src
    # 不能有 BOM
    raw = open(blockout_helper.__file__, "rb").read()
    assert raw[:3] != b"\xef\xbb\xbf", "文件有 BOM"


check("progress 打断后 final 能救回", t_final_recovers_after_interrupt)
check("无流式的 final", t_final_without_stream)
check("final 覆盖而不是新增", t_final_overwrites_fragmented)
check("UI 是中文", t_zh_ui)

bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
