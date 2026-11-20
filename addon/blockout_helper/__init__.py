"""Blockout Helper - video to blockout + AI assistant for Blender.

This module was rebuilt because the previous copy was corrupted by a
toolchain encoding bug (GBK round-trip destroyed every CJK string).
Code structure follows the original contracts so existing tests pass.

Layers:
  kit / skeleton / camera / scene / ops_video  - video pipeline
  dispatch / api_client / tools / agent / ui_ai - AI chat layer
"""

bl_info = {
    "name": "Blockout Helper",
    "author": "MrBear9, dafeiyu",
    "maintainer": "MrBear9",
    "version": (1, 14, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Blockout",
    "description": "Video to blockout + AI assistant: camera alignment, "
                   "box modeling, joint->rig->camera pipeline; LLM chat "
                   "(DeepSeek / OpenAI / Ollama)",
    "category": "3D View",
    "doc_url": "https://github.com/MrBear9/blender-video-to-blockout",
    "tracker_url": "https://github.com/MrBear9/blender-video-to-blockout/issues",
}

import bpy
import bmesh
import json
import math
import os

from mathutils import Vector, Matrix, Quaternion, Euler

# ---------------------------------------------------------------------------
# Sub-modules (defensive imports: one broken layer must not kill the addon)
# ---------------------------------------------------------------------------

try:
    from . import kit, skeleton, camera as camera_mod, scene as scene_mod
    from . import ops_video
    from . import video_ref
    _video_ops = ops_video.classes
    _VIDEO_OK = True
    _VIDEO_ERR = ""
except Exception as _e:
    _video_ops = ()
    _VIDEO_OK = False
    _VIDEO_ERR = "%s: %s" % (type(_e).__name__, _e)

try:
    from . import ui_ai
    _ai_ok = True
    _ai_err = ""
except Exception as _e2:
    ui_ai = None
    _ai_ok = False
    _ai_err = "%s: %s" % (type(_e2).__name__, _e2)

# ── DSH Blender Bridge（第三方，作者 Siddharth Ahuja，原为独立插件）──
# 合并成一个插件的目的是"只装一次"，但它内部有 69 个文件、自己的面板组
# （侧边栏 Category 仍是 "DSH Blender Bridge"，和 Blockout 分两个 tab）。
# 它是操作 Blender 的运行时通道，导入失败**绝不能**连累建模面板 ——
# 所以这里单独兜住，出问题时插件照常可用，只是桥接不启动。
try:
    from . import bridge as bridge_mod
    _bridge_ok = True
    _bridge_err = ""
except Exception as _e3:
    bridge_mod = None
    _bridge_ok = False
    _bridge_err = "%s: %s" % (type(_e3).__name__, _e3)


# ---------------------------------------------------------------------------
# Coordinate conversion (COLMAP -> Blender)
# ---------------------------------------------------------------------------

A_WORLD = Matrix((
    (1, 0, 0),
    (0, 0, 1),
    (0, -1, 0),
))

A_CAM = Matrix((
    (1, 0, 0),
    (0, -1, 0),
    (0, 0, -1),
))


def colmap_to_blender_rot(R_flat):
    """COLMAP cam->world rotation to Blender rotation.

    Verified numerically in check_transform.py:
        R_blender = A_world @ R_colmap @ A_cam
    """
    m = Matrix((
        (R_flat[0], R_flat[1], R_flat[2]),
        (R_flat[3], R_flat[4], R_flat[5]),
        (R_flat[6], R_flat[7], R_flat[8]),
    ))
    return A_WORLD @ m @ A_CAM


def colmap_to_blender_pt(x, y, z):
    return Vector((x, z, -y))


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------

def ensure_collection(name, parent=None):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(col)
    return col


def clear_collection(name):
    col = bpy.data.collections.get(name)
    if not col:
        return
    for obj in list(col.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def link_to(obj, col_name, parent=None):
    col = ensure_collection(col_name, parent)
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    col.objects.link(obj)


# ---------------------------------------------------------------------------
# Import reconstruction
# ---------------------------------------------------------------------------

def import_reconstruction(json_path, scene):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "error" in data:
        raise RuntimeError("recon.json has error flag: %s" % data["error"])

    cams = data.get("cameras", [])
    if not cams:
        raise RuntimeError("recon.json contains no cameras")

    clear_collection("Cameras")

    cam_data = bpy.data.cameras.new("TrajectoryCam")
    cam_data.sensor_width = 36.0

    first = cams[0]
    img_w = first.get("width", 1920)
    img_h = first.get("height", 1080)
    cam_data.lens = (first.get("focal", 1000.0) / img_w) * cam_data.sensor_width

    traj = bpy.data.objects.new("CameraTrajectory", cam_data)
    ensure_collection("Cameras").objects.link(traj)
    scene.camera = traj

    scene.render.resolution_x = img_w
    scene.render.resolution_y = img_h
    scene.render.resolution_percentage = 100

    for i, c in enumerate(cams):
        frame = c.get("blender_frame", c["frame"] + 1)
        C = colmap_to_blender_pt(*c["center"])
        Rb = colmap_to_blender_rot(c["rotation"])

        traj.location = C
        traj.rotation_mode = "QUATERNION"
        traj.rotation_quaternion = Rb.to_quaternion()
        traj.keyframe_insert("location", frame=frame)
        traj.keyframe_insert("rotation_quaternion", frame=frame)

        if i == 0:
            cam_data.keyframe_insert("lens", frame=frame)

    if traj.animation_data and traj.animation_data.action:
        for fc in traj.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"

    scene.frame_start = 1
    scene.frame_end = max(
        c.get("blender_frame", c["frame"] + 1) for c in cams
    )

    # --- point cloud ---
    pts = data.get("points", [])
    if pts:
        clear_collection("PointCloud")
        mesh = bpy.data.meshes.new("COLMAPPoints")
        verts = [tuple(colmap_to_blender_pt(*p)) for p in pts]
        mesh.from_pydata(verts, [], [])
        mesh.update()

        pc = bpy.data.objects.new("COLMAPPoints", mesh)
        ensure_collection("PointCloud").objects.link(pc)

        m = bpy.data.materials.new("PointMat")
        m.use_nodes = True
        nt = m.node_tree
        nt.nodes.clear()
        out_n = nt.nodes.new("ShaderNodeOutputMaterial")
        emit = nt.nodes.new("ShaderNodeEmission")
        emit.inputs["Color"].default_value = (0.45, 0.78, 1.0, 1)
        emit.inputs["Strength"].default_value = 4.0
        nt.links.new(emit.outputs[0], out_n.inputs["Surface"])

        pc.data.materials.append(m)

        ng = bpy.data.node_groups.get("_bo_points")
        if ng is None:
            ng = bpy.data.node_groups.new("_bo_points", "GeometryNodeTree")
            ng.interface.new_socket("Geometry", in_out="INPUT",
                                    socket_type="NodeSocketGeometry")
            ng.interface.new_socket("Geometry", in_out="OUTPUT",
                                    socket_type="NodeSocketGeometry")
            n_in = ng.nodes.new("NodeGroupInput")
            n_out = ng.nodes.new("NodeGroupOutput")
            to_pts = ng.nodes.new("GeometryNodeMeshToPoints")
            to_pts.inputs["Radius"].default_value = 0.012
            setmat = ng.nodes.new("GeometryNodeSetMaterial")
            setmat.inputs["Material"].default_value = m
            ng.links.new(n_in.outputs[0], to_pts.inputs["Mesh"])
            ng.links.new(to_pts.outputs[0], setmat.inputs["Geometry"])
            ng.links.new(setmat.outputs[0], n_out.inputs[0])

        mod = pc.modifiers.new("Points", "NODES")
        mod.node_group = ng

        pc.hide_set(True)

    return data


# ---------------------------------------------------------------------------
# Background overlay
# ---------------------------------------------------------------------------

def setup_background(path, scene, alpha=0.5):
    traj = bpy.data.objects.get("CameraTrajectory")
    if traj is None:
        raise RuntimeError("import recon.json first")

    cam_data = traj.data
    cam_data.show_background_images = True
    cam_data.background_images.clear()

    bg = cam_data.background_images.new()

    if path.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
        clip = bpy.data.movieclips.load(path)
        bg.source = "MOVIE_CLIP"
        bg.clip = clip
    else:
        img = bpy.data.images.load(path)
        bg.source = "IMAGE"
        bg.image = img

    bg.alpha = alpha
    bg.display_depth = "BACK"
    bg.frame_method = "FIT"

    return bg


# ---------------------------------------------------------------------------
# Materials / geometry
# ---------------------------------------------------------------------------

_PROXY_MAT = None


def proxy_material():
    global _PROXY_MAT
    if _PROXY_MAT and _PROXY_MAT.name in bpy.data.materials:
        return _PROXY_MAT

    m = bpy.data.materials.new("ProxyMat")
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.66, 0.68, 0.72, 1)
        bsdf.inputs["Roughness"].default_value = 0.82
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = 0.2

    _PROXY_MAT = m
    return m


def make_box(name, center, size):
    """Build a box with exact dimensions (no bpy.ops)."""
    col = ensure_collection("Proxies")

    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(me)
    bm.free()

    obj = bpy.data.objects.new(name, me)
    col.objects.link(obj)

    obj.location = Vector(center)
    obj.scale = Vector(size)

    me.materials.append(proxy_material())
    return obj


def auto_name(prefix):
    n = 1
    while True:
        name = "%s_%02d" % (prefix, n)
        if name not in bpy.data.objects:
            return name
        n += 1


def select_only(obj):
    for o in bpy.context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------

def project_point(scene, cam, p):
    """World point -> normalized screen (x, y, depth); None if behind."""
    from bpy_extras.object_utils import world_to_camera_view
    co = world_to_camera_view(scene, cam, p)
    if co.z <= 0.001:
        return None
    return (co.x, co.y, co.z)


def screen_to_world_ray(scene, cam, nx, ny):
    """Normalized screen coords -> world ray (origin, direction)."""
    mw = cam.matrix_world
    lens = cam.data.lens
    sensor = cam.data.sensor_width
    sensor_fit = cam.data.sensor_fit

    rx = scene.render.resolution_x
    ry = scene.render.resolution_y

    if sensor_fit == "AUTO":
        sensor_fit = "HORIZONTAL" if rx >= ry else "VERTICAL"

    if sensor_fit == "HORIZONTAL":
        sensor_w = sensor
        sensor_h = sensor * ry / rx
    else:
        sensor_h = sensor
        sensor_w = sensor * rx / ry

    px = nx - 0.5
    py = ny - 0.5

    local = Vector((
        px * sensor_w / lens,
        py * sensor_h / lens,
        -1.0,
    ))
    local.normalize()

    direction = (mw.to_quaternion() @ local).normalized()
    origin = mw.translation.copy()

    return origin, direction


def project_box(obj, cam, scene):
    from bpy_extras.object_utils import world_to_camera_view
    out = []
    for corner in obj.bound_box:
        p = obj.matrix_world @ Vector(corner)
        co = world_to_camera_view(scene, cam, p)
        if co.z <= 0.001:
            return None
        out.append((co.x, co.y))
    return out


def aabb_iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    u = area_a + area_b - inter
    return inter / u if u > 1e-12 else 0.0


def estimate_depth_from_cloud(scene, cam, x0, x1, y0, y1):
    """Median depth of point-cloud points inside the normalized rectangle.

    Returns None when there are too few points (caller falls back to a
    default depth).
    """
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0

    pc = bpy.data.objects.get("COLMAPPoints")
    if pc is None:
        return None
    if not hasattr(pc.data, "vertices") or len(pc.data.vertices) == 0:
        return None

    mw = pc.matrix_world
    depths = []
    for v in pc.data.vertices:
        p = mw @ v.co
        co = project_point(scene, cam, p)
        if co is None:
            continue
        if x0 <= co[0] <= x1 and y0 <= co[1] <= y1:
            depths.append(co[2])

    if len(depths) < 5:
        return None

    depths.sort()
    return depths[len(depths) // 2]


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class BO_OT_import(bpy.types.Operator):
    """读 recon.json，建相机轨迹和点云"""
    bl_idname = "blockout.import_recon"
    bl_label = "① 导入重建"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        try:
            data = import_reconstruction(self.filepath, context.scene)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        n = len(data.get("cameras", []))
        self.report({"INFO"},
                    "相机 %d 帧，点 %d 个"
                    % (n, len(data.get("points", []))))
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class BO_OT_load_ref(bpy.types.Operator):
    """把视频或图片作为相机背景，随时对着看"""
    bl_idname = "blockout.load_ref"
    bl_label = "② 载入原片"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(
        default="*.mp4;*.mov;*.avi;*.mkv;*.webm;*.jpg;*.jpeg;*.png",
        options={"HIDDEN"},
    )

    def execute(self, context):
        try:
            setup_background(self.filepath, context.scene,
                             context.scene.blockout.bg_alpha)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, "原片已压进视口，拖时间轴对一对")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class BO_OT_draw_box(bpy.types.Operator):
    """Drag a rectangle in the viewport to create a box"""
    bl_idname = "blockout.draw_box"
    bl_label = "Draw box"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        self._start = None
        self._cur = None
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "LEFTMOUSE":
            if event.value == "PRESS":
                self._start = (event.mouse_region_x, event.mouse_region_y)
                self._cur = self._start
                return {"RUNNING_MODAL"}
            if event.value == "RELEASE":
                self._cur = (event.mouse_region_x, event.mouse_region_y)
                self._finish(context)
                return {"FINISHED"}

        if event.type == "MOUSEMOVE":
            if self._start:
                self._cur = (event.mouse_region_x, event.mouse_region_y)
            return {"RUNNING_MODAL"}

        if event.type in {"ESC", "RIGHTMOUSE"}:
            self._start = None
            return {"CANCELLED"}

        return {"RUNNING_MODAL"}

    # --- core box builders (also used directly by tests) ---

    def _flat_box(self, scene, cam, x0, x1, y0, y1):
        """Thin box facing the camera, sized to the rectangle."""
        props = scene.blockout

        depth = estimate_depth_from_cloud(scene, cam, x0, x1, y0, y1)
        if depth is None:
            depth = props.default_depth

        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        pts = []
        for nx, ny in corners:
            origin, direction = screen_to_world_ray(scene, cam, nx, ny)
            pts.append(origin + direction * depth)

        center = sum(pts, Vector((0, 0, 0))) / 4.0

        q = cam.matrix_world.to_quaternion()
        q_inv = q.inverted()
        local = [q_inv @ (p - center) for p in pts]
        xs = [p.x for p in local]
        ys = [p.y for p in local]
        zs = [p.z for p in local]

        size = (max(xs) - min(xs),
                props.default_thickness,
                max(zs) - min(zs))

        obj = make_box(auto_name("Proxy"), center, size)
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = q
        return obj

    def _box_ground(self, scene, cam, x0, x1, y0, y1):
        """Solid box standing on the ground plane, from the rectangle.

        Height is derived from the rectangle's pixel extent mapped through
        the camera FOV and depth. Using the vertical world distance between
        the top and bottom rays fails when the camera looks horizontally
        (the two rays differ in depth, not height) - measured 0.0 m height.
        """
        props = scene.blockout

        depth = estimate_depth_from_cloud(scene, cam, x0, x1, y0, y1)
        if depth is None:
            depth = props.default_depth

        # 底边中点 -> 地面落点
        bx = (x0 + x1) * 0.5
        origin, direction = screen_to_world_ray(scene, cam, bx, y1)
        foot = origin + direction * depth

        # 用相机的垂直视场把归一化高度换算成米
        lens = cam.data.lens
        sensor = cam.data.sensor_width
        rx = scene.render.resolution_x
        ry = scene.render.resolution_y
        tan_h = (sensor * 0.5) / lens
        tan_v = tan_h * (ry / float(rx))

        height = 2.0 * depth * tan_v * abs(y1 - y0)
        width = 2.0 * depth * tan_h * abs(x1 - x0) * (ry / float(rx))

        if height < props.default_thickness * 2:
            height = props.default_thickness * 2
        if width < 0.05:
            width = 0.05

        ground_z = min(foot.z, 0.0) if abs(foot.z) < 0.5 else foot.z

        size = (width, width * props.ground_depth_ratio, height)
        center = (foot.x, foot.y, ground_z + height * 0.5)

        obj = make_box(auto_name("Proxy"), center, size)
        obj.rotation_euler = (0, 0, 0)
        return obj

    def _finish(self, context):
        if not self._start or not self._cur:
            return
        x0, y0 = self._start
        x1, y1 = self._cur
        if abs(x1 - x0) < 8 or abs(y1 - y0) < 8:
            return

        region = context.region
        rw, rh = region.width, region.height
        nx0, nx1 = sorted((x0 / rw, x1 / rw))
        ny0, ny1 = sorted((y0 / rh, y1 / rh))

        props = context.scene.blockout
        scene = context.scene
        cam = scene.camera
        if cam is None:
            self.report({"ERROR"}, "no active camera")
            return

        if props.box_mode == "FLAT":
            obj = self._flat_box(scene, cam, nx0, nx1, ny0, ny1)
        else:
            obj = self._box_ground(scene, cam, nx0, nx1, ny0, ny1)

        if obj is None:
            self.report({"WARNING"}, "could not create box")
            return

        select_only(obj)
        self.report({"INFO"}, "created %s" % obj.name)


class BO_OT_box_from_camera(bpy.types.Operator):
    """Create a camera-facing box from normalized screen coords (or align)"""
    bl_idname = "blockout.box_from_camera"
    bl_label = "Align to camera"

    # 数字版：直接给归一化坐标建盒（不用鼠标）
    x0: bpy.props.FloatProperty(default=0.3, min=0.0, max=1.0)
    x1: bpy.props.FloatProperty(default=0.7, min=0.0, max=1.0)
    y0: bpy.props.FloatProperty(default=0.3, min=0.0, max=1.0)
    y1: bpy.props.FloatProperty(default=0.7, min=0.0, max=1.0)
    depth: bpy.props.FloatProperty(default=2.5, min=0.01)
    thickness: bpy.props.FloatProperty(default=0.3, min=0.01)
    name: bpy.props.StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        cam = scene.camera
        if cam is None:
            self.report({"ERROR"}, "no active camera")
            return {"CANCELLED"}

        corners = [(self.x0, self.y0), (self.x1, self.y0),
                   (self.x1, self.y1), (self.x0, self.y1)]
        pts = []
        for nx, ny in corners:
            origin, direction = screen_to_world_ray(scene, cam, nx, ny)
            pts.append(origin + direction * self.depth)

        center = sum(pts, Vector((0, 0, 0))) / 4.0
        q = cam.matrix_world.to_quaternion()
        q_inv = q.inverted()
        local = [q_inv @ (p - center) for p in pts]
        xs = [p.x for p in local]
        zs = [p.z for p in local]

        size = (max(xs) - min(xs), self.thickness, max(zs) - min(zs))
        nm = self.name or auto_name("Proxy")
        obj = make_box(nm, center, size)
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = q

        select_only(obj)
        self.report({"INFO"}, "created %s (depth %.2f)" % (obj.name, self.depth))
        return {"FINISHED"}


class BO_OT_box_ground(bpy.types.Operator):
    """Stand the selected box on the ground"""
    bl_idname = "blockout.box_ground"
    bl_label = "Ground box"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "select a box first")
            return {"CANCELLED"}
        obj.rotation_euler = (0, 0, 0)
        return {"FINISHED"}


class BO_OT_snap_ground(bpy.types.Operator):
    """Move the object so its bottom sits on the given Z"""
    bl_idname = "blockout.snap_ground"
    bl_label = "落到地面"

    ground_z: bpy.props.FloatProperty(name="Ground Z", default=0.0)

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "select a box first")
            return {"CANCELLED"}

        zs = [(obj.matrix_world @ Vector(c)).z for c in obj.bound_box]
        obj.location.z += self.ground_z - min(zs)
        self.report({"INFO"}, "snapped to Z=%.2f" % self.ground_z)
        return {"FINISHED"}


class BO_OT_face_camera(bpy.types.Operator):
    """Rotate the box to face the camera"""
    bl_idname = "blockout.face_camera"
    bl_label = "正对相机"

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "select a box first")
            return {"CANCELLED"}
        cam = context.scene.camera
        if cam is None:
            self.report({"ERROR"}, "no active camera")
            return {"CANCELLED"}
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = cam.matrix_world.to_quaternion()
        return {"FINISHED"}


class BO_OT_fit_points(bpy.types.Operator):
    """Fit the selected box to the point cloud bounds"""
    bl_idname = "blockout.fit_points"
    bl_label = "用点云拟合"

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "select a box first")
            return {"CANCELLED"}

        pc = bpy.data.objects.get("COLMAPPoints")
        if pc is None:
            self.report({"ERROR"}, "no point cloud; import recon.json first")
            return {"CANCELLED"}

        pts = [pc.matrix_world @ v.co for v in pc.data.vertices]
        if not pts:
            self.report({"ERROR"}, "point cloud is empty")
            return {"CANCELLED"}

        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        zs = [p.z for p in pts]

        obj.location = (sum(xs) / len(xs), sum(ys) / len(ys),
                        (min(zs) + max(zs)) / 2.0)
        obj.scale = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))

        self.report({"INFO"}, "fitted to %d points" % len(pts))
        return {"FINISHED"}


class BO_OT_snapshot(bpy.types.Operator):
    """Save a comparison render"""
    bl_idname = "blockout.snapshot"
    bl_label = "存一张对照图"

    def execute(self, context):
        props = context.scene.blockout
        d = bpy.path.abspath(props.shot_dir or "//")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "shot_%04d.png" % context.scene.frame_current)
        context.scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        self.report({"INFO"}, "saved %s" % path)
        return {"FINISHED"}


class BO_OT_score(bpy.types.Operator):
    """Compute IoU between box projections and mask bitmaps"""
    bl_idname = "blockout.score"
    bl_label = "算对齐分数"

    def execute(self, context):
        props = context.scene.blockout
        d = bpy.path.abspath(props.mask_dir or "")
        if not os.path.isdir(d):
            self.report({"ERROR"}, "set a valid mask directory")
            return {"CANCELLED"}

        scene = context.scene
        cam = scene.camera
        if cam is None:
            self.report({"ERROR"}, "no active camera")
            return {"CANCELLED"}

        rows = []
        for obj in bpy.data.objects:
            if not obj.name.startswith("Proxy"):
                continue
            box = project_box(obj, cam, scene)
            if box is None:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            aabb = (min(xs), min(ys), max(xs), max(ys))

            mask = os.path.join(d, obj.name + ".png")
            if not os.path.isfile(mask):
                rows.append("%s\t0.00\t0.00\tno mask" % obj.name)
                continue

            mb = mask_aabb(mask)
            iou = aabb_iou(aabb, mb) if mb else 0.0
            rows.append("%s\t%.3f\t%.3f\t%s"
                        % (obj.name, iou, iou,
                           "ok" if iou >= props.threshold else "low"))

        props.last_report = "\n".join(rows) if rows else ""
        n_ok = sum(1 for r in rows if r.endswith("ok"))
        self.report({"INFO"}, "%d boxes, %d pass" % (len(rows), n_ok))
        return {"FINISHED"}


def mask_aabb(path):
    """Read a mask PNG and return its normalized bbox (x0, y0, x1, y1)."""
    img = None
    try:
        img = bpy.data.images.load(path)
        w, h = img.size
        px = list(img.pixels)
        xs, ys = [], []
        for i in range(w * h):
            if px[i * 4] > 0.5:
                xs.append((i % w) / w)
                ys.append(1.0 - (i // w) / h)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        return None
    finally:
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass


def _update_ref_overlay(self, context):
    """视口叠加开关：把原片压成当前相机的背景图。

    为什么用相机背景图而不是 gpu draw handler：
    背景图是 Blender 原生功能，会跟着相机透视正确贴合，Numpad 0 进相机视角
    就能直接和白模比对；自绘句柄要自己算投影矩阵，还得处理 DNA 主题、
    多视口、色彩管理，为这一个开关不值得。
    """
    try:
        apply_ref_overlay(context.scene, self)
    except Exception as e:                     # noqa: BLE001
        print("[blockout_helper] 原片叠加失败: %s" % e)


def apply_ref_overlay(scene, props):
    """按 props.ref_overlay 把原片挂到当前相机的背景上（或摘掉）。"""
    cam = scene.camera
    if cam is None:
        return False
    cam_data = cam.data
    cam_data.background_images.clear()

    if not props.ref_overlay or not props.ref_path:
        cam_data.show_background_images = False
        return False

    path = props.ref_path
    bg = cam_data.background_images.new()
    if path.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
        # 复用已有 clip，别每次点开关都新建一个数据块
        clip = bpy.data.movieclips.get(os.path.basename(path))
        if clip is None:
            clip = bpy.data.movieclips.load(path)
        bg.source = "MOVIE_CLIP"
        bg.clip = clip
    else:
        img = bpy.data.images.get(os.path.basename(path))
        if img is None:
            img = bpy.data.images.load(path)
        bg.source = "IMAGE"
        bg.image = img

    bg.alpha = props.ref_overlay_alpha
    bg.display_depth = "BACK"
    bg.frame_method = "FIT"
    cam_data.show_background_images = True
    return True


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class BlockoutProps(bpy.types.PropertyGroup):
    bg_alpha: bpy.props.FloatProperty(
        name="原片透明度", default=0.5, min=0.0, max=1.0)
    mask_dir: bpy.props.StringProperty(
        name="Mask 目录", subtype="DIR_PATH", default="")
    threshold: bpy.props.FloatProperty(
        name="及格线", default=0.7, min=0.0, max=1.0)
    shot_dir: bpy.props.StringProperty(
        name="对照图目录", subtype="DIR_PATH", default="")

    box_mode: bpy.props.EnumProperty(
        name="盒子类型",
        items=[
            ("FLAT", "贴片", "Thin box facing the camera"),
            ("GROUND", "立体", "Block standing on the ground"),
        ],
        default="FLAT",
    )
    default_depth: bpy.props.FloatProperty(
        name="默认深度", default=2.0, min=0.05)
    default_thickness: bpy.props.FloatProperty(
        name="贴片厚度", default=0.12, min=0.01)
    ground_depth_ratio: bpy.props.FloatProperty(
        name="进深比例", default=0.6, min=0.1, max=3.0)

    last_report: bpy.props.StringProperty(default="")

    joint_source: bpy.props.StringProperty(
        name="关节文件", subtype="FILE_PATH", default="")
    joint_frames: bpy.props.IntProperty(name="帧数", default=0)
    joint_unit: bpy.props.StringProperty(name="单位", default="cm")
    joint_summary: bpy.props.StringProperty(default="")

    smooth_median: bpy.props.IntProperty(
        name="中值窗口", default=5, min=1, max=31)
    smooth_mean: bpy.props.IntProperty(
        name="均值窗口", default=3, min=1, max=15)

    rig_height_m: bpy.props.FloatProperty(
        name="身高(m)", default=1.70, min=0.3, max=3.0)
    joint_radius_m: bpy.props.FloatProperty(
        name="关节球半径(m)", default=0.019, min=0.002, max=0.2)
    rig_with_head: bpy.props.BoolProperty(name="带头", default=True)
    rig_with_joints: bpy.props.BoolProperty(name="带关节球", default=True)
    rig_summary: bpy.props.StringProperty(default="")
    rig_ground_offset: bpy.props.FloatProperty(default=0.0)
    playback_fps: bpy.props.IntProperty(
        name="播放帧率", default=25, min=1, max=120)

    camera_preset: bpy.props.EnumProperty(
        name="运镜",
        items=[
            ("follow_ref", "跟原片", "Use the camera trajectory from recon.json"),
            ("cinematic", "Cinematic", "Low angle to overhead"),
            ("turntable", "Turntable", "Full orbit"),
            ("dolly_in", "Dolly in", "Wide to half body"),
            ("top_down", "Top down", "Overhead"),
        ],
        default="follow_ref",
    )
    camera_wobble: bpy.props.BoolProperty(
        name="手持微抖", default=True)
    camera_summary: bpy.props.StringProperty(default="")

    # ── 原片参考 / 对比播帧 ──
    ref_path: bpy.props.StringProperty(
        name="原片", subtype="FILE_PATH", default="")
    ref_frames: bpy.props.IntProperty(name="原片帧数", default=0)
    ref_overlay: bpy.props.BoolProperty(
        name="视口叠加原片", default=False, update=_update_ref_overlay)
    ref_overlay_alpha: bpy.props.FloatProperty(
        name="叠加透明度", default=0.45, min=0.0, max=1.0,
        update=_update_ref_overlay)
    ref_show_player: bpy.props.BoolProperty(
        name="显示播放器", default=True)

    verify_verdict: bpy.props.StringProperty(default="")
    verify_worst: bpy.props.FloatProperty(default=0.0)
    verify_summary: bpy.props.StringProperty(default="")

    output_dir: bpy.props.StringProperty(
        name="输出目录", subtype="DIR_PATH", default="//blockout_out")
    render_w: bpy.props.IntProperty(name="宽", default=1280, min=64, max=7680)
    render_h: bpy.props.IntProperty(name="高", default=720, min=64, max=4320)
    output_fps: bpy.props.IntProperty(name="输出帧率", default=25, min=1, max=120)
    encode_quality: bpy.props.EnumProperty(
        name="画质",
        items=[("LOW", "Draft", ""), ("MEDIUM", "Medium", ""),
               ("HIGH", "High", ""), ("PERC_LOSSLESS", "Near lossless", "")],
        default="HIGH")
    render_force: bpy.props.BoolProperty(
        name="重渲已有帧", default=False)
    render_summary: bpy.props.StringProperty(default="")


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def _ref_image():
    """面板播放器用的 Image —— 对应「最近一次取到的帧」。

    ⚠ **这里不能取帧**。面板的 draw() 每秒会被调用好几次，
    而取一帧要渲染（~20ms）并临时改 scene 的渲染设置和帧号 ——
    在 draw 里做等于每帧都卡一次，还会干扰正在跑的渲染。
    取帧统一由 frame_change_post 回调在「帧真的变了」时做一次，
    这里只负责把那张图交给 UI。

    ⚠ 必须返回真 Image 对象给 template_image，传 None 会让面板画不出来。
    """
    fp = bpy.app.driver_namespace.get("_bo_ref_frame", "")
    if not fp:
        return None

    img = bpy.data.images.get("_BO_REF_VIEW")
    if img is None:
        try:
            img = bpy.data.images.load(fp, check_existing=False)
            img.name = "_BO_REF_VIEW"
        except Exception:
            return None
        return img

    # 同一张图换内容：改 filepath 再 reload，比反复新建/删除数据块便宜
    if bpy.path.abspath(img.filepath) != fp:
        try:
            img.filepath = fp
            img.reload()
        except Exception:
            return img
    return img


def _on_frame_change(scene, depsgraph=None):
    """时间轴变帧 → 取一张原片缩略图。

    只在「载了原片 + 播放器开着」时做，其它时候立刻返回，
    不打扰正常建模。
    """
    try:
        props = getattr(scene, "blockout", None)
        if props is None or not props.ref_path or not props.ref_show_player:
            return
        from . import video_ref

        fp = video_ref.grab(scene, props.ref_path, scene.frame_current)
        if fp:
            bpy.app.driver_namespace["_bo_ref_frame"] = fp
    except Exception as e:                     # noqa: BLE001
        # 帧正在被拖、文件被删、渲染冲突…… 都不该打断用户操作
        print("[blockout_helper] 原片取帧失败: %s" % e)


class VIEW3D_PT_blockout_main(bpy.types.Panel):
    """视频转白膜 —— 一条流程走完：原片 → 静态搭建 → 动态骨骼 → 验收

    以前拆成「视频转白膜」和「视频转白膜 · 骨骼」两个面板，看着像两条路，
    实际是同一条：先静态搭好，再让骨骼动起来。合并成一个，按步骤编号走。
    """

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blockout"
    bl_label = "视频转白膜"

    def draw(self, context):
        layout = self.layout
        props = context.scene.blockout
        scene = context.scene

        # ── 原片：整条流程的参照物，放最上面 ──
        box = layout.box()
        box.label(text="① 原片", icon="FILE_MOVIE")
        row = box.row(align=True)
        row.operator("blockout.load_ref_video", text="载入原片", icon="FILEBROWSER")
        if props.ref_frames:
            box.label(text="%d 帧 · 时间轴 1–%d"
                      % (props.ref_frames, scene.frame_end), icon="INFO")

        # 播放器：拖时间轴就能看到对应帧，用来比对白模
        if props.ref_path:
            box.prop(props, "ref_show_player", text="显示播放器")
            if props.ref_show_player:
                img = _ref_image()
                if img is not None:
                    # ⚠ template_image(img, "pixels") 实测画不出东西 ——
                    #   它要求 Image 上有名为 "pixels" 的**属性**，实际取不到。
                    #   走 preview（icon_id）这条路才真的渲染出来。
                    try:
                        img.preview_ensure()
                        if img.preview and img.preview.icon_id:
                            box.template_icon(icon_value=img.preview.icon_id,
                                              scale=9.0)
                        else:
                            box.label(text="缩略图未就绪", icon="IMAGE_DATA")
                    except Exception as e:      # noqa: BLE001
                        box.label(text="缩略图失败: %s" % e, icon="ERROR")
                else:
                    box.label(text="还没取到帧", icon="INFO")
                box.label(text="第 %d 帧 / 共 %d"
                          % (scene.frame_current, props.ref_frames),
                          icon="TIME")
                row = box.row(align=True)
                row.operator("blockout.compare_frame", text="并排对照",
                             icon="IMAGE_REFERENCE")
            row = box.row()
            row.prop(props, "ref_overlay", text="视口叠加原片")
            if props.ref_overlay:
                row.prop(props, "ref_overlay_alpha", text="")

        # ── 静态搭建 ──
        box = layout.box()
        box.label(text="② 静态搭建", icon="MESH_CUBE")
        box.operator("blockout.import_recon", text="导入 recon.json",
                     icon="IMPORT")
        box.label(text="重建相机轨迹 + 点云，白模对齐的基准", icon="INFO")

        row = box.row(align=True)
        row.prop(props, "box_mode", expand=True)
        col = box.column(align=True)
        col.operator("blockout.draw_box", text="拖一个框 → 生成盒子",
                     icon="SELECT_SET")
        box.label(text="拖的时候按住左键，松手就成", icon="INFO")

        col = box.column(align=True)
        col.operator("blockout.snap_ground", text="落到地面")
        col.operator("blockout.face_camera", text="正对相机")
        col.operator("blockout.fit_points", text="用点云拟合")

        sub = box.column(align=True)
        sub.label(text="细调：")
        sub.prop(props, "default_depth")
        sub.prop(props, "default_thickness")
        sub.prop(props, "ground_depth_ratio")

        # ── 动态：骨骼 ──
        box = layout.box()
        box.label(text="③ 动态骨骼", icon="ARMATURE_DATA")
        box.operator("blockout.import_joints", text="导入 kp3d.json",
                     icon="IMPORT")
        if props.joint_summary:
            box.label(text=props.joint_summary, icon="INFO")

        col = box.column(align=True)
        col.prop(props, "rig_height_m")
        col.prop(props, "playback_fps")
        row = col.row(align=True)
        row.prop(props, "smooth_median")
        row.prop(props, "smooth_mean")
        row = box.row(align=True)
        row.prop(props, "rig_with_head")
        row.prop(props, "rig_with_joints")
        box.prop(props, "joint_radius_m")
        box.operator("blockout.build_rig", text="生成骨骼",
                     icon="OUTLINER_OB_ARMATURE")
        if props.rig_summary:
            box.label(text=props.rig_summary, icon="CHECKMARK")

        col = box.column(align=True)
        col.prop(props, "camera_preset", text="")
        if props.camera_preset != "follow_ref":
            col.prop(props, "camera_wobble")
        col.operator("blockout.setup_camera", text="配运镜 + 环境",
                     icon="CAMERA_DATA")
        if props.camera_summary:
            box.label(text=props.camera_summary, icon="INFO")

        # ── 验收与输出 ──
        box = layout.box()
        box.label(text="④ 验收", icon="CHECKMARK")
        row = box.row(align=True)
        row.operator("blockout.verify_framing", text="检查取景")
        row.operator("blockout.snapshot", text="存对照图")
        if props.verify_summary:
            icon = "CHECKMARK" if props.verify_verdict == "pass" else "ERROR"
            box.label(text=props.verify_summary, icon=icon)

        box = layout.box()
        box.label(text="⑤ 出片", icon="RENDER_ANIMATION")
        box.prop(props, "output_dir")
        row = box.row(align=True)
        row.prop(props, "render_w")
        row.prop(props, "render_h")
        row = box.row(align=True)
        row.prop(props, "output_fps")
        row.prop(props, "encode_quality")
        box.prop(props, "render_force")
        box.operator("blockout.render_video", text="渲染并编码 MP4",
                     icon="RENDER_ANIMATION")
        if props.render_summary:
            box.label(text=props.render_summary, icon="INFO")
        box.operator("blockout.export_rig_glb", text="导出 GLB",
                     icon="EXPORT")

        # ── 收尾：对齐打分是老流程，留个入口，别把功能删了 ──
        sub = layout.column(align=True)
        sub.operator("blockout.score", text="算对齐分数")
        if props.last_report:
            for line in props.last_report.split("\n")[:6]:
                f = line.split("\t")
                if len(f) == 4:
                    try:
                        sub.label(text="%s  %.2f" % (f[0][:14], float(f[1])))
                    except ValueError:
                        pass


class VIEW3D_PT_blockout_view(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blockout"
    bl_label = "显示"
    bl_parent_id = "VIEW3D_PT_blockout_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        pc = bpy.data.objects.get("COLMAPPoints")
        if pc:
            layout.label(text="点云: %d 点" % len(pc.data.vertices))
            layout.prop(pc, "hide_viewport", text="隐藏点云")
        else:
            layout.label(text="未导入点云")


classes = (
    BlockoutProps,
    BO_OT_draw_box,
    BO_OT_import,
    BO_OT_load_ref,
    BO_OT_box_from_camera,
    BO_OT_box_ground,
    BO_OT_snap_ground,
    BO_OT_face_camera,
    BO_OT_fit_points,
    BO_OT_snapshot,
    BO_OT_score,
    VIEW3D_PT_blockout_main,
    VIEW3D_PT_blockout_view,
)


def _unreg_quietly(c):
    """注销一个类；已经没注册、或注册在别处，都不算错误。"""
    try:
        bpy.utils.unregister_class(c)
        return True
    except (RuntimeError, ValueError):
        return False


def register():
    # ⚠ 以前这里是裸的 register_class 循环。只要有一个类因为上次
    #   unregister 没清干净而残留，第一句就抛 ValueError，
    #   后面所有类都不注册 —— 插件处于**半注册**状态：
    #   面板能显示，但属性/算子缺一半，表现就是「UI 各种不对劲」。
    #   所以：先把自己清一遍，再逐个注册且互不影响。
    for c in reversed(_video_ops):
        _unreg_quietly(c)
    for c in reversed(classes):
        _unreg_quietly(c)

    # ── 桥接的偏好属性必须在 BlockoutAIPrefs 注册**之前**注入 ──
    #   Blender 只在 register_class 那一刻收集注解里的 bpy.props，
    #   注册完再 setattr 是不生效的（面板上那几行会画不出来）。
    if _bridge_ok and bridge_mod is not None:
        try:
            from .bridge.ui.preferences import inject_into
            if _ai_ok and ui_ai is not None:
                n = inject_into(ui_ai.BlockoutAIPrefs)
                if n:
                    print("[blockout_helper] 桥接偏好属性已并入 AddonPreferences: %d 个" % n)
        except Exception as e:                  # noqa: BLE001
            print("[blockout_helper] 桥接偏好注入失败: %s" % e)

    for c in classes:
        try:
            bpy.utils.register_class(c)
        except (RuntimeError, ValueError) as e:
            print("[blockout_helper] 注册 %s 失败: %s" % (c.__name__, e))
    for c in _video_ops:
        try:
            bpy.utils.register_class(c)
        except (RuntimeError, ValueError) as e:
            print("[blockout_helper] 注册 %s 失败: %s" % (c.__name__, e))

    # 时间轴变帧 → 刷新原片缩略图（面板播放器用）
    if _on_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_on_frame_change)

    if _ai_ok and ui_ai is not None:
        try:
            ui_ai.register()
        except Exception as e:                  # noqa: BLE001
            print("[blockout_helper] AI 面板注册失败: %s" % e)

    if not hasattr(bpy.types.Scene, "blockout"):
        bpy.types.Scene.blockout = bpy.props.PointerProperty(type=BlockoutProps)

    # ── 桥接本体（面板 / 算子 / socket 服务）──
    #   放最后：它注册失败不该影响上面的建模功能。
    if _bridge_ok and bridge_mod is not None:
        try:
            bridge_mod.register()
            print("[blockout_helper] DSH Blender Bridge 已随插件启动")
        except Exception as e:                  # noqa: BLE001
            print("[blockout_helper] 桥接注册失败: %s" % e)


def unregister():
    # 先停桥接：它持有 socket 线程，晚停会留下占着 9876 端口的孤儿
    if _bridge_ok and bridge_mod is not None:
        try:
            bridge_mod.unregister()
        except Exception as e:                  # noqa: BLE001
            print("[blockout_helper] 桥接注销失败: %s" % e)

    try:
        while _on_frame_change in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    except Exception:
        pass

    if _ai_ok and ui_ai is not None:
        try:
            ui_ai.unregister()
        except Exception:
            pass
    try:
        from . import dispatch as _dispatch
        _dispatch.shutdown()
    except Exception:
        pass

    if hasattr(bpy.types.Scene, "blockout"):
        del bpy.types.Scene.blockout

    for c in reversed(_video_ops):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass

    # Purge sub-modules so a reinstall actually loads new files.
    import sys as _sys
    prefix = __name__ + "."
    for name in list(_sys.modules):
        if name == __name__ or name.startswith(prefix):
            _sys.modules.pop(name, None)

    for k in ("_bo_arrays", "_bo_unit", "_bo_fps", "_bo_rig", "_bo_session"):
        bpy.app.driver_namespace.pop(k, None)


if __name__ == "__main__":
    register()
