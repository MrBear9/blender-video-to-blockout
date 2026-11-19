"""
skeleton.py —— 3D 关节轨迹 → 白模骨骼

输入是「每帧一组 3D 关节」，不是 2D 关键点。原因很硬：
    2D 关键点当 2.5D 用，人物一转身左右肢就叠在一起，
    看起来"没动"或者"粘成一根柱子"。必须有多视三角化出来的真 3D。

输入契约（kp3d.json）
---------------------
    {
      "unit": "cm",                  # 必填：cm / m / mm
      "fps": 59.94,                  # 可选，缺省用场景帧率
      "names": ["nose", "shoulder_l", ...],
      "frames": [
        {"idx": 0, "joints": {"nose": {"xyz_cm": [x,y,z], "reproj_px": 3.8}, ...}},
        ...
      ]
    }

关节名与 COCO 17 一致；缺的关节会被跳过（该段骨骼不生成），不报错。
坐标系：AIST 风格 —— **Y 轴向上**，X 向右，Z 向前；单位见 "unit"。
"""

import json
import math

import bpy
from mathutils import Vector

from . import kit

#: 关节名 → COCO 顺序（用于兼容 "kp": [[x,y,c], ...] 的旧格式）
COCO17 = ["nose", "eye_l", "eye_r", "ear_l", "ear_r",
          "shoulder_l", "shoulder_r", "elbow_l", "elbow_r",
          "wrist_l", "wrist_r", "hip_l", "hip_r",
          "knee_l", "knee_r", "ankle_l", "ankle_r"]

#: 骨骼段：(id, 起点关节们, 终点关节们, 粗细占身高比)
#: 起点/终点可以是多个关节 —— 取中点，这样脊柱/颈就是"中线"而不是斜线。
BONES = [
    ("spine", ("hip_l", "hip_r"), ("shoulder_l", "shoulder_r"), 0.30, 0.19),
    ("ua_l", ("shoulder_l",), ("elbow_l",), 0.105, 0.090),
    ("fa_l", ("elbow_l",), ("wrist_l",), 0.088, 0.075),
    ("ua_r", ("shoulder_r",), ("elbow_r",), 0.105, 0.090),
    ("fa_r", ("elbow_r",), ("wrist_r",), 0.088, 0.075),
    ("thigh_l", ("hip_l",), ("knee_l",), 0.150, 0.128),
    ("shin_l", ("knee_l",), ("ankle_l",), 0.115, 0.098),
    ("thigh_r", ("hip_r",), ("knee_r",), 0.150, 0.128),
    ("shin_r", ("knee_r",), ("ankle_r",), 0.115, 0.098),
]


class ContractError(ValueError):
    """输入契约不满足。明确报错，不猜。"""


def load_track(path):
    """读 kp3d.json 并做契约校验。返回 (frames, unit, fps, names)。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "error" in data:
        raise ContractError("输入文件带 error 标记：%s" % data["error"])

    frames = data.get("frames") or []
    if not frames:
        raise ContractError("frames 为空：先跑多视三角化")

    unit = data.get("unit")
    if unit is None:
        unit = "cm"
    if unit not in kit.UNIT_TO_M:
        raise ContractError("unit=%r 不支持（只接受 mm/cm/m）" % (unit,))

    names = data.get("names") or COCO17
    fps = float(data.get("fps") or 0.0) or None
    return frames, unit, fps, names


def joints_to_arrays(frames, unit, names):
    """→ {joint: [Vector(米)...]}；同时输出每帧缺失情况。"""
    idx = {n: i for i, n in enumerate(names)}
    keys = set()
    for fr in frames:
        js = fr.get("joints")
        if isinstance(js, dict):
            keys.update(js.keys())
        else:
            keys.update(names)          # 旧格式：kp 是 [[x,y,c]...] 列表

    arrays = {k: [None] * len(frames) for k in sorted(keys)}
    for fi, fr in enumerate(frames):
        js = fr.get("joints")
        if isinstance(js, dict):
            for name, info in js.items():
                xyz = info.get("xyz_cm") if isinstance(info, dict) else info
                if xyz is None or len(xyz) != 3:
                    continue
                arrays[name][fi] = kit.aist_pt_to_m(xyz[0], xyz[1], xyz[2], unit)
        else:
            kp = fr.get("kp") or []
            for name, i in idx.items():
                if i < len(kp) and len(kp[i]) >= 3 and kp[i][2] > 0.05:
                    # 旧格式只有 2D：放平为 z=0，明确标注不可当 3D 用
                    arrays[name][fi] = Vector((kp[i][0] / 100.0,
                                               -kp[i][1] / 100.0, 0.0))
    return arrays


def smooth_arrays(arrays, median_k=5, mean_k=3):
    """逐轴中值 + 均值平滑。三角化跳点靠中值吃掉，抖动用均值收平。"""
    out = {}
    for name, seq in arrays.items():
        n = len(seq)
        valid = [i for i, v in enumerate(seq) if v is not None]
        if not valid:
            out[name] = seq
            continue
        # 先把缺口用最近有效值填上，避免 None 传染
        filled = list(seq)
        for i in range(n):
            if filled[i] is None:
                near = min(valid, key=lambda j: abs(j - i))
                filled[i] = filled_seq_copy(filled, near)

        med = []
        h = max(1, median_k // 2)
        for i in range(n):
            a, b = max(0, i - h), min(n, i + h + 1)
            xs = sorted(filled[j].x for j in range(a, b))
            ys = sorted(filled[j].y for j in range(a, b))
            zs = sorted(filled[j].z for j in range(a, b))
            med.append(Vector((xs[len(xs) // 2], ys[len(ys) // 2], zs[len(zs) // 2])))

        res = []
        h2 = max(0, mean_k // 2)
        for i in range(n):
            a, b = max(0, i - h2), min(n, i + h2 + 1)
            s = Vector((0, 0, 0))
            for j in range(a, b):
                s += med[j]
            res.append(s / (b - a))
        out[name] = res
    return out


def filled_seq_copy(seq, i):
    return seq[i]


def ground_offset(arrays, floor_joints=("ankle_l", "ankle_r"), margin_m=0.0):
    """脚底最低点 → 0 的平移量（米）。"""
    zs = []
    for name in floor_joints:
        for v in arrays.get(name) or []:
            if v is not None:
                zs.append(v.z)
    if not zs:
        allz = [v.z for seq in arrays.values() for v in seq if v is not None]
        if not allz:
            return 0.0
        zs = allz
    return -min(zs) + margin_m


class RigSpec:
    """骨架构建参数。默认值是"1700mm 成人"的比例，不做个体识别。"""

    def __init__(self, height_m=1.70, joint_radius_m=0.019,
                 head_radius_ratio=0.078, fps=None,
                 body_color=(0.58, 0.62, 0.68),
                 joint_color=(0.20, 0.72, 1.00),
                 head_color=(0.96, 0.96, 0.98),
                 collection="Blockout"):
        self.height_m = height_m
        self.joint_radius_m = joint_radius_m
        self.head_radius_ratio = head_radius_ratio
        self.fps = fps
        self.body_color = body_color
        self.joint_color = joint_color
        self.head_color = head_color
        self.collection = collection


def build_rig(arrays, spec=None, with_head=True, with_joints=True):
    """建骨骼对象 + 逐帧关键帧。返回 {对象名: 对象}。

    只写几何，不碰相机、不碰渲染设置 —— 那些交给 scene 模块，
    这样"重建骨骼"和"重调运镜"可以分开迭代。
    """
    spec = spec or RigSpec()
    frames = 0
    for seq in arrays.values():
        frames = max(frames, len(seq))
    if frames == 0:
        raise ContractError("没有任何关节轨迹")

    H = spec.height_m
    col = kit.ensure_collection(spec.collection)
    kit.clear_collection(spec.collection)

    mat_body = kit.solid_material("BO_Body", spec.body_color, 0.62)
    mat_joint = kit.solid_material("BO_Joint", spec.joint_color, 0.35)
    mat_head = kit.solid_material("BO_Head", spec.head_color, 0.55)

    objs = {}

    def seg_obj(name, mesh):
        o = bpy.data.objects.new(name, mesh)
        col.objects.link(o)
        o.rotation_mode = "QUATERNION"
        return o

    for bid, ka, kb, w_ratio, t_ratio in BONES:
        if not all(k in arrays for k in ka + kb):
            continue
        # 单位立方体：orient_box_z 用 scale 表达尺寸，所以网格边长得是 1
        o = seg_obj("BO-%s" % bid, kit.unit_cube_mesh("BO-%s" % bid))
        o.data.materials.append(mat_body)
        objs[bid] = (o, ka, kb, w_ratio, t_ratio)

    if with_joints:
        for name in arrays:
            o = seg_obj("BJ-%s" % name, kit.new_uv_sphere_mesh("BJ-%s" % name))
            o.data.materials.append(mat_joint)
            kit.smooth_shade(o)
            objs["joint:" + name] = (o, name)

    if with_head and "nose" in arrays:
        o = seg_obj("BO-head", kit.new_uv_sphere_mesh("BO-head", 20, 12))
        o.data.materials.append(mat_head)
        kit.smooth_shade(o)
        objs["head"] = o

    # ── 逐帧写关键帧 ──
    for fi in range(frames):
        f = 1 + fi

        def at(name):
            seq = arrays.get(name)
            return seq[fi] if seq and seq[fi] is not None else None

        def mid(keys):
            pts = [at(k) for k in keys]
            pts = [p for p in pts if p is not None]
            if not pts:
                return None
            s = Vector((0, 0, 0))
            for p in pts:
                s += p
            return s / len(pts)

        for bid, ka, kb, w_ratio, t_ratio in BONES:
            if bid not in objs:
                continue
            o = objs[bid][0]
            p0, p1 = mid(ka), mid(kb)
            if p0 is None or p1 is None:
                continue
            # 手臂/腿用单关节时，两端点可能重合 → 用最小长度兜住
            if (p1 - p0).length < 1e-4:
                p1 = p0 + Vector((0, 0, 0.02))
            kit.orient_box_z(o, p0, p1, H * w_ratio, H * t_ratio)
            for ch in ("location", "rotation_quaternion", "scale"):
                o.keyframe_insert(ch, frame=f)

        if with_joints:
            for key, val in list(objs.items()):
                if not key.startswith("joint:"):
                    continue
                name = val[1]
                p = at(name)
                if p is None:
                    continue
                o = val[0]
                o.location = p
                o.scale = (spec.joint_radius_m,) * 3
                o.keyframe_insert("location", frame=f)
                o.keyframe_insert("scale", frame=f)

        if with_head and "head" in objs:
            p = at("nose")
            if p is not None:
                o = objs["head"]
                o.location = p
                o.scale = (H * spec.head_radius_ratio,) * 3
                o.keyframe_insert("location", frame=f)
                o.keyframe_insert("scale", frame=f)

    return {k: (v[0] if isinstance(v, tuple) else v) for k, v in objs.items()}


def _action_last_keyframe(action):
    """读 action 关键帧末尾。兼容 4.x 的 .fcurves 与 5.x 的 layered action。"""
    last = 0

    def scan(fcurves):
        nonlocal last
        for fc in fcurves:
            for kp in fc.keyframe_points:
                f = int(round(kp.co[0]))
                if f > last:
                    last = f

    try:
        scan(action.fcurves)
    except AttributeError:
        pass

    if last:
        return last

    # Blender 5.x：layered / slotted action
    try:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", []):
                    scan(bag.fcurves)
    except (AttributeError, TypeError):
        pass
    return last


def rig_report(objects):
    """骨骼体检：对象数、帧范围、关键帧末尾。"""
    meshes = [o for o in objects.values() if o.type == "MESH"]
    frame_end = 0
    for o in meshes:
        ad = o.animation_data
        if ad and ad.action:
            frame_end = max(frame_end, _action_last_keyframe(ad.action))
    return {
        "objects": len(meshes),
        "bones": len([k for k in objects if not k.startswith(("joint:", "head"))]),
        "joint_spheres": len([k for k in objects if k.startswith("joint:")]),
        "last_keyframe": frame_end,
    }
