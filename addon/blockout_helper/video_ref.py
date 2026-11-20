"""
video_ref.py —— 原片参考：按帧取画面，给面板播放器和视口叠加用

为什么不能直接 `Image.pixels`
-----------------------------
`bpy.data.images.load(mp4)` 读到的**永远是第一帧**。改 `image_user.frame_current`
不会重新解码，`image.reload()` 也只是重读同一帧（实测三种方法取到的像素完全一样）。

`MovieClip` 能拿到帧号，但没有把某一帧变成 `Image` 的接口。

可行路径只有一条：**VSE 把视频挂成 strip，逐帧渲染成 PNG**。
实测第 1/200/600 帧渲染出的图完全不同（灰度均值 0.273 / 0.339 / 0.254），
说明这条路是真按帧取的。

代价与对策
----------
一次渲染 ~330ms（480x270）。拖动时间轴会卡，所以：
  * 结果按帧号**落盘缓存**，同一帧只渲染一次
  * 播放器用低分辨率 + 跳帧采样，不追求流畅，追求"能对着看"
"""

import os
import tempfile

import bpy

#: 缓存目录。放 temp，不进工程目录、不进 git。
CACHE_ROOT = os.path.join(tempfile.gettempdir(), "bo_video_ref")

#: 面板播放器用的缩略图尺寸。太小看不清细节，太大渲染慢。
THUMB = (384, 216)


def _strips(scene):
    se = scene.sequence_editor
    if se is None:
        return None, None
    # Blender 4.4+ 叫 strips，之前叫 sequences
    return se, (getattr(se, "strips", None) or getattr(se, "sequences", None))


def ensure_strip(scene, path):
    """把视频挂进 VSE（只挂一条，重复调用复用）。

    返回 dict：{strip, frames, size, path}
    """
    se, strips = _strips(scene)
    if se is None:
        se = scene.sequence_editor_create()
        strips = getattr(se, "strips", None) or getattr(se, "sequences")

    want = os.path.abspath(path)
    for s in list(strips):
        fp = bpy.path.abspath(getattr(s, "filepath", "") or "")
        if fp and os.path.abspath(fp) == want:
            return {
                "strip": s,
                "frames": int(s.frame_final_duration),
                "size": (int(s.elements[0].orig_width), int(s.elements[0].orig_height)),
                "path": want,
            }

    # 换片：清掉旧的参考条，别叠加
    for s in list(strips):
        if getattr(s, "name", "").startswith("BO_REF"):
            strips.remove(s)

    s = strips.new_movie(name="BO_REF", filepath=want, channel=1, frame_start=1)
    # ⚠ 挂了 strip 之后，只要 use_sequencer 是开的，正常渲染就会渲**原片**
    #   而不是场景 —— 出片会得到一段"原视频"而不是白模。
    #   这里关掉，只在 grab() 内部临时打开。
    scene.render.use_sequencer = False
    return {
        "strip": s,
        "frames": int(s.frame_final_duration),
        "size": (int(s.elements[0].orig_width), int(s.elements[0].orig_height)),
        "path": want,
    }


def frame_path(path, frame, size=THUMB):
    """某一帧的缓存文件路径。文件名带源片 hash + 尺寸，换片不会串。"""
    import hashlib

    key = hashlib.md5(
        ("%s|%dx%d" % (os.path.abspath(path), size[0], size[1])).encode("utf-8")
    ).hexdigest()[:12]
    d = os.path.join(CACHE_ROOT, key)
    return os.path.join(d, "f_%05d.png" % int(frame))


def grab(scene, path, frame, size=THUMB, force=False):
    """取第 frame 帧，返回 PNG 路径。有缓存就直接给。

    ⚠ 会临时改 scene 的帧范围 / 渲染设置，取完必须还原 ——
    否则外面正在做的取景、渲染全被带偏。调用方负责在主线程调用。

    ⚠⚠ 内部要 `scene.frame_set()`，这会再触发一次 frame_change_post，
    而我们的回调正是调这个函数的 —— 不拦就是无限递归（实测
    "maximum recursion depth exceeded"）。所以用一个重入标记挡住。
    """
    if _IN_GRAB:
        return frame_path(path, frame, size)
    fp = frame_path(path, frame, size)
    if os.path.exists(fp) and not force:
        return fp
    os.makedirs(os.path.dirname(fp), exist_ok=True)

    _IN_GRAB.append(1)
    try:
        return _grab_impl(scene, path, frame, size, fp)
    finally:
        _IN_GRAB.pop()


#: 重入保护。frame_set 会回调 frame_change_post，进而又走到 grab()。
_IN_GRAB = []


def _grab_impl(scene, path, frame, size, fp):
    info = ensure_strip(scene, path)
    strip = info["strip"]
    n = info["frames"]
    f = max(int(scene.frame_start), min(int(frame), int(scene.frame_start) + n - 1))

    keep = {
        "frame": scene.frame_current,
        "res_x": scene.render.resolution_x,
        "res_y": scene.render.resolution_y,
        "res_pct": scene.render.resolution_percentage,
        "fmt": scene.render.image_settings.file_format,
        "use_seq": scene.render.use_sequencer,
        "filepath": scene.render.filepath,
    }
    try:
        scene.render.resolution_x, scene.render.resolution_y = size
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.use_sequencer = True
        # strip 的起始帧可能不是 scene.frame_start，对齐一下
        strip.frame_start = scene.frame_start
        scene.frame_set(f)
        scene.render.filepath = fp
        bpy.ops.render.render(write_still=True)
    finally:
        scene.render.resolution_x = keep["res_x"]
        scene.render.resolution_y = keep["res_y"]
        scene.render.resolution_percentage = keep["res_pct"]
        scene.render.image_settings.file_format = keep["fmt"]
        # ⚠ 这里**不能**恢复成 keep["use_seq"]：取帧时打开的序列开关如果被还原成
        #   True，之后的正常渲染就会渲出原片而不是白模。参考序列只服务于
        #   面板缩略图，正常态永远是关。
        scene.render.use_sequencer = False
        scene.render.filepath = keep["filepath"]
        # 帧号也要还回去，否则取完帧时间轴自己跳了
        if scene.frame_current != keep["frame"]:
            scene.frame_set(keep["frame"])

    return fp if os.path.exists(fp) else ""


def load_image(path):
    """取得一个可给 UILayout.template_image 用的 Image（复用，不重复加载）。"""
    if not path or not os.path.exists(path):
        return None
    img = bpy.data.images.get(path)
    if img is None:
        try:
            img = bpy.data.images.load(path, check_existing=True)
        except Exception:
            return None
        img.name = path
    return img


def clear_cache(path=None, size=THUMB):
    """清缓存。给 path 只清那一个源片的。"""
    import shutil

    if path:
        d = os.path.dirname(frame_path(path, 0, size))
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        return d
    if os.path.isdir(CACHE_ROOT):
        shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    return CACHE_ROOT
