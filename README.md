# blender-video-to-blockout

Video-to-blockout toolchain for Blender, with an AI assistant panel and a DSH bridge plugin.

[中文](#中文) · [English](#english)

---

## 中文

从视频还原三维白膜的工具链，配一个能在 Blender 面板里直接对话改场景的 AI 助手。

面板有两种请求方式：自己填 API Key 直连，或者把请求交给 DSH——复用 DSH 里
已经配好的模型和凭据，不用重复填 Key。

### 目录结构

```
addon/blockout_helper/      Blender 插件（可安装的 zip 源码）
dsh-plugin/                 DSH 插件：让 Blender 面板借用 DSH 的模型
tests/                      回归测试（30 套）
tools/                      构建与运维脚本
docs/                       设计与实验记录
data/                       产物与素材（不入库）
```

### 快速开始

**1. 打包并安装 Blender 插件**

```powershell
python tools/build_zip.py
```

产物在 `data/blockout_helper.zip`。在 Blender 里
`Edit > Preferences > Add-ons > Install` 安装。

> **升级前必须先禁用旧版。** Blender 只注销顶层模块，子模块会留在
> `sys.modules` 里，不禁用的话装的还是旧代码。

**2. 配置请求方式**

| 方式 | 说明 |
|---|---|
| 插件直连 | 自己填 API Key。改完自动保存到 `userpref.blend`，重启不丢。 |
| 走 DSH | 复用 DSH 已配好的模型，不用填 Key。端口填 `0` 自动探测。 |

**3. （可选）安装 DSH 插件**

```powershell
cd dsh-plugin
node install.mjs      # 装进当前 DSH profile
node doctor.mjs       # 五步诊断
node probe.mjs        # 端到端自检
```

要求 DSH 正在运行。端口每次启动都会变，三个脚本都会自动发现。

### 产物

| 目录 | 内容 |
|---|---|
| `data/aist/video3d/` | `dancer_3d.mp4`、`skeleton3d.blend` |
| `data/aist/studio_long/` | `studio_60s.mp4`、`scene_long.blend` |
| `data/aist/studio/` | `studio_blockout.mp4`、`scene_full.blend` |
| `data/aist/video/` | `dancer_whitebox.mp4`、`dancer_plugin_video.mp4` |
| `data/crowd_ultra/` | 人群精细版成片与场景 |
| `data/crowd_match/` | 运镜匹配版成片与场景 |
| `data/dataset/` | 关键点、轨迹、相机运动等中间数据 |

`data/` 共约 590 MB，体积远超源码，且都能由源码重新生成，因此整个目录不入库。
成片到 [Release](https://github.com/MrBear9/blender-video-to-blockout/releases) 下载。

### 测试

```powershell
& "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe" `
  -b --factory-startup --python tests/test_addon.py
```

30 套全部通过。需要真实 API Key 的两套在无 Key 时自行跳过。

### 注意

- `tools/check_icons.py` 不是可有可无的：Blender 的 icon 是 enum，写错一个就往
  `draw()` 里抛 `TypeError`，而 **draw 的异常完全静默**——只画到出错那行，
  不报错不打印。一个拼错的 icon 就能让整块 UI 永久消失。
- 改文件请用 Python 的 `io.open(encoding='utf-8')`。PowerShell 的
  `Set-Content -Encoding UTF8` 会把中文按 GBK 往返一次，全变乱码。

---

## English

A pipeline that reconstructs 3D blockouts from video, plus an AI assistant panel
inside Blender for editing the scene by conversation.

The panel supports two request modes: call the model directly with your own API
key, or hand the request to DSH so the model and credentials configured there are
reused without re-entering a key.

### Layout

```
addon/blockout_helper/      Blender addon (installable zip source)
dsh-plugin/                 DSH plugin: lets the Blender panel borrow DSH's model
tests/                      Regression tests (30 suites)
tools/                      Build and maintenance scripts
docs/                       Design notes and experiment logs
data/                       Outputs and assets (not tracked)
```

### Quick start

**1. Build and install the Blender addon**

```powershell
python tools/build_zip.py
```

The artifact lands in `data/blockout_helper.zip`. Install it in Blender via
`Edit > Preferences > Add-ons > Install`.

> **Disable the previous version before upgrading.** Blender only unregisters the
> top-level module; submodules stay in `sys.modules`, so a straight upgrade keeps
> running the old code.

**2. Choose a request mode**

| Mode | Description |
|---|---|
| Direct | Supply your own API key. It is saved to `userpref.blend` automatically. |
| Via DSH | Reuse the model already configured in DSH. No key needed. Set port to `0` for auto-detection. |

**3. (Optional) Install the DSH plugin**

```powershell
cd dsh-plugin
node install.mjs      # install into the current DSH profile
node doctor.mjs       # five-step diagnostic
node probe.mjs        # end-to-end self test
```

Requires a running DSH instance. The port changes on every start; all three
scripts detect it automatically.

### Outputs

| Directory | Contents |
|---|---|
| `data/aist/video3d/` | `dancer_3d.mp4`, `skeleton3d.blend` |
| `data/aist/studio_long/` | `studio_60s.mp4`, `scene_long.blend` |
| `data/aist/studio/` | `studio_blockout.mp4`, `scene_full.blend` |
| `data/aist/video/` | `dancer_whitebox.mp4`, `dancer_plugin_video.mp4` |
| `data/crowd_ultra/` | Detailed crowd render and scene |
| `data/crowd_match/` | Source-matched camera motion render and scene |
| `data/dataset/` | Intermediate keypoints, tracks, camera motion |

`data/` totals roughly 590 MB. It dwarfs the source and can be regenerated, so
the whole directory is excluded from version control. Grab the renders from the
[Releases](https://github.com/MrBear9/blender-video-to-blockout/releases) page.

### Tests

```powershell
& "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe" `
  -b --factory-startup --python tests/test_addon.py
```

All 30 suites pass. The two that need a real API key skip themselves when none
is configured.

### Notes

- `tools/check_icons.py` is not optional. Blender icons are an enum; a single
  wrong name raises `TypeError` inside `draw()`, and **draw exceptions are
  completely silent** — Blender stops at the offending line without any error or
  traceback. One misspelled icon makes an entire UI section vanish permanently.
- Use Python's `io.open(encoding='utf-8')` when writing files. PowerShell's
  `Set-Content -Encoding UTF8` round-trips CJK through GBK and corrupts it.

---

MIT License © 2026 MrBear9 · [LICENSE](LICENSE) · [NOTICE](NOTICE.md) · [CONTRIBUTING](CONTRIBUTING.md)
