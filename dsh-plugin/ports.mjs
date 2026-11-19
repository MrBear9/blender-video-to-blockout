/**
 * ports.mjs — 动态找出 DSH 正在监听的所有端口
 *
 * 为什么不能硬编码：
 *   DSH 每次启动都换端口，实测见过 53064 → 64944 → 58169 → 63524 → 49648。
 *   写死候选单必然跟不上。
 *
 * 做法（Windows）：
 *   1. 用 `netstat -ano` 拿到所有 LISTEN 的 (端口, PID)
 *   2. 找出进程名匹配 DeepSeek 的 PID
 *   3. 取这些 PID 的端口
 *
 * 非 Windows / 拿不到时退回常见端口列表。
 */
import { execFileSync } from 'node:child_process';
import os from 'node:os';

/** 进程名里出现这些才算 DSH 侧 */
const PROC_HINTS = ['deepseek', 'dsh', 'electron'];

/** 兜底候选（本机实测出现过的） */
export const FALLBACK_PORTS = [49648, 63524, 64944, 53064, 58169, 8326];

/**
 * 拿所有 LISTEN 状态的 (port, pid)。
 * @returns {Array<{port:number,pid:number}>}
 */
export function listListening() {
  const out = [];
  try {
    if (os.platform() === 'win32') {
      const text = execFileSync('netstat', ['-ano'], {
        encoding: 'utf8',
        timeout: 8000,
        windowsHide: true,
      });
      for (const line of text.split(/\r?\n/)) {
        //  TCP    127.0.0.1:49648    0.0.0.0:0    LISTENING    38200
        const m = /^\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$/i
          .exec(line);
        if (!m) continue;
        out.push({ port: Number(m[2]), pid: Number(m[3]) });
      }
    } else {
      const text = execFileSync('sh', ['-c', 'ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null'], {
        encoding: 'utf8',
        timeout: 8000,
      });
      for (const line of text.split(/\r?\n/)) {
        const m = /:(\d+)\s.*?pid=(\d+)/.exec(line);
        if (m) out.push({ port: Number(m[1]), pid: Number(m[2]) });
      }
    }
  } catch {
    return [];
  }
  return out;
}

/** 拉进程名映射 pid -> name */
export function processNames(pids) {
  const map = new Map();
  if (pids.length === 0) return map;
  try {
    if (os.platform() === 'win32') {
      const text = execFileSync('tasklist', ['/FO', 'CSV', '/NH'], {
        encoding: 'utf8',
        timeout: 8000,
        windowsHide: true,
      });
      for (const line of text.split(/\r?\n/)) {
        const m = /^"([^"]+)","(\d+)"/.exec(line.trim());
        if (!m) continue;
        const pid = Number(m[2]);
        if (pids.includes(pid)) map.set(pid, m[1]);
      }
    } else {
      for (const pid of pids) {
        try {
          const name = execFileSync('ps', ['-p', String(pid), '-o', 'comm='], {
            encoding: 'utf8', timeout: 3000,
          }).trim();
          if (name) map.set(pid, name);
        } catch { /* ignore */ }
      }
    }
  } catch { /* ignore */ }
  return map;
}

/**
 * 对候选端口评分，找出「最像 DSH Web」的那个。
 *
 * 打分口径（与 Blender 端 dsh_client.detect_port 完全一致）：
 *   200 → 4  插件已装
 *   405 → 3  路径在，方法不对
 *   404 → 2  是真 DSH，但路由没注册（= 插件没装）
 *   401/403 → 1  别的服务在统一鉴权
 *
 * ⚠ 关键补充：404 还要看 body
 *   实测本机 9877（dsh-blender-plugin 后端）也回 404，
 *   但它回的是 {"ok":false,"error":"not found"}，
 *   而真 DSH Web 回的是**空 body**。带 JSON 错误体的降到 1。
 *
 * @param {number[]} ports
 * @param {(port:number, path:string, timeout?:number)=>Promise<string|null>} rawGet
 * @returns {Promise<{port:number, code:number, rank:number, all:Array}>}
 */
export async function pickDshPort(ports, rawGet) {
  const all = [];
  for (const p of ports) {
    const raw = await rawGet(p, '/blender-chat/status', 2500);
    if (!raw) {
      all.push({ port: p, code: 0, rank: -1, note: '非 HTTP' });
      continue;
    }
    const m = /^HTTP\/\d\.\d\s+(\d{3})/.exec(raw);
    const code = m ? Number(m[1]) : 0;
    if (code === 0) {
      all.push({ port: p, code: 0, rank: -1, note: '非 HTTP' });
      continue;
    }

    let rank = code === 200 ? 4 : code === 405 ? 3
      : code === 404 ? 2 : (code === 401 || code === 403) ? 1 : 0;
    let note = '';

    if (code === 404) {
      const body = raw.split('\r\n\r\n').slice(1).join('');
      if (body.includes('"not found"') || body.includes('"error"')) {
        rank = 1;
        note = '带 JSON 错误体（不是 DSH Web）';
      }
    }
    if (code === 401) {
      const body = raw.split('\r\n\r\n').slice(1).join('');
      if (body.includes('api key') || body.includes('invalid_api_key')) {
        rank = 0;
        note = 'OpenAI 风格网关';
      }
    }

    all.push({ port: p, code, rank, note });
  }

  const scored = all.filter((x) => x.rank >= 0);
  if (scored.length === 0) return { port: 0, code: 0, rank: -1, all };
  scored.sort((a, b) => b.rank - a.rank || a.port - b.port);
  return { ...scored[0], all };
}
export function dshCandidatePorts() {
  const listening = listListening();
  if (listening.length === 0) return [...FALLBACK_PORTS];

  const pids = [...new Set(listening.map((x) => x.pid))];
  const names = processNames(pids);

  const hitPids = new Set();
  for (const [pid, name] of names) {
    const low = String(name).toLowerCase();
    if (PROC_HINTS.some((h) => low.includes(h))) hitPids.add(pid);
  }

  const ports = [];
  for (const { port, pid } of listening) {
    if (hitPids.has(pid) && !ports.includes(port)) ports.push(port);
  }

  // DSH 侧一个都没识别到时，退回兜底列表（再拼上所有本地监听端口）
  if (ports.length === 0) {
    for (const p of FALLBACK_PORTS) if (!ports.includes(p)) ports.push(p);
  }

  // 把已知高频端口提到前面（63524 这类作为 web 入口的概率更高）
  ports.sort((a, b) => {
    const rank = (p) => (FALLBACK_PORTS.includes(p)
      ? FALLBACK_PORTS.indexOf(p) : 999);
    return rank(a) - rank(b) || a - b;
  });
  return ports;
}
