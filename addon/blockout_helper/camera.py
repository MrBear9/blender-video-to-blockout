"""
camera.py —— 运镜预设（环绕 / 推近 / 升降 / 拉远）

为什么单独一模块：骨骼重建和运镜是两条独立的迭代线。
换镜头不该重建骨骼，改关节不该动镜头。

运镜的判据不是"我觉得能看"，是 framing_ndc() 的 worst ≤ 1.0。
"""

import math

import bpy
from mathutils import Vector

from . import kit


def _smoothstep(t):
    return t * t * (3.0 - 2.0 * t)


#: 预设名 → [(t, 方位角deg, 距离m, 相机高m, 目标高m, 焦距mm), ...]
PRESETS = {
    # 低机位起 → 贴脸 → 绕背升高俯拍 → 拉远
    "cinematic": [
        (0.00, -155.0, 6.60, 0.30, 0.88, 34.0),
        (0.14, -128.0, 5.60, 0.36, 0.92, 40.0),
        (0.30, -72.0, 4.60, 0.66, 0.95, 46.0),
        (0.46, -8.0, 4.20, 1.05, 0.92, 50.0),
        (0.62, 62.0, 4.60, 1.75, 0.86, 48.0),
        (0.78, 132.0, 4.90, 2.60, 0.78, 44.0),
        (0.90, 188.0, 4.90, 2.85, 0.74, 40.0),
        (1.00, 225.0, 5.20, 2.70, 0.78, 38.0),
    ],
    # 匀速转台：恒定距离/高度，只转角度。给参考包用。
    "turntable": [
        (0.00, 0.0, 5.20, 1.60, 0.90, 50.0),
        (1.00, 360.0, 5.20, 1.60, 0.90, 50.0),
    ],
    # 缓慢推近：远景 → 半身。适合看形体转折。
    "dolly_in": [
        (0.00, -30.0, 8.00, 1.10, 0.92, 35.0),
        (0.55, -18.0, 5.00, 1.00, 0.95, 50.0),
        (1.00, -10.0, 3.30, 0.95, 1.00, 70.0),
    ],
    # 纯俯拍：正上方高机位往下看，检查落脚/重心
    # ⚠ 这里 cam_h 必须够高：视线近乎垂直时，"水平半径"不再决定画幅，
    #   高度差才是。设 4m 会在 1.7m 身上贴边（实测 worst=1.54）。
    "top_down": [
        (0.00, -90.0, 2.00, 7.50, 0.60, 40.0),
        (1.00, 90.0, 2.00, 7.50, 0.60, 40.0),
    ],
}


def _sample(keys, t):
    if t <= keys[0][0]:
        return keys[0][1:]
    if t >= keys[-1][0]:
        return keys[-1][1:]
    for i in range(len(keys) - 1):
        t0, *v0 = keys[i]
        t1, *v1 = keys[i + 1]
        if t0 <= t <= t1:
            u = _smoothstep((t - t0) / (t1 - t0)) if t1 > t0 else 0.0
            return [a + (b - a) * u for a, b in zip(v0, v1)]
    return keys[-1][1:]


def _wobble(f, amp, k):
    """低频微抖。没有它，环绕像转盘；太多，像帕金森。"""
    return amp * math.sin(f * k) * math.sin(f * k * 0.37 + 1.1)


def _cam_basis(loc, look):
    """相机基向量。和 to_track_quat('-Z','Y') 的结果一致，但显式可控。

    俯拍（视线垂直）时 up 轴会退化，此时沿用 to_track_quat 的兜底行为：
    它会在退化时改用一个不共线的 up，导致 roll 翻转 —— 这里显式复刻，
    保证"求解时假设的朝向"和"实际写进相机的朝向"是同一个。
    """
    fwd = (look - loc)
    if fwd.length < 1e-9:
        fwd = Vector((0, 1, 0))
    fwd.normalize()
    z_ax = -fwd
    up = Vector((0, 0, 1))
    if abs(fwd.dot(up)) > 0.9999:
        up = Vector((0, 1, 0))
    y_ax = (up - z_ax * up.dot(z_ax))
    if y_ax.length < 1e-6:
        y_ax = Vector((0, 1, 0))
    y_ax.normalize()
    x_ax = y_ax.cross(z_ax).normalized()
    return x_ax, y_ax, fwd


def _fit_radius(scene, cam, focus, look_at, cam_h, ang_rad, want_ndc=0.78,
                iters=30, lo=0.3, hi=400.0):
    """解相机水平半径 R，使 focus 在画面里的最坏 NDC ≈ want_ndc。

    相机位置 = (look.x + R·cosθ, look.y + R·sinθ, cam_h)。
    解的变量和最终摆放**完全一致** —— 解哪个变量，就必须按哪个摆放。

    为什么不用包围盒估 R：3/4 视角下 AABB 的 dx/dy/dz 和投影范围无关，
    实测会差 2 倍，直接切边。必须按**投影后**的画面范围解。

    为什么用二分：worst(R) 随 R 单调不增，二分 30 次就到 1cm 精度，
    比指数爬稳（指数爬遇到退化会震荡）。
    """
    tan_h = 18.0 / cam.data.lens
    tan_v = tan_h * scene.render.resolution_y / float(scene.render.resolution_x)

    samples = []
    for obj in focus:
        if obj.type != "MESH":
            continue
        m = obj.matrix_world
        verts = obj.data.vertices
        for i in range(0, len(verts), 14):
            samples.append(m @ verts[i].co)
    if not samples:
        return None

    cos_a, sin_a = math.cos(ang_rad), math.sin(ang_rad)
    zmid = (max(p.z for p in samples) + min(p.z for p in samples)) * 0.5
    look = Vector((look_at.x, look_at.y, zmid))

    def spread(R):
        origin = Vector((look.x + R * cos_a, look.y + R * sin_a, cam_h))
        x_ax, y_ax, fwd = _cam_basis(origin, look)
        xs, ys = [], []
        for p in samples:
            v = p - origin
            dep = v.dot(fwd)
            if dep <= 1e-6:
                return None
            xs.append(v.dot(x_ax) / (dep * tan_h))
            ys.append(v.dot(y_ax) / (dep * tan_v))
        cxx = (min(xs) + max(xs)) * 0.5
        cyy = (min(ys) + max(ys)) * 0.5
        return max(max(abs(x - cxx) for x in xs),
                   max(abs(y - cyy) for y in ys))

    # 二分：lo 太满、hi 太空
    lo_b, hi_b = lo, hi
    w_hi = spread(hi_b)
    if w_hi is None:
        return hi_b
    if w_hi > want_ndc:
        return hi_b                    # 拉到头了还装不下
    for _ in range(iters):
        mid = (lo_b + hi_b) * 0.5
        w = spread(mid)
        if w is None:
            lo_b = mid                 # 相机在样本内部 → 拉远
            continue
        if w > want_ndc:
            lo_b = mid
        else:
            hi_b = mid
        if hi_b - lo_b < 0.01:
            break
    return (lo_b + hi_b) * 0.5


def setup_camera(scene, focus_objects, preset="cinematic", shot=None,
                 wobble=True, fit=True, want_ndc=0.78):
    """给场景配一台跟随 + 环绕的相机。

    focus_objects：被摄对象（用来算中心与高度）
    preset：PRESETS 的键，或直接给 [(t, az, dist, cam_h, tgt_h, lens), ...]
    fit：True 时按投影实际范围解半径（推荐）；False 用预设值
    want_ndc：目标占画幅比例。每帧求解时取**整帧全片最坏**当基准，
              这样自检抽查任何一帧都不会超。

    判据不是"看起来能看"，是 verify_framing() 的 worst ≤ 1.0。
    """
    if isinstance(preset, str):
        if preset not in PRESETS:
            raise ValueError("未知运镜预设 %r（可选：%s）"
                             % (preset, ", ".join(sorted(PRESETS))))
        keys = PRESETS[preset]
    else:
        keys = list(preset)

    focus = [o for o in focus_objects if o.type == "MESH"]
    if not focus:
        raise ValueError("没有可对焦的网格对象")

    scene.frame_set(scene.frame_start)
    bpy.context.view_layer.update()

    # 全程包围盒（逐帧采样，别只取首帧 —— 人会在动）
    step = max(1, (scene.frame_end - scene.frame_start) // 12)
    centers = []
    zmax = 0.0
    for f in range(scene.frame_start, scene.frame_end + 1, step):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        centers.append(kit.objects_bbox_center(focus))
        for o in focus:
            for c in o.bound_box:
                zmax = max(zmax, (o.matrix_world @ Vector(c)).z)
    scene.frame_set(scene.frame_start)

    cx = sum(c.x for c in centers) / len(centers)
    cy = sum(c.y for c in centers) / len(centers)
    cz = sum(c.z for c in centers) / len(centers)
    hmax = max(zmax, 0.5)

    # 相机 / 目标
    name = shot or "BO_Cam"
    cam = bpy.data.objects.get(name)
    if cam is None:
        cam_data = bpy.data.cameras.new(name)
        cam = bpy.data.objects.new(name, cam_data)
        scene.collection.objects.link(cam)
    cam.data.clip_start = 0.05
    cam.data.clip_end = 500.0
    cam.rotation_mode = "XYZ"
    scene.camera = cam

    tgt_name = name + "_target"
    tgt = bpy.data.objects.get(tgt_name)
    if tgt is None:
        tgt = bpy.data.objects.new(tgt_name, None)
        scene.collection.objects.link(tgt)

    # 先清掉旧动画，否则叠加会变成乱抖
    for ob in (cam, tgt):
        if ob.animation_data:
            ob.animation_data_clear()
    for c in list(cam.constraints):
        cam.constraints.remove(c)

    span = float(scene.frame_end - scene.frame_start + 1)

    # ── 逐帧求半径；取全片最大值当统一基准 ──
    # 分帧各解会有一帧贴边：verify_framing 抽查的帧和求解时的那帧不同。
    R_common = None
    if fit:
        Rs = []
        for f in range(scene.frame_start, scene.frame_end + 1):
            t = (f - scene.frame_start) / max(1.0, span - 1.0)
            az_d, dist, cam_h, tgt_h, lens = _sample(keys, t)
            cam_h = max(cam_h, 0.35)
            cam.data.lens = lens
            look = Vector((cx, cy, min(tgt_h, 1.2) * hmax / 1.70))
            ang = math.radians(az_d)
            R = _fit_radius(scene, cam, focus, look, cam_h, ang, want_ndc)
            if R:
                Rs.append(R)
        if Rs:
            R_common = max(Rs)

    # ── 第二遍：按统一半径摆回来、写关键帧 ──
    fitted = []
    for idx, f in enumerate(range(scene.frame_start, scene.frame_end + 1)):
        t = (f - scene.frame_start) / max(1.0, span - 1.0)
        az_d, dist, cam_h, tgt_h, lens = _sample(keys, t)

        if wobble:
            az_d += _wobble(f, 1.8, 0.021)
            cam_h += _wobble(f, 0.010, 0.033)
        cam_h = max(cam_h, 0.35)

        cam.data.lens = lens
        look = Vector((cx, cy, min(tgt_h, 1.2) * hmax / 1.70))
        if wobble:
            look.x += _wobble(f, 0.05, 0.029)
            look.y += _wobble(f, 0.05, 0.023)

        ang = math.radians(az_d)
        if fit:
            # 统一半径 + 微抖（微抖只往外扩，不往里收，避免贴边）
            R = R_common
            if wobble:
                R *= 1.0 + abs(_wobble(f, 0.012, 0.017))
        else:
            R = dist

        loc = Vector((look.x + R * math.cos(ang),
                      look.y + R * math.sin(ang), cam_h))
        # 用同一个基向量造旋转，保证与 _fit_radius 的假设一致；
        # 拆成 location + rotation_euler 才能写关键帧。
        from mathutils import Matrix
        x_ax, y_ax, fwd = _cam_basis(loc, look)
        basis = Matrix((
            (x_ax.x, y_ax.x, -fwd.x),
            (x_ax.y, y_ax.y, -fwd.y),
            (x_ax.z, y_ax.z, -fwd.z),
        ))
        cam.location = loc
        cam.rotation_euler = basis.to_euler("XYZ")

        cam.keyframe_insert("location", frame=f)
        cam.keyframe_insert("rotation_euler", frame=f)
        cam.data.keyframe_insert("lens", frame=f)
        tgt.location = look
        tgt.keyframe_insert("location", frame=f)
        fitted.append(R)

    scene.frame_set(scene.frame_start)
    return {
        "camera": cam,
        "target": tgt,
        "preset": preset if isinstance(preset, str) else "custom",
        "frames": int(span),
        "scene_center": [round(cx, 3), round(cy, 3), round(cz, 3)],
        "hmax": round(hmax, 3),
        "keyframes": len(keys),
        "fit": bool(fit),
        "radius_common": round(R_common, 3) if R_common else None,
        "radius_min": round(min(fitted), 3) if fitted else None,
        "radius_max": round(max(fitted), 3) if fitted else None,
    }


def verify_framing(scene, cam, focus_objects, frames=None, sample_step=14):
    """逐帧取景自检。抽样步长必须和取景时一致，否则自检在骗你。"""
    if frames is None:
        n = scene.frame_end - scene.frame_start + 1
        frames = sorted({scene.frame_start + int(i * (n - 1) / 7.0)
                         for i in range(8)})
    rows = []
    worst = 0.0
    behind_total = 0
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        r = kit.framing_ndc(scene, cam, focus_objects, sample_step)
        rows.append({"frame": int(f), "worst_ndc": r["worst_ndc"],
                     "behind": r["behind_camera"]})
        worst = max(worst, r["worst_ndc"])
        behind_total += r["behind_camera"]
    scene.frame_set(scene.frame_start)
    return {
        "frames": rows,
        "worst_ndc": round(worst, 4),
        "behind_camera": behind_total,
        "verdict": "pass" if (worst <= 1.0 and behind_total == 0) else "fail",
        "note": "worst<=1.0 且无顶点跑到相机后面才算过；1.0 就是贴边",
    }
