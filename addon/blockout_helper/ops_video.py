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
    """③ 给骨骼配运镜（环绕/推近/俯拍/转台）"""

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


classes = (
    BO_OT_import_joints,
    BO_OT_build_rig,
    BO_OT_setup_camera,
    BO_OT_verify,
    BO_OT_render_video,
    BO_OT_export_glb,
)
