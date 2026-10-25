// CH-D1 spike ROUND 4 glue over sandbox-runtime 0.0.81. Copy of spike3/tools/lib_proxy3.mjs (strict mode only; the
// round-2 "asis" mode is dropped) with the round-4 changes marked R4:
//   R4-a (R15-17-1) an OBSERVATION channel (--obs-fd, a second socket to the decider): every allowed request's
//        onResponse(status, headers) is reported as one JSON line {t:'resp', scheme, host, port, path, service, status,
//        ct}. The decider, not the glue, decides what an advertisement is. Written at onResponse, which srt calls
//        BEFORE it relays the response head to the client (http-proxy.js 774/806/846, tls-terminate-proxy.js 1002).
//   R4-b (R15-17-5) on the plain path (no sni), a request whose URL scheme is not http: is denied in the
//        filterRequest wrapper, before the decider is asked, whatever the port.
//   R4-c (R15-17-8) --log safe: no event receives request.url or a query value. Events carry an opaque id, method,
//        host, port, a path SHAPE and query NAMES. --log raw = the round-3 events (for the before/after measurement).
//   R4-d --upstream-ca system-plus:<pem> = tls.rootCertificates PLUS the local origin CA (probe 2 needs github.com
//        and the local origin in one run).
// usage: node lib_proxy4.mjs --listen-fd N --decider-fd N --ca-fd N --lifeline-fd N --obs-fd N
//          --denied levain-xlat --dns-port P --carve "127.0.0.1:9443,..." --upstream-ca ... --events <jsonl>
//          --tls-ports 443,9443 --http-ports 80,9080 --log safe|raw
// TEST SEAMS (measurement only, never product): --dns-port, --carve, --upstream-ca, as in round 3.
import { appendFileSync, readFileSync, createReadStream } from 'node:fs';
import { createServer as createNetServer, Socket as NetSocket } from 'node:net';
import { lookup as dnsLookup, Resolver } from 'node:dns';
import { rootCertificates } from 'node:tls';
import { randomBytes } from 'node:crypto';
// CHD2-S1 PATCH (the only change to this file): the srt location comes from `--srt-dist <dir>` (the dist/sandbox/ directory
// of an installed @anthropic-ai/sandbox-runtime 0.0.81), not a hard-coded spike path. node runs with a CLOSED env (B6),
// so an env var would not reach it.
const SRT = process.argv[process.argv.indexOf('--srt-dist') + 1].replace(/\/?$/, '/');
const { createDecider } = await import(SRT + 'decider-client.js');
const { keepFlushedHeaderBatches, writeHeadsAsLatin1 } = await import(SRT + 'emitted-connection.js');
const { createHttpProxyServer, markHandedInConnection } = await import(SRT + 'http-proxy.js');
const { createMitmCA } = await import(SRT + 'mitm-ca.js');
const { canonicalizeHost } = await import(SRT + 'parent-proxy.js');
const { createResolvedAddressGuard } = await import(SRT + 'resolved-address-guard.js');

const argv = process.argv;
const opt = (name, dflt) => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : dflt);
const arg = name => Number(opt(name));
const TLS_PORTS = (opt('--tls-ports', '443')).split(',').map(Number);
const HTTP_PORTS = (opt('--http-ports', '80')).split(',').map(Number);
const EVENTS = opt('--events', '/dev/null');
const LOG = opt('--log', 'safe');
const ev = o => appendFileSync(EVENTS, JSON.stringify({ at: Date.now() / 1000, ...o }) + '\n');

// R4-c: the shapes an event may carry. A path segment is kept when it is short and plain; anything else (long,
// percent-encoded, or high-entropy-looking) becomes <seg:LEN>. The query is reduced to its NAMES.
const segShape = s => (/^[A-Za-z0-9._~-]{1,24}$/.test(s) && !/[0-9a-f]{16,}/i.test(s) ? s : `<seg:${s.length}>`);
const pathShape = p => p.split('/').map(s => (s === '' ? '' : segShape(s))).join('/');
const queryNames = q => [...new URLSearchParams(q).keys()].map(segShape);
const shapeOf = (request, info) => {
  let u;
  try { u = new URL(request.url); } catch { return { url: '<unparsable>' }; }
  if (LOG === 'raw') return { url: request.url, sni: info?.sni ?? null };
  return { scheme: u.protocol.replace(':', ''), host: u.hostname, port: u.port || null, path: pathShape(u.pathname),
    queryNames: queryNames(u.search), sni: info?.sni ? 'present' : null };
};

const DENIED_LEVAIN = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
  '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
  '224.0.0.0/4', '240.0.0.0/4',
  '::/96', '::ffff:0:0/96', '::ffff:0:0:0/96', '64:ff9b:1::/48', '100::/64', '2001::/32', '2001:db8::/32',
  '2002::/16', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8', '3fff::/20', '5f00::/16'];
// levain-xlat (round 3): the levain table minus '::ffff:0:0/96' (verbatim, it refuses every IPv4 answer in node's BlockList).
const DENIED = DENIED_LEVAIN.filter(r => r !== '::ffff:0:0/96');

const dnsPort = opt('--dns-port');
const local = new Resolver({ timeout: 2000, tries: 1 });
if (dnsPort) local.setServers([`127.0.0.1:${dnsPort}`]);
const testResolve = (hostname, options, cb) => {
  if (!dnsPort || !hostname.endsWith('.rb.test')) return dnsLookup(hostname, options, cb);
  const out = [];
  local.resolve4(hostname, (e4, a4) => {
    for (const a of e4 ? [] : a4) out.push({ address: a, family: 4 });
    local.resolve6(hostname, (e6, a6) => {
      for (const a of e6 ? [] : a6) out.push({ address: a, family: 6 });
      ev({ t: 'srt-lookup', hostname, answer: out.map(x => x.address) });
      if (!out.length) return cb(Object.assign(new Error(`getaddrinfo ENOTFOUND ${hostname}`), { code: 'ENOTFOUND' }), []);
      cb(null, out);
    });
  });
};
const carve = (opt('--carve', '') || '').split(',').filter(Boolean);
const guard = createResolvedAddressGuard({ deniedResolvedAddresses: DENIED, allowedDomains: carve, resolve: testResolve });

const sock = new NetSocket({ fd: arg('--decider-fd'), readable: true, writable: true });
const obs = new NetSocket({ fd: arg('--obs-fd'), readable: false, writable: true });          // R4-a
const decider = createDecider({ input: sock, output: sock, timeoutMs: 130_000,
  onDead: why => { console.error(`lib proxy: decider dead: ${why}`); process.exit(3); } });
const pem = readFileSync(arg('--ca-fd'), 'utf8');
const mitmCA = createMitmCA({
  caCertPem: /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/.exec(pem)[0] + '\n',
  caKeyPem: /-----BEGIN (?:RSA )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA )?PRIVATE KEY-----/.exec(pem)[0] + '\n',
});
const up = opt('--upstream-ca', 'none');
const upstreamCA = up === 'none' ? undefined : up === 'system-explicit' ? [...rootCertificates]
  : up.startsWith('system-plus:') ? [...rootCertificates, readFileSync(up.slice('system-plus:'.length), 'utf8')]
  : readFileSync(up, 'utf8');
await decider.hello;

const filter = (port, host, _s, _c, setReason) => {
  if (canonicalizeHost(host) === undefined) return false;
  const isConnect = typeof setReason === 'function';
  const ok = isConnect ? TLS_PORTS.includes(port) : HTTP_PORTS.includes(port);
  if (!ok) {
    setReason?.(`port ${port} is not admitted for ${isConnect ? 'CONNECT' : 'plain HTTP'}`);
    ev({ t: 'filter-deny', host, port, connect: isConnect });
  }
  return ok;
};
const SCHEME_CHECK = opt('--scheme-check', 'on') === 'on';   // R4-b; 'off' reproduces the round-3 glue for probe 5
const filterRequest = async (request, info) => {
  const n = randomBytes(6).toString('hex');                    // R4-c: an opaque id, not a sequence that maps to a URL
  ev({ t: 'req', n, method: request.method, ...shapeOf(request, info) });
  request.signal.addEventListener('abort', () => ev({ t: 'client-closed', n }));
  let u;
  try { u = new URL(request.url); } catch { return { action: 'deny', status: 400, reason: 'unparsable target' }; }
  if (SCHEME_CHECK && !info?.sni && u.protocol !== 'http:') {   // R4-b (R15-17-5)
    ev({ t: 'scheme-deny', n, scheme: u.protocol.replace(':', '') });
    return { action: 'deny', status: 403, reason: `wall: ${u.protocol.replace(':', '')} target on the plain path` };
  }
  const d = await decider.filterRequest(request, info);
  ev({ t: 'decision', n, action: d.action, status: d.status ?? null, reason: d.reason ?? null, aborted: request.signal.aborted });
  if (d.action === 'allow') {
    const scheme = info?.sni ? 'https' : 'http';
    const port = Number(u.port) || (scheme === 'https' ? 443 : 80);
    const service = u.searchParams.get('service');            // a git service name, not a secret; sent to the decider only
    d.onResponse = (status, headers) => {
      const ct = headers['content-type'] ?? null;
      ev({ t: 'onResponse', n, status, contentType: ct, contentLength: headers['content-length'] ?? null,
        headerNames: Object.keys(headers) });
      obs.write(JSON.stringify({ t: 'resp', scheme, host: u.hostname, port, path: u.pathname, service, status, ct }) + '\n');
    };
  }
  return d;
};
const server = createHttpProxyServer({
  filter,
  mitmCA, shouldTerminateTLS: (_h, port) => TLS_PORTS.includes(port), refuseOpaqueTunnels: true, requireHostMatch: true,
  denyHeader: 'X-Deny-Reason', requestTargetAsSpelled: false, ambiguousRequestTarget: 'refuse', requireAsciiHost: true, foldHopByHop: true,
  plaintextHeaderSet: false, filterRequest, lookupFor: port => guard.lookupFor(port),
  ...(upstreamCA ? { tlsTerminateUpstreamCA: upstreamCA } : {}),
});
createNetServer(s => {
  markHandedInConnection(s); writeHeadsAsLatin1(s); server.emit('connection', s); keepFlushedHeaderBatches(s);
}).listen({ fd: arg('--listen-fd') }, () => console.error(`lib proxy4: serving log=${LOG} scheme-check=${SCHEME_CHECK} upstream-ca=${up.split(':')[0]} node=${process.version} env-keys=${Object.keys(process.env).length}`));
const life = createReadStream(null, { fd: arg('--lifeline-fd') });
life.on('end', () => process.exit(0)); life.on('error', () => process.exit(0)); life.resume();
