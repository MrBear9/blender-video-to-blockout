"""
ops_video.py —— 「视频 → 3D 白模」这条新链路的面板算子

和旧算子的关系
--------------
旧算子（__init__.py）解决**单帧对着原片拖框**；
这里解决**整段视频的关节轨迹 → 骨骼 → 运镜 → 出片**。

两者共用同一个面板 category，互不依赖。旧算子完全不动。
"""

import os
import time

import bpy
from mathutils import Vector

from . import camera as cam_mod
from . import kit
from . import scene as scene_mod
from . import skeleton


def _report(op, level, msg):
    op.report({level}, msg)


def _err(op, exc):
    _report(op, "ERROR", "%s: %s" % (type(exc).__name__, str(exc)[:240]))
    return {"CANCELLED"}


class BO_OT_import_joints(bpy.types.Operator):
    """读 kp3d.json（3D 关节轨迹），校验契约后缓存到场景"""

    bl_idname = "blockout.import_joints"
    bl_label = "① 导入 3D 关节"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        props = context.scene.blockout
        try:
            frames, unit, fps, names = skeleton.load_track(self.filepath)
            arrays = skeleton.joints_to_arrays(frames, unit, names)
        except Exception as e:
            return _err(self, e)

        # 缓存到 blender 数据（不进 .blend，重建骨骼时不用重读文件）
        bpy.app.driver_namespace["_bo_arrays"] = arrays
        bpy.app.driver_namespace["_bo_unit"] = unit
        bpy.app.driver_namespace["_bo_fps"] = fps

        props.joint_source = self.filepath
        props.joint_frames = len(frames)
        props.joint_unit = unit

        have = sorted(k for k, v in arrays.items()
                      if any(p is not None for p in v))
        missing = [b for b in skeleton.COCO17 if b not in have]
        props.joint_summary = "%d 帧 · %d 关节" % (len(frames), len(have))
        if missing:
            props.joint_summary += " · 缺 %d" % len(missing)

        scene = context.scene
        # 骨骼是"每个采样一帧"。播放帧率决定动作快慢：
        # 源视频 60fps 直接播 480 帧只有 8 秒；25fps 播是 19 秒。
        scene_mod.set_fps(scene, props.playback_fps)
        scene.frame_start = 1
        scene.frame_end = max(2, len(frames))

        self.report({"INFO"}, props.joint_summary)
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class BO_OT_build_rig(bpy.types.Operator):
    """② 关节轨迹 → 骨骼白模（逐帧关键帧）"""

    bl_idname = "blockout.build_rig"
    bl_label = "② 生成骨骼"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.blockout
        arrays = bpy.app.driver_namespace.get("_bo_arrays")
        if not arrays:
            self.report({"ERROR"}, "先导入 3D 关节")
            return {"CANCELLED"}

        scene = context.scene
        # 骨架重建是"整场景重来"，把上一轮的骨骼/地面/相机残留清掉，
        # 不然叠加两层骨骼会让取景与渲染全部错乱。
        try:
            from . import kit as _kit
            _kit.clear_collection("Blockout")
            for nm in ("ENV_ground", "BO_Cam", "BO_Cam_target"):
                old = bpy.data.objects.get(nm)
                if old:
                    bpy.data.objects.remove(old, do_unlink=True)
        except Exception:
            pass

        try:
            smoothed = skeleton.smooth_arrays(
                arrays,
                median_k=props.smooth_median,
                mean_k=props.smooth_mean,
            )
            zoff = skeleton.ground_offset(smoothed)
            for seq in smoothed.values():
                for p in seq:
                    if p is not None:
                        p.z += zoff

            spec = skeleton.RigSpec(
                height_m=props.rig_height_m,
                joint_radius_m=props.joint_radius_m,
                collection="Blockout",
            )
            t0 = time.time()
            objs = skeleton.build_rig(smoothed, spec,
                                      with_head=props.rig_with_head,
                                      with_joints=props.rig_with_joints)
            dt = time.time() - t0
        except Exception as e:
            return _err(self, e)

        bpy.app.driver_namespace["_bo_rig"] = objs
        scene_mod.set_fps(scene, props.playback_fps)
        scene.frame_start = 1
        rep = skeleton.rig_report(objs)
        # 帧范围必须覆盖到动画末尾 —— 否则在 Blender 里点播放只走前几帧，
        # 看起来像"人物不动"（实测被外面脚本改成 1..40 后就是这个现象）。
        if rep.get("last_keyframe"):
            scene.frame_end = max(scene.frame_start + 1, int(rep["last_keyframe"]))
        scene.frame_set(scene.frame_start)
        props.rig_summary = "%d 件 · %d 帧 @%dfps（%.1f s）" % (
            rep["objects"], scene.frame_end, scene.render.fps,
            (scene.frame_end - scene.frame_start + 1) / float(scene.render.fps))
        props.rig_ground_offset = zoff
        self.report({"INFO"}, "%s（%.1f s）" % (props.rig_summary, dt))
        return {"FINISHED"}


class BO_OT_setup_camera(bpy.types.Operator):
    """③ 配运镜：跟原片（用 recon.json 的轨迹）或自动运镜"""

    bl_idname = "blockout.setup_camera"
    bl_label = "③ 配运镜"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.blockout
        objs = bpy.app.driver_namespace.get("_bo_rig")
        if not objs:
            self.report({"ERROR"}, "先生成骨骼")
            return {"CANCELLED"}

        scene = context.scene
        try:
            live = [o for o in objs.values()
                    if o is not None and o.name in bpy.data.objects]
            if not live:
                raise RuntimeError("骨骼对象已不在场景里")

            # ── 跟原片：直接复用 recon.json 的相机轨迹 ──
            #
            #   以前这里**无条件**新建 BO_Cam 并设成 scene.camera，
            #   把 import_reconstruction() 建好的 CameraTrajectory 顶掉。
            #   结果就是白模永远从"另一个自选机位"看，和导入的视频对不上。
            #   要和白模逐帧比对，必须用原片自己的运镜。
            traj = bpy.data.objects.get("CameraTrajectory")
            if props.camera_preset == "follow_ref" and traj is not None:
                scene.camera = traj
                # 原片相机可能没有目标空物体；对焦用骨骼中心
                c = kit.objects_bbox_center(live)
                info = {
                    "camera": traj, "target": None, "preset": "follow_ref",
                    "frames": int(scene.frame_end - scene.frame_start + 1),
                    "scene_center": [round(c.x, 3), round(c.y, 3), round(c.z, 3)],
                    "hmax": round(max(
                        (o.matrix_world @ Vector(v)).z
                        for o in live for v in o.bound_box), 3),
                }
                # 轨迹相机已经带关键帧，不要再写一遍
                scene_mod.setup_world(
                    scene, center=(c.x, c.y),
                    floor_size=max(6.0, info["hmax"] * 4.0))
                scene_mod.setup_workbench(
                    scene, res=(props.render_w, props.render_h))
                props.camera_summary = "跟原片 · %d 帧" % info["frames"]
                self.report({"INFO"}, props.camera_summary)
                return {"FINISHED"}

            if props.camera_preset == "follow_ref":
                # 选了跟原片但没导过 recon.json —— 说清缺什么，别默默换别的机位
                self.report(
                    {"ERROR"},
                    "没有原片相机轨迹：先在第 ① 步导入 recon.json，"
                    "或者把运镜改成 Cinematic 等预设")
                return {"CANCELLED"}

            info = cam_mod.setup_camera(
                scene, live,
                preset=props.camera_preset,
                wobble=props.camera_wobble,
            )
            # 地面跟着被摄体走：低机位时地面横在镜头前会把画面全挡住
            c = info["scene_center"]
            scene_mod.setup_world(scene, center=(c[0], c[1]),
                                  floor_size=max(6.0, info["hmax"] * 4.0))
            scene_mod.setup_workbench(
                scene, res=(props.render_w, props.render_h))
        except Exception as e:
            return _err(self, e)

        props.camera_summary = "%s · %d 帧" % (info["preset"], info["frames"])
        self.report({"INFO"}, props.camera_summary)
        return {"FINISHED"}


class BO_OT_verify(bpy.types.Operator):
    """④ 验收：取景是否切边 / 是否有顶点跑到相机后面"""

    bl_idname = "blockout.verify_framing"
    bl_label = "④ 检查取景"

    def execute(self, context):
        scene = context.scene
        objs = bpy.app.driver_namespace.get("_bo_rig")
        cam = scene.camera
        if not objs or cam is None:
            self.report({"ERROR"}, "先配运镜")
            return {"CANCELLED"}
        # 只查还在场景里的对象：重建骨骼后旧引用会失效，
        # 拿失效引用算包围盒会得到垃圾数字（实测 NDC 冲到 1.54）
        live = [o for o in objs.values()
                if o is not None and o.name in bpy.data.objects]
        if not live:
            self.report({"ERROR"}, "骨骼对象已不在场景里，重新生成骨骼")
            return {"CANCELLED"}
        try:
            r = cam_mod.verify_framing(scene, cam, live)
        except Exception as e:
            return _err(self, e)

        props = context.scene.blockout
        props.verify_verdict = r["verdict"]
        props.verify_worst = r["worst_ndc"]
        props.verify_summary = "worst %.3f %s" % (
            r["worst_ndc"], "通过" if r["verdict"] == "pass" else "切边")
        if r["verdict"] != "pass":
            self.report({"WARNING"}, "取景不过：worst=%.3f" % r["worst_ndc"])
        return {"FINISHED"}


class BO_OT_render_video(bpy.types.Operator):
    """⑤ 渲染 PNG 序列 + 编码 MP4（存在即跳过，可续跑）"""

    bl_idname = "blockout.render_video"
    bl_label = "⑤ 渲染并编码"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.blockout
        scene = context.scene
        outdir = bpy.path.abspath(props.output_dir)
        if not outdir:
            self.report({"ERROR"}, "先设输出目录")
            return {"CANCELLED"}
        frames_dir = os.path.join(outdir, "frames")
        mp4 = os.path.join(outdir, "blockout.mp4")

        # 记录动画真实长度：编码会临时改帧范围，渲完要还回去
        keep_start, keep_end = scene.frame_start, scene.frame_end

        try:
            t0 = time.time()
            r = scene_mod.render_sequence(
                scene, frames_dir, force=props.render_force)
            e = scene_mod.encode_mp4(
                scene, frames_dir, mp4,
                fps=props.output_fps, crf=props.encode_quality)
            dt = time.time() - t0
        except Exception as ex:
            return _err(self, ex)
        finally:
            scene.frame_start, scene.frame_end = keep_start, keep_end

        props.render_summary = "%d 帧 / 跳过 %d · %.0f s" % (
            r["rendered"], r["skipped"], dt)
        self.report({"INFO"}, "%s → %s" % (props.render_summary, mp4))
        return {"FINISHED"}


class BO_OT_export_glb(bpy.types.Operator):
    """⑥ 把骨骼导成 GLB（给下游 / 视频模型）"""

    bl_idname = "blockout.export_rig_glb"
    bl_label = "⑥ 导出 GLB"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.glb", options={"HIDDEN"})

    def execute(self, context):
        objs = bpy.app.driver_namespace.get("_bo_rig")
        if not objs:
            self.report({"ERROR"}, "先生成骨骼")
            return {"CANCELLED"}
        path = self.filepath or os.path.join(
            bpy.path.abspath(context.scene.blockout.output_dir or "."),
            "blockout_rig.glb")
        try:
            # 只导出骨骼：临时选中，导出后还原选择
            prev = [o for o in bpy.context.selected_objects]
            bpy.ops.object.select_all(action="DESELECT")
            for o in objs.values():
                if o.name in bpy.data.objects:
                    o.select_set(True)
            bpy.ops.export_scene.gltf(
                filepath=path, export_format="GLB",
                use_selection=True, export_animations=True,
                export_frame_range=True)
            for o in prev:
                if o.name in bpy.data.objects:
                    o.select_set(True)
        except Exception as e:
            return _err(self, e)

        size = os.path.getsize(path) if os.path.exists(path) else 0
        self.report({"INFO"}, "导出 %s（%.1f KB）" % (path, size / 1024.0))
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class BO_OT_load_ref_video(bpy.types.Operator):
    """① 载入原片：挂进 VSE 取帧，并压成相机背景对着看"""

    bl_idname = "blockout.load_ref_video"
    bl_label = "载入原片"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(
        default="*.mp4;*.mov;*.avi;*.mkv;*.webm;*.jpg;*.jpeg;*.png",
        options={"HIDDEN"},
    )

    def execute(self, context):
        props = context.scene.blockout
        scene = context.scene
        path = self.filepath
        if not path:
            self.report({"ERROR"}, "没选文件")
            return {"CANCELLED"}

        try:
            from . import video_ref

            is_video = path.lower().endswith(
                (".mp4", ".mov", ".avi", ".mkv", ".webm"))
            if is_video:
                info = video_ref.ensure_strip(scene, path)
                props.ref_frames = info["frames"]
                # 原片帧数决定时间轴长度。骨骼如果已经建好，取两者大的那个，
                # 免得载入原片把已经做好的动画切短。
                scene.frame_start = 1
                scene.frame_end = max(scene.frame_end, info["frames"])
            else:
                props.ref_frames = 1

            props.ref_path = path
            # 存进 driver_namespace 给面板播放器读，避免每帧查磁盘
            bpy.app.driver_namespace["_bo_ref_path"] = path

            # 立刻抓一帧：不然面板播放器要等用户动时间轴才出图
            bpy.app.driver_namespace.pop("_bo_ref_frame", None)
            fp = video_ref.grab(scene, path, scene.frame_current)
            if fp:
                bpy.app.driver_namespace["_bo_ref_frame"] = fp
        except Exception as e:
            return _err(self, e)

        self.report({"INFO"}, "原片已载入（%d 帧）" % props.ref_frames)
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class BO_OT_grab_ref_frame(bpy.types.Operator):
    """取当前帧的原片画面（面板播放器刷新用）"""

    bl_idname = "blockout.grab_ref_frame"
    bl_label = "刷新原片帧"
    bl_options = {"REGISTER"}

    force: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        props = context.scene.blockout
        if not props.ref_path:
            self.report({"ERROR"}, "先载入原片")
            return {"CANCELLED"}
        try:
            from . import video_ref

            fp = video_ref.grab(
                context.scene, props.ref_path,
                context.scene.frame_current, force=self.force)
            if not fp:
                self.report({"ERROR"}, "取帧失败")
                return {"CANCELLED"}
            bpy.app.driver_namespace["_bo_ref_frame"] = fp
            # 让面板立刻重绘：不重绘的话点了按钮图还是旧的
            for a in context.screen.areas:
                if a.type in ("VIEW_3D", "PROPERTIES"):
                    a.tag_redraw()
        except Exception as e:
            return _err(self, e)
        return {"FINISHED"}


class BO_OT_compare_frame(bpy.types.Operator):
    """把「原片帧 / 白模帧」并排渲成一张对照图"""

    bl_idname = "blockout.compare_frame"
    bl_label = "并排对照"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.blockout
        scene = context.scene
        if not props.ref_path:
            self.report({"ERROR"}, "先载入原片")
            return {"CANCELLED"}

        outdir = bpy.path.abspath(props.output_dir or "//blockout_out")
        try:
            from . import video_ref

            os.makedirs(outdir, exist_ok=True)
            f = scene.frame_current

            ref_png = video_ref.grab(scene, props.ref_path, f,
                                     size=(640, 360), force=True)
            if not ref_png:
                raise RuntimeError("取原片帧失败")

            # 白模当前帧
            keep_fmt = scene.render.image_settings.file_format
            keep_rx, keep_ry = scene.render.resolution_x, scene.render.resolution_y
            keep_seq = scene.render.use_sequencer
            keep_path = scene.render.filepath
            mine = os.path.join(outdir, "compare_%05d.png" % f)
            try:
                scene.render.use_sequencer = False
                scene.render.resolution_x, scene.render.resolution_y = 640, 360
                scene.render.image_settings.file_format = "PNG"
                scene.render.filepath = mine
                bpy.ops.render.render(write_still=True)
            finally:
                scene.render.image_settings.file_format = keep_fmt
                scene.render.resolution_x, scene.render.resolution_y = keep_rx, keep_ry
                scene.render.use_sequencer = keep_seq
                scene.render.filepath = keep_path

            side = _side_by_side(ref_png, mine, os.path.join(
                outdir, "compare_side_%05d.png" % f))
        except Exception as e:
            return _err(self, e)

        bpy.app.driver_namespace["_bo_compare"] = side
        self.report({"INFO"}, "对照图: %s" % side)
        return {"FINISHED"}


def _side_by_side(left_png, right_png, out_png):
    """把两张 PNG 左右拼起来。

    用 numpy 直接读像素 —— 不走 Blender 的合成节点，
    因为切场景节点会把用户正在用的节点树改掉。
    """
    import numpy as np

    def load(p):
        img = bpy.data.images.load(p, check_existing=False)
        try:
            w, h = img.size
            a = np.empty(w * h * 4, dtype=np.float32)
            img.pixels.foreach_get(a)
            return a.reshape(h, w, 4), w, h
        finally:
            bpy.data.images.remove(img)

    la, lw, lh = load(left_png)
    ra, rw, rh = load(right_png)
    # 高度对齐到较小者，宽度各自缩放由调用方保证（都传 640x360）
    h = min(lh, rh)
    la = la[:h, :, :]
    ra = ra[:h, :, :]
    out = np.concatenate([la, ra], axis=1)

    oh, ow = out.shape[0], out.shape[1]
    img = bpy.data.images.new("_bo_side", width=ow, height=oh, alpha=True)
    try:
        img.pixels.foreach_set(out.reshape(-1))
        img.filepath_raw = out_png
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)
    return out_png


classes = (
    BO_OT_import_joints,
    BO_OT_build_rig,
    BO_OT_setup_camera,
    BO_OT_verify,
    BO_OT_render_video,
    BO_OT_export_glb,
    BO_OT_load_ref_video,
    BO_OT_grab_ref_frame,
    BO_OT_compare_frame,
)
