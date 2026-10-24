// CH-D2 S4: the glue's own pieces under `node --test`, no srt: the filterRequest wrapper's six steps (R15-25-1), the
// per-request machine (P-2: one test per (state, event)), the event fields (R15-25-2, R15-25-3), the flood bound
// (R15-27-1, R15-28-1, R15-28-2, R15-29-2, R15-30-1/-2/-6, P-9) and the escaper against wire.ESCAPE_VECTORS.
// The srt-backed reds are glue_srt.test.mjs; the Python reader of these lines is test_glue_node.py.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  escapeText, jsonLine, createFlood, createFilter, wrapFilterRequest, foldHeaderName, ESCAPE_VECTORS_JS,
} from '../../../levain/chat_egress/node/glue.mjs';

const ID = '0f0e0d0c-0b0a-4908-8706-050403020100';
const parse = lines => lines.map(l => { assert.ok(l.endsWith('\n')); return JSON.parse(l); });

function sink() { const lines = []; const w = l => lines.push(typeof l === 'string' ? l : l.toString()); w.lines = lines; return w; }

function fakeRequest({ url = 'http://h.example/p?a=1&b=2', method = 'GET', raw = [] } = {}) {
  const ac = new AbortController();
  const headers = new Headers();
  for (let i = 0; i < raw.length; i += 2) { try { if (raw[i].toLowerCase() !== 'host') headers.append(raw[i], raw[i + 1]); } catch {} }
  return { request: { url, method, headers, signal: ac.signal }, ac, info: { rawHeaders: [...raw], requestTarget: new URL(url).pathname + new URL(url).search } };
}

function rig(innerImpl, opts = {}) {
  const ev = sink(), obs = sink();
  let t = opts.t0 ?? 1_700_000_000_000;
  const flood = createFlood({ writeLine: ev, now: () => t, schedule: () => {} });
  const seen = [];
  const inner = async (request, info) => { seen.push({ request, info, headers: [...request.headers.entries()] }); return innerImpl(request, info); };
  const fr = wrapFilterRequest(inner, { writeEvent: ev, writeObs: obs, flood, randomUUID: () => ID });
  return { ev, obs, flood, fr, seen, setT: v => { t = v; } };
}

test('escapeText matches wire.ESCAPE_VECTORS (R15-25-2, R15-25-3)', () => {
  assert.ok(ESCAPE_VECTORS_JS.length >= 8);
  for (const [raw, limit, want] of ESCAPE_VECTORS_JS) assert.equal(escapeText(raw, limit), want, JSON.stringify(raw));
});

test('jsonLine escapes as Python json.dumps(ensure_ascii=True, compact)', () => {
  assert.equal(jsonLine({ a: 'é\u007f😀\n', b: null, c: [1] }), '{"a":"\\u00e9\\u007f\\ud83d\\ude00\\n","b":null,"c":[1]}\n');
});

test('wrapper steps 1-4: x-levain names dropped in every spelling, one audit id reaches the inner call (R15-25-1)', async () => {
  const raw = ['Host', 'h.example', 'X-Levain-Audit', 'forged', 'x_levain_audit', 'f2', 'x.levain.audit', 'f3',
    'X-Levain-Audit', 'dup', 'Accept', '*/*'];
  const { fr, seen, ev } = rig(() => ({ action: 'allow' }));
  const { request, info } = fakeRequest({ raw });
  await fr(request, info);
  const got = seen[0];
  const audit = [];
  for (let i = 0; i < got.info.rawHeaders.length; i += 2) {
    const n = foldHeaderName(got.info.rawHeaders[i]);
    if (n.startsWith('x-levain-')) audit.push([n, got.info.rawHeaders[i + 1]]);
  }
  assert.deepEqual(audit, [['x-levain-audit', ID]]);
  assert.deepEqual(got.headers.filter(([n]) => foldHeaderName(n).startsWith('x-levain-')), [['x-levain-audit', ID]]);
  assert.ok(got.info.rawHeaders.includes('Accept'));
  const req = parse(ev.lines).find(e => e.t === 'req');
  assert.equal(req.request_id, ID);
  assert.equal(req.dropped_count, 3);   // three distinct raw spellings (lower-cased), X-Levain-Audit sent twice
});

test('wrapper step 5: an allow removes the decider list + audit + every dropped name, deduped by fold (R15-25-1)', async () => {
  const raw = ['X-Levain-A', '1', 'x_levain_a', '2', 'X-Levain-B', '3'];
  const { fr } = rig(() => ({ action: 'allow', removeHeaders: ['authorization', 'Authorization'] }));
  const { request, info } = fakeRequest({ raw });
  const d = await fr(request, info);
  const folded = d.removeHeaders.map(foldHeaderName);
  assert.deepEqual(new Set(folded), new Set(['authorization', 'x-levain-audit', 'x-levain-a', 'x-levain-b']));
  assert.equal(folded.length, new Set(folded).size);
});

test('wrapper step 5: 189 agent x-levain names -> the allow carries all of them, past srt\'s 190 decider cap (R15-25-1)', async () => {
  const raw = [];
  for (let i = 0; i < 189; i++) raw.push(`x-levain-n${i}`, 'v');
  const { fr } = rig(() => ({ action: 'allow', removeHeaders: ['cookie'] }));
  const { request, info } = fakeRequest({ raw });
  const d = await fr(request, info);
  assert.equal(d.removeHeaders.length, 189 + 2);
});

test('a deny carries no removals and no obs line (R15-25-1 step 5; R-10)', async () => {
  const { fr, obs } = rig(() => ({ action: 'deny', status: 403, reason: 'card: x' }));
  const { request, info, ac } = fakeRequest();
  const d = await fr(request, info);
  assert.equal(d.removeHeaders, undefined);
  ac.abort();
  assert.deepEqual(obs.lines, []);
});

test('R15-25-2: 10 agent names, one 100 chars with a control byte -> count 10, 8 names, cut to 64, escaped', async () => {
  const raw = [];
  for (let i = 0; i < 9; i++) raw.push(`x-levain-k${i}`, 'v');
  const long = 'x-levain-' + 'z'.repeat(40) + '\u0001' + 'y'.repeat(50);
  raw.splice(0, 0, long, 'v');
  const { fr, ev } = rig(() => ({ action: 'allow' }));
  const { request, info } = fakeRequest({ raw });
  // a raw control byte cannot be a Headers name; the walk reads info.rawHeaders, so put it only there
  await fr(request, info);
  const e = parse(ev.lines).find(x => x.t === 'req');
  assert.equal(e.dropped_count, 10);
  assert.equal(e.dropped_names.length, 8);
  assert.equal(e.dropped_names[0], escapeText(long.toLowerCase(), 64));
  assert.ok(e.dropped_names[0].includes('\\x01'));
  assert.ok(!JSON.stringify(e).includes('\u0001'));
});

test('the req event: no path, no query value, no header value; the query-name COUNT (R15-17-8, R15-21-2)', async () => {
  const { fr, ev } = rig(() => ({ action: 'allow' }));
  const { request, info } = fakeRequest({ url: 'http://h.example/secretpath/SEG?tok=SECRETVAL&x=1', raw: ['Authorization', 'Bearer HDRVAL'] });
  await fr(request, info);
  const text = ev.lines.join('');
  for (const leak of ['secretpath', 'SEG', 'SECRETVAL', 'tok', 'HDRVAL']) assert.ok(!text.includes(leak), leak);
  const e = parse(ev.lines)[0];
  assert.deepEqual(Object.keys(e), ['t', 'request_id', 'method', 'host', 'query_name_count', 'dropped_count', 'dropped_names']);
  assert.equal(e.query_name_count, 2);
});

// P-2: the glue's per-request machine. States: pending (the decider asked), allowed (wrapper returned allow, no
// onResponse yet), closed (denied, walled, responded or ended). Events: abort, allow, deny, response, inner-throw.
test('machine: pending + abort -> one ended line, nothing after the late allow (R15-30-5, R15-29-1 case a)', async () => {
  let release;
  const { fr, obs } = rig(() => new Promise(r => { release = r; }));
  const { request, info, ac } = fakeRequest();
  const p = fr(request, info);
  ac.abort();
  release({ action: 'allow' });
  const d = await p;
  d.onResponse?.(200, {});
  assert.deepEqual(parse(obs.lines), [{ t: 'ended', request_id: ID }]);
});

test('machine: allowed + abort -> ended (case b); then response -> nothing', async () => {
  const { fr, obs } = rig(() => ({ action: 'allow' }));
  const { request, info, ac } = fakeRequest();
  const d = await fr(request, info);
  ac.abort();
  d.onResponse(200, { 'content-type': 'text/plain' });
  assert.deepEqual(parse(obs.lines), [{ t: 'ended', request_id: ID }]);
});

test('machine: allowed + response -> one resp line, then abort (res close) -> nothing', async () => {
  const { fr, obs } = rig(() => ({ action: 'allow' }));
  const { request, info, ac } = fakeRequest();
  const d = await fr(request, info);
  d.onResponse(200, { 'content-type': 'application/x-git-upload-pack-advertisement', 'content-length': '12' });
  ac.abort();
  d.onResponse(201, {});
  assert.deepEqual(parse(obs.lines), [{ t: 'resp', request_id: ID, status: 200,
    content_type: 'application/x-git-upload-pack-advertisement', content_length: '12' }]);
});

test('machine: pending + deny -> closed; abort after -> nothing', async () => {
  const { fr, obs } = rig(() => ({ action: 'deny', status: 403, reason: 'r' }));
  const { request, info, ac } = fakeRequest();
  await fr(request, info);
  ac.abort();
  assert.deepEqual(obs.lines, []);
});

test('machine: pending + inner throws -> closed, rethrown (srt turns it into its deny); abort after -> nothing', async () => {
  const { fr, obs } = rig(() => { throw new Error('decider dead'); });
  const { request, info, ac } = fakeRequest();
  await assert.rejects(fr(request, info), /decider dead/);
  ac.abort();
  assert.deepEqual(obs.lines, []);
});

test('machine: already aborted at entry -> ended once, the decider still asked (its attempt row)', async () => {
  const { fr, obs, seen } = rig(() => ({ action: 'allow' }));
  const { request, info, ac } = fakeRequest();
  ac.abort();
  await fr(request, info);
  assert.equal(seen.length, 1);
  assert.deepEqual(parse(obs.lines), [{ t: 'ended', request_id: ID }]);
});

test('two concurrent requests: each outcome carries its own id (R15-23-3)', async () => {
  const ev = sink(), obs = sink();
  const flood = createFlood({ writeLine: ev, now: () => 0, schedule: () => {} });
  let n = 0;
  const ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'];
  const fr = wrapFilterRequest(async () => ({ action: 'allow' }), { writeEvent: ev, writeObs: obs, flood, randomUUID: () => ids[n++] });
  const a = fakeRequest(), b = fakeRequest();
  const [da, db] = await Promise.all([fr(a.request, a.info), fr(b.request, b.info)]);
  db.onResponse(404, {}); da.onResponse(200, {});
  assert.deepEqual(parse(obs.lines).map(o => [o.request_id, o.status]), [[ids[1], 404], [ids[0], 200]]);
});

// --- glue walls: the scheme check (R15-17-5, R15-19-4, R15-23-2) and the port filter (R15-12-B1, R15-14-3)
test('scheme wall: no sni + https: target -> deny before the decider, ONE glue event, no req event, no obs', async () => {
  const { fr, ev, obs, seen } = rig(() => ({ action: 'allow' }));
  const { request, info } = fakeRequest({ url: 'https://user:pass@s.rb.test:9080/p5-https?q=SECRET' });
  const d = await fr(request, { ...info, sni: undefined });
  assert.equal(d.action, 'deny');
  assert.equal(seen.length, 0);
  const evs = parse(ev.lines);
  assert.deepEqual(evs, [{ t: 'wall', request_id: ID, method: 'GET', host: 's.rb.test', reason: 'scheme' }]);
  for (const leak of ['user', 'pass', 'p5-https', 'SECRET']) assert.ok(!ev.lines.join('').includes(leak), leak);
  assert.deepEqual(obs.lines, []);
});

test('scheme check is for the plain path only: an sni request with https: goes to the decider', async () => {
  const { fr, seen } = rig(() => ({ action: 'allow' }));
  const { request, info } = fakeRequest({ url: 'https://s.rb.test/x' });
  await fr(request, { ...info, sni: 's.rb.test' });
  assert.equal(seen.length, 1);
});

function filterRig(opts = {}) {
  const ev = sink();
  const flood = createFlood({ writeLine: ev, now: () => 0, schedule: () => {} });
  const canon = h => (/^[a-z0-9.-]+$/i.test(h) && !/^[0-9.]+$/.test(h) ? h.toLowerCase() : (h === '127.0.0.1' ? h : undefined));
  const f = createFilter({ tlsPorts: [443], httpPorts: [80], canonicalizeHost: canon, flood, ...opts });
  return { f, ev };
}

test('port filter: CONNECT admitted to 443 only, plain to 80 only, by the fifth argument (R15-12-B1)', () => {
  const { f, ev } = filterRig();
  const reasons = [];
  const setReason = r => reasons.push(r);
  assert.equal(f(443, 'h.example', null, undefined, setReason), true);
  assert.equal(f(8443, 'h.example', null, undefined, setReason), false);
  assert.equal(f(80, 'h.example', null, undefined, setReason), false);
  assert.equal(f(80, 'h.example', null, undefined), true);
  assert.equal(f(8080, 'h.example', null, undefined), false);
  assert.equal(f(443, 'h.example', null, undefined), false);
  const walls = parse(ev.lines);
  assert.deepEqual(walls.map(w => [w.reason, w.request_id, w.method, w.host]),
    [['port', null, null, 'h.example'], ['port', null, null, 'h.example'], ['port', null, null, 'h.example'],
      ['port', null, null, 'h.example']]);
  assert.equal(reasons.length, 2);
});

test('port filter: a host srt cannot canonicalise walls non_dns_host, escaped, "<unparsed>" when empty (C-2)', () => {
  const { f, ev } = filterRig();
  assert.equal(f(443, 'bad\u0001host_', null, undefined, () => {}), false);
  assert.equal(f(443, '', null, undefined, () => {}), false);
  assert.deepEqual(parse(ev.lines).map(w => [w.reason, w.host]), [['non_dns_host', 'bad\\x01host_'], ['non_dns_host', '<unparsed>']]);
});

test('R15-14-3: with the fifth-argument test disabled the filter admits the UNION of the port sets (the other layers wall)', () => {
  const { f } = filterRig({ fifthArg: false });
  assert.equal(f(80, 'h.example', null, undefined, () => {}), true);
  assert.equal(f(443, 'h.example', null, undefined), true);
  assert.equal(f(8443, 'h.example', null, undefined, () => {}), false);
});

// --- the flood bound
function floodRig(t0 = 1_700_000_000_000) {
  const ev = sink();
  let t = t0;
  const timers = [];
  const flood = createFlood({ writeLine: ev, now: () => t, schedule: (ms, fn) => timers.push([ms, fn]) });
  return { ev, flood, set: v => { t = v; }, get t() { return t; }, timers, events: () => parse(ev.lines) };
}
const wall = (flood, host, reason = 'port') => flood.wall({ request_id: null, method: null, host, reason });

test('R15-29-2 / R15-30-6: 200 concurrent walls -> exactly 100 detailed (synchronous reservation)', async () => {
  const r = floodRig();
  await Promise.all(Array.from({ length: 200 }, (_, i) => Promise.resolve().then(() => wall(r.flood, `h${i}.example`))));
  assert.equal(r.events().filter(e => e.t === 'wall').length, 100);
});

test('R15-27-1 / R15-28-2: 10,000 walls over 10,000 hostnames -> 100 detailed, map 64, overflow 9,936, total 10,000', () => {
  const r = floodRig(1_700_000_000_000 - (1_700_000_000_000 % 60000));
  for (let i = 0; i < 10000; i++) { r.set(r.t + 15); wall(r.flood, `h${i}.example`); }   // 150 s: three minutes
  r.flood.final();
  const evs = r.events();
  assert.equal(evs.filter(e => e.t === 'wall').length, 100);
  const sums = evs.filter(e => e.t === 'summary');
  const fin = sums.at(-1);
  assert.equal(fin.final, true);
  assert.equal(fin.total, 10000);
  assert.equal(Object.keys(fin.hosts).length, 64);
  assert.equal(fin.overflow, 9936);
  assert.deepEqual(fin.by_reason, { port: 10000 });
  // R15-28-1: every summary bounded, its running total = the walls so far; at most one per tumbling minute
  const minutes = sums.filter(s => !s.final).map(s => s.minute);
  assert.equal(new Set(minutes).size, minutes.length);
  assert.ok(minutes.every(m => m % 60 === 0));
  let seen = 0;
  for (const s of sums) {
    assert.ok(Object.keys(s.hosts).length <= 64);
    assert.ok(Object.keys(s.by_reason).length <= 3);
    seen += s.minute_count;
    assert.equal(s.total, seen);
  }
  assert.ok(sums.length >= 3 && sums.length <= 4, String(sums.length));
});

test('R15-27-1: 200 walls on one hostname, then one on a second -> the second is summarised, not detailed', () => {
  const r = floodRig();
  for (let i = 0; i < 200; i++) wall(r.flood, 'one.example');
  wall(r.flood, 'two.example');
  const walls = r.events().filter(e => e.t === 'wall');
  assert.equal(walls.length, 100);
  assert.ok(walls.every(w => w.host === 'one.example'));
});

test('R15-27-1: 65 hostnames -> the 65th counts in overflow, no listed hostname leaves the map', () => {
  const r = floodRig();
  for (let i = 0; i < 65; i++) wall(r.flood, `h${i}.example`);
  r.flood.final();
  const fin = r.events().at(-1);
  assert.equal(Object.keys(fin.hosts).length, 64);
  assert.ok(!('h64.example' in fin.hosts) && 'h0.example' in fin.hosts && 'h63.example' in fin.hosts);
  assert.equal(fin.overflow, 1);
});

test('R15-29-2: <unparsed> holds one ordinary slot: it + 64 hostnames -> the 64th hostname is overflow', () => {
  const r = floodRig();
  wall(r.flood, '');
  for (let i = 0; i < 64; i++) wall(r.flood, `h${i}.example`);
  r.flood.final();
  const fin = r.events().at(-1);
  assert.equal(fin.hosts['<unparsed>'], 1);
  assert.ok(!('h63.example' in fin.hosts));
  assert.equal(fin.overflow, 1);
  assert.equal(r.events()[0].host, '<unparsed>');
});

test('R15-29-2 / P-9: 150 walls then a clean end in the same minute -> the final flush total 150', () => {
  const r = floodRig();
  for (let i = 0; i < 150; i++) wall(r.flood, 'a.example', i % 2 ? 'port' : 'scheme');
  r.flood.final();
  const sums = r.events().filter(e => e.t === 'summary');
  assert.equal(sums.length, 1);
  assert.equal(sums[0].final, true);
  assert.equal(sums[0].total, 150);
  assert.deepEqual(sums[0].by_reason, { scheme: 75, port: 75 });
});

test('P-9: 50 walls then a clean end -> a final flush with total 50; zero walls -> a final flush with total 0', () => {
  const r = floodRig();
  for (let i = 0; i < 50; i++) wall(r.flood, 'a.example');
  r.flood.final();
  assert.equal(r.events().filter(e => e.t === 'summary').at(-1).total, 50);
  const z = floodRig(60_000 * 1000 + 7);
  z.flood.final();
  assert.deepEqual(z.events(), [{ t: 'summary', minute: 60000, minute_count: 0, total: 0, by_reason: {}, hosts: {}, overflow: 0, final: true }]);
});

test('P-9: final() is written once; a wall after it is not counted into a second flush', () => {
  const r = floodRig();
  r.flood.final(); r.flood.final();
  assert.equal(r.events().length, 1);
});

test('R15-30-1: tumbling wall-clock minutes; a zero-wall minute emits no summary; R15-30-2 running per-reason totals', () => {
  const m0 = 1_700_000_040_000 - (1_700_000_040_000 % 60000);
  const r = floodRig(m0 + 59_000);
  for (let i = 0; i < 101; i++) wall(r.flood, 'a.example', 'port');    // minute 0: limit spent, one summarised
  r.set(m0 + 60_000 + 1);                                               // minute 1: no wall
  r.flood.tick();
  r.set(m0 + 180_000 + 5);                                              // minute 3: one wall
  wall(r.flood, 'b.example', 'scheme');
  r.set(m0 + 240_000);
  r.flood.tick();
  const sums = r.events().filter(e => e.t === 'summary');
  assert.deepEqual(sums.map(s => [s.minute, s.minute_count, s.total, s.by_reason]),
    [[m0 / 1000, 101, 101, { port: 101 }], [m0 / 1000 + 180, 1, 102, { port: 101, scheme: 1 }]]);
  assert.ok(r.timers.length >= 1 && r.timers.every(([ms]) => ms > 0 && ms <= 60_000));
});

test('a minute whose walls were all detailed emits no summary of its own', () => {
  const m0 = 1_700_000_040_000 - (1_700_000_040_000 % 60000);
  const r = floodRig(m0);
  for (let i = 0; i < 100; i++) wall(r.flood, 'a.example');
  r.set(m0 + 61_000); r.flood.tick();
  assert.equal(r.events().filter(e => e.t === 'summary').length, 0);
});
