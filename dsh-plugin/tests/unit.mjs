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

  const agents = {
    create: async (opts) => {
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
    get: () => undefined,
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
    _handlers: handlers,
    _routes: routes,
    _created: created,
    _followups: followups,
    _emit(evt, payload) {
      for (const fn of handlers.get(evt) ?? []) fn(payload);
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

check('注册 4 条路由', () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  const paths = ctx._routes.map((r) => r.path).sort();
  console.log('       路由:', paths);
  assert.deepStrictEqual(paths, [
    '/blender-chat/poll',
    '/blender-chat/reset',
    '/blender-chat/send',
    '/blender-chat/status',
  ]);
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

await checkAsync('final 事件能到邮箱', async () => {
  const ctx = freshCtx();
  mod.apply(ctx, {});
  await callRoute(ctx, '/blender-chat/send', makeReq({ sid: 'a', text: 'q' }));
  const agent = ctx._created[0];

  ctx._emit('agent/assistant/message', {
    agent,
    message: { content: [{ type: 'text', text: '完整回答' }] },
  });

  const r = await callRoute(ctx, '/blender-chat/poll',
    makeReq(undefined, { method: 'GET', url: '/blender-chat/poll?sid=a&since=0' }));
  const finals = r.json.events.filter((e) => e.kind === 'final');
  console.log('       final:', finals);
  assert.strictEqual(finals.length, 1);
  assert.strictEqual(finals[0].text, '完整回答');
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
