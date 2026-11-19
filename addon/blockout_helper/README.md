# Blockout Helper v1.0

Blender 插件：**视频转白模** + **AI 对话助手**。

装一次，两个能力都有：

| 能力 | 干什么 |
|---|---|
| 视频转白模 | 3D 关节轨迹 → 骨骼白模 → 运镜 → 渲染 MP4 / 导出 GLB |
| AI 对话 | 用自然语言改场景、跑白膜链路，接 DeepSeek / OpenAI / Ollama |

---

## 安装

1. Blender 4.2+
2. `Edit → Preferences → Add-ons → Install…`
3. 选 `blockout_helper.zip`，勾选启用
4. 3D 视图按 `N` → 侧栏找到 **Blockout** 标签

装好后侧栏有 5 段：视频转白模（单帧对位）、视频转白模·骨骼、AI 助手、显示。

---

## 一、AI 对话（先配这个）

### 配 API

`Edit → Preferences → Add-ons → Blockout Helper → 展开`

| 项 | 说明 |
|---|---|
| 提供商 | DeepSeek / OpenAI / Anthropic / Ollama / 自定义 |
| API Key | **存在本机用户配置，不写进 .blend**（分享文件不会泄漏） |
| 模型 | DeepSeek 用 `deepseek-flash` 或 `deepseek-v4-pro` |
| Base URL | 只有"自定义"需要填 |
| 温度 | 建模建议 0.2–0.4 |

填完点 **测试连接**。成功会显示模型的回复。

DeepSeek 取 key：https://platform.deepseek.com/api_keys

### 用对话

侧栏 `Blockout → AI 助手`：

- 输入框输入需求，点 **发送**
- 运行中可点 **停止**
- `自动允许破坏性操作` 关着时，删除/执行 Python 会弹确认——**建议保持关闭**
- 底部有快捷按钮：看场景 / 查对象 / 白膜流程

能干什么：

```
加一排 5 个柱子，间距 2 米，高度递增
把选中的物体都改成灰色哑光材质
给场景打三点布光，主光从左上 45 度
按 import_joints → build_rig → verify_framing 跑一遍白膜
把这个场景导出成 GLB 到 D:/out/test.glb
```

### 安全边界

| 机制 | 作用 |
|---|---|
| 破坏性工具要确认 | `delete_objects` / `run_python` 默认弹窗，拒绝就不执行 |
| 单轮步数上限 | 最多 12 次工具往返，防止模型死循环 |
| 删除保护 | `delete_objects` 空参数直接拒绝，不会误删全场景 |
| 历史裁剪 | 保留最近 8 轮，防 token 爆炸 |

---

## 二、视频转白膜

### 完整链路

```
kp3d.json（3D 关节，厘米，Y-up）
  → ① 导入 3D 关节
  → ② 生成骨骼
  → ③ 配运镜
  → ④ 检查取景
  → ⑤ 渲染并编码 MP4
  → ⑥ 导出 GLB
```

### 输入契约 `kp3d.json`

```json
{
  "unit": "cm",
  "fps": 59.94,
  "names": ["nose", "shoulder_l", ...],
  "frames": [
    {"idx": 0, "joints": {
      "nose": {"xyz_cm": [x, y, z], "reproj_px": 3.8}
    }}
  ]
}
```

- `unit` 必填（`mm`/`cm`/`m`），写错直接报错，**不会静默当米用**
- 坐标系 **Y 轴向上**，插件内部转 Z-up
- 关节名跟 COCO 17 一致，缺哪个就少那一段骨骼

> ⚠ 必须是**真 3D**。拿 2D 关键点当 2.5D 用，人物一转身左右肢会叠在一起。

### 运镜预设

| 预设 | 轨迹 |
|---|---|
| `cinematic` | 低机位广角起 → 贴脸 → 绕背升高 → 俯拍 → 拉远 |
| `turntable` | 恒距恒高，转 360° |
| `dolly_in` | 远景推半身 |
| `top_down` | 正上方俯瞰（机位要够高，低了会切边） |

相机距离不是写死的数字，是每帧按**投影后的画面范围**解出来的。

### 验收判据

**取景自检 `worst NDC ≤ 1.0`** 才算不切边。`1.0` 就是贴边。

渲染前一定要跑"检查取景"，`fail` 就别渲。

---

## 三、Python API

不进面板也能用：

```python
from blockout_helper import kit, skeleton, camera as cam_mod, scene as scene_mod

frames, unit, fps, names = skeleton.load_track("kp3d.json")
arrays = skeleton.joints_to_arrays(frames, unit, names)
sm = skeleton.smooth_arrays(arrays, median_k=5, mean_k=3)

zoff = skeleton.ground_offset(sm)
for seq in sm.values():
    for p in seq:
        if p is not None:
            p.z += zoff

objs = skeleton.build_rig(sm, skeleton.RigSpec(height_m=1.70))
info = cam_mod.setup_camera(scene, list(objs.values()), preset="cinematic")
r = cam_mod.verify_framing(scene, scene.camera, list(objs.values()))
assert r["verdict"] == "pass", r

scene_mod.render_sequence(scene, "out/frames")
scene_mod.encode_mp4(scene, "out/frames", "out/blockout.mp4", fps=25)
```

### 用 AI 的工具也可以直接调

```python
from blockout_helper import tools, dispatch

# 主线程
print(tools.call("get_scene_info", {}))

# 后台线程必须走 dispatch
r = dispatch.call_on_main(tools.call, "add_primitive",
                          {"kind": "cube", "size": 0.5})
```

---

## 四、架构

```
blockout_helper/
├── __init__.py      旧算子（对位搭盒/打分）+ 面板 + 注册
├── kit.py           单位/坐标/集合/几何/取景数学
├── skeleton.py      关节契约 → 骨骼白模
├── camera.py        运镜预设 + 投影求解
├── scene.py         环境/渲染/编码
├── ops_video.py     视频链路的 6 个算子
├── dispatch.py      ★ bpy.app.timers 主线程派发
├── api_client.py    ★ 多提供商 HTTP 客户端（纯标准库）
├── tools.py         ★ 18 个场景工具（给 AI 用）
├── agent.py         ★ tool-use 循环 + 会话
└── ui_ai.py         ★ 偏好设置 + 对话面板
```

标 ★ 的是 v1.0 新增。旧算子一行没动，三个新层都是**隔离导入**——缺任何一个，其余部分照常工作。

---

## 五、开发者备忘：三个必踩的坑

### 1. bpy 不是线程安全的（最容易崩）

在后台线程调用 bpy 的结果不是抛异常，是**硬崩溃**——Blender 直接退出，连 traceback 都没有。

但 LLM 的 HTTP 请求必须放后台，否则界面冻死。

**解法**：HTTP 在后台，任何碰 bpy 的代码用 `bpy.app.timers` 排回主线程。

```python
# dispatch.py 的核心
def call_on_main(fn, *args, **kwargs):
    if is_main_thread():
        return fn(*args, **kwargs)          # 别自己等自己
    res = DispatchResult()
    def _run():
        try: res.value = fn(*args, **kwargs)
        except Exception as e: res.error = e
        finally: res.event.set()
        return None                          # None = 不重复执行
    _queue.append(_run)
    _ensure_tick()
    if not res.event.wait(timeout):
        raise TimeoutError(...)              # 宁可报错，不要永远挂住
    if res.error: raise res.error
    return res.value
```

**实测**：后台线程调 `get_scene_info` 到返回 3 个对象，**11.1ms**。

> ⚠ `blender -b`（后台模式）**没有事件循环**，timer 永远不触发。
> 离线测试里必须手动调 `_TIMER_TICK()`。真实 GUI 里不用管。

### 2. 偏好设置不能读太快

`AddonPreferences` 的属性只在 addon **被 enable 之后**才存在。用 `sys.path` 手动 import 的模块不会进 `preferences.addons`，读属性会 `AttributeError`。

正确验证路径：真装 zip → `addon_utils.enable()` → 再读。

### 3. API Key 不能放 scene 属性

`scene` 会被 `.blend` 文件带走。放 `AddonPreferences` 存本机，用户分享文件才不泄漏。

---

## 六、测试

```bash
# 单位/坐标/骨骼/运镜/渲染 15 项
blender -b --factory-startup --python test_video_chain.py

# 旧算子 13 项（回归）
blender -b --factory-startup --python test_addon.py

# 插件本体 16 项（含真实 zip 安装）
blender -b --factory-startup --python test_plugin_full.py

# agent tool-use 5 项（本地 mock 服务，不花 token）
blender -b --factory-startup --python test_agent_e2e.py

# API 客户端 9 项（纯 Python，不需要 Blender）
python test_api.py
```

全部判据都是数字，不靠肉眼。

---

## 七、已知限制

| 限制 | 说明 |
|---|---|
| 模型要支持 tool-calling | 纯文本模型没法调工具 |
| Ollama 小模型可能吐原始 JSON | 代码里做了兜底解析，但不保证 |
| 不做流式输出 | 一次请求拿完整回复；长回答会等几秒 |
| 三角化不在插件里 | 依赖 numpy/torch，故意留给外部脚本，只收 `kp3d.json` |
| 多人追踪有上限 | 密集重叠场景身份容易串，需要更严格的匹配阈值 |

---

## 八、许可

MIT。构建于本人的实验结果，参考了社区在 Blender+LLM 集成上的公开经验。
