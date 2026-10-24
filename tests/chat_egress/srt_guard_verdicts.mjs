// CH-D2 S2 test helper: per-address verdicts through srt's REAL resolved-address guard (the dial path), and srt's
// own request-target reading, for test_guard_srt.py. No network: the guard's resolver is stubbed to answer the probe
// address, and the host's own interface addresses are stubbed empty so a verdict does not depend on the machine.
// usage: node srt_guard_verdicts.mjs <srt dist/sandbox dir> <input.json>
//   input: {"tables": {"<name>": [cidr, ...]}, "addresses": [addr, ...], "targets": [raw request-target, ...]}
//   output (stdout, one JSON object): {"verdicts": {"<name>": {addr: "refused" | "permitted"}},
//                                      "targets": {raw: {"refusal": str | null, "forwarded": str | null}}}
import { readFileSync } from 'node:fs';

const SRT = process.argv[2].replace(/\/?$/, '/');
const input = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const { createResolvedAddressGuard } = await import(SRT + 'resolved-address-guard.js');
const { readRequestTarget } = await import(SRT + 'request-filter.js');

const verdict = (lookup, addr) => new Promise(res => {
  // the same call srt's dialer makes: lookupFor(port)(hostname, {all: true}, cb)
  lookup('probe.example', { all: true }, err => res(err ? 'refused' : 'permitted'));
});

const out = { verdicts: {}, targets: {} };
for (const [name, ranges] of Object.entries(input.tables ?? {})) {
  out.verdicts[name] = {};
  for (const addr of input.addresses ?? []) {
    const guard = createResolvedAddressGuard({
      deniedResolvedAddresses: ranges,
      localAddresses: () => [],
      resolve: (_h, _o, cb) => cb(null, [{ address: addr, family: addr.includes(':') ? 6 : 4 }]),
    });
    out.verdicts[name][addr] = await verdict(guard.lookupFor(443), addr);
  }
}
for (const raw of input.targets ?? []) {
  const r = readRequestTarget(raw, 'GET', 'refuse');
  out.targets[raw] = { refusal: r.refusal ?? null, forwarded: r.forwarded ?? null };
}
process.stdout.write(JSON.stringify(out));
