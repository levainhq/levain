// Which of levain's _REFUSED_NETS, loaded into srt's guard as deniedResolvedAddresses, refuse a PUBLIC address?
// usage: node table_check.mjs   (no network: the guard's resolve is stubbed to return the probe address)
// CHD2-S1 PATCH: the srt location is the first argument (the dist/sandbox/ directory).
const SRT = process.argv[2].replace(/\/?$/, '/');
const { createResolvedAddressGuard } = await import(SRT + 'resolved-address-guard.js');
const LEVAIN = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
  '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
  '224.0.0.0/4', '240.0.0.0/4',
  '::/96', '::ffff:0:0/96', '::ffff:0:0:0/96', '64:ff9b:1::/48', '100::/64', '2001::/32', '2001:db8::/32',
  '2002::/16', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8', '3fff::/20', '5f00::/16'];
const probes = ['104.20.23.154', '8.8.8.8', '1.1.1.1', '2606:4700:10::6814:179a', '2001:4860:4860::8888', '10.0.0.1', '192.0.2.10'];
const verdict = (ranges, addr) => new Promise(res => {
  const g = createResolvedAddressGuard({ deniedResolvedAddresses: ranges, localAddresses: () => [],
    resolve: (_h, _o, cb) => cb(null, [{ address: addr, family: addr.includes(':') ? 6 : 4 }]) });
  g.lookupFor(443)('probe.example', { all: true }, (err) => res(err ? `REFUSED (${err.message.split(': ')[1]})` : 'permitted'));
});
console.log('--- whole levain table');
for (const a of probes) console.log(a.padEnd(26), await verdict(LEVAIN, a));
const XLAT = LEVAIN.filter(r => r !== '::ffff:0:0/96');
console.log('--- levain table minus ::ffff:0:0/96 (levain-xlat)');
for (const a of [...probes, '::ffff:127.0.0.1', '::ffff:10.0.0.1', '::ffff:8.8.8.8', '::127.0.0.1', '64:ff9b::a00:1', '64:ff9b::808:808', '198.51.100.7', '203.0.113.7', '100.64.0.1', 'fec0::1', '2001:db8::1'])
  console.log(a.padEnd(26), await verdict(XLAT, a));
console.log('--- one range at a time: ranges that refuse 104.20.23.154 or 2606:4700:10::6814:179a');
for (const r of LEVAIN) {
  const v4 = await verdict([r], '104.20.23.154'); const v6 = await verdict([r], '2606:4700:10::6814:179a');
  if (v4 !== 'permitted' || v6 !== 'permitted') console.log(r.padEnd(18), 'v4:', v4, '| v6:', v6);
}
