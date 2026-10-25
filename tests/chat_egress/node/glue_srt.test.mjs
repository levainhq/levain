// CH-D2 S4: the glue over the REAL srt 0.0.81, driven by fake_decider.py (srt's decider frames through the spike
// decider's Link). Every listener is 127.0.0.1; port stand-ins as in the spikes (macOS refuses a non-root :80/:443).
// Rows: R15-10-A9, R15-12-B1, R15-14-3, R15-17-5, R15-19-4, R15-23-2, R15-23-3, R15-25-1, R15-30-4, P-9; and P-3's
// runs of C2, C7 and C11 against srt's own dial and pumps (a fail there is a HIGH for the head, never a patch).
// Skipped, with the reason, when node's srt or the Python driver is absent.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer as createHttpServer } from 'node:http';
import { createServer as createNetServer, connect as netConnect } from 'node:net';
import { connect as tlsConnect } from 'node:tls';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..', '..');
const TABLE = join(REPO, 'levain/chat_egress/denied_table.json');
const PY = process.env.CHD2_PYTHON || join(REPO, '.venv/bin/python');
const pkgCands = [process.env.CHD2_SRT_DIST,
  join(homedir(), 'Briefcase/levain/project_memory/1024-6/spike/run/node_modules/@anthropic-ai/sandbox-runtime')].filter(Boolean);
const SRT = pkgCands.map(c => (c.endsWith('sandbox') ? c : join(c, 'dist/sandbox'))).find(d => existsSync(join(d, 'http-proxy.js')));
const srtVersion = SRT ? JSON.parse(readFileSync(join(SRT, '../../package.json'), 'utf8')).version : null;
const SKIP = !SRT ? 'srt not installed (CHD2_SRT_DIST)' : srtVersion !== '0.0.81' ? `srt ${srtVersion} != 0.0.81`
  : !existsSync(PY) ? `no python at ${PY} (CHD2_PYTHON)` : false;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const lines = p => (existsSync(p) ? readFileSync(p, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)) : []);

// --- listeners ---------------------------------------------------------------------------------------------------------
function rawListener(host = '127.0.0.1', port = 0) {
  const st = { conns: 0, open: 0, bytes: 0, text: '', sockets: [] };
  const srv = createNetServer(s => {
    st.conns++; st.open++; st.sockets.push(s);
    s.on('data', d => { st.bytes += d.length; st.text += d.toString('latin1'); });
    s.on('close', () => { st.open--; }); s.on('error', () => {});
  });
  return new Promise(r => srv.listen(port, host, () => { st.port = srv.address().port; st.close = () => { st.sockets.forEach(x => x.destroy()); srv.close(); }; r(st); }));
}
function origin() {
  const st = { reqs: [], open: 0, text: '' };
  const srv = createHttpServer({ keepAlive: true }, (req, res) => {
    st.reqs.push({ method: req.method, url: req.url, raw: req.rawHeaders });
    req.resume(); req.on('end', () => { res.writeHead(200, { 'content-type': 'text/plain', 'content-length': '2' }); res.end('ok'); });
  });
  srv.on('connection', s => { st.open++; s.on('data', d => { st.text += d.toString('latin1'); }); s.on('close', () => { st.open--; }); });
  return new Promise(r => srv.listen(0, '127.0.0.1', () => { st.port = srv.address().port; st.close = () => { srv.closeAllConnections(); srv.close(); }; r(st); }));
}

// --- one launch -----------------------------------------------------------------------------------------------------------
async function launch(name, script, extra = [], { events = 'events.jsonl' } = {}) {
  const dir = mkdtempSync(join(tmpdir(), `chd2s4-${name}-`));
  writeFileSync(join(dir, 'script.json'), JSON.stringify(script));
  const args = [join(HERE, 'fake_decider.py'), dir, join(dir, 'script.json'), '--', '--srt-dist', SRT,
    '--events', join(dir, events), '--denied-table', TABLE, ...extra];
  const p = spawn(PY, args, { stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, CHD2_NODE: process.execPath } });
  let out = '';
  p.stdout.on('data', d => { out += d; });
  for (let i = 0; i < 400 && !/READY \d+/.test(out); i++) await sleep(25);
  const m = /READY (\d+) pid (\d+)/.exec(out);
  assert.ok(m, `driver never ready: ${out}`);
  for (let i = 0; i < 400 && !readFileSync(join(dir, 'proxy.stderr'), 'utf8').includes('glue: '); i++) await sleep(25);
  const L = { dir, port: Number(m[1]), nodePid: Number(m[2]), proc: p,
    events: () => lines(join(dir, 'events.jsonl')),
    frames: () => lines(join(dir, 'frames.jsonl')).filter(f => f.way === 'in' && f.msg.t === 'req').map(f => f.msg),
    obs: () => lines(join(dir, 'obs.raw')), obsRaw: () => (existsSync(join(dir, 'obs.raw')) ? readFileSync(join(dir, 'obs.raw')) : Buffer.alloc(0)),
    stderr: () => readFileSync(join(dir, 'proxy.stderr'), 'utf8'),
    async stop() {
      if (L.code !== undefined) return L.code;
      p.stdin.end('\n');
      for (let i = 0; i < 600 && !/EXIT -?\d+/.test(out); i++) await sleep(25);
      L.code = Number(/EXIT (-?\d+)/.exec(out)?.[1]);
      await new Promise(r => (p.exitCode !== null ? r() : p.on('exit', r)));
      return L.code;
    } };
  return L;
}
const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };
// Whether the glue still accepts on its port. The driver closed its copy of the listen fd, so a refusal means the glue
// is gone (a pid probe cannot say: an exited glue is a zombie until the driver reaps it at stop()).
const serving = port => new Promise(res => {
  const s = netConnect(port, '127.0.0.1');
  s.on('connect', () => { s.destroy(); res(true); }); s.on('error', () => res(false));
});
const untilGone = async port => { for (let i = 0; i < 200 && await serving(port); i++) await sleep(10); return !(await serving(port)); };

// --- a raw HTTP/1.1 client: one socket, requests in sequence --------------------------------------------------------
function conn(port) {
  const s = netConnect(port, '127.0.0.1');
  let buf = Buffer.alloc(0); let waiter = null; let closed = false;
  const pump = () => {
    if (!waiter) return;
    const i = buf.indexOf('\r\n\r\n');
    if (i < 0) { if (closed) { waiter.res({ status: 0, closed: true }); waiter = null; } return; }
    const head = buf.subarray(0, i).toString('latin1');
    const status = Number(head.split(' ')[1]);
    let end;
    if (/transfer-encoding:\s*chunked/i.test(head)) {
      const z = buf.indexOf('\r\n0\r\n\r\n', i + 4);
      const z0 = buf.subarray(i + 4, i + 9).toString('latin1') === '0\r\n\r\n' ? i + 9 : -1;
      if (z < 0 && z0 < 0) { if (!closed) return; end = buf.length; } else end = z0 >= 0 ? z0 : z + 7;
    } else {
      const cl = Number(/content-length:\s*(\d+)/i.exec(head)?.[1] ?? 0);
      if (buf.length < i + 4 + cl && !closed) return;
      end = i + 4 + cl;
    }
    const body = buf.subarray(i + 4, end).toString('latin1');
    buf = buf.subarray(end);
    const w = waiter; waiter = null; w.res({ status, head, body });
  };
  s.on('data', d => { buf = Buffer.concat([buf, d]); pump(); });
  s.on('close', () => { closed = true; pump(); }); s.on('error', () => {});
  const send = (text, connect = false) => new Promise(res => { waiter = { res, connect }; s.write(text); pump(); });
  return { s, send, get closed() { return closed; }, destroy: () => s.destroy() };
}
const get = (target, host, extra = '') => `GET ${target} HTTP/1.1\r\nHost: ${host}\r\n${extra}\r\n`;

let O, RAW, TLSRAW, OFF;
before(async () => {
  if (SKIP) return;
  O = await origin(); RAW = await rawListener(); TLSRAW = await rawListener(); OFF = await rawListener();
});
after(() => { for (const x of [O, RAW, TLSRAW, OFF]) x?.close(); });

function baseArgs() {
  const carve = [O.port, RAW.port, TLSRAW.port, OFF.port].map(p => `127.0.0.1:${p}`).join(',');
  return ['--tls-ports', `443,${TLSRAW.port}`, '--http-ports', `80,${O.port},${RAW.port}`, '--carve', carve,
    '--resolve-map', 'pub.rb.test=127.0.0.1;loop.rb.test=127.0.0.2'];
}
const script = (o = {}) => ({ remove: ['authorization'], literal_wall: true, sni_rule: false,
  tls_ports: [443, TLSRAW.port], http_ports: [80, O.port, RAW.port], ...o });
const snap = l => ({ conns: l.conns, bytes: l.bytes });

// Measured (S4, 10-24): srt CLOSES the client connection after a decider deny (endDeniedBody), its `Connection:
// keep-alive` header notwithstanding, so a request after a deny never arrives on that connection (fail-closed). The
// sequence therefore puts the dial-time refusal (which keeps the connection) before the decider's literal wall.
test('R15-10-A9 + R15-30-4: one keep-alive plain connection: public, name->loopback, literal loopback: walls 2 and 3', { skip: SKIP }, async () => {
  const L = await launch('a9', script(), baseArgs());
  try {
    const before = O.reqs.length;
    const c = conn(L.port);
    const r1 = await c.send(get(`http://pub.rb.test:${O.port}/a9-1`, `pub.rb.test:${O.port}`));
    const r2 = await c.send(get(`http://loop.rb.test:${O.port}/a9-2`, `loop.rb.test:${O.port}`));
    assert.equal(c.closed, false, 'the connection survives the dial-time refusal');
    const r3 = await c.send(get(`http://127.0.0.1:${O.port}/a9-3`, `127.0.0.1:${O.port}`));
    assert.deepEqual([r1.status, r2.status, r3.status], [200, 403, 403], JSON.stringify([r2.head, r3.head]));
    assert.match(r2.head, /X-Deny-Reason: address_not_allowed/);
    c.destroy();
    await sleep(300);
    assert.deepEqual(O.reqs.slice(before).map(r => r.url), ['/a9-1']);
    assert.deepEqual(L.frames().map(f => f.host), ['pub.rb.test', 'loop.rb.test', '127.0.0.1'], 'the decider is asked per request');
    const ids = L.events().filter(e => e.t === 'req').map(e => e.request_id);
    // resp for 1; ended for 2 (srt's 403 after the wrapper's allow, R15-30-4); nothing for 3 (a deny)
    assert.deepEqual(L.obs().map(o => [o.t, o.request_id]), [['resp', ids[0]], ['ended', ids[1]]]);
  } finally { assert.equal(await L.stop(), 0); }
});

test('R15-12-B1: CONNECT off 443, CONNECT to 80, plain off 80 -> walls, zero decider frames, zero upstream bytes; P-9 flush', { skip: SKIP }, async () => {
  const L = await launch('b1', script(), baseArgs());
  const s0 = [snap(OFF), snap(O), snap(TLSRAW)];
  try {
    const a = conn(L.port);
    const r1 = await a.send(`CONNECT pub.rb.test:${OFF.port} HTTP/1.1\r\nHost: pub.rb.test:${OFF.port}\r\n\r\n`, true);
    const b = conn(L.port);
    const r2 = await b.send(`CONNECT pub.rb.test:${O.port} HTTP/1.1\r\nHost: pub.rb.test:${O.port}\r\n\r\n`, true);
    const c = conn(L.port);
    const r3 = await c.send(get(`http://pub.rb.test:${TLSRAW.port}/b1`, `pub.rb.test:${TLSRAW.port}`));
    for (const x of [a, b, c]) x.destroy();
    assert.deepEqual([r1.status, r2.status, r3.status], [403, 403, 403]);
    await sleep(300);
    assert.deepEqual([snap(OFF), snap(O).conns === s0[1].conns, snap(TLSRAW)], [s0[0], true, s0[2]]);
    assert.equal(L.frames().length, 0);
    const walls = L.events().filter(e => e.t === 'wall');
    assert.deepEqual(walls.map(w => [w.reason, w.request_id, w.method, w.host]),
      [['port', null, null, 'pub.rb.test'], ['port', null, null, 'pub.rb.test'], ['port', null, null, 'pub.rb.test']]);
  } finally {
    assert.equal(await L.stop(), 0);
    const fin = L.events().at(-1);
    assert.deepEqual([fin.t, fin.final, fin.total, fin.by_reason], ['summary', true, 3, { port: 3 }]);
  }
});

test('R15-17-5 / R15-19-4 / R15-23-2: plain GET https://host:<http port>/ -> glue wall: zero frames, no lookup, no bytes, ONE event', { skip: SKIP }, async () => {
  const L = await launch('p5', script(), baseArgs());
  const s0 = snap(RAW);
  try {
    const c = conn(L.port);
    const r = await c.send(get(`https://pub.rb.test:${RAW.port}/p5-https?q=SECRETQ`, `pub.rb.test:${RAW.port}`));
    assert.equal(r.status, 403);
    const ws = await conn(L.port).send(get(`ws://pub.rb.test:${RAW.port}/w`, `pub.rb.test:${RAW.port}`));
    const ftp = await conn(L.port).send(get(`ftp://pub.rb.test:${RAW.port}/f`, `pub.rb.test:${RAW.port}`));
    assert.deepEqual([ws.status, ftp.status], [400, 400], 'ws:// and ftp:// are srt\'s own 400');
    c.destroy();
    await sleep(300);
    assert.deepEqual(snap(RAW), s0);
    assert.equal(L.frames().length, 0, 'zero decider frames');
    assert.ok(!L.stderr().includes('lookup pub.rb.test'), 'no DNS lookup');
    const evs = L.events();
    assert.equal(evs.length, 1);
    assert.deepEqual([evs[0].t, evs[0].reason, evs[0].method, evs[0].host], ['wall', 'scheme', 'GET', 'pub.rb.test']);
    assert.ok(!readFileSync(join(L.dir, 'events.jsonl'), 'utf8').match(/p5-https|SECRETQ/));
  } finally { assert.equal(await L.stop(), 0); }
});

test('R15-25-1 / R15-23-3: every x-levain spelling dropped; the frame holds exactly the glue id; the origin gets no x-levain byte', { skip: SKIP }, async () => {
  const L = await launch('audit', script(), baseArgs());
  try {
    const t0 = O.text.length;
    const extra = 'X-Levain-Audit: forged\r\nx_levain_audit: f2\r\nx.levain.audit: f3\r\nX-Levain-Audit: dup\r\n'
      + 'X-Levain-Other: o\r\nAuthorization: Bearer TOPSECRET\r\n';
    const c = conn(L.port);
    const r = await c.send(get(`http://pub.rb.test:${O.port}/audit`, `pub.rb.test:${O.port}`, extra));
    const r2 = await c.send(get(`http://pub.rb.test:${O.port}/plain`, `pub.rb.test:${O.port}`));
    assert.deepEqual([r.status, r2.status], [200, 200]);
    c.destroy();
    await sleep(300);
    const fr = L.frames();
    const lev = fr.map(f => Object.entries(f.headers).filter(([n]) => n.replace(/[_.]/g, '-').startsWith('x-levain-')));
    const ev = L.events().filter(e => e.t === 'req');
    assert.deepEqual(lev, [[['x-levain-audit', [ev[0].request_id]]], [['x-levain-audit', [ev[1].request_id]]]]);
    assert.notEqual(ev[0].request_id, ev[1].request_id);
    assert.deepEqual([ev[0].dropped_count, ev[1].dropped_count], [4, 0]);
    const seen = O.text.slice(t0);
    assert.ok(!/levain/i.test(seen), 'the origin received an x-levain byte');
    assert.ok(!seen.includes('TOPSECRET'), 'the decider\'s removal reached srt');
    assert.deepEqual(L.obs().map(o => [o.t, o.request_id, o.status]), [['resp', ev[0].request_id, 200], ['resp', ev[1].request_id, 200]]);
  } finally { assert.equal(await L.stop(), 0); }
});

test('R15-14-3: fifth-argument test OFF -> CONNECT to an http port still walls by the 443-only TLS termination alone', { skip: SKIP }, async () => {
  const L = await launch('l2', script({ sni_rule: false }), [...baseArgs(), '--fifth-arg', 'off']);
  const s0 = snap(RAW);
  try {
    const c = conn(L.port);
    const r = await c.send(`CONNECT pub.rb.test:${RAW.port} HTTP/1.1\r\nHost: pub.rb.test:${RAW.port}\r\n\r\n`, true);
    if (r.status === 200) c.s.write('GET / HTTP/1.1\r\nHost: x\r\n\r\n');
    await sleep(500);
    c.destroy();
    assert.notEqual(r.status, 200, `srt opened the tunnel: ${r.head}`);
    assert.deepEqual(snap(RAW), s0);
    assert.equal(L.frames().length, 0);
  } finally { assert.equal(await L.stop(), 0); }
});

test('R15-14-3: fifth-argument test OFF -> plain HTTP to the TLS port walls by the decider\'s SNI rule alone (control: rule off reaches)', { skip: SKIP }, async () => {
  for (const [rule, want] of [[true, 'walled'], [false, 'reached']]) {
    const L = await launch(`l3${rule}`, script({ sni_rule: rule }), [...baseArgs(), '--fifth-arg', 'off']);
    const s0 = snap(TLSRAW);
    try {
      const c = conn(L.port);
      c.s.write(get(`http://pub.rb.test:${TLSRAW.port}/l3`, `pub.rb.test:${TLSRAW.port}`));
      await sleep(800);
      c.destroy();
      const got = TLSRAW.bytes > s0.bytes ? 'reached' : 'walled';
      assert.equal(got, want, `sni_rule=${rule}`);
      assert.equal(L.frames().length, 1, 'the decider was asked');
    } finally { await L.stop(); }
  }
});

test('R15-14-3: fifth-argument OFF + TLS terminated on every port -> a TLS CONNECT to an http port walls by the SNI rule alone', { skip: SKIP }, async () => {
  const L = await launch('l3tls', script({ sni_rule: true }),
    [...baseArgs(), '--fifth-arg', 'off', '--terminate-ports', `443,${TLSRAW.port},${O.port},${RAW.port}`]);
  const before = O.reqs.length;
  try {
    const c = conn(L.port);
    const r = await c.send(`CONNECT pub.rb.test:${O.port} HTTP/1.1\r\nHost: pub.rb.test:${O.port}\r\n\r\n`, true);
    assert.equal(r.status, 200, 'the union admits it and srt terminates TLS on that port');
    const t = tlsConnect({ socket: c.s, servername: 'pub.rb.test', ca: readFileSync(join(L.dir, 'ca.pem')) });
    const resp = await new Promise((res, rej) => {
      let b = ''; t.on('data', d => { b += d; if (b.includes('\r\n\r\n')) res(b); }); t.on('error', rej);
      t.on('secureConnect', () => t.write(`GET /l3tls HTTP/1.1\r\nHost: pub.rb.test:${O.port}\r\n\r\n`));
    });
    t.destroy();
    assert.match(resp, /^HTTP\/1\.1 403/);
    await sleep(300);
    assert.equal(O.reqs.length, before);
    assert.equal(L.frames().at(-1).sni, 'pub.rb.test');
  } finally { assert.equal(await L.stop(), 0); }
});

test('P-9: a clean end with no wall writes the final flush, total 0; the node process is gone', { skip: SKIP }, async () => {
  const L = await launch('zero', script(), baseArgs());
  assert.equal(await L.stop(), 0);
  const evs = L.events();
  assert.equal(evs.length, 1);
  assert.deepEqual([evs[0].t, evs[0].final, evs[0].total, evs[0].minute_count], ['summary', true, 0, 0]);
  assert.equal(alive(L.nodePid), false);
});

test('a crash writes no final flush (R15-30-3: K1 tells a crashed launch from a quiet one)', { skip: SKIP }, async () => {
  const L = await launch('crash', script(), baseArgs());
  process.kill(L.nodePid, 'SIGKILL');
  await sleep(300);
  await L.stop();
  assert.equal(L.events().filter(e => e.t === 'summary').length, 0);
});

// --- P-3: C2, C7, C11 describe srt's own dial and pumps: RUN, a fail is a HIGH for the head -----------------------------
test('P-3 C2: a client gone during the decision -> no dial; a client gone at dial completion -> the upstream socket closes', { skip: SKIP }, async () => {
  const L = await launch('c2', script(), baseArgs());
  try {
    const s0 = RAW.conns;
    const a = conn(L.port);
    a.s.write(get(`http://pub.rb.test:${RAW.port}/hold/1.0`, `pub.rb.test:${RAW.port}`));
    await sleep(300); a.destroy(); await sleep(1500);
    assert.equal(RAW.conns, s0, 'srt dialled for a client that left during the decision');
    const b = conn(L.port);
    b.s.write(get(`http://pub.rb.test:${RAW.port}/c2`, `pub.rb.test:${RAW.port}`));
    // srt's plain-path dial is two connects: dialDirect's probe (pins the checked address, then destroyed) and the
    // request's own. Wait for the request's connection to be the one left open.
    for (let i = 0; i < 400 && !(RAW.conns >= s0 + 2 && RAW.open >= 1); i++) await sleep(5);
    console.log(`# C2 measured: upstream connects for one request = ${RAW.conns - s0}, open at client close = ${RAW.open}`);
    assert.ok(RAW.open >= 1, 'the dial completed and is held open by the request');
    b.destroy();
    for (let i = 0; i < 100 && RAW.open > 0; i++) await sleep(20);
    assert.equal(RAW.open, 0, 'the upstream socket outlived its client (C2)');
  } finally { assert.equal(await L.stop(), 0); }
});

test('P-3 C11: after the clients close, no relay/pump holds an upstream socket open', { skip: SKIP }, async () => {
  const L = await launch('c11', script(), baseArgs());
  try {
    // six requests in flight to an upstream that never answers (srt's pumps hold both sides), then every client closes
    const cs = Array.from({ length: 6 }, () => conn(L.port));
    cs.forEach((c, i) => c.s.write(get(`http://pub.rb.test:${RAW.port}/c11-${i}`, `pub.rb.test:${RAW.port}`)));
    for (let i = 0; i < 400 && RAW.open < 6; i++) await sleep(10);
    const held = RAW.open;
    cs.forEach(c => c.destroy());
    for (let i = 0; i < 150 && RAW.open > 0; i++) await sleep(20);
    console.log(`# C11 measured: upstream sockets held mid-exchange = ${held}, open 3 s after every client closed = ${RAW.open}`);
    assert.equal(held, 6, 'six upstream exchanges in flight');
    assert.equal(RAW.open, 0, 'upstream sockets left open after every client closed (C11)');
  } finally { assert.equal(await L.stop(), 0); }
});

// P-3 C7 (HEAD_RULINGS_S4 C7 FINAL; 1024-20 C7_DIAG.md): the old fixture's "hanging" [::1] listener CONNECTED on this
// host (macOS completes loopback handshakes past a listen(0) backlog), so it measured a never-answered request, not
// a hang. The hung family here is a real blackhole, TEST-NET-1 192.0.2.1 (nothing can accept on it), answered FIRST;
// the working family (a [::1] listener) second. The control proves the blackhole: a direct connect never reaches
// `connect` (and is not refused) within 2 s, and a v4-only name through the glue gets no answer and no v6 dial.
test('P-3 C7: 4 A answers that blackhole, then 4 AAAA answers -> the working family dialled within 2 s', { skip: SKIP }, async () => {
  const BH = '192.0.2.1';
  const V6 = await rawListener('::1');
  const P = V6.port;
  const args = ['--tls-ports', '443', '--http-ports', `80,${P}`, '--carve', `${BH}:${P},[::1]:${P},::1:${P}`,
    '--resolve-map', `dual.rb.test=${[BH, BH, BH, BH, '::1', '::1', '::1', '::1'].join(',')};hole.rb.test=${BH}`];
  const L = await launch('c7', script(), args);
  try {
    const probe = await new Promise(res => {
      const s = netConnect({ host: BH, port: P, autoSelectFamily: false });
      const to = setTimeout(() => { s.destroy(); res('hung'); }, 2000);
      s.on('connect', () => { clearTimeout(to); s.destroy(); res('connected'); });
      s.on('error', e => { clearTimeout(to); res(`error ${e.code}`); });
    });
    assert.equal(probe, 'hung', `${BH}:${P} is not a blackhole on this host (${probe}), so C7 is not exercised`);
    const c0 = conn(L.port);
    const r0 = await Promise.race([c0.send(get(`http://hole.rb.test:${P}/hole`, `hole.rb.test:${P}`)), sleep(2500).then(() => ({ status: 'pending' }))]);
    c0.destroy();
    assert.deepEqual([r0.status, V6.conns], ['pending', 0], 'the v4-only name through the glue: no answer, no v6 dial');
    const t = Date.now();
    const c = conn(L.port);
    c.s.write(get(`http://dual.rb.test:${P}/dual`, `dual.rb.test:${P}`));
    for (let i = 0; i < 300 && V6.conns === 0; i++) await sleep(10);
    const ms = Date.now() - t;
    c.destroy();
    console.log(`# C7 measured: blackhole probe ${probe}; v4-only ${r0.status}; dual: v6 dialled=${V6.conns > 0} after ${ms} ms`);
    assert.ok(V6.conns > 0 && ms < 2000, `the AAAA answers were not dialled within 2 s (${ms} ms)`);
  } finally { await L.stop(); V6.close(); }
});

// --- L3 r1 fix round (1024-20 S4_r1_findings.md): the audit record never carries a false clean marker -------------------
test('F2: lifeline EOF with a request pending at the decider -> its `ended` line, THEN the final flush, exit 0', { skip: SKIP }, async () => {
  const L = await launch('f2eof', script(), baseArgs());
  const c = conn(L.port);
  c.s.write(get(`http://pub.rb.test:${O.port}/hold/3`, `pub.rb.test:${O.port}`));
  for (let i = 0; i < 200 && L.frames().length === 0; i++) await sleep(10);
  assert.equal(L.frames().length, 1, 'the request reached the decider');
  const t = Date.now();
  assert.equal(await L.stop(), 0);
  assert.ok(Date.now() - t < 2500, 'the clean end did not wait for the decider');
  const id = L.events().find(e => e.t === 'req').request_id;
  assert.deepEqual(L.obs().map(o => [o.t, o.request_id]), [['ended', id]], 'the pending outcome was lost');
  const fin = L.events().at(-1);
  assert.deepEqual([fin.t, fin.final], ['summary', true]);
});

// a read error on the lifeline (watchLifeline -> fatal(6)) is glue_unit.test.mjs's; this one is a lifeline fd the glue
// cannot read as a socket at all (a directory), which throws before the handler exists
test('F2: an unusable lifeline fd is not a clean end: non-zero, no final flush', { skip: SKIP }, async () => {
  const L = await launch('f2err', script({ lifeline_dir: true }), baseArgs());
  assert.ok(await untilGone(L.port), 'the glue kept serving on an unreadable lifeline');
  const code = await L.stop();
  assert.notEqual(code, 0, L.stderr());
  assert.equal(L.events().filter(e => e.t === 'summary').length, 0, 'a lifeline error wrote the clean marker');
});

test('F1: the observation peer closed while the decider link is open -> the glue fails closed: non-zero, no final flush', { skip: SKIP }, async () => {
  const L = await launch('f1', script({ obs_close: true }), baseArgs());
  const c = conn(L.port);
  await c.send(get(`http://pub.rb.test:${O.port}/f1-1`, `pub.rb.test:${O.port}`));
  const gone = await untilGone(L.port);
  const r2 = await Promise.race([conn(L.port).send(get(`http://pub.rb.test:${O.port}/f1-2`, `pub.rb.test:${O.port}`)), sleep(1500).then(() => ({ status: 'pending' }))]);
  c.destroy();
  const code = await L.stop();
  assert.equal(gone, true, 'the glue kept serving with its outcome channel gone');
  assert.notEqual(r2.status, 200, 'a request was allowed after the outcome channel failed');
  assert.notEqual(code, 0);
  assert.equal(L.events().filter(e => e.t === 'summary').length, 0, 'a failed outcome channel wrote the clean marker');
});

test('F4: an events-file write that fails is fatal on purpose: the glue exits non-zero at the wall, no silent divergence', { skip: SKIP }, async () => {
  const L = await launch('f4', script(), baseArgs(), { events: 'no-such-dir/events.jsonl' });
  const c = conn(L.port);
  const r = await c.send(`CONNECT pub.rb.test:${OFF.port} HTTP/1.1\r\nHost: pub.rb.test:${OFF.port}\r\n\r\n`, true);
  c.destroy();
  const gone = await untilGone(L.port);
  const code = await L.stop();
  assert.notEqual(r.status, 200);
  assert.equal(gone, true, 'the glue kept running with a wall missing from its event file');
  assert.equal(code, 4, L.stderr());
});
