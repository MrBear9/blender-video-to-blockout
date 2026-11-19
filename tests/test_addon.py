"""
插件测试 —— 重点验证几何，不靠肉眼。

用法：
    blender --background --factory-startup --python test_addon.py
"""

import sys
import os
import json
import math
import traceback

ADDON_PARENT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "addon"))
if ADDON_PARENT not in sys.path:
    sys.path.insert(0, ADDON_PARENT)

import bpy
from mathutils import Vector

print("=" * 64)
print("Blender", bpy.app.version_string)
print("=" * 64)

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        traceback.print_exc()
        failures.append((name, str(e)))


def t_register():
    import blockout_helper
    blockout_helper.register()
    assert hasattr(bpy.context.scene, "blockout")
    print(f"    版本 {blockout_helper.bl_info['version']}")


check("注册", t_register)

import blockout_helper

# ═══ 造测试数据 ═══
WORK = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "dataset", "addon_test"))
os.makedirs(WORK, exist_ok=True)
RECON = os.path.join(WORK, "recon.json")


def make_recon():
    """相机绕圆，看向 COLMAP 的 (0, 0, 0.3)，即 Blender 的 (0, 0.3, 0)。"""
    cams = []
    n = 8
    radius = 3.0
    target_c = Vector((0.0, 0.0, 0.3))

    for i in range(n):
        ang = i / n * math.pi * 2
        eye = Vector((radius * math.sin(ang),
                      -radius * math.cos(ang),
                      0.8))

        fwd = (target_c - eye).normalized()
        right = fwd.cross(Vector((0, 0, 1))).normalized()
        down = fwd.cross(right).normalized()

        R = [[right.x, down.x, fwd.x],
             [right.y, down.y, fwd.y],
             [right.z, down.z, fwd.z]]

        cams.append({
            "frame": i,
            "image": f"frame_{i:05d}.jpg",
            "center": eye[:],
            "rotation": [R[r][c] for r in range(3) for c in range(3)],
            "focal": 500.0,
            "width": 640,
            "height": 480,
            "cx": 320.0,
            "cy": 240.0,
        })

    # 点云：一个直径 1 的球壳，中心在 Blender 的 (0, 0.3, 0)
    pts = []
    for a in range(40):
        for b in range(10):
            th = a / 40 * math.pi * 2
            ph = b / 10 * math.pi
            x = 0.5 * math.sin(ph) * math.cos(th)
            y = 0.5 * math.sin(ph) * math.sin(th)
            z = 0.5 * math.cos(ph)
            # COLMAP 坐标：blender (x,y,z) = (x, z, -y)
            pts.append([x, -z, y + 0.3])

    with open(RECON, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "cameras": cams, "points": pts}, f)


make_recon()

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
for c in list(bpy.data.collections):
    if c.name in ("Cameras", "PointCloud", "Proxies"):
        bpy.data.collections.remove(c)


def t_import():
    bpy.ops.blockout.import_recon(filepath=RECON)
    traj = bpy.data.objects.get("CameraTrajectory")
    assert traj is not None
    assert traj.animation_data is not None
    pc = bpy.data.objects.get("COLMAPPoints")
    assert pc is not None
    print(f"    相机 40 条曲线, 点 {len(pc.data.vertices)}")


check("导入 recon.json", t_import)


def t_projection():
    from bpy_extras.object_utils import world_to_camera_view

    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]
    scene.frame_set(1)
    bpy.context.view_layer.update()

    W, H = scene.render.resolution_x, scene.render.resolution_y
    target = Vector((0.0, 0.3, 0.0))
    co = world_to_camera_view(scene, cam, target)

    err = math.hypot(co.x * W - W / 2, (1 - co.y) * H - H / 2)
    print(f"    偏差 {err:.4f} 像素")
    assert err < 8.0, f"偏差 {err:.2f} 像素"


check("投影精度", t_projection)


def t_ray_inverse():
    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]
    scene.frame_set(3)
    bpy.context.view_layer.update()

    W, H = scene.render.resolution_x, scene.render.resolution_y
    from bpy_extras.object_utils import world_to_camera_view

    max_err = 0
    for nx, ny in [(0.5, 0.5), (0.3, 0.4), (0.7, 0.6), (0.15, 0.85), (0.85, 0.2)]:
        origin, direction = blockout_helper.screen_to_world_ray(scene, cam, nx, ny)
        p = origin + direction * 2.5
        co = world_to_camera_view(scene, cam, p)
        err = math.hypot((co.x - nx) * W, (co.y - ny) * H)
        max_err = max(max_err, err)

    print(f"    五个测试点，最大误差 {max_err:.4f} 像素")
    assert max_err < 1.0, f"{max_err:.2f} 像素"


check("射线/投影互逆", t_ray_inverse)


def t_depth_from_cloud():
    """
    关键测试：框住点云中心区域，估出来的深度应该
    等于相机到点云中心的真实距离。
    """
    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]
    scene.frame_set(1)
    bpy.context.view_layer.update()

    eye = cam.matrix_world.translation
    cloud_center = Vector((0.0, 0.3, 0.0))
    true_dist = (cloud_center - eye).length

    # 点云中心投影到屏幕的哪
    from bpy_extras.object_utils import world_to_camera_view
    co = world_to_camera_view(scene, cam, cloud_center)

    # 在中心附近框一小块
    m = 0.06
    est = blockout_helper.estimate_depth_from_cloud(
        scene, cam, co.x - m, co.x + m, co.y - m, co.y + m)

    print(f"    点云中心真实距离: {true_dist:.3f}")
    print(f"    估出来: {est:.3f}" if est else "    估出来: None")
    assert est is not None, "估不出深度"
    # 球壳上点有半径 0.5，允许一定误差
    assert abs(est - true_dist) < 0.6, f"误差 {abs(est-true_dist):.2f}"


check("从点云估深度", t_depth_from_cloud)


def t_box_from_camera():
    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]
    scene.frame_set(3)
    bpy.context.view_layer.update()

    bpy.ops.blockout.box_from_camera(
        x0=0.3, x1=0.7, y0=0.3, y1=0.7,
        depth=2.5, thickness=0.3, name="T_Flat")

    obj = bpy.data.objects["T_Flat"]
    dist = (obj.matrix_world.translation - cam.matrix_world.translation).length

    print(f"    深度 {dist:.3f}（指定 2.5）")
    print(f"    尺寸 {tuple(round(s,3) for s in obj.scale)}")
    assert abs(dist - 2.5) < 0.15
    assert obj.scale.x > 0.05


check("框选成盒（填数字版）", t_box_from_camera)


def t_flat_box_direct():
    """直接调内部函数，模拟鼠标拖框产出的盒子。"""
    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]
    scene.frame_set(3)
    bpy.context.view_layer.update()

    # Operator 类不能直接实例化，用临时对象借用方法
    op = type("TmpOp", (), {})()
    op._flat_box = blockout_helper.BO_OT_draw_box._flat_box.__get__(op, type(op))
    obj = op._flat_box(scene, cam, 0.35, 0.65, 0.35, 0.65)

    assert obj is not None, "没生成"
    print(f"    {obj.name} 尺寸 {tuple(round(s,3) for s in obj.scale)}")

    # 更新依赖图，让 matrix_world 反映刚设的旋转
    bpy.context.view_layer.update()

    cam_q = cam.matrix_world.to_quaternion()
    obj_q = obj.matrix_world.to_quaternion()
    dot = abs(cam_q.dot(obj_q))

    print(f"    相机四元数 {tuple(round(v,3) for v in cam_q)}")
    print(f"    盒子四元数 {tuple(round(v,3) for v in obj_q)}")
    print(f"    点积 {dot:.4f}")
    assert dot > 0.98, f"没正对相机，点积 {dot:.4f}"


check("拖框产出的贴片盒子", t_flat_box_direct)


def t_ground_box_direct():
    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]
    scene.frame_set(3)
    bpy.context.view_layer.update()

    op = type("TmpOp2", (), {})()
    op._box_ground = blockout_helper.BO_OT_draw_box._box_ground.__get__(op, type(op))
    obj = op._box_ground(scene, cam, 0.3, 0.7, 0.1, 0.8)

    assert obj is not None, "没生成"
    print(f"    {obj.name} 尺寸 {tuple(round(s,3) for s in obj.scale)}")

    assert obj.scale.x > 0.05 and obj.scale.z > 0.05, "尺寸不对"
    assert obj.scale.z > scene.blockout.default_thickness


check("拖框产出的立体盒子", t_ground_box_direct)


def t_snap_ground():
    obj = bpy.data.objects.get("T_Flat")
    obj.location.z = 5.0
    blockout_helper.select_only(obj)
    bpy.ops.blockout.snap_ground(ground_z=0.0)

    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lowest = min(c.z for c in corners)
    print(f"    最低点 z = {lowest:.5f}")
    assert abs(lowest) < 0.01


check("落到地面", t_snap_ground)


def t_iou():
    f = blockout_helper.aabb_iou
    assert abs(f((0, 0, 1, 1), (0, 0, 1, 1)) - 1.0) < 1e-9
    assert abs(f((0, 0, 1, 1), (1, 1, 2, 2)) - 0.0) < 1e-9
    assert abs(f((0, 0, 1, 1), (0, 0, 0.5, 1)) - 0.5) < 1e-9
    assert abs(f((0, 0, 1, 1), (0.5, 0, 1.5, 1)) - 1/3) < 1e-9
    assert abs(f((0, 0, 2, 2), (0, 0, 1, 1)) - 0.25) < 1e-9
    print("    五组用例通过")


check("IoU", t_iou)


def t_projection_all_frames():
    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]
    obj = bpy.data.objects.get("T_Flat")

    ok = skip = 0
    for f in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(f)
        bpy.context.view_layer.update()
        if blockout_helper.project_box(obj, cam, scene) is None:
            skip += 1
        else:
            ok += 1

    print(f"    可投影 {ok} 帧，跳过 {skip} 帧")
    assert ok > 0


check("全帧投影", t_projection_all_frames)


def t_estimate_edge_cases():
    """点云估深度的边界情况不能崩。"""
    scene = bpy.context.scene
    cam = bpy.data.objects["CameraTrajectory"]

    # 框到完全空白的地方
    r = blockout_helper.estimate_depth_from_cloud(scene, cam, 0.98, 1.0, 0.98, 1.0)
    print(f"    空白区域: {r}")
    assert r is None, "空白处应该返回 None"

    # 临时藏掉点云
    pc = bpy.data.objects.get("COLMAPPoints")
    h = pc.hide_viewport
    pc.hide_viewport = True
    r2 = blockout_helper.estimate_depth_from_cloud(scene, cam, 0.4, 0.6, 0.4, 0.6)
    pc.hide_viewport = h
    print(f"    点云隐藏时: {r2}")


check("估深度边界情况", t_estimate_edge_cases)


def t_unregister():
    blockout_helper.unregister()


check("注销", t_unregister)


print("\n" + "=" * 64)
if failures:
    print(f"✗ {len(failures)} 项失败:")
    for n, e in failures:
        print(f"    {n}: {e}")
    sys.exit(1)
else:
    print("✓ 全部通过")
    sys.exit(0)
