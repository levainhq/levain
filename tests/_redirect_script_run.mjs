// Runs levain/templates/web/phone_redirect.js once in a bare VM context with a recording `location`, `history` and
// `document`, for tests/test_phone_redirect.py. argv: <script> <href> <meta content | "">. Prints the calls in order as
// JSON: ["replaceState", url] / ["replace", url] / ["assign", url] (an href set) / ["read-hash-after", n] when the
// fragment is read after the bar was scrubbed.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [script, href, meta] = process.argv.slice(2);
const u = new URL(href);
const calls = [];
let scrubbed = false;
const location = {
  get hash() { if (scrubbed) calls.push(["read-hash-after", calls.length]); return scrubbed ? "" : u.hash; },
  get pathname() { return u.pathname; },
  get href() { return u.href; },
  set href(v) { calls.push(["assign", String(v)]); },
  assign(v) { calls.push(["assign", String(v)]); },
  replace(v) { calls.push(["replace", String(v)]); },
};
const history = { replaceState(_s, _t, url) { scrubbed = true; calls.push(["replaceState", String(url)]); } };
const document = { querySelector: (sel) => (meta && sel === 'meta[name="levain-redirect-to"]'
  ? { getAttribute: (n) => (n === "content" ? meta : null) } : null) };
vm.runInNewContext(readFileSync(script, "utf8"), { location, history, document, Map, RegExp });
process.stdout.write(JSON.stringify(calls));
