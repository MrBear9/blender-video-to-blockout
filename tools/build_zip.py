"""build_zip.py — 打包插件 zip

用法：
    python build_zip.py                 # 发布版：不含 bridge（纯 MIT）
    python build_zip.py --with-bridge   # 本地版：把 bridge 一起打进去
    python build_zip.py --out xxx.zip   # 指定输出路径

产物：
    data/blockout_helper.zip            （发布版）
    data/blockout_helper_with_bridge.zip（本地版）

为什么要分两个
--------------
`addon/blockout_helper/bridge/` 是第三方代码，许可证为 **GPL-3.0-or-later**；
本仓库是 MIT，GPL 有传染性。所以：

  * **发布版**不含 bridge —— 仓库可以按 MIT 公开分发。用户若要 agent 通道，
    自己装上游的 `dsh_blender_bridge.zip`（面板里会提示）。
  * **本地版**含 bridge —— 自己用，一个插件搞定，侧边栏两个 tab 都在。

额外写入 build_info.json（版本 + 构建时间），装错版本时面板上一眼就能看出来。
"""
import argparse
import json
import os
import sys
import time
import zipfile
from pathlib import Path

# ⚠ Windows 控制台默认 GBK，打印 ✓ / ⚠ 这类字符会抛 UnicodeEncodeError，
#   而且是在**打包成功之后**才炸 —— 让人以为打包失败了。
#   必须在任何输出之前把 stdout 切成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

HERE = Path(__file__).parent.parent
SRC = HERE / "addon" / "blockout_helper"
BRIDGE = SRC / "bridge"
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


def _bridge_file_count():
    if not BRIDGE.is_dir():
        return 0
    n = 0
    for root, dirs, files in os.walk(BRIDGE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        n += sum(1 for f in files if f.endswith(".py"))
    return n


def main():
    ap = argparse.ArgumentParser(description="打包 blockout_helper 插件")
    ap.add_argument("--with-bridge", action="store_true",
                    help="把第三方 bridge 一起打进包（本地版，含 GPL 代码）")
    ap.add_argument("--out", default="", help="输出路径（默认按模式自动定）")
    args = ap.parse_args()

    if not SRC.is_dir():
        raise SystemExit("找不到插件目录：%s" % SRC)

    include_bridge = args.with_bridge
    if include_bridge and not BRIDGE.is_dir():
        raise SystemExit(
            "要打本地版但 bridge/ 不存在。先跑：\n"
            "    python tools/fetch_bridge.py")

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = HERE / out
    else:
        out = (HERE / "data" / ("blockout_helper_with_bridge.zip"
                                if include_bridge else "blockout_helper.zip"))

    ver = read_version()
    ver_str = "%d.%d.%d" % ver
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # 写构建信息（面板会显示）。带上 bridge 标记，装错版本时看得出来。
    info = {
        "version": ver_str,
        "built": stamp,
        "bridge": bool(include_bridge),
    }
    (SRC / "build_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    n = 0
    total = 0
    skipped_bridge = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
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
                # 发布版：整个 bridge/ 排除在外（GPL 代码不进 MIT 包）
                if not include_bridge and BRIDGE in p.parents:
                    skipped_bridge += 1
                    continue
                arc = p.relative_to(SRC.parent)
                z.write(p, arc.as_posix())
                n += 1
                total += p.stat().st_size

    size = out.stat().st_size
    kind = "本地版(含 bridge)" if include_bridge else "发布版(纯 MIT)"
    print("PACKED %d files | 源 %.1f KB -> zip %.1f KB"
          % (n, total / 1024, size / 1024))
    print("版本 %s   构建 %s   %s" % (ver_str, stamp, kind))
    if skipped_bridge:
        print("已排除 bridge/：%d 个文件（GPL，不进 MIT 包）" % skipped_bridge)
    print("zip:", out)


    # ── 自检 ──
    with zipfile.ZipFile(out) as z:
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

        # ⚠ 模式自检：两种包的内容必须真的不同。
        #   发布版里混进 GPL 代码是许可证事故；本地版漏了 bridge 则是白干。
        has_bridge = any(n.startswith("blockout_helper/bridge/") for n in names)
        if include_bridge:
            assert has_bridge, "本地版却没有 bridge/ —— 打包漏了"
            # GPL 要求随分发保留许可全文
            assert "blockout_helper/bridge/LICENSE" in names, \
                "本地版缺 bridge/LICENSE（GPL 要求保留许可全文）"
            assert (BRIDGE / "compat.py").is_file(), "bridge 适配文件丢了"
            print("模式自检：本地版含 bridge + LICENSE ✓")
        else:
            assert not has_bridge, "发布版混进了 bridge/（GPL 代码）—— 许可证冲突"
            print("模式自检：发布版不含 bridge ✓")

        # ⚠ icon 白名单校验：Blender 4.5 里不存在的 icon 会让 draw()
        #   抛 TypeError 且**完全静默**（只画到出错那行，不报错不打印）。
        #   实测曾因为用了 MESSAGEBOX，导致回答框从来没被画出来过，
        #   排查了好几轮。这里用静态表挡住，不依赖 Blender 环境。
        #
        # ⚠ 这张表必须和真实 Blender 对得上。之前凭印象把 TRIA_RIGHT /
        #   TRIA_UP / FILE_3D / MESH_UVSPHERE 也列成"不存在"，结果桥接面板
        #   用了合法的 TRIA_RIGHT 就被拦住打包 —— 冤枉代码，还挡了发布。
        #   下面这 5 个是在 Blender 4.5 里逐个查过确实没有的。
        BAD_ICONS_45 = {"MESSAGEBOX", "ARROW_RIGHT", "ARROW_LEFT",
                        "TRIP_UP", "TRIP_DOWN"}
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
