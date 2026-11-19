# 贡献指南 / Contributing

[中文](#中文) · [English](#english)

## 中文

### 报告问题

提 issue 时请带上：

- Blender 版本（`Help > About Blender`）与操作系统
- 复现步骤，越短越好
- 面板上「诊断」区展开后的截图（含版本号、构建时间、心跳）
- 相关日志：偏好设置里点「查看 AI 日志」，把 `ai_debug.log` 尾部贴上来

插件在 `draw()` 里抛异常时 Blender **完全静默**——不报错、不打印、只画到
出错那行。所以「面板某块不见了」几乎总是 draw 异常，日志比截图有用得多。

### 提交代码

1. 先跑一遍测试，确认基线是绿的：

   ```powershell
   & "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe" `
     -b --factory-startup --python tests/test_addon.py
   ```

2. 改动涉及 UI 的，跑 `tests/test_icons.py`——它会拦住 Blender 4.5
   不存在的 icon 名。

3. 改动涉及打包的，跑 `python tools/build_zip.py`，它有自检。

4. 提交信息用**英文**，一句话说清做了什么；正文可以写原因。

### 代码约定

- **不要用 PowerShell 的 `Set-Content -Encoding UTF8` 改文件**。它会把中文按
  GBK 往返一次，全变乱码，还会加 BOM。用 Python 的
  `io.open(path, encoding='utf-8', newline='\n')`。
- 后台线程不能碰 `bpy`。所有 bpy 调用走 `dispatch.call_on_main` 或
  `bpy.app.timers`。
- 新增 icon 名请先跑 `tools/check_icons.py`。
- 中文注释可以，但**提交信息和 tag 用英文**。

---

## English

### Reporting issues

Please include:

- Blender version (`Help > About Blender`) and OS
- Reproduction steps, as short as possible
- A screenshot of the expanded "Diagnostics" section in the panel (version,
  build time, heartbeat)
- Relevant log: click "View AI log" in the preferences and paste the tail of
  `ai_debug.log`

When the addon raises inside `draw()`, Blender is **completely silent** — no
error, no traceback, it just stops rendering at the offending line. A missing UI
section is almost always a draw exception, so the log is far more useful than a
screenshot.

### Submitting code

1. Run the tests first to confirm a green baseline:

   ```powershell
   & "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe" `
     -b --factory-startup --python tests/test_addon.py
   ```

2. For UI changes, also run `tests/test_icons.py`. It catches icon names that
   do not exist in Blender 4.5.

3. For packaging changes, run `python tools/build_zip.py`; it self-checks.

4. Commit messages in **English**, one line describing what changed; the body
   can explain why.

### Conventions

- **Never edit files with PowerShell's `Set-Content -Encoding UTF8`.** It
  round-trips CJK through GBK (corrupting it) and adds a BOM. Use Python's
  `io.open(path, encoding='utf-8', newline='\n')`.
- Background threads must not touch `bpy`. Route all bpy calls through
  `dispatch.call_on_main` or `bpy.app.timers`.
- Run `tools/check_icons.py` before adding a new icon name.
- Chinese comments are fine, but **commit messages and tags must be English**.
