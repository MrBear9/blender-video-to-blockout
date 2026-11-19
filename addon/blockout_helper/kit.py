"""
kit.py —— 新模块共用件（单位/坐标、集合、材质、几何、取景数学）

设计原则
--------
1. 不依赖 DSH 插件、不依赖 numpy/torch：纯 bpy + mathutils。
2. 单位显式：所有对外参数都带单位后缀（_mm / _m / _cm），函数内部立刻换算。
3. 坐标显式：每个来源（COLMAP / AIST / 屏幕）都有自己的转换函数，不许在调用处手写。

踩过的坑（写在这里，别再犯）
--------------------------
* blockout_base 的 at_mm 是**物体中心**，不是底面锚点 —— 用 box_center_mm() 别手算。
* AIST 单位为厘米、Y 轴向上；直接当米用会得到 15 米高的人。
* Blender 场景单位是**米**，而装配/白模参数常用**毫米** —— 混用会让相机跑到 7 公里外。
"""

import math
import bmesh
import bpy
from mathutils import Vector, Matrix

# ═══════════════════════════════════════════════════════════════════
# 单位
# ═══════════════════════════════════════════════════════════════════

UNIT_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0}

#: Blender 场景里 1 个单位通常等于 1 米
DEFAULT_UNIT_PER_BU = 1.0


def to_m(value, unit):
    """把 (mm|cm|m) 换算成米。未知单位直接报错，不静默当米用。"""
    if unit not in UNIT_TO_M:
        raise ValueError("不支持的单位 %r（只接受 mm/cm/m）" % (unit,))
    return float(value) * UNIT_TO_M[unit]


def aist_unit(unit):
    """AIST++ 标定文件是**厘米**。返回该单位到米的系数。"""
    if unit == "cm":
        return 0.01
    return to_m(1.0, unit)


def mm_per_bu(scene):
    """场景单位：1 Blender 单位 = 多少毫米。"""
    s = scene.unit_settings
    return float(getattr(s, "scale_length", 1.0)) * 1000.0


def check_scene_units(scene):
    """场景单位自检：返回 verdict，别等到量纲炸了才发现。"""
    s = scene.unit_settings
    scale = float(getattr(s, "scale_length", 1.0))
    system = getattr(s, "system", "METRIC")
    ok = system == "METRIC" and abs(scale - 1.0) < 1e-6
    return {
        "system": system,
        "scale_length": scale,
        "mm_per_bu": round(scale * 1000.0, 4),
        "verdict": "pass" if ok else "degraded",
        "note": "" if ok else "场景单位不是 1BU=1m，所有 _m 参数都可能被放大/缩小",
    }


# ═══════════════════════════════════════════════════════════════════
# 坐标转换
# ═══════════════════════════════════════════════════════════════════

#: COLMAP cam→world 的轴置换（已用真 Blender API 数值验证，见 check_transform.py）
A_WORLD = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))
A_CAM = Matrix(((1, 0, 0), (0, -1, 0), (0, 0, -1)))


def colmap_rot_to_blender(r_flat):
    """COLMAP 3x3（行优先扁平）→ Blender 旋转矩阵。"""
    m = Matrix(((r_flat[0], r_flat[1], r_flat[2]),
                (r_flat[3], r_flat[4], r_flat[5]),
                (r_flat[6], r_flat[7], r_flat[8])))
    return A_WORLD @ m @ A_CAM


def colmap_pt_to_blender(x, y, z):
    """COLMAP (x, y, z) → Blender (x, z, -y)。"""
    return Vector((x, z, -y))


def aist_pt_to_m(x, y, z, unit="cm"):
    """AIST++ 关节 (x, y=up, z) → Blender 米制 (x, -z, y)。

    AIST 是 Y 轴向上、单位厘米；不换算直接当米会得到 15 米高的人。
    """
    k = to_m(1.0, unit)
    return Vector((x * k, -z * k, y * k))


def screen_to_ray(scene, cam, nx, ny):
    """归一化屏幕坐标 → 世界射线 (origin, direction)。"""
    mw = cam.matrix_world
    lens = cam.data.lens
    sensor = cam.data.sensor_width
    fit = cam.data.sensor_fit
    rx, ry = scene.render.resolution_x, scene.render.resolution_y
    if fit == "AUTO":
        fit = "HORIZONTAL" if rx >= ry else "VERTICAL"
    if fit == "HORIZONTAL":
        sw, sh = sensor, sensor * ry / rx
    else:
        sh, sw = sensor, sensor * rx / ry
    local = Vector(((nx - 0.5) * sw / lens, (ny - 0.5) * sh / lens, -1.0))
    local.normalize()
    return mw.translation.copy(), (mw.to_quaternion() @ local).normalized()


# ═══════════════════════════════════════════════════════════════════
# 集合 / 对象
# ═══════════════════════════════════════════════════════════════════

def ensure_collection(name, parent=None):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(col)
    return col


def clear_collection(name):
    col = bpy.data.collections.get(name)
    if not col:
        return []
    killed = []
    for obj in list(col.objects):
        killed.append(obj.name)
        bpy.data.objects.remove(obj, do_unlink=True)
    return killed


def link_to(obj, col_name, parent=None):
    col = ensure_collection(col_name, parent)
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    col.objects.link(obj)
    return obj


# ═══════════════════════════════════════════════════════════════════
# 材质
# ═══════════════════════════════════════════════════════════════════

_CACHE = {}


def solid_material(name, rgb, roughness=0.6, metallic=0.0):
    """中性灰/纯色材质。白模别用纯白 —— 白对白看不出板缝。"""
    key = (name, tuple(rgb), roughness, metallic)
    m = _CACHE.get(key)
    if m and m.name in bpy.data.materials:
        return m
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Metallic"].default_value = metallic
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = 0.2
    m.diffuse_color = (*rgb, 1.0)
    _CACHE[key] = m
    return m


def smooth_shade(obj):
    """平滑着色：直接改面属性，headless 也稳（不用 bpy.ops）。"""
    for p in obj.data.polygons:
        p.use_smooth = True
    return obj


# ═══════════════════════════════════════════════════════════════════
# 几何
# ═══════════════════════════════════════════════════════════════════

def new_cube_mesh(name, size=2.0):
    """立方体网格，**边长 = size**，中心在原点。

    ⚠ bmesh.ops.create_cube(size=X) 的 X 是**边长**（不是半长），
      而 bpy.ops.mesh.primitive_cube_add 默认 size=2.0 也是边长。
      这里默认 2.0，配合 scale = 目标尺寸/2 才等于 world 尺寸。
      直接用 add_box_m 就别管这些 —— 它内部处理好。
    """
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=float(size))
    bm.to_mesh(me)
    bm.free()
    return me


def new_uv_sphere_mesh(name, segments=16, rings=10):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=1.0)
    bm.to_mesh(me)
    bm.free()
    return me


def add_box_m(collection, name, center_m, size_m, material=None):
    """米制盒子：center 是**中心**（不是底面）。

    自己按目标尺寸建顶点，不依赖 scale —— scale 不是 1 的对象
    在布尔、导出、父子关系里都会惹麻烦。
    """
    me = bpy.data.meshes.new(name)
    sx, sy, sz = (float(v) * 0.5 for v in size_m)
    verts = [(-sx, -sy, -sz), (sx, -sy, -sz), (sx, sy, -sz), (-sx, sy, -sz),
             (-sx, -sy, sz), (sx, -sy, sz), (sx, sy, sz), (-sx, sy, sz)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me.from_pydata(verts, [], faces)
    me.validate()
    me.update()
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.location = Vector(center_m)
    if material:
        me.materials.append(material)
    return obj


def add_box_mm(collection, name, center_mm, size_mm, material=None):
    """毫米制盒子；center_mm 是**中心**。

    这是 blockout_base 的 at_mm 语义 —— 曾经误当成底面锚点，
    结果四个盒子各差 300mm，模型散开。
    """
    c = Vector(center_mm) / 1000.0
    s = Vector(size_mm) / 1000.0
    return add_box_m(collection, name, c, s, material)


def box_center_mm_from_bottom(bottom_mm, size_mm):
    """已知底面高度 → 中心高度。写模型时最常用的一条换算。"""
    return Vector(bottom_mm) + Vector((0.0, 0.0, size_mm[2] / 2.0))


def orient_box_z(obj, p0, p1, width_m, thick_m, min_len_m=0.005):
    """把盒子沿 p0→p1 摆放：局部 Z 对齐线段，长度 = 线段长。

    ⚠ 这里**用 scale** 表达尺寸（骨骼每帧都变长，重建顶点很贵）。
    所以对象的 scale 不是 1，导出前如果要布尔/焊接请先 apply。
    """
    v = Vector(p1) - Vector(p0)
    length = max(v.length, min_len_m)
    obj.location = (Vector(p0) + Vector(p1)) * 0.5
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(v.normalized())
    # 网格是边长 1 的单位立方体 ⇒ scale 直接等于世界尺寸
    obj.scale = (width_m, thick_m, length)
    return obj


def unit_cube_mesh(name):
    """边长 1 的单位立方体 —— orient_box_z 的配套网格。"""
    return new_cube_mesh(name, size=1.0)


# ═══════════════════════════════════════════════════════════════════
# 高细节基本件（做"精细白模"用）
# ═══════════════════════════════════════════════════════════════════

def new_cylinder_mesh(name, segments=16, radius=1.0, depth=1.0):
    """圆柱：沿局部 Z，半径/高度按真实尺寸建（scale 保持 1）。"""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm, cap_ends=True, cap_tris=False, segments=segments,
        radius1=float(radius), radius2=float(radius), depth=float(depth))
    bm.to_mesh(me)
    bm.free()
    return me


def new_cone_mesh(name, segments=16, radius1=1.0, radius2=0.5, depth=1.0):
    """锥台（上下半径不同）。肢体/树/柱收分用它，比等粗盒子像东西。"""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm, cap_ends=True, cap_tris=False, segments=segments,
        radius1=float(radius1), radius2=float(radius2), depth=float(depth))
    bm.to_mesh(me)
    bm.free()
    return me


def add_limb_unit_m(collection, name, r0, r1, material=None, segments=12,
                    flat_ratio=1.0):
    """建一段**单位长度**（沿 Z，从 z=-0.5 到 z=+0.5）的锥台，供逐帧拉伸使用。

    半径按米建死在网格里，长度靠 obj.scale.z 逐帧改 —— 这样既保持收分
    又能适配任意两点距离。scale 只用 Z 一维，截面不会跟着变形。
    """
    me = new_cone_mesh(name, segments=segments,
                       radius1=float(r0), radius2=float(r1), depth=1.0)
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.rotation_mode = "QUATERNION"
    if abs(flat_ratio - 1.0) > 1e-6:
        obj.scale = (1.0, flat_ratio, 1.0)
    if material:
        me.materials.append(material)
    if segments >= 12:
        smooth_shade(obj)
    return obj


def stretch_limb(obj, p0, p1, flat_ratio=1.0):
    """把单位锥台拉到 p0→p1：只改 Z 缩放（长度），截面半径保持不变。

    ⚠ 这是唯一正确的用法：网格半径 = 真实半径，scale.x/y 必须留在 1
    （flat_ratio 是刻意的椭圆截面，不算破坏）。
    """
    p0 = Vector(p0)
    p1 = Vector(p1)
    v = p1 - p0
    L = max(v.length, 0.01)
    obj.location = (p0 + p1) * 0.5
    obj.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(v.normalized())
    obj.scale = (1.0, flat_ratio, L)
    return obj


def add_ellipsoid_m(collection, name, center, radii, material=None,
                    segments=20, rings=12):
    """椭球（头/肩/关节）。radii 是 (rx, ry, rz) 米。"""
    me = new_uv_sphere_mesh(name, segments, rings)
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.location = Vector(center)
    obj.scale = Vector(radii)
    if material:
        me.materials.append(material)
    smooth_shade(obj)
    return obj


def add_cylinder_m(collection, name, center_m, radius_m, height_m,
                   material=None, segments=16):
    """圆柱（垃圾桶/消防栓/灯杆/管道）。半径与高度按真实米数建。"""
    me = new_cylinder_mesh(name, segments, radius=radius_m, depth=height_m)
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.location = Vector(center_m)
    if material:
        me.materials.append(material)
    smooth_shade(obj)
    return obj


def emission_material(name, rgb, strength=3.0):
    key = ("emis", name, tuple(rgb), strength)
    m = _CACHE.get(key)
    if m and m.name in bpy.data.materials:
        return m
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (*rgb, 1.0)
    em.inputs["Strength"].default_value = float(strength)
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    m.diffuse_color = (*rgb, 1.0)
    _CACHE[key] = m
    return m


# ═══════════════════════════════════════════════════════════════════
# 取景
# ═══════════════════════════════════════════════════════════════════

def framing_ndc(scene, cam, objects, sample_step=14):
    """逐帧取景自检：把顶点投到相机 NDC，返回最坏值。

    worst <= 1.0 表示没切边；1.0 就是贴边。
    抽样步长要和取景时用的**一致**，否则自检比被检对象松，等于没检。

    ⚠ 俯拍时视线近乎垂直，`to_track_quat('-Z','Y')` 的 up 轴会退化，
    相机 roll 会翻转。所以这里**从相机矩阵反解基向量**，不假设 up。
    """
    mw = cam.matrix_world
    origin = mw.translation
    x_axis = mw.col[0].to_3d().normalized()
    y_axis = mw.col[1].to_3d().normalized()
    fwd = -mw.col[2].to_3d().normalized()
    lens = cam.data.lens
    tan_h = 18.0 / lens
    tan_v = tan_h * scene.render.resolution_y / float(scene.render.resolution_x)

    worst = 0.0
    behind = 0
    sampled = 0
    for obj in objects:
        if obj.type != "MESH":
            continue
        # 对象可能已被删除/替换：跳过而不是拿垃圾矩阵
        if obj.name not in bpy.data.objects:
            continue
        verts = obj.data.vertices
        m = obj.matrix_world
        for i in range(0, len(verts), max(1, sample_step)):
            p = m @ verts[i].co
            v = p - origin
            depth = v.dot(fwd)
            sampled += 1
            if depth <= 1e-6:
                behind += 1
                continue
            worst = max(worst, abs(v.dot(x_axis) / (depth * tan_h)),
                        abs(v.dot(y_axis) / (depth * tan_v)))
    return {"worst_ndc": round(worst, 4), "behind_camera": behind,
            "sampled": sampled,
            "verdict": "pass" if (worst <= 1.0 and behind == 0) else "fail"}


def objects_bbox_center(objects):
    """一组对象的包围盒中心（世界坐标，米）。逐帧算包围盒前记得刷依赖图。"""
    lo = Vector((1e18, 1e18, 1e18))
    hi = Vector((-1e18, -1e18, -1e18))
    for obj in objects:
        if obj.type != "MESH":
            continue
        for c in obj.bound_box:
            p = obj.matrix_world @ Vector(c)
            lo.x, lo.y, lo.z = min(lo.x, p.x), min(lo.y, p.y), min(lo.z, p.z)
            hi.x, hi.y, hi.z = max(hi.x, p.x), max(hi.y, p.y), max(hi.z, p.z)
    if lo.x > hi.x:
        return Vector((0, 0, 0))
    return (lo + hi) * 0.5


def fit_distance(scene, half_width_m, half_height_m, lens):
    """给定要装下的半宽/半高和焦距，求相机距离（透视近似）。"""
    tan_h = 18.0 / lens
    tan_v = tan_h * scene.render.resolution_y / float(scene.render.resolution_x)
    return max(half_width_m / tan_h, half_height_m / tan_v)
