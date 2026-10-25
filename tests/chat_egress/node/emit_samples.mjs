// CH-D2 S4: writes glue lines with the glue's OWN functions (wrapper, filter, flood), for test_glue_node.py to read
// with Python's frozen wire readers. usage: node emit_samples.mjs <out-dir>; prints {"events", "obs", "vectors"}.
import { appendFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { createFlood, createFilter, wrapFilterRequest, ESCAPE_VECTORS_JS } from '../../../levain/chat_egress/node/glue.mjs';

const out = process.argv[2];
const EV = join(out, 'events.jsonl'), OB = join(out, 'obs.jsonl');
writeFileSync(EV, ''); writeFileSync(OB, '');
let ne = 0, no = 0;
const writeEvent = l => { appendFileSync(EV, l); ne++; };
const writeObs = l => { appendFileSync(OB, l); no++; };
let t = 1_700_000_040_000 - (1_700_000_040_000 % 60000);
const flood = createFlood({ writeLine: writeEvent, now: () => t, schedule: () => {} });
const fr = wrapFilterRequest(async request => (new URL(request.url).pathname === '/deny'
  ? { action: 'deny', status: 403, reason: 'x' } : { action: 'allow', removeHeaders: ['cookie'] }), { writeEvent, writeObs, flood });
const filter = createFilter({ tlsPorts: [443], httpPorts: [80], canonicalizeHost: h => (/^[a-z0-9.-]+$/.test(h) ? h : undefined), flood });

const req = (url, raw = [], method = 'GET') => {
  const ac = new AbortController();
  const headers = new Headers();
  return { r: { url, method, headers, signal: ac.signal }, info: { rawHeaders: raw, requestTarget: new URL(url).pathname + new URL(url).search }, ac };
};
// req events: plain, dropped names (incl. a control byte and non-ASCII), non-ASCII host
const a = req('http://café.example/p?x=1&y', ['X-Levain-é\u0001', 'v', 'x_levain_a', 'v', 'Accept', '*/*']);
const da = await fr(a.r, a.info);
da.onResponse(200, { 'content-type': 'application/x-git-upload-pack-advertisement', 'content-length': '123' });
const b = req('http://h.example/q', [], 'POST');
const db = await fr(b.r, b.info);
db.onResponse(204, {});
const c = req('http://h.example/r');
await fr(c.r, c.info);
c.ac.abort();
const d = req('http://h.example/deny');
await fr(d.r, d.info);
// walls of every reason, then past the detailed limit, across two minutes, then the final flush
await fr(req('https://s☃.example:80/x').r, {});
filter(8443, 'h.example', null, undefined, () => {});
filter(443, 'bad host', null, undefined, () => {});
filter(443, '', null, undefined, () => {});
for (let i = 0; i < 150; i++) { t += 300; filter(8080, `w${i}.example`, null, undefined); }
t += 60_000; flood.tick();
flood.final();
console.log(JSON.stringify({ events: ne, obs: no, vectors: ESCAPE_VECTORS_JS }));
