"""build_zip.py — 打包插件 zip

用法：
    python build_zip.py
产物：
    data/blockout_helper.zip

额外写入 build_info.json（版本 + 构建时间），
装错版本时面板上一眼就能看出来。
"""
import json
import os
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).parent.parent
SRC = HERE / "addon" / "blockout_helper"
OUT = HERE / "data" / "blockout_helper.zip"

SKIP_DIRS = {"__pycache__", ".git", ".idea"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}
SKIP_EXT = {".pyc", ".pyo", ".blend1", ".zip"}
#: 备份/损坏残留文件：绝不能进包（曾把 60 KB 的 __init__.py.broken 打进 zip，
#: 安装后 Blender 会当普通文件留着，白白多一份旧代码，误导排查）
SKIP_SUFFIX = {".broken", ".bak", ".orig", ".rej", ".old", ".tmp"}


def read_version():
    """从 __init__.py 的 bl_info 读版本号（不 import，避免依赖 bpy）。"""
    src = (SRC / "__init__.py").read_text(encoding="utf-8")
    import re
    m = re.search(r'"version"\s*:\s*\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)', src)
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())


def main():
    if not SRC.is_dir():
        raise SystemExit("找不到插件目录：%s" % SRC)

    ver = read_version()
    ver_str = "%d.%d.%d" % ver
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # 写构建信息（面板会显示）
    info = {"version": ver_str, "built": stamp}
    (SRC / "build_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    n = 0
    total = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in sorted(files):
                if f in SKIP_FILES:
                    continue
                if Path(f).suffix in SKIP_EXT:
                    continue
                # 后缀可能是 .py.broken 这种双扩展名，用 endswith 判断
                if any(f.endswith(sfx) for sfx in SKIP_SUFFIX):
                    continue
                p = Path(root) / f
                arc = p.relative_to(SRC.parent)
                z.write(p, arc.as_posix())
                n += 1
                total += p.stat().st_size

    size = OUT.stat().st_size
    print("PACKED %d files | 源 %.1f KB -> zip %.1f KB"
          % (n, total / 1024, size / 1024))
    print("版本 %s   构建 %s" % (ver_str, stamp))
    print("zip:", OUT)

    # ── 自检 ──
    with zipfile.ZipFile(OUT) as z:
        names = z.namelist()
        assert "blockout_helper/__init__.py" in names, names[:5]
        init = z.read("blockout_helper/__init__.py").decode("utf-8")
        assert "bl_info" in init

        # 关键模块必须都在
        for need in ("ui_ai.py", "agent.py", "api_client.py",
                     "dispatch.py", "debuglog.py", "tools.py",
                     "dsh_client.py"):
            assert ("blockout_helper/" + need) in names, "缺 %s" % need

        # ui_ai 必须有 import os（曾因缺它报 NameError）
        ui = z.read("blockout_helper/ui_ai.py").decode("utf-8")
        assert "import os" in ui, "ui_ai.py 缺 import os"

        # build_info 要在
        assert "blockout_helper/build_info.json" in names, "缺 build_info.json"
        bi = json.loads(z.read("blockout_helper/build_info.json").decode("utf-8"))
        assert bi["version"] == ver_str, bi

        # ⚠ icon 白名单校验：Blender 4.5 里不存在的 icon 会让 draw()
        #   抛 TypeError 且**完全静默**（只画到出错那行，不报错不打印）。
        #   实测曾因为用了 MESSAGEBOX，导致回答框从来没被画出来过，
        #   排查了好几轮。这里用静态表挡住，不依赖 Blender 环境。
        BAD_ICONS_45 = {"MESSAGEBOX", "ARROW_RIGHT", "ARROW_LEFT",
                        "FILE_3D", "MESH_UVSPHERE",
                        "TRIP_UP", "TRIP_DOWN", "TRIA_RIGHT", "TRIA_UP"}
        bad = []
        for name in names:
            if not name.endswith(".py"):
                continue
            src = z.read(name).decode("utf-8", "replace")
            for i, line in enumerate(src.splitlines(), 1):
                code = line.split("#", 1)[0]
                for b in BAD_ICONS_45:
                    if '"%s"' % b in code or "'%s'" % b in code:
                        bad.append("%s:%d %s" % (name, i, b))
        assert not bad, "用了 Blender 4.5 不存在的 icon：%s" % bad

    print("SELFTEST ok：模块齐全 / import os 存在 / 版本 %s" % ver_str)


if __name__ == "__main__":
    main()
