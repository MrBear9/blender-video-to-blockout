"""运行时验证 v1.1.0 的所有新算子，确保不再有 NameError"""
import sys
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import blockout_helper as bh

bh.register()
print("VERSION", bh.bl_info["version"])

from blockout_helper import debuglog, ui_ai

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


def t_open_log():
    """这是曾经 NameError: name 'os' is not defined 的地方"""
    debuglog.write("runtime test")
    r = bpy.ops.blockout.ai_open_log()
    assert r == {"FINISHED"}, r


def t_new_session():
    r = bpy.ops.blockout.ai_new_session()
    assert r == {"FINISHED"}, r
    st = bpy.context.window_manager.blockout_ai
    assert len(st.sessions) >= 1, len(st.sessions)


def t_switch():
    st = bpy.context.window_manager.blockout_ai
    assert len(st.sessions) >= 1
    st.session_index = 0
    r = bpy.ops.blockout.ai_switch_session()
    assert r == {"FINISHED"}, r


def t_delete():
    st = bpy.context.window_manager.blockout_ai
    before = len(st.sessions)
    st.session_index = 0
    r = bpy.ops.blockout.ai_delete_session()
    assert r == {"FINISHED"}, r
    assert len(st.sessions) == before - 1 or before == 1


def t_clear():
    r = bpy.ops.blockout.ai_clear()
    assert r == {"FINISHED"}, r


def t_quick():
    r = bpy.ops.blockout.ai_quick(prompt="测试提示")
    assert r == {"FINISHED"}, r
    assert bpy.context.window_manager.blockout_ai.input_text == "测试提示"


def t_prefs_draw():
    """偏好设置在真实安装下才能验证。

    ⚠ 用 sys.path 手动 import 的模块不进 preferences.addons，
    必须在子进程里真装 zip → enable → 读属性。
    """
    import subprocess
    zip_path = os.path.join(HERE, "data", "blockout_helper.zip")
    assert os.path.exists(zip_path), "先跑 build_zip.py"

    code = (
        "import bpy, addon_utils, json\n"
        "bpy.ops.preferences.addon_install(filepath=%r)\n"
        "addon_utils.enable('blockout_helper', default_set=True)\n"
        "e = bpy.context.preferences.addons.get('blockout_helper')\n"
        "assert e is not None\n"
        "p = e.preferences\n"
        "attrs = ['provider','api_key','model','base_url','temperature','test_status']\n"
        "missing = [a for a in attrs if not hasattr(p, a)]\n"
        "print('PREFS ' + json.dumps({'provider': p.provider,"
        " 'missing': missing, 'model': p.model}))\n"
    ) % zip_path

    out = subprocess.run([bpy.app.binary_path, "-b", "--factory-startup",
                          "--python-expr", code],
                         capture_output=True, text=True, timeout=180)
    log = (out.stdout or "") + (out.stderr or "")
    line = next((l for l in log.splitlines() if l.startswith("PREFS ")), None)
    assert line, "子进程无输出\n" + log[-1200:]
    import json as _json
    data = _json.loads(line[6:])
    assert not data["missing"], data
    assert data["provider"] == "deepseek", data
    print("       ", data)


def t_panel_classes():
    """面板类要能被 UI 系统接受（有 draw 且 bl_idname 合法）"""
    for cls in (ui_ai.VIEW3D_PT_blockout_ai,):
        assert hasattr(cls, "draw")
        assert cls.bl_space_type == "VIEW_3D"


check("查看日志（曾 NameError）", t_open_log)
check("新建会话", t_new_session)
check("切换会话", t_switch)
check("删除会话", t_delete)
check("清空会话", t_clear)
check("快捷提问", t_quick)
check("偏好设置属性", t_prefs_draw)
check("面板类", t_panel_classes)

bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
