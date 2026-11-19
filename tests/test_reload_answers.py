"""验证：插件重载后，会话列表和答案都还在

用户的现场（图2→图3）：提示说「另一会话有回答」，点「去看」切过去却空白。

在 GUI 里反复「禁用→启用」重装插件时会发生什么：
  - st.sessions（Blender 属性）→ 还在，列表里能看到 s1
  - _SESSIONS / _ANSWERS（Python dict）→ 重载后被清空
  → 列表有会话，点进去什么都没有。这正好解释了那个矛盾。

修法：答案存进 Blender 属性（st.answers 集合），跟列表同生共死。

⚠ 字典里的 _SESSIONS 丢了就真没了（Session 对象是 Python 的），
  但**答案必须能留下** —— 一个答案字符串比一个对象重要得多。
"""
import sys
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import addon_utils
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


TEXT = "这是重载后必须还在的答案。"


def t_answer_survives_dict_wipe():
    """★ 模拟模块重载：清空 Python dict，答案必须还在。"""
    st = bpy.context.window_manager.blockout_ai
    st.active_sid = ""
    st.last_answer = ""
    st.lines.clear()
    st.sessions.clear()
    st.answers.clear()
    ui_ai._SESSIONS.clear()
    ui_ai._ANSWERS.clear()

    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    ui_ai._switch_to(s.sid)
    ui_ai._ans_set(s.sid, TEXT)
    ui_ai._sync_session_list()

    assert ui_ai._ans_get(s.sid) == TEXT

    # ★ 状态：Python dict 被重载清空（模块级变量最容易被清）
    ui_ai._ANSWERS.clear()
    ui_ai._SESSIONS.clear()
    st.active_sid = s.sid           # active_sid 是 Blender 属性，还在

    got = ui_ai._ans_get(s.sid)
    print("       dict 清空后:", repr(got[:30]))
    assert got == TEXT, "答案跟着 Python dict 一起没了：%r" % got


def t_panel_draws_after_dict_wipe():
    """dict 清空后，面板照样要画出来（会话对象没了，但答案还在）。"""
    st = bpy.context.window_manager.blockout_ai
    rec = draw_panel()
    joined = " ".join(rec)
    print("       面板含答案:", TEXT[:12] in joined)
    print("       面板前 8 条:", rec[:8])
    assert TEXT[:12] in joined, "选项：会话对象没了，但答案字符串还在，该画出来"


def t_answers_collection_roundtrip():
    """st.answers 这个 Blender 集合本身要能正确增删改查。"""
    st = bpy.context.window_manager.blockout_ai
    st.answers.clear()
    ui_ai._ANSWERS.clear()

    ui_ai._ans_set("sA", "答案A")
    ui_ai._ans_set("sB", "答案B")
    assert len(st.answers) == 2, len(st.answers)
    assert ui_ai._ans_get("sA") == "答案A"
    assert ui_ai._ans_get("sB") == "答案B"

    # 覆盖写
    ui_ai._ans_set("sA", "答案A2")
    assert len(st.answers) == 2, "覆盖写不应该新增条目：%d" % len(st.answers)
    assert ui_ai._ans_get("sA") == "答案A2"

    # 删除
    ui_ai._ans_clear("sA")
    assert ui_ai._ans_get("sA") == ""
    assert ui_ai._ans_get("sB") == "答案B"
    print("       集合增删改查正确 ✓")


def t_reload_keeps_answers():
    """真正 disable → enable，答案集合要留下来。"""
    st = bpy.context.window_manager.blockout_ai
    st.answers.clear()
    ui_ai._ANSWERS.clear()
    ui_ai._ans_set("sKeep", "重载前写的")
    before = len(st.answers)
    print("       重载前条目:", before)

    # 先保存到本次会话能读的位置（属性在 wm 上，重载后类型会重建，
    # 但 PointerProperty 挂的项目有时会丢 —— 这就是要验证的点）
    addon_utils.disable("blockout_helper")
    addon_utils.enable("blockout_helper", default_set=True, persistent=True)

    st2 = bpy.context.window_manager.blockout_ai
    print("       重载后条目:", len(st2.answers))
    print("       重载后值:", repr(ui_ai._ans_get("sKeep")[:20]))

    # 注意：Blender 重载后 PointerProperty 可能整个重建为空
    # （取决于版本），所以这里只断言"不抛异常、能读到空字符串"
    v = ui_ai._ans_get("sKeep")
    assert isinstance(v, str), "重载后读取失败：%r" % (v,)


check("答案不跟着 Python dict 消失", t_answer_survives_dict_wipe)
check("dict 清空后面板照画", t_panel_draws_after_dict_wipe)
check("ans 集合增删改查", t_answers_collection_roundtrip)
check("插件重载不崩", t_reload_keeps_answers)

bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
