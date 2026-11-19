"""
scene.py —— 场景环境（地面 / 灯光 / Workbench / 交付）

全部走 Workbench 白模口径：不追材质，只保证形体、阴影、轮廓读得出来。

判据：一张看不出板缝 / 轮廓的图，不能当 QC 证据。
所以默认是「中性灰 + 硬阴影 + 中灰背景」，不是白对白。
"""

import bpy
from mathutils import Vector

from . import kit

#: 白模起手就用这套，别每次手调
STYLE = {
    "background": (0.05, 0.07, 0.10),
    "ground": (0.10, 0.11, 0.14),
    "body": (0.58, 0.62, 0.68),
    "joint": (0.20, 0.72, 1.00),
    "head": (0.96, 0.96, 0.98),
}


def setup_world(scene, style=None, floor=True, floor_size=12.0, floor_margin=0.004,
                center=None):
    """地面 + 背景色。地板放在最低点之下，别切进脚。

    center：地面中心（米）。低机位时地面很大、相机贴地，
    地面必须跟着人被摄体走，不然会横在镜头前把画面全挡住。
    """
    s = dict(STYLE)
    if style:
        s.update(style)

    if floor:
        mats = kit.solid_material("ENV_Ground", s["ground"], 0.9)
        old = bpy.data.objects.get("ENV_ground")
        if old:
            bpy.data.objects.remove(old, do_unlink=True)
        # 用真正的平面（z=0 一个面）：薄盒子会给"竖立大板"的机会，
        # 而且 scale 与边长语义容易错（create_cube 的 size 是边长）。
        me = bpy.data.meshes.new("ENV_ground")
        h = float(floor_size) * 0.5
        me.from_pydata([(-h, -h, 0.0), (h, -h, 0.0), (h, h, 0.0), (-h, h, 0.0)],
                       [], [(0, 1, 2, 3)])
        me.validate()
        me.update()
        g = bpy.data.objects.new("ENV_ground", me)
        kit.ensure_collection("ENV").objects.link(g)
        cx, cy = (center[0], center[1]) if center else (0.0, 0.0)
        g.location = (cx, cy, -floor_margin)
        me.materials.append(mats)

    scene.display.shading.background_type = "VIEWPORT"
    scene.display.shading.background_color = s["background"]
    return s


def setup_workbench(scene, res=(1280, 720), shadows=True, cavity=True):
    """Workbench 渲染设置。50–90 ms/帧，适合长序列。"""
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False

    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "MATERIAL"
    sh.show_shadows = shadows
    sh.shadow_intensity = 0.45
    sh.show_cavity = cavity
    if cavity:
        sh.cavity_type = "BOTH"
    return {"engine": "BLENDER_WORKBENCH", "res": list(res)}


def setup_eevee(scene, res=(1280, 720), samples=32):
    """需要真阴影/材质时用。比 Workbench 慢一两个数量级。"""
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    try:
        scene.eevee.taa_render_samples = samples
    except AttributeError:
        pass
    return {"engine": "BLENDER_EEVEE_NEXT", "res": list(res), "samples": samples}


def add_sun(name="ENV_Sun", energy=3.0, angle_deg=2.0, rot_deg=(45, 0, 35)):
    """单侧强太阳。硬阴影能把 1–2mm 的台阶照出来。"""
    old = bpy.data.objects.get(name)
    if old:
        bpy.data.objects.remove(old, do_unlink=True)
    d = bpy.data.lights.new(name, "SUN")
    d.energy = energy
    d.angle = angle_deg * 3.141592653589793 / 180.0
    o = bpy.data.objects.new(name, d)
    kit.ensure_collection("ENV").objects.link(o)
    o.rotation_euler = tuple(a * 3.141592653589793 / 180.0 for a in rot_deg)
    return o


def frame_scene(scene, focus_objects, margin=1.18):
    """把 scene.frame_start/end 提到动画实际长度（有些对象只写到一半）。

    ⚠ 范围小于动画时，在 Blender 里点播放只走头几帧，
    看起来就像"人物不动"。这是实测被踩到的坑。
    """
    from .skeleton import _action_last_keyframe

    last = scene.frame_start
    for o in focus_objects:
        ad = o.animation_data
        if ad and ad.action:
            last = max(last, _action_last_keyframe(ad.action))
    scene.frame_end = max(scene.frame_start + 1, last)
    return {"frame_start": scene.frame_start, "frame_end": scene.frame_end}


def set_fps(scene, fps):
    scene.render.fps = int(round(fps))
    scene.render.fps_base = 1.0
    return scene.render.fps


def render_sequence(scene, outdir, frame_range=None, force=False, on_progress=None):
    """渲染 PNG 序列，**存在即跳过** —— 可续跑是长活的第一属性。"""
    import os

    os.makedirs(outdir, exist_ok=True)
    a, b = frame_range or (scene.frame_start, scene.frame_end)
    done = 0
    skipped = 0
    for f in range(a, b + 1):
        path = os.path.join(outdir, "f_%04d.png" % f)
        if os.path.exists(path) and not force:
            skipped += 1
            continue
        scene.frame_set(f)
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        done += 1
        if on_progress:
            on_progress(f, b - a + 1, done, skipped)
    return {"rendered": done, "skipped": skipped, "dir": outdir, "range": [a, b]}


def encode_mp4(scene, frames_dir, out_path, fps=25, pattern="f_*.png",
               crf="HIGH", preset="GOOD", restore_frames=True):
    """PNG 序列 → MP4，走 Blender 内置 FFMPEG（不需要系统 ffmpeg）。

    ⚠ 序列编辑器要求 scene.frame_start/end 覆盖整段序列，
    所以编码过程会临时改帧范围。restore_frames=True 时编码后还原，
    否则下一次运镜取景会拿到被改过的范围（实测第二轮起全部取景错）。
    """
    import glob
    import os

    keep_start, keep_end = scene.frame_start, scene.frame_end

    files = sorted(glob.glob(os.path.join(frames_dir, pattern)))
    if not files:
        raise RuntimeError("没有帧可编码：%s/%s" % (frames_dir, pattern))

    se = scene.sequence_editor_create()
    try:
        se.sequences.clear()
    except AttributeError:
        pass

    strip = se.sequences.new_image(name="seq", filepath=files[0],
                                   channel=1, frame_start=1,
                                   fit_method="ORIGINAL")
    for f in files[1:]:
        strip.elements.append(os.path.basename(f))
    strip.frame_final_duration = len(files)

    scene.frame_start = 1
    scene.frame_end = len(files)
    scene.render.fps = int(fps)
    scene.render.fps_base = 1.0
    scene.render.use_sequencer = True
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = crf
    scene.render.ffmpeg.ffmpeg_preset = preset
    scene.render.ffmpeg.audio_codec = "NONE"
    scene.render.filepath = out_path
    scene.render.use_file_extension = False

    bpy.ops.render.render(animation=True)

    ok = os.path.exists(out_path)
    size = os.path.getsize(out_path) if ok else 0

    # ⚠ 序列编辑器一旦存在，`render.render` 会去渲序列而不是场景相机。
    # 下一轮渲染就会渲出"上一轮的片子"，看起来像"运镜没生效"。
    # 所以编码后必须清掉序列并把 use_sequencer 关掉。
    try:
        if scene.sequence_editor:
            scene.sequence_editor_clear()
    except (AttributeError, RuntimeError):
        try:
            se = scene.sequence_editor
            if se:
                for s in list(se.sequences):
                    se.sequences.remove(s)
        except Exception:
            pass
    scene.render.use_sequencer = False
    scene.render.image_settings.file_format = "PNG"

    if restore_frames:
        scene.frame_start, scene.frame_end = keep_start, keep_end
        scene.frame_set(keep_start)

    return {"path": out_path, "bytes": size,
            "frames": len(files), "ok": ok}
