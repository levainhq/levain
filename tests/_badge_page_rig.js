// Runs the cockpit page's syncAppBadge (levain/templates/web/dashboard_core.js) over a stub window.navigator and prints the calls.
// usage: node _badge_page_rig.js <path to dashboard_core.js> '<json {view, n, nav: "full"|"none"|"set-only"|"throw"|"reject"}>'
const fs = require('fs');
const vm = require('vm');

const [src, arg] = process.argv.slice(2);
const spec = JSON.parse(arg);
const text = fs.readFileSync(src, 'utf8');
const a = text.indexOf('  function syncAppBadge(');
const b = text.indexOf('  function renderNeeds(');
if (a < 0 || b < a) { console.log(JSON.stringify({ error: 'syncAppBadge not found' })); process.exit(0); }
const calls = [];
const rec = (k) => (...x) => {
  calls.push([k, ...x]);
  if (spec.nav === 'throw') throw new Error('refused');
  return spec.nav === 'reject' ? Promise.reject(new Error('refused')) : Promise.resolve();
};
let navigator;
if (spec.nav === 'full' || spec.nav === 'throw' || spec.nav === 'reject') navigator = { setAppBadge: rec('set'), clearAppBadge: rec('clear') };
else if (spec.nav === 'set-only') navigator = { setAppBadge: rec('set') };
else navigator = {};
const sandbox = { window: { navigator } };
vm.createContext(sandbox);
vm.runInContext(text.slice(a, b) + '\nthis.fn = syncAppBadge;', sandbox);
let threw = false;
try { sandbox.fn(spec.view, spec.n); } catch (_) { threw = true; }
setTimeout(() => console.log(JSON.stringify({ calls, threw })), 20);
