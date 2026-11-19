# dsh-blender-chat —— 让 Blender 面板借用 DSH 的模型

## 它解决什么

你现在的插件是自己直接请求 `api.deepseek.com`：

```
Blender 面板 → 自己 HTTP → DeepSeek（要在插件里填 API Key）
```

装上这个 DSH 插件后多一条路：

```
Blender 面板 → DSH 插件 → DSH 的 agent（复用已配好的模型，零 Key）
```

**好处**：
- 不用再填 API Key，也不会因为 Blender 没存偏好设置而丢
- 可以用 DSH 那套 **34 family / 228 个建模 op**（插件自己调不到）
- 模型/密钥换在 DSH 里改，Blender 端不用动

## 装之前

需要 DSH 在跑。检查：

```powershell
Invoke-WebRequest http://127.0.0.1:53064 -UseBasicParsing | Select-Object StatusCode
```

端口看浏览器地址栏（DSH Web GUI 的 URL）。

## 安装

```powershell
cd E:\Xiongwq\RobotChat\blender-video-to-blockout\dsh-plugin

# 先看会改什么（不落盘）
node install.mjs --dry-run

# 真装
node install.mjs

# 装到别的 profile
node install.mjs --profile <名字>
```

安装器做两件事：
1. 复制插件到 `~/.dsh/profiles/<profile>/node_modules/dsh-blender-chat/`
2. 改 `package.json`（加依赖 + 加进 bundles），**改之前自动备份**

**然后重启 DSH**（Desktop 可以在插件页刷新）。

## 自检

```powershell
node probe.mjs
```

正常输出：

```
[1] /blender-chat/status
  ✓ 路由已挂载
[2] /blender-chat/send
  ✓ 已投递给 DSH agent
[3] /blender-chat/poll（最多等 90 秒）
  ✓ 收到回复
```

如果返回 **401**，说明 DSH 要鉴权：
```powershell
node probe.mjs --auth "Bearer <从浏览器 DevTools 抄的>"
```

## Blender 端切换

装完 DSH 插件后，在 Blender 里：

1. `Edit → Preferences → Add-ons → Blockout Helper → 偏好设置`
2. 「① 请求方式」选 **走 DSH**
3. 填端口（默认 53064）
4. 点「测试 DSH 通道」

通过之后，面板发消息就不需要 API Key 了。

## 两条路可以并存

「插件直连」和「走 DSH」随时切。DSH 没开的时候用直连，开了就用 DSH。

## 卸载

```powershell
node install.mjs --uninstall
```

## 排查

| 现象 | 原因 | 修法 |
|---|---|---|
| probe 报 404 | 插件没装或 DSH 没重启 | `node install.mjs` 然后重启 DSH |
| probe 报 401 | DSH 要鉴权 | 带 `--auth`，或关掉本地鉴权 |
| probe 连不上 | DSH 没跑 / 端口错 | 看浏览器地址栏的端口 |
| 收到 send 但没回复 | 事件名不对 | 看 `src/index.js` 里 `agent/assistant/message` 是否与实际一致 |
| 回复很慢 | 模型本身慢 | 正常，轮询会一直等 |

## 技术说明

- 插件类型：Cordis 插件，`inject = ['agents']`
- 用 `ctx.agents.create()` 建会话、`agent.followup(msg)` 投稿
- 监听 `agent/assistant-stream`（增量）和 `agent/assistant/message`（完整）
- 通过 `ctx.webServer.register()` 挂 4 条路由
- Blender 侧轮询取事件，不依赖 WebSocket

**为什么用轮询不用推送**：Blender 的 `bpy` 只能主线程碰，WebSocket 回调线程不安全。
轮询放在后台线程，事件经 `bpy.app.timers` 回主线程 —— 和现有架构一致。

## 已知限制

- **事件名可能要调**：`agent/assistant/message` 是参照 qqbot 插件的用法写的，
  如果实际 DSH 版本用别的名字（比如 `assistant/message`），改 `src/index.js` 里那两行 `ctx.on(...)`。
  `probe.mjs` 收到 send 但拿不到回复，就是这个原因。
- 工具调用由 DSH 侧决定，Blender 面板看不到中间过程（只有最终文本）。
- 一轮一个 session，不做并发合并。
