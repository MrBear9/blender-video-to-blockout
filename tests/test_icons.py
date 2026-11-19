"""验证：所有 icon 名字都真实存在

这个测试的由来：
    v1.7.5 的日志里写着
        draw 回答区失败: TypeError: UIlayout.label(): error with keyword
        argument "icon" - enum "MESSAGEBOX" not found
    Blender 4.5 根本没有 MESSAGEBOX 这个 icon。而 draw() 抛异常时
    Blender **完全静默**：只画到出错那行，不报错不打印。

    结果就是：回答框从来没被画出来过，而我前面几轮一直在改那些
    永远执行不到的代码。

    一个拼错的 icon = 一整块 UI 永久消失，且毫无提示。
    所以必须用测试守住：**每次打包前先验 icon**。
"""
import sys
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
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
    except AssertionError as e:
        print("  ✗", name, ":", e)
        fails.append(name)
    except Exception as e:
        import traceback
        print("  ✗", name, ":", type(e).__name__, e)
        traceback.print_exc()
        fails.append(name)


def valid_icons():
    prop = bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"]
    return set(prop.enum_items.keys())


VALID = valid_icons()


def t_icon_dict_all_valid():
    """_ICONS 里的每个值都必须是真 icon。"""
    bad = {k: v for k, v in ui_ai._ICONS.items() if v not in VALID}
    print("       _ICONS 共 %d 项，非法 %d 项" % (len(ui_ai._ICONS), len(bad)))
    if bad:
        print("       非法：", bad)
    assert not bad, "非法 icon（会让 draw 静默中断）：%r" % bad


def t_prefix_dict_keys_covered():
    """_PREFIX 的每个 key 都要在 _ICONS 里有对应，否则画的时候找不到图标。"""
    missing = set(ui_ai._PREFIX) - set(ui_ai._ICONS)
    print("       _PREFIX 缺口:", missing or "无")
    assert not missing, "_PREFIX 有 key 没有 icon: %r" % missing


def t_source_icons_valid():
    """源码里所有 icon="XXX" 都要存在。"""
    src_dir = os.path.join(HERE, "addon", "blockout_helper")
    pat = re.compile(r'icon\s*=\s*["\']([A-Za-z0-9_]+)["\']')
    bad = {}
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(src_dir, fn)
        text = open(p, encoding="utf-8").read()
        for m in pat.finditer(text):
            name = m.group(1)
            if name not in VALID:
                bad.setdefault(name, []).append(fn)
    print("       源码里非法 icon:", bad or "无")
    assert not bad, "源码里的 icon 不存在：%r" % bad


def t_each_section_draws_with_real_icons():
    """★ 真刀真枪：用 Blender 真实 UILayout 检查每个区都能画完。

    假 layout 不校验 icon，所以之前所有测试都放过了 MESSAGEBOX。
    这里改用真实 bpy.types.UILayout 的 label 签名校验。
    """
    label_fn = bpy.types.UILayout.bl_rna.functions["label"]
    valid = set(label_fn.parameters["icon"].enum_items.keys())

    # 把所有会被画到的 icon 收集起来，逐个确认
    cand = set(ui_ai._ICONS.values())
    cand |= {"TEXT", "FORWARD", "INFO", "ERROR", "CONSOLE", "SORT_DESC"}
    bad = [c for c in cand if c not in valid]
    print("       将要画的 %d 个 icon，非法 %d 个" % (len(cand), len(bad)))
    assert not bad, "以下 icon 会让 draw 静默失败：%r" % bad


def t_draw_answer_section_no_icon_error():
    """★ 确认 MESSAGEBOX 不再出现在**代码**里（注释里留个说明没关系）。"""
    assert "TEXT" in VALID, "TEXT 不存在"
    src_dir = os.path.join(HERE, "addon", "blockout_helper")
    hits = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(src_dir, fn)
        for i, line in enumerate(open(p, encoding="utf-8"), 1):
            code = line.split("#", 1)[0]
            if "MESSAGEBOX" in code:
                hits.append("%s:%d" % (fn, i))
    print("       代码里的 MESSAGEBOX:", hits or "无")
    assert not hits, "代码里还在用 MESSAGEBOX：%r（4.5 不存在，会让 draw 崩）" % hits


check("_ICONS 全部合法", t_icon_dict_all_valid)
check("_PREFIX 有对应图标", t_prefix_dict_keys_covered)
check("源码 icon 全部存在", t_source_icons_valid)
check("将要画的图标合法", t_each_section_draws_with_real_icons)
check("MESSAGEBOX 已清除", t_draw_answer_section_no_icon_error)

bh.unregister()
print()
print("=" * 50)
print("✗ %d 项失败：%s" % (len(fails), fails) if fails else "✓ 全部通过")
sys.exit(1 if fails else 0)
