"""完整插件测试：注册 / 工具真跑 / 偏好设置 / 线程安全"""
import sys
import os
import json
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)                       # mock_llm 在项目根
sys.path.insert(0, os.path.join(HERE, "addon"))
sys.path.insert(0, os.path.join(HERE, "tools"))   # mock_llm 在这

import bpy

fails = []


def check(name, fn):
    try:
        fn()
        print("  ✓", name)
    except Exception as e:
        import traceback
        print("  ✗", name, ":", e)
        traceback.print_exc()
        fails.append(name)


import blockout_helper as bh
from blockout_helper import tools, dispatch, api_client, agent, ui_ai

bh.register()
print("VERSION", bh.bl_info["version"])
print("VIDEO_OK", bh._VIDEO_OK, "AI_OK", bh._ai_ok)


# ═══ 注册面 ═══
def t_ops():
    ops = [o for o in dir(bpy.ops.blockout) if not o.startswith("_")]
    assert "ai_send" in ops, ops
    assert "import_joints" in ops
    assert len(ops) >= 22, len(ops)


def t_tools():
    names = sorted(tools.REGISTRY)
    for must in ("get_scene_info", "list_objects", "add_primitive",
                 "build_rig", "run_python", "export_glb"):
        assert must in names, (must, names)
    assert len(names) >= 18, len(names)


def t_prefs_real_install():
    """真实安装路径：装 zip → enable → 读偏好设置。

    这是用户的实际路径，也是唯一能验证 AddonPreferences 的办法。
    （用 sys.path 手动 import 的模块不会进 preferences.addons）
    """
    import subprocess
    import tempfile
    zip_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "blockout_helper.zip")
    assert os.path.exists(zip_path), \
        "先跑 python tools/build_zip.py 打包"

    code = (
        "import bpy, addon_utils, json\n"
        "bpy.ops.preferences.addon_install(filepath=%r)\n"
        "addon_utils.enable('blockout_helper', default_set=True, persistent=True)\n"
        "e = bpy.context.preferences.addons.get('blockout_helper')\n"
        "assert e is not None, 'not installed'\n"
        "p = e.preferences\n"
        "print('PREFS_JSON ' + json.dumps({\n"
        "  'provider': p.provider, 'model': p.model,\n"
        "  'base_url': p.base_url, 'temperature': p.temperature,\n"
        "  'has_key': hasattr(p, 'api_key')}))\n"
    ) % zip_path

    exe = bpy.app.binary_path
    out = subprocess.run([exe, "-b", "--factory-startup", "--python-expr", code],
                         capture_output=True, text=True, timeout=180)
    log = (out.stdout or "") + (out.stderr or "")
    line = next((l for l in log.splitlines() if l.startswith("PREFS_JSON")), None)
    assert line, "子进程没输出 PREFS_JSON\n" + log[-1500:]
    data = json.loads(line[len("PREFS_JSON "):])
    assert data["provider"] == "deepseek", data
    assert data["has_key"], data
    assert abs(data["temperature"] - 0.3) < 1e-6, data
    print("       zip 安装后：", data)


def t_state():
    wm = bpy.context.window_manager
    st = wm.blockout_ai
    st.input_text = "hello"
    assert st.input_text == "hello"
    line = st.lines.add()
    line.level = "user"
    line.text = "hi"
    assert len(st.lines) == 1
    st.lines.clear()
    assert len(st.lines) == 0


check("算子注册", t_ops)
check("工具注册表", t_tools)
check("对话状态属性", t_state)


# ═══ 工具真跑（在主线程，经 dispatch）═══
def t_scene_info():
    r = dispatch.call_on_main(tools.call, "get_scene_info", {})
    assert "objects_total" in r, r
    assert r["unit_scale"] == 1.0


def t_add_and_inspect():
    r = dispatch.call_on_main(tools.call, "add_primitive",
                              {"kind": "cube", "name": "T_Cube",
                               "size": 0.5, "location": [1, 2, 3]})
    assert r.get("ok"), r
    assert r["name"] == "T_Cube"
    # 棱长 0.5 ⇒ size 参数乘 2 传进去，尺寸应该是 1.0
    assert abs(r["dimensions"][0] - 1.0) < 1e-5, r

    info = dispatch.call_on_main(tools.call, "get_object_info", {"name": "T_Cube"})
    assert info["name"] == "T_Cube"
    assert abs(info["location"][2] - 3.0) < 1e-5


def t_transform():
    r = dispatch.call_on_main(tools.call, "set_transform",
                              {"name": "T_Cube", "location": [0, 0, 1],
                               "rotation_deg": [0, 0, 90]})
    assert r.get("ok"), r
    assert abs(r["location"][2] - 1.0) < 1e-5


def t_material():
    r = dispatch.call_on_main(tools.call, "assign_material",
                              {"name": "T_Cube", "rgb": [1, 0, 0]})
    assert r.get("ok"), r
    assert r["material"].startswith("M_")


def t_bad_args():
    r = dispatch.call_on_main(tools.call, "add_primitive", {"kind": "cube",
                                                            "bogus_kw": 1})
    assert "error" in r, r


def t_unknown_tool():
    r = dispatch.call_on_main(tools.call, "no_such_tool", {})
    assert "error" in r, r


def t_delete_guard():
    """空参数必须拒绝，防止误删全场景。"""
    r = dispatch.call_on_main(tools.call, "delete_objects", {})
    assert "error" in r, r
    r2 = dispatch.call_on_main(tools.call, "delete_objects", {"names": ["T_Cube"]})
    assert r2.get("ok"), r2
    assert "T_Cube" not in bpy.data.objects


def t_run_python():
    r = dispatch.call_on_main(tools.call, "run_python", {
        "code": "result = len(bpy.data.objects)"})
    assert r.get("ok"), r
    assert isinstance(r["result"], int)
    # 异常要带回来
    r2 = dispatch.call_on_main(tools.call, "run_python",
                               {"code": "raise ValueError('x')"})
    assert "error" in r2


check("get_scene_info", t_scene_info)
check("加基本体 + 查对象", t_add_and_inspect)
check("设置变换", t_transform)
check("赋材质", t_material)
check("错误参数报错", t_bad_args)
check("未知工具报错", t_unknown_tool)
check("删除保护 + 删除", t_delete_guard)
check("run_python", t_run_python)


# ═══ 后台线程路径 ═══
def t_background_dispatch():
    import threading
    box = {}

    def worker():
        try:
            box["r"] = dispatch.call_on_main(tools.call, "get_scene_info", {})
            box["ok"] = True
        except Exception as e:
            box["err"] = str(e)

    t = threading.Thread(target=worker)
    t.start()
    from blockout_helper.dispatch import _TIMER_TICK
    deadline = time.time() + 15
    while t.is_alive() and time.time() < deadline:
        _TIMER_TICK()
        time.sleep(0.01)
    t.join(timeout=5)
    assert box.get("ok"), box
    assert "objects_total" in box["r"]


check("后台线程派发工具", t_background_dispatch)


# ═══ agent 逻辑（打桩，不发网络）═══
def t_agent_trim():
    s = agent.Session()
    s.add_user("a")
    s.add_assistant("b")
    for i in range(20):
        s.add_user("u%d" % i)
        s.add_assistant("a%d" % i)
    s.trim()
    assert len(s.messages) < 40, len(s.messages)
    assert s.messages[0]["role"] == "system"


def t_agent_schemas():
    sc = tools.schemas()
    assert len(sc) >= 18
    assert all("name" in x and "parameters" in x for x in sc)


def t_agent_destructive_flag():
    assert tools.is_destructive("delete_objects")
    assert tools.is_destructive("run_python")
    assert not tools.is_destructive("get_scene_info")


check("历史裁剪", t_agent_trim)
check("schema 导出", t_agent_schemas)
check("破坏性标记", t_agent_destructive_flag)
def t_sessions():
    """会话管理：新建 / 切换 / 删除 / ID 唯一。"""
    st = bpy.context.window_manager.blockout_ai
    st.active_sid = ""

    s1 = ui_ai._get_session()
    assert s1.sid, "会话应有 ID"
    sid1 = s1.sid

    # 新建第二个
    s2 = ui_ai._new_session()
    assert s2.sid != sid1, "ID 必须唯一"

    ui_ai._sync_session_list()
    sids = [x.sid for x in st.sessions]
    assert sid1 in sids and s2.sid in sids, sids

    # 切换
    assert ui_ai._switch_to(sid1), "切换失败"
    assert st.active_sid == sid1

    # 删除（未运行）
    del ui_ai._SESSIONS[s2.sid]
    ui_ai._sync_session_list()
    sids2 = [x.sid for x in st.sessions]
    assert s2.sid not in sids2, sids2

    # 首条消息用于命名
    s1.add_user("给场景加一排柱子")
    if s1.message_count() == 1:
        s1.title = "给场景加一排柱子"
    assert s1.message_count() == 1


def t_empty_response_reported():
    """模型返回空内容时必须报出来，不能静默结束。

    实测 DeepSeek 关掉思考后可能返回 content="" + 无 tool_calls，
    老代码会静默 return，界面只剩"正在请求模型…"。
    """
    import mock_llm
    port = mock_llm.start()
    try:
        saved = api_client.PROVIDERS["custom"]["base_url"]
        api_client.PROVIDERS["custom"] = {
            "label": "Mock", "format": "openai",
            "base_url": "http://127.0.0.1:%d/v1" % port,
            "models": ["m"], "docs": "",
        }
        # 连续两次都空：流式一次空 → 回落非流式也空 → 才会报错
        empty = {"choices": [{"message": {"role": "assistant", "content": ""},
                              "finish_reason": "stop"}],
                 "usage": {"total_tokens": 15}}
        mock_llm.reset([empty, empty, empty, empty])

        s = agent.Session()
        evs = []
        s.on_event = lambda lv, tx: evs.append((lv, str(tx)))
        s.add_user("hello")
        r = agent.run_turn(s, "custom", "k", "m", 0.0)

        assert r["ok"] is False, r
        assert "空" in (r.get("error") or ""), r
        warns = [e for e in evs if e[0] == "warn"]
        assert warns, evs
        assert any("空" in w[1] for w in warns), warns
        api_client.PROVIDERS["custom"]["base_url"] = saved
    finally:
        mock_llm.stop()


check("会话管理", t_sessions)
check("空回复要报错", t_empty_response_reported)
check("zip 安装 + 偏好设置", t_prefs_real_install)

bh.unregister()
print()
print("=" * 56)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
