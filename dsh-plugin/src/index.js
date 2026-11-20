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
 * 同步取 node 内建模块。
 *
 * 本文件是 ESM（package.json 里 type: module），没有全局 require；
 * 读标题要在同步函数里解 zstd，所以用 process.getBuiltinModule（Node 20+）。
 * 只取内建模块，不会引入任何插件依赖。
 */
function requireSync(id) {
  if (typeof globalThis.process?.getBuiltinModule === 'function') {
    return globalThis.process.getBuiltinModule(id);
  }
  throw new Error('当前 Node 不支持同步取内建模块，标题将不可用');
}

/**
 * 异步版：拿 node 内建模块。
 *
 * 优先 process.getBuiltinModule（同步、无副作用）；
 * 不支持时退回动态 import('node:xxx') —— 后者到处都能用。
 * 所有可能跑在老 Node 上的代码都该用这个。
 */
async function importBuiltin(id) {
  if (typeof globalThis.process?.getBuiltinModule === 'function') {
    return globalThis.process.getBuiltinModule(id);
  }
  return import(id);
}

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

/**
 * 把 `--E-Xiongwq-RobotChat--` 还原成 `E:\Xiongwq\RobotChat`。
 *
 * DSH 用这个规则给每个 cwd 建一个会话目录：去掉首尾的 `--`，
 * 再把 `-` 当作路径分隔符。非盘符路径（如用户目录）也能大致还原，
 * 还原不准时只影响列表里的说明文字，不影响会话 id。
 */
function decodeProjectDir(name) {
    const s = String(name ?? '');
    if (!s.startsWith('--') || !s.endsWith('--')) return null;
    const body = s.slice(2, -2);
    if (!body) return null;
    const parts = body.split('-');
    // 盘符形式：E-Xiongwq-RobotChat → E:\Xiongwq\RobotChat
    if (parts.length > 1 && /^[A-Za-z]$/.test(parts[0])) {
        return `${parts[0]}:\\${parts.slice(1).join('\\')}`;
    }
    return parts.join('\\');
}



/** 对外 HTTP 前缀 */
const BASE = '/blender-chat';

/**
 * 把「用户输入的一段 id」解析成真实存在的 sessionId。
 *
 * 支持完整 id 或唯一前缀（`session-abc` 这种），方便面板里手输。
 * 读的是宿主的 sessionPersistence.stat；没有该服务时返回原样字符串
 * —— 让 agents.resume 自己去报错，比这里静默失败好排查。
 */
async function resolveSessionId(ctx, wanted) {
    const raw = String(wanted ?? '').trim();
    if (!raw) return undefined;
    let persistence;
    try {
        persistence = typeof ctx.get === 'function' ? ctx.get('sessionPersistence') : undefined;
    } catch { return raw; }
    if (!persistence || typeof persistence.stat !== 'function') return raw;

    // 完整 id：存在就直接用
    try {
        if ((await persistence.stat(raw)) !== undefined) return raw;
    } catch { /* 继续按前缀找 */ }

    // 前缀匹配：扫已知会话（list 存在才扫）
    const all = await listSessions(ctx, {});
    const hits = all.filter((s) => s.sessionId.startsWith(raw));
    if (hits.length === 1) return hits[0].sessionId;
    if (hits.length > 1) {
        throw new Error(`前缀 "${raw}" 匹配到 ${hits.length} 个会话，请写更长的 id`);
    }
    return undefined;
}

/**
 * 列出宿主上已有的会话，供 Blender 面板让用户自己挑。
 *
 * 优先用 sessionPersistence 的列举能力；不同 DSH 版本方法名不一致，
 * 这里逐个探测，都不可用就回空数组（面板会提示"列不出来，手动填 id"）。
 */
async function listSessions(ctx, opts = {}) {
    const limit = Number(opts.limit) || 100;
    let persistence;
    try {
        persistence = typeof ctx.get === 'function' ? ctx.get('sessionPersistence') : undefined;
    } catch { return []; }
    if (!persistence) return [];

    const out = [];
    const push = (h) => {
        const id = h?.id ?? h?.sessionId;
        if (!id || out.some((x) => x.sessionId === id)) return;
        out.push({
            sessionId: id,
            title: h?.title ?? null,
            cwd: h?.cwd ?? null,
            createdAt: h?.createdAt ?? null,
            agentPreset: h?.agentPreset ?? null,
        });
    };

    // 形态 1：list() 直接返回 header 数组
    for (const name of ['list', 'listSessions', 'listHeaders']) {
        if (typeof persistence[name] !== 'function') continue;
        try {
            const res = await persistence[name]();
            const arr = Array.isArray(res) ? res : (Array.isArray(res?.sessions) ? res.sessions : []);
            for (const h of arr) push(h);
            if (out.length > 0) break;
        } catch { /* 换下一个方法 */ }
    }

    // 形态 2：扫描 sessions 目录（最可靠，不依赖宿主暴露什么方法）。
    //   目录名形如 `--E-Xiongwq-RobotChat--`，每个会话一个子目录；
    //   目录名里的 `--...--` 是 cwd 的编码形式，可以还原成路径给人看。
    if (out.length === 0) {
        try {
            const fs = await import('node:fs');
            const path = await import('node:path');
            const os = await import('node:os');
            const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
            const root = path.join(home, 'sessions');
            if (fs.existsSync(root)) {
                for (const proj of fs.readdirSync(root, { withFileTypes: true })) {
                    if (!proj.isDirectory()) continue;
                    const projDir = path.join(root, proj.name);
                    let entries = [];
                    try { entries = fs.readdirSync(projDir, { withFileTypes: true }); }
                    catch { continue; }
                    for (const e of entries) {
                        if (!e.isDirectory()) continue;
                        let mtime = null;
                        try {
                            mtime = fs.statSync(path.join(projDir, e.name)).mtime.toISOString();
                        } catch { /* 读不到时间就算了 */ }
                        push({
                            id: e.name,
                            cwd: decodeProjectDir(proj.name),
                            createdAt: mtime,
                        });
                    }
                }
            }
        } catch { /* 读不到就空着 */ }
    }

    // ── 补标题 ──
    //   光看 sessionId 根本选不出来，必须有标题。
    //   标题不跟会话文件走，存在 storages/session_projcache 里
    //   （`record.rows.title.val`）；projcache 只缓存当前工作区，
    //   所以还要从会话日志本身的 `session/title` 事件兜底。
    try {
        const fs = await import('node:fs');
        const path = await import('node:path');
        const os = await import('node:os');
        const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');

        // 2a. projcache：快，一次读一个 json
        const cacheDir = path.join(home, 'storages', 'session_projcache', 'sessions');
        if (fs.existsSync(cacheDir)) {
            for (const s of out) {
                if (s.title) continue;
                try {
                    const f = path.join(cacheDir, `${s.sessionId}.json`);
                    if (!fs.existsSync(f)) continue;
                    const j = JSON.parse(fs.readFileSync(f, 'utf8'));
                    const t = j?.record?.rows?.title?.val;
                    if (typeof t === 'string' && t.trim()) s.title = t.trim();
                    // 顺手补 cwd（projcache 里更准）
                    const c = j?.record?.identity?.cwd;
                    if (!s.cwd && typeof c === 'string') s.cwd = c;
                } catch { /* 单个坏了不影响整体 */ }
            }
        }

        // 2b. 会话日志的 session/title 事件：projcache 没命中的兜底
        const rootDir = path.join(home, 'sessions');
        if (fs.existsSync(rootDir)) {
            const need = new Set(out.filter((s) => !s.title).map((s) => s.sessionId));
            if (need.size > 0) {
                for (const proj of fs.readdirSync(rootDir, { withFileTypes: true })) {
                    if (!proj.isDirectory()) continue;
                    for (const id of [...need]) {
                        const dir = path.join(rootDir, proj.name, id);
                        if (!fs.existsSync(dir)) continue;
                        const t = readTitleFromSession(dir);
                        if (t) {
                            const hit = out.find((s) => s.sessionId === id);
                            if (hit) hit.title = t;
                            need.delete(id);
                        }
                    }
                    if (need.size === 0) break;
                }
            }
        }
    } catch { /* 标题补不上不影响列表可用 */ }

    // ── 没标题的给个可读兜底 ──
    //   有些会话（刚建、或被别的插件创建）既没有 session/title 事件、
    //   projcache 里也没有记录。列表里显示成一串截断的 UUID 根本选不出来，
    //   所以退一步：用「工作区名 + id 后 6 位」拼个能认的标签。
    for (const s of out) {
        if (s.title) continue;
        const tail = String(s.sessionId).slice(-6);
        const projName = s.cwd
            ? String(s.cwd).replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop()
            : '';
        s.title = projName ? `${projName} · ${tail}` : `会话 ${tail}`;
        s.titleFallback = true;
    }

    // ── 过滤 ──
    //   opts.liveOnly=true 时只留「还在用」的会话：
    //   工作区登记的 + QQ 机器人绑定的 + **本插件自己建过的**。
    //   归档的、孤儿残留、测试壳子全排除，列表才对得上网页左侧。
    if (opts.liveOnly) {
        const live = await readLiveSessionIds();
        if (live !== null) {
            // ★ 本插件内存里还活着的会话一律保留。
            //   它们刚建、还没被 workspace.json 登记，光看登记表会
            //   把"用户自己正在用的这条"过滤掉 —— 列表里就少了当前会话。
            for (const rec of sessions.values()) live.add(rec.sessionId);

            // live 为 null 表示读不到 registry —— 那就不过滤，
            // 宁可多给也别让用户看到空列表。
            for (let i = out.length - 1; i >= 0; i -= 1) {
                if (!live.has(out[i].sessionId)) out.splice(i, 1);
            }
        }
    }

    out.sort((a, b) => String(b.createdAt ?? '').localeCompare(String(a.createdAt ?? '')));
    return out.slice(0, limit);
}

/**
 * 读「还在用」的 sessionId 集合。
 *
 * 两个来源：
 *   1. storages/workspace.json 的 tables.workspaces[*].sessionIds —— 网页端列表里的
 *   2. ~/.dsh-qqbot/model-prefs.json 的 sessionIds —— **QQ 机器人在用的**
 *
 * 为什么必须带第 2 个：QQ 会话是由 qqbot 插件自己创建的，
 * 不走 workspace.json 登记，所以只读第 1 个会把它们全过滤掉 ——
 * 表现就是「QQ 里明明有会话，Blender 列表里一条都看不到」。
 *
 * 读不到返回 null（调用方据此跳过过滤，而不是当成空集合把列表清空）。
 */
async function readLiveSessionIds() {
    let live = null;
    try {
        const fs = await importBuiltin('node:fs');
        const path = await importBuiltin('node:path');
        const os = await importBuiltin('node:os');
        const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');

        const f = path.join(home, 'storages', 'workspace.json');
        if (fs.existsSync(f)) {
            const j = JSON.parse(fs.readFileSync(f, 'utf8'));
            live = new Set();
            for (const ws of Object.values(j?.tables?.workspaces ?? {})) {
                for (const sid of ws?.sessionIds ?? []) live.add(sid);
            }
        }

        // QQ 机器人绑定的会话
        const prefs = path.join(os.homedir(), '.dsh-qqbot', 'model-prefs.json');
        if (fs.existsSync(prefs)) {
            const p = JSON.parse(fs.readFileSync(prefs, 'utf8'));
            for (const sid of Object.values(p?.sessionIds ?? {})) {
                if (typeof sid !== 'string' || !sid) continue;
                if (live === null) live = new Set();
                live.add(sid);
            }
        }
    } catch {
        return live;      // 至少把已经读到的部分返回去
    }
    return live;
}

/**
 * 从会话目录里的日志捞标题。
 *
 * 日志是 zstd 多帧压缩的 jsonl，v4 格式里标题是一条
 * `{"type":"session/title","data":{"title":"..."}}` 事件。
 * 只解前若干帧（标题通常在前面），避免整份解压太慢。
 */
function readTitleFromSession(dir) {
    try {
        // ESM 里没有 require；用 createRequire 拿同步版（这里要同步解压）
        const fs = requireSync('node:fs');
        const zlib = requireSync('node:zlib');
        const path = requireSync('node:path');
        const f = path.join(dir, 'session.v4.jsonl.zstd');
        if (!fs.existsSync(f)) return null;
        const buf = fs.readFileSync(f);
        // 只取前 64KB，标题一般都在开头
        const head = buf.subarray(0, Math.min(buf.length, 64 * 1024));
        const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
        const idx = [];
        let i = 0;
        while (true) {
            const p = head.indexOf(MAGIC, i);
            if (p === -1) break;
            idx.push(p);
            i = p + 4;
        }
        let text = '';
        for (let k = 0; k < idx.length; k += 1) {
            const end = k + 1 < idx.length ? idx[k + 1] : head.length;
            try { text += zlib.zstdDecompressSync(head.subarray(idx[k], end)).toString('utf8'); }
            catch { break; }
        }
        for (const line of text.split('\n')) {
            if (!line.includes('session/title')) continue;
            try {
                const j = JSON.parse(line);
                if (j.type !== 'session/title') continue;
                const t = j?.data?.title;
                if (typeof t === 'string' && t.trim()) return t.trim();
            } catch { /* 跳过坏行 */ }
        }
    } catch { /* 读不到就算了 */ }
    return null;
}


/**
 * 解析要用的 provider/model。
 *
 * ⚠ 为什么必须给：agents.create() 不传 agentOptions 时，
 *   新会话没有继承任何模型路由，第一次 followup 直接失败：
 *     agent "xxx" has no provider/model: set AgentOptions.provider
 *     and AgentOptions.model or record a default
 *   会话照样建出来了，但一个字都吐不出来 —— 表现就是
 *   poll 一直等不到 final。qqbot 插件同样要自己解一遍。
 *
 * 优先级：插件 config 显式指定 > 宿主 agentDefaultModel > 兜底。
 */
function resolveRoute(ctx, cfg) {
    if (cfg?.provider && cfg?.model) {
        return { provider: cfg.provider, model: cfg.model };
    }
    try {
        const adm = typeof ctx.get === 'function' ? ctx.get('agentDefaultModel') : undefined;
        const sel = adm?.currentSelection?.();
        if (sel?.provider && sel?.model) {
            return { provider: sel.provider, model: sel.model };
        }
    } catch { /* 服务未注册，走兜底 */ }
    return undefined;
}

/** 内存里的会话表：blenderSid -> { agent, handle, sessionId } */
const sessions = new Map();

/** 事件邮箱：sessionId -> [ {seq, kind, text} ]，供 Blender 轮询取走 */
const mailboxes = new Map();

/** 自增序号，保证 Blender 侧不重不漏 */
let seqCounter = 0;

/**
 * 诊断：最近收到的原始事件（只在内存里，供 /debug 查看）。
 * 上线后可以删掉，但排查"为什么收不到回复"时它是唯一线索 ——
 * 没有它就只能靠猜事件名。
 */
const eventLog = [];
const listenerNames = [];

function logEvent(tag, extra) {
    eventLog.push({
        at: Date.now(),
        tag,
        ...extra,
    });
    if (eventLog.length > 200) eventLog.shift();
}

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
/**
 * 从 session 对象里取 id。
 *
 * ⚠ 实测 DSH 的 session id 在 **session.header.id**，
 *   不在 session.id。dsh-qqbot 的写法也是 `session.header.id`。
 *   早先只读 session.id 会得到 undefined，导致邮箱全都归档到
 *   "undefined" 这个键上，poll 永远取不到东西。
 */
function sessionIdOf(session) {
    if (!session) return '';
    if (typeof session === 'string') return session;
    return session.header?.id ?? session.id ?? session.sessionId ?? '';
}

/** 从消息里抽纯文本 */
function textOf(message) {
    const blocks = message?.content;
    if (!Array.isArray(blocks)) return '';
    return blocks
        .filter((b) => b && b.type === 'text' && typeof b.text === 'string')
        .map((b) => b.text)
        .join('');
}

/**
 * 生成一个全局唯一的 sessionId。
 *
 * ⚠ 为什么必须自己给：不传 sessionId 时 DSH 会按递增序号分配
 *   （session-1, session-2, session-3...）。插件重载后内存里的
 *   sessions 表被清空，下次又从 session-3 开始分配，就撞上磁盘上
 *   已存在的会话 —— 报错 `session-3 already exists`，HTTP 500。
 *
 *   用时间戳 + 随机后缀，跨进程、跨重载都不会重复。
 */
function mintSessionId() {
    const rnd = (globalThis.crypto?.randomUUID?.()
        ?? Math.random().toString(36).slice(2, 10))
        .replace(/-/g, '')
        .slice(0, 12);
    return `blender-chat-${Date.now().toString(36)}-${rnd}`;
}

/**
 * 试着直接 resume 一条已有会话。
 *
 * 成功返回 record（放进 sessions 表），失败返回 undefined —— 让调用方
 * 降级到「借上下文」。**不抛错**：占用/加载失败都属于预期情况。
 *
 * 常见失败原因：那条会话正被网页端打开，写句柄被独占，
 * resume 会报 already owned by an active writer handle。
 */
async function tryResume(ctx, agents, blenderSid, targetId, cfg) {
    if (typeof agents.resume !== 'function') return undefined;
    try {
        const route = resolveRoute(ctx, cfg);
        const resumed = await agents.resume({
            resumeSessionId: targetId,
            ...(route ? { agentOptions: route } : {}),
        });
        if (!resumed?.agent) return undefined;

        const sessionObj = resumed.session ?? resumed.agent?.session ?? null;
        const record = {
            sessionId: sessionIdOf(sessionObj) || targetId,
            session: sessionObj,
            agent: resumed.agent,
            handle: resumed,
            attached: true,
        };
        sessions.set(blenderSid, record);
        return record;
    } catch (e) {
        // 占用 / 找不到 / 加载失败：都不算错误，交给上层降级
        logger_warn(ctx, `blender-chat: resume ${targetId} 失败，降级为借上下文: `
            + (e instanceof Error ? e.message : String(e)));
        return undefined;
    }
}

/**
 * 磁盘上还有没有这条会话（只看文件，不碰 SessionStore）。
 */
async function sessionExistsOnDisk(ctx, sessionId) {
    try {
        const fs = await importBuiltin('node:fs');
        const path = await importBuiltin('node:path');
        const os = await importBuiltin('node:os');
        const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
        const root = path.join(home, 'sessions');
        for (const proj of fs.readdirSync(root, { withFileTypes: true })) {
            if (!proj.isDirectory()) continue;
            if (fs.existsSync(path.join(root, proj.name, sessionId))) return true;
        }
    } catch { /* 查不到就当没在 */ }
    return false;
}

/** 安全地打一条 warn（logger 可能没有该方法） */
function logger_warn(ctx, msg) {
    try { (ctx.logger ?? console).warn?.(msg); } catch { /* ignore */ }
}

/**
 * 取或建 agent。
 *
 * 语义与 qqbot 一致：一个 Blender 会话对应一个 DSH 会话；
 * 第一次来就 create，之后一直复用，历史自然累积在 DSH 里。
 */
async function ensureAgent(ctx, agents, blenderSid, cwd, cfg) {
    const existing = sessions.get(blenderSid);
    if (existing) {
        // ★ 必须先确认这个会话在**当前进程的 SessionStore 里还活着**。
        //
        //   踩过的坑：DSH 重启后磁盘上的会话目录会被整理/清掉，但插件
        //   内存的 sessions 表还留着旧记录（同一个进程地址空间里模块不重载）。
        //   这时再去 followup 就报
        //     session "<id>" ... outside a SessionStore
        //   表现就是"重启过一次就再也发不出消息"。
        //
        //   agents.get() 返回 falsy = 这个 agent 不在注册表里 → 视为失效，
        //   丢掉旧记录、往下走新建流程。
        let live;
        try {
            live = agents.get?.(existing.sessionId);
        } catch { live = undefined; }

        if (live) {
            existing.agent = live;
            return existing;
        }

        // 失效：先清掉内存记录，再确认磁盘上是否真的没了。
        sessions.delete(blenderSid);
        const stillOnDisk = await sessionExistsOnDisk(ctx, existing.sessionId);
        if (stillOnDisk) {
            // 磁盘上还在 —— 但当前进程没加载它。不要冒险复用失效句柄，
            // 直接新建；旧会话的记录留在磁盘上，网页端还能打开。
            logger_warn(ctx, `blender-chat: ${existing.sessionId} 不在当前 SessionStore，改为新建`);
        }
    }

    // ── 新建一个属于 Blender 的会话 ──
    // ★ 自己给一个唯一 id，不让 DSH 按序号分配（会撞车）
    //
    // 注：「借已有会话的上下文」在 /send 路由里做（读摘要 + 拼进第一条消息），
    //     这里只负责建一个干净的新会话 —— 不 resume 也不 fork，
    //     所以永远不会碰上 "already owned by an active writer handle"。
    const wantedId = mintSessionId();
    const route = resolveRoute(ctx, cfg);
    // cwd 来自 Blender 的 .blend 所在目录。给空/给了个不存在的目录时
    // 退回宿主进程的 cwd —— DSH 对不存在的目录会直接建会话失败，
    // 报出来的错还很难懂。
    let effectiveCwd = cwd || process.cwd();
    try {
        if (cwd) {
            const fs = await importBuiltin('node:fs');
            if (!fs.existsSync(cwd) || !fs.statSync(cwd).isDirectory()) {
                logger_warn(ctx, `blender-chat: cwd 不存在，改用进程目录: ${cwd}`);
                effectiveCwd = process.cwd();
            }
        }
    } catch { /* 查不了就用原值 */ }
    const meta = { cwd: effectiveCwd, blenderChat: true };
    let created;
    try {
        created = await agents.create({
            sessionId: wantedId,
            meta,
            ...(route ? { agentOptions: route } : {}),
        });
    } catch (e) {
        // 万一这个 id 也撞了（几乎不可能），换个 id 再试一次
        const msg = String(e?.message ?? e);
        if (!msg.includes('already exists')) throw e;
        created = await agents.create({
            sessionId: mintSessionId(),
            meta,
            ...(route ? { agentOptions: route } : {}),
        });
    }
    // ⚠ 返回结构是 { agent, session, handle? }。实测 DSH 会**忽略**
    //   传入的 sessionId 自己分配（传 blender-chat-xxx，回 session-4），
    //   所以必须以返回值里的 id 为准；取不到才退回 wantedId。
    const sessionObj = created.session ?? created.agent?.session ?? null;
    const sessionId = sessionIdOf(sessionObj) || created.sessionId || wantedId;
    const record = {
        sessionId,
        session: sessionObj,
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
    //
    // 签名：`session/event` 的参数是**两个独立值** `(session, event)`，
    // 不是打包对象。写成 (payload) 会拿到 session、event 是 undefined，
    // 于是 type 永远匹配不上，表现得像"事件名不对"。
    //
    // event.type 取值（实测）：
    //   'assistant/chunk'   —— 增量（部分版本才有）
    //   'assistant/message' —— 完整消息，data.message.content 里是文本块
    //   'turn/end'          —— 本轮结束，data.reason 说明失败原因
    //
    // ⚠ 两个监听器都必须 { global: true }（踩过坑）：
    //   session/event 与 agent/assistant-stream 都按 agent 作用域派发，
    //   本插件挂在根作用域；不带 global 时只有恰好落在根作用域的事件能收到
    //   —— 现象是网页会话的事件能到、插件自己建的会话一条都不到，
    //   send 成功、会话里有回复，poll 却永远空。
    ctx.on('session/event', (session, event) => {
        try {
            const type = event?.type ?? event?.kind;
            const sid = sessionIdOf(session);
            logEvent('session/event', {
                type: type ?? '(no type)',
                sid,
                matched: Boolean(findBySession(session)),
                keys: event ? Object.keys(event).slice(0, 12) : [],
            });
            const rec = findBySession(session);
            if (!rec) return;

            if (type === 'assistant/chunk') {
                const t = textOf(event.message ?? event.delta ?? event);
                if (t) push(rec.sessionId, 'stream', t);
            } else if (type === 'assistant/message') {
                // ⚠ 真实结构是 event.data.message.content，不是 event.message。
                //
                // ★ 一轮里 DSH 会发**多条** assistant/message：
                //   先一句"我先看一眼场景"，然后才是完整答案。
                //   以前每来一条就 push('final')，Blender 只认第一条，
                //   于是用户看到的永远是那个开头短句 ——
                //   表现就是"问题和回答对不上"。
                //
                //   现在改成**只暂存、不推**：留最后一条（最完整的那条），
                //   等这一轮结束再一次性推出去。
                const t = textOf(event.data?.message)
                    || textOf(event.message)
                    || textOf(event.data);
                if (t) {
                    rec.pendingFinal = t;
                    if (rec.pendingEnd) {
                        rec.pendingEnd.finalSeen = true;
                    }
                }
            } else if (type === 'turn/end') {
                // 本轮结束：把暂存的完整答案推出去，然后收尾。
                //
                // 依次尝试三种时机，谁先到用谁：
                //   1. pendingFinal 已经有内容 → 立刻推 final + end
                //   2. 暂时没有 → 等 400ms（DSH 可能还在写 message）
                //   3. 等到也没有 → 只推 end（这一轮确实没产出）
                const sidNow = rec.sessionId;
                const flush = () => {
                    if (rec.sessionId !== sidNow) return;
                    const text = rec.pendingFinal;
                    rec.pendingFinal = '';
                    if (text) push(sidNow, 'final', text);
                    push(sidNow, 'end', '');
                };

                if (rec.pendingFinal) {
                    flush();
                } else {
                    rec.pendingEnd = { sid: sidNow, timer: null };
                    rec.pendingEnd.timer = setTimeout(() => {
                        const pend = rec.pendingEnd;
                        rec.pendingEnd = null;
                        if (!pend || pend.sid !== sidNow) return;
                        flush();
                    }, 400);
                }
            }
        } catch (e) {
            logger.warn?.(`blender-chat session/event 处理失败: ${e}`);
            logEvent('session/event', { error: String(e) });
        }
    }, { global: true });
    listenerNames.push('session/event(global)');

    // 流式帧：这个通道**必须**带 global，否则根作用域收不到
    ctx.on('agent/assistant-stream', (payload) => {
        try {
            logEvent('agent/assistant-stream', {
                keys: payload ? Object.keys(payload).slice(0, 12) : [],
                matched: Boolean(findByAgent(payload?.agent)),
            });
            const rec = findByAgent(payload?.agent);
            if (!rec) return;
            const delta = payload?.delta;
            const t = typeof delta === 'string' ? delta : textOf(delta);
            if (t) push(rec.sessionId, 'stream', t);
        } catch (e) {
            logger.warn?.(`blender-chat stream 处理失败: ${e}`);
        }
    }, { global: true });
    listenerNames.push('agent/assistant-stream(global)');

    function findByAgent(agent) {
        if (!agent) return undefined;
        for (const rec of sessions.values()) {
            if (rec.agent === agent) return rec;
        }
        return undefined;
    }

    function findBySession(session) {
        if (!session) return undefined;
        // session 可能是对象，也可能直接是 id 字符串
        if (typeof session === 'string') {
            for (const rec of sessions.values()) {
                if (rec.sessionId === session) return rec;
            }
            return undefined;
        }
        const sid = sessionIdOf(session);
        for (const rec of sessions.values()) {
            if (rec.sessionId === sid || rec.session === session) return rec;
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
                const { sid = 'default', text = '', cwd = '', attach = '' } = body;
                if (!text.trim()) {
                    sendJson(res, 400, { error: 'text 不能为空' });
                    return;
                }
                try {
                    // attach = 绑定哪条已有会话（完整 id 或唯一前缀）。
                    //   只在这次还没绑过时生效；绑上后这个 sid 一直复用它。
                    const attachSid = attach && !sessions.has(sid) ? attach : undefined;

                    // 绑定的唯一目的就是「接进那条会话」。
                    //
                    // 接不进去（最常见：那条会话正被网页端开着，写句柄被独占
                    // → already owned by an active writer handle）**不降级、不兜底**，
                    // 直接如实报错，让用户关掉网页那边再试。
                    //
                    // 之前试过"降级成新建会话 + 注入历史摘要"，结果是：
                    // 用户以为自己在聊那条会话，实际在跟一个只有摘要的新会话聊天，
                    // 回答对不上还很难查。**伪装成成功比直接失败更糟**。
                    let rec;
                    let attachMode = 'new';

                    if (attachSid) {
                        const targetId = await resolveSessionId(ctx, attachSid);
                        if (!targetId) {
                            throw new Error(
                                `找不到会话 ${attachSid}`
                                + `（可能已删除或已归档）。点「刷新」重新拉一次列表再选。`);
                        }

                        const resumed = await tryResume(ctx, agents, sid, targetId, cfg);
                        if (!resumed) {
                            throw new Error(
                                `接不进会话 ${targetId}：它正被别处打开着`
                                + `（网页端 DSH 界面或另一个窗口占了写句柄）。`
                                + `把那个页面关掉后点「重发上一条」即可。`);
                        }
                        rec = resumed;
                        attachMode = 'resume';
                    } else {
                        rec = await ensureAgent(ctx, agents, sid, cwd, cfg);
                    }

                    // 上下文管理交给 DSH：这里**不做任何历史拼接**，
                    // 原样把用户这句话发出去。会话自己会带着它的历史。
                    const message = makeUserMessage(text);
                    rec.agent.followup(message);
                    sendJson(res, 200, {
                        ok: true,
                        sessionId: rec.sessionId,
                        attachMode,
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
            // 列出宿主上已有的会话，供面板下拉选择
            //
            // 查询参数：
            //   limit=N        最多返回几条（默认 100）
            //   liveOnly=1     只返回「工作区里登记着」的会话
            //                  —— 排掉归档的、孤儿残留、测试壳子
            kind: 'exact',
            path: `${BASE}/sessions`,
            handler: async (req, res) => {
                const url = new URL(req.url ?? '', 'http://127.0.0.1');
                const liveOnly = ['1', 'true', 'yes'].includes(
                    String(url.searchParams.get('liveOnly') ?? '').toLowerCase());
                try {
                    const list = await listSessions(ctx, {
                        limit: Number(url.searchParams.get('limit') ?? '100'),
                        liveOnly,
                    });
                    sendJson(res, 200, {
                        ok: true,
                        count: list.length,
                        sessions: list,
                        liveOnly,
                        // 面板据此决定显示下拉框还是"手动填 id"输入框
                        canList: list.length > 0,
                    });
                } catch (e) {
                    sendJson(res, 200, {
                        ok: false,
                        count: 0,
                        sessions: [],
                        canList: false,
                        liveOnly,
                        error: e instanceof Error ? e.message : String(e),
                    });
                }
            },
        },
        {
            kind: 'exact',
            path: `${BASE}/poll`,
            handler: async (req, res) => {
                const url = new URL(req.url ?? '', 'http://127.0.0.1');
                const sid = url.searchParams.get('sid') ?? 'default';
                const since = Number(url.searchParams.get('since') ?? '0');
                const rec = sessions.get(sid);
                if (!rec) {
                    sendJson(res, 200, { events: [], running: false });
                    return;
                }
                // 不扫历史。只返回本轮监听器收到的事件 ——
                // 翻历史会把旧回复当成新回复灌给用户（曾经踩过）。

                const box = mailboxOf(rec.sessionId);
                const events = box.filter((e) => e.seq > since);
                // 取走就清，避免无限增长。
                //
                // ⚠ 这里必须按**实际取走的那几个 seq** 删，不能用
                //   `seq > since + events.length`：
                //   seq 是全局自增计数器，中间会被别的会话/别的事件跳号，
                //   算出来的边界会保住本该删掉的事件。
                //   于是下一轮 poll 又把**属于上一轮**的 final 吐出来一次，
                //   表现就是「AI 回复的总是上一条消息」。
                if (events.length > 0) {
                    const taken = new Set(events.map((e) => e.seq));
                    const rest = box.filter((e) => !taken.has(e.seq));
                    box.length = 0;
                    box.push(...rest);
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
            path: `${BASE}/debug`,
            handler: (req, res) => {
                // 排查用：看看到底收到了哪些事件、哪些没被匹配上
                sendJson(res, 200, {
                    ok: true,
                    sessions: [...sessions.entries()].map(([sid, rec]) => ({
                        sid,
                        sessionId: rec.sessionId,
                        hasAgent: Boolean(rec.agent),
                        hasSession: Boolean(rec.session),
                        sessionKeys: rec.session
                            ? Object.keys(rec.session).slice(0, 20) : [],
                        agentStatus: rec.agent?.status,
                    })),
                    eventLog: eventLog.slice(-40),
                    listeners: listenerNames,
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
