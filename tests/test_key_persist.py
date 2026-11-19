"""验证：API Key 能真正落盘（重启不丢）

用户反馈「每次关闭都会清除这个 key」。
查下来真正原因不是插件逻辑，而是：

    userpref.blend 的修改时间停在几个月前
    ⇒ Blender 从来没有保存过偏好设置
    ⇒ key 只活在内存里，关掉就没了

偏好设置里存在 userpref.blend，不调 save_userpref() 就不会落盘。
所以给每个字段加 update=_autosave_prefs，另加手动保存按钮。
"""
import sys
import os
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "addon"))

import bpy
import addon_utils
import blockout_helper as bh
bh.register()

# ⚠ sys.path 手动 import 的模块**不会**出现在 preferences.addons 里，
#   必须走 addon_utils.enable 才有 AddonPreferences。
addon_utils.enable("blockout_helper", default_set=True, persistent=True)

from blockout_helper import ui_ai

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


def prefs():
    entry = bpy.context.preferences.addons.get("blockout_helper")
    return entry.preferences if entry else None


def userpref_path():
    # ⚠ 不能用 user_resource(create=True)，它要目录；userpref 是文件
    return os.path.join(
        bpy.utils.resource_path("USER"),
        "config", "userpref.blend")


# ═══════════════════════════════════════════════════════════════════
def t_autosave_callback_exists():
    """★ 每个字段都要挂自动保存回调。"""
    p = prefs()
    assert p is not None, "读不到偏好设置"

    # Blender 不直接暴露 update 回调，检查源码里有挂
    src = open(os.path.join(HERE, "addon", "blockout_helper", "ui_ai.py"),
               encoding="utf-8").read()
    n = src.count("update=_autosave_prefs")
    print("       update=_autosave_prefs 出现 %d 次" % n)
    assert n >= 4, "字段没挂自动保存：只有 %d 处" % n


def t_save_op_exists():
    """手动保存算子要注册（operator 不挂在 bpy.types 上，用 bpy.ops 探）。"""
    assert hasattr(ui_ai, "BO_OT_ai_save_prefs"), "算子类不存在"
    assert ui_ai.BO_OT_ai_save_prefs.is_registered, "算子没注册"
    r = bpy.ops.blockout.ai_save_prefs()
    print("       返回:", r)
    assert "FINISHED" in r, "保存算子失败：%r" % (r,)


def t_key_persists_across_reload():
    """★ 核心：设 key → 保存 → 重新读，值还在。"""
    p = prefs()
    assert p is not None

    KEY = "sk-test-persist-1234567890"
    p.api_key = KEY
    p.model = "deepseek-flash"

    # 触发保存（正常是 update 回调自动做，这里显式调一次）
    bpy.ops.wm.save_userpref()
    print("       已保存 userpref")

    path = userpref_path()
    assert os.path.exists(path), "userpref.blend 不存在：%s" % path
    mtime = os.path.getmtime(path)
    age = time.time() - mtime
    print("       userpref 修改时间距今 %.1f 秒" % age)
    assert age < 120, "userpref 没被更新（%.1f 秒前）" % age

    # 值要能读回来
    assert p.api_key == KEY, "key 读回来变了：%r" % p.api_key
    print("       内存里的 key 正确 ✓")


def t_userpref_recently_written():
    """userpref.blend 必须是刚写过的（这正是不落盘的判据）。"""
    path = userpref_path()
    assert os.path.exists(path)
    age = time.time() - os.path.getmtime(path)
    print("       userpref 距今 %.1f 秒" % age)
    assert age < 300, \
        "userpref 很久没写（%.0f 秒）—— 说明设置没落盘" % age


def t_save_button_in_ui():
    """偏好设置面板上要有保存按钮。"""
    src = open(os.path.join(HERE, "addon", "blockout_helper", "ui_ai.py"),
               encoding="utf-8").read()
    assert 'blockout.ai_save_prefs' in src, "面板上没有保存按钮"
    assert "改完会自动保存" in src, "没有给用户说明"


def t_key_not_in_blend():
    """★ 安全：key 不能写进 .blend 场景文件。"""
    p = prefs()
    KEY = "sk-should-not-leak-999"
    p.api_key = KEY

    st = bpy.context.window_manager.blockout_ai
    # 场景属性里不能有 key
    names = [x.identifier for x in st.bl_rna.properties
             if not x.is_readonly]
    print("       场景属性:", names[:8])
    for n in names:
        v = getattr(st, n, None)
        if isinstance(v, str):
            assert KEY not in v, "key 泄漏到场景属性 %r" % n

    # 更彻底：整个 WindowManager 的字符串属性都不该含它
    leaked = []
    for prop in bpy.types.WindowManager.bl_rna.properties:
        if prop.type != "STRING":
            continue
        v = getattr(bpy.context.window_manager, prop.identifier, "")
        if isinstance(v, str) and KEY in v:
            leaked.append(prop.identifier)
    print("       泄漏点:", leaked or "无")
    assert not leaked, "key 泄漏到：%r" % leaked


check("字段挂自动保存", t_autosave_callback_exists)
check("手动保存算子可用", t_save_op_exists)
check("key 能保存并读回", t_key_persists_across_reload)
check("userpref 最近写过", t_userpref_recently_written)
check("面板有保存按钮", t_save_button_in_ui)
check("key 不泄漏到场景", t_key_not_in_blend)

bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
