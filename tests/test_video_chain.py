"""
新模块测试：kit / skeleton / camera / scene / ops_video

用法：
    blender --background --factory-startup --python test_video_chain.py

判据一律是数字，不靠肉眼：
    * 单位换算误差 < 1e-9
    * AIST Y-up → Blender Z-up 的轴映射逐分量核对
    * 骨骼包围盒高度 ≈ 输入身高
    * 运镜取景 worst NDC ≤ 1.0
    * 编码出的 MP4 有真 ftyp 头
"""

import json
import math
import os
import sys
import tempfile
import traceback

ADDON_PARENT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "addon"))
if ADDON_PARENT not in sys.path:
    sys.path.insert(0, ADDON_PARENT)

import bpy
from mathutils import Vector

print("=" * 64)
print("视频链路测试 · Blender", bpy.app.version_string)
print("=" * 64)

failures = []


def check(name, fn):
    try:
        fn()
        print("  ✓ %s" % name)
    except Exception as e:
        print("  ✗ %s: %s" % (name, e))
        traceback.print_exc()
        failures.append((name, str(e)))


import blockout_helper
from blockout_helper import kit, skeleton, camera as cam_mod, scene as scene_mod

blockout_helper.register()
print("    版本 %s · 视频链路 %s"
      % (blockout_helper.bl_info["version"],
         "OK" if getattr(blockout_helper, "_VIDEO_OK", False)
         else "FAIL: " + getattr(blockout_helper, "_VIDEO_ERR", "?")))

WORK = os.path.join(tempfile.gettempdir(), "bo_video_chain")
os.makedirs(WORK, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════
# kit：单位与坐标
# ═══════════════════════════════════════════════════════════════════

def t_units():
    assert abs(kit.to_m(1, "mm") - 0.001) < 1e-12
    assert abs(kit.to_m(1, "cm") - 0.01) < 1e-12
    assert abs(kit.to_m(1, "m") - 1.0) < 1e-12
    assert abs(kit.to_m(2500, "mm") - 2.5) < 1e-12
    try:
        kit.to_m(1, "inch")
    except ValueError:
        pass
    else:
        raise AssertionError("未知单位必须报错，不能静默当米用")


def t_aist_axis():
    """AIST 是 Y-up；Blender 是 Z-up。逐分量核对。"""
    # 向"上" 100cm → Blender +Z 1m
    v = kit.aist_pt_to_m(0, 100, 0, "cm")
    assert abs(v.x) < 1e-9 and abs(v.y) < 1e-9 and abs(v.z - 1.0) < 1e-9, v
    # 向"前" +z 100cm → Blender -Y 1m
    v = kit.aist_pt_to_m(0, 0, 100, "cm")
    assert abs(v.y + 1.0) < 1e-9, v
    # 向右不变
    v = kit.aist_pt_to_m(150, 0, 0, "cm")
    assert abs(v.x - 1.5) < 1e-9, v


def t_box_center_semantics():
    """_mm 盒子用中心，不是底面 —— 这是踩过的坑。"""
    col = kit.ensure_collection("_T")
    o = kit.add_box_mm(col, "_T_box", (0, 0, 500), (200, 200, 1000))
    # ⚠ bound_box / matrix_world 在改完几何后要刷依赖图，否则读到旧值
    bpy.context.view_layer.update()
    # 中心 500mm、高 1000mm ⇒ 底面 0，顶面 1000mm
    zs = [(o.matrix_world @ Vector(c)).z for c in o.bound_box]
    assert abs(o.location.z - 0.5) < 1e-9, o.location
    assert abs(o.dimensions.z - 1.0) < 1e-6, o.dimensions
    assert abs(min(zs) - 0.0) < 1e-6, min(zs)
    assert abs(max(zs) - 1.0) < 1e-6, max(zs)
    # 底面→中心的换算公式必须给出同一个数
    c = kit.box_center_mm_from_bottom((0, 0, 0), (200, 200, 1000))
    assert abs(c.z - 500) < 1e-9, c
    bpy.data.objects.remove(o, do_unlink=True)


def t_scene_units():
    r = kit.check_scene_units(bpy.context.scene)
    assert r["verdict"] == "pass", r
    assert abs(r["mm_per_bu"] - 1000.0) < 1e-6, r


check("单位换算（mm/cm/m）", t_units)
check("AIST Y-up → Blender Z-up", t_aist_axis)
check("_mm 盒子用中心而非底面", t_box_center_semantics)
check("场景单位自检", t_scene_units)


# ═══════════════════════════════════════════════════════════════════
# 造一份合成 kp3d.json（Y-up、厘米）
# ═══════════════════════════════════════════════════════════════════

KP = os.path.join(WORK, "kp3d_fake.json")
NFRAMES = 40


def make_kp3d():
    """一个原地屈膝又站起的人：膝/髋高度随正弦变化，横向也在摆动。

    身高用「踝 0cm / 鼻 170cm」，正好能验证地面吸附与身高。
    """
    frames = []
    for i in range(NFRAMES):
        t = i / float(NFRAMES - 1)
        bob = 12.0 * math.sin(t * math.pi * 2)      # 整体上下 12cm
        crouch = 30.0 * max(0.0, math.sin(t * math.pi))  # 中段下蹲 30cm
        sway = 8.0 * math.sin(t * math.pi * 4)      # 左右摆 8cm

        ankle_y = 0.0 + bob * 0.4
        knee_y = 50.0 + bob - crouch
        hip_y = 95.0 + bob - crouch
        sh_y = 142.0 + bob - crouch * 0.4
        nose_y = 170.0 + bob - crouch * 0.4

        def J(x, y, z=0.0):
            return {"xyz_cm": [round(x, 3), round(y, 3), round(z, 3)],
                    "reproj_px": 4.0}

        frames.append({
            "idx": i,
            "joints": {
                "nose": J(sway, nose_y),
                "shoulder_l": J(20 + sway, sh_y),
                "shoulder_r": J(-20 + sway, sh_y),
                "elbow_l": J(22 + sway, sh_y - 30),
                "elbow_r": J(-22 + sway, sh_y - 30),
                "wrist_l": J(24 + sway, sh_y - 58),
                "wrist_r": J(-24 + sway, sh_y - 58),
                "hip_l": J(11 + sway, hip_y),
                "hip_r": J(-11 + sway, hip_y),
                "knee_l": J(12 + sway, knee_y),
                "knee_r": J(-12 + sway, knee_y),
                "ankle_l": J(13 + sway, ankle_y),
                "ankle_r": J(-13 + sway, ankle_y),
            },
        })

    with open(KP, "w", encoding="utf-8") as f:
        json.dump({"unit": "cm", "fps": 24.0,
                   "names": skeleton.COCO17, "frames": frames}, f)


make_kp3d()


# ═══════════════════════════════════════════════════════════════════
# skeleton
# ═══════════════════════════════════════════════════════════════════

def t_load_contract():
    frames, unit, fps, names = skeleton.load_track(KP)
    assert len(frames) == NFRAMES, len(frames)
    assert unit == "cm", unit
    assert abs(fps - 24.0) < 1e-6, fps
    assert "nose" in names


def t_load_rejects():
    bad = os.path.join(WORK, "bad.json")
    # ① 空 frames
    with open(bad, "w", encoding="utf-8") as f:
        json.dump({"unit": "cm", "frames": []}, f)
    try:
        skeleton.load_track(bad)
    except skeleton.ContractError:
        pass
    else:
        raise AssertionError("空 frames 必须报 ContractError")

    # ② 错误单位
    with open(bad, "w", encoding="utf-8") as f:
        json.dump({"unit": "inch", "frames": [{"joints": {}}]}, f)
    try:
        skeleton.load_track(bad)
    except skeleton.ContractError:
        pass
    else:
        raise AssertionError("未知 unit 必须报 ContractError")


def t_arrays_and_ground():
    frames, unit, fps, names = skeleton.load_track(KP)
    arrays = skeleton.joints_to_arrays(frames, unit, names)
    assert len(arrays["nose"]) == NFRAMES

    # 合成的踝随 bob 摆到 -4.8cm，所以地面偏移 ≈ +4.8cm
    zoff_raw = skeleton.ground_offset(arrays)
    assert 0.03 < zoff_raw < 0.06, zoff_raw

    sm = skeleton.smooth_arrays(arrays, 5, 3)
    # 平滑后不应把"屈膝"抹平：膝的 Z 行程要保留一半以上
    knee_z = [p.z for p in sm["knee_l"]]
    span = max(knee_z) - min(knee_z)
    assert span > 0.15, "平滑过度，屈膝被抹平：span=%.3f" % span

    zoff = skeleton.ground_offset(sm)
    for seq in sm.values():
        for p in seq:
            if p is not None:
                p.z += zoff
    lowest = min(min(p.z for p in seq if p is not None)
                 for seq in sm.values() if any(x is not None for x in seq))
    assert abs(lowest) < 1e-6, "地面吸附失败：最低点 %.4f" % lowest


def t_build_rig():
    frames, unit, fps, names = skeleton.load_track(KP)
    arrays = skeleton.joints_to_arrays(frames, unit, names)
    sm = skeleton.smooth_arrays(arrays, 5, 3)
    zoff = skeleton.ground_offset(sm)
    for seq in sm.values():
        for p in seq:
            if p is not None:
                p.z += zoff

    spec = skeleton.RigSpec(height_m=1.70)
    objs = skeleton.build_rig(sm, spec)

    # build_rig 返回的键是裸 id（spine / thigh_l / head / joint:xxx）
    assert "spine" in objs, sorted(objs)
    assert "thigh_l" in objs
    assert "head" in objs
    assert any(k.startswith("joint:") for k in objs)

    rep = skeleton.rig_report(objs)
    assert rep["bones"] == 9, rep          # 9 段：脊柱 + 四肢各 2
    assert rep["objects"] == len(objs)

    # 关键帧真的写进去了（不是只写了第 1 帧）
    scene = bpy.context.scene
    scene.frame_set(1)
    bpy.context.view_layer.update()
    p1 = objs["thigh_l"].matrix_world.translation.copy()
    scene.frame_set(NFRAMES // 2)
    bpy.context.view_layer.update()
    p2 = objs["thigh_l"].matrix_world.translation.copy()
    delta = (p2 - p1).length
    assert delta > 0.02, "骨骼没动起来：Δ=%.4f m" % delta

    # 整个人（含头）的高度应接近 1.70m
    scene.frame_set(1)
    bpy.context.view_layer.update()
    zs = []
    for o in objs.values():
        for c in o.bound_box:
            zs.append((o.matrix_world @ Vector(c)).z)
    height = max(zs) - min(zs)
    assert 1.2 < height < 2.2, "白模高度 %.2f m 不合理" % height
    # 交给后面的相机测试用
    bpy.app.driver_namespace["_t_rig"] = objs
    return objs


check("读 kp3d.json 契约", t_load_contract)
check("契约拒绝：空 frames / 未知单位", t_load_rejects)
check("关节数组 + 地面吸附", t_arrays_and_ground)
check("生成骨骼（含动起来）", t_build_rig)

RIG = bpy.app.driver_namespace.get("_t_rig") or {}


# ═══════════════════════════════════════════════════════════════════
# camera / scene
# ═══════════════════════════════════════════════════════════════════

def t_camera_presets():
    for name in ("cinematic", "turntable", "dolly_in", "top_down"):
        keys = cam_mod.PRESETS[name]
        assert len(keys) >= 2, name
        # 时间必须单调递增，且落在 [0,1]
        ts = [k[0] for k in keys]
        assert ts[0] == 0.0 and abs(ts[-1] - 1.0) < 1e-9, (name, ts)
        assert all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)), (name, ts)
        # 焦距和距离必须为正
        for k in keys:
            assert k[2] > 0, (name, k)
            assert k[5] > 0, (name, k)
    try:
        cam_mod.setup_camera(bpy.context.scene, [], preset="nope")
    except ValueError:
        pass
    else:
        raise AssertionError("未知预设必须报错")


def t_camera_setup():
    scene = bpy.context.scene
    objs = list(RIG.values())
    assert objs, "没有骨骼可对焦"
    # 场景帧范围是全局的；先收到的长度要和它一致
    expected = scene.frame_end - scene.frame_start + 1
    info = cam_mod.setup_camera(scene, objs, preset="cinematic", wobble=True)
    assert info["frames"] == expected, (info["frames"], expected)
    cam = info["camera"]
    assert scene.camera is cam
    assert info["fit"] is True
    assert info["radius_min"] and info["radius_min"] > 0.3

    # 相机真的在动
    scene.frame_set(scene.frame_start)
    bpy.context.view_layer.update()
    a = cam.matrix_world.translation.copy()
    scene.frame_set(scene.frame_end)
    bpy.context.view_layer.update()
    b = cam.matrix_world.translation.copy()
    assert (b - a).length > 0.5, "相机没动：Δ=%.3f" % (b - a).length

    # 焦距也在变（否则只是平移，没有推拉）
    lens = []
    for f in (scene.frame_start, scene.frame_end):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        lens.append(cam.data.lens)
    assert abs(lens[1] - lens[0]) > 1.0, "焦距没变：%s" % lens


def t_framing():
    scene = bpy.context.scene
    objs = list(RIG.values())
    r = cam_mod.verify_framing(scene, scene.camera, objs)
    assert r["verdict"] == "pass", r
    assert r["worst_ndc"] <= 1.0, r
    assert r["behind_camera"] == 0, r


check("运镜预设合法性", t_camera_presets)
check("配运镜（相机在动）", t_camera_setup)
check("取景自检 worst≤1.0", t_framing)


# ═══════════════════════════════════════════════════════════════════
# ops_video：算子注册与端到端
# ═══════════════════════════════════════════════════════════════════

def t_ops_registered():
    ids = ["blockout.import_joints", "blockout.build_rig",
           "blockout.setup_camera", "blockout.verify_framing",
           "blockout.render_video", "blockout.export_rig_glb"]
    have = dir(bpy.ops.blockout)
    for i in ids:
        short = i.split(".")[1]
        assert short in have, "算子缺失：%s" % i


def t_e2e_pipeline():
    """完整跑一遍：导入 → 骨骼 → 运镜 → 验收（不渲染，省时间）"""
    scene = bpy.context.scene
    for o in list(bpy.data.objects):
        if o.name.startswith(("BO-", "BJ-", "ENV_")):
            bpy.data.objects.remove(o, do_unlink=True)

    r = bpy.ops.blockout.import_joints(filepath=KP)
    assert r == {"FINISHED"}, r

    r = bpy.ops.blockout.build_rig()
    assert r == {"FINISHED"}, r
    n_objs = len([o for o in bpy.data.objects
                  if o.name.startswith(("BO-", "BJ-"))])
    assert n_objs >= 20, "骨骼件数偏少：%d" % n_objs

    r = bpy.ops.blockout.setup_camera()
    assert r == {"FINISHED"}, r

    r = bpy.ops.blockout.verify_framing()
    assert r == {"FINISHED"}, r
    assert scene.blockout.verify_verdict == "pass", scene.blockout.verify_summary


def t_render_and_encode():
    """真渲 6 帧 + 编码，验证 MP4 有 ftyp 头。"""
    scene = bpy.context.scene
    outdir = os.path.join(WORK, "out")
    # 清掉上次残留：否则"存在即跳过"会让第一次渲染变成 0 帧
    frames_dir = os.path.join(outdir, "frames")
    if os.path.isdir(frames_dir):
        for f in os.listdir(frames_dir):
            os.remove(os.path.join(frames_dir, f))

    scene.frame_start = 1
    scene.frame_end = 6
    scene_mod.setup_workbench(scene, res=(320, 240))

    r = scene_mod.render_sequence(scene, os.path.join(outdir, "frames"))
    assert r["rendered"] == 6, r

    # 再跑一次：存在即跳过
    r2 = scene_mod.render_sequence(scene, os.path.join(outdir, "frames"))
    assert r2["rendered"] == 0 and r2["skipped"] == 6, r2

    mp4 = os.path.join(outdir, "t.mp4")
    e = scene_mod.encode_mp4(scene, os.path.join(outdir, "frames"), mp4, fps=24)
    assert e["ok"], e
    with open(mp4, "rb") as f:
        head = f.read(12)
    assert head[4:8] == b"ftyp", head


check("新算子已注册", t_ops_registered)
check("端到端：导入→骨骼→运镜→验收", t_e2e_pipeline)
check("渲染（可续跑）+ 编码 MP4", t_render_and_encode)


def t_encode_restores_frames():
    """编码会改 scene.frame_start/end，必须还原。

    不还原的后果：面板上换预设再取景时，拿到的是被编码改过的范围，
    第二轮起运镜全错（实测 4 个预设渲出完全相同的机位）。
    """
    scene = bpy.context.scene
    keep_global = (scene.frame_start, scene.frame_end)

    scene.frame_start, scene.frame_end = 1, 9
    before = (scene.frame_start, scene.frame_end)

    outdir = os.path.join(WORK, "restore")
    scene_mod.setup_workbench(scene, res=(160, 120))
    scene_mod.render_sequence(scene, os.path.join(outdir, "frames"))

    scene_mod.encode_mp4(scene, os.path.join(outdir, "frames"),
                         os.path.join(outdir, "r.mp4"), fps=24)
    after = (scene.frame_start, scene.frame_end)
    assert after == before, "帧范围没还原：%s → %s" % (before, after)

    # 关掉还原时确实会改：范围设得和序列长度不同（3 帧 vs 序列 9 帧）
    scene.frame_start, scene.frame_end = 1, 3
    odd = (scene.frame_start, scene.frame_end)
    scene_mod.encode_mp4(scene, os.path.join(outdir, "frames"),
                         os.path.join(outdir, "r2.mp4"), fps=24,
                         restore_frames=False)
    assert (scene.frame_start, scene.frame_end) != odd, \
        "restore_frames=False 应改帧范围，实际仍是 %s" % (odd,)

    # 收尾：还给后面的用例（用例之间共享同一个 scene）
    scene.frame_start, scene.frame_end = keep_global


def t_preset_switch_keeps_framing():
    """面板上换预设重取景：每轮取景都必须重解，且都得 pass。

    ⚠ 这条用例必须自己把渲染参数恢复成 16:9 ——
    前一个用例为了省时间改成了 160x120，而取景公式依赖分辨率。
    用例之间共享 scene，谁改了谁负责还。
    """
    scene = bpy.context.scene
    objs = list(RIG.values())
    assert objs
    p = scene.blockout
    keep = (scene.frame_start, scene.frame_end)

    scene_mod.setup_workbench(scene, res=(1280, 720))

    bad = []
    for preset in ("cinematic", "turntable", "dolly_in", "top_down"):
        scene.frame_start, scene.frame_end = keep
        p.camera_preset = preset
        bpy.ops.blockout.setup_camera()
        bpy.ops.blockout.verify_framing()
        if scene.blockout.verify_verdict != "pass":
            bad.append((preset, round(scene.blockout.verify_worst, 3)))
    assert not bad, "换预设后取景不过：%s" % bad
    scene.frame_start, scene.frame_end = keep


def t_presets_are_visually_distinct():
    """四个预设必须真的看不一样 —— 尤其是俯拍得真的俯视。

    ⚠ 早先按"相机→目标"的斜线解距离，解完又按固定 cam_h 摆放，
    两套几何不自洽：top_down（cam_h=4，目标 0.67）被拉成平视，
    四段片子渲出来几乎一模一样。这条用例锁住它。
    """
    scene = bpy.context.scene
    objs = list(RIG.values())
    assert objs
    p = scene.blockout
    keep = (scene.frame_start, scene.frame_end)
    scene_mod.setup_workbench(scene, res=(1280, 720))

    stats = {}
    for preset in ("cinematic", "turntable", "dolly_in", "top_down"):
        scene.frame_start, scene.frame_end = keep
        p.camera_preset = preset
        bpy.ops.blockout.setup_camera()
        cam = scene.camera
        scene.frame_set(keep[0] + (keep[1] - keep[0]) // 2)
        bpy.context.view_layer.update()
        loc = cam.matrix_world.translation.copy()
        fwd = -cam.matrix_world.col[2].to_3d().normalized()
        # 俯角：视线的向下分量（0=平视，1=垂直俯视）
        pitch = -fwd.z
        stats[preset] = {"pitch": round(pitch, 3), "h": round(loc.z, 3)}

    # 俯拍必须比环绕更"往下看"
    assert stats["top_down"]["pitch"] > 0.45, \
        "top_down 没有俯视：pitch=%s" % stats["top_down"]
    assert stats["top_down"]["pitch"] > stats["turntable"]["pitch"] + 0.2, \
        "top_down 和 turntable 的俯角太接近：%s" % stats
    # 环绕有明确的低机位起点
    scene.frame_start, scene.frame_end = keep
    p.camera_preset = "cinematic"
    bpy.ops.blockout.setup_camera()
    scene.frame_set(keep[0])
    bpy.context.view_layer.update()
    h_start = scene.camera.matrix_world.translation.z
    scene.frame_set(keep[1])
    bpy.context.view_layer.update()
    h_end = scene.camera.matrix_world.translation.z
    assert h_end > h_start + 0.5, \
        "cinematic 没有升降：%.2f → %.2f" % (h_start, h_end)

    scene.frame_start, scene.frame_end = keep


check("编码后还原帧范围", t_encode_restores_frames)
def t_encode_leaves_no_sequencer():
    """编码后必须清掉序列编辑器。

    序列编辑器一存在，`bpy.ops.render.render` 就渲序列而不是场景相机。
    实测后果：换预设后仍然渲出上一轮的片子，看起来像"运镜没生效"。
    """
    scene = bpy.context.scene
    keep = (scene.frame_start, scene.frame_end)

    outdir = os.path.join(WORK, "seq_clear")
    os.makedirs(os.path.join(outdir, "frames"), exist_ok=True)
    # 造两张帧
    scene.frame_start, scene.frame_end = 1, 2
    scene_mod.setup_workbench(scene, res=(160, 120))
    scene_mod.render_sequence(scene, os.path.join(outdir, "frames"), force=True)

    scene_mod.encode_mp4(scene, os.path.join(outdir, "frames"),
                         os.path.join(outdir, "x.mp4"), fps=24)

    assert scene.render.use_sequencer is False, "编码后 use_sequencer 应关掉"
    has = bool(scene.sequence_editor and len(scene.sequence_editor.sequences))
    assert not has, "编码后序列编辑器还留着内容，下一轮会渲错"
    assert scene.render.image_settings.file_format == "PNG", \
        "编码后应还原成 PNG，否则下一轮会写成视频"

    scene.frame_start, scene.frame_end = keep


def t_rig_extends_scene_range():
    """生成骨骼后，场景帧范围必须覆盖到动画末尾。

    ⚠ 否则在 Blender 里点播放只走头几帧，看起来像"人物不动"。
    实测：动画 1..480，但场景被外面改成 1..40，用户点播放就是"没动"。
    """
    scene = bpy.context.scene
    # 故意把范围压小，模拟外部脚本改过
    scene.frame_start, scene.frame_end = 1, 5

    r = bpy.ops.blockout.build_rig()
    assert r == {"FINISHED"}, r

    rig = [o for o in bpy.data.objects if o.name.startswith(("BO-", "BJ-"))]
    assert rig
    last = 0
    for o in rig:
        if o.animation_data and o.animation_data.action:
            try:
                for fc in o.animation_data.action.fcurves:
                    for kp in fc.keyframe_points:
                        last = max(last, int(kp.co[0]))
            except AttributeError:
                pass
    assert last > 5, "测试本身失效：没能读到关键帧末尾"
    assert scene.frame_end >= last, \
        "场景帧范围没覆盖动画末尾：frame_end=%d 但动画到 %d" % (scene.frame_end, last)


check("换预设重取景仍通过", t_preset_switch_keeps_framing)
check("四个预设视觉真的不同", t_presets_are_visually_distinct)
check("编码后不留序列编辑器", t_encode_leaves_no_sequencer)
check("生成骨骼后帧范围覆盖动画末尾", t_rig_extends_scene_range)


def t_unregister():
    blockout_helper.unregister()
    assert not hasattr(bpy.context.scene, "blockout")


check("注销", t_unregister)

print()
print("=" * 64)
if failures:
    print("✗ %d 项失败" % len(failures))
    for n, e in failures:
        print("   - %s: %s" % (n, e))
else:
    print("✓ 全部通过")
print("=" * 64)
sys.exit(1 if failures else 0)
