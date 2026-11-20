/**
 * doctor.mjs — 插件没被加载时，逐项排查到底断在哪
 *
 * 用法： node doctor.mjs
 *
 * 它检查 5 件事，每件都能给出「是/否」和修法：
 *   1. 插件目录在不在、文件全不全
 *   2. profile/package.json 里登记了没
 *   3. 模块能不能被 Node 正常 import
 *   4. apply() 能不能跑完（用假 ctx，不碰真 DSH）
 *   5. DSH 那边路由挂上没（要 DSH 在跑）
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import { dshCandidatePorts, pickDshPort } from './ports.mjs';

const argv = process.argv.slice(2);
const pi = argv.indexOf('--profile');
const profileName = pi >= 0 ? argv[pi + 1] : 'desktop';

const dshHome = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
const profileDir = path.join(dshHome, 'profiles', profileName);
const pluginDir = path.join(profileDir, 'node_modules', 'dsh-blender-chat');
const pkgPath = path.join(profileDir, 'package.json');

const rows = [];
function item(name, ok, detail, fix) {
  rows.push({ name, ok, detail, fix });
  console.log(`  ${ok ? '✓' : '✗'} ${name}`);
  if (detail) console.log(`      ${detail}`);
  if (!ok && fix) console.log(`      → ${fix}`);
}

console.log('='.repeat(58));
console.log('dsh-blender-chat 诊断');
console.log('='.repeat(58));
console.log(`profile: ${profileDir}`);
console.log();

// ── 1. 文件 ──
console.log('[1] 插件文件');
// ⚠ cordis.patch.yml 是 DSH 找到插件的唯一凭据。
//   没有它 → dsh.bundle.patch 指向空 → 插件不会被 insert，
//   表现就是"装上了但路由 404"。
const needFiles = ['package.json', 'src/index.js', 'cordis.patch.yml'];
const missing = needFiles.filter(
  (f) => !fs.existsSync(path.join(pluginDir, f)));
if (missing.length === 0) {
  const st = fs.statSync(path.join(pluginDir, 'src/index.js'));
  item('文件齐全', true,
    `src/index.js ${st.size} bytes, ${st.mtime.toISOString()}`);
} else {
  item('文件齐全', false, `缺: ${missing.join(', ')}`,
    '跑 node install.mjs（会带上 cordis.patch.yml）');
  rows[rows.length - 1].fatal = true;
}

// 顺便确认 package.json 里的 dsh.bundle.patch 指对了
if (!missing.includes('package.json')) {
  try {
    const pj = JSON.parse(
      fs.readFileSync(path.join(pluginDir, 'package.json'), 'utf8'));
    const patch = pj.dsh?.bundle?.patch;
    const resolved = patch
      ? path.join(pluginDir, patch.replace(/^\.\//, ''))
      : '';
    const okPatch = Boolean(patch) && fs.existsSync(resolved);
    item('dsh.bundle.patch 指向存在', okPatch,
      patch ? `${patch} → ${resolved}` : '(没声明 dsh.bundle.patch)',
      'package.json 里要有 dsh.bundle.patch，且文件真实存在');
  } catch (e) {
    item('dsh.bundle.patch 指向存在', false, e.message, '检查 package.json');
  }
}

// ── 2. 登记 ──
console.log();
console.log('[2] profile 登记');
let pkg = null;
try {
  pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
} catch (e) {
  item('读 profile/package.json', false, e.message, '检查文件是否损坏');
}
if (pkg) {
  const dep = pkg.dependencies?.['dsh-blender-chat'];
  item('dependencies 有它', dep !== undefined, dep ?? '(无)',
    '跑 node install.mjs');

  const bundles = pkg.dsh?.profile?.bundles ?? [];
  const inBundles = bundles.includes('dsh-blender-chat');
  item('bundles 有它', inBundles,
    inBundles ? `第 ${bundles.indexOf('dsh-blender-chat') + 1} 个` : '(无)',
    '跑 node install.mjs');
}

// ── 3. 模块可导入 ──
console.log();
console.log('[3] 模块可导入');
let mod = null;
try {
  const url = new URL(`file://${pluginDir.replace(/\\/g, '/')}/src/index.js`);
  mod = await import(url.href);
  item('import 成功', true,
    `name=${mod.name} inject=${JSON.stringify(mod.inject)}`);
} catch (e) {
  item('import 成功', false, `${e.constructor.name}: ${e.message}`,
    '看错误信息；常见是缺依赖或语法错误');
  rows[rows.length - 1].fatal = true;
}

// ── 4. apply 能跑 ──
if (mod?.apply) {
  console.log();
  console.log('[4] apply() 冒烟');
  const routes = [];
  const ctx = {
    logger: {
      info: (s) => console.log(`      [info] ${s}`),
      warn: (s) => console.log(`      [warn] ${s}`),
      error: (s) => console.log(`      [error] ${s}`),
    },
    agents: {
      create: async () => ({
        agent: {
          session: { id: 'x' }, status: 'idle',
          followup() {}, cancel() {},
        },
        sessionId: 'x',
        dispose: async () => {},
      }),
      get: () => undefined,
    },
    on() {},
    inject(_deps, fn) {
      fn({ webServer: { register(s) { routes.push(s.path); return () => {}; } } });
    },
  };
  try {
    mod.apply(ctx, {});
    // 只校验 "路由注册了且都挂在 /blender-chat 前缀下"，
    // 不写死条数 —— 每加一条路由就得改一次 doctor，太脆。
    const bad = routes.filter((p) => !String(p).startsWith('/blender-chat'));
    const ok = routes.length > 0 && bad.length === 0;
    item('apply 跑通', ok,
      `注册了 ${routes.length} 条: ${routes.join(', ')}`,
      ok ? null
        : routes.length === 0 ? '一条路由都没注册'
          : `路由没挂在 /blender-chat 前缀下: ${bad.join(', ')}`);
  } catch (e) {
    item('apply 跑通', false, `${e.constructor.name}: ${e.message}`,
      '看上面的 [error] 输出');
    rows[rows.length - 1].fatal = true;
  }
}

// ── 5. DSH 那边 ──
console.log();
console.log('[5] DSH 路由');
function rawGet(port, p, timeout = 2500) {
  return new Promise((resolve) => {
    const s = net.connect({ host: '127.0.0.1', port });
    let buf = '';
    let done = false;
    const fin = (v) => {
      if (done) return;
      done = true;
      clearTimeout(t);
      try { s.destroy(); } catch { /* ignore */ }
      resolve(v);
    };
    const t = setTimeout(() => fin(null), timeout);
    s.once('connect', () => s.write(
      `GET ${p} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n`));
    s.on('data', (d) => {
      buf += d.toString('latin1');
      if (buf.includes('\r\n\r\n')) fin(buf);
    });
    s.once('end', () => fin(buf));
    s.once('error', () => fin(null));
  });
}

const candidates = dshCandidatePorts();
console.log(`      候选端口（动态发现）: ${candidates.join(', ') || '(无)'}`);

// ★ 用统一打分挑端口。之前这里是"第一个 404 就收"，会选中
//   9877（dsh-blender-plugin 后端，404 带 JSON body）而不是真 DSH。
const picked = await pickDshPort(candidates, rawGet);
for (const s of picked.all) {
  if (s.rank < 0) {
    console.log(`     ${s.port} → ${s.note ?? '无响应'}`);
  } else {
    console.log(`     ${s.port} → HTTP ${s.code}（权重 ${s.rank}）`
      + (s.note ? ` ${s.note}` : ''));
  }
}

let routeOk = false;
if (picked.port && picked.code === 200) {
  item(`DSH 路由（端口 ${picked.port}）`, true, 'HTTP 200');
  routeOk = true;
} else if (picked.port) {
  item('DSH 路由', false,
    `端口 ${picked.port} 回 ${picked.code} —— 服务在跑，但插件没被加载`,
    '完全退出 DSH（不是最小化），再重新启动');
} else {
  item('DSH 路由', false, '所有候选端口都不像 DSH Web',
    '确认 DSH 已启动');
}

// ── 汇总 ──
console.log();
console.log('='.repeat(58));
const failed = rows.filter((r) => !r.ok);
if (failed.length === 0) {
  console.log('✓ 全部正常。可以用 node probe.mjs 做端到端验证。');
} else {
  console.log(`✗ ${failed.length} 项有问题：`);
  for (const f of failed) {
    console.log(`   - ${f.name}${f.detail ? '：' + f.detail : ''}`);
    if (f.fix) console.log(`     → ${f.fix}`);
  }
  const beforeDsh = failed.filter((f) => f.name !== 'DSH 路由');
  if (beforeDsh.length === 0 && failed.length === 1) {
    console.log();
    console.log('★ 插件本身完全正常，只是 DSH 进程里没加载它。');
    console.log('   修法（顺序执行）：');
    console.log('     1. 完全退出 DSH —— 托盘图标右键退出，不是关窗口');
    console.log('     2. 重新启动 DSH');
    console.log('     3. node doctor.mjs   再跑一遍');
  }
}
process.exit(failed.length === 0 ? 0 : 1);
