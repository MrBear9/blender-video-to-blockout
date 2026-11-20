"""检查打包产物：版本、关键功能是否都在。"""
import json
import re
import sys
import zipfile

z = zipfile.ZipFile(sys.argv[1])
names = z.namelist()
print("文件数:", len(names))

ui = z.read("blockout_helper/ui_ai.py").decode("utf-8")
init = z.read("blockout_helper/__init__.py").decode("utf-8")
dsh = z.read("blockout_helper/dsh_client.py").decode("utf-8")
agy = z.read("blockout_helper/agent.py").decode("utf-8")
bi = json.loads(z.read("blockout_helper/build_info.json").decode("utf-8"))

print("版本(build_info):", bi["version"], "| 构建:", bi["built"])
m = re.search(r'"version"\s*:\s*\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)', init)
print("版本(bl_info)   :", ".".join(m.groups()) if m else "(未找到)")

checks = [
    ("AIDshPickItem 类", "class AIDshPickItem" in ui),
    ("ai_pick_dsh 操作符", "blockout.ai_pick_dsh" in ui),
    ("ai_attach_dsh 操作符", "blockout.ai_attach_dsh" in ui),
    ("ai_unattach_dsh 操作符", "blockout.ai_unattach_dsh" in ui),
    ("ai_export_text 操作符", "blockout.ai_export_text" in ui),
    ("面板会话选择器 UI", "接: 新建会话" in ui),
    ("is_current 已修复", "is_current = (not owner) or (owner == st.active_sid)" in ui),
    ("放大改为弹窗", "invoke_props_dialog" in ui),
    ("dsh_picks 集合属性", "dsh_picks: bpy.props.CollectionProperty" in ui),
    ("attach 传给后台线程", "_attach_of(session.sid)" in ui),
    ("_sync_session_list 保住绑定", "bound.get(sid" in ui),
    ("dsh_client.list_sessions", "def list_sessions" in dsh),
    ("dsh_client send 支持 attach", 'body["attach"] = attach' in dsh),
    ("agent 传递 attach", "attach=attach" in agy),
]
print()
for label, ok in checks:
    print(("  OK   " if ok else "  MISS ") + label)

bad = [label for label, ok in checks if not ok]
print()
print("结论:", "全部就绪" if not bad else ("缺 %d 项: %s" % (len(bad), bad)))
sys.exit(0 if not bad else 1)
