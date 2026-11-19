/**
 * install.mjs — 把 dsh-blender-chat 装进指定的 DSH profile
 *
 * 用法：
 *   node install.mjs                          # 装进默认 profile（desktop）
 *   node install.mjs --profile <名字>
 *   node install.mjs --dry-run                # 只看会改什么，不落盘
 *   node install.mjs --uninstall              # 卸载
 *
 * 为什么用 Node 而不是 PowerShell：
 *   本项目已被 PowerShell 的 Set-Content -Encoding UTF8 坑过 ——
 *   它会把中文按 GBK 往返一次，package.json 里的中文注释全变乱码。
 *   Node 的 fs.writeFileSync(..., 'utf8') 不会。
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const PLUGIN_NAME = 'dsh-blender-chat';

// ── 参数 ──
const argv = process.argv.slice(2);
const dryRun = argv.includes('--dry-run');
const uninstall = argv.includes('--uninstall');
const pi = argv.indexOf('--profile');
const profileName = pi >= 0 ? argv[pi + 1] : 'desktop';

if (!profileName) {
  console.error('用法: node install.mjs [--profile <名字>] [--dry-run] [--uninstall]');
  process.exit(1);
}

// ── 定位 profile ──
const dshHome = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
const profileDir = path.join(dshHome, 'profiles', profileName);

if (!fs.existsSync(profileDir)) {
  console.error(`✗ profile 不存在: ${profileDir}`);
  const base = path.join(dshHome, 'profiles');
  if (fs.existsSync(base)) {
    console.error('  可用的 profile:');
    for (const d of fs.readdirSync(base)) {
      if (fs.statSync(path.join(base, d)).isDirectory()) console.error('   -', d);
    }
  }
  process.exit(1);
}

const pkgPath = path.join(profileDir, 'package.json');
if (!fs.existsSync(pkgPath)) {
  console.error(`✗ 找不到 ${pkgPath}`);
  process.exit(1);
}

const targetDir = path.join(profileDir, 'node_modules', PLUGIN_NAME);
const sourceDir = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));

console.log('='.repeat(56));
console.log(`DSH profile : ${profileDir}`);
console.log(`插件源码    : ${sourceDir}`);
console.log(`安装目标    : ${targetDir}`);
console.log(`模式        : ${uninstall ? '卸载' : (dryRun ? '试运行' : '安装')}`);
console.log('='.repeat(56));

// ── 复制文件 ──
function copyPlugin() {
  fs.mkdirSync(path.join(targetDir, 'src'), { recursive: true });
  for (const f of fs.readdirSync(path.join(sourceDir, 'src'))) {
    if (f.endsWith('.js')) {
      fs.copyFileSync(
        path.join(sourceDir, 'src', f),
        path.join(targetDir, 'src', f),
      );
      console.log(`  复制 src/${f}`);
    }
  }
  // ⚠ cordis.patch.yml 必须带上：
  //   DSH 靠 dsh.bundle.patch 找到它，才知道要 insert 哪个模块。
  //   漏了这个文件，表现是"装上了但路由 404"。
  for (const f of ['package.json', 'cordis.patch.yml', 'README.md']) {
    const s = path.join(sourceDir, f);
    if (!fs.existsSync(s)) {
      if (f === 'cordis.patch.yml') {
        console.error(`  ✗ 缺 ${f} —— DSH 将无法加载本插件`);
        process.exit(1);
      }
      continue;
    }
    fs.copyFileSync(s, path.join(targetDir, f));
    console.log(`  复制 ${f}`);
  }
}

// ── 改 profile/package.json ──
function editProfile(enable) {
  const raw = fs.readFileSync(pkgPath, 'utf8');
  const pkg = JSON.parse(raw);

  pkg.dependencies ??= {};
  pkg.dsh ??= {};
  pkg.dsh.profile ??= {};
  pkg.dsh.profile.bundles ??= [];

  const bundles = pkg.dsh.profile.bundles;
  const has = bundles.includes(PLUGIN_NAME);

  if (enable) {
    if (pkg.dependencies[PLUGIN_NAME] === undefined) {
      // file: 指向本地目录，DSH 会按路径加载
      pkg.dependencies[PLUGIN_NAME] = `file:${targetDir.replace(/\\/g, '/')}`;
      console.log(`  + dependencies["${PLUGIN_NAME}"]`);
    }
    if (!has) {
      bundles.push(PLUGIN_NAME);
      console.log(`  + bundles += "${PLUGIN_NAME}"`);
    } else {
      console.log('  = bundles 里已有，跳过');
    }
  } else {
    if (pkg.dependencies[PLUGIN_NAME] !== undefined) {
      delete pkg.dependencies[PLUGIN_NAME];
      console.log(`  - dependencies["${PLUGIN_NAME}"]`);
    }
    const i = bundles.indexOf(PLUGIN_NAME);
    if (i >= 0) {
      bundles.splice(i, 1);
      console.log(`  - bundles -= "${PLUGIN_NAME}"`);
    }
  }

  const out = JSON.stringify(pkg, null, 2) + '\n';
  if (dryRun) {
    console.log('\n--- 将要写入的 package.json ---');
    console.log(out);
    return;
  }

  // 先备份（只留一份，避免堆一堆）
  const bak = `${pkgPath}.bak-blender-chat`;
  if (!fs.existsSync(bak)) {
    fs.copyFileSync(pkgPath, bak);
    console.log(`  备份 -> ${path.basename(bak)}`);
  }
  fs.writeFileSync(pkgPath, out, 'utf8');
  console.log(`  ✓ 已写入 package.json`);
}

// ── 主流程 ──
try {
  if (uninstall) {
    editProfile(false);
    if (!dryRun && fs.existsSync(targetDir)) {
      fs.rmSync(targetDir, { recursive: true, force: true });
      console.log('  ✓ 已删除插件目录');
    }
    if (!dryRun) {
      console.log('\n已卸载。重启 DSH 生效。');
    }
  } else {
    if (!dryRun) copyPlugin();
    editProfile(true);
    if (!dryRun) {
      console.log('\n装完了。下一步：');
      console.log('  1. 重启 DSH（Desktop 可在插件页刷新）');
      console.log('  2. 跑一遍自检： node probe.mjs');
      console.log('  3. 自检通过后，Blender 面板就能借用 DSH 的模型了');
    }
  }
} catch (e) {
  console.error('\n✗ 失败:', e.message);
  console.error('  备份在:', pkgPath + '.bak-blender-chat');
  process.exit(1);
}
