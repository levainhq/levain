// Levain's thin Node glue over @anthropic-ai/sandbox-runtime 0.0.81 (srt), CH-D2 S4. Design §3.8.5(o) item 2 (rev
// 15.11) with R15-30 and the CH-D2 plan rulings (P-1 .. P-9); the reference glue is round 4's lib_proxy4.mjs (P-4).
//
// What this file owns, and nothing else (srt owns the connection, the dial, TLS and the pumps):
//   - the port filter (srt's `filter`): CONNECT to the TLS ports only, absolute-form plain HTTP to the HTTP ports only,
//     told apart by the fifth argument srt passes for a CONNECT only (undocumented; srt is pinned, R15-14-3); a host
//     srt cannot canonicalise walls `non_dns_host` (C-2, R15-12-B1).
//   - the filterRequest WRAPPER, six steps (R15-25-1): (1) a crypto.randomUUID() request id; (2) every inbound name
//     whose srt fold starts with `x-levain-` dropped from info.rawHeaders and request.headers, the names recorded;
//     (3) `x-levain-audit: <id>` appended; (4) the decider asked with the rewritten raw list; (5) on an allow, AFTER the
//     decider client validated the verdict, removeHeaders = the decider's list + x-levain-audit + every dropped name,
//     deduped by fold; (6) onResponse and the abort report carry the id. Before step (2), on the plain path (no sni) a
//     target whose scheme is not http: is a glue wall (R15-17-5, R15-19-4): no decider frame, no DNS, no dial
//     (R15-23-2).
//   - the per-request machine (P-2): pending -> allowed -> closed, or pending|allowed -> ended. The observation line
//     `resp` is written in onResponse (before srt relays the head); `ended` when the abort fires while pending or
//     allowed (R15-29-1, R15-30-4/-5); never after a deny, a wall or an outcome; at most one of the two per request.
//   - the event file (wire.py's GlueReq / GlueWall / GlueSummary, byte-compatible with wire.parse_glue_event) and the
//     flood bound (R15-27-1, R15-28-1/-2, R15-29-2, R15-30-1/-2/-6, P-9): 100 detailed walls per launch in total,
//     reserved synchronously; past them one summary per tumbling wall-clock minute that summarised a wall; running
//     totals; a 64-host map with no eviction and an overflow count; a final flush at every clean end, total 0 when no
//     wall fired.
// No event, log line or exception path receives request.url, a path, a query value or a header value (R15-17-8,
// R15-21-2).
//
// usage (S8's launcher): node glue.mjs --srt-dist <dist/sandbox dir> --listen-fd N --decider-fd N --obs-fd N
//          --ca-fd N --lifeline-fd N --events <jsonl> --denied-table <denied_table.json>
// TEST SEAMS (never product; S8 passes none of them): --tls-ports, --http-ports (port stand-ins: macOS refuses a
// non-root bind of 80/443), --carve (srt's allowedDomains literal-address carve-outs, for a 127.0.0.1 origin),
// --resolve-map name=addr[,addr];..., --upstream-ca <pem>, --fifth-arg off and --terminate-ports (R15-14-3's
// per-layer reds).
import { appendFileSync, readFileSync, createReadStream } from 'node:fs';
import { randomUUID as cryptoRandomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';

// --- wire.py's bounds and reasons -------------------------------------------------------------------------------------
export const DETAILED_WALL_LIMIT = 100;
export const HOST_MAP_LIMIT = 64;
export const HOST_LIMIT = 253;
export const DROPPED_NAME_LIMIT = 64;
export const DROPPED_NAMES_SHOWN = 8;
export const AUDIT_HEADER = 'x-levain-audit';
export const LEVAIN_HEADER_PREFIX = 'x-levain-';
export const UNPARSED = '<unparsed>';

/** srt 0.0.81's foldHeaderName (request-filter.js), = wire.fold_name. */
export const foldHeaderName = name => name.toLowerCase().replace(/[_.]/g, '-');

/** wire.escape_text: cut to `limit` UTF-16 units, THEN escape each unit. */
export function escapeText(text, limit) {
  const s = String(text).slice(0, limit);
  let out = '';
  for (let i = 0; i < s.length; i++) {
    const u = s.charCodeAt(i);
    if (u >= 0x20 && u <= 0x7e && u !== 0x5c) out += s[i];
    else if (u <= 0xff) out += '\\x' + u.toString(16).padStart(2, '0');
    else out += '\\u' + u.toString(16).padStart(4, '0');
  }
  return out;
}

/** wire.ESCAPE_VECTORS, copied; test_glue_node.py asserts the two lists are equal. */
export const ESCAPE_VECTORS_JS = [
  ['example.com', 253, 'example.com'],
  ['a\x00b\x1f\x7f\x9f', 253, 'a\\x00b\\x1f\\x7f\\x9f'],
  ['café', 253, 'caf\\xe9'],
  ['☃', 253, '\\u2603'],
  ['\u{1f600}', 253, '\\ud83d\\ude00'],
  ['a\\x41', 253, 'a\\x5cx41'],
  ['x'.repeat(300), 253, 'x'.repeat(253)],
  ['\x00'.repeat(70), 64, '\\x00'.repeat(64)],
];

/** One line as wire._line writes it: json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n". */
export function jsonLine(obj) {
  return JSON.stringify(obj).replace(/[\u007f-￿]/g,
    c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')) + '\n';
}

const hostField = h => (typeof h === 'string' && h !== '' ? escapeText(h, HOST_LIMIT) : UNPARSED);

// --- the flood bound ----------------------------------------------------------------------------------------------------
/**
 * Every glue wall goes through `wall()`. The check-remaining / decrement / write of a detailed event is one
 * synchronous block (R15-29-2, R15-30-6), so exactly DETAILED_WALL_LIMIT are admitted however many walls are in flight.
 * `writeLine` is synchronous (appendFileSync in the product). `schedule(ms, fn)` arms the minute-boundary timer.
 */
export function createFlood({ writeLine, now = () => Date.now(), schedule = defaultSchedule } = {}) {
  let detailedLeft = DETAILED_WALL_LIMIT;
  let total = 0;
  let overflow = 0;
  const byReason = new Map();
  const hosts = new Map();
  let minute = null;          // the open minute's start (epoch s), while it holds a wall not yet in a summary
  let minuteCount = 0;        // walls in the open minute
  let minuteSummarised = false;
  let finished = false;
  let armed = false;

  const minuteOf = ms => Math.floor(ms / 60000) * 60;
  const summary = (m, count, final) => jsonLine({
    t: 'summary', minute: m, minute_count: count, total, by_reason: Object.fromEntries(byReason),
    hosts: Object.fromEntries(hosts), overflow, final,
  });
  function rollover(ms) {
    if (minute === null || minuteOf(ms) === minute) return;
    if (minuteSummarised) writeLine(summary(minute, minuteCount, false));
    minute = null; minuteCount = 0; minuteSummarised = false;
  }
  function arm(ms) {
    if (armed) return;
    armed = true;
    schedule(Math.max(1, (minuteOf(ms) + 60) * 1000 - ms), () => { armed = false; tick(); });
  }
  function tick() { if (!finished) rollover(now()); }
  function wall({ request_id = null, method = null, host, reason }) {
    if (finished) return;
    const ms = now();
    rollover(ms);
    const h = hostField(host);
    total += 1;
    byReason.set(reason, (byReason.get(reason) ?? 0) + 1);
    if (hosts.has(h)) hosts.set(h, hosts.get(h) + 1);
    else if (hosts.size < HOST_MAP_LIMIT) hosts.set(h, 1);
    else overflow += 1;
    if (minute === null) minute = minuteOf(ms);
    minuteCount += 1;
    if (detailedLeft > 0) {
      detailedLeft -= 1;
      writeLine(jsonLine({ t: 'wall', request_id, method, host: h, reason }));
    } else {
      minuteSummarised = true;
      arm(ms);
    }
  }
  /** The clean-end flush (P-9): always written, once; it covers the open minute. */
  function final() {
    if (finished) return;
    const ms = now();
    rollover(ms);
    finished = true;
    writeLine(summary(minuteOf(ms), minuteCount, true));
  }
  return { wall, tick, final, get total() { return total; } };
}

function defaultSchedule(ms, fn) { const t = setTimeout(fn, ms); t.unref?.(); return t; }

// --- the port filter (srt's `filter`) ---------------------------------------------------------------------------------
/**
 * srt calls filter(port, host, socket, encodedCommand) per plain request and filter(port, host, socket, cmd, setReason)
 * per CONNECT. A wall here has no request yet: request_id and method are null (wire GlueWall).
 */
export function createFilter({ tlsPorts, httpPorts, canonicalizeHost, flood, fifthArg = true }) {
  return (port, host, _socket, _cmd, setReason) => {
    if (typeof host !== 'string' || host === '' || canonicalizeHost(host) === undefined) {
      setReason?.('the destination host is not a DNS name');
      flood.wall({ host, reason: 'non_dns_host' });
      return false;
    }
    const isConnect = typeof setReason === 'function';
    const ok = !fifthArg ? (tlsPorts.includes(port) || httpPorts.includes(port))
      : isConnect ? tlsPorts.includes(port) : httpPorts.includes(port);
    if (!ok) {
      setReason?.(`port ${port} is not admitted for ${isConnect ? 'CONNECT' : 'plain HTTP'}`);
      flood.wall({ host, reason: 'port' });
    }
    return ok;
  };
}

// --- the filterRequest wrapper ----------------------------------------------------------------------------------------
const queryNameCount = target => {
  const q = typeof target === 'string' ? target.indexOf('?') : -1;
  return q < 0 ? 0 : target.slice(q + 1).split('&').filter(p => p !== '').length;
};
const headerValue = v => (typeof v === 'string' ? v : Array.isArray(v) ? v.join(', ') : null);

export function wrapFilterRequest(inner, { writeEvent, writeObs, flood, randomUUID = cryptoRandomUUID }) {
  return async (request, info) => {
    const id = randomUUID();                                                   // (1)
    let u;
    try { u = new URL(request.url); } catch { u = null; }
    const method = request.method;
    if (!info?.sni && (u === null || u.protocol !== 'http:')) {                // the plain-path scheme wall
      flood.wall({ request_id: id, method, host: u?.hostname, reason: 'scheme' });
      return { action: 'deny', status: 403, reason: 'wall: scheme (only http: is forwarded on the plain path)' };
    }
    if (u === null) return { action: 'deny', status: 400, reason: 'unparsable target' };   // srt built it; defensive
    const raw = info?.rawHeaders ?? [];                                        // (2)
    const kept = [];
    const dropped = [];
    for (let i = 0; i + 1 < raw.length; i += 2) {
      if (foldHeaderName(raw[i]).startsWith(LEVAIN_HEADER_PREFIX)) {
        const n = raw[i].toLowerCase();
        if (!dropped.includes(n)) dropped.push(n);
      } else kept.push(raw[i], raw[i + 1]);
    }
    for (const n of [...request.headers.keys()]) {
      if (foldHeaderName(n).startsWith(LEVAIN_HEADER_PREFIX)) request.headers.delete(n);
    }
    kept.push(AUDIT_HEADER, id);                                               // (3)
    request.headers.set(AUDIT_HEADER, id);
    writeEvent(jsonLine({
      t: 'req', request_id: id, method, host: hostField(u.hostname),
      query_name_count: queryNameCount(info?.requestTarget ?? (u.pathname + u.search)),
      dropped_count: dropped.length,
      dropped_names: dropped.slice(0, DROPPED_NAMES_SHOWN).map(n => escapeText(n, DROPPED_NAME_LIMIT)),
    }));
    let state = 'pending';                                                     // (6) the per-request machine
    const ended = () => {
      if (state !== 'pending' && state !== 'allowed') return;
      state = 'ended';
      writeObs(jsonLine({ t: 'ended', request_id: id }));
    };
    if (request.signal?.aborted) ended();
    else request.signal?.addEventListener('abort', ended, { once: true });
    let d;
    try {
      d = await inner(request, { ...info, rawHeaders: kept });                 // (4)
    } catch (err) {
      if (state === 'pending') state = 'closed';
      throw err;
    }
    if (d?.action !== 'allow') {
      if (state === 'pending') state = 'closed';
      return d;
    }
    if (state === 'pending') state = 'allowed';
    const removals = [];                                                       // (5)
    const seen = new Set();
    for (const n of [...(d.removeHeaders ?? []), AUDIT_HEADER, ...dropped]) {
      const f = foldHeaderName(n);
      if (!seen.has(f)) { seen.add(f); removals.push(f); }
    }
    return {
      ...d,
      removeHeaders: removals,
      onResponse: (status, headers) => {
        if (state !== 'allowed') return;
        state = 'closed';
        writeObs(jsonLine({
          t: 'resp', request_id: id, status,
          content_type: headerValue(headers?.['content-type']), content_length: headerValue(headers?.['content-length']),
        }));
      },
    };
  };
}

// --- the launch (S8 spawns this with a closed env and explicit fds) ---------------------------------------------------
async function main(argv) {
  const opt = (name, dflt) => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : dflt);
  const need = name => { const v = opt(name); if (v === undefined) throw new Error(`glue: ${name} is required`); return v; };
  const ports = (name, dflt) => opt(name, dflt).split(',').filter(Boolean).map(Number);
  const SRT = need('--srt-dist').replace(/\/?$/, '/');
  const { createDecider } = await import(SRT + 'decider-client.js');
  const { keepFlushedHeaderBatches, writeHeadsAsLatin1 } = await import(SRT + 'emitted-connection.js');
  const { createHttpProxyServer, markHandedInConnection } = await import(SRT + 'http-proxy.js');
  const { createMitmCA } = await import(SRT + 'mitm-ca.js');
  const { canonicalizeHost } = await import(SRT + 'parent-proxy.js');
  const { createResolvedAddressGuard } = await import(SRT + 'resolved-address-guard.js');
  const { createServer: createNetServer, Socket: NetSocket } = await import('node:net');
  const { lookup: dnsLookup } = await import('node:dns');

  const TLS_PORTS = ports('--tls-ports', '443');
  const HTTP_PORTS = ports('--http-ports', '80');
  const TERMINATE = ports('--terminate-ports', TLS_PORTS.join(','));
  const EVENTS = need('--events');
  const table = JSON.parse(readFileSync(need('--denied-table'), 'utf8'));
  if (!Array.isArray(table.ranges) || table.ranges.length === 0) throw new Error('glue: the denied table has no ranges');
  const resolveMap = new Map((opt('--resolve-map', '') || '').split(';').filter(Boolean)
    .map(e => { const [n, a] = e.split('='); return [n, a.split(',').filter(Boolean)]; }));
  const resolve = (hostname, options, cb) => {
    if (resolveMap.size) console.error(`glue: test-seam lookup ${hostname}`);    // the seam's trace, for the reds
    if (!resolveMap.has(hostname)) return dnsLookup(hostname, options, cb);
    const out = resolveMap.get(hostname).map(address => ({ address, family: address.includes(':') ? 6 : 4 }));
    if (!out.length) return cb(Object.assign(new Error(`getaddrinfo ENOTFOUND ${hostname}`), { code: 'ENOTFOUND' }), []);
    return cb(null, out);
  };
  const guard = createResolvedAddressGuard({
    deniedResolvedAddresses: table.ranges,
    allowedDomains: (opt('--carve', '') || '').split(',').filter(Boolean),
    resolve,
  });

  const writeEvent = line => appendFileSync(EVENTS, line);
  const flood = createFlood({ writeLine: writeEvent });
  const sock = new NetSocket({ fd: Number(need('--decider-fd')), readable: true, writable: true });
  const obs = new NetSocket({ fd: Number(need('--obs-fd')), readable: false, writable: true });
  obs.on('error', () => {});
  const decider = createDecider({ input: sock, output: sock, timeoutMs: 130_000,
    onDead: why => { console.error(`glue: decider dead: ${why}`); process.exit(3); } });
  const pem = readFileSync(Number(need('--ca-fd')), 'utf8');
  const mitmCA = createMitmCA({
    caCertPem: /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/.exec(pem)[0] + '\n',
    caKeyPem: /-----BEGIN (?:RSA )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA )?PRIVATE KEY-----/.exec(pem)[0] + '\n',
  });
  const up = opt('--upstream-ca');
  await decider.hello;

  const filter = createFilter({ tlsPorts: TLS_PORTS, httpPorts: HTTP_PORTS, canonicalizeHost, flood,
    fifthArg: opt('--fifth-arg', 'on') !== 'off' });
  const filterRequest = wrapFilterRequest((r, i) => decider.filterRequest(r, i),
    { writeEvent, writeObs: line => obs.write(line), flood });
  const server = createHttpProxyServer({
    filter, mitmCA, shouldTerminateTLS: (_h, port) => TERMINATE.includes(port), refuseOpaqueTunnels: true,
    requireHostMatch: true, denyHeader: 'X-Deny-Reason', requestTargetAsSpelled: false,
    ambiguousRequestTarget: 'refuse', requireAsciiHost: true, foldHopByHop: true, plaintextHeaderSet: false,
    filterRequest, lookupFor: port => guard.lookupFor(port),
    ...(up ? { tlsTerminateUpstreamCA: readFileSync(up, 'utf8') } : {}),
  });
  const listener = createNetServer(s => {
    markHandedInConnection(s); writeHeadsAsLatin1(s); server.emit('connection', s); keepFlushedHeaderBatches(s);
  }).listen({ fd: Number(need('--listen-fd')) }, () => console.error(`glue: serving node=${process.version}`));

  // A clean end is the lifeline's EOF (S8 closes its end): stop accepting, write the final flush (P-9), drain the
  // observation channel, exit 0. A crash writes no flush, which is how K1 tells the two apart (R15-30-3).
  let ending = false;
  const cleanEnd = () => {
    if (ending) return;
    ending = true;
    listener.close();
    flood.final();
    obs.end(() => process.exit(0));
    setTimeout(() => process.exit(0), 2000).unref();
  };
  const life = createReadStream(null, { fd: Number(need('--lifeline-fd')) });
  life.on('end', cleanEnd); life.on('error', cleanEnd); life.resume();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv).catch(err => { console.error(`glue: ${err?.message ?? err}`); process.exit(2); });
}
