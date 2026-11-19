"""校验所有 icon 名字在 Blender 里是否真的存在

Blender 的 icon 是个 enum，写错一个就往 draw() 里抛 TypeError，
而 draw 的异常是**完全静默**的 —— 只画到出错那行，不报错不打印。
所以一个拼错的 icon 就能让整个区域永远看不见，且毫无提示。

本脚本把项目里用到的所有 icon 逐个验一遍，列出不存在的。
"""
import sys
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "addon", "blockout_helper")

import bpy

# 取得 Blender 认识的 icon 全集
valid = set()
try:
    prop = bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"]
    valid = set(prop.enum_items.keys())
except Exception as e:
    print("取 enum 失败：%s" % e)

print("Blender 认识的 icon 数：%d" % len(valid))

# 扫描所有 py 文件里的 icon="XXX" / icon='XXX'
pat = re.compile(r'icon\s*=\s*["\']([A-Za-z0-9_]+)["\']')
used = {}
for fn in sorted(os.listdir(SRC)):
    if not fn.endswith(".py"):
        continue
    p = os.path.join(SRC, fn)
    src = open(p, encoding="utf-8").read()
    for m in pat.finditer(src):
        name = m.group(1)
        used.setdefault(name, set()).add(fn)

# 还有 _ICONS 字典里的值
for fn in sorted(os.listdir(SRC)):
    if not fn.endswith(".py"):
        continue
    p = os.path.join(SRC, fn)
    src = open(p, encoding="utf-8").read()
    for m in re.finditer(r'^\s*"[a-z_]+":\s*"([A-Z0-9_]+)",', src, re.M):
        used.setdefault(m.group(1), set()).add(fn + " (dict)")

print("\n用到的 icon 数：%d" % len(used))
bad = {k: v for k, v in used.items() if k not in valid}
ok = {k: v for k, v in used.items() if k in valid}

print("\n" + "=" * 60)
if bad:
    print("✗ 不存在的 icon（会让 draw 抛异常且静默）：")
    for k in sorted(bad):
        print("   %-22s %s" % (k, ", ".join(sorted(bad[k]))))
else:
    print("✓ 所有 icon 都存在")

print("\n" + "=" * 60)
print("存在的 icon（%d 个）：" % len(ok))
for k in sorted(ok):
    print("   %-22s %s" % (k, ", ".join(sorted(ok[k]))))

# ── 推荐替代 ──
if bad:
    print("\n" + "=" * 60)
    print("建议替代：")
    ALT = {
        "MESSAGEBOX": "INFO / TEXT / SORTALPHA / COMMENT",
        "TRIP_UP": "EVENT_UP_ARROW",
        "TRIP_DOWN": "EVENT_DOWN_ARROW",
        "TRIA_RIGHT": "DISCLOSURE_TRI_RIGHT",
    }
    for k in sorted(bad):
        print("   %-22s → %s" % (k, ALT.get(k, "查上面的存在列表")))
