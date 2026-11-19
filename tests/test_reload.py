"""验证：卸载插件后 sys.modules 缓存必须清干净

否则重装后跑的还是旧代码 —— 这就是"新版装上了但界面还是旧的"的原因。
"""
import sys
import os
import importlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy

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


# 开头先确保是干净状态：清掉可能残留的缓存
import sys as _sys
for _n in list(_sys.modules):
    if _n == "blockout_helper" or _n.startswith("blockout_helper."):
        _sys.modules.pop(_n, None)


def modules_under_pkg():
    return sorted(n for n in sys.modules
                  if n == "blockout_helper" or n.startswith("blockout_helper."))


def t_register_creates_modules():
    import blockout_helper as bh
    bh.register()
    mods = modules_under_pkg()
    assert "blockout_helper" in mods
    assert "blockout_helper.ui_ai" in mods, mods
    assert "blockout_helper.agent" in mods, mods
    print("       注册后模块：", len(mods))


def t_unregister_clears_cache():
    """核心：卸载后所有子模块都要从 sys.modules 消失。"""
    import blockout_helper as bh
    before = modules_under_pkg()
    assert len(before) >= 5, before

    bh.unregister()

    after = modules_under_pkg()
    leftover = [m for m in after if m.startswith("blockout_helper")]
    assert not leftover, "缓存没清干净，重装会跑旧代码：%s" % leftover
    print("       卸载后残留：", leftover or "无")


def t_reimport_gets_fresh():
    """重新导入应拿到新模块对象（不是旧缓存）。"""
    import blockout_helper as bh1
    id1 = id(bh1)
    bh1.register()
    mod1 = sys.modules["blockout_helper.ui_ai"]

    bh1.unregister()

    import blockout_helper as bh2
    id2 = id(bh2)
    bh2.register()
    mod2 = sys.modules["blockout_helper.ui_ai"]

    assert id1 != id2, "顶层模块还是同一个对象"
    assert id(mod1) != id(mod2), "子模块还是同一个对象"
    bh2.unregister()


def t_unregister_clears_sessions():
    """卸载后会话要清空。"""
    import blockout_helper as bh
    bh.register()
    from blockout_helper import ui_ai

    s = ui_ai._new_session()
    s.add_user("测试")
    ui_ai._sync_session_list()
    st = bpy.context.window_manager.blockout_ai
    assert len(st.sessions) >= 1
    assert len(ui_ai._SESSIONS) >= 1

    bh.unregister()

    assert not ui_ai._SESSIONS, "会话没清：%s" % list(ui_ai._SESSIONS)


def t_unregister_clears_log():
    """卸载后日志文件要删掉。"""
    import blockout_helper as bh
    bh.register()
    from blockout_helper import debuglog

    debuglog.write("这条应该被清掉")
    p = debuglog.log_path()
    assert os.path.exists(p), p
    body = open(p, encoding="utf-8").read()
    assert "这条应该被清掉" in body

    bh.unregister()

    assert not os.path.exists(p), "日志文件没删：%s" % p


def t_driver_namespace_clean():
    """卸载后 driver_namespace 不应残留会话。"""
    import blockout_helper as bh
    bh.register()
    from blockout_helper import ui_ai
    ui_ai._get_session()
    assert "_bo_session" in bpy.app.driver_namespace

    bh.unregister()
    assert "_bo_session" not in bpy.app.driver_namespace, \
        "driver_namespace 残留 _bo_session"


check("注册创建模块", t_register_creates_modules)
check("卸载清 sys.modules 缓存", t_unregister_clears_cache)
check("重新导入是全新对象", t_reimport_gets_fresh)
check("卸载清会话", t_unregister_clears_sessions)
check("卸载删日志", t_unregister_clears_log)
check("卸载清 driver_namespace", t_driver_namespace_clean)

print()
print("=" * 56)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
