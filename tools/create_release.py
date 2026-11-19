"""create_release.py — 用 GitHub API 创建 Release 并上传两个 zip

为什么不手工点：两个资产 + 一大段说明，手工容易漏。
为什么用 Python：PowerShell 的引号和 UTF-8 处理在中文 body 上反复出问题。

用法：
    python tools/create_release.py            # 创建 Release
    python tools/create_release.py --dry-run  # 只看会发什么
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# ⚠ Windows 控制台默认 GBK，打印 ✓ / ✗ 会抛 UnicodeEncodeError。
#   必须在任何输出之前把 stdout 切成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

OWNER = "MrBear9"
REPO = "blender-video-to-blockout"
TAG = "v1.10.3"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BODY = """## 安装

### Blender 插件

下载 **blockout_helper.zip** → Blender 里
`Edit > Preferences > Add-ons > Install` 安装。

> **升级前必须先禁用旧版。** Blender 只注销顶层模块，子模块会留在
> `sys.modules` 里，不禁用的话装的还是旧代码。

装好后在偏好设置里二选一：
- **插件直连** — 填 API Key，改完自动保存，重启不丢
- **走 DSH** — 复用 DSH 已配好的模型，端口填 `0` 自动探测

### DSH 插件（可选，让 Blender 借 DSH 的模型）

下载 **dsh-blender-chat.zip**，解开后：

```powershell
node install.mjs   # 装进当前 DSH profile
node doctor.mjs    # 五步诊断
node probe.mjs     # 端到端自检
```

---

## 功能

**Blender 插件**
- 视频 / COLMAP → 白膜的完整链路（导入、骨骼、运镜、验收、导出）
- AI 助手面板：三档权限、上下滚动、放大到文本编辑器、随时中断
- 空回复、工具轮无文本、达到步数上限时都给出可读说明，不再白屏

**DSH 插件**
- 让 Blender 面板复用 DSH 已配置的模型，不用填 API Key
- 端口自动探测（DSH 每次启动都换端口，实测见过 53064 → 64944 → 58169 → 63524 → 49648）

---

## 这一版修掉的问题

| 问题 | 原因 |
|---|---|
| 面板画到一半就不见了 | draw() 里某段抛异常，Blender 完全静默，只画到出错那行 |
| 回答框从来没出现过 | `MESSAGEBOX` 这个 icon 在 Blender 4.5 不存在 |
| 切换会话后答案消失 | 答案存在一个全局槽位，切走就清空 |
| API Key 每次重启都没了 | 偏好设置从不落盘 |
| 空回复时面板一片空白 | 空文本不发 final 事件 |
| DSH 端口老是选错 | 58000+ 端口会接受 TCP 后立刻 RST，被误判成 HTTP 服务 |

---

## 测试

30 套回归，全部通过。覆盖插件注册与重装、视频链路、agent 工具循环、
SSE 流式解析、会话隔离、答案持久化、面板分段渲染、icon 校验、
权限三档、Key 落盘、DSH 通道、端口探测。

需要真实 API Key 的两套在无 Key 时自行跳过。
"""

ASSETS = [
    "data/blockout_helper.zip",
    "data/dsh-blender-chat.zip",
]


def get_token():
    """从 Git Credential Manager 取 token（不落盘、不打印）。"""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, env=env, timeout=30)
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            return line[len("password="):]
    raise SystemExit("取不到 GitHub token，请确认已登录")


def api(method, path, token, payload=None, raw=None, content_type=None):
    url = "https://api.github.com" + path
    headers = {
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dsh-release-script",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"

    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"error": body[:500]}


def upload_asset(rel, path, token):
    """上传 Release 资产。

    ⚠ 必须用 uploads.github.com，不是 api.github.com。
      用后者会回 404，且错误信息只说 Not Found，看不出原因。
      upload_url 里已经带了 {?name,label} 模板，替换掉即可。
    """
    name = os.path.basename(path)
    with open(path, "rb") as f:
        raw = f.read()
    base = rel["upload_url"].split("{")[0]
    url = "%s?name=%s" % (base, urllib.parse.quote(name))
    headers = {
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/zip",
        "Content-Length": str(len(raw)),
        "User-Agent": "dsh-release-script",
    }
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"error": body[:400]}


def main():
    dry = "--dry-run" in sys.argv
    token = get_token()

    print("=" * 56)
    print("创建 Release: %s/%s @ %s" % (OWNER, REPO, TAG))
    print("=" * 56)

    # 1. 检查是否已存在
    code, rel = api("GET", "/repos/%s/%s/releases/tags/%s"
                    % (OWNER, REPO, TAG), token)
    if code == 200:
        print("Release 已存在，复用 id=%s" % rel.get("id"))
        rel_id = rel["id"]
    else:
        payload = {
            "tag_name": TAG,
            "name": "v1.10.3 — Blender 白膜插件 + DSH 桥接插件",
            "body": BODY,
            "draft": False,
            "prerelease": False,
        }
        if dry:
            print("[dry-run] 会创建的 Release：")
            print(json.dumps({k: (v[:80] + "…" if isinstance(v, str)
                                  and len(v) > 80 else v)
                              for k, v in payload.items()},
                             ensure_ascii=False, indent=2))
            rel_id = None
        else:
            code, rel = api("POST", "/repos/%s/%s/releases"
                            % (OWNER, REPO), token, payload)
            if code not in (200, 201):
                print("✗ 创建失败 HTTP %d" % code)
                print(rel.get("error"))
                return 1
            rel_id = rel["id"]
            print("✓ Release 已创建 id=%s" % rel_id)
            print("  %s" % rel.get("html_url"))

    if dry or rel_id is None:
        print("\n[dry-run] 资产：")
        for a in ASSETS:
            p = os.path.join(ROOT, a)
            print("  %-32s %7.1f KB  %s"
                  % (a, os.path.getsize(p) / 1024, "存在" if os.path.exists(p)
                     else "缺失"))
        return 0

    # 2. 上传资产
    print("\n上传资产：")
    # 重新取一次，确保 upload_url 是最新的
    _, rel_latest = api("GET", "/repos/%s/%s/releases/tags/%s"
                        % (OWNER, REPO, TAG), token)
    existing = {a["name"] for a in rel_latest.get("assets", [])}
    for a in ASSETS:
        p = os.path.join(ROOT, a)
        if not os.path.exists(p):
            print("  ✗ 缺文件: %s" % a)
            continue
        name = os.path.basename(p)
        if name in existing:
            print("  = %-28s 已存在，跳过" % name)
            continue
        code, res = upload_asset(rel_latest, p, token)
        if code in (200, 201):
            print("  ✓ %-28s %7.1f KB" % (name, os.path.getsize(p) / 1024))
        elif code == 422:
            print("  = %-28s 已存在" % name)
        else:
            print("  ✗ %-28s HTTP %d: %s"
                  % (name, code, str(res.get("error"))[:160]))

    # 3. 汇总
    code, final = api("GET", "/repos/%s/%s/releases/tags/%s"
                      % (OWNER, REPO, TAG), token)
    if code == 200:
        print("\n" + "=" * 56)
        print("Release: %s" % final.get("html_url"))
        print("资产 %d 个：" % len(final.get("assets", [])))
        for a in final.get("assets", []):
            print("  %-28s %8.1f KB  下载 %d 次"
                  % (a["name"], a["size"] / 1024, a.get("download_count", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
