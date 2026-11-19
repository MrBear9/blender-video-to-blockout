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
