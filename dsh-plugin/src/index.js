/**
 * dsh-blender-chat — 让 Blender 面板直接借用 DSH 的模型
 *
 * 为什么要有它
 * ------------
 * 用户不想在插件里单独填 API Key：DSH 里已经配好了模型，
 * 而且 DSH 能调 34 个 family / 228 个建模 op（Blender 插件自己调不到）。
 *
 * 数据流
 * ------
 *   Blender N 面板「AI 助手」
 *        ↓ HTTP POST /blender-chat/send
 *   本插件：agents.create() 建会话 → followup(用户消息)
 *        ↓ 监听 agent/assistant-stream 与 assistant/message
 *   把增量文本推回 Blender（Blender 侧轮询 /blender-chat/poll）
 *
 * ⚠ 刻意**不 import** @deepseek-ai/dsh-llm：
 *   实测那些 peer 依赖只在 pnpm-cache/dlx 的临时目录里能解析到，
 *   DSH 主进程加载插件时未必找得到。一旦 import 失败，
 *   Cordis 会静默跳过整个插件 —— 表现就是「装好了但路由 404」。
 *   createUserMessage 本身只是包一层 role/id，自己写等价实现更稳。
 */

export const name = 'blender-chat';
export const inject = ['agents'];

/**
 * 构造一条 user 消息。
 *
 * 等价于 @deepseek-ai/dsh-llm 的 createUserMessage，
 * 但零依赖 —— 见文件头的说明。
 */
function makeUserMessage(text) {
  const id = (globalThis.crypto?.randomUUID?.()
    ?? `um-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  return {
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'user' },
    id,
  };
}

/** 对外 HTTP 前缀 */
const BASE = '/blender-chat';

/** 内存里的会话表：blenderSid -> { agent, handle, sessionId } */
const sessions = new Map();

/** 事件邮箱：sessionId -> [ {seq, kind, text} ]，供 Blender 轮询取走 */
const mailboxes = new Map();

/** 自增序号，保证 Blender 侧不重不漏 */
let seqCounter = 0;

function mailboxOf(sessionId) {
    let box = mailboxes.get(sessionId);
    if (!box) {
        box = [];
        mailboxes.set(sessionId, box);
    }
    return box;
}

function push(sessionId, kind, text) {
    seqCounter += 1;
    mailboxOf(sessionId).push({ seq: seqCounter, kind, text });
    // 防止长期不取把内存撑爆：只保留最近 400 条
    const box = mailboxOf(sessionId);
    if (box.length > 400) box.splice(0, box.length - 400);
}

/** 读请求体（带大小上限，避免被打爆） */
async function readJson(req, maxBytes = 1024 * 1024) {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
        size += chunk.length;
        if (size > maxBytes) throw new Error('request body too large');
        chunks.push(chunk);
    }
    if (size === 0) return {};
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function sendJson(res, status, body) {
    const payload = JSON.stringify(body);
    res.writeHead(status, {
        'content-type': 'application/json; charset=utf-8',
        'cache-control': 'no-store',
    });
    res.end(payload);
}

/** 从 assistant 消息里抽纯文本 */
function textOf(message) {
    const blocks = message?.content;
    if (!Array.isArray(blocks)) return '';
    return blocks
        .filter((b) => b && b.type === 'text' && typeof b.text === 'string')
        .map((b) => b.text)
        .join('');
}

/**
 * 取或建 agent。
 *
 * 语义与 qqbot 一致：一个 Blender 会话对应一个 DSH 会话；
 * 第一次来就 create，之后一直复用，历史自然累积在 DSH 里。
 */
async function ensureAgent(ctx, agents, blenderSid, cwd) {
    const existing = sessions.get(blenderSid);
    if (existing) {
        // ★ 建过就一直复用：历史留在 DSH 里，不重建。
        //   agents.get() 只是"顺带刷新一下引用"，拿不到也没关系 ——
        //   不能用它做复用判据（测试里它返回 undefined，
        //   真机上 agent 也可能因为空闲回收而暂时不在注册表里）。
        try {
            const live = agents.get?.(existing.sessionId);
            if (live) existing.agent = live;
        } catch { /* ignore */ }
        return existing;
    }
    const created = await agents.create({
        meta: {
            cwd: cwd || process.cwd(),
            blenderChat: true,
        },
    });
    const record = {
        sessionId: created.session?.id ?? created.sessionId,
        agent: created.agent,
        handle: created,
    };
    sessions.set(blenderSid, record);
    return record;
}

/**
 * 仅供测试：清空模块级状态。
 * 生产环境不需要 —— 真实 DSH 里一个进程只 apply 一次。
 */
export function __resetForTests() {
    sessions.clear();
    mailboxes.clear();
    seqCounter = 0;
}

export function apply(ctx, config) {
    const logger = ctx.logger ?? console;
    const cfg = config ?? {};
    // ⚠ agents 必须从 ctx 取：inject=['agents'] 只是声明依赖，
    //   真正拿到实例是 ctx.agents（漏了这行会 ReferenceError）。
    const agents = ctx.agents;
    if (!agents) {
        logger.error?.('blender-chat: 没有 agents 服务，插件无法工作');
        return;
    }

    // ── 出站：把 agent 的回复推回邮箱 ──
    ctx.on('agent/assistant-stream', (payload) => {
        try {
            const agent = payload?.agent;
            const rec = findByAgent(agent);
            if (!rec) return;
            const delta = payload?.delta;
            const t = typeof delta === 'string' ? delta : textOf(delta);
            if (t) push(rec.sessionId, 'stream', t);
        } catch (e) {
            logger.warn?.(`blender-chat stream 处理失败: ${e}`);
        }
    });

    ctx.on('agent/assistant/message', (payload) => {
        try {
            const rec = findByAgent(payload?.agent);
            if (!rec) return;
            const t = textOf(payload?.message ?? payload);
            if (t) push(rec.sessionId, 'final', t);
        } catch (e) {
            logger.warn?.(`blender-chat message 处理失败: ${e}`);
        }
    });

    function findByAgent(agent) {
        if (!agent) return undefined;
        for (const rec of sessions.values()) {
            if (rec.agent === agent) return rec;
        }
        return undefined;
    }

    // ── HTTP 路由 ──
    const routes = [
        {
            kind: 'exact',
            path: `${BASE}/send`,
            handler: async (req, res) => {
                if (req.method !== 'POST') {
                    sendJson(res, 405, { error: 'method not allowed' });
                    return;
                }
                let body;
                try {
                    body = await readJson(req);
                } catch (e) {
                    sendJson(res, 400, { error: String(e?.message ?? e) });
                    return;
                }
                const { sid = 'default', text = '', cwd = '' } = body;
                if (!text.trim()) {
                    sendJson(res, 400, { error: 'text 不能为空' });
                    return;
                }
                try {
                    const rec = await ensureAgent(ctx, agents, sid, cwd);
                    const message = makeUserMessage(text);
                    rec.agent.followup(message);
                    sendJson(res, 200, {
                        ok: true,
                        sessionId: rec.sessionId,
                    });
                } catch (e) {
                    logger.error?.(`blender-chat send 失败: ${e}`);
                    sendJson(res, 500, {
                        error: e instanceof Error ? e.message : String(e),
                    });
                }
            },
        },
        {
            kind: 'exact',
            path: `${BASE}/poll`,
            handler: (req, res) => {
                const url = new URL(req.url ?? '', 'http://127.0.0.1');
                const sid = url.searchParams.get('sid') ?? 'default';
                const since = Number(url.searchParams.get('since') ?? '0');
                const rec = sessions.get(sid);
                if (!rec) {
                    sendJson(res, 200, { events: [], running: false });
                    return;
                }
                const box = mailboxOf(rec.sessionId);
                const events = box.filter((e) => e.seq > since);
                // 取走就清，避免无限增长
                if (events.length > 0) {
                    const keep = box.filter((e) => e.seq > since + events.length);
                    mailboxOf(rec.sessionId).length = 0;
                    mailboxOf(rec.sessionId).push(...keep);
                }
                sendJson(res, 200, {
                    events,
                    running: rec.agent.status !== 'idle',
                    sessionId: rec.sessionId,
                });
            },
        },
        {
            kind: 'exact',
            path: `${BASE}/status`,
            handler: (req, res) => {
                const list = [];
                for (const [sid, rec] of sessions) {
                    list.push({
                        sid,
                        sessionId: rec.sessionId,
                        running: rec.agent.status !== 'idle',
                    });
                }
                sendJson(res, 200, { ok: true, sessions: list });
            },
        },
        {
            kind: 'exact',
            path: `${BASE}/reset`,
            handler: async (req, res) => {
                const url = new URL(req.url ?? '', 'http://127.0.0.1');
                const sid = url.searchParams.get('sid') ?? 'default';
                const rec = sessions.get(sid);
                if (rec) {
                    sessions.delete(sid);
                    try {
                        rec.agent.cancel({ kind: 'user' });
                    } catch { /* ignore */ }
                    try {
                        await rec.handle.dispose();
                    } catch { /* ignore */ }
                }
                mailboxes.delete(rec?.sessionId ?? '');
                sendJson(res, 200, { ok: true });
            },
        },
    ];

    ctx.inject(['webServer'], (scoped) => {
        const webServer = scoped.webServer;
        if (webServer === undefined || typeof webServer.register !== 'function') {
            logger.warn?.('blender-chat: 没有 webServer，HTTP 通道不可用');
            return;
        }
        const disposers = routes.map((r) => webServer.register(r));
        logger.info?.(`blender-chat: 已注册 ${routes.length} 条路由，前缀 ${BASE}`);
        return () => {
            for (const d of disposers) d();
            for (const rec of sessions.values()) {
                try { rec.handle.dispose(); } catch { /* ignore */ }
            }
            sessions.clear();
            mailboxes.clear();
        };
    });
}
