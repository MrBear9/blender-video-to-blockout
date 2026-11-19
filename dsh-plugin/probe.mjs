/**
 * probe.mjs — 自检：DSH 那边的通道到底通不通
 *
 * 用法：
 *   node probe.mjs                    # 自动探测端口
 *   node probe.mjs --port 64944       # 指定端口
 *   node probe.mjs --auth "Bearer x"  # 需要鉴权时
 *
 * ⚠ DSH **每次启动都换端口**（实测 53064 → 64944），
 *   所以默认自动扫描，不用每次手填。
 */
import net from 'node:net';
import { dshCandidatePorts } from './ports.mjs';

const argv = process.argv.slice(2);
function arg(name, dflt) {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : dflt;
}

const auth = arg('--auth', null);
const givenPort = arg('--port', null);
const sid = arg('--sid', 'probe');

//: 候选端口。DSH 的 Web 端口是动态分配的，
//: 这几个是本机实测出现过的（63524 当前就在用）。
const CANDIDATES = [
  63524, 64944, 53064, 58169, 8326,
];

function tcpAlive(port, timeout = 150) {
  return new Promise((resolve) => {
    const s = net.connect({ host: '127.0.0.1', port });
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      clearTimeout(t);
      try { s.destroy(); } catch { /* ignore */ }
      resolve(v);
    };
    const t = setTimeout(() => finish(false), timeout);
    s.once('connect', () => finish(true));
    s.once('error', () => finish(false));
  });
}

/**
 * 判断这个端口是不是 DSH 的 HTTP 服务。
 *
 * ⚠ 踩过两个坑：
 *   1. 58169 端口**接受 TCP 连接然后立刻 RST**，
 *      旧实现把 ECONNRESET 当成"是 HTTP 服务"，probe 就选错了端口。
 *   2. `fetch` 遇到 ECONNRESET 会抛一个包装过的 `fetch failed`，
 *      看不出原因。所以这里直接用 net 发最小 GET，读回原始响应行 ——
 *      只要看到 "HTTP/" 开头就算数（401/404 都算，那都是 DSH 在应答）。
 */
function rawHttpGet(port, path, timeout = 3000) {
  return new Promise((resolve) => {
    const s = net.connect({ host: '127.0.0.1', port });
    let buf = '';
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      clearTimeout(t);
      try { s.destroy(); } catch { /* ignore */ }
      resolve(v);
    };
    const t = setTimeout(() => finish(null), timeout);
    s.once('connect', () => {
      s.write(`GET ${path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n`);
    });
    s.on('data', (d) => {
      buf += d.toString('latin1');
      if (buf.includes('\r\n\r\n') || buf.length > 4096) finish(buf);
    });
    s.once('end', () => finish(buf));
    s.once('error', () => finish(null));      // ECONNRESET 等 → null
  });
}

/** 返回原始 HTTP 响应文本（拿不到返回 null） */
async function probeHttp(port, path = '/blender-chat/status', timeout = 3000) {
  return rawHttpGet(port, path, timeout);
}

function statusOf(raw) {
  const m = /^HTTP\/\d\.\d\s+(\d{3})/.exec(raw ?? '');
  return m ? Number(m[1]) : 0;
}

async function looksLikeDsh(port, timeout = 3000) {
  const raw = await probeHttp(port, '/blender-chat/status', timeout);
  if (!raw) return false;
  return /^HTTP\/\d\.\d\s+\d{3}/.test(raw);
}

async function detectPort() {
  const seen = new Set();
  const ordered = [];
  for (const p of [givenPort, ...dshCandidatePorts()]) {
    const n = Number(p);
    if (n && !seen.has(n)) { seen.add(n); ordered.push(n); }
  }

  console.log('[0] 探测 DSH 端口');
  console.log(`     候选（动态发现）: ${ordered.join(', ')}`);

  // ★ 打分而不是"第一个命中的就用"：
  //   同一台机器上 DSH 会开好几个端口（实测 8326 / 58169 / 63524 同属一个进程），
  //   其中有些对所有路径都回 401。必须按"能不能问到 /blender-chat"来排。
  //
  //   200 → 插件已装，最好
  //   405 → 路径存在但方法不对，也算装上了
  //   404 → HTTP 服务在，但路由没注册（= 真 DSH，插件没装）
  //   401 → 可能只是别的服务在统一鉴权，也可能 DSH 把这条路由也拦了
  const scored = [];

  const consider = async (p) => {
    if (!(await tcpAlive(p))) return;
    const raw = await probeHttp(p, '/blender-chat/status', 3000);
    if (!raw) {
      console.log(`     ${p} 拒绝 HTTP（不是 web 服务）`);
      return;
    }
    const code = statusOf(raw);
    let rank = code === 200 ? 4 : code === 405 ? 3 : code === 404 ? 2
      : (code === 401 || code === 403) ? 1 : 0;

    // ★ 404 还要看 body：带 {"error":"not found"} 的是别的服务
    //   （实测 9877 dsh-blender-plugin 后端就是这样），
    //   真 DSH Web 回的是空 body。
    if (code === 404) {
      const body = raw.split('\r\n\r\n').slice(1).join('');
      if (body.includes('"not found"') || body.includes('"error"')) {
        rank = 1;
      }
    }

    console.log(`     ${p} → HTTP ${code}（权重 ${rank}）`);
    scored.push({ port: p, code, rank });
  };

  for (const p of ordered) await consider(p);

  if (scored.length === 0) {
    // 常见端口都没命中：扫一段 ephemeral 范围（只做 TCP 探活，快）
    console.log('     常见端口都没命中，扫描 50000–65535…');
    const hits = [];
    for (let base = 50000; base <= 65535; base += 500) {
      const slice = [];
      for (let p = base; p < base + 500 && p <= 65535; p += 1) slice.push(p);
      const res = await Promise.all(slice.map((p) => tcpAlive(p, 60)));
      slice.forEach((p, k) => { if (res[k]) hits.push(p); });
    }
    if (hits.length) console.log(`     开着的端口: ${hits.join(', ')}`);
    for (const p of hits) await consider(p);
  }

  if (scored.length === 0) return 0;

  scored.sort((a, b) => b.rank - a.rank || a.port - b.port);
  const best = scored[0];
  const label = best.code === 200 ? '插件已装'
    : best.code === 404 ? '路由未注册（插件没装）'
      : `HTTP ${best.code}`;
  console.log(`  ✓ 用 ${best.port}（${label}）`);
  for (const s of scored.slice(1)) {
    console.log(`     备选 ${s.port} → HTTP ${s.code}`);
  }
  return best.port;
}

const headers = { 'content-type': 'application/json' };
if (auth) headers.authorization = auth;

let base = '';

async function call(path, init = {}) {
  try {
    const r = await fetch(base + path, {
      ...init,
      headers: { ...headers, ...(init.headers ?? {}) },
      signal: AbortSignal.timeout(40_000),
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* 非 JSON */ }
    return { status: r.status, json, text: text.slice(0, 400) };
  } catch (e) {
    // ⚠ 绝不把裸异常抛给用户 —— 之前的 ECONNRESET 就是这么崩的
    const code = e?.cause?.code ?? e?.code ?? e?.name ?? 'unknown';
    return {
      status: 0,
      json: null,
      text: `${code}: ${e?.message ?? e}`,
    };
  }
}

const line = (s = '') => console.log(s);
const ok = (s) => console.log('  ✓', s);
const bad = (s) => console.log('  ✗', s);

line('='.repeat(56));
line('DSH × Blender 通道自检');
line('='.repeat(56));

const port = await detectPort();
if (!port) {
  bad('没找到在跑的 DSH');
  line('     确认 DSH Desktop / dsh web 已启动。');
  process.exit(1);
}
base = `http://127.0.0.1:${port}`;
line(`目标: ${base}`);
line(`鉴权: ${auth ? '已提供' : '(无)'}`);
line();

// ── 1. 插件路由 ──
line('[1] /blender-chat/status');
const rawStatus = await probeHttp(port, '/blender-chat/status', 8000);
const code = statusOf(rawStatus);
line(`     HTTP ${code || '(无响应)'}`);

if (!rawStatus) {
  bad('端口不应答 HTTP');
  process.exit(2);
}
if (code === 401 || code === 403) {
  bad(`需要鉴权（HTTP ${code}）`);
  line('     从浏览器 DevTools 抄一份 Authorization 头，用 --auth 传入');
  process.exit(2);
}
if (code === 404) {
  bad('路由不存在 —— 插件没装上，或 DSH 没重启');
  line('');
  line('     这个 404 很关键：说明 DSH 的 HTTP 服务在跑，');
  line('     但 /blender-chat 这条路由没注册 —— 插件没被加载。');
  line('');
  line('     依次确认：');
  line('       1. node install.mjs        装插件');
  line('       2. 完全退出 DSH 再启动      热更新不一定加载新插件');
  line('       3. node probe.mjs          再跑一次');
  process.exit(2);
}
if (code !== 200) {
  bad(`意外状态码 HTTP ${code}`);
  line('     ' + (rawStatus ?? '').slice(0, 300));
  process.exit(2);
}

// 200：解析 JSON 体
let stJson = null;
try {
  const body = rawStatus.split('\r\n\r\n').slice(1).join('\r\n\r\n');
  // HTTP/1.0 + Connection: close 的分块/identity 都能直接 JSON.parse
  stJson = JSON.parse(body.trim());
} catch {
  // 分块编码的话再走一次 fetch 取完整体
  try {
    const r = await fetch(base + '/blender-chat/status', {
      headers,
      signal: AbortSignal.timeout(8000),
    });
    stJson = await r.json();
  } catch (e) {
    stJson = null;
  }
}
ok('路由已挂载');
line(`     ${JSON.stringify(stJson)}`);

// ── 2. 发一句 ──
line();
line('[2] /blender-chat/send');
const send = await call('/blender-chat/send', {
  method: 'POST',
  body: JSON.stringify({ sid, text: '只回复两个字：收到' }),
});
if (send.status !== 200) {
  bad(`HTTP ${send.status}: ${send.text}`);
  process.exit(3);
}
ok('已投递给 DSH agent');
line(`     sessionId = ${send.json?.sessionId}`);

// ── 3. 等回复 ──
line();
line('[3] /blender-chat/poll（最多等 90 秒）');
let since = 0;
let finalText = '';
const deadline = Date.now() + 90_000;
let polls = 0;
let sawAnyEvent = false;
while (Date.now() < deadline) {
  polls += 1;
  const r = await call(
    `/blender-chat/poll?sid=${encodeURIComponent(sid)}&since=${since}`,
  );
  if (r.status !== 200) {
    bad(`HTTP ${r.status}: ${r.text}`);
    process.exit(4);
  }
  for (const ev of r.json?.events ?? []) {
    sawAnyEvent = true;
    since = Math.max(since, ev.seq ?? 0);
    if (ev.kind === 'final') finalText += ev.text;
    else if (ev.kind === 'stream') process.stdout.write('.');
  }
  if (finalText) break;
  await new Promise((res) => setTimeout(res, 400));
}

line();
if (finalText) {
  ok(`收到回复（${polls} 次轮询）`);
  line('     ─────────────────────────────');
  line('     ' + finalText.trim().slice(0, 500).replace(/\n/g, '\n     '));
  line('     ─────────────────────────────');
  line();
  line('通道打通。Blender 偏好设置里把「请求方式」切到「走 DSH」即可。');
} else if (sawAnyEvent) {
  bad('收到了事件但没有 final');
  line('     说明 agent 在输出，但最终消息事件名可能不对。');
  line('     看 src/index.js 里 ctx.on(\'agent/assistant/message\', ...)');
  process.exit(5);
} else {
  bad('完全没收到事件');
  line('     可能原因：');
  line('     1. 模型请求失败（看 DSH 日志/界面）');
  line('     2. 事件名不对（agent/assistant-stream / agent/assistant/message）');
  line(`     3. 超过 90 秒（polls=${polls}）`);
  process.exit(5);
}
