/**
 * patch.test.mjs — 守住 cordis.patch.yml 的格式
 *
 * 这轮排查绕了很久，根因就两个：
 *   1. 没有 cordis.patch.yml          → DSH 不知道要 insert 哪个模块
 *   2. 有，但写成平铺 `- id: ...`      → 同样不生效
 *
 * 正确形状（对齐 dsh-office-tools）：
 *
 *   - insert:
 *       - id: dsh-blender-chat
 *         name: dsh-blender-chat
 *
 * 症状一模一样：package.json 看着装上了，但 HTTP 路由 404。
 * 所以必须有测试守住。
 */
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert';

const HERE = path.dirname(new URL(import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, '$1'));
const ROOT = path.join(HERE, '..');

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push([name, true]);
    console.log('  ✓', name);
  } catch (e) {
    results.push([name, false, e]);
    console.log('  ✗', name, ':', e.message);
  }
}

const patchPath = path.join(ROOT, 'cordis.patch.yml');
const pkgPath = path.join(ROOT, 'package.json');

check('cordis.patch.yml 存在', () => {
  assert.ok(fs.existsSync(patchPath), '缺 cordis.patch.yml');
});

check('package.json 声明 dsh.bundle.patch', () => {
  const pj = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
  const patch = pj.dsh?.bundle?.patch;
  assert.ok(patch, '没声明 dsh.bundle.patch');
  const resolved = path.join(ROOT, patch.replace(/^\.\//, ''));
  assert.ok(fs.existsSync(resolved), `指向的文件不存在: ${resolved}`);
});

check('patch 用 - insert: 形状', () => {
  const src = fs.readFileSync(patchPath, 'utf8');
  assert.match(src, /^-\s*insert:/m, '必须是 `- insert:` 开头');
});

check('insert 下是嵌套列表', () => {
  const src = fs.readFileSync(patchPath, 'utf8');
  // insert: 之后的缩进行里要有 `- id:`
  const after = src.split(/^-\s*insert:/m)[1] ?? '';
  assert.match(after, /^\s+-\s*id:/m, 'insert 下面要有嵌套的 `- id:`');
});

check('不能是平铺 - id: 列表', () => {
  const src = fs.readFileSync(patchPath, 'utf8');
  // 顶级（行首）不能直接是 `- id:`（那会被当成 patch 条目而不是 insert）
  assert.doesNotMatch(src, /^-\s*id:/m, '这是平铺列表，DSH 不会加载');
});

check('id 与包名一致', () => {
  const src = fs.readFileSync(patchPath, 'utf8');
  const pj = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
  assert.ok(src.includes(`id: ${pj.name}`), `id 应为 ${pj.name}`);
  assert.ok(src.includes(`name: ${pj.name}`), `name 应为 ${pj.name}`);
});

check('不 import 外部 dsh 包（避免加载期报错）', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src', 'index.js'), 'utf8');
  const imports = [...src.matchAll(/^import\s+.*?from\s+['"]([^'"]+)['"]/gm)]
    .map((m) => m[1]);
  console.log('       imports:', imports.length ? imports : '(无)');
  assert.deepStrictEqual(imports, [],
    '按需引入会让 DSH 在加载期静默失败，应保持零依赖');
});

check('package.json 没有 peerDependencies', () => {
  const pj = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
  const peers = Object.keys(pj.peerDependencies ?? {});
  console.log('       peerDependencies:', peers.length ? peers : '(无)');
  assert.deepStrictEqual(peers, [],
    'peerDependencies 只在 pnpm-cache 里能解析，DSH 主进程可能找不到');
});

check('main 指向存在的文件', () => {
  const pj = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
  const resolved = path.join(ROOT, pj.main.replace(/^\.\//, ''));
  assert.ok(fs.existsSync(resolved), `main 指向的文件不存在: ${resolved}`);
});

const failed = results.filter(([, ok]) => !ok);
console.log();
console.log('='.repeat(50));
if (failed.length) {
  console.log(`✗ ${failed.length} 项失败`);
  process.exit(1);
} else {
  console.log('✓ 全部通过');
}
