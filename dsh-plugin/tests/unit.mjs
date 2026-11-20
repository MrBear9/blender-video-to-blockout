/**
 * 纯 Node 单测：不启动 DSH，用假 ctx 验证插件逻辑。
 *
 * 覆盖：
 *   1. apply 注册了哪些路由
 *   2. /send 空文本被拒
 *   3. /send 正常 → 建 agent + followup
 *   4. /poll 能取走事件（不重不漏）
 *   5. 同一 sid 复用 agent（不重复 create）
 *   6. /reset 释放 agent
 *
 * 跑法：node tests/unit.mjs
 */
import assert from 'node:assert';

const results = [];
function check(name, fn) {
  try {
    const r = fn();
    if (r instanceof Promise) throw new Error('用 checkAsync');
    results.push([name, true]);
    console.log('  ✓', name);
  } catch (e) {
    results.push([name, false, e]);
    console.log('  ✗', name, ':', e.message);
  }
}
async function checkAsync(name, fn) {
  try {
    await fn();
    results.push([name, true]);
    console.log('  ✓', name);
  } catch (e) {
    results.push([name, false, e]);
    console.log('  ✗', name, ':', e.message);
  }
}

// ── 假 ctx ──
function makeCtx() {
  const handlers = new Map();
  const routes = [];
  const created = [];
  const followups = [];
  const createOpts = [];
  const knownSessions = [];
  const resumeOpts = [];

  const agents = {
    create: async (opts) => {
      createOpts.push(opts ?? {});
      const rec = {
        id: 'agent-' + (created.length + 1),
        session: { id: 'session-' + (created.length + 1) },
        status: 'idle',
        followup(msg) {
          followups.push({ agent: rec, msg });
          rec.status = 'running';
        },
        cancel() { rec.status = 'idle'; },
        whenIdle: async () => {},
      };
      created.push(rec);
      return {
        agent: rec,
        sessionId: rec.session.id,
        dispose: async () => {},
      };
    },
    // 模拟真实 SessionStore：只认 create 出来、还活着的 agent
    get: (sessionId) => {
      const hit = created.find((a) => a.session && a.session.id === sessionId);
      return hit || undefined;
    },
    // attach 到已有会话时用；记录被 resume 的 id，供断言
    resume: async (opts) => {
      resumeOpts.push(opts ?? {});
      const id = opts?.resumeSessionId;
      const rec = {
        id: 'agent-resumed',
        session: { id },
        status: 'idle',
        followup(msg) { followups.push({ agent: rec, msg }); rec.status = 'running'; },
        cancel() { rec.status = 'idle'; },
        whenIdle: async () => {},
      };
      created.push(rec);
      return { agent: rec, session: rec.session, dispose: async () => {} };
    },
  };

  const ctx = {
    logger: {
      info: () => {},
      warn: () => {},
      error: () => {},
    },
    agents,
    on(evt, fn) {
      if (!handlers.has(evt)) handlers.set(evt, []);
      handlers.get(evt).push(fn);
    },
    inject(deps, fn) {
      // 假 webServer：记录注册的路由
      fn({
        webServer: {
          register(spec) {
            routes.push(spec);
            return () => {};
          },
        },
      });
    },
    // 宿主服务查询：模型路由与会话列举都走这里
    get(name) {
      if (name === 'agentDefaultModel') {
        return { currentSelection: () => ({ provider: 'test-provider', model: 'test-model' }) };
      }
      // 故意不提供 list/listSessions：真机上未必有，
      // 插件必须能自己扫 sessions 目录兜底，这里就是要验这条路径。
      if (name === 'sessionPersistence') {
        return {
          async stat(id) {
            return knownSessions.some((s) => s.sessionId === id) ? { header: { id } } : undefined;
          },
        };
      }
      return undefined;
    },
    _knownSessions: knownSessions,
    _resumeOpts: resumeOpts,
    _handlers: handlers,
    _routes: routes,
    _created: created,
    _createOpts: createOpts,
    _followups: followups,
    _emit(evt, ...args) {
      for (const fn of handlers.get(evt) ?? []) fn(...args);
    },
  };
  return ctx;
}

// ── 假 req/res ──
function makeReq(body, opts = {}) {
  const chunks = [];
  if (body !== undefined) {
    chunks.push(Buffer.from(JSON.stringify(body)));
  }
  const req = {
    method: opts.method ?? 'POST',
    url: opts.url ?? '/',
    async* [Symbol.asyncIterator]() {
      for (const c of chunks) yield c;
    },
  };
  return req;
}

function makeRes() {
  const out = { status: 0, body: '', headers: {} };
  const res = {
    writeHead(status, headers) {
      out.status = status;
      Object.assign(out.headers, headers);
    },
    end(payload) {
      out.body = payload ?? '';
    },
  };
  return { res, out };
}

async function callRoute(ctx, path, req) {
  const route = ctx._routes.find((r) => r.path === path);
  assert.ok(route, `没找到路由 ${path}，实际有：${ctx._routes.map((r) => r.path)}`);
  const { res, out } = makeRes();
  await route.handler(req, res);
  let parsed = null;
  try { parsed = JSON.parse(out.body); } catch { /* 非 JSON */ }
  return { status: out.status, json: parsed, raw: out.body };
}

// ═══════════════════════════════════════════════════════════════════
const mod = await import('../src/index.js');

/** 每个用例前清空模块级状态（sessions/mailboxes） */
function freshCtx() {
  mod.__resetForTests();
  return makeCtx();
}

check('注入 agents', () => {
  assert.deepStrictEqual(mod.inject, ['agents']);
});

check('路由齐全且都在 /blender-chat 下', () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const paths = ctx._routes.map((r) => r.path).sort();
  console.log('       路由:', paths);
  // 不写死条数：加一条路由就不该让这条断言红掉。
  // 真正要守的是"每条都是绝对路径、都挂在同一个前缀下、都不重复"。
  assert.ok(paths.length > 0, '一条路由都没注册');
  for (const p of paths) {
    assert.ok(p.startsWith('/blender-chat/'), `路由不在 /blender-chat 下: ${p}`);
  }
  assert.strictEqual(new Set(paths).size, paths.length, '有重复路由');
  // 这几个是 Blender 面板依赖的核心端点，缺一不可
  for (const must of ['/blender-chat/send', '/blender-chat/poll', '/blender-chat/status']) {
    assert.ok(paths.includes(must), `缺核心路由 ${must}`);
  }
});

await checkAsync('空文本被拒', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const r = await callRoute(ctx, '/blender-chat/send', makeReq({ text: '   ' }));
  assert.strictEqual(r.status, 400);
  assert.ok(r.json.error.includes('text'));
});

await checkAsync('正常发送 → 建 agent + followup', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const r = await callRoute(ctx, '/blender-chat/send',
    makeReq({ sid: 'a', text: '场景里有什么' }));
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.json.ok, true);
  assert.strictEqual(ctx._created.length, 1, '应该建了 1 个 agent');
  assert.strictEqual(ctx._followups.length, 1, '应该有 1 次 followup');
  const msg = ctx._followups[0].msg;
  assert.strictEqual(msg.role, 'user');
  assert.strictEqual(msg.content[0].text, '场景里有什么');
});

await checkAsync('同一 sid 复用 agent', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: '一' }));
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: '二' }));
  console.log('       create 次数:', ctx._created.length);
  assert.strictEqual(ctx._created.length, 1, '不该重复 create');
  assert.strictEqual(ctx._followups.length, 2);

  // 不同 sid 要各建一个
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'b', text: '三' }));
  assert.strictEqual(ctx._created.length, 2, '不同 sid 应各建一个');
});

await checkAsync('poll 取走事件且不重复', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));

  const agent = ctx._created[0];
  // 模拟模型输出
  ctx._emit('agent/assistant-stream', { agent, delta: '你好' });
  ctx._emit('agent/assistant-stream', { agent, delta: '，世界' });

  const r1 = await callRoute(ctx, '/blender-chat/poll',
    makeReq(undefined, { method: 'GET', url: '/blender-chat/poll?sid=a&since=0' }));
  console.log('       events:', r1.json.events);
  assert.strictEqual(r1.json.events.length, 2);
  assert.strictEqual(r1.json.events[0].text, '你好');

  // 再 poll 一次（since 用最大 seq）—— 应该空
  const maxSeq = Math.max(...r1.json.events.map((e) => e.seq));
  const r2 = await callRoute(ctx, '/blender-chat/poll',
    makeReq(undefined, { method: 'GET', url: `/blender-chat/poll?sid=a&since=${maxSeq}` }));
  console.log('       第二次 events:', r2.json.events);
  assert.strictEqual(r2.json.events.length, 0, '不该重复给');
});

await checkAsync('final 事件能到邮箱（等 turn/end 后）', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  const agent = ctx._created[0];
  const session = agent.session;

  // 真实通道是 session/event，签名是 (session, event) 两个独立参数
  ctx._emit('session/event', session, {
    type: 'assistant/message',
    message: { content: [{ type: 'text', text: '完整回答' }] },
  });
  // ★ 新逻辑：assistant/message 只暂存，等 turn/end 才推 final ——
  //   因为一轮里 DSH 会发好几条 message（先一句"我看看"再补完整答案），
  //   逐条推会让用户只看到那个开头短句。
  ctx._emit('session/event', session, {
    type: 'turn/end',
    data: { reason: { kind: 'completed' } },
  });
  await new Promise((r) => setTimeout(r, 30));

  const r = await callRoute(ctx, '/blender-chat/poll',
    makeReq(undefined, { method: 'GET', url: '/blender-chat/poll?sid=a&since=0' }));
  const finals = r.json.events.filter((e) => e.kind === 'final');
  console.log('       final:', finals.map((e) => e.text));
  assert.strictEqual(finals.length, 1);
  assert.strictEqual(finals[0].text, '完整回答');
});

await checkAsync('一轮多条 message 只取最后一条', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  const session = ctx._created[0].session;

  // 模拟真实节奏：先说一句，再说完整答案
  for (const t of ['我先看一眼场景', '看完了', '完整答案在这里']) {
    ctx._emit('session/event', session, {
      type: 'assistant/message',
      data: { message: { content: [{ type: 'text', text: t }] } },
    });
  }
  ctx._emit('session/event', session, {
    type: 'turn/end',
    data: { reason: { kind: 'completed' } },
  });
  await new Promise((r) => setTimeout(r, 30));

  const r = await callRoute(ctx, '/blender-chat/poll',
    makeReq(undefined, { method: 'GET', url: '/blender-chat/poll?sid=a&since=0' }));
  const finals = r.json.events.filter((e) => e.kind === 'final');
  console.log('       final:', finals.map((e) => e.text));
  // ★ 曾经的真实 bug：逐条推 → Blender 只认第一条，用户看到"我先看一眼场景"，
  //   和问题完全对不上。
  assert.strictEqual(finals.length, 1, '一轮只该有一条 final');
  assert.strictEqual(finals[0].text, '完整答案在这里', '应取最后那条完整答案');
});

await checkAsync('status 列出会话', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  const r = await callRoute(ctx, '/blender-chat/status',
    makeReq(undefined, { method: 'GET', url: '/blender-chat/status' }));
  console.log('       sessions:', r.json.sessions);
  assert.strictEqual(r.json.sessions.length, 1);
  assert.strictEqual(r.json.sessions[0].sid, 'a');
});

await checkAsync('reset 释放会话', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  const r = await callRoute(ctx, '/blender-chat/reset',
    makeReq(undefined, { method: 'POST', url: '/blender-chat/reset?sid=a' }));
  assert.strictEqual(r.json.ok, true);
  const s = await callRoute(ctx, '/blender-chat/status',
    makeReq(undefined, { method: 'GET', url: '/blender-chat/status' }));
  assert.strictEqual(s.json.sessions.length, 0, 'reset 后应为空');
});

// ── 汇总 ──
await checkAsync('每次 create 都带唯一 sessionId', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'b', text: 'q' }));

  const ids = ctx._createOpts.map((o) => o.sessionId);
  console.log('       传入的 sessionId:', ids);
  assert.strictEqual(ids.length, 2);
  for (const id of ids) {
    assert.ok(id, 'sessionId 不能是 undefined —— 不传会让 DSH 按序号分配，'
      + '插件重载后必然撞车（session-3 already exists）');
    assert.match(id, /^blender-chat-/, 'sessionId 应带前缀便于识别');
  }
  assert.notStrictEqual(ids[0], ids[1], '两次 create 的 id 不能相同');
});

await checkAsync('撞车时换 id 重试', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  // 第一次 create 抛 already exists，之后正常
  let threw = false;
  const origCreate = ctx.agents.create;
  ctx.agents.create = async (opts) => {
    if (!threw) {
      threw = true;
      throw new Error("'session-3' already exists");
    }
    return origCreate(opts);
  };
  const r = await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  console.log('       HTTP', r.status, 'retried=', threw);
  assert.strictEqual(r.status, 200, '应该重试成功');
});

await checkAsync('create 带上 provider/model', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  const opts = ctx._createOpts[0];
  console.log('       agentOptions:', JSON.stringify(opts.agentOptions));
  // 不传的话会话能建出来但一个字都不回，报 "has no provider/model"
  assert.ok(opts.agentOptions, '必须传 agentOptions，否则 agent 没有模型路由');
  assert.strictEqual(opts.agentOptions.provider, 'test-provider');
  assert.strictEqual(opts.agentOptions.model, 'test-model');
});

await checkAsync('config 显式 provider/model 优先于宿主默认', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, { provider: 'custom-provider', model: 'custom-model' });
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  const opts = ctx._createOpts[0];
  console.log('       agentOptions:', JSON.stringify(opts.agentOptions));
  assert.strictEqual(opts.agentOptions.provider, 'custom-provider');
  assert.strictEqual(opts.agentOptions.model, 'custom-model');
});

await checkAsync('attach 优先直接 resume 进那条会话', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  // 用磁盘上真实存在的会话
  const listed = await callRoute(ctx, '/blender-chat/sessions', makeReq(undefined, {
    method: 'GET', url: '/blender-chat/sessions',
  }));
  const first = listed.json?.sessions?.[0];
  if (!first) {
    console.log('       （本机没有可扫到的会话，跳过）');
    return;
  }

  const r = await callRoute(ctx, '/blender-chat/send', makeReq({
    sid: 'panel', text: '继续', attach: first.sessionId,
  }));
  console.log('       HTTP', r.status, 'mode=', r.json?.attachMode,
    'sessionId=', String(r.json?.sessionId ?? '').slice(0, 30));

  assert.strictEqual(r.status, 200, `不该报错：${r.json?.error ?? ''}`);
  // 优先 resume：绑的就是那条，历史天然累积在同一处
  assert.strictEqual(r.json.attachMode, 'resume', '应该先走 resume');
  assert.strictEqual(ctx._resumeOpts.length, 1, 'resume 应被调用一次');
  assert.strictEqual(ctx._resumeOpts[0].resumeSessionId, first.sessionId);
});

await checkAsync('resume 失败时明确报错（不伪装成功）', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const listed = await callRoute(ctx, '/blender-chat/sessions', makeReq(undefined, {
    method: 'GET', url: '/blender-chat/sessions',
  }));
  const first = listed.json?.sessions?.[0];
  if (!first) {
    console.log('       （本机没有可扫到的会话，跳过）');
    return;
  }

  // 模拟会话被网页端占用
  ctx.agents.resume = async () => {
    throw new Error('session "x" is already owned by an active writer handle');
  };

  const r = await callRoute(ctx, '/blender-chat/send', makeReq({
    sid: 'panel', text: '继续', attach: first.sessionId,
  }));
  console.log('       HTTP', r.status, 'error=', String(r.json?.error ?? '').slice(0, 70));

  // ★ 必须如实失败，不能偷偷新建一个让用户以为接上了 ——
  //   之前"降级成借上下文"就是这个毛病：看着成功，其实在跟别的会话聊。
  assert.strictEqual(r.status, 500);
  assert.match(String(r.json?.error ?? ''), /接不进会话|被别处打开/);
  assert.strictEqual(ctx._created.length, 0, '不该偷偷新建会话');
});

await checkAsync('attach 不存在的会话会报错', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const r = await callRoute(ctx, '/blender-chat/send', makeReq({
    sid: 'panel', text: 'hi', attach: 'zzz-no-such-session-xyz-000',
  }));
  console.log('       HTTP', r.status, 'error=', String(r.json?.error ?? '').slice(0, 60));
  // 找不到就明确报出来，别静默新建一个让用户以为接上了
  assert.strictEqual(r.status, 500);
  assert.match(String(r.json?.error ?? ''), /找不到会话|不存在/);
});

await checkAsync('/sessions 列出可接的会话', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const r = await callRoute(ctx, '/blender-chat/sessions', makeReq(undefined, {
    method: 'GET', url: '/blender-chat/sessions',
  }));
  console.log('       HTTP', r.status, 'canList=', r.json?.canList, 'count=', r.json?.count);
  assert.strictEqual(r.status, 200);
  assert.ok(Array.isArray(r.json.sessions), 'sessions 应该是数组');
  // 环境里没有 list() 也读不到索引文件时允许为空，但字段必须齐全
  assert.strictEqual(typeof r.json.canList, 'boolean');
});

await checkAsync('agent 失效后自动重建（重启过的场景）', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: '一' }));
  assert.strictEqual(ctx._created.length, 1);

  // 模拟 DSH 重启：SessionStore 里没了这个 agent（get 返回 undefined），
  // 但插件内存的 sessions 表还留着旧记录。
  // 旧代码会继续复用失效句柄 → 报 "outside a SessionStore"。
  ctx._created.length = 0;                       // 让 get() 再也找不到它
  const r = await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: '二' }));

  console.log('       HTTP', r.status, '新建:', ctx._created.length);
  assert.strictEqual(r.status, 200, `不该报错：${r.json?.error ?? ''}`);
  assert.strictEqual(ctx._created.length, 1, '应该重建一个新会话，而不是复用失效的');
});

await checkAsync('多轮不串台：第 N 轮只拿到第 N 轮的回答', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const send = ctx._routes.find((r) => r.path === '/blender-chat/send');
  const poll = ctx._routes.find((r) => r.path === '/blender-chat/poll');
  const evs = ctx._handlers.get('session/event') || [];

  async function round(text, answer) {
    await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 't', text }));
    // created 里存的是假 agent 记录本身（不是 {agent:...} 包装）
    const ses = ctx._created[0].session;
    for (const fn of evs) {
      fn(ses, {
        type: 'assistant/message',
        data: { message: { role: 'assistant', content: [{ type: 'text', text: answer }] } },
      });
    }
    // 新逻辑要等 turn/end 才 flush
    for (const fn of evs) {
      fn(ses, { type: 'turn/end', data: { reason: { kind: 'completed' } } });
    }
    await new Promise((r) => setTimeout(r, 30));
    const o = { writeHead() {}, end(p) { this.p = p; } };
    await poll.handler({ method: 'GET', url: '/blender-chat/poll?sid=t&since=0' }, o);
    // 只取 final 的文本（end 是空串，不该算进答案）
    return JSON.parse(o.p).events
      .filter((e) => e.kind === 'final')
      .map((e) => e.text).join(',');
  }

  const r1 = await round('问题1', '回答1');
  const r2 = await round('问题2', '回答2');
  const r3 = await round('问题3', '回答3');
  console.log(`       轮次结果: "${r1}" / "${r2}" / "${r3}"`);

  // ★ 曾经的真实 bug：mailbox 清理用 `seq > since + events.length`，
  //   seq 跳号时算错边界，旧 final 留在盒子里，下一轮又吐出来 →
  //   用户看到「AI 回复的总是上一条」。
  assert.strictEqual(r1, '回答1', '第 1 轮应只有回答1');
  assert.strictEqual(r2, '回答2', '第 2 轮不该带出回答1');
  assert.strictEqual(r3, '回答3', '第 3 轮不该带出前两轮');
});

const failed = results.filter(([, ok]) => !ok);
console.log();
console.log('='.repeat(50));
if (failed.length) {
  console.log(`✗ ${failed.length} 项失败`);
  for (const [n, , e] of failed) console.log('   ', n, e?.stack?.split('\n')[1] ?? '');
  process.exit(1);
} else {
  console.log('✓ 全部通过');
}
