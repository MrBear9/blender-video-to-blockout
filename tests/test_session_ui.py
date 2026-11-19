"""验证会话列表与清空功能真的有效（在真实 Blender 里）"""
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


def t_list_populated():
    """新建会话后列表里必须有条目"""
    st = bpy.context.window_manager.blockout_ai
    n0 = len(st.sessions)

    s = ui_ai._new_session()
    ui_ai._sync_session_list()

    assert len(st.sessions) == n0 + 1, (n0, len(st.sessions))
    items = [(x.sid, x.title, x.count) for x in st.sessions]
    assert any(x[0] == s.sid for x in items), items
    print("       列表：", items)


def t_list_reflects_message():
    """发消息后列表里的条数要更新"""
    st = bpy.context.window_manager.blockout_ai
    s = ui_ai._get_session()
    before = s.message_count()
    s.add_user("测试消息")
    ui_ai._sync_session_list()

    item = next((x for x in st.sessions if x.sid == s.sid), None)
    assert item is not None
    assert item.count == before + 1, (item.count, before)


def t_clear_works():
    """清空：消息行要清掉，会话本身保留"""
    st = bpy.context.window_manager.blockout_ai
    s = ui_ai._get_session()
    s.add_user("要被清掉的")
    ui_ai._append_line(st, "user", "要被清掉的行")
    assert len(st.lines) > 0, "前置：应该有行"
    assert s.message_count() > 0

    r = bpy.ops.blockout.ai_clear()
    assert r == {"FINISHED"}, r
    assert len(st.lines) == 0, "lines 没清空：%d" % len(st.lines)
    assert s.message_count() == 0, "会话消息没清：%d" % s.message_count()
    # 会话本身还在
    assert s.sid in ui_ai._SESSIONS


def t_switch_replays():
    """切换会话要把历史重放出来"""
    st = bpy.context.window_manager.blockout_ai
    s = ui_ai._get_session()
    s.add_user("历史消息 A")
    s.add_assistant("回复 B")
    st.lines.clear()

    assert ui_ai._switch_to(s.sid)
    assert len(st.lines) >= 2, "重放的行数 %d" % len(st.lines)
    texts = [x.text for x in st.lines]
    assert any("历史消息 A" in t for t in texts), texts
    print("       重放：", texts[:3])


def t_delete_removes():
    st = bpy.context.window_manager.blockout_ai
    s = ui_ai._new_session()
    ui_ai._sync_session_list()
    sid = s.sid
    st.active_sid = sid

    st.session_index = next(i for i, x in enumerate(st.sessions) if x.sid == sid)
    r = bpy.ops.blockout.ai_delete_session()
    assert r == {"FINISHED"}, r
    assert sid not in ui_ai._SESSIONS


def t_open_log_no_error():
    """曾 NameError: os 的地方"""
    from blockout_helper import debuglog
    debuglog.write("x")
    r = bpy.ops.blockout.ai_open_log()
    assert r == {"FINISHED"}, r


check("会话列表有内容", t_list_populated)
check("列表条数随消息更新", t_list_reflects_message)
check("清空真的清掉", t_clear_works)
check("切换重放历史", t_switch_replays)
check("删除移除条目", t_delete_removes)
check("查看日志不报错", t_open_log_no_error)

bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
