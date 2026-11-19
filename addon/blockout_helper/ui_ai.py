"""ui_ai.py —— AI 配置（偏好设置）+ 对话面板（N 侧栏）

设计约束（都是踩过的坑）：
  1. **绝不在 _poll_body 里用 st.lines.remove()**
     Blender 的 CollectionProperty 删除元素后，之前的索引引用会失效，
     后续访问抛异常 → 整批事件丢失。实测表现：面板上连 done 都不出现。
  2. **每个事件单独 try/except**
     一个坏事件不能拖垮整批，否则后面的 final / done 全丢。
  3. **答案额外写进 st.last_answer（普通 StringProperty）**
     集合操作可能失败，但简单赋值不会。面板直接画它，保证一定看得见。
  4. **心跳计数**
     st.heartbeat 每次 tick +1，用来判断 timer 是否还在跑、面板是否在重绘。
  5. 流式块只更新 status，不建行。
     只有 final 建一行——这样永远不会产生碎片。
"""

import json
import os
import threading
import time

import bpy

from . import agent, api_client, debuglog, dispatch, tools


def _build_info():
    """读 build_info.json（打包时写入），用来确认装的是哪个版本。"""
    try:
        p = os.path.join(os.path.dirname(__file__), "build_info.json")
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"version": "?", "built": "?"}


# ═══════════════════════════════════════════════════════════════════
# 偏好设置：API 配置
# ═══════════════════════════════════════════════════════════════════

def _autosave_prefs(self, context):
    """改了就存偏好设置。

    ⚠ 这是「key 每次重启都没了」的真正修法：
      偏好设置存在 userpref.blend 里，不 save_userpref() 就不落盘。
      实测用户那台机器 userpref.blend 停在几个月前 —— 从没保存过，
      所以每次填的 key 都只活在内存里。
    """
    try:
        bpy.ops.wm.save_userpref()
    except Exception as e:                      # noqa: BLE001
        debuglog.write("保存偏好设置失败: %s" % e)


class BlockoutAIPrefs(bpy.types.AddonPreferences):
    bl_idname = "blockout_helper"

    provider: bpy.props.EnumProperty(
        name="提供商",
        items=[(k, v["label"], v["docs"] or k)
               for k, v in api_client.PROVIDERS.items()],
        default="deepseek",
        update=_autosave_prefs,
    )
    #: 请求走哪边：
    #:   direct = 插件自己直连（需要填 API Key）
    #:   dsh    = 交给 DSH（复用 DSH 已配好的模型，不填 Key）
    backend: bpy.props.EnumProperty(
        name="请求方式",
        items=[
            ("direct", "插件直连",
             "自己请求模型，需要在下面填 API Key"),
            ("dsh", "走 DSH",
             "交给 DSH 的模型，不用填 Key；还能用它那 228 个建模 op"),
        ],
        default="direct",
        update=_autosave_prefs,
    )
    dsh_port: bpy.props.IntProperty(
        name="DSH 端口", default=0, min=0, max=65535,
        description="填 0 = 自动探测（DSH 每次启动都换端口，建议留 0）",
        update=_autosave_prefs,
    )
    api_key: bpy.props.StringProperty(
        name="API Key", subtype="PASSWORD", default="",
        description="存在本机用户配置里，不写进 .blend 文件",
        update=_autosave_prefs,
    )
    model: bpy.props.StringProperty(
        name="模型", default="deepseek-flash",
        description="DeepSeek 用 deepseek-flash 或 deepseek-v4-pro",
        update=_autosave_prefs,
    )
    base_url: bpy.props.StringProperty(
        name="Base URL", default="",
        description="留空用提供商默认值",
        update=_autosave_prefs,
    )
    temperature: bpy.props.FloatProperty(
        name="温度", default=0.3, min=0.0, max=1.5,
        description="越低越稳定，建模建议 0.2–0.4",
    )
    test_status: bpy.props.StringProperty(default="")

    def draw(self, context):
        layout = self.layout
        col = layout.column()

        box = col.box()
        box.label(text="① 请求方式", icon="PREFERENCES")
        box.prop(self, "backend", text="")
        if self.backend == "dsh":
            box.prop(self, "dsh_port")
            box.label(text="不用填 API Key，直接用 DSH 的模型", icon="INFO")
            box.label(text="端口填 0 = 自动探测（推荐）", icon="SORTTIME")
            box.operator("blockout.ai_dsh_test", text="测试 DSH 通道",
                         icon="PLUGIN")
        else:
            box.prop(self, "provider", text="")
            prov = api_client.PROVIDERS.get(self.provider, {})
            if prov.get("docs"):
                box.label(text="文档：" + prov["docs"], icon="URL")
            if prov.get("models"):
                box.label(text="常用模型：" + ", ".join(prov["models"]),
                          icon="INFO")

        box = col.box()
        if self.backend == "dsh":
            box.label(text="② 凭据（由 DSH 提供）", icon="CHECKMARK")
            box.label(text="无需在此填写")
        else:
            box.label(text="② 凭据", icon="LOCKED")
            box.prop(self, "api_key")
            box.prop(self, "model")
            if self.provider == "custom":
                box.prop(self, "base_url")
        # ⚠ 手动保存按钮：万一 update 回调被跳过（改完没回车就关窗口），
        #   点这个能确保 key 落盘，不用重启后再填一遍。
        row = box.row(align=True)
        row.operator("blockout.ai_save_prefs", text="保存设置",
                     icon="FILE_TICK")
        row.label(text="改完会自动保存")

        box = col.box()
        box.label(text="③ 连通性自检", icon="PLUGIN")
        box.operator("blockout.ai_test", text="测试连接", icon="PLAY")
        if self.test_status:
            for line in self.test_status.split("\n")[:6]:
                box.label(text=line[:70])

        box = col.box()
        box.label(text="会话", icon="TRASH")
        box.operator("blockout.ai_clear", text="清空当前会话消息",
                     icon="X")


class BO_OT_ai_save_prefs(bpy.types.Operator):
    """把当前偏好设置（含 API Key）存盘，下次启动还在"""
    bl_idname = "blockout.ai_save_prefs"
    bl_label = "保存设置"

    def execute(self, context):
        try:
            bpy.ops.wm.save_userpref()
        except Exception as e:                  # noqa: BLE001
            self.report({"ERROR"}, "保存失败：%s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "已保存，下次启动不用重填")
        return {"FINISHED"}


class BO_OT_ai_dsh_test(bpy.types.Operator):
    """测一次 DSH 通道（不需要 API Key）"""
    bl_idname = "blockout.ai_dsh_test"
    bl_label = "测试 DSH 通道"

    def execute(self, context):
        entry = context.preferences.addons.get("blockout_helper")
        if not entry:
            self.report({"ERROR"}, "读不到偏好设置")
            return {"CANCELLED"}
        p = entry.preferences

        from . import dsh_client
        ok, msg = dsh_client.self_test(p.dsh_port)
        p.test_status = ("✓ " if ok else "✗ ") + msg
        debuglog.write("DSH 自检 ok=%s: %s" % (ok, msg[:200]))
        if ok:
            self.report({"INFO"}, "DSH 通道正常")
            return {"FINISHED"}
        self.report({"ERROR"}, msg[:120])
        return {"CANCELLED"}


class BO_OT_ai_test(bpy.types.Operator):
    """测一次最简请求，验证 key / base_url / model 是否可用"""

    bl_idname = "blockout.ai_test"
    bl_label = "测试连接"

    def execute(self, context):
        prefs = context.preferences.addons.get("blockout_helper")
        if not prefs:
            self.report({"ERROR"}, "偏好设置读不到")
            return {"CANCELLED"}
        p = prefs.preferences
        if not p.api_key and p.provider != "ollama":
            p.test_status = "✗ 还没填 API Key"
            return {"CANCELLED"}

        debuglog.banner("测试连接 provider=%s model=%s key=%s"
                        % (p.provider, p.model, debuglog.redact_key(p.api_key)))
        ok, msg = api_client.test_connection(
            p.provider, p.api_key, p.model, p.base_url)
        p.test_status = ("✓ 连接正常：%s" % msg) if ok else ("✗ %s" % msg)
        debuglog.write("测试连接结果 ok=%s: %s" % (ok, str(msg)[:200]))
        if ok:
            self.report({"INFO"}, "连接正常")
            return {"FINISHED"}
        self.report({"ERROR"}, msg[:120])
        return {"CANCELLED"}


# ═══════════════════════════════════════════════════════════════════
# 场景属性
# ═══════════════════════════════════════════════════════════════════

class AILine(bpy.types.PropertyGroup):
    level: bpy.props.StringProperty(default="info")
    text: bpy.props.StringProperty(default="")


class AISessionItem(bpy.types.PropertyGroup):
    sid: bpy.props.StringProperty(default="")
    title: bpy.props.StringProperty(default="")
    created: bpy.props.StringProperty(default="")
    count: bpy.props.IntProperty(default=0)


class AIAnswerItem(bpy.types.PropertyGroup):
    """每个会话的最后一条答案，存在 **Blender 属性** 里。

    ⚠ 为什么不用 Python dict：模块被重载/重装时 dict 直接没了，
    而面板上的会话列表（也是 Blender 属性）还在 —— 结果就是
    "列表里有会话，点进去却什么都没有"。答案必须和列表同生共死。
    """
    sid: bpy.props.StringProperty(default="")
    text: bpy.props.StringProperty(default="")


class BlockoutAIState(bpy.types.PropertyGroup):
    input_text: bpy.props.StringProperty(
        name="输入", default="", description="输入需求，回车或点发送")

    lines: bpy.props.CollectionProperty(type=AILine)
    sessions: bpy.props.CollectionProperty(type=AISessionItem)
    session_index: bpy.props.IntProperty(default=0)
    # ★ 答案也存成 Blender 集合：跟会话列表同生共死，插件重载不会丢
    answers: bpy.props.CollectionProperty(type=AIAnswerItem)

    active_sid: bpy.props.StringProperty(default="")
    status: bpy.props.StringProperty(default="就绪")

    # ★ 答案的直接载体：不经过集合操作，简单赋值，绝不失败
    last_answer: bpy.props.StringProperty(default="")
    # ★ 这条答案属于哪个会话。跟 last_answer 配对使用：
    #   面板据此判断「这条是当前会话的」还是「别的会话的（要提示切换）」
    last_answer_sid: bpy.props.StringProperty(default="")
    last_error: bpy.props.StringProperty(default="")
    # ★ 心跳：每次 tick +1，用来确认 timer 在跑、面板在重绘
    heartbeat: bpy.props.IntProperty(default=0)
    tick_time: bpy.props.StringProperty(default="")

    # ★ 对话记录的滚动与放大
    scroll_top: bpy.props.IntProperty(default=0)
    view_lines: bpy.props.IntProperty(default=12, min=3, max=60)
    expanded: bpy.props.BoolProperty(default=False)
    show_debug: bpy.props.BoolProperty(default=False)
    #: 流式接收中的原文（增量累积）。中断时用它保住已生成的部分。
    live_text: bpy.props.StringProperty(default="")

    busy: bpy.props.BoolProperty(default=False)
    #: 权限级别（三档，跟 DSH 那种会话管理一样）
    #:   ask   = 破坏性操作先问我（默认，最安全）
    #:   auto  = 白名单内的直接干，只弹真正危险的
    #:   full  = 全部放行，不再询问（自己负责）
    permission: bpy.props.EnumProperty(
        name="权限",
        items=[
            ("ask", "询问", "删除/执行 Python 等操作前先问我"),
            ("auto", "自动", "常规破坏性操作直接执行，只拦最危险的"),
            ("full", "完全", "全部放行，不再任何询问"),
        ],
        default="ask",
    )
    auto_allow: bpy.props.BoolProperty(
        name="自动允许破坏性操作", default=False,
        description="旧版开关，等价于把权限设成「完全」")


# ═══════════════════════════════════════════════════════════════════
# 事件缓冲：后台线程 → 主线程 → draw
# ═══════════════════════════════════════════════════════════════════

_PENDING = []
_PENDING_LOCK = None
_IN_POLL = False
#: draw 里发现「列表想切到某条」，交给 timer 真正执行
_PENDING_SWITCH = ""


def _ensure_lock():
    global _PENDING_LOCK
    if _PENDING_LOCK is None:
        _PENDING_LOCK = threading.Lock()
    return _PENDING_LOCK


def post_event(level, text, sid=None):
    """任意线程可调用：把一条消息排进 UI 缓冲。

    sid 是**发出这条事件的会话**。面板只消费 active_sid 的事件——
    否则后台还在跑的旧会话会把答案写进你正看着的另一个会话。
    """
    lock = _ensure_lock()
    with lock:
        _PENDING.append((level, str(text), sid))


# ═══════════════════════════════════════════════════════════════════
# 事件应用（每个事件独立 try/except，一个失败不影响其它）
# ═══════════════════════════════════════════════════════════════════

_MAX_LINES = 300


def _append_line(st, level, text):
    """加一行。只做 add()，**从不 remove()**。"""
    if len(st.lines) >= _MAX_LINES:
        # 到上限就整体清空重建，避免逐条 remove 引发索引失效
        keep = [(st.lines[i].level, st.lines[i].text)
                for i in range(_MAX_LINES // 2, len(st.lines))]
        st.lines.clear()
        for lv, tx in keep:
            ln = st.lines.add()
            ln.level = lv
            ln.text = tx
    ln = st.lines.add()
    ln.level = level
    ln.text = text[:16000]


def _apply_event(st, level, text, sid=None):
    """处理一条事件。任何异常都不往外抛。

    ★ 关键约定：
      - **答案类事件（final / say / error）无论属于哪个会话，一律先记账**
        （写进 _ANSWERS[会话]）。这样答案永远不会丢，切回去就有。
      - **只有属于当前会话的**才同时写进面板属性 st.last_answer。
      - 过程类事件（think / raw / stream）只对当前会话生效，
        免得别的会话的进度条在这边乱闪。
    """
    try:
        # 事件自带的 sid 优先（它一定是对的），没有才退回 active_sid
        owner = sid or st.active_sid
        is_current = (not owner) or (not st.active_sid) or (owner == st.active_sid)

        if level == "live_reset":
            if is_current:
                st.live_text = ""
            return

        if level == "stream":
            # ★ on_delta 给的是**增量**，必须累积。
            #   中断时已经生成的部分就能留下来，不会白跑。
            if is_current:
                cur = st.live_text or ""
                # 新一轮开始时 agent 会先发 think，这里用长度判断防重复拼接
                if len(text) > 40 and cur.endswith(text):
                    pass                      # 已经是完整快照，直接用
                else:
                    st.live_text = (cur + text)[-16000:]
                st.status = "接收中… %d 字" % len(st.live_text)
            return

        if level == "reason":
            if is_current:
                st.status = "思考中…"
            return

        if level == "final" or level == "say":
            # ★ 先记账：不管当前看的是哪个会话
            if owner:
                _ans_set(owner, text)
                _remember_answer_sid(owner)
            if is_current:
                st.last_answer = text[:16000]
                st.last_answer_sid = owner
                _append_line(st, "say", text)
                st.status = "已回复"
            return

        if level == "error":
            if owner:
                _ans_set(owner, text)
                _remember_answer_sid(owner)
            if is_current:
                st.last_error = text[:1000]
                st.last_answer = text[:16000]
                st.last_answer_sid = owner
                _append_line(st, "error", text)
            return

        if not is_current:
            # 别的会话的 think/raw/tool/result —— 不动当前面板
            return

        if level == "think":
            st.status = text[:80]
            _append_line(st, "think", text)
            return

        _append_line(st, level, text)
    except Exception as e:                       # noqa: BLE001
        # 记录到日志，并尽量在面板上留痕（用最简单的方式）
        try:
            debuglog.write("_apply_event(%s) 失败: %s: %s"
                           % (level, type(e).__name__, e))
        except Exception:
            pass


def _poll_body(redraw=True):
    """主线程：把缓冲刷进属性。整个函数不抛异常。

    redraw=False 用于 **draw() 内部**调用：draw 期间调 tag_redraw()
    可能让 Blender 中断本次绘制，结果就是"数据都对、框就是画不出来"。

    这里是**唯一**写 UI 属性的地方 —— 主线程、有 bpy.context、
    拿得到 blockout_ai。后台线程只往 _PENDING 里塞事件。
    """
    # ★ 防重入：本函数会 tag_redraw()，而 tag_redraw 可能同步触发
    #   面板 draw()，draw 里又会调本函数 —— 不加这层会无限递归。
    global _IN_POLL
    if _IN_POLL:
        return
    _IN_POLL = True
    try:
        _poll_body_inner(redraw)
    finally:
        _IN_POLL = False


def _poll_body_inner(redraw=True):
    lock = _ensure_lock()
    with lock:
        batch = list(_PENDING)
        _PENDING.clear()

    st = getattr(bpy.context.window_manager, "blockout_ai", None)
    if st is None:
        return

    # 心跳 + 时间戳：面板上直接可见，用来判断刷新是否在工作
    st.heartbeat = (st.heartbeat + 1) % 100000
    st.tick_time = time.strftime("%H:%M:%S")

    # ★ 执行 draw 里记下的切换请求（timer 里改数据是安全的，
    #   draw 里改会被 Blender 打断绘制）
    global _PENDING_SWITCH
    if _PENDING_SWITCH:
        target = _PENDING_SWITCH
        _PENDING_SWITCH = ""
        if target in _SESSIONS and target != st.active_sid:
            _switch_to(target)
            return

    changed = bool(batch)

    for item in batch:
        level, text, sid = item
        _apply_event(st, level, text, sid)

    # 只认**当前会话**的 session，避免旧会话的答案串台
    session = _peek_session()

    # ★ 兜底：线程已结束但答案还没进面板 → 从会话自己那儿取
    #   （不只看当前会话：后台跑完的那个会话也要记账，
    #    否则你切走再切回来，答案就没了）
    for sid_k, s in list(_SESSIONS.items()):
        if s.running:
            continue
        saved = (getattr(s, "_last_final_text", "") or "").strip()
        if not saved:
            saved = _session_last_answer(s)
        if saved:
            # 历史为准：只要历史里有，就用历史覆盖记账表
            _ans_set(sid_k, saved)
            _remember_answer_sid(sid_k)

    if session is not None:
        st.busy = bool(session.running)
        saved = _answer_of(session.sid)
        if not session.running and saved and st.last_answer != saved:
            st.last_answer = saved[:16000]
            st.last_answer_sid = session.sid
            st.status = "已回复"
            changed = True
            # 历史里没有对应 say 行才补，避免重复
            has_say = any(st.lines[i].level == "say"
                          and st.lines[i].text == saved
                          for i in range(len(st.lines)))
            if not has_say:
                _append_line(st, "say", saved)
        elif (not session.running and not saved
              and session.last_error and not st.last_answer):
            msg = "【请求失败】" + session.last_error
            st.last_answer = msg[:16000]
            st.last_answer_sid = session.sid
            st.status = "出错"
            changed = True
    else:
        st.busy = False

    # 只有内容真变了才重绘，否则每次 tag_redraw 都在制造新的一轮 draw
    if changed and redraw:
        _tag_redraw()


def _tag_redraw():
    try:
        for win in bpy.context.window_manager.windows:
            scr = win.screen
            if scr:
                for area in scr.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
    except Exception as e:                       # noqa: BLE001
        debuglog.write("tag_redraw 失败: %s" % e)

def _poll():
    """timer 入口。绝不能抛异常——抛了 timer 会被注销。"""
    try:
        _poll_body()
    except Exception:                            # noqa: BLE001
        import traceback
        debuglog.write("_poll 异常:\n" + traceback.format_exc()[-1500:])
        traceback.print_exc()
    try:
        if not bpy.app.timers.is_registered(_poll):
            bpy.app.timers.register(_poll, first_interval=0.2,
                                    persistent=True)
    except (ValueError, RuntimeError):
        pass
    return 0.2


def _ensure_poll():
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=0.1, persistent=True)


def _stop_poll():
    try:
        if bpy.app.timers.is_registered(_poll):
            bpy.app.timers.unregister(_poll)
    except (ValueError, RuntimeError):
        pass


# ═══════════════════════════════════════════════════════════════════
# 会话管理
# ═══════════════════════════════════════════════════════════════════

_SESSIONS = {}

#: 每个会话的最后一条答案的**内存镜像**（真正存储是 st.answers 这个
#: Blender 集合）。这里只做快速查询缓存，来源永远是属性。
#: ⚠ 别把它当唯一来源：模块重载会清空它。
_ANSWERS = {}

#: 最近一次产生答案的会话（用来给「别的会话有新回答」提示定位）
_LAST_ANSWER_SID = ""


def _state():
    return getattr(bpy.context.window_manager, "blockout_ai", None)


def _ans_get(sid):
    """读答案：**先**Blender 属性（跨重载可靠），再退回内存镜像。"""
    if not sid:
        return ""
    st = _state()
    if st is not None:
        for i in range(len(st.answers)):
            it = st.answers[i]
            if it.sid == sid and it.text:
                return it.text
    return (_ANSWERS.get(sid) or "")


def _ans_set(sid, text):
    """写答案：属性 + 镜像都写。"""
    if not sid:
        return
    text = (text or "")[:16000]
    _ANSWERS[sid] = text
    st = _state()
    if st is None:
        return
    for i in range(len(st.answers)):
        if st.answers[i].sid == sid:
            st.answers[i].text = text
            return
    it = st.answers.add()
    it.sid = sid
    it.text = text


def _ans_clear(sid):
    _ANSWERS.pop(sid, None)
    st = _state()
    if st is None:
        return
    for i in range(len(st.answers) - 1, -1, -1):
        if st.answers[i].sid == sid:
            st.answers.remove(i)
            return


def _remember_answer_sid(sid):
    global _LAST_ANSWER_SID
    _LAST_ANSWER_SID = sid


def _new_session():
    s = agent.Session()
    s.on_event = post_event
    _SESSIONS[s.sid] = s
    return s


def _peek_session():
    """只看不建：返回当前 active_sid 对应的会话，没有就 None。

    ⚠ draw() 必须用这个。draw 里新建会话会让每次重绘都造出一个
    幽灵会话（实测：面板列表里多出「会话 2 / 会话 3」，答案却
    留在真正的会话里，看起来像回复丢了）。
    """
    st = getattr(bpy.context.window_manager, "blockout_ai", None)
    sid = st.active_sid if st else ""
    if not sid:
        return None
    s = _SESSIONS.get(sid)
    if s is not None:
        s.on_event = post_event
    return s


def _get_session():
    """取当前会话；没有就建一个。**只允许在 operator 里调用。**"""
    st = getattr(bpy.context.window_manager, "blockout_ai", None)
    s = _peek_session()
    if s is not None:
        return s
    s = _new_session()
    if st is not None:
        st.active_sid = s.sid
        _sync_session_list()
    bpy.app.driver_namespace["_bo_session"] = s
    return s


def _sync_session_list():
    st = getattr(bpy.context.window_manager, "blockout_ai", None)
    if st is None:
        return
    try:
        st.sessions.clear()
        for sid, s in sorted(_SESSIONS.items(), key=lambda kv: kv[1].created):
            item = st.sessions.add()
            item.sid = s.sid
            item.title = s.title
            item.created = s.created
            item.count = s.message_count()
    except Exception as e:                       # noqa: BLE001
        debuglog.write("_sync_session_list 失败: %s" % e)


def _auto_switch_on_select(st):
    """列表选中项变了就自动切会话。

    ⚠ draw 里**不能**直接改集合（st.lines.clear 之类），Blender 会中断
      本次绘制 —— 这正是"面板画一半就没了"的原因之一。
      所以这里只把「想切到哪条」记下来，交给 operator 做真正切换。
    """
    try:
        idx = st.session_index
        if not (0 <= idx < len(st.sessions)):
            return
        sid = st.sessions[idx].sid
        if not sid or sid == st.active_sid:
            return
        if sid not in _SESSIONS:
            return
        global _PENDING_SWITCH
        _PENDING_SWITCH = sid
    except Exception as e:                      # noqa: BLE001
        debuglog.write("_auto_switch_on_select 失败: %s" % e)


def _qa_rows(s):
    """只取**问答**（user / assistant 有文字的），过程信息一律不要。

    N 面板很窄。工具调用、STREAM POST、心跳这些会占掉真正的回答，
    用户根本读不到内容。要看过程有 ai_debug.log。
    """
    rows = []
    if s is None:
        return rows
    for m in s.messages:
        role = m.get("role")
        if role == "user":
            c = (m.get("content") or "").strip()
            if c:
                rows.append(("user", c))
        elif role == "assistant":
            c = (m.get("content") or "").strip()
            if c:
                rows.append(("say", c))
    return rows


def _history_rows(s):
    """把会话历史整理成 [(level, text)] 供面板渲染。

    这是面板显示的**唯一可靠来源**：session.messages 是纯 Python 列表，
    不会被 Blender 的属性系统重建掉。
    """
    rows = []
    if s is None:
        return rows
    for m in s.messages:
        role = m.get("role")
        if role == "system":
            continue
        c = (m.get("content") or "").strip()
        if role == "user":
            rows.append(("user", c))
        elif role == "assistant":
            if c:
                rows.append(("say", c))
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                rows.append(("tool", "调用 %s" % fn.get("name", "?")))
        elif role == "tool":
            rows.append(("result", str(m.get("content") or "")[:200]))
    return rows


def _any_text_in_history(s):
    """兜底：历史里任何一条 assistant 的文本，取**最后**一条非空的。

    和 _session_last_answer 的区别：这个会看 tool 消息之后的全部内容，
    连 reasoning 抢救出来的文本也算。只用在面板最后一层兜底。
    """
    if s is None:
        return ""
    best = ""
    for m in s.messages:
        if m.get("role") != "assistant":
            continue
        c = (m.get("content") or "").strip()
        if c:
            best = c
    if best:
        return best
    last = (getattr(s, "_last_final_text", "") or "").strip()
    return last


def _answer_of(sid):
    """取某会话的答案。**纯函数**：只看 sid，不看当前 active_sid。

    优先级：
      1. 会话历史里的文本（真答案，最可信）
      2. session._last_final_text（同步记录，含空回复/失败等告示文案）
      3. Blender 属性里的答案（跨重载可靠）+ 内存镜像
    """
    if not sid:
        return ""
    s = _SESSIONS.get(sid)

    hist = _session_last_answer(s)
    if hist:
        return hist

    # 历史里没文本 → 用 session 的同步记录（这是「用了工具但没文本」的救兵）
    if s is not None:
        last = (getattr(s, "_last_final_text", "") or "").strip()
        if last:
            return last

    return (_ans_get(sid) or "").strip()


def _session_last_answer(s):
    """从会话历史里取最后一条**有内容**的 assistant 文本。

    ⚠ 关键：带 tool_calls 的 assistant 消息 content 通常是空字符串。
    一轮里可能连着好几条空 assistant（每次调工具一条），
    真正的回答在**更靠前**（或更靠后）那条有文本的消息上。
    所以这里必须跳过空 content 往前找，不能只看最后一条。
    """
    if s is None:
        return ""
    for m in reversed(s.messages):
        if m.get("role") != "assistant":
            continue
        c = (m.get("content") or "").strip()
        if c:
            return c
    return ""


def _select_in_list(sid):
    """把列表的 session_index 挪到 sid 上，让高亮跟 active_sid 一致。"""
    st = getattr(bpy.context.window_manager, "blockout_ai", None)
    if st is None:
        return
    for i in range(len(st.sessions)):
        if st.sessions[i].sid == sid:
            st.session_index = i
            return


def _switch_to(sid):
    s = _SESSIONS.get(sid)
    if s is None:
        return False
    st = bpy.context.window_manager.blockout_ai
    st.active_sid = sid
    bpy.app.driver_namespace["_bo_session"] = s

    # ★ 让列表高亮跟 active_sid 一致。
    #   之前点「新建」后 active_sid=s2，列表却还高亮 s1，
    #   面板顶部写「会话 2」、列表亮「你好」—— 状态自相矛盾，
    #   用户看到的是「答案在一个没有答案的会话里」。
    _select_in_list(sid)

    st.lines.clear()
    st.last_error = ""

    # 重放历史
    for m in s.messages:
        if m["role"] == "system":
            continue
        if m["role"] == "user":
            _append_line(st, "user", m.get("content") or "")
        elif m["role"] == "assistant":
            c = (m.get("content") or "").strip()
            if c:
                _append_line(st, "say", c)
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                _append_line(st, "tool", "调用 %s" % fn.get("name", "?"))
        elif m["role"] == "tool":
            _append_line(st, "result", str(m.get("content") or "")[:300])

    # ★ 从**该会话自己的**答案表恢复（不是从当前槽位猜）
    st.last_answer = _answer_of(sid)[:16000]
    st.last_answer_sid = sid if st.last_answer else ""
    if st.last_answer:
        st.status = "已回复"
    else:
        st.status = "已切换到 %s（%d 条）" % (s.title, s.message_count())
    return True


class BO_OT_ai_new_session(bpy.types.Operator):
    """新建一个会话"""
    bl_idname = "blockout.ai_new_session"
    bl_label = "新会话"

    def execute(self, context):
        s = _new_session()
        _sync_session_list()
        _switch_to(s.sid)
        self.report({"INFO"}, "已新建 %s" % s.title)
        return {"FINISHED"}


class BO_OT_ai_switch_session(bpy.types.Operator):
    """切换到选中的会话"""
    bl_idname = "blockout.ai_switch_session"
    bl_label = "切换"

    sid: bpy.props.StringProperty()

    def execute(self, context):
        st = context.window_manager.blockout_ai
        sid = self.sid
        if not sid:
            idx = st.session_index
            if 0 <= idx < len(st.sessions):
                sid = st.sessions[idx].sid
        if not sid:
            self.report({"WARNING"}, "没选中会话")
            return {"CANCELLED"}
        if _switch_to(sid):
            return {"FINISHED"}
        self.report({"ERROR"}, "会话不存在")
        return {"CANCELLED"}


class BO_OT_ai_delete_session(bpy.types.Operator):
    """删除会话（运行中不能删）"""
    bl_idname = "blockout.ai_delete_session"
    bl_label = "删除"

    sid: bpy.props.StringProperty()

    def execute(self, context):
        st = context.window_manager.blockout_ai
        sid = self.sid
        if not sid:
            idx = st.session_index
            if 0 <= idx < len(st.sessions):
                sid = st.sessions[idx].sid
        if not sid:
            self.report({"WARNING"}, "没选中会话")
            return {"CANCELLED"}

        s = _SESSIONS.get(sid)
        if s is None:
            self.report({"ERROR"}, "会话不存在")
            return {"CANCELLED"}
        if s.running:
            self.report({"ERROR"}, "会话正在运行，先点停止")
            return {"CANCELLED"}

        del _SESSIONS[sid]
        if st.active_sid == sid:
            st.active_sid = ""
            st.lines.clear()
            st.last_answer = ""
            st.last_answer_sid = ""
            if _SESSIONS:
                _switch_to(sorted(_SESSIONS.values(),
                                  key=lambda x: x.created)[-1].sid)
            else:
                _get_session()
        _sync_session_list()
        self.report({"INFO"}, "已删除")
        return {"FINISHED"}


class BO_OT_ai_rename_session(bpy.types.Operator):
    """重命名当前会话"""
    bl_idname = "blockout.ai_rename_session"
    bl_label = "重命名"

    new_title: bpy.props.StringProperty(name="标题")

    def invoke(self, context, event):
        s = _get_session()
        self.new_title = s.title
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        s = _get_session()
        s.title = (self.new_title or "").strip() or s.title
        _sync_session_list()
        return {"FINISHED"}


class BO_OT_ai_clear(bpy.types.Operator):
    """清空**当前会话**的消息（保留会话本身）"""
    bl_idname = "blockout.ai_clear"
    bl_label = "清空当前会话消息"

    def execute(self, context):
        st = context.window_manager.blockout_ai
        st.lines.clear()
        st.last_answer = ""
        st.last_answer_sid = ""
        st.last_error = ""
        s = _get_session()
        s.reset()
        # ★ 记账表也要清：reset 之后历史空了，旧答案不能再冒出来
        _ans_clear(s.sid)
        global _LAST_ANSWER_SID
        if _LAST_ANSWER_SID == s.sid:
            _LAST_ANSWER_SID = ""
        _sync_session_list()
        self.report({"INFO"}, "已清空当前会话消息")
        return {"FINISHED"}


class BO_OT_ai_open_log(bpy.types.Operator):
    """打开 AI 调试日志"""
    bl_idname = "blockout.ai_open_log"
    bl_label = "查看 AI 日志"

    def execute(self, context):
        p = debuglog.log_path()
        if not os.path.exists(p):
            self.report({"WARNING"}, "还没有日志：%s" % p)
            return {"CANCELLED"}
        try:
            bpy.ops.wm.path_open(filepath=p)
        except (RuntimeError, AttributeError):
            self.report({"INFO"}, p)
        return {"FINISHED"}


class BO_UL_ai_sessions(bpy.types.UIList):
    """会话列表

    ★ 单击选中即切换（不用再点「切换」按钮）。
    之前要"选中 + 点切换"两步，列表高亮和 active_sid 还可能不同步，
    用户点半天切不过去 —— 现在点哪条就是哪条。
    """

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        s = _SESSIONS.get(item.sid)
        running = bool(s and s.running)
        st = getattr(context.window_manager, "blockout_ai", None)
        is_active = bool(st and st.active_sid == item.sid)

        # 当前会话用不同的图标，一眼看出在哪条
        ico = "SORTTIME" if running else ("LAYER_ACTIVE" if is_active else "DOT")
        row.label(text="", icon=ico)
        row.label(text=item.title[:16])
        row.label(text="%d" % item.count)

    def invoke(self, context, event):
        """点一下（或双击）就切到那条会话。"""
        try:
            st = context.window_manager.blockout_ai
            idx = st.session_index
            if 0 <= idx < len(st.sessions):
                sid = st.sessions[idx].sid
                if sid and sid != st.active_sid:
                    _switch_to(sid)
        except Exception as e:                  # noqa: BLE001
            debuglog.write("UIList invoke 失败: %s" % e)
        return {"PASS_THROUGH"}


# ═══════════════════════════════════════════════════════════════════
# 发送 / 取消 / 确认
# ═══════════════════════════════════════════════════════════════════

class BO_OT_ai_send(bpy.types.Operator):
    """把输入框内容发给 AI"""
    bl_idname = "blockout.ai_send"
    bl_label = "发送"

    def execute(self, context):
        st = context.window_manager.blockout_ai
        text = (st.input_text or "").strip()
        if not text:
            self.report({"WARNING"}, "输入为空")
            return {"CANCELLED"}

        entry = context.preferences.addons.get("blockout_helper")
        if not entry:
            self.report({"ERROR"}, "读不到偏好设置")
            return {"CANCELLED"}
        p = entry.preferences
        # 走 DSH 时不检查 key —— 那是 DSH 的事
        if p.backend != "dsh" and not p.api_key and p.provider != "ollama":
            self.report({"ERROR"}, "先在偏好设置里填 API Key")
            return {"CANCELLED"}

        session = _get_session()
        if session.running:
            self.report({"WARNING"}, "上一轮还没结束")
            return {"CANCELLED"}

        session.permission = st.permission
        session.auto_allow = (st.permission == agent.PERM_FULL)
        if session.message_count() == 0:
            session.title = text[:20] or session.title

        session.add_user(text)
        st.input_text = ""
        st.busy = True
        st.last_answer = ""
        st.last_answer_sid = ""
        st.last_error = ""
        _append_line(st, "user", text)
        st.status = "已发送，等待模型…"
        _sync_session_list()

        # ⚠ 把偏好设置的值先拷成普通变量再交给后台线程。
        # 后台线程读 bpy.context / prefs 是非线程安全的。
        if p.backend == "dsh":
            # 走 DSH：不需要 key，凭据和模型都在 DSH 那边
            port = int(p.dsh_port)
            t = threading.Thread(
                target=agent.run_turn_via_dsh,
                args=(session, port, text),
                daemon=True)
            session.thread = t
            t.start()
        else:
            agent.start(session, p.provider, p.api_key, p.model, p.temperature)
        return {"FINISHED"}


class BO_OT_ai_cancel(bpy.types.Operator):
    """中断当前请求：立刻停止生成，已收到的内容保留"""
    bl_idname = "blockout.ai_cancel"
    bl_label = "中断"

    #: True = 没在运行时点它，只给个轻提示，不报错
    quiet: bpy.props.BoolProperty(default=True)

    def execute(self, context):
        st = context.window_manager.blockout_ai
        session = _peek_session()
        if session is None or not session.running:
            # 没在跑：把按钮当"停止并保留"用，不弹错误
            post_event("info", "当前没有正在进行的请求")
            st.status = "就绪"
            return {"CANCELLED"}

        # ★ 先停网络：cancel() 会 raise_if_cancelled 打断流式循环
        try:
            agent.cancel(session)
        except Exception as e:                  # noqa: BLE001
            debuglog.write("cancel 失败: %s" % e)

        # ★ 已经收到的内容要保住：中断不等于把回答丢掉
        live = (st.live_text or "").strip()
        if live:
            kept = "【已中断】以下是中断前已生成的内容：\n" + live
            session.messages.append({"role": "assistant", "content": kept})
            session._last_final_text = kept
            _ans_set(session.sid, kept)
            st.last_answer = kept[:16000]
            st.last_answer_sid = session.sid
            st.status = "已中断（保留 %d 字）" % len(live)
            post_event("warn", "已中断，保留已生成的 %d 字" % len(live))
        else:
            st.status = "已中断"
            post_event("warn", "已中断本轮生成")

        st.live_text = ""
        # ★ 立刻复位运行标记：后台线程的 finally 还没跑到时，
        #   界面必须马上回到可用状态，否则用户以为还在跑、不敢再发。
        session.running = False
        st.busy = False
        self.report({"INFO"}, "已中断")
        return {"FINISHED"}


class BO_OT_ai_confirm(bpy.types.Operator):
    """同意/拒绝执行破坏性操作"""
    bl_idname = "blockout.ai_confirm"
    bl_label = "确认"

    allow: bpy.props.BoolProperty(default=True)

    def execute(self, context):
        session = bpy.app.driver_namespace.get("_bo_session")
        if not session or not session.pending_confirm:
            return {"CANCELLED"}
        name, args, ev, box = session.pending_confirm
        box["ok"] = bool(self.allow)
        ev.set()
        post_event("info", "已%s %s" % ("允许" if self.allow else "拒绝", name))
        return {"FINISHED"}


class BO_OT_ai_quick(bpy.types.Operator):
    """把预设提示词填进输入框"""
    bl_idname = "blockout.ai_quick"
    bl_label = "快捷提问"

    prompt: bpy.props.StringProperty()

    def execute(self, context):
        context.window_manager.blockout_ai.input_text = self.prompt
        return {"FINISHED"}


class BO_OT_ai_scroll(bpy.types.Operator):
    """上下滚动对话记录"""
    bl_idname = "blockout.ai_scroll"
    bl_label = "滚动"
    bl_options = {"REGISTER", "UNDO"}

    delta: bpy.props.IntProperty(default=0)

    def execute(self, context):
        st = context.window_manager.blockout_ai
        st.scroll_top = max(0, st.scroll_top + self.delta)
        return {"FINISHED"}


def _export_to_text(session):
    """把整段对话写成 Blender 文本块，返回文本名。

    独立成普通函数：算子不能直接构造（bpy_struct 限制），
    测试和算子都调它，逻辑只有一份。
    """
    rows = _qa_rows(session)
    lines = []
    title = session.title if session is not None else "无会话"
    lines.append("# %s · %d 条对话" % (title, len(rows)))
    lines.append("# 这个窗口可以拖动边缘放大、滚动、选中复制")
    lines.append("")
    for lv, tx in rows:
        mark = "你" if lv == "user" else "AI"
        lines.append("【%s】" % mark)
        lines.append(tx)
        lines.append("")

    name = "AI对话_%s" % (session.sid if session else "tmp")
    txt = bpy.data.texts.get(name)
    if txt is None:
        txt = bpy.data.texts.new(name)
    else:
        txt.clear()
    txt.write("\n".join(lines))
    return name, txt


class BO_OT_ai_expand(bpy.types.Operator):
    """放大查看：把完整对话写进 Blender 文本编辑器

    ⚠ 为什么不用 invoke_popup：实测在 N 侧栏的 panel 里调用，
      弹窗要么不出现、要么一闪就没（上下文不对）。用户点了没反应。
      改用文本编辑器：**一定能开、能自由拉伸、能选中复制**，
      而且不依赖任何弹窗上下文。
    """
    bl_idname = "blockout.ai_expand"
    bl_label = "放大查看"
    bl_options = {"REGISTER"}

    def execute(self, context):
        st = context.window_manager.blockout_ai
        session = _peek_session()

        name, txt = _export_to_text(session)

        # 尽量在某个区域里打开它
        opened = False
        try:
            for area in context.screen.areas:
                if area.type == "TEXT_EDITOR":
                    area.spaces.active.text = txt
                    opened = True
                    break
        except Exception as e:                  # noqa: BLE001
            debuglog.write("打开文本编辑器失败: %s" % e)

        if not opened:
            # 没有文本编辑器区域 → 直接把主区域切成文本编辑器
            try:
                area = context.area
                if area is not None and area.type == "VIEW_3D":
                    area.type = "TEXT_EDITOR"
                    area.spaces.active.text = txt
                    opened = True
            except Exception as e:              # noqa: BLE001
                debuglog.write("无法切区域: %s" % e)

        st.status = "已输出到文本：%s" % name
        self.report({"INFO"}, "完整对话已输出到文本编辑器：%s" % name)
        return {"FINISHED"}


# ═══════════════════════════════════════════════════════════════════
# 面板
# ═══════════════════════════════════════════════════════════════════

_ICONS = {
    "user": "USER",
    "say": "TEXT",          # ⚠ MESSAGEBOX 在 4.5 不存在，会让 draw 静默中断
    "tool": "FORWARD",      # ⚠ ARROW_RIGHT 也不存在
    "result": "DOT",
    "error": "ERROR",
    "warn": "INFO",
    "think": "TIME",
    "done": "CHECKMARK",
    "info": "INFO",
    "confirm": "QUESTION",
    "raw": "CONSOLE",
    "reason": "SORT_DESC",
}

_PREFIX = {
    "user": "你：", "say": "AI：", "tool": "› ", "result": "  ",
    "error": "✗ ", "warn": "! ", "think": "· ", "done": "✓ ",
    "confirm": "? ", "raw": "🔍 ", "reason": "  💭 ",
}


class VIEW3D_PT_blockout_ai(bpy.types.Panel):
    """AI 对话：用自然语言改场景、跑白膜链路"""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blockout"
    bl_label = "AI 助手"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        st = context.window_manager.blockout_ai

        # ★★ 每次重绘都自己消费一次事件缓冲。
        #   timer 有可能没在跑（重装插件、timer 被异常顶掉、面板刚注册），
        #   那时事件会永远堆在 _PENDING 里 —— 日志有、UI 没有。
        #   ⚠ redraw=False：draw 期间绝不能 tag_redraw，
        #     否则 Blender 可能中断本次绘制，后半截框就画不出来。
        try:
            _poll_body(redraw=False)
        except Exception:                       # noqa: BLE001
            pass

        # ⚠ 这里**只能用 _peek_session**：draw 会被高频调用，
        #   在里面新建会话会每重绘一次就造一个幽灵会话。
        session = _peek_session()

        # ════════════════════════════════════════════════════════
        # ⚠ draw() 里抛异常时 Blender **完全静默**：只画到出错那行，
        #   不报错不打印，面板看着就像"内容丢了"。
        #   所以每一段都单独包起来：一段坏了，后面的照画。
        # ════════════════════════════════════════════════════════
        draw_error = ""
        try:
            self._draw_sessions(layout, st, session)
        except Exception as e:                  # noqa: BLE001
            draw_error = "会话区: %s: %s" % (type(e).__name__, e)
            debuglog.write("draw 会话区失败: %s" % draw_error)

        try:
            self._draw_answer(layout, st, session)
        except Exception as e:                  # noqa: BLE001
            draw_error = "回答区: %s: %s" % (type(e).__name__, e)
            debuglog.write("draw 回答区失败: %s" % draw_error)

        try:
            self._draw_transcript(layout, st, session)
        except Exception as e:                  # noqa: BLE001
            draw_error = "对话记录: %s: %s" % (type(e).__name__, e)
            debuglog.write("draw 对话记录失败: %s" % draw_error)

        try:
            self._draw_input(layout, st)
        except Exception as e:                  # noqa: BLE001
            draw_error = "输入区: %s: %s" % (type(e).__name__, e)
            debuglog.write("draw 输入区失败: %s" % draw_error)

        try:
            self._draw_debug(layout, st)
        except Exception as e:                  # noqa: BLE001
            draw_error = "信息区: %s: %s" % (type(e).__name__, e)
            debuglog.write("draw 信息区失败: %s" % draw_error)

        # ★ 有异常必须画出来 —— 否则用户只会看到"面板少了一半"
        if draw_error:
            try:
                ebox = layout.box()
                ebox.label(text="面板绘制出错", icon="ERROR")
                for i in range(0, min(len(draw_error), 300), 52):
                    ebox.label(text=draw_error[i:i + 52])
            except Exception:                   # noqa: BLE001
                pass

    # ── 各段单独实现，互不影响 ──
    def _draw_sessions(self, layout, st, session):
        # ── 会话管理 ──
        # ★ 列表选中哪条，就自动切到哪条（不用再点「切换」）。
        #   列表的 index 是 Blender 属性，用户一点就变，这里跟着走。
        _auto_switch_on_select(st)

        box = layout.box()
        top = box.row(align=True)
        if session is not None:
            top.label(text="会话：%s" % session.title, icon="FILE_TEXT")
            top.label(text=session.sid)
        else:
            top.label(text="还没有会话", icon="INFO")

        box.template_list("BO_UL_ai_sessions", "", st, "sessions",
                          st, "session_index", rows=3)

        row = box.row(align=True)
        row.operator("blockout.ai_switch_session", text="切换",
                     icon="FILE_REFRESH")
        row.operator("blockout.ai_new_session", text="新建", icon="ADD")
        row.operator("blockout.ai_rename_session", text="",
                     icon="GREASEPENCIL")
        row.operator("blockout.ai_delete_session", text="", icon="TRASH")

        # ── 运行状态 ──
        #   ★ 中断按钮就在这里，一运行就能看到，不用翻到别处。
        row = layout.row(align=True)
        running = bool(session and session.running)
        if running:
            row.label(text="运行中…", icon="SORTTIME")
            op = row.operator("blockout.ai_cancel", text="中断",
                              icon="CANCEL")
            op.quiet = False
        else:
            row.label(text="就绪", icon="CHECKMARK")
            row.operator("blockout.ai_cancel", text="中断", icon="CANCEL")
        row.label(text="%d 条" % (session.message_count() if session else 0))

        # ── 待确认 ──
        if session and session.pending_confirm:
            name, args, ev, box_obj = session.pending_confirm
            b = layout.box()
            b.label(text="需要确认：%s" % name, icon="QUESTION")
            preview = json.dumps(args, ensure_ascii=False)
            for i in range(0, min(len(preview), 180), 58):
                b.label(text=preview[i:i + 58])
            r = b.row(align=True)
            op = r.operator("blockout.ai_confirm", text="允许", icon="CHECKMARK")
            op.allow = True
            op = r.operator("blockout.ai_confirm", text="拒绝", icon="X")
            op.allow = False

    def _draw_answer(self, layout, st, session):
        # ── 最新回答：只在**没有对话记录**时兜底显示 ──
        #   有历史就直接看下面，避免同一段文字画两遍占地方。
        cur_sid = st.active_sid
        cur = _answer_of(cur_sid) if cur_sid else ""
        if not cur and session is not None:
            cur = _any_text_in_history(session)

        rows = _history_rows(session)
        if not rows and cur:
            abox = layout.box()
            abox.label(text="AI 回答", icon="TEXT")
            txt = cur.replace("\n", " ")
            for i in range(0, min(len(txt), 900), 52):
                abox.label(text=txt[i:i + 52])
        elif not rows:
            # 当前会话没答案 → 找**任何**一个有答案的会话
            other_sid = ""
            other_text = ""
            for sid_k in _SESSIONS:
                if sid_k == cur_sid:
                    continue
                t = _answer_of(sid_k)
                if t:
                    other_sid, other_text = sid_k, t
                    break

            if other_sid:
                other = _SESSIONS.get(other_sid)
                title = other.title if other is not None else other_sid
                b = layout.box()
                b.label(text="另一会话有回答", icon="INFO")
                b.label(text="%s：「%s…」" % (title[:14],
                                            other_text.replace("\n", " ")[:22]))
                op = b.operator("blockout.ai_switch_session",
                                text="去看", icon="FORWARD")
                op.sid = other_sid
            elif session is not None:
                b = layout.box()
                b.label(text="这个会话还没有对话", icon="INFO")
                b.label(text="在下面输入需求后点发送")

        if st.last_error:
            ebox = layout.box()
            ebox.label(text="错误", icon="ERROR")
            txt = st.last_error.replace("\n", " ")
            for i in range(0, min(len(txt), 300), 52):
                ebox.label(text=txt[i:i + 52])

    def _draw_transcript(self, layout, st, session):
        # ── 对话记录：只画问答 ──
        #   过程信息（STREAM POST / 心跳 / 工具日志）一律不画。
        #   N 面板本来就窄，那些东西会把真正的回答挤出去。
        rows = _qa_rows(session)
        if not rows:
            return

        # ★ 滚动：scroll 是 Blender 属性，用户上下滚就变
        n = len(rows)
        view = max(3, st.view_lines)          # 可显示的条数
        max_top = max(0, n - view)
        top = max(0, min(st.scroll_top, max_top))
        if top != st.scroll_top:
            st.scroll_top = top               # 自动夹住越界值

        visible = rows[top:top + view]

        # 工具条：滚动 + 放大
        bar = layout.row(align=True)
        bar.label(text="%d/%d" % (min(top + view, n), n), icon="TEXT")
        op = bar.operator("blockout.ai_scroll", text="", icon="EVENT_UP_ARROW")
        op.delta = -view
        op = bar.operator("blockout.ai_scroll", text="", icon="EVENT_DOWN_ARROW")
        op.delta = view
        bar.operator("blockout.ai_expand", text="放大",
                     icon="FULLSCREEN_ENTER")

        col = layout.column()
        for lv, tx in visible:
            icon = _ICONS.get(lv, "DOT")
            text = tx.replace("\n", " ")
            prefix = _PREFIX.get(lv, "")
            for k in range(0, min(len(text), 720), 52):
                col.label(text=(prefix if k == 0 else "   ") + text[k:k + 52],
                          icon=icon if k == 0 else "BLANK1")

    def _draw_input(self, layout, st):
        # ── 权限：三档，一眼看出现在什么级别 ──
        prow = layout.row(align=True)
        prow.label(text="权限", icon="LOCKED" if st.permission == "ask"
                   else ("UNLOCKED" if st.permission == "full"
                         else "GREASEPENCIL"))
        prow.prop(st, "permission", text="")

        # ── 输入 ──
        layout.separator()
        layout.prop(st, "input_text", text="")
        row = layout.row(align=True)
        row.operator("blockout.ai_send", text="发送", icon="PLAY")
        # ★ 中断就在发送旁边：回答不理想时不用找
        op = row.operator("blockout.ai_cancel", text="中断", icon="CANCEL")
        op.quiet = False
        row.operator("blockout.ai_clear", text="", icon="X")

        # ── 快捷 ──
        b = layout.box()
        b.label(text="快捷", icon="PRESET")
        r = b.row(align=True)
        r.operator("blockout.ai_quick", text="看场景").prompt = "描述当前场景里有什么"
        r.operator("blockout.ai_quick", text="查对象").prompt = "列出所有网格对象"
        r = b.row(align=True)
        r.operator("blockout.ai_quick", text="白膜流程").prompt = (
            "按 import_joints→build_rig→setup_camera→verify_framing 跑一遍白膜")

    def _draw_debug(self, layout, st):
        # ── 排查：默认收起，只占一行 ──
        #   心跳、构建时间、状态这些平时不需要看，展开了才有。
        row = layout.row(align=True)
        row.prop(st, "show_debug", text="",
                 icon="TRIA_DOWN" if st.show_debug else "DISCLOSURE_TRI_RIGHT",
                 emboss=False)
        row.label(text="诊断")
        if st.show_debug:
            b = layout.box()
            bi = _build_info()
            b.label(text="版本 %s · 构建 %s"
                    % (bi.get("version", "?"), bi.get("built", "?")),
                    icon="INFO")
            b.label(text="心跳 %d · %s" % (st.heartbeat, st.tick_time),
                    icon="SORTTIME")
            if st.status:
                b.label(text=st.status[:60], icon="DOT")
            b.operator("blockout.ai_open_log", text="查看 AI 日志", icon="TEXT")
            b.operator("blockout.ai_clear", text="清空当前会话消息", icon="X")


# ═══════════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════════

classes = (
    BlockoutAIPrefs,
    AILine,
    AISessionItem,
    AIAnswerItem,
    BlockoutAIState,
    BO_UL_ai_sessions,
    BO_OT_ai_save_prefs,
    BO_OT_ai_dsh_test,
    BO_OT_ai_test,
    BO_OT_ai_clear,
    BO_OT_ai_send,
    BO_OT_ai_cancel,
    BO_OT_ai_scroll,
    BO_OT_ai_expand,
    BO_OT_ai_confirm,
    BO_OT_ai_quick,
    BO_OT_ai_new_session,
    BO_OT_ai_switch_session,
    BO_OT_ai_delete_session,
    BO_OT_ai_rename_session,
    BO_OT_ai_open_log,
    VIEW3D_PT_blockout_ai,
)


def register():
    # 先清一遍：上次 unregister 万一没走完，残留的类会让 register_class 抛
    # ValueError，然后后面全部注册不上 → 半注册状态（面板在、属性缺）。
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except (RuntimeError, ValueError):
            pass

    for c in classes:
        try:
            bpy.utils.register_class(c)
        except (RuntimeError, ValueError) as e:
            print("[ui_ai] 注册 %s 失败: %s" % (c.__name__, e))

    try:
        bpy.types.WindowManager.blockout_ai = bpy.props.PointerProperty(
            type=BlockoutAIState)
    except (ValueError, RuntimeError) as e:
        print("[ui_ai] blockout_ai 属性注册失败: %s" % e)
    _ensure_poll()


def unregister():
    _stop_poll()

    for s in list(_SESSIONS.values()):
        try:
            if s.running and s.cancel_token:
                s.cancel_token.cancel()
        except Exception:                       # noqa: BLE001
            pass
    _SESSIONS.clear()

    st = getattr(bpy.context.window_manager, "blockout_ai", None)
    if st is not None:
        try:
            st.lines.clear()
            st.sessions.clear()
            st.active_sid = ""
            st.input_text = ""
            st.last_answer = ""
            st.last_answer_sid = ""
            st.last_error = ""
            st.status = "就绪"
            st.busy = False
        except Exception:                       # noqa: BLE001
            pass

    lock = _ensure_lock()
    with lock:
        _PENDING.clear()

    try:
        debuglog.clear()
    except Exception:                           # noqa: BLE001
        pass

    bpy.app.driver_namespace.pop("_bo_session", None)

    if hasattr(bpy.types.WindowManager, "blockout_ai"):
        del bpy.types.WindowManager.blockout_ai
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
