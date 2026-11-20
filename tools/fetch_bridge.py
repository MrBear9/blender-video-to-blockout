"""fetch_bridge.py —— 把上游的 DSH Blender Bridge 拉进 addon/blockout_helper/bridge/

为什么需要这一步
----------------
`addon/blockout_helper/bridge/` **不入版本控制**：上游那份 Blender 插件包是
GPL-3.0-or-later，而本仓库是 MIT，GPL 有传染性 —— 把它提交进仓库会让整个
`blockout_helper` 的分发受 GPL 约束。

所以仓库里只留"合并所需的适配代码"，第三方代码按需拉取。

用法
----
    python tools/fetch_bridge.py                # 拉到 addon/blockout_helper/bridge/
    python tools/fetch_bridge.py --check        # 只检查在不在，不下载
    python tools/fetch_bridge.py --remove       # 删掉已拉取的副本

拉完还要装插件（build_zip.py 会自动打包它）。

许可证
------
拉下来的内容是 GPL-3.0-or-later，版权归 sixtyseven67 及其上游
（Siddharth Ahuja / blender-mcp，MIT）所有，详见拉取后目录里的
LICENSE 与 NOTICE。本仓库不对其声称版权。
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEST = os.path.join(ROOT, "addon", "blockout_helper", "bridge")

#: 上游仓库与插件在仓库里的子目录。
#:
#: ⚠ 位置**随版本变过**：v3.0.x 在 `addon/dsh_blender_bridge`，
#:   更新的分支挪到了 `extension/dsh_blender_bridge`。所以不写死，
#:   按候选顺序探测（见 SUBDIR_CANDIDATES），都没有再全仓搜 manifest。
UPSTREAM = "https://github.com/sixtysevenlf/dsh-blender-plugin.git"
SUBDIR_CANDIDATES = (
    "addon/dsh_blender_bridge",
    "extension/dsh_blender_bridge",
)

#: 默认拉哪个版本。
#:
#: ⚠ **不要用 main**。上游 main 是开发中的分支，实测拉下来是半成品：
#:   某个算子类被删了，但 registration.py 还在 import 它、preferences 还在
#:   调它的 operator —— 导入直接失败，整个桥接不加载（而宿主做了容错，
#:   于是静默退化成"没有 agent 通道"，很难发现）。
#:   这里钉一个正式 tag。要跟着上游升级，改这里的版本号并重跑 --force。
DEFAULT_REF = "v3.0.6"

#: 合并所需的适配代码。拉取时**不覆盖**这些文件 —— 它们是本仓库改过的。
#: 格式：相对 DEST 的路径 -> 说明
PATCHED = {
    "compat.py": "ADDON_MODULE 改成取顶层包名",
    "registration.py": "跳过 AddonPreferences 的注册/注销",
    "ui/preferences.py": "偏好属性改为注入宿主类",
}


def log(msg):
    print(msg)


def git(*args, **kw):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True, **kw)


def _selfcheck_imports():
    """拉完就地验证：registration.py 里 import 的名字，真能找到。

    ⚠ 这一步不是多余的。上游某些版本是半成品——某个类被删了，但
    registration.py 还在 `from .ui.operators import DSHBB_OT_xxx`。
    这种错误在 Blender 里表现为**整个桥接静默不加载**（宿主做了容错，
    建模功能照常），用户只会觉得"agent 通道莫名没了"，极难排查。
    所以在这里用纯 Python 静态检查拦下来。
    """
    import ast
    import re

    reg = os.path.join(DEST, "registration.py")
    if not os.path.exists(reg):
        return ["registration.py 不存在"]

    with io.open(reg, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), reg)

    # 收集本包各模块实际定义的顶层名字。
    # ⚠ 要处理**包**（目录）：`from .ui.panels import X` 里 ui/panels 是个包，
    #   X 定义在它的某个子模块里。只扫 ui/panels.py 会漏，误报"没有 X"。
    defined = {}
    for root, dirs, files in os.walk(DEST):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            if p == reg:
                continue
            rel = os.path.relpath(p, DEST)[:-3].replace(os.sep, ".")
            if rel.endswith(".__init__"):
                rel = rel[: -len(".__init__")]
            try:
                with io.open(p, "r", encoding="utf-8") as f:
                    t = ast.parse(f.read(), p)
            except SyntaxError as e:
                return ["%s 语法错误: %s" % (rel, e)]
            names = set()
            for node in ast.walk(t):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            names.add(tgt.id)
                elif isinstance(node, ast.AnnAssign) and \
                        isinstance(node.target, ast.Name):
                    names.add(node.target.id)
            defined.setdefault(rel, set()).update(names)
            # 包的子模块也要并入包名下
            parts = rel.split(".")
            for i in range(1, len(parts)):
                parent = ".".join(parts[:i])
                defined.setdefault(parent, set()).update(names)

    # 检查 registration.py 里所有 `from .xxx import a, b, c`
    problems = []
    with io.open(reg, "r", encoding="utf-8") as f:
        src = f.read()
    for m in re.finditer(r"^from (\.[\w.]*) import \(?([^)\n]+)\)?", src, re.M):
        mod_rel = m.group(1).lstrip(".")
        mod_rel = re.sub(r"^\.", "", mod_rel)
        if not mod_rel:
            continue
        want = [w.strip().rstrip(",") for w in m.group(2).split(",")
                if w.strip() and not w.strip().startswith("(")]
        have = defined.get(mod_rel)
        if have is None:
            # 可能是包或别名，交给 Blender 自己判断，不误报
            continue
        for name in want:
            name = name.split(" as ")[0].strip()
            if name and name not in have:
                problems.append("%s 里没有 %s（registration.py 还在 import）"
                                % (mod_rel, name))
    return problems


def check():
    if not os.path.isdir(DEST):
        log("bridge/ 不存在")
        return False
    n = 0
    for root, dirs, files in os.walk(DEST):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        n += sum(1 for f in files if f.endswith(".py"))
    log("bridge/ 已存在：%d 个 .py" % n)
    missing = [p for p in PATCHED if not os.path.exists(os.path.join(DEST, p))]
    if missing:
        log("⚠ 缺少适配文件（说明不是本仓库适配过的版本）：%s" % missing)
        return False
    if not os.path.exists(os.path.join(DEST, "LICENSE")):
        log("⚠ 缺 LICENSE —— GPL 要求随分发保留许可全文，请补上")
    return True


def remove():
    if not os.path.isdir(DEST):
        log("bridge/ 本来就不存在")
        return 0
    # 保险：确认要删的是那个目录，别是算错了路径
    if os.path.basename(DEST) != "bridge" or not DEST.endswith(
            os.path.join("blockout_helper", "bridge")):
        raise SystemExit("拒绝删除：路径看起来不对 -> %s" % DEST)
    shutil.rmtree(DEST)
    log("已删除 %s" % DEST)
    log("注意：插件现在只提供建模面板，agent 通道需要另外安装上游 zip。")
    return 0


def find_subdir(tmp):
    """在上游克隆里定位插件目录。返回 (目录绝对路径, 相对路径) 或 (None, None)。"""
    for rel in SUBDIR_CANDIDATES:
        p = os.path.join(tmp, *rel.split("/"))
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "__init__.py")):
            return p, rel
    # 兜底：全仓找带 blender_manifest.toml 的目录
    for root, dirs, files in os.walk(tmp):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        if "blender_manifest.toml" in files and "__init__.py" in files:
            return root, os.path.relpath(root, tmp).replace(os.sep, "/")
    return None, None


def fetch(ref=DEFAULT_REF):
    tmp = tempfile.mkdtemp(prefix="dsbbb_")
    try:
        log("克隆上游 %s（浅克隆）…" % ref)
        r = git("clone", "--depth", "1", "--branch", ref, UPSTREAM, tmp)
        if r.returncode != 0:
            log("  拉取 %s 失败，可能是标记不存在" % ref)
            log(r.stderr[-600:])
            return 1

        src, src_rel = find_subdir(tmp)
        if not src:
            log("上游里没找到 Blender 插件目录 —— 这个版本可能不含插件")
            log("  试过的位置：%s" % ", ".join(SUBDIR_CANDIDATES))
            log("  （v3.0.6 在 addon/ 下；更新的版本挪到了 extension/ 下）")
            return 1
        log("定位到插件目录：%s" % src_rel)

        os.makedirs(DEST, exist_ok=True)

        # ⚠ 先备份本仓库的适配文件，否则会被上游原版覆盖掉，
        #   表现是 merged 后两个 AddonPreferences 打架、偏好全读不到。
        stash = {}
        for rel in PATCHED:
            p = os.path.join(DEST, *rel.split("/"))
            if os.path.exists(p):
                with io.open(p, "r", encoding="utf-8") as f:
                    stash[rel] = f.read()

        n = 0
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                sp = os.path.join(root, f)
                rel = os.path.relpath(sp, src)
                dp = os.path.join(DEST, rel)
                os.makedirs(os.path.dirname(dp), exist_ok=True)
                shutil.copy2(sp, dp)
                n += 1
        log("已复制 %d 个 .py" % n)

        # 许可证与声明必须一起带过来（GPL 要求）。
        # 位置也随版本变：v3.0.x 放在 addon/ 下，新版在 extension/ 下，
        # 有的版本干脆只有仓库根的 LICENSE。按优先级找。
        got = set()
        cand_dirs = [
            os.path.dirname(src),                    # addon/ 或 extension/
            tmp,                                     # 仓库根
            src,                                     # 插件目录本身
        ]
        for name in ("LICENSE", "NOTICE"):
            for d in cand_dirs:
                s = os.path.join(d, name)
                if os.path.exists(s) and name not in got:
                    shutil.copy2(s, os.path.join(DEST, name))
                    log("已带上 %s（来自 %s）"
                        % (name, os.path.relpath(s, tmp).replace(os.sep, "/")))
                    got.add(name)
                    break
        if "LICENSE" not in got:
            log("⚠ 上游没找到 LICENSE —— GPL 要求随分发保留许可全文，请手工补上")

        # 还原适配文件
        for rel, text in stash.items():
            p = os.path.join(DEST, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            log("已还原适配文件 %s（%s）" % (rel, PATCHED[rel]))

        problems = _selfcheck_imports()
        if problems:
            log("")
            log("⚠ 拉下来的版本是**半成品**，桥接导入会失败（宿主会静默跳过它）：")
            for p in problems[:8]:
                log("    - %s" % p)
            log("")
            log("换一个正式 tag 重试，例如：")
            log("    python tools/fetch_bridge.py --force --ref v3.0.6")
            log("（可用 tag 列表：git ls-remote --tags %s）" % UPSTREAM)
            return 2

        log("自检通过：registration.py 引用的类都能找到")

        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="拉取上游 DSH Blender Bridge 到 addon/ 下")
    ap.add_argument("--check", action="store_true", help="只检查，不下载")
    ap.add_argument("--remove", action="store_true", help="删掉已拉取的副本")
    ap.add_argument("--ref", default=DEFAULT_REF,
                    help="上游分支或 tag（默认 %s，**别用 main**，那是半成品）"
                         % DEFAULT_REF)
    ap.add_argument("--force", action="store_true", help="已存在也重新拉")
    a = ap.parse_args()

    if a.check:
        return 0 if check() else 1
    if a.remove:
        return remove()

    if os.path.isdir(DEST) and not a.force:
        log("bridge/ 已存在。要覆盖请加 --force")
        check()
        return 0

    rc = fetch(a.ref)
    if rc:
        return rc
    log("")
    check()
    log("")
    log("下一步：python tools/build_zip.py   （会把 bridge 一起打进 zip）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
