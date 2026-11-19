"""agent.py —— 对话式 agent：tool-use 循环

流程
----
    用户消息
      ↓
    [后台线程] POST /chat/completions（带 tools 定义）
      ↓
    模型返回 text 或 tool_calls
      ↓
    [主线程 via dispatch] 执行工具，结果作为 tool 消息回填
      ↓
    带着 tool 结果再请求……直到模型给出纯文本回答

关键约束
--------
* **网络在后台线程，bpy 在主线程**。这中间的边界只能是 dispatch。
* **循环上限**：MAX_STEPS 防止模型无限调工具。超了就停并如实报告。
* **破坏性工具要确认**：delete_objects / run_python 默认要用户点同意。
* **历史要瘦身**：长对话会爆 token，保留最近 N 轮 + 一条场景摘要。
"""

import json
import threading
import time

from . import api_client, debuglog, dispatch, tools

#: 单次对话最多允许的「模型↔工具」往返次数
MAX_STEPS = 12

#: 单次 HTTP 请求超时（秒）。
#: 实测 DeepSeek 带 18 个 tool schema 约 8009 字节，正常 2–15 秒返回。
#: 给 90 秒足够；太长会让"卡住"看起来像死了。
REQUEST_TIMEOUT = 90

#: 历史保留轮数（一对 user/assistant 算一轮）
MAX_HISTORY_TURNS = 8

SYSTEM_PROMPT = """你是 Blender 里的建模助手，用户用中文和你交流。

工作原则：
1. **先看再动**：任何操作前先调 get_scene_info 或 list_objects，不要凭空猜场景内容。
2. **不确定 API 就查**：用 get_blender_api 确认属性名，不要凭记忆写。
3. **单位是米**：Blender 场景 1 单位 = 1 米。白模参数里 _mm 后缀的是毫米，注意换算。
4. **破坏性操作先说清楚**：删除、清空、执行任意 Python 之前，用一句话说明你要做什么。
5. **一次一小步**：改完一步就看结果（verify_framing / get_object_info），不要一次写一大段。
6. **失败要如实说**：工具返回 error 就复述原因，不要假装成功。

视频转白膜的完整流程：
    import_joints → build_rig → setup_camera → verify_framing
    → render_sequence → encode_mp4 → export_glb

回答要短。技术结论优先，不要客套。
"""


class Session:
    """一次对话的会话状态（UI 持有它）。"""

    _seq = 0

    def __init__(self, sid=None, title=None):
        Session._seq += 1
        self.sid = sid or ("s%d" % Session._seq)
        self.title = title or ("会话 %d" % Session._seq)
        self.created = time.strftime("%H:%M:%S")
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.running = False
        self.cancel_token = None
        self.thread = None
        self.last_error = None
        self.on_event = None            # 回调(level, text)，UI 用来实时刷状态
        self.auto_allow = False         # 旧字段，保留兼容
        self.permission = "ask"         # ask / auto / full（见 need_confirm）
        self.pending_confirm = None     # (tool_name, args, threading.Event, bool_box)
        self._last_final_text = ""      # 本轮最终答案（兜底写 UI 用）

    def message_count(self):
        """真实对话轮数（不含 system）。"""
        return len([m for m in self.messages if m["role"] != "system"])

    def summary(self):
        return {
            "sid": self.sid,
            "title": self.title,
            "created": self.created,
            "messages": self.message_count(),
            "running": self.running,
        }

    # ── 历史管理 ──
    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text, tool_calls=None):
        msg = {"role": "assistant", "content": text or ""}
        if tool_calls:
            msg["tool_calls"] = [{
                "id": c["id"], "type": "function",
                "function": {"name": c["name"],
                             "arguments": json.dumps(c["arguments"],
                                                     ensure_ascii=False)},
            } for c in tool_calls]
        self.messages.append(msg)
        # ★ 有文本就立刻记成「本会话最后的答案」。
        #   这是**同步写入**，不经过事件队列 —— 即使 UI 那侧事件全丢，
        #   面板兜底也能从这儿读到。带 tool_calls 的空文本不覆盖它。
        if (text or "").strip():
            self._last_final_text = str(text)

    def add_tool_result(self, call_id, payload):
        self.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(payload, ensure_ascii=False)[:6000],
        })

    def trim(self):
        """保留 system + 最近 N 轮，避免 token 爆炸。"""
        sys_msgs = [m for m in self.messages if m["role"] == "system"]
        rest = [m for m in self.messages if m["role"] != "system"]
        if len(rest) <= MAX_HISTORY_TURNS * 2:
            return
        # 从尾部往前数，保证不切断 assistant/tool 配对
        keep = []
        for m in reversed(rest):
            keep.append(m)
            if len([x for x in keep if x["role"] == "user"]) >= MAX_HISTORY_TURNS:
                break
        keep.reverse()
        # 丢掉开头那些没有对应 assistant 的 tool 消息
        while keep and keep[0]["role"] == "tool":
            keep.pop(0)
        self.messages = sys_msgs + keep

    def reset(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.last_error = None

    # ── 事件回传（主线程回调）──
    def emit(self, level, text):
        # 记录最终答案，供 _push_final_to_ui 兜底使用。
        # 事件队列如果没被及时消费，这个字段能救回面板。
        if level == "final":
            self._last_final_text = str(text)
        if self.on_event:
            try:
                # ★ 带上自己的 sid：面板只消费当前会话的事件。
                #   否则切到新会话后，旧会话迟到的 final 会写到新面板上。
                try:
                    self.on_event(level, text, self.sid)
                except TypeError:
                    # 回调还是旧签名（两个参数）—— 兼容，别崩
                    self.on_event(level, text)
            except Exception:                   # noqa: BLE001
                pass


#: 三档权限
#:   ask  —— 破坏性操作一律问
#:   auto —— 常规破坏性操作放行，只拦真正危险的
#:   full —— 全部放行
PERM_ASK = "ask"
PERM_AUTO = "auto"
PERM_FULL = "full"

#: 即使 auto 档也要问的工具（能执行任意代码 / 彻底删数据）
ALWAYS_ASK = {"run_python"}


def need_confirm(session, name):
    """这个工具在当前权限下要不要问用户。

    ⚠ 只看权限，不问「工具有没有破坏性」——调用方已经先判过
      is_destructive。这里负责的是「在破坏性操作内部再分级」。
    """
    perm = getattr(session, "permission", PERM_ASK) or PERM_ASK
    if perm == PERM_FULL:
        return False
    if perm == PERM_AUTO:
        # 自动档：run_python 这种能跑任意代码的仍然问
        return name in ALWAYS_ASK
    return True          # ask 档：都问


def _confirm(session, name, args):
    """向 UI 请求确认。返回 True/False。"""
    if not need_confirm(session, name):
        return True
    ev = threading.Event()
    box = {"ok": False}
    session.pending_confirm = (name, args, ev, box)
    session.emit("confirm", "需要你确认：%s %s" % (name, json.dumps(args, ensure_ascii=False)[:200]))
    # 最多等 120 秒；UI 没响应就当拒绝
    ev.wait(timeout=120)
    session.pending_confirm = None
    return box["ok"]


def run_turn_via_dsh(session, port, text):
    """走 DSH 通道跑一轮。**在后台线程里调用。**

    和 run_turn 的区别：
      * 不碰 provider / api_key —— 凭据在 DSH 那边
      * 工具调用由 DSH 自己按它那套（228 个 op）决定，这边只是传话
      * 仍然把所有事件 emit 给面板，界面体验一致
    """
    from . import dsh_client

    session.running = True
    session.cancel_token = dsh_client.CancelToken() if hasattr(
        dsh_client, "CancelToken") else None
    tok = session.cancel_token
    t0 = time.time()

    debuglog.banner("DSH 通道 sid=%s port=%s" % (session.sid, port))

    try:
        session.emit("think", "已交给 DSH…")

        def _on_delta(chunk):
            session.emit("stream", chunk)

        def _on_final(chunk):
            # DSH 那边给的是最终全文，直接当权威答案
            session.emit("final", chunk)

        def _on_progress(m):
            session.emit("think", str(m)[:80])

        res = dsh_client.run_turn(
            port, session.sid, text,
            cwd="",
            on_delta=_on_delta,
            on_final=_on_final,
            on_progress=_on_progress,
            cancel=tok,
            max_wait=300,
        )

        final_text = (res.get("text") or "").strip()
        if final_text:
            session.add_assistant(final_text)
            session.emit("done", "完成（%.1fs）" % (time.time() - t0))
            return {"ok": True, "text": final_text, "steps": 1}

        err = res.get("error") or "DSH 没有返回内容"
        session.last_error = err
        session.emit("error", err)
        session.emit("final", "【DSH 通道失败】%s" % err[:600])
        return {"ok": False, "text": "", "error": err, "steps": 1}

    except Exception as e:                      # noqa: BLE001
        import traceback
        session.last_error = "%s: %s" % (type(e).__name__, e)
        debuglog.write("DSH 通道异常:\n" + traceback.format_exc()[-1200:])
        session.emit("final", "【DSH 通道异常】%s" % session.last_error[:600])
        return {"ok": False, "text": "", "error": session.last_error}
    finally:
        session.running = False
        debuglog.write("DSH 通道结束 用时=%.1fs" % (time.time() - t0))


def run_turn(session, provider, api_key, model, temperature=0.3):
    """执行一轮完整对话（在后台线程里调用）。

    provider / api_key / model 由调用方从偏好设置读出来传进来 ——
    **不要在后台线程读 bpy.context.preferences**（非线程安全）。
    """
    """执行一轮完整对话（在后台线程里调用）。

    provider / api_key / model 由调用方从偏好设置读出来传进来 ——
    **不要在后台线程读 bpy.context.preferences**（非线程安全）。
    """
    session.running = True
    session.cancel_token = api_client.CancelToken()
    tok = session.cancel_token
    steps = 0
    t0 = time.time()

    debuglog.banner("新对话 sid=%s provider=%s model=%s key=%s"
                    % (session.sid, provider, model,
                       debuglog.redact_key(api_key)))

    try:
        while steps < MAX_STEPS:
            steps += 1
            tok.raise_if_cancelled()
            session.trim()
            session.emit("think", "第 %d 轮：正在请求模型…" % steps)

            schemas = tools.schemas()
            msg = ("第 %d 轮：messages=%d, tools=%d, bytes=%d"
                   % (steps, len(session.messages), len(schemas),
                      len(json.dumps(schemas, ensure_ascii=False).encode())))
            debuglog.write(msg)

            # ⚠ 诊断必须**同时**进 UI 和日志。
            # v1.2.0 只写文件，结果界面上什么都看不到，用户只能看着转圈。
            def _dbg(m, _s=session):
                debuglog.write(m)
                _s.emit("raw", str(m)[:600])

            t_req = time.time()

            # 流式回调：每来一块就刷到面板（这是"能不能实时看到字"的关键）
            session.emit("live_reset", "")      # 新一轮，清空流式缓冲

            def _on_delta(chunk, _s=session):
                _s.emit("stream", chunk)

            def _on_reason(chunk, _s=session):
                _s.emit("reason", chunk)

            def _on_progress(m, _s=session):
                _s.emit("think", m)

            resp = None
            used_stream = False
            try:
                _dbg("开始流式请求…")
                resp = api_client.stream_chat(
                    provider, api_key, model, session.messages,
                    tools=schemas, temperature=temperature,
                    timeout=REQUEST_TIMEOUT, cancel=tok,
                    on_delta=_on_delta, on_reason=_on_reason,
                    on_progress=_on_progress, debug=_dbg)
                used_stream = True
            except api_client.ApiError as e:
                _dbg("流式失败：%s" % str(e)[:400])
                session.emit("warn", "流式请求失败，改用普通请求…")
                resp = None
            except Exception as exc:            # noqa: BLE001
                _dbg("流式异常：%s: %s" % (type(exc).__name__, exc))
                session.emit("warn", "流式异常，改用普通请求…")
                resp = None

            # 流式为空或失败 → 回落到非流式（非流式已验证能通）
            if resp is None or (not resp.get("text") and not resp.get("tool_calls")):
                if used_stream:
                    _dbg("流式无内容（finish=%s, chunks=%s）→ 回落非流式"
                         % (resp.get("raw_finish") if resp else "?",
                            resp.get("stream_chunks") if resp else "?"))
                    session.emit("warn", "流式没收到内容，改用普通请求重试…")
                _dbg("开始普通请求…")
                resp = api_client.chat_once(
                    provider, api_key, model, session.messages,
                    tools=schemas, temperature=temperature,
                    timeout=REQUEST_TIMEOUT, cancel=tok,
                    debug=_dbg)

            session.emit("info", "模型返回（耗时 %.1fs）" % (time.time() - t_req))

            text = resp.get("text") or ""
            calls = resp.get("tool_calls") or []
            debuglog.write("解析结果：text=%d字 calls=%d finish=%s usage=%s"
                           % (len(text), len(calls), resp.get("raw_finish"),
                              resp.get("usage")))
            if text:
                debuglog.write("TEXT >>> %s" % text[:400].replace("\n", " "))

            session.add_assistant(text, calls if calls else None)

            if not text and not calls:
                finish = resp.get("raw_finish") or "?"
                usage = resp.get("usage") or {}
                reason = (resp.get("reasoning") or "").strip()
                if reason:
                    # 内容跑进了 reasoning_content —— 显示出来，别让它白跑
                    msg = "（模型只给了思考内容，没有正式回答）\n" + reason
                else:
                    msg = ("【空回复】模型没返回内容（finish=%s, tokens=%s）。"
                           % (finish, usage.get("total_tokens", "?")))
                # ★ 必须发 final：否则面板上什么框都不出现，
                #   看起来就像「回复丢了」——实际是空回复。
                session.emit("final", msg[:8000])
                # ★ 同时写进历史：面板从 messages 渲染，
                #   不写进去的话这段说明就只活在事件里，划一走就没了。
                if not (text or "").strip():
                    for m in reversed(session.messages):
                        if m.get("role") == "assistant":
                            if not (m.get("content") or "").strip():
                                m["content"] = msg[:8000]
                            break
                session.emit("warn",
                             "空回复（finish=%s, tokens=%s）。"
                             "日志已写到 ai_debug.log，可发给开发者。"
                             % (finish, usage.get("total_tokens", "?")))
                return {"ok": False, "text": "", "steps": steps,
                        "error": "空回复 finish=%s" % finish,
                        "raw": resp.get("raw_all")}

            # 流式已经在面板上逐字显示过了。
            # ⚠ 但仍然要发一个 "final" 事件：流式分块可能因为 progress /
            #   raw 事件穿插而被打断，面板上的行会散掉或互相覆盖。
            #   final 是唯一权威的完整文本，_poll_body 会用它覆盖最后一行。
            if text:
                session.emit("final", text)
            elif calls:
                session.emit("info", "模型请求执行 %d 个工具" % len(calls))
            if not calls:
                session.emit("done", "完成（%d 轮，%.1fs）"
                             % (steps, time.time() - t0))
                return {"ok": True, "text": text, "steps": steps}

            for c in calls:
                tok.raise_if_cancelled()
                name = c["name"]
                args = c.get("arguments") or {}
                debuglog.write("工具调用 %s %s"
                               % (name, json.dumps(args, ensure_ascii=False)[:200]))

                if tools.is_destructive(name):
                    if not _confirm(session, name, args):
                        session.add_tool_result(c["id"], {
                            "error": "用户拒绝执行该操作", "tool": name})
                        session.emit("warn", "已跳过 %s（用户拒绝）" % name)
                        continue

                session.emit("tool", "执行 %s" % name)
                result = dispatch.call_on_main(tools.call, name, args)
                session.add_tool_result(c["id"], result)

                brief = json.dumps(result, ensure_ascii=False)
                debuglog.write("工具结果 %s → %s" % (name, brief[:300]))
                session.emit("result", "%s → %s"
                             % (name, brief[:200] + ("…" if len(brief) > 200 else "")))

        session.emit("warn", "已达单轮上限 %d 步，停止。把需求拆小一点再试。"
                     % MAX_STEPS)
        session.emit("final", "【达到步数上限 %d 步】已停止。请把需求拆得更小。"
                     % MAX_STEPS)
        return {"ok": False, "text": "", "steps": steps,
                "error": "超过 %d 步" % MAX_STEPS}

    except api_client.ApiError as e:
        session.last_error = str(e)
        debuglog.write("ApiError: %s" % str(e)[:800])
        # 错误也要走 final，让面板上有明确一行（不是空白）
        session.emit("error", str(e))
        session.emit("final", "【请求失败】%s" % str(e)[:800])
        return {"ok": False, "error": str(e), "steps": steps}
    except Exception as e:                      # noqa: BLE001
        import traceback
        session.last_error = "%s: %s" % (type(e).__name__, e)
        debuglog.write("异常: %s\n%s" % (session.last_error,
                                        traceback.format_exc()[-1200:]))
        session.emit("error", session.last_error)
        session.emit("final", "【内部错误】%s" % session.last_error[:800])
        traceback.print_exc()
        return {"ok": False, "error": session.last_error, "steps": steps}
    finally:
        session.running = False
        debuglog.write("对话结束 steps=%d 用时=%.1fs"
                       % (steps, time.time() - t0))

        # ★ 最后一道保险：这一轮结束了却**没有任何答案** → 必须给一句话。
        #   否则面板上「就绪」+ 空框，用户完全不知道发生了什么。
        #   （实测形态：模型连调几次工具，每条 assistant 的 content 都空，
        #     然后正常结束 —— 历史里一条文本都没有，面板就是空白。）
        if not session._last_final_text:
            if session.last_error:
                msg = "【请求失败】%s" % session.last_error
            else:
                n = session.message_count()
                msg = ("【没有生成回答】这一轮调了 %d 条消息，模型只发了工具调用、"
                       "没有给出文字回答。\n"
                       "可以直接再问一次，或把需求说得更具体些。" % n)
            session._last_final_text = msg
            session.emit("final", msg)
            # ★ 也写进历史：面板从 messages 渲染，不写进去就看不到
            filled = False
            for m in reversed(session.messages):
                if m.get("role") == "assistant":
                    if not (m.get("content") or "").strip():
                        m["content"] = msg
                        filled = True
                    break
            if not filled:
                session.messages.append({"role": "assistant", "content": msg})
            debuglog.write("兜底答案: %s" % msg[:200].replace("\n", " "))


def _get_ai_state():
    """取 blockout_ai 状态。

    ⚠ 实测在 agent 模块内 `bpy.context.window_manager` 有时拿不到属性
    （同一进程里直接写 getattr 却能拿到）。所以这里**多路兜底**，
    并把每次失败的原因写进日志，避免静默返回 None。
    """
    reasons = []

    try:
        wm = bpy.context.window_manager
        if wm is None:
            reasons.append("ctx.wm=None")
        else:
            st = getattr(wm, "blockout_ai", None)
            if st is not None:
                return st
            reasons.append("ctx.wm 无 blockout_ai")
    except Exception as e:                  # noqa: BLE001
        reasons.append("ctx 异常 %s" % type(e).__name__)

    try:
        for win in bpy.data.window_managers:
            st = getattr(win, "blockout_ai", None)
            if st is not None:
                return st
        reasons.append("data.wm 都无该属性")
    except Exception as e:                  # noqa: BLE001
        reasons.append("data 异常 %s" % type(e).__name__)

    # 第三路：从已注册的类型反查（PointerProperty 挂在 WindowManager 上）
    try:
        wms = list(bpy.data.window_managers)
        if wms:
            st = wms[0].bl_rna.properties.get("blockout_ai")
            reasons.append("rna 有属性=%s" % bool(st))
    except Exception as e:                  # noqa: BLE001
        reasons.append("rna 异常 %s" % type(e).__name__)

    try:
        debuglog.write("_get_ai_state 全失败: %s" % " | ".join(reasons))
    except Exception:
        pass
    return None


def _push_final_to_ui(session):
    """把本轮的最终答案直接写进 UI 状态（主线程）。

    ⚠ 用 dispatch.call_on_main，但**加超时保护**：
    如果主线程忙（渲染中），不能把后台线程永远挂住。
    """
    text = getattr(session, "_last_final_text", "") or ""
    err = session.last_error or ""
    debuglog.write("_push_final_to_ui: text=%d字 err=%r"
                   % (len(text), (err or "")[:60]))
    if not text and not err:
        return

    def _apply():
        st = _get_ai_state()
        debuglog.write("_apply 开始: st=%r text=%d字" % (st, len(text)))
        if st is None:
            debuglog.write("_push_final_to_ui: 拿不到 blockout_ai 状态")
            return
        try:
            if text:
                st.last_answer = text[:16000]
                st.status = "已回复"
            elif err:
                st.last_answer = ("【请求失败】" + err)[:16000]
                st.status = "出错"
            debuglog.write("_apply 完成: last_answer=%r"
                           % str(st.last_answer)[:60])
        except Exception as e:                  # noqa: BLE001
            debuglog.write("_apply 写属性失败: %s: %s" % (type(e).__name__, e))

    try:
        # _timeout 必须是关键字参数（call_on_main 从 **kwargs 里 pop）
        dispatch.call_on_main(_apply, _timeout=10)
    except Exception as e:                      # noqa: BLE001
        debuglog.write("_push_final_to_ui 失败: %s: %s"
                       % (type(e).__name__, e))
        try:
            if dispatch.is_main_thread():
                _apply()
        except Exception:
            pass


def start(session, provider, api_key, model, temperature=0.3):
    """在后台线程启动一轮对话。返回 thread。"""
    if session.running:
        return None
    t = threading.Thread(target=run_turn,
                         args=(session, provider, api_key, model, temperature),
                         daemon=True)
    session.thread = t
    t.start()
    return t


def cancel(session):
    if session.cancel_token:
        session.cancel_token.cancel()
    if session.pending_confirm:
        name, args, ev, box = session.pending_confirm
        box["ok"] = False
        ev.set()
