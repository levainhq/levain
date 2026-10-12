// Drives driver.html in headless Chrome over CDP: real frames, real time. Prints the page's RESULT line,
// and saves screenshots when asked.   node run_cdp.mjs <chrome> <outdir|-> 
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const [chrome, outdir = '-'] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const port = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(join(tmpdir(), 'sky-cdp-'));
const proc = spawn(chrome, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--window-size=1180,700', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function targets() {
  for (let i = 0; i < 50; i++) { try { const r = await fetch(`http://127.0.0.1:${port}/json`); const t = (await r.json()).find((x) => x.type === 'page'); if (t) return t; } catch {} await sleep(200); }
  throw new Error('no chrome target');
}
async function session(wsUrl) {
  const ws = new WebSocket(wsUrl); await new Promise((r) => (ws.onopen = r));
  let id = 0; const pend = new Map();
  ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pend.has(d.id)) { pend.get(d.id)(d); pend.delete(d.id); } };
  return { send: (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); }), close: () => ws.close() };
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
} finally { proc.kill(); }
