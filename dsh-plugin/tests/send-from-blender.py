#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""send-from-blender.py —— 从 Blender 侧发消息给 DSH 的端到端测试

它测的就是插件面板按「发送」时走的那条路：

    addon/blockout_helper/dsh_client.py  →  HTTP  →  dsh-blender-chat 插件

跑法
----
    # 独立跑（不开 Blender）
    python dsh-plugin/tests/send-from-blender.py

    # 指定端口 / 指定问题
    python dsh-plugin/tests/send-from-blender.py --port 50339 --text "回一个字：好"

    # 在 Blender 里跑（Scripting 工作区 → Text Editor → Run Script）
    #   会自动额外验证 bpy 上下文可用

退出码：0 全通 · 1 有步骤失败 · 2 前置条件不满足
"""
import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CLIENT = os.path.join(ROOT, "addon", "blockout_helper", "dsh_client.py")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PASS, FAIL, SKIP = "[PASS]", "[FAIL]", "[SKIP]"
_failed = []


def load_client():
    """按文件路径加载 dsh_client —— 不依赖 bpy，也不污染 sys.path。"""
    if not os.path.isfile(CLIENT):
        raise SystemExit("%s dsh_client.py 不存在：%s" % (FAIL, CLIENT))
    spec = importlib.util.spec_from_file_location("dsh_client_under_test", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def step(name, ok, detail=""):
    mark = PASS if ok else FAIL
    if not ok:
        _failed.append(name)
    print("%s %-28s %s" % (mark, name, detail))
    return ok


def in_blender():
    try:
        import bpy  # noqa: F401
        return True
    except ImportError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="0 = 自动探测")
    ap.add_argument("--text", default="回一个字：好", help="要发的问题")
    ap.add_argument("--sid", default="", help="会话名，留空则自动生成")
    ap.add_argument("--timeout", type=float, default=180.0, help="等回复的上限秒数")
    ap.add_argument("--verbose", action="store_true", help="打印每一轮 poll 的原始事件")
    args = ap.parse_args()

    print("=" * 62)
    print("Blender → DSH 发消息测试")
    print("=" * 62)

    c = load_client()

    # ---- 1. 端口 ----
    t0 = time.time()
    try:
        port = args.port or c.resolve_port(0)
    except Exception as e:
        step("1 端口解析", False, "%s: %s" % (type(e).__name__, e))
        print("\nDSH 没开？先启动 DSH Desktop，再跑一次。")
        return 2
    if not step("1 端口解析", bool(port), "port=%s (%dms)" % (port, (time.time() - t0) * 1000)):
        return 2

    # ---- 2. 后端在线 ----
    try:
        st = c.status(port)
        ok = isinstance(st, dict) and st.get("ok")
        step("2 插件在线", bool(ok), json.dumps(st, ensure_ascii=False)[:120])
    except Exception as e:
        step("2 插件在线", False, "%s: %s" % (type(e).__name__, e))
        print("\n端口通了但没有 /blender-chat —— 插件没加载或没重启 DSH。")
        return 2

    # ---- 3. 发送 ----
    sid = args.sid or ("blender-test-%d" % (int(time.time() * 1000) % 100000000))
    t0 = time.time()
    try:
        sent = c.send(port, sid, args.text)
        step("3 发送", bool(sent.get("ok")),
             "session=%s attach=%s (%dms)" % (
                 str(sent.get("sessionId"))[:28], sent.get("attachMode"),
                 (time.time() - t0) * 1000))
    except Exception as e:
        step("3 发送", False, "%s: %s" % (type(e).__name__, e))
        return 1

    # ---- 4. 收回复 ----
    print("    等回复…（最多 %.0fs）" % args.timeout)
    deadline = time.time() + args.timeout
    chunks, final_text, last_seq = [], None, 0
    polls = 0
    while time.time() < deadline:
        try:
            r = c.poll(port, sid, since=last_seq)
        except Exception as e:
            print("    poll 出错：%s: %s" % (type(e).__name__, e))
            time.sleep(1.0)
            continue
        polls += 1
        for ev in (r.get("events") or []):
            last_seq = max(last_seq, int(ev.get("seq") or 0))
            kind, text = ev.get("kind"), (ev.get("text") or "")
            if args.verbose:
                print("    ev seq=%s kind=%s %r" % (last_seq, kind, text[:60]))
            if kind in ("delta", "stream", "chunk"):
                chunks.append(text)
            elif kind == "final":
                final_text = text
        if final_text is not None:
            break
        if not r.get("running") and polls > 2:
            break
        time.sleep(0.6)

    elapsed = time.time() - t0
    body = (final_text if final_text is not None else "".join(chunks)).strip()
    if step("4 收到回复", bool(body), "%d polls / %.1fs" % (polls, elapsed)):
        print()
        print("-" * 62)
        print(body[:1500])
        print("-" * 62)

    # ---- 5. 多轮（同一会话） ----
    if body:
        try:
            c.send(port, sid, "再说两个字：收到")
            t1 = time.time()
            second = None
            seq2 = last_seq
            while time.time() - t1 < args.timeout:
                r = c.poll(port, sid, since=seq2)
                for ev in (r.get("events") or []):
                    seq2 = max(seq2, int(ev.get("seq") or 0))
                    if ev.get("kind") == "final":
                        second = ev.get("text")
                if second is not None:
                    break
                time.sleep(0.6)
            step("5 会话多轮", bool(second), repr((second or "")[:40]))
        except Exception as e:
            step("5 会话多轮", False, "%s: %s" % (type(e).__name__, e))

    # ---- 6. Blender 上下文（只在 Blender 里跑时） ----
    if in_blender():
        try:
            import bpy
            name = bpy.context.scene.name if bpy.context.scene else "?"
            step("6 bpy 上下文", True, "scene=%s" % name)
        except Exception as e:
            step("6 bpy 上下文", False, "%s: %s" % (type(e).__name__, e))
    else:
        print("%s 6 bpy 上下文             不在 Blender 里，跳过" % SKIP)

    print()
    if _failed:
        print("%s 失败 %d 项：%s" % (FAIL, len(_failed), ", ".join(_failed)))
        return 1
    print("%s 全通" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
