// A real browser, the cockpit's real worker, then the server GONE (the parent decides how: refused, or hanging): install the app shell online, wait for the shell cache, tell
// the parent (one JSON line: "online"), wait for a line on stdin (the parent has stopped the server), then COLD-OPEN the URL in a
// new tab and report what that tab shows (one JSON line: "offline"). argv: <chrome> <url>. Full Chrome only (the headless shell
// has no service worker); it runs headless with the repo's mock-keychain flags, in a profile of its own removed on exit.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const [chromePath, url] = process.argv.slice(2);
const flags = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "chrome_flags.json"), "utf8")).flags;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const port = await new Promise((r) => { const s = createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
const prof = mkdtempSync(join(tmpdir(), "levain-sw-offline-"));
const chrome = spawn(chromePath, [...flags, `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`, "--no-first-run", "about:blank"], { stdio: "ignore" });
const done = (code) => { try { chrome.kill("SIGKILL"); } catch {} setTimeout(() => { try { rmSync(prof, { recursive: true, force: true }); } catch {} process.exit(code); }, 300); };
process.on("uncaughtException", (e) => { console.error(e && e.stack || String(e)); done(1); });
process.on("unhandledRejection", (e) => { console.error(e && e.stack || String(e)); done(1); });
for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, () => done(130));
setInterval(() => { if (process.ppid === 1) done(130); }, 1000).unref();

async function targets() {
  for (let i = 0; i < 80; i++) { try { return await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { await sleep(250); } }
  throw new Error("chrome never answered");
}
async function attach(wsUrl) {
  const ws = new WebSocket(wsUrl); await new Promise((r) => ws.addEventListener("open", r));
  let id = 0; const pend = new Map();
  ws.addEventListener("message", (m) => {
    const d = JSON.parse(m.data);
    // a prompt (the write-token ask) is declined, as an operator with no token would: it must not block the page
    if (d.method === "Page.javascriptDialogOpening") ws.send(JSON.stringify({ id: ++id, method: "Page.handleJavaScriptDialog", params: { accept: false } }));
    if (d.id && pend.has(d.id)) { pend.get(d.id)(d); pend.delete(d.id); }
  });
  return (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
}
async function evalIn(send, expr) {
  const r = await Promise.race([send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }),
    sleep(8000).then(() => ({ result: { result: { value: { error: "no answer in 8s" } } } }))]);
  return r.result?.result?.value ?? { error: JSON.stringify(r.result?.exceptionDetails ?? r.error) };
}
const wsOf = async (match) => (await targets()).find((t) => t.type === "page" && match(t)).webSocketDebuggerUrl;
// what the tab shows once its load settled
const SNAP = `(async () => ({ href: location.href, title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 300),
  bodyChildren: document.body ? document.body.children.length : -1, htmlLength: document.documentElement.outerHTML.length,
  controlled: !!navigator.serviceWorker.controller, readyState: document.readyState, html: document.documentElement.outerHTML.slice(0, 1500), nav: (performance.getEntriesByType('navigation')[0] || {}).responseStart, bg: document.body ? getComputedStyle(document.body).backgroundColor : null }))()`;

const p1 = await attach(await wsOf((t) => t.url === "about:blank"));
await p1("Page.enable");
await p1("Page.navigate", { url });
await sleep(1500);
let state = {};
for (let i = 0; i < 60; i++) {
  state = await evalIn(p1, `(async () => {
    const reg = await navigator.serviceWorker.getRegistration();
    const names = await caches.keys();
    const shell = names.find((n) => n.startsWith('levain-shell-v'));
    const keys = shell ? (await (await caches.open(shell)).keys()).map((r) => new URL(r.url).pathname + new URL(r.url).search) : [];
    return { active: !!(reg && reg.active), controlled: !!navigator.serviceWorker.controller, shell: shell || null, keys };
  })()`);
  if (state.active && state.controlled && state.keys && state.keys.includes("/")) break;
  await sleep(500);
}
// SW_OFFLINE_DROP_SHELL=1: the shell cache is deleted before the server goes (a worker whose shell was never cached)
if (process.env.SW_OFFLINE_DROP_SHELL === "1") {
  await evalIn(p1, `caches.keys().then((ns) => Promise.all(ns.filter((n) => n.startsWith('levain-shell-v')).map((n) => caches.delete(n)))).then(() => 1)`);
}
console.log(JSON.stringify({ online: state }));
// the parent stops the server, then writes one line
await new Promise((r) => { process.stdin.once("data", r); process.stdin.once("end", r); });
const t0 = Date.now();
const made = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: "PUT" })).json();
const p2 = await attach(made.webSocketDebuggerUrl);
await p2("Page.enable");
// the cold-open tab: polled until it shows text at the url (a tab still waiting on the network reads as about:blank), up to
// WAIT_MS; `ms` is how long the user stared at the blank before it painted, null when it never did
const WAIT_MS = Number(process.env.SW_OFFLINE_WAIT_MS || 15000);
let snap = { error: "never polled" }, ms = null;
while (Date.now() - t0 < WAIT_MS) {
  await sleep(250);
  snap = await evalIn(p2, SNAP);
  if (snap && snap.href && snap.href.startsWith(url.replace(/\/$/, "")) && snap.text) { ms = Date.now() - t0; break; }
}
console.log(JSON.stringify({ offline: snap, ms }));
done(0);
