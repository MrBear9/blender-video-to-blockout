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

BODY = """## Installation

### Blender addon

Download **blockout_helper.zip**, then in Blender:
`Edit > Preferences > Add-ons > Install`.

> **Disable the previous version before upgrading.** Blender only unregisters the
> top-level module; submodules stay in `sys.modules`, so a straight upgrade keeps
> running the old code.

Choose a request mode in the addon preferences:
- **Direct** — supply your own API key. It is saved automatically and survives
  restarts.
- **Via DSH** — reuse the model already configured in DSH. Set the port to `0`
  for auto-detection.

### DSH plugin (optional)

Download **dsh-blender-chat.zip**, extract it, then:

```powershell
node install.mjs   # install into the current DSH profile
node doctor.mjs    # five-step diagnostic
node probe.mjs     # end-to-end self test
```

---

## Features

**Blender addon**
- Full video / COLMAP to blockout pipeline: import, rig, camera, verify, export
- AI assistant panel: three permission levels, scrolling, expand to text editor,
  interrupt at any time
- Readable messages when the model returns nothing, when a tool-only turn
  produces no text, or when the step limit is reached, instead of a blank panel

**DSH plugin**
- Lets the Blender panel reuse the model configured in DSH, no API key needed
- Automatic port detection, since DSH picks a new port on every start
  (observed 53064 -> 64944 -> 58169 -> 63524 -> 49648)

---

## Problems fixed in this release

| Symptom | Cause |
|---|---|
| Panel stopped rendering halfway | A `draw()` section raised; Blender reports nothing and simply stops at that line |
| The answer box never appeared | `MESSAGEBOX` does not exist as an icon in Blender 4.5 |
| Answers vanished after switching sessions | The answer lived in a single global slot that was cleared on switch |
| API key lost on every restart | Addon preferences were never saved to disk |
| Blank panel on an empty response | Empty text never emitted a `final` event |
| Wrong DSH port chosen | Some ports accept TCP then send RST, and were mistaken for HTTP services |

---

## Tests

30 regression suites, all passing. Coverage: addon registration and reinstall,
video chain, agent tool loop, SSE stream parsing, session isolation, answer
persistence, panel sectioned drawing, icon validation, three permission levels,
key persistence, DSH channel, port detection.

The two suites that need a real API key skip themselves when none is configured.
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
            "name": "v1.10.3 — Blender blockout addon + DSH bridge plugin",
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
