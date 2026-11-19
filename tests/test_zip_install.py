"""模拟用户从 zip 安装插件：装 → 启用 → 用一遍 → 卸"""
import bpy, os, json, sys

ZIP = r"E:\Xiongwq\RobotChat\blender-video-to-blockout\data\blockout_helper.zip"
KP = r"E:\Xiongwq\RobotChat\blender-video-to-blockout\data\dataset\kp3d.json"

r = bpy.ops.preferences.addon_install(filepath=ZIP)
print("INSTALL", r)

import addon_utils
addon_utils.enable("blockout_helper", default_set=True, persistent=True)
print("ENABLED", any(m.__name__ == "blockout_helper" for m in addon_utils.modules()))

import blockout_helper as bh
print("VERSION", bh.bl_info["version"])
print("VIDEO_OK", getattr(bh, "_VIDEO_OK", False), getattr(bh, "_VIDEO_ERR", ""))

sc = bpy.context.scene
for name in ("Cube", "Light"):
    o = bpy.data.objects.get(name)
    if o:
        bpy.data.objects.remove(o, do_unlink=True)

# 面板 + 算子都在
print("PROPS", hasattr(sc, "blockout"))
ops = [o for o in dir(bpy.ops.blockout) if not o.startswith("_")]
print("OPS", sorted(ops))
panels = [c.__name__ for c in bpy.types.Panel.__subclasses__()
          if c.__module__.startswith("blockout_helper")]
print("PANELS", sorted(panels))

# 跑一遍
p = sc.blockout
print("IMPORT", bpy.ops.blockout.import_joints(filepath=KP), p.joint_summary)
print("RIG", bpy.ops.blockout.build_rig(), p.rig_summary)
print("CAM", bpy.ops.blockout.setup_camera(), p.camera_summary)
print("VERIFY", bpy.ops.blockout.verify_framing(), p.verify_verdict, p.verify_summary)

# 每个运镜预设都能用
for preset in ("cinematic", "turntable", "dolly_in", "top_down"):
    p.camera_preset = preset
    bpy.ops.blockout.setup_camera()
    bpy.ops.blockout.verify_framing()
    print("PRESET", preset, p.verify_verdict, p.verify_summary)

print("ZIP_INSTALL_OK")
