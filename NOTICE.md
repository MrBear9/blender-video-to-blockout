# 来源与致谢 / Sources and Credits

## 数据集

**AIST++ / AIST Dance Video Database**

本项目的舞蹈多视角白膜使用 AIST 的相机标定数据与视频素材。

> Li, Ruilong, et al. "AI Choreographer: Music Conditioned 3D Dance Generation
> with AIST++." Proceedings of the IEEE/CVF International Conference on Computer
> Vision (ICCV), 2021.

数据集的许可与使用条款由 AIST 官方发布，本仓库不重新分发数据集本身，
`data/` 目录也不纳入版本控制。使用前请阅读官方条款。

---

## 库

**Blender** — GNU General Public License v2.0 或更高版本。
本项目的 Blender 插件以 Blender API（`bpy`）为接口，不包含 Blender 源码
本身。插件代码以 MIT 许可发布。

**DSH（DeepSeek Harness）** — 见其自身仓库的许可条款。
`dsh-plugin/` 是 DSH 的插件，通过公开的插件接口（Cordis、`ctx.agents`、
`ctx.webServer`）与之交互，不包含 DSH 源码。

**NumPy** — BSD 3-Clause License。
用于视频链路的数值计算。

**OpenCV** — Apache License 2.0。
用于特征点检测与光流估计。注意 OpenCV 5.0 已移除 `HOGDescriptor`，
本项目改用差分后端。

---

## 第三方插件（vendored）

**DSH Blender Bridge** — 作者 Siddharth Ahuja
（[github.com/ahujasid](https://github.com/ahujasid)，插件主页
[github.com/sixtysevenlf/dsh-blender-plugin](https://github.com/sixtysevenlf/dsh-blender-plugin)）。

它原本是一个独立的 Blender 插件，与本项目无关，用于把 Blender 接到 DSH
（DeepSeek Harness）的运行时通道上：在本地开一个 TCP 服务（默认 9876），
让 agent 能读场景状态、执行代码、取视口截图。

本项目把它**原样内置**在 `addon/blockout_helper/bridge/` 下，目的是让用户
只装一个插件就同时拿到「建模面板」和「agent 通道」，不必分别安装、分别
卸载。代码本身没有改动功能逻辑，只做了三处必要的适配：

1. `bridge/compat.py` 的 `ADDON_MODULE` 由 `__package__` 改为取顶层包名 ——
   作为子包后 `__package__` 会变成 `blockout_helper.bridge`，而 Blender 查
   `preferences.addons` 只认顶层模块名。
2. `bridge/ui/preferences.py` 的 `AddonPreferences` 不再自行注册 ——
   一个插件只能有一个 `AddonPreferences`，它的属性改为注入到宿主的
   `BlockoutAIPrefs` 上，读写方式不变。
3. `bridge/registration.py` 相应地跳过该类的注册与注销。

**许可证：上游按目录分别声明，不是单一许可。** 这一点必须说清，否则容易
误判本仓库能对这部分代码做什么：

| 范围 | 许可证 |
|---|---|
| 上游仓库根目录（含 DSH 侧 Node 代码） | BSD 3-Clause |
| `extension/` 下的 Blender 插件包 | **GPL-3.0-or-later** |
| 插件上游来源 `ahujasid/blender-mcp` | MIT |

本仓库内置的 `addon/blockout_helper/bridge/` 取自 **`extension/dsh_blender_bridge/`**，
因此对应的是 **GPL-3.0-or-later**，而不是仓库根的 BSD。上游 `extension/NOTICE`
对 GPL 有明确声明，并保留了 MIT 上游（Siddharth Ahuja, `blender-mcp`）的版权与
许可全文。

**这对本仓库意味着什么**：GPL-3.0 具有传染性。内置 GPL 代码会让
`blockout_helper` 的整体分发受 GPL-3.0 约束，而本仓库自身的许可是 MIT ——
两者并不兼容。

**本仓库的处理方式：那份代码不入库。** `addon/blockout_helper/bridge/` 被
`.gitignore` 排除，仓库因此保持纯 MIT，可以正常公开分发。需要 agent 通道时
用 `python tools/fetch_bridge.py` 从上游拉取，再
`python tools/build_zip.py --with-bridge` 打一个**本地自用**的合并包。

仓库里保留的是**合并适配代码**（三处改动）与 `blockout_helper` 里对桥接的
可选加载逻辑 —— 找不到 `bridge/` 时 `_bridge_ok = False`，建模功能照常可用，
只是不启动 agent 通道。这部分是本项目自己写的，属 MIT。

`addon/blockout_helper/bridge/` 下原始文件的版权归原作者；本仓库不声称对其
拥有版权，也不对其另行授权。本项目自身的 MIT 许可**不覆盖**该目录。

上游代码若有更新，替换 `addon/blockout_helper/bridge/` 整个目录即可，
上述三处适配需重新应用。

---

## 算法与思路

- **COLMAP 格式** — 相机内参、外参与稀疏点云的读写遵循
  [COLMAP](https://colmap.github.io/) 的文件格式约定。
- **Lucas-Kanade 光流** — 相机运动估计使用 OpenCV 的
  `estimateAffinePartial2D`，并对累积漂移做了去趋势处理。
- **TUM RGB-D 数据集** — 标定验证的参照（ATE 实测 1.86 cm）。

---

## 图标与素材

Blender 面板使用 Blender 内置的图标名（`bpy.types.UILayout` 的 icon enum）。
本项目不附带任何图片素材；`data/` 下的渲染图均为本项目自行生成。
