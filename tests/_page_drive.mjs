// Drives a served cockpit page in headless Chrome over the DevTools protocol, for tests that must
// submit a form rather than read the DOM. argv: <chrome> <url> <steps.json>. A step is one of
//   {"eval": "<expression>"}                 its value is collected
//   {"until": "<expression>", "ms": <n>}     polled until truthy; collected, or {"timeout": ...}
//   {"posts": <n>, "ms": <n>}                waits until n POSTs have been sent
//   {"emulate": {"w": <px>, "h": <px>, "dpr": <n>, "mobile": <bool>}}   a device size, applied before the page is drawn again
//   {"scheme": "dark"|"light"}              the phone's colour-scheme setting (prefers-color-scheme), applied before the next read
//   {"shot": "<abs path.png>", "full": <bool>}   saves a PNG screenshot there (the whole page when full); collected as {"shot": path}
// A dialog is declined. Prints {"values": [...], "posts": ["<path> <body>", ...]} as JSON on stdout.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";

const [chromePath, url, stepsPath] = process.argv.slice(2);
const steps = JSON.parse(readFileSync(stepsPath, "utf8"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const port = await new Promise((r) => { const s = createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
const prof = mkdtempSync(join(tmpdir(), "levain-page-drive-"));
const chromeFlags = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "chrome_flags.json"), "utf8")).flags;
const chrome = spawn(chromePath, [...chromeFlags, `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`,
  "--no-first-run", "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
// the Python runner gives the driver its own TMPDIR (levain-run-*); a parent that died leaves that for the driver to remove
const scratch = /\/levain-run-[^/]+\/?$/.test(tmpdir()) ? tmpdir() : null;
const rm = (d) => { try { rmSync(d, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch {} };
// remove the profile only once Chrome is gone: a live Chrome writes into it again
const done = (code) => {
  const fin = () => { rm(prof); if (scratch) rm(scratch); process.exit(code); };
  if (chrome.exitCode !== null || chrome.signalCode !== null) return fin();
  chrome.once("exit", fin); setTimeout(fin, 3000); chrome.kill("SIGKILL");
};
// Chrome must not outlive this process on any exit node can see: a signal, a thrown error, or the normal exit.
// (SIGKILL is invisible to node; the Python runner owns that case by killing the whole process group, and a parent
// that died without doing so is noticed below: node is then adopted by PID 1.)
for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, () => done(130));
process.on("uncaughtException", (e) => { console.error(e && e.stack || String(e)); done(1); });
process.on("unhandledRejection", (e) => { console.error(e && e.stack || String(e)); done(1); });
process.on("exit", () => { try { chrome.kill(); } catch {} });
const parent = process.ppid;
setInterval(() => { if (process.ppid !== parent) done(130); }, 1000).unref();
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
  // a prompt (the write-token ask) is declined, as an operator who has no token would
  if (d.method === "Page.javascriptDialogOpening") ws.send(JSON.stringify({ id: ++id, method: "Page.handleJavaScriptDialog", params: { accept: false } }));
  if (d.id && pend.has(d.id)) { pend.get(d.id)(d); pend.delete(d.id); }
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Page.enable"); await send("Network.enable");
await send("Page.navigate", { url });
const values = [];
const evaluate = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  return r.result?.exceptionDetails ? { error: r.result.exceptionDetails.text + " " + (r.result.exceptionDetails.exception?.description || "") }
    : r.result?.result?.value ?? null;
};
for (const s of steps) {
  const deadline = Date.now() + (s.ms || 20000);
  if (s.emulate !== undefined) {
    await send("Emulation.setDeviceMetricsOverride", { width: s.emulate.w, height: s.emulate.h, deviceScaleFactor: s.emulate.dpr || 1, mobile: !!s.emulate.mobile });
    await sleep(600);
  } else if (s.scheme !== undefined) {
    await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: s.scheme }] });
    await sleep(300);
  } else if (s.shot !== undefined) {
    // a page still being attached answers "Not attached to an active page": retry until the step's deadline
    let r;
    for (;;) {
      r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: !!s.full });
      if (r.result?.data || Date.now() >= deadline) break;
      await sleep(200);
    }
    if (!r.result?.data) throw new Error("screenshot failed: " + JSON.stringify(r.error || r.result));
    writeFileSync(s.shot, Buffer.from(r.result.data, "base64"));
    values.push({ shot: s.shot });
  } else if (s.posts !== undefined) {
    while (posts.length < s.posts && Date.now() < deadline) await sleep(100);
  } else if (s.until !== undefined) {
    let v = null;
    for (;;) {
      v = await evaluate(`(() => { try { return (${s.until}); } catch (e) { return null; } })()`);
      if (v || Date.now() >= deadline) break;
      await sleep(100);
    }
    values.push(v || { timeout: s.until });
  } else {
    values.push(await evaluate(s.eval));
  }
}
process.stdout.write(JSON.stringify({ values, posts }));
ws.close(); done(0);
