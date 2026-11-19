"""tools.py —— 给 AI 用的场景工具注册表

设计原则
--------
1. **每个工具都只碰 bpy 的一小块**，并且全部通过 dispatch.call_on_main 执行
   （由 agent 层统一派发，工具本身写成普通函数即可）。
2. **参数 schema 严格**：模型乱传参数要能报错并纠正，而不是静默改变场景。
3. **破坏性操作默认要确认**：删除/清空/覆盖类工具标 destructive=True，
   由 agent 层决定是否需要用户点"同意"。
4. **返回值必须可序列化**（JSON），因为要作为 tool 消息发给模型。

工具清单
--------
只读:  get_scene_info / list_objects / get_object_info / get_blender_api
场景:  add_object / set_transform / assign_material / rename_objects
修改:  delete_objects / run_python
白膜:  import_joints / build_rig / setup_camera / verify_framing
      render_sequence / encode_mp4 / export_glb
建模:  build_blockout / add_primitive
"""

import json
import math

import bpy
from mathutils import Vector


# ═══════════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════════

REGISTRY = {}


def tool(name, description, parameters, destructive=False):
    """装饰器：注册一个工具。"""
    def deco(fn):
        REGISTRY[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "destructive": destructive,
            "fn": fn,
        }
        return fn
    return deco


def schemas(only=None):
    """导出给模型的 tool schema 列表。"""
    out = []
    for name, spec in REGISTRY.items():
        if only and name not in only:
            continue
        out.append({"name": name,
                    "description": spec["description"],
                    "parameters": spec["parameters"]})
    return out


def call(name, args):
    """执行工具。返回 JSON 可序列化的 dict。"""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"error": "未知工具 %r。可用：%s" % (name, ", ".join(sorted(REGISTRY)))}
    try:
        result = spec["fn"](**(args or {}))
    except TypeError as e:
        return {"error": "参数不对：%s" % e,
                "expected": spec["parameters"]}
    except Exception as e:                      # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}
    if result is None:
        return {"ok": True}
    return result


def is_destructive(name):
    spec = REGISTRY.get(name)
    return bool(spec and spec["destructive"])


# ═══════════════════════════════════════════════════════════════════
# 只读：场景查询
# ═══════════════════════════════════════════════════════════════════

@tool("get_scene_info",
      "读取当前场景概况：对象数、帧范围、渲染设置、单位。每次操作前先调它。",
      {"type": "object", "properties": {}, "required": []})
def get_scene_info():
    sc = bpy.context.scene
    counts = {}
    for o in bpy.data.objects:
        counts[o.type] = counts.get(o.type, 0) + 1
    return {
        "scene": sc.name,
        "objects_total": len(bpy.data.objects),
        "by_type": counts,
        "frame_range": [sc.frame_start, sc.frame_end],
        "current_frame": sc.frame_current,
        "fps": sc.render.fps,
        "engine": sc.render.engine,
        "resolution": [sc.render.resolution_x, sc.render.resolution_y],
        "unit_scale": getattr(sc.unit_settings, "scale_length", 1.0),
        "camera": sc.camera.name if sc.camera else None,
    }


@tool("list_objects",
      "列出对象名称（可过滤类型/名称关键字）。最多返回 300 个。",
      {"type": "object",
       "properties": {
           "type": {"type": "string", "description": "MESH / CAMERA / LIGHT 等，留空=全部"},
           "name_contains": {"type": "string", "description": "名称关键字"},
           "limit": {"type": "integer", "default": 300},
       },
       "required": []})
def list_objects(type="", name_contains="", limit=300):
    out = []
    for o in bpy.data.objects:
        if type and o.type != type.upper():
            continue
        if name_contains and name_contains.lower() not in o.name.lower():
            continue
        out.append(o.name)
        if len(out) >= int(limit):
            break
    return {"count": len(out), "names": out}


@tool("get_object_info",
      "读取单个对象的变换、尺寸、材质、修改器。",
      {"type": "object",
       "properties": {"name": {"type": "string"}},
       "required": ["name"]})
def get_object_info(name):
    o = bpy.data.objects.get(name)
    if o is None:
        return {"error": "没有对象 %r" % name}
    d = {
        "name": o.name,
        "type": o.type,
        "location": [round(v, 4) for v in o.location],
        "rotation_euler_deg": [round(math.degrees(v), 2) for v in o.rotation_euler],
        "scale": [round(v, 4) for v in o.scale],
        "dimensions": [round(v, 4) for v in o.dimensions],
        "parent": o.parent.name if o.parent else None,
        "collections": [c.name for c in o.users_collection],
        "materials": [m.name for m in o.data.materials] if hasattr(o.data, "materials") else [],
        "modifiers": [m.name for m in o.modifiers],
        "visible": not o.hide_viewport,
    }
    if o.type == "MESH":
        d["verts"] = len(o.data.vertices)
        d["faces"] = len(o.data.polygons)
    if o.animation_data and o.animation_data.action:
        d["has_animation"] = True
    return d


@tool("get_blender_api",
      "查 Blender Python API 的对象有哪些属性/方法。用于确认 API 用法，不要凭记忆写。",
      {"type": "object",
       "properties": {
           "type_name": {"type": "string",
                         "description": "如 Object / Mesh / Scene / Camera"},
           "filter": {"type": "string", "description": "属性名关键字过滤"},
           "limit": {"type": "integer", "default": 80},
       },
       "required": ["type_name"]})
def get_blender_api(type_name, filter="", limit=80):
    cls = getattr(bpy.types, type_name, None)
    if cls is None:
        return {"error": "bpy.types 里没有 %r" % type_name}
    names = [n for n in dir(cls) if not n.startswith("__")]
    if filter:
        f = filter.lower()
        names = [n for n in names if f in n.lower()]
    return {"type": type_name, "count": len(names), "names": names[:int(limit)]}


# ═══════════════════════════════════════════════════════════════════
# 场景修改
# ═══════════════════════════════════════════════════════════════════

@tool("add_primitive",
      "加一个基本体。尺寸是米。location 是物体中心。",
      {"type": "object",
       "properties": {
           "kind": {"type": "string", "enum": ["cube", "sphere", "cylinder", "plane", "cone"]},
           "name": {"type": "string"},
           "size": {"type": "number", "description": "立方体棱长 / 球半径 / 圆柱半径，米"},
           "depth": {"type": "number", "description": "圆柱/锥高度，米"},
           "location": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
       },
       "required": ["kind"]})
def add_primitive(kind, name="", size=1.0, depth=1.0, location=(0, 0, 0)):
    fn = {"cube": bpy.ops.mesh.primitive_cube_add,
          "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
          "cylinder": bpy.ops.mesh.primitive_cylinder_add,
          "plane": bpy.ops.mesh.primitive_plane_add,
          "cone": bpy.ops.mesh.primitive_cone_add}.get(kind)
    if fn is None:
        return {"error": "不支持的 kind=%r" % kind}
    kw = {"location": tuple(location)}
    if kind == "cube":
        kw["size"] = float(size) * 2.0        # primitive_cube_add 的 size 是棱长
    elif kind == "sphere":
        kw["radius"] = float(size)
    elif kind in ("cylinder", "cone"):
        kw["radius"] = float(size)
        kw["depth"] = float(depth)
    elif kind == "plane":
        kw["size"] = float(size)
    fn(**kw)
    o = bpy.context.object
    if name:
        o.name = name
    return {"ok": True, "name": o.name,
            "dimensions": [round(v, 4) for v in o.dimensions]}


@tool("set_transform",
      "设置对象的位移/旋转/缩放。rotation 用**度**。",
      {"type": "object",
       "properties": {
           "name": {"type": "string"},
           "location": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
           "rotation_deg": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
           "scale": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
       },
       "required": ["name"]})
def set_transform(name, location=None, rotation_deg=None, scale=None):
    o = bpy.data.objects.get(name)
    if o is None:
        return {"error": "没有对象 %r" % name}
    if location is not None:
        o.location = Vector(location)
    if rotation_deg is not None:
        o.rotation_euler = [math.radians(v) for v in rotation_deg]
    if scale is not None:
        o.scale = Vector(scale)
    bpy.context.view_layer.update()
    return {"ok": True, "name": o.name,
            "location": [round(v, 4) for v in o.location],
            "dimensions": [round(v, 4) for v in o.dimensions]}


@tool("assign_material",
      "给对象一个纯色材质（RGB 0..1）。已有同名材质会复用。",
      {"type": "object",
       "properties": {
           "name": {"type": "string"},
           "rgb": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
           "roughness": {"type": "number", "default": 0.6},
           "metallic": {"type": "number", "default": 0.0},
           "material_name": {"type": "string"},
       },
       "required": ["name", "rgb"]})
def assign_material(name, rgb, roughness=0.6, metallic=0.0, material_name=""):
    from . import kit
    o = bpy.data.objects.get(name)
    if o is None:
        return {"error": "没有对象 %r" % name}
    if not hasattr(o.data, "materials"):
        return {"error": "%s 没有材质槽" % o.name}
    mname = material_name or ("M_%s" % o.name)
    m = kit.solid_material(mname, tuple(rgb), roughness, metallic)
    o.data.materials.clear()
    o.data.materials.append(m)
    return {"ok": True, "object": o.name, "material": m.name}


@tool("rename_objects",
      "批量改名。加前缀/后缀，或按类型统一命名。",
      {"type": "object",
       "properties": {
           "prefix": {"type": "string"},
           "suffix": {"type": "string"},
           "only_type": {"type": "string", "description": "只改这个类型，留空=全部 MESH"},
       },
       "required": []})
def rename_objects(prefix="", suffix="", only_type="MESH"):
    t = (only_type or "").upper()
    n = 0
    for o in bpy.data.objects:
        if t and o.type != t:
            continue
        base = o.name
        new = (prefix or "") + base + (suffix or "")
        if new != base:
            o.name = new
            n += 1
    return {"ok": True, "renamed": n}


@tool("delete_objects",
      "删除对象。name_contains 为空时**必须**给 names，防止误删全场景。",
      {"type": "object",
       "properties": {
           "names": {"type": "array", "items": {"type": "string"}},
           "name_contains": {"type": "string"},
       },
       "required": []}, destructive=True)
def delete_objects(names=None, name_contains=""):
    targets = []
    if names:
        targets = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
    elif name_contains:
        targets = [o for o in bpy.data.objects if name_contains.lower() in o.name.lower()]
    else:
        return {"error": "拒绝执行：必须给 names 或 name_contains"}
    killed = [o.name for o in targets]
    for o in targets:
        bpy.data.objects.remove(o, do_unlink=True)
    return {"ok": True, "deleted": len(killed), "names": killed[:50]}


@tool("run_python",
      "在 Blender 里执行 Python 代码片段。用于工具没覆盖的操作。"
      "代码里可用 bpy / math / mathutils。返回值放 result 变量会带回。",
      {"type": "object",
       "properties": {"code": {"type": "string"}},
       "required": ["code"]}, destructive=True)
def run_python(code):
    import mathutils
    ns = {"bpy": bpy, "math": math, "mathutils": mathutils,
          "Vector": mathutils.Vector, "Matrix": mathutils.Matrix}
    try:
        exec(compile(code, "<ai>", "exec"), ns)     # noqa: S102 - 这是工具本身的功能
    except Exception as e:                          # noqa: BLE001
        import traceback
        return {"error": "%s: %s" % (type(e).__name__, e),
                "traceback": traceback.format_exc()[-1500:]}
    out = ns.get("result")
    try:
        json.dumps(out)
    except (TypeError, ValueError):
        out = repr(out)
    return {"ok": True, "result": out}


# ═══════════════════════════════════════════════════════════════════
# 视频转白膜链路
# ═══════════════════════════════════════════════════════════════════

@tool("import_joints",
      "导入 3D 关节轨迹 kp3d.json，缓存到场景。之后才能 build_rig。",
      {"type": "object",
       "properties": {"path": {"type": "string"}},
       "required": ["path"]})
def import_joints(path):
    from . import skeleton, scene as scene_mod
    frames, unit, fps, names = skeleton.load_track(path)
    arrays = skeleton.joints_to_arrays(frames, unit, names)
    bpy.app.driver_namespace["_bo_arrays"] = arrays
    bpy.app.driver_namespace["_bo_unit"] = unit
    bpy.app.driver_namespace["_bo_fps"] = fps
    sc = bpy.context.scene
    if fps:
        scene_mod.set_fps(sc, fps)
    sc.frame_start, sc.frame_end = 1, max(2, len(frames))
    return {"ok": True, "frames": len(frames), "unit": unit,
            "joints": sorted(k for k, v in arrays.items()
                             if any(p is not None for p in v))}


@tool("build_rig",
      "关节轨迹 → 骨骼白模。可指定身高和播放帧率。",
      {"type": "object",
       "properties": {
           "height_m": {"type": "number", "default": 1.70},
           "fps": {"type": "integer", "default": 25},
           "with_head": {"type": "boolean", "default": True},
           "with_joints": {"type": "boolean", "default": True},
       },
       "required": []})
def build_rig(height_m=1.70, fps=25, with_head=True, with_joints=True):
    from . import skeleton, scene as scene_mod
    arrays = bpy.app.driver_namespace.get("_bo_arrays")
    if not arrays:
        return {"error": "先调 import_joints"}
    sm = skeleton.smooth_arrays(arrays, 5, 3)
    zoff = skeleton.ground_offset(sm)
    for seq in sm.values():
        for p in seq:
            if p is not None:
                p.z += zoff
    spec = skeleton.RigSpec(height_m=height_m, collection="Blockout")
    objs = skeleton.build_rig(sm, spec, with_head=with_head,
                              with_joints=with_joints)
    bpy.app.driver_namespace["_bo_rig"] = objs
    sc = bpy.context.scene
    scene_mod.set_fps(sc, fps)
    sc.frame_start = 1
    rep = skeleton.rig_report(objs)
    if rep.get("last_keyframe"):
        sc.frame_end = max(2, int(rep["last_keyframe"]))
    return {"ok": True, "objects": rep["objects"], "bones": rep["bones"],
            "frames": sc.frame_end, "fps": sc.render.fps,
            "seconds": round(sc.frame_end / float(sc.render.fps), 2)}


@tool("setup_camera",
      "按预设配运镜。预设：cinematic / turntable / dolly_in / top_down。",
      {"type": "object",
       "properties": {
           "preset": {"type": "string", "default": "cinematic"},
           "wobble": {"type": "boolean", "default": True},
       },
       "required": []})
def setup_camera(preset="cinematic", wobble=True):
    from . import camera as cam_mod, scene as scene_mod
    objs = bpy.app.driver_namespace.get("_bo_rig")
    if not objs:
        return {"error": "先调 build_rig"}
    live = [o for o in objs.values() if o and o.name in bpy.data.objects]
    if not live:
        return {"error": "骨骼对象已不在场景里"}
    sc = bpy.context.scene
    info = cam_mod.setup_camera(sc, live, preset=preset, wobble=wobble)
    c = info["scene_center"]
    scene_mod.setup_world(sc, center=(c[0], c[1]),
                          floor_size=max(6.0, info["hmax"] * 4.0))
    return {"ok": True, "preset": info["preset"], "frames": info["frames"],
            "radius": info.get("radius_common")}


@tool("verify_framing",
      "取景自检：worst NDC ≤ 1.0 才算不切边。渲染前必查。",
      {"type": "object", "properties": {}, "required": []})
def verify_framing():
    from . import camera as cam_mod
    objs = bpy.app.driver_namespace.get("_bo_rig")
    sc = bpy.context.scene
    if not objs or sc.camera is None:
        return {"error": "先调 setup_camera"}
    live = [o for o in objs.values() if o and o.name in bpy.data.objects]
    r = cam_mod.verify_framing(sc, sc.camera, live)
    return {"verdict": r["verdict"], "worst_ndc": r["worst_ndc"],
            "behind_camera": r["behind_camera"]}


@tool("render_sequence",
      "渲染 PNG 序列（存在即跳过，可续跑）。",
      {"type": "object",
       "properties": {
           "outdir": {"type": "string"},
           "start": {"type": "integer"},
           "end": {"type": "integer"},
           "force": {"type": "boolean", "default": False},
           "width": {"type": "integer", "default": 1280},
           "height": {"type": "integer", "default": 720},
       },
       "required": ["outdir"]})
def render_sequence(outdir, start=None, end=None, force=False,
                    width=1280, height=720):
    from . import scene as scene_mod
    sc = bpy.context.scene
    scene_mod.setup_workbench(sc, res=(width, height), shadows=True)
    rng = (start, end) if (start and end) else None
    r = scene_mod.render_sequence(sc, outdir, frame_range=rng, force=force)
    return {"ok": True, "rendered": r["rendered"], "skipped": r["skipped"],
            "dir": r["dir"], "range": r["range"]}


@tool("encode_mp4",
      "把 PNG 序列编码成 MP4（Blender 内置 FFMPEG，不需要系统装 ffmpeg）。",
      {"type": "object",
       "properties": {
           "frames_dir": {"type": "string"},
           "out_path": {"type": "string"},
           "fps": {"type": "integer", "default": 25},
       },
       "required": ["frames_dir", "out_path"]})
def encode_mp4(frames_dir, out_path, fps=25):
    from . import scene as scene_mod
    sc = bpy.context.scene
    r = scene_mod.encode_mp4(sc, frames_dir, out_path, fps=fps)
    return {"ok": r["ok"], "path": r["path"], "bytes": r["bytes"],
            "frames": r["frames"]}


@tool("export_glb",
      "导出 GLB。不给 objects 就导出全部 MESH。",
      {"type": "object",
       "properties": {
           "out_path": {"type": "string"},
           "objects": {"type": "array", "items": {"type": "string"}},
       },
       "required": ["out_path"]})
def export_glb(out_path, objects=None):
    prev = list(bpy.context.selected_objects)
    bpy.ops.object.select_all(action="DESELECT")
    if objects:
        for n in objects:
            o = bpy.data.objects.get(n)
            if o:
                o.select_set(True)
    else:
        for o in bpy.data.objects:
            if o.type == "MESH":
                o.select_set(True)
    try:
        bpy.ops.export_scene.gltf(filepath=out_path, export_format="GLB",
                                  use_selection=True, export_animations=True)
    finally:
        for o in prev:
            if o.name in bpy.data.objects:
                o.select_set(True)
    import os
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    return {"ok": size > 0, "path": out_path, "bytes": size}


@tool("build_blockout",
      "声明式白模：给一组盒子（毫米制，at_mm 是**中心**），生成低模 + GLB。",
      {"type": "object",
       "properties": {
           "name": {"type": "string"},
           "parts": {"type": "array", "items": {
               "type": "object",
               "properties": {
                   "id": {"type": "string"},
                   "size_mm": {"type": "array", "items": {"type": "number"}},
                   "at_mm": {"type": "array", "items": {"type": "number"}},
               },
               "required": ["id", "size_mm", "at_mm"]}},
           "glb_path": {"type": "string"},
       },
       "required": ["name", "parts"]})
def build_blockout(name, parts, glb_path=""):
    from . import kit
    col = kit.ensure_collection("Blockout")
    made = []
    for p in parts:
        o = kit.add_box_mm(col, "BO-%s-%s" % (name, p["id"]),
                           p["at_mm"], p["size_mm"],
                           kit.solid_material("BO_White", (0.62, 0.65, 0.70)))
        made.append(o.name)
    out = {"ok": True, "objects": made}
    if glb_path:
        out["glb"] = export_glb(glb_path, made)
    return out
