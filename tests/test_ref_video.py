"""验证：原片播放 / 跟原片运镜 / 并排对照

用户要求：
  1. 视频转白膜和视频转白膜骨骼合成一条流程，不分两个面板
  2. 相机视角必须和导入的视频一致
  3. 面板里能播原片，用来比对白模效果

本测试锁死这三件事。

用法：
    blender --background --factory-startup --python test_ref_video.py
"""
import os
import sys
import traceback

ADDON_PARENT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "addon"))
if ADDON_PARENT not in sys.path:
    sys.path.insert(0, ADDON_PARENT)

import bpy

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECON = os.path.join(HERE, "data", "dataset", "addon_test", "recon.json")
VIDEO = os.path.join(HERE, "data", "aist", "video", "dancer_plugin_video.mp4")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  \u2713 %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAIL.append(name)
        print("  \u2717 %s%s" % (name, ("  " + detail) if detail else ""))


def main():
    print("=" * 64)
    print("Blender %s" % bpy.app.version_string)
    print("=" * 64)

    import addon_utils

    addon_utils.enable("blockout_helper", default_set=True)
    import blockout_helper as bh
    from blockout_helper import video_ref

    scene = bpy.context.scene
    props = scene.blockout

    # ── 1. 面板合并：只能有一个主面板 ──
    panels = [c for c in dir(bh) if c.startswith("VIEW3D_PT_blockout")
              and c != "VIEW3D_PT_blockout_view"]
    check("只保留一个主面板（不再分骨骼面板）",
          not hasattr(bh, "VIEW3D_PT_blockout_rig"),
          "panels=%s" % panels)

    # ── 2. 运镜默认跟原片 ──
    check("运镜默认 follow_ref", props.camera_preset == "follow_ref",
          "got %s" % props.camera_preset)

    # ── 3. 导入 recon → 相机轨迹确实是动的 ──
    if not os.path.exists(RECON):
        print("  ! 缺测试数据 %s，跳过相机部分" % RECON)
    else:
        bpy.ops.blockout.import_recon(filepath=RECON)
        traj = bpy.data.objects.get("CameraTrajectory")
        check("导入 recon 建出相机轨迹", traj is not None)

        locs = []
        for f in range(1, 9):
            scene.frame_set(f)
            locs.append(tuple(round(v, 3) for v in traj.matrix_world.translation))
        check("轨迹逐帧位置不同（不是钉死在第一帧）",
              len(set(locs)) >= 4, "%d 个不同位置" % len(set(locs)))

        # ── 4. 跟原片：不能把 CameraTrajectory 顶掉 ──
        meshes = [o for o in scene.objects if o.type == "MESH"]
        if meshes:
            tgt = max(meshes, key=lambda o: len(o.data.vertices))
            bpy.app.driver_namespace["_bo_rig"] = {tgt.name: tgt}
            props.camera_preset = "follow_ref"
            r = bpy.ops.blockout.setup_camera()
            check("跟原片模式执行成功", r == {"FINISHED"}, str(r))
            check("相机仍是 CameraTrajectory（没被 BO_Cam 顶掉）",
                  bpy.context.scene.camera
                  and bpy.context.scene.camera.name == "CameraTrajectory",
                  "got %s" % (bpy.context.scene.camera.name
                              if bpy.context.scene.camera else None))
        else:
            print("  ! 场景里没有网格，跳过相机绑定检查")

    # ── 5. 原片取帧 ──
    if not os.path.exists(VIDEO):
        print("  ! 缺测试视频 %s，跳过播放器部分" % VIDEO)
    else:
        props.ref_path = ""
        props.ref_frames = 0
        scene.render.use_sequencer = False
        r = bpy.ops.blockout.load_ref_video(filepath=VIDEO)
        check("载入原片成功", r == {"FINISHED"}, str(r))
        check("拿到帧数", props.ref_frames > 100, "%d 帧" % props.ref_frames)
        check("挂 strip 后 use_sequencer 必须是关的",
              scene.render.use_sequencer is False,
              "got %s" % scene.render.use_sequencer)

        # 不同帧取到的图必须不同
        sizes = []
        for f in (1, 200, 600):
            fp = video_ref.grab(scene, VIDEO, f)
            sizes.append(os.path.getsize(fp) if fp and os.path.exists(fp) else 0)
        check("不同帧画面不同（真按帧取）",
              all(s > 0 for s in sizes) and len(set(sizes)) > 1,
              "bytes=%s" % sizes)

        # 缓存：第二次取同一帧不该重新渲染
        t0 = video_ref.grab(scene, VIDEO, 200)
        check("同帧命中缓存", t0 == video_ref.grab(scene, VIDEO, 200))

        # 取帧不能把当前帧数带跑
        scene.frame_set(300)
        before = scene.frame_current
        video_ref.grab(scene, VIDEO, 100)
        check("取帧后当前帧不变", scene.frame_current == before,
              "%d -> %d" % (before, scene.frame_current))

        # ── 6. 视口叠加开关 ──
        props.ref_overlay = True
        cam = scene.camera
        ok_on = (cam is not None
                 and cam.data.show_background_images
                 and len(cam.data.background_images) > 0)
        check("开叠加后相机挂上背景图", ok_on)
        props.ref_overlay = False
        ok_off = (cam is not None and not cam.data.show_background_images)
        check("关叠加后背景图摘掉", ok_off)

        # ── 7. 并排对照 ──
        outdir = os.path.join(HERE, "data", "_test_out")
        props.output_dir = outdir
        scene.frame_set(3)
        r = bpy.ops.blockout.compare_frame()
        side = bpy.app.driver_namespace.get("_bo_compare", "")
        check("并排对照出图",
              r == {"FINISHED"} and side and os.path.exists(side),
              side)
        if side and os.path.exists(side):
            img = bpy.data.images.load(side, check_existing=False)
            w, h = img.size
            bpy.data.images.remove(img)
            check("对照图是左右拼接（宽≈两倍高）", w > h * 2.5,
                  "%dx%d" % (w, h))

    # ── 8. 没原片时 follow_ref 必须明确报错，不能默默换机位 ──
    for nm in ("CameraTrajectory", "BO_Cam", "BO_Cam_target"):
        o = bpy.data.objects.get(nm)
        if o:
            bpy.data.objects.remove(o, do_unlink=True)
    props.camera_preset = "follow_ref"
    meshes = [o for o in scene.objects if o.type == "MESH"]
    if meshes:
        tgt = max(meshes, key=lambda o: len(o.data.vertices))
        bpy.app.driver_namespace["_bo_rig"] = {tgt.name: tgt}
        try:
            bpy.ops.blockout.setup_camera()
            check("没轨迹时 follow_ref 报错", False, "居然成功了")
        except RuntimeError as e:
            check("没轨迹时 follow_ref 明确报错", "原片相机轨迹" in str(e),
                  str(e)[:60])

    print()
    print("=" * 64)
    if FAIL:
        print("\u2717 失败 %d 项: %s" % (len(FAIL), FAIL))
        return 1
    print("\u2713 全部通过（%d 项）" % len(PASS))
    return 0


try:
    sys.exit(main())
except Exception:
    traceback.print_exc()
    sys.exit(1)
