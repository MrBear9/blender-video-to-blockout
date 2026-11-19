"""端到端验证：装 zip → 禁用 → 再启用，代码必须是新的

在子进程里跑真实安装流程（用户的实际路径）。
"""
import json
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(HERE, "data", "blockout_helper.zip")

# 先确认 zip 里是新代码
with zipfile.ZipFile(ZIP) as z:
    ui = z.read("blockout_helper/ui_ai.py").decode("utf-8")
    bi = json.loads(z.read("blockout_helper/build_info.json").decode("utf-8"))
    has_build_info = "_build_info" in ui
    has_os = "import os" in ui
    old_marker = 'v1.1.0 · 流式输出' in ui

print("ZIP 版本", bi["version"], "构建", bi["built"])
print("  含 _build_info:", has_build_info)
print("  含 import os :", has_os)
print("  残留旧文案  :", old_marker)

assert has_build_info, "zip 里没有新面板代码"
assert has_os, "zip 里缺 import os"
assert not old_marker, "zip 里还有旧文案"

CODE = r'''
import bpy, addon_utils, json, sys

zip_path = %r
bpy.ops.preferences.addon_install(filepath=zip_path)
addon_utils.enable("blockout_helper", default_set=True)
entry = bpy.context.preferences.addons.get("blockout_helper")
assert entry is not None, "安装失败"

import blockout_helper as bh
ver = bh.bl_info["version"]

# 读 build_info
import os
p = os.path.join(os.path.dirname(bh.__file__), "build_info.json")
bi = json.load(open(p, encoding="utf-8"))

# 检查模块内是否有新函数
from blockout_helper import ui_ai
has_new = hasattr(ui_ai, "_build_info")
mods_before = len([m for m in sys.modules if m.startswith("blockout_helper")])

# 禁用（触发 unregister → 清缓存）
addon_utils.disable("blockout_helper")
mods_after = len([m for m in sys.modules if m.startswith("blockout_helper")])

# 再启用
addon_utils.enable("blockout_helper", default_set=True)
import blockout_helper as bh2
mods_reload = len([m for m in sys.modules if m.startswith("blockout_helper")])

print("RESULT " + json.dumps({
    "version": list(ver),
    "build_info": bi,
    "has_build_info_fn": has_new,
    "mods_before_disable": mods_before,
    "mods_after_disable": mods_after,
    "mods_after_reload": mods_reload,
}))
''' % ZIP

exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                   "Blender Foundation", "Blender 4.5", "blender.exe")
out = subprocess.run([exe, "-b", "--factory-startup", "--python-expr", CODE],
                     capture_output=True, text=True, timeout=300)
log = (out.stdout or "") + (out.stderr or "")
line = next((l for l in log.splitlines() if l.startswith("RESULT ")), None)
if not line:
    print("子进程没输出 RESULT")
    print(log[-2500:])
    sys.exit(1)

data = json.loads(line[7:])
print()
print("安装后结果：")
print("  版本         :", data["version"])
print("  build_info   :", data["build_info"])
print("  有新面板函数 :", data["has_build_info_fn"])
print("  禁用前模块数 :", data["mods_before_disable"])
print("  禁用后模块数 :", data["mods_after_disable"])
print("  重启用模块数 :", data["mods_after_reload"])

ok = True
if not data["has_build_info_fn"]:
    print("✗ 加载的不是新代码")
    ok = False
if data["mods_after_disable"] != 0:
    print("✗ 禁用后模块没清干净（%d 个残留）" % data["mods_after_disable"])
    ok = False
if data["version"] != list(ver):
    print("✗ 版本不对：", data["version"], "应为", list(ver))
    ok = False

print()
print("✓ 通过：装的是新版，禁用会清缓存，重新启用拿新代码" if ok else "✗ 失败")
sys.exit(0 if ok else 1)
