// Drives a served cockpit page in headless Chrome over the DevTools protocol, for tests that must
// submit a form rather than read the DOM. argv: <chrome> <url> <steps.json>. A step is
// {"eval": "<expression>"} (its value is collected) or {"wait": <ms>}. Prints
// {"values": [...], "posts": ["<path> <body>", ...]} as JSON on stdout.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";

const [chromePath, url, stepsPath] = process.argv.slice(2);
const steps = JSON.parse(readFileSync(stepsPath, "utf8"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const port = await new Promise((r) => { const s = createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
const prof = mkdtempSync(join(tmpdir(), "levain-page-drive-"));
const chrome = spawn(chromePath, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`,
  "--no-first-run", "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
const done = (code) => { chrome.kill(); try { rmSync(prof, { recursive: true, force: true }); } catch {} process.exit(code); };
let targets = null;
for (let i = 0; i < 80 && !targets; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { await sleep(250); } }
if (!targets) { console.error("chrome never answered"); done(2); }
const ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0; const pend = new Map(); const posts = [];
ws.addEventListener("message", (m) => {
  const d = JSON.parse(m.data);
  if (d.method === "Network.requestWillBeSent" && d.params.request.method === "POST") {
    posts.push(d.params.request.url.replace(/^https?:\/\/[^/]+/, "") + " " + (d.params.request.postData || ""));
  }
  if (d.id && pend.has(d.id)) { pend.get(d.id)(d); pend.delete(d.id); }
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Page.enable"); await send("Network.enable");
await send("Page.navigate", { url });
const values = [];
for (const s of steps) {
  if (s.wait) { await sleep(s.wait); continue; }
  const r = await send("Runtime.evaluate", { expression: s.eval, awaitPromise: true, returnByValue: true });
  values.push(r.result?.exceptionDetails ? { error: r.result.exceptionDetails.text + " " + (r.result.exceptionDetails.exception?.description || "") }
    : r.result?.result?.value ?? null);
}
process.stdout.write(JSON.stringify({ values, posts }));
ws.close(); done(0);
