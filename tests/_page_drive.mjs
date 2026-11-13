// Drives a served cockpit page in headless Chrome over the DevTools protocol, for tests that must
// submit a form rather than read the DOM. argv: <chrome> <url> <steps.json>. A step is one of
//   {"eval": "<expression>"}                 its value is collected
//   {"until": "<expression>", "ms": <n>}     polled until truthy and collected; a wait that never comes true FAILS the
//                                            drive (stderr names the step and its expression, exit 1), unless the step
//                                            carries "may_timeout": true, which collects {"timeout": ...} instead
//   {"posts": <n>, "ms": <n>}                waits until n POSTs have been sent; fewer by the deadline FAILS the drive
//                                            (same rule and same "may_timeout": true opt-out as `until`)
//   {"do": "<expr>", "until": "<expr>", "ms": <n>, "every": <m>}   act until: runs `do`, polls the condition every 100 ms, and
//   {"do": "<expr>", "posts": <n>, "ms": <n>, "every": <m>}        runs `do` AGAIN every `every` ms (default 1500) while the condition
//                                            is false, until the deadline. For an action whose effect can be lost (a click on a node a repaint
//                                            replaced, a click before the handler is bound): the action is retried together with its assertion.
//                                            Only for an action that is safe to repeat. A throw inside `do` is retried, and the last one is named
//                                            in the failure message. Fails, and opts out with "may_timeout", exactly as `until` / `posts` do;
//                                            the `until` form collects the condition's value, the `posts` form collects nothing.
// An `eval` that throws (a null element, a syntax error, a rejected promise) FAILS the drive: stderr names the step and the error, and
// the timeout-step-<n> files are saved as for a wait. A step that expects the error carries "may_error": true, and then the
// error is collected as {"error": "..."} in place of a value. The one exception is a plain `eval` that navigates the page (a reload, a
// location change): Chrome may answer it with a destroyed-context / navigated-or-closed error (NAVIGATION_ERRORS below), which is
// collected as {"navigated": true} and does not fail the drive.
// Every deadline here (`ms` of until / posts / act-until / shot, the 20000 default, the page-load gate) is multiplied by the
// LEVAIN_TEST_WAIT_SCALE environment variable, so a slow runner is one setting. The whole string must be a finite decimal number
// (tests/_waits.py parses it by the same pattern); it is clamped to [1, 20]; anything else (2junk, inf, nan, 1e9999) is 1.
// The fixed settle sleeps (emulate / scheme / motion / key) are not scaled.
// A failing wait also saves timeout-step-<n>.png (+ .txt, the page text) beside the steps file and names it on stderr.
//   {"emulate": {"w": <px>, "h": <px>, "dpr": <n>, "mobile": <bool>, "safe": {"top","bottom","left","right"} optional}}   a device size, applied before the page is drawn again
//   {"scheme": "dark"|"light"}              the phone's colour-scheme setting (prefers-color-scheme), applied before the next read
//   {"motion": "reduce"|"no-preference"}    the phone's reduce-motion setting (prefers-reduced-motion); it keeps a scheme step's setting
//   {"init": "<js source>"}                 runs before the page's own scripts on every later load, then reloads the page once (a
//                                            faked user agent, a deleted Notification); nothing is collected
//   {"shot": "<abs path.png>", "full": <bool>}   saves a PNG screenshot there (the whole page when full); collected as {"shot": path}
//   {"authenticator": {"rpId", "privateKey", "credentialId", "userHandle"}}   a virtual platform authenticator (user
//                                            verification on) holding that one resident P-256 credential (base64
//                                            PKCS#8 key, base64 ids), so the page's navigator.credentials.get signs
//                                            with a key the test knows; nothing is collected. A later `authenticator` step adds
//                                            its credential to that same authenticator (a device holding several passkeys)
//   {"user_verified": <bool>}               the last authenticator's answer to user verification from now on (false: the
//                                            page's navigator.credentials.get with userVerification "required" is refused,
//                                            a cancelled Face ID as the page sees it); nothing is collected
//   {"key": "<key>"}                        one trusted key press (down, then up) to the focused element; nothing is collected
//   {"sw": "<expression>", "ms": <n>}      evaluates the expression (awaited, by value) inside the page origin's service worker (its /sw.js
//                                            target, found over the same debugging port) and collects the value; the worker must already be
//                                            running (a page that registered it and a step that waited for `navigator.serviceWorker.ready`).
//                                            Needs the full Chrome (a drive that names serviceWorker is routed there). A worker that never
//                                            shows up, or an expression that throws, FAILS the drive
//   {"tap": "<expression>", "ms": <n>}      one trusted mouse press and release at the centre of the element the expression
//                                            names, scrolled into view first, so the page's listeners read event.isTrusted
//                                            true (an evaluated .click() is never trusted). Polled until the element exists,
//                                            is not disabled and is the node hit at that point (or holds it); otherwise the
//                                            drive FAILS as a wait does. Collected as 1, as an `eval` of `x.click() || 1` was.
//                                            An element in a same-origin frame is pressed at its point in the top page.
//                                            "before": "<expression>" runs once ahead of it (not collected); "times": <n>
//                                            presses n times at the one point; "after": "<expression>" runs once right
//                                            after the presses (not collected)
// App content that loads asynchronously is not waited for: put an `until` step before the `shot`.
// LEVAIN_DRIVE_CDP_PORT: the port of an already running headless Chrome (the test session's shared one). The drive then runs in a
// fresh browser context of that Chrome (its own cookies, storage and cache) and disposes the context on every exit, instead of
// launching a Chrome of its own: each launch costs macOS keychain calls that no flag removes. A call that passes
// LEVAIN_DRIVE_CHROME_FLAGS (flags only a new process can take) ignores the port and spawns, exactly as without it.
// LEVAIN_DRIVE_LAUNCH_LOG: a file that gets one "spawn" line per Chrome this driver launches (a count for a measurement).
// A dialog is declined. Prints {"values": [...], "posts": ["<path> <body>", ...], "dialogs": ["<type>: <message>", ...]} as JSON on stdout.
import { spawn } from "node:child_process";
import { accessSync, appendFileSync, constants, mkdtempSync, readdirSync, statSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";

const [chromePath, url, stepsPath] = process.argv.slice(2);
const steps = JSON.parse(readFileSync(stepsPath, "utf8"));
const media = {};   // the emulated media features set so far: each setEmulatedMedia call replaces the whole list
// the one environment scale for every deadline (never below 1: a fast machine does not shorten what a test asked for)
const parseScale = (raw) => {
  if (!/^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/.test(raw)) return 1;
  const v = Number(raw);
  return Number.isFinite(v) ? Math.min(Math.max(v, 1), 20) : 1;
};
const WAIT_SCALE = parseScale(process.env.LEVAIN_TEST_WAIT_SCALE ?? "");
const scaled = (ms) => Math.round(ms * WAIT_SCALE);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chromeCfg = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "chrome_flags.json"), "utf8"));
const chromeFlags = chromeCfg.flags;
const DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const versionKey = (name) => (name.match(/([0-9]+(?:\.[0-9]+)*)$/)?.[1] ?? "").split(".").filter(Boolean).map(Number);
const newerFirst = (a, b) => {
  const x = versionKey(a), y = versionKey(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] ?? -1) !== (y[i] ?? -1)) return (y[i] ?? -1) - (x[i] ?? -1);
  return 0;
};
const dirsOf = (d) => readdirSync(d, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
// chrome-headless-shell (Chrome for Testing's headless binary) when installed under chrome_flags.json's headless_shell_root: full
// Chrome mints a Secure Enclave key in the operator's keychain on every fresh profile (no flag stops it), the shell never does.
// A shell binary counts only if it is a regular executable file in a "-mac-<this host's arch>" directory (versions compared
// numerically; a stray file is skipped); anything else falls back to the given Chrome. The given Chrome is kept when
// LEVAIN_FULL_CHROME=1, when CHROME is set (as Python reads it), or when it is not DEFAULT_CHROME (a caller that named a Chrome on purpose gets that Chrome).
const pickChrome = (given, cfg) => {
  if (process.env.LEVAIN_FULL_CHROME === "1" || (process.env.CHROME ?? "") !== "" || given !== DEFAULT_CHROME) return given;
  const raw = cfg.headless_shell_root;
  if (raw === undefined || raw === null || raw === "") return given;
  if (typeof raw !== "string") { console.error("chrome_flags.json: headless_shell_root is not a string; using the given Chrome"); return given; }
  const arch = { arm64: "arm64", x64: "x64" }[process.arch];
  if (process.platform !== "darwin" || !arch) return given;
  let root, versions;
  try { root = raw.replace(/^~/, userInfo().homedir); versions = dirsOf(root).sort(newerFirst); } catch { return given; }
  for (const v of versions) {
    let subs;
    try { subs = dirsOf(join(root, v)).filter((n) => n.endsWith(`-mac-${arch}`)).sort(); } catch { continue; }
    for (const a of subs) {
      const bin = join(root, v, a, "chrome-headless-shell");
      try { if (statSync(bin).isFile()) { accessSync(bin, constants.X_OK); return bin; } } catch {}
    }
  }
  return given;
};
// LEVAIN_DRIVE_CHROME_FLAGS: a JSON array of extra Chrome flags (a test that must reach the server under a proxied name)
const extraFlags = process.env.LEVAIN_DRIVE_CHROME_FLAGS ? JSON.parse(process.env.LEVAIN_DRIVE_CHROME_FLAGS) : [];
// chrome-headless-shell has no service worker, Push or Notification API: a drive that needs them DECLARES it with
// LEVAIN_DRIVE_FULL_CHROME=1 (the test sets it; a pytest fixture in the test file) and then runs on the full Chrome given in argv, in
// a process of its own (never the session's shell), and pays the one keychain key that costs. The source-text sniff below is only a
// fail-safe that also routes to the full Chrome (union with the declaration, never instead of it). LEVAIN_FULL_CHROME=1 also takes
// a drive off the session's shell port: the drive spawns its own full Chrome.
const needsFullChrome = process.env.LEVAIN_DRIVE_FULL_CHROME === "1" || process.env.LEVAIN_FULL_CHROME === "1"
  || steps.some((s) => s.sw !== undefined)   // a `sw` step talks to a worker the shell cannot run, whatever its expression says
  || /serviceWorker|[Pp]ushManager\.|\.pushManager|Notification\.|new Notification/.test(JSON.stringify(steps));   // actual use only: a drive that DELETES window.PushManager (no push API on purpose) stays on the shell
let sharedPort = /^[0-9]+$/.test(process.env.LEVAIN_DRIVE_CDP_PORT ?? "") && extraFlags.length === 0 && !needsFullChrome ? Number(process.env.LEVAIN_DRIVE_CDP_PORT) : null;
// a Chrome of our own: this process owns it, its port and its profile (also the way out when the shared Chrome fails to set up)
let prof = null; let port = null;
let chrome = { exitCode: 0, signalCode: null, kill() {}, once() {} };   // nothing of ours to kill while the Chrome belongs to the session
const startSpawn = async () => {
  const bin = needsFullChrome ? chromePath : pickChrome(chromePath, chromeCfg);
  if (process.env.LEVAIN_TEST_PICK_CHROME_DRY === "1") { process.stdout.write(bin + "\n"); process.exit(0); }   // a test of the selection: print the pick, launch nothing
  port = await new Promise((r) => { const s = createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
  prof = mkdtempSync(join(tmpdir(), "levain-page-drive-"));
  if (process.env.LEVAIN_DRIVE_LAUNCH_LOG) { try { appendFileSync(process.env.LEVAIN_DRIVE_LAUNCH_LOG, "spawn\n"); } catch {} }
  chrome = spawn(bin, [...chromeFlags, ...extraFlags, `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`,
    "--no-first-run", "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
};
if (!sharedPort) await startSpawn();
// the Python runner gives the driver its own TMPDIR (levain-run-*); a parent that died leaves that for the driver to remove
const scratch = /\/levain-run-[^/]+\/?$/.test(tmpdir()) ? tmpdir() : null;
const rm = (d) => { try { rmSync(d, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch {} };
// a wait that gives up: every setup step against the shared Chrome is bounded, and a refusal there falls back to a Chrome of our own
const SETUP_MS = 8000;
const within = (p, ms, what) => Promise.race([p, sleep(ms).then(() => { throw new Error(`${what}: no answer within ${ms}ms`); })]);
// pending commands of one websocket: all of them are rejected when the socket closes or errors, never left waiting
const failPending = (map, why) => { for (const { rej } of [...map.values()]) rej(new Error(why)); map.clear(); };
const openSocket = (url, what) => new Promise((res, rej) => {
  const w = new WebSocket(url);
  w.addEventListener("open", () => res(w));
  w.addEventListener("error", () => rej(new Error(`${what}: the connection failed`)));
  w.addEventListener("close", () => rej(new Error(`${what}: the connection closed`)));
});
// shared Chrome: the browser connection that owns this drive's context, and the context to dispose
let bws = null; let contextId = null; let bid = 0; const bpend = new Map();
const bsend = (method, params = {}) => new Promise((res, rej) => {
  if (!bws || bws.readyState !== 1) return rej(new Error("the browser connection is not open"));
  const i = ++bid; bpend.set(i, { res, rej });
  try { bws.send(JSON.stringify({ id: i, method, params })); } catch (e) { bpend.delete(i); rej(e); }
});
const disposeContext = async () => {
  if (!bws || !contextId) return;
  const c = contextId; contextId = null;
  // bounded: a wedged browser must not hold the exit; disposeOnDetach closes the context when this connection drops anyway
  await Promise.race([bsend("Target.disposeBrowserContext", { browserContextId: c }), sleep(2000)]);
};
// remove the profile only once Chrome is gone: a live Chrome writes into it again
let exiting = false;
const done = (code) => {
  if (exiting) return;   // the first exit reason stands; later calls (a signal during a normal exit) change nothing
  exiting = true;
  if (sharedPort) {
    const fin = () => { if (scratch) rm(scratch); process.exit(code); };
    setTimeout(fin, 3000); disposeContext().then(fin, fin);
    return;
  }
  const fin = () => { if (prof) rm(prof); if (scratch) rm(scratch); process.exit(code); };
  if (chrome.exitCode !== null || chrome.signalCode !== null) return fin();
  chrome.once("exit", fin); setTimeout(fin, 3000); chrome.kill("SIGKILL");
};
// done(), then stop here: the exit is scheduled, not immediate, and nothing after it may run
const giveUp = async (code) => { done(code); await new Promise(() => {}); };
// Chrome must not outlive this process on any exit node can see: a signal, a thrown error, or the normal exit.
// (SIGKILL is invisible to node; the Python runner owns that case by killing the whole process group, and a parent
// that died without doing so is noticed below: node is then adopted by PID 1.)
for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, () => done(130));
process.on("uncaughtException", (e) => { console.error(e && e.stack || String(e)); done(1); });
process.on("unhandledRejection", (e) => { console.error(e && e.stack || String(e)); done(1); });
process.on("exit", () => { try { chrome.kill(); } catch {} });
const parent = process.ppid;
setInterval(() => { if (process.ppid !== parent) done(130); }, 1000).unref();
let pageWsUrl = null; let ourTargetId = null;   // ourTargetId: the shared Chrome's id for this drive's page (the timeout block asks the browser whether it is alive)
const setupShared = async () => {
  let version = null;
  for (const end = Date.now() + 5000; Date.now() < end && !version;) {
    try { version = await (await fetch(`http://127.0.0.1:${sharedPort}/json/version`, { signal: AbortSignal.timeout(3000) })).json(); } catch { await sleep(250); }
  }
  if (!version?.webSocketDebuggerUrl) throw new Error("the shared chrome never answered");
  bws = await within(openSocket(version.webSocketDebuggerUrl, "the shared chrome"), SETUP_MS, "the shared chrome's websocket");
  bws.addEventListener("message", (m) => { const d = JSON.parse(m.data); if (d.id && bpend.has(d.id)) { const e = bpend.get(d.id); bpend.delete(d.id); e.res(d); } });
  bws.addEventListener("close", () => failPending(bpend, "the shared chrome closed the connection"));
  bws.addEventListener("error", () => failPending(bpend, "the shared chrome's connection failed"));
  const ctx = await within(bsend("Target.createBrowserContext", { disposeOnDetach: true }), SETUP_MS, "createBrowserContext");
  contextId = ctx.result?.browserContextId;
  if (!contextId) throw new Error("no browser context: " + JSON.stringify(ctx.error || ctx.result));
  // newWindow: the width and height apply to a target only when it gets a window of its own
  const made = await within(bsend("Target.createTarget", { url: "about:blank", browserContextId: contextId, width: 1440, height: 1000, newWindow: true }), SETUP_MS, "createTarget");
  if (!made.result?.targetId) throw new Error("no target: " + JSON.stringify(made.error || made.result));
  ourTargetId = made.result.targetId;
  pageWsUrl = `ws://127.0.0.1:${sharedPort}/devtools/page/${made.result.targetId}`;
  return await within(openSocket(pageWsUrl, "the shared chrome's page"), SETUP_MS, "the page websocket");
};
let ws = null;
if (sharedPort) {
  try { ws = await setupShared(); } catch (e) {
    // a setup failure is never a test failure: this drive runs on a Chrome of its own, as without a shared one
    console.error(`shared chrome unusable (${e && e.message || e}); launching a Chrome of its own`);
    const b = bws; bws = null; contextId = null; sharedPort = null;   // the context goes with the connection (disposeOnDetach)
    try { b && b.close(); } catch {}
    failPending(bpend, "abandoned");
    await startSpawn();
  }
}
if (!ws) {
  let targets = null;
  for (let i = 0; i < 80 && !targets; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { await sleep(250); } }
  if (!targets) { console.error("chrome never answered"); await giveUp(2); }
  const pageTarget = targets.find((t) => t.type === "page");
  pageWsUrl = pageTarget.webSocketDebuggerUrl; ourTargetId = pageTarget.id;   // the id of the page THIS drive drives: a popup the page opened is a page target too
  ws = await within(openSocket(pageWsUrl, "chrome's page"), 15000, "the page websocket");
}
let id = 0; let navStarts = 0; let loads = 0; const pend = new Map(); const posts = []; const dialogs = []; const trail = []; const consoleLog = []; const workerWaits = [];   // consoleLog: the page's console output and errors, for the timeout block; trail: what a timeout dump reports of the page's refusals and prompts; workerWaits: how each awaitWorkerSettled ended (the last 10)
const T0 = Date.now(); let lastEventAt = T0; const navTrail = [];   // lastEventAt / navTrail: what the page last said and when, for the timeout block (a page that stops answering is told apart from one that never loaded)
ws.addEventListener("message", (m) => {
  const d = JSON.parse(m.data);
  if (d.method) lastEventAt = Date.now();   // events only: the {id,result} replies to the driver's own commands (the timeout probe's included) are not the page speaking
  if (/^(Page\.(frameStartedLoading|frameNavigated|frameStoppedLoading|loadEventFired|domContentEventFired|navigatedWithinDocument)|Inspector\.(targetCrashed|detached))$/.test(d.method || "")) { navTrail.push(`+${Date.now() - T0}ms ${d.method.replace(/^(Page|Inspector)\./, "")}${d.params && d.params.reason ? " " + d.params.reason : ""}`); if (navTrail.length > 14) navTrail.shift(); }
  if (d.method === "Inspector.targetCrashed") consoleLog.push("PAGE CRASHED: Inspector.targetCrashed (the renderer died)");
  if (d.method === "Network.responseReceived" && d.params.response.status >= 400) trail.push(`${Math.round(d.params.timestamp * 1000) % 1e6}ms ${d.params.response.status} ${d.params.response.url.replace(/^https?:\/\/[^/]+/, "")}`);
  if (d.method === "Runtime.consoleAPICalled") consoleLog.push(`console.${d.params.type}: ` + (d.params.args || []).map((a) => String(a.value !== undefined ? a.value : (a.description || a.type))).join(" ").slice(0, 300));
  if (d.method === "Runtime.exceptionThrown") consoleLog.push("page error: " + String(d.params.exceptionDetails?.exception?.description || d.params.exceptionDetails?.text).slice(0, 300));
  if (d.method === "Log.entryAdded") consoleLog.push(`log.${d.params.entry.level}: ${String(d.params.entry.text).slice(0, 300)}`);
  if (consoleLog.length > 200) consoleLog.shift();
  if (d.method === "Page.loadEventFired") loads++;
  if (trail.length > 50) trail.shift();          // a page refused in a loop keeps its last 50
  if (d.method === "Page.javascriptDialogOpening") dialogs.push(`${d.params.type}: ${String(d.params.message)}`);
  if (d.method === "Page.frameStartedLoading" || d.method === "Page.frameRequestedNavigation" || d.method === "Page.frameNavigated") navStarts++;
  if (d.method === "Page.javascriptDialogOpening") trail.push(`dialog declined: ${d.params.type} ${String(d.params.message).slice(0, 80)}`);
  // the page's own telemetry, never an operator POST: the SW-open landing report (POST /webpush/landed, sw_open.js 1106-53) and the
  // ring-trace report (POST /webpush/trace, sw_open.js reportTrace: 2.5 s after load once the worker has written ring entries, so
  // whether it fires inside a drive is timing, and a test asserting no posts must not depend on it)
  if (d.method === "Network.requestWillBeSent" && d.params.request.method === "POST" && !/^https?:\/\/[^/]+\/webpush\/(landed|trace)(\?|$)/.test(d.params.request.url)) {
    posts.push(d.params.request.url.replace(/^https?:\/\/[^/]+/, "") + " " + (d.params.request.postData || ""));
  }
  // a prompt (the write-token ask) is declined, as an operator who has no token would
  if (d.method === "Page.javascriptDialogOpening") ws.send(JSON.stringify({ id: ++id, method: "Page.handleJavaScriptDialog", params: { accept: false } }));
  if (d.id && pend.has(d.id)) { const e = pend.get(d.id); pend.delete(d.id); e.res(d); }
});
ws.addEventListener("close", () => failPending(pend, "the page connection closed"));
ws.addEventListener("error", () => failPending(pend, "the page connection failed"));
const send = (method, params = {}) => new Promise((res, rej) => {
  if (ws.readyState !== 1) return rej(new Error("the page connection is not open"));
  const i = ++id; pend.set(i, { res, rej });
  try { ws.send(JSON.stringify({ id: i, method, params })); } catch (e) { pend.delete(i); rej(e); }
});
await send("Page.enable"); await send("Network.enable");
for (const m of ["Runtime.enable", "Log.enable", "Inspector.enable"]) { try { await Promise.race([send(m), sleep(3000)]); } catch {} }   // console capture is best-effort: never a reason to fail a drive
await send("Page.navigate", { url });
const values = [];
// an act-until re-run's own result key, and whether a `do` source parses as one expression (compiled here in node, never run)
const GUARD_KEY = "__pageDriveGuard";
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const isExpression = (src) => { try { new AsyncFunction(`return (${src}\n);`); return true; } catch (e) { return false; } };
// a CDP call that gives up after `ms`: a page that never answers must not outlive the step's deadline
const sendWithin = (ms, method, params) => Promise.race([send(method, params), sleep(Math.max(ms, 1)).then(() => ({ timedOut: true }))]);
// the outcome of an evaluation, kept apart from the page's own value: { value } or { error } (an exception the page raised,
// a syntax error, a wait on a promise that rejects), or { error: "no answer ..." } when CDP did not reply in time
const evaluateOutcome = async (expression, ms = 60000) => {
  const r = await sendWithin(ms, "Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.timedOut) return { error: `no answer from the page within ${ms}ms` };
  if (r.error) return { error: `CDP error ${r.error.code}: ${r.error.message}` };       // a top-level protocol error is never a value
  if (r.result?.exceptionDetails) return { error: r.result.exceptionDetails.text + " " + (r.result.exceptionDetails.exception?.description || "") };
  if (!r.result?.result) return { error: "malformed Runtime.evaluate answer: " + JSON.stringify(r).slice(0, 200) };
  return { value: r.result.result.value ?? null };
};
// what Chrome says when a navigation takes the page out from under an evaluation (the context is gone / the target moved on)
const NAVIGATION_ERRORS = ["Execution context was destroyed", "Inspected target navigated or closed", "Cannot find context with specified id"];
const isNavigationError = (msg) => NAVIGATION_ERRORS.some((m) => String(msg).includes(m));
const evaluate = async (expression) => {
  const o = await evaluateOutcome(expression);
  return "error" in o ? { error: o.error } : o.value;
};
// Page.navigate returns at commit: wait (bounded) for the load before the first step, or an early shot sees a blank page
for (const end = Date.now() + scaled(15000); Date.now() < end;) {
  if (await evaluate("document.readyState") === "complete") break;
  await sleep(100);
}
// a wait that fails the drive leaves a PNG of what the page showed beside the steps file (the test's tmp dir),
// and its path on stderr, so the failure can be read rather than guessed at
// (each call is raced against ~3 s: a wedged page must not turn the named failure into the runner's timeout)
// the diagnostic block a timeout prints AFTER its named failure line: page state through one probe that cannot throw, the
// last 20 console messages / page errors, and the screenshot path; capped at 4000 characters, never able to change the exit
// does the BROWSER still list this drive's page, and what does it say it is showing? (the page's own channel is the one in doubt, so ask the browser's)
const targetState = async () => {
  try {
    if (sharedPort && bws && ourTargetId) {
      const r = await Promise.race([bsend("Target.getTargets"), sleep(2000).then(() => null)]);
      if (!r) return "browser did not answer Target.getTargets within 2000ms";
      if (r.error) return `browser answered Target.getTargets with an error: ${String(r.error.message || JSON.stringify(r.error)).slice(0, 120)}`;
      const t = (r.result?.targetInfos || []).find((x) => x.targetId === ourTargetId);
      return t ? `listed, id=${ourTargetId} attached=${t.attached} url=${String(t.url).slice(0, 120)} title=${String(t.title).slice(0, 60)}` : "NOT LISTED (the page target is gone)";
    }
    if (port) {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`, { signal: AbortSignal.timeout(2000) })).json();
      const t = list.find((x) => x.id === ourTargetId);
      return t ? `listed, id=${ourTargetId} url=${String(t.url).slice(0, 120)} title=${String(t.title).slice(0, 60)}` : "NOT LISTED (this drive's page target is not in the browser's list)";
    }
  } catch (e) { return "browser unreachable: " + String(e && e.message || e).slice(0, 80); }
  return "no browser connection to ask";
};
const timeoutBlock = async (shotPath) => {
  const quietMs = Date.now() - lastEventAt;   // taken before any await: the probe's own wait must not read as the page having spoken or being silent
  try {
    const probe = await evaluateOutcome("(() => { try { const m = document.querySelector('#levain-modal'); return JSON.stringify({ url: location.href, ready: document.readyState, modal: !!m, rows: m ? m.querySelectorAll('[data-question-row]').length : null, text: String((document.body && document.body.innerText) || '').slice(0, 600) }); } catch (e) { return JSON.stringify({ probe_error: String(e) }); } })()", 3000);
    let o = {}; try { o = JSON.parse(probe.value); } catch { o = { probe_error: probe.error || "no answer" }; }
    // What the page's evaluation cannot say when the page does not answer: how the service-worker waits before reloads ended, how many
    // navigation / load events this connection saw, and the url the BROWSER holds for this page's target (the /json endpoint answers
    // from the browser process, so an empty url there is a navigation that never committed, not a page that is merely busy).
    const waits = workerWaits.length
      ? workerWaits.map((w) => `${w.how} after ${w.ms}ms/${w.polls} polls (last ${w.last}, ${Math.max(0, Date.now() - w.at)}ms ago)`).join("; ")
      : "none run";
    let target = "unknown";
    try {
      const list = await (await fetch(`http://127.0.0.1:${sharedPort || port}/json`, { signal: AbortSignal.timeout(3000) })).json();
      const mine = list.find((t) => pageWsUrl && String(t.webSocketDebuggerUrl || "").endsWith("/" + String(pageWsUrl).split("/").pop()));
      target = mine ? `${mine.type} url=${JSON.stringify(String(mine.url).slice(0, 120))} title=${JSON.stringify(String(mine.title || "").slice(0, 40))}` : `not listed among ${list.length} targets`;
    } catch (e) { target = `/json failed: ${String((e && e.message) || e).slice(0, 80)}`; }
    const lines = ["--- page state at the timeout ---",
      `PAGE URL: ${o.url ?? "?"}`, `READY STATE: ${o.ready ?? "?"}`,
      `WORKER WAITS: ${waits}`, `NAVIGATION: events=${navStarts} loads=${loads}`, `TARGET (browser's /json): ${target}`,
      `MODAL: #levain-modal exists=${o.modal ?? "?"} [data-question-row] count=${o.rows ?? "?"}` + (o.probe_error ? ` (probe error: ${o.probe_error})` : ""),
      `TARGET: ${await targetState()}`, `LAST PAGE EVENT: ${quietMs}ms ago; NAV TRAIL: ${navTrail.join(", ") || "none"}`,
      `CONSOLE (last 20 of ${consoleLog.length}):`, ...consoleLog.slice(-20).map((c) => "  " + c),
      "BODY TEXT (first 600):", String(o.text ?? "").replace(/\n{2,}/g, "\n"),
      `SCREENSHOT: ${shotPath || "none"}`, "--- end page state ---"];
    console.error(lines.join("\n").slice(0, 4000));
  } catch {}
};
const shotOnTimeout = async (n) => {
  let shotPath = null;
  try { await shotOnTimeoutInner(n, (p) => { shotPath = p; }); } catch {}
  await timeoutBlock(shotPath);
};
const shotOnTimeoutInner = async (n, setPath) => {
  try {
    const r = await sendWithin(3000, "Page.captureScreenshot", { format: "png" });
    if (!r.result?.data) return;
    const p = join(dirname(stepsPath), `timeout-step-${n}.png`);
    writeFileSync(p, Buffer.from(r.result.data, "base64")); setPath(p);
    console.error(`screenshot at the timeout: ${p}`);
    const t = await evaluateOutcome("document.body.innerText", 3000);
    // the page's own state beside its text: which tab, which address, which kept keys (a timeout names where the page was)
    const st = await evaluateOutcome("JSON.stringify({mode: document.body.dataset.mode, hash: location.hash, ready: document.readyState, local: Object.keys(localStorage), session: Object.keys(sessionStorage), savedMode: (() => { try { return [localStorage, sessionStorage].map((x) => Object.keys(x).filter((k) => /mode/i.test(k)).map((k) => k + '=' + x.getItem(k))); } catch (e) { return String(e); } })()})", 3000);
    if (typeof t.value === "string") writeFileSync(p.replace(/\.png$/, ".txt"), "PAGE STATE: " + (typeof st.value === "string" ? st.value : JSON.stringify(st)) + "\nREFUSALS AND PROMPTS: " + JSON.stringify(trail) + "\n" + t.value);
  } catch {}
};
let authenticatorId = null;   // the virtual authenticator an `authenticator` step made (a `user_verified` step changes it)// The cockpit draws its first board in parts and the whole draw then REPLACES every node (S4): a step that taps or tags a node on a part loses
// it. The page says when its board is the one that stays with a POSITIVE signal, html[data-board-whole] (dashboard_boot.js: set only by the
// code that has just drawn the whole board, or by a terminal failure; absent is the default); a page with no #board is not the cockpit board
// and has nothing to wait for. The wait is bounded and not fatal (a page that never says gets its step after the bound, as every step did
// before the parts), reads through the short-timeout evaluateOutcome, and an error outcome (the page navigating, no answer) is a RETRY, never
// "ready". After one timeout the later steps skip the wait (a stuck page is named once, not paid for on every step). A step that watches the
// parts themselves says `"partial": true`.
let boardWaitGaveUp = false;
const WHOLE_BOARD = "(() => { try { return document.readyState === 'complete' && (!document.getElementById('board') || document.documentElement.hasAttribute('data-board-whole')); } catch (e) { return false; } })()";
const SETUP_STEPS = ["authenticator", "user_verified", "init", "emulate", "scheme"];
const waitForWholeBoard = async () => {
  for (const end = Date.now() + scaled(15000); Date.now() < end;) {
    const o = await evaluateOutcome(WHOLE_BOARD, Math.min(Math.max(end - Date.now(), 250), 1500));
    if (!("error" in o) && o.value === true) return;
    await sleep(50);
  }
  boardWaitGaveUp = true;
  console.error("page-drive: the board never said it was whole within the bound; later steps will not wait for it");
};
// The same race belongs to the `init` step's reload and to an `eval` step whose source contains `location.reload` (RELOADS_PAGE): both wait for the
// worker first. Any other way a step reloads or navigates (`location.assign`, `location.href = ...`, `history.go(0)`, a click that navigates) is NOT
// covered: such a step waits for nothing, and a flake in one of them gets the same fix by widening RELOADS_PAGE.
const awaitWorkerSettled = async () => {
  // A page that registers a service worker at load (the cockpit's /sw.js) is still INSTALLING it when the load event fires. Reloading
  // while it installs races the worker's install -> activate -> clients.claim against the reload's navigation: measured, under heavy
  // machine load, the reload's document response arrived, the worker activated in that same instant, and the navigation never
  // committed (no Page.frameNavigated, and every Runtime.evaluate queued behind it for 60 s). So a reload waits, bounded, until the
  // worker the page registered has settled (activated and in control); a page with no service-worker API passes at once. Not fatal: a worker
  // that never takes control only costs this bounded wait, and the reload is then sent as before.
  // No registration is NOT settled at once: register() is asynchronous, so under load the first poll can run before the page's own
  // registration exists. "No registration" counts only after it has been seen on 8 polls in a row (a page with no worker
  // pays that short wait); a registration that has appeared is waited for until it is activated and in control.
  // A register() and a reload sent in the SAME eval are not covered either: the registration does not exist yet when this wait runs.
  // Each probe is raced against the time left (as waitForWholeBoard does), so a page that does not answer cannot stretch the bound to the
  // 60 s evaluate default; an error outcome (the page navigating, no answer) is a retry until the deadline, never "settled".
  // How each wait ENDED is kept (workerWaits, reported by the timeout block): a hang after a reload reads very differently when the wait
  // left on `settled` than when it ran out its deadline or gave up on an absent registration, and the log alone cannot tell them apart.
  let absent = 0; let how = "deadline"; let last = "not polled"; let polls = 0; const t0 = Date.now();
  for (const end = t0 + scaled(5000); Date.now() < end;) {
    const o = await evaluateOutcome("(async () => { const sw = navigator.serviceWorker; if (!sw) return 'none'; const r = await sw.getRegistration();"
      + " if (!r) return 'absent'; return (!r.installing && !!r.active && r.active.state === 'activated' && !!sw.controller) ? 'settled' : 'pending'; })()",
      Math.min(Math.max(end - Date.now(), 250), scaled(1500)));
    polls++;
    last = "error" in o ? `error: ${String(o.error).slice(0, 80)}` : String(o.value);
    if ("error" in o) { absent = 0; await sleep(50); continue; }       // "8 in a row" means in a row: an error breaks the run
    if (o.value === "none" || o.value === "settled") { how = o.value; break; }
    absent = o.value === "absent" ? absent + 1 : 0;
    if (absent >= 8) { how = "absent8"; break; }
    await sleep(50);
  }
  workerWaits.push({ how, last, polls, ms: Date.now() - t0, at: Date.now() });
  if (workerWaits.length > 10) workerWaits.shift();
};
const RELOADS_PAGE = /\blocation\.reload\b/;
for (const s of steps) {
  if (!s.partial && !boardWaitGaveUp && !SETUP_STEPS.some((k) => s[k] !== undefined)) await waitForWholeBoard();
  const waitMs = scaled(s.ms || 20000);
  const deadline = Date.now() + waitMs;
  if (s.authenticator !== undefined) {
    // a second `authenticator` step adds its credential to the one authenticator (Chrome allows one internal authenticator
    // per environment), so a device that holds several passkeys is two steps
    if (!authenticatorId) {
      await send("WebAuthn.enable", { enableUI: false });
      const made = await send("WebAuthn.addVirtualAuthenticator", { options: { protocol: "ctap2", transport: "internal",
        hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
      authenticatorId = made.result?.authenticatorId;
      if (!authenticatorId) throw new Error("no virtual authenticator: " + JSON.stringify(made.error || made.result));
    }
    const a = s.authenticator;
    const added = await send("WebAuthn.addCredential", { authenticatorId, credential: { credentialId: a.credentialId,
      isResidentCredential: true, rpId: a.rpId, privateKey: a.privateKey, userHandle: a.userHandle, signCount: 0 } });
    if (added.error) throw new Error("the credential was not added: " + JSON.stringify(added.error));
  } else if (s.user_verified !== undefined) {
    if (!authenticatorId) throw new Error("user_verified needs an authenticator step before it");
    const set = await send("WebAuthn.setUserVerified", { authenticatorId, isUserVerified: !!s.user_verified });
    if (set.error) throw new Error("user verification was not set: " + JSON.stringify(set.error));
  } else if (s.init !== undefined) {
    // a script that runs before the page's own scripts on every load from now on, then one reload so it applies to this page
    const added = await send("Page.addScriptToEvaluateOnNewDocument", { source: s.init });
    if (added.error) throw new Error("the init script was not added: " + JSON.stringify(added.error));
    // the first load must be over before the reload is sent. The reloaded document is recognised by its own state (the mark the old document
    // carried is gone and it is complete), never by counting Page.loadEventFired: that event can be missed or land early/late against the
    // snapshot, which left this step waiting out its whole deadline on a reload that had in fact finished. Any stall is a setup failure.
    for (const end = Date.now() + scaled(15000);;) {
      if (await evaluate("document.readyState") === "complete") break;
      if (Date.now() >= end) throw new Error("init: the first load did not complete");
      await sleep(100);
    }
    await awaitWorkerSettled();
    await evaluate("window.__driveOldDocument = true");   // a mark the reloaded document will not carry
    const reloaded = await send("Page.reload", {});
    if (reloaded.error) throw new Error("init: the reload was refused: " + JSON.stringify(reloaded.error));
    // the old document's readyState can still read "complete" just after the call, so the mark, not the state alone, tells the new document
    for (const end = Date.now() + scaled(15000);;) {
      if (await evaluate("document.readyState === 'complete' && window.__driveOldDocument === undefined") === true) break;
      if (Date.now() >= end) throw new Error("init: the reload did not complete");
      await sleep(100);
    }
  } else if (s.emulate !== undefined) {
    await send("Emulation.setDeviceMetricsOverride", { width: s.emulate.w, height: s.emulate.h, deviceScaleFactor: s.emulate.dpr || 1, mobile: !!s.emulate.mobile });
    if (s.emulate.safe) await send("Emulation.setSafeAreaInsetsOverride", { insets: s.emulate.safe });   // the iPhone notch / status bar / home indicator
    await sleep(600);
  } else if (s.scheme !== undefined) {
    media["prefers-color-scheme"] = s.scheme;
    await send("Emulation.setEmulatedMedia", { features: Object.entries(media).map(([name, value]) => ({ name, value })) });
    await sleep(300);
  } else if (s.key !== undefined) {
    // a key a person presses, to whatever holds focus: a trusted event, so a focused <select> answers it as it answers a keyboard
    // (a typed letter selects an option starting with it; `change` fires only when that moves the selection)
    // optional: `modifiers` (1 alt, 2 ctrl, 4 meta, 8 shift), `vk` (windowsVirtualKeyCode) and `text`, so Enter and Shift+Enter can be pressed
    const extra = { ...(s.modifiers ? { modifiers: s.modifiers } : {}), ...(s.vk ? { windowsVirtualKeyCode: s.vk, nativeVirtualKeyCode: s.vk } : {}) };
    const text = s.text !== undefined ? s.text : (s.key.length === 1 ? s.key : undefined);
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: s.key, ...extra, ...(text !== undefined ? { text } : {}) });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: s.key, ...extra });
    await sleep(200);
  } else if (s.tap !== undefined) {
    // `before` runs once ahead of the press (a stub to put in place), and its value is not collected
    if (s.before !== undefined) {
      const b = await evaluateOutcome(s.before);
      if ("error" in b) {
        console.error(`step ${steps.indexOf(s)}: tap's before failed: ${b.error}: ${s.before}`);
        await shotOnTimeout(steps.indexOf(s));
        done(1); await sleep(10000);
      }
    }
    // where a person's finger lands: the element's centre, once it is drawn, enabled and not covered by another node in its
    // own document; an element inside a same-origin frame is pressed at its point in the top page (the frames' offsets added)
    const at = `(() => { const e = (${s.tap}\n); if (!e || e.disabled || !e.getBoundingClientRect) return null; `
      + `e.scrollIntoView({ block: "center", inline: "center" }); `
      + `for (let w = e.ownerDocument.defaultView; w && w !== window && w.frameElement; w = w.parent) w.frameElement.scrollIntoView({ block: "center", inline: "center" }); `
      + `const r = e.getBoundingClientRect(); `
      + `if (!r.width || !r.height) return null; let x = r.left + r.width / 2, y = r.top + r.height / 2; `
      + `const h = e.ownerDocument.elementFromPoint(x, y); if (!(h && (h === e || e.contains(h)))) return null; `
      + `for (let w = e.ownerDocument.defaultView; w && w !== window; w = w.parent) { const f = w.frameElement; `
      + `if (!f) return null; const fr = f.getBoundingClientRect(); x += fr.left + f.clientLeft; y += fr.top + f.clientTop; } `
      + `return [x, y]; })()`;
    let xy = null, lastError = null;
    for (;;) {
      const o = await evaluateOutcome(at, Math.min(Math.max(deadline - Date.now(), 250), 3000));
      if ("error" in o) { lastError = o.error; xy = null; } else xy = o.value;
      if (xy || Date.now() >= deadline) break;
      await sleep(100);
    }
    if (!xy) {
      console.error(`step ${steps.indexOf(s)}: tap found no element to press within ${waitMs}ms: ${s.tap}` +
        (lastError ? ` (last error: ${lastError})` : ""));
      await shotOnTimeout(steps.indexOf(s));
      done(1); await sleep(10000);
    }
    const [x, y] = xy;
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    // `times`: that many presses at the one point, back to back (a double tap on a control the first press may disable)
    for (let i = 0; i < (s.times || 1); i++) {
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
    }
    // `after` runs once right after the presses (a confirm the press drew), and its value is not collected
    if (s.after !== undefined) {
      const a = await evaluateOutcome(s.after);
      if ("error" in a) {
        console.error(`step ${steps.indexOf(s)}: tap's after failed: ${a.error}: ${s.after}`);
        await shotOnTimeout(steps.indexOf(s));
        done(1); await sleep(10000);
      }
    }
    values.push(1);
  } else if (s.sw !== undefined) {
    // the service worker of the page's origin: a target of its own, listed by the browser at /json beside the pages
    const origin = await evaluate("location.origin");
    // discovery and the connection retry together until the step's deadline: the browser can stop an idle worker between listing it and
    // the socket opening, and a listed target that is gone is not the end of the step while time remains
    let sws = null; let lastWhy = "no running service worker listed";
    while (!sws) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${sharedPort || port}/json`, { signal: AbortSignal.timeout(3000) })).json();
        const target = list.find((x) => x.type === "service_worker" && String(x.url).startsWith(origin + "/") && /\/sw\.js$/.test(x.url) && x.webSocketDebuggerUrl) || null;
        if (target) sws = await within(openSocket(target.webSocketDebuggerUrl, "the service worker"), Math.min(Math.max(deadline - Date.now(), 1000), 15000), "the service worker websocket");
      } catch (err) { lastWhy = String(err && err.message || err); }
      if (sws || Date.now() >= deadline) break;
      await sleep(150);
    }
    if (!sws) {
      console.error(`step ${steps.indexOf(s)}: sw found no reachable service worker for ${origin} within ${waitMs}ms (${lastWhy})`);
      await shotOnTimeout(steps.indexOf(s));
      done(1); await sleep(10000);
    }
    try {
      const answer = await new Promise((res, rej) => {
        // a fresh budget for the answer (never the sliver the discovery left), and a socket that closes or errors mid-evaluate fails the step at once
        const timer = setTimeout(() => rej(new Error(`no answer from the service worker within ${waitMs}ms`)), Math.max(deadline - Date.now(), 5000));
        const gone = () => { clearTimeout(timer); rej(new Error("service worker closed")); };
        sws.addEventListener("close", gone);
        sws.addEventListener("error", gone);
        sws.addEventListener("message", (m) => { const d = JSON.parse(m.data); if (d.id === 1) { clearTimeout(timer); res(d); } });
        sws.send(JSON.stringify({ id: 1, method: "Runtime.evaluate", params: { expression: s.sw, awaitPromise: true, returnByValue: true } }));
      });
      if (answer.error || answer.result?.exceptionDetails) {
        console.error(`step ${steps.indexOf(s)}: sw evaluation failed: ` + JSON.stringify(answer.error || answer.result.exceptionDetails.exception?.description || answer.result.exceptionDetails.text) + `: ${s.sw}`);
        done(1); await sleep(10000);
      }
      values.push(answer.result?.result?.value ?? null);
    } finally { try { sws.close(); } catch {} }
  } else if (s.focus !== undefined) {
    await send("Emulation.setFocusEmulationEnabled", { enabled: !!s.focus });         // a headless page is never the focused window: without this no focusin / focusout fires
  } else if (s.motion !== undefined) {
    media["prefers-reduced-motion"] = s.motion;
    await send("Emulation.setEmulatedMedia", { features: Object.entries(media).map(([name, value]) => ({ name, value })) });
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
  } else if (s.do !== undefined) {
    // act until: the action is repeated with its assertion (see the header); one poll slice bounds an action that never answers
    const every = s.every || 1500, n = steps.indexOf(s);
    const slice = () => Math.min(Math.max(deadline - Date.now(), 250), 3000);
    let v = null, lastError = null, lastDoError = null, nextDo = 0, ran = 0;
    const check = async () => {
      if (s.posts !== undefined) return posts.length >= s.posts;
      const o = await evaluateOutcome(`(() => { try { return (${s.until}); } catch (e) { return null; } })()`, slice());
      if ("error" in o) { lastError = o.error; return null; }
      return o.value;
    };
    // an `until` re-run checks its condition and acts in ONE page evaluation, so nothing can come true between the check and
    // the action; a `posts` re-run checks the driver's own POST count right before it acts
    // The action's source is inlined, never passed to an in-page eval: a page CSP without 'unsafe-eval' blocks
    // eval once the evaluation has awaited anything, and the condition is awaited (a promise condition is read for its value,
    // as `check` reads it). An expression `do` is awaited; a statement list runs as statements in its own block (its
    // completion value is not awaited). EVERY run, the first included, goes through this one wrapper, so a re-run never
    // scopes or awaits the action differently from the first run; only the re-run checks the condition first.
    // The result carries the driver's own key, so an action that happens to return {done: true} is never read as a result.
    const act = isExpression(s.do) ? `await (${s.do}\n);` : `{ ${s.do}\n; }`;
    const cond = s.posts !== undefined ? "" :
      `try { ${GUARD_KEY}c = await (${s.until}\n); } catch (e) { ${GUARD_KEY}c = null; } ` +
      `if (${GUARD_KEY}c) return { ${GUARD_KEY}: true, v: ${GUARD_KEY}c }; `;
    const wrap = (checkFirst) =>
      `(async () => { let ${GUARD_KEY}c = null; ${checkFirst ? cond : ""}${act} return { ${GUARD_KEY}: false }; })()`;
    const firstRun = wrap(false), reRun = wrap(true);
    for (;;) {
      if (Date.now() >= nextDo) {
        if (ran > 0 && Date.now() >= deadline) break;
        if (ran > 0 && s.posts !== undefined && posts.length >= s.posts) { v = true; break; }
        const a = await evaluateOutcome(ran > 0 ? reRun : firstRun, slice());
        if (!("error" in a) && a.value && a.value[GUARD_KEY] === true) { v = a.value.v; break; }
        ran++; if ("error" in a) lastDoError = a.error;
        nextDo = Date.now() + every;
      }
      v = await check();
      if (v || Date.now() >= deadline) break;
      await sleep(100);
    }
    if (!v && !s.may_timeout) {
      console.error(`step ${n}: ${s.posts !== undefined ? `posts never reached ${s.posts}` : `until never came true`} within ${waitMs}ms ` +
        `after the action ran ${ran} time(s)` + (s.posts !== undefined ? ` (saw ${posts.length})` : `: ${s.until}`) +
        (lastDoError ? ` (last error from the action: ${lastDoError})` : "") + (lastError ? ` (last error: ${lastError})` : ""));
      await shotOnTimeout(n);
      done(1); await sleep(10000);
    }
    if (s.posts === undefined) values.push(v || { timeout: s.until });
  } else if (s.posts !== undefined) {
    while (posts.length < s.posts && Date.now() < deadline) await sleep(100);
    if (posts.length < s.posts && !s.may_timeout) {
      console.error(`step ${steps.indexOf(s)}: posts never reached ${s.posts} within ${waitMs}ms (saw ${posts.length})`);
      await shotOnTimeout(steps.indexOf(s));
      done(1); await sleep(10000);
    }
  } else if (s.until !== undefined) {
    let v = null, lastError = null;
    for (;;) {
      // an error outcome never satisfies the wait (a context destroyed by a navigation is transient: keep polling)
      // each poll gets a slice of the deadline, not all of it: an evaluate sent while the page navigates can go unanswered
      // for good (CI run 40994737528: one such call held the whole 20 s), and the next poll finds the new document
      // (so a promise-returning `until` must settle inside one slice, 3 s; the longest in tests/ is 1.5 s)
      const o = await evaluateOutcome(`(() => { try { return (${s.until}); } catch (e) { return null; } })()`, Math.min(Math.max(deadline - Date.now(), 250), 3000));
      if ("error" in o) { lastError = o.error; v = null; } else v = o.value;
      if (v || Date.now() >= deadline) break;
      await sleep(100);
    }
    if (!v && !s.may_timeout) {
      console.error(`step ${steps.indexOf(s)}: until never came true within ${waitMs}ms: ${s.until}` +
        (lastError ? ` (last error: ${lastError})` : ""));
      await shotOnTimeout(steps.indexOf(s));
      done(1); await sleep(10000);
    }
    values.push(v || { timeout: s.until });
  } else {
    if (RELOADS_PAGE.test(String(s.eval))) await awaitWorkerSettled();
    const before = navStarts;       // after the wait: a navigation that began during it is not this eval's
    const o = await evaluateOutcome(s.eval);
    if ("error" in o && isNavigationError(o.error)) {
      // a navigation-class error counts as "the eval navigated" only when the page really began a navigation during it
      for (const end = Date.now() + 1000; navStarts === before && Date.now() < end;) await sleep(50);
      if (navStarts !== before) { values.push({ navigated: true }); continue; }
    }
    if ("error" in o && !s.may_error) {
      console.error(`step ${steps.indexOf(s)}: eval failed: ${o.error}: ${s.eval}`);
      await shotOnTimeout(steps.indexOf(s));
      done(1); await sleep(10000);
    }
    values.push("error" in o ? { error: o.error } : o.value);
  }
}
process.stdout.write(JSON.stringify({ values, posts, dialogs }));
ws.close(); done(0);
