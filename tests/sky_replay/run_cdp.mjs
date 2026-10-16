// Drives driver.html in headless Chrome over CDP: real frames, real time. Prints the page's RESULT line,
// and saves screenshots when asked.   node run_cdp.mjs <chrome> <outdir|-> 
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const [chrome, outdir = '-'] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const profile = mkdtempSync(join(tmpdir(), 'sky-cdp-'));
const chromeFlags = JSON.parse(readFileSync(join(here, '..', 'chrome_flags.json'), 'utf8')).flags;
const proc = spawn(chrome, [...chromeFlags, '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--window-size=1180,700', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Chrome must not outlive this process on any exit node can see (a signal, a thrown error, the parent dying);
// a SIGKILL is invisible to node, and the Python runner (tests/_page_drive_run.py) kills the whole process group.
const scratch = /\/levain-run-[^/]+\/?$/.test(tmpdir()) ? tmpdir() : null;   // the runner's own TMPDIR; see _page_drive.mjs
const cleanup = () => { try { proc.kill(); } catch {} try { rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); if (scratch) rmSync(scratch, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch {} };
const bail = (code) => {
  const fin = () => { cleanup(); process.exit(code); };
  if (exited || proc.exitCode !== null || proc.signalCode !== null) return fin();
  proc.once('exit', fin); setTimeout(fin, 3000); proc.kill('SIGKILL');
};
for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(sig, () => bail(130));
process.on('exit', cleanup);
const parent = process.ppid;
setInterval(() => { if (process.ppid !== parent) bail(130); }, 1000).unref();
// Chrome picks a free port itself (port 0) and writes it to <profile>/DevToolsActivePort, the way Puppeteer finds
// it: a guessed port could collide with another Chrome on the runner, which CI hit as "no chrome target".
let exited = null;
let onExit = () => {};
const died = (why) => { exited = exited ?? why; onExit(new Error(`chrome died: ${exited}`)); };
proc.on('exit', (code, signal) => died(`chrome exited (code ${code}, signal ${signal})`));
proc.on('error', (e) => died(`chrome could not start (${e.message})`));
// every later await races Chrome's death, so a crash mid-run fails with its cause instead of hanging
const orDeath = (p) => exited ? Promise.reject(new Error(`chrome died: ${exited}`))
  : Promise.race([p, new Promise((_, rej) => { const prev = onExit; onExit = (e) => { prev(e); rej(e); }; })]);
let port = null;
async function devtoolsPort() {
  for (let i = 0; i < 150; i++) {
    if (exited) throw new Error(`no chrome target: ${exited}`);
    try { const p = parseInt(readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0], 10); if (p > 0) return p; } catch {}
    await sleep(200);
  }
  throw new Error('no chrome target: DevToolsActivePort never appeared in 30 s');
}
async function targets() {
  port = port ?? await devtoolsPort();
  for (let i = 0; i < 150; i++) {
    if (exited) throw new Error(`no chrome target: ${exited}`);
    try { const r = await fetch(`http://127.0.0.1:${port}/json`); const t = (await r.json()).find((x) => x.type === 'page'); if (t) return t; } catch {}
    await sleep(200);
  }
  throw new Error(`no chrome target on port ${port} in 30 s`);
}
async function session(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0; const pend = new Map();
  let closed = null;
  ws.onclose = () => { closed = new Error('the DevTools socket closed'); for (const [, [, rej]] of pend) rej(closed); pend.clear(); };
  await orDeath(new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('the DevTools socket did not open')); }));
  ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pend.has(d.id)) { pend.get(d.id)[0](d); pend.delete(d.id); } };
  return { send: (method, params = {}) => orDeath(new Promise((res, rej) => { if (closed) return rej(closed); const i = ++id; pend.set(i, [res, rej]); ws.send(JSON.stringify({ id: i, method, params })); })), close: () => ws.close() };
}
async function run(query, shotName, midShot) {
  const t = await targets(); const s = await session(t.webSocketDebuggerUrl);
  await s.send('Page.enable');
  await s.send('Emulation.setDeviceMetricsOverride', { width: 1180, height: 700, deviceScaleFactor: 1, mobile: false });
  await s.send('Page.navigate', { url: 'file://' + resolve(here, 'driver.html') + query });
  const shot = async (name) => { if (outdir === '-') return; const r = await s.send('Page.captureScreenshot', { format: 'png' }); writeFileSync(join(outdir, name), Buffer.from(r.result.data, 'base64')); };
  if (midShot) { await sleep(520); await shot(midShot); }
  let res = '';
  for (let i = 0; i < 40 && !res.startsWith('RESULT'); i++) { await sleep(300); const r = await s.send('Runtime.evaluate', { expression: 'document.getElementById("result").textContent', returnByValue: true }); res = r.result.result.value; }
  if (shotName) await shot(shotName);
  s.close(); return res;
}
try {
  console.log('multi', await run('', outdir === '-' ? null : 'ck3_sky.png', outdir === '-' ? null : 'ck3_sky_midflight.png'));
  console.log('single', await run('?single=repo-alpha', outdir === '-' ? null : 'ck3_sky_single_repo.png'));
  console.log('hidden', await run('?hidden=1', null));
} finally {
  if (!exited) {
    let timer; const gone = new Promise((r) => proc.once('exit', r));
    proc.kill(); await Promise.race([gone, new Promise((r) => { timer = setTimeout(r, 5000); })]); clearTimeout(timer);
  }
  // cleanup must never fail a run or replace its error: Chrome's helpers can still hold the profile briefly
  try { rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch {}
}
