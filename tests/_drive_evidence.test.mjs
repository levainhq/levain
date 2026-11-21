// Node tests of the driver's passive evidence record (tests/_drive_evidence.mjs) against a stub CDP event stream on a virtual clock: no Chrome,
// no real time. By hand: node --test tests/_drive_evidence.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { createEvidence, renderEvidence, LOAD_ERRORS_SCRIPT, SHELL_CACHE_PROBE, RING_MAX, EVIDENCE_MAX, DIALOG_MAX } from "./_drive_evidence.mjs";
import * as mod from "./_drive_evidence.mjs";   // the names added with the L3 fixes are read off the namespace, so a missing one fails its own test
const END_MARK = "--- end drive evidence ---";

const rig = () => {
  let T = 0;
  const ev = createEvidence({ now: () => T, t0: 0 });
  return { ev, at: (t) => { T = t; }, feed: (t, d) => { T = t; ev.onMessage(d); } };
};
const req = (requestId, type, url) => ({ method: "Network.requestWillBeSent", params: { requestId, type, request: { url, method: "GET" } } });

test("dialogs: when each opened and closed, and what Chrome answered to the reply the driver sent", () => {
  const r = rig();
  r.feed(100, { method: "Page.javascriptDialogOpening", params: { type: "prompt", message: "Write token" } });
  r.ev.dialogReplySent(7);
  r.feed(103, { id: 7, result: {} });
  r.feed(104, { method: "Page.javascriptDialogClosed", params: { result: false, userInput: "" } });
  r.feed(900, { method: "Page.javascriptDialogOpening", params: { type: "alert", message: "second" } });
  r.ev.dialogReplySent(8);
  r.feed(905, { id: 8, error: { code: -32000, message: "No dialog is showing" } });
  const text = renderEvidence(r.ev);
  assert.match(text, /\+100ms prompt "Write token" closed \+104ms accepted=false; handleJavaScriptDialog sent \+100ms, reply ok after 3ms/);
  assert.match(text, /\+900ms alert "second" closed never; handleJavaScriptDialog sent \+900ms, reply error No dialog is showing after 5ms/);
});

test("requests: a script that failed to load is in the ring with its errorText, beside the document and the stylesheet", () => {
  const r = rig();
  r.feed(10, req("1", "Document", "http://127.0.0.1:5000/"));
  r.feed(20, { method: "Network.responseReceived", params: { requestId: "1", type: "Document", response: { url: "http://127.0.0.1:5000/", status: 200, fromServiceWorker: true } } });
  r.feed(30, req("2", "Script", "http://127.0.0.1:5000/static/cockpit_view.js?v=abc123"));
  r.feed(35, { method: "Network.loadingFailed", params: { requestId: "2", type: "Script", errorText: "net::ERR_CONNECTION_RESET", canceled: false } });
  r.feed(40, req("3", "Stylesheet", "http://127.0.0.1:5000/static/dashboard.css"));
  r.feed(45, { method: "Network.responseReceived", params: { requestId: "3", type: "Stylesheet", response: { url: "http://127.0.0.1:5000/static/dashboard.css", status: 404 } } });
  r.feed(50, req("4", "Image", "http://127.0.0.1:5000/pixel.png"));                                  // not a kind the ring takes
  r.feed(55, { method: "Network.loadingFailed", params: { requestId: "5", type: "Fetch", errorText: "net::ERR_ABORTED", canceled: true } });   // a failed load of any kind is kept
  const text = renderEvidence(r.ev);
  assert.match(text, /\+10ms Document \/ 200 via-sw/);
  assert.match(text, /\+30ms Script \/static\/cockpit_view\.js\?v=abc123 FAILED net::ERR_CONNECTION_RESET/);
  assert.match(text, /Stylesheet \/static\/dashboard\.css 404/);
  assert.doesNotMatch(text, /pixel\.png/);
  assert.match(text, /Fetch\s+FAILED net::ERR_ABORTED canceled/);
});

test("a reload's canceled loads never push a real failure out of the failed ring", () => {
  const r = rig();
  r.feed(10, req("real", "Script", "http://127.0.0.1:5000/static/cockpit_view.js?v=abc123"));
  r.feed(20, { method: "Network.loadingFailed", params: { requestId: "real", type: "Script", errorText: "net::ERR_CONNECTION_RESET", canceled: false } });
  for (let i = 0; i < 60; i++) {
    r.feed(100 + i, req("c" + i, "Script", `http://127.0.0.1:5000/static/old${i}.js`));
    r.feed(200 + i, { method: "Network.loadingFailed", params: { requestId: "c" + i, type: "Script", errorText: "net::ERR_ABORTED", canceled: true } });
  }
  assert.ok(r.ev.failed.has("real"), "the genuine failure is still in the failed ring after 60 canceled loads");
  assert.equal(r.ev.failed.size, 1, "a canceled load is not kept in the failed ring");
  assert.match(renderEvidence(r.ev), /cockpit_view\.js\?v=abc123 FAILED net::ERR_CONNECTION_RESET/);
});

test("a canceled load that was blocked (a CSP or mixed-content block) is a real failure and is kept", () => {
  const r = rig();
  r.feed(10, req("b", "Script", "http://127.0.0.1:5000/static/cockpit_view.js?v=abc123"));
  r.feed(20, { method: "Network.loadingFailed", params: { requestId: "b", type: "Script", errorText: "net::ERR_BLOCKED_BY_CSP", canceled: true, blockedReason: "csp" } });
  assert.ok(r.ev.failed.has("b"), "a blocked load enters the failed ring even when Chrome also marks it canceled");
});

test("the shell-cache probe counts every name the markup holds, however many, and checks the scripts first", async () => {
  const css = Array.from({ length: 450 }, (_, i) => `<link href="/static/s${i}.css?v=1">`).join("");
  const enc = new TextEncoder().encode(css + `<script src="/static/late.js?v=1"></script>`);
  const cache = { match: async (k) => (k === "/" ? { status: 200, body: { getReader: () => { let done = false; return { read: async () => (done ? { done: true } : ((done = true), { done: false, value: enc })), cancel: async () => {} }; } } } : undefined) };
  globalThis.caches = { keys: async () => ["levain-shell-v1"], open: async () => cache };
  try {
    const c = JSON.parse(await (0, eval)(mod.SHELL_CACHE_PROBE)).shell[0];
    assert.equal(c.total, 451, "the count is every name, not a cap");
    assert.equal(c.entries[0][0].replace(/\?.*/, ""), "/static/late.js", "a script after hundreds of stylesheets is still checked first");
  } finally { delete globalThis.caches; }
});

test("the shell-cache probe checks the scripts first and reports how many names it saw against how many it checked", async () => {
  const css = Array.from({ length: 20 }, (_, i) => `<link href="/static/s${i}.css?v=1">`).join("");
  const js = Array.from({ length: 3 }, (_, i) => `<script src="/static/j${i}.js?v=1"></script>`).join("");
  const html = css + js;
  const enc = new TextEncoder().encode(html);
  const entry = { status: 200, headers: { get: () => "5" } };
  const cache = { match: async (k) => (k === "/" ? { status: 200, body: { getReader: () => { let done = false; return { read: async () => (done ? { done: true } : ((done = true), { done: false, value: enc })), cancel: async () => {} }; } } } : entry) };
  globalThis.caches = { keys: async () => ["levain-shell-v1"], open: async () => cache };
  try {
    const out = JSON.parse(await (0, eval)(mod.SHELL_CACHE_PROBE));
    const c = out.shell[0];
    assert.equal(c.total, 23, "every name the markup holds is counted");
    assert.equal(c.entries.length, mod.SHELL_NAMES_MAX, "the checks stay bounded");
    assert.deepEqual(c.entries.slice(0, 3).map((e) => e[0].replace(/\?.*/, "")), ["/static/j0.js", "/static/j1.js", "/static/j2.js"], "scripts are checked before stylesheets");
  } finally { delete globalThis.caches; }
});

test("service worker status events are kept with their running status and status", () => {
  const r = rig();
  r.feed(300, { method: "ServiceWorker.workerVersionUpdated", params: { versions: [{ versionId: "12", runningStatus: "starting", status: "installing", scriptURL: "http://127.0.0.1:5000/sw.js" }] } });
  r.feed(450, { method: "ServiceWorker.workerVersionUpdated", params: { versions: [{ versionId: "12", runningStatus: "running", status: "activated", scriptURL: "http://127.0.0.1:5000/sw.js" }] } });
  const text = renderEvidence(r.ev);
  assert.match(text, /\+300ms version 12 starting\/installing \/sw\.js/);
  assert.match(text, /\+450ms version 12 running\/activated \/sw\.js/);
});

test("the resend count starts at 0 and counts every firing", () => {
  const r = rig();
  assert.match(renderEvidence(r.ev), /^resend fired: 0$/m);
  r.ev.resendFired(); r.ev.resendFired();
  assert.match(renderEvidence(r.ev), /^resend fired: 2$/m);
});

test("what the driver reads at the timeout is printed: every target, window.__loadErrors, the shell cache entries", () => {
  const r = rig();
  const text = renderEvidence(r.ev, {
    targets: { source: "Target.getTargets", list: [{ type: "page", url: "", title: "Levain · substrate control", attached: true }, { type: "service_worker", url: "http://127.0.0.1:5000/sw.js", title: "", attached: false }] },
    page: { loadErrors: ["SCRIPT http://127.0.0.1:5000/static/cockpit_view.js?v=abc123"] },
    shell: { caches: 1, shell: [{ cache: "levain-shell-v9", root: 200, entries: [["/static/cockpit_view.js?v=abc123", 200, 91234], ["/static/dashboard.css?v=def", "MISSING"]] }] },
  });
  assert.match(text, /TARGETS \(Target\.getTargets; 2 listed\)/);
  assert.match(text, /page attached=true url="" title="Levain · substrate control"/);
  assert.match(text, /service_worker attached=false url="http:\/\/127\.0\.0\.1:5000\/sw\.js"/);
  assert.match(text, /window\.__loadErrors: \["SCRIPT http:\/\/127\.0\.0\.1:5000\/static\/cockpit_view\.js\?v=abc123"\]/);
  assert.match(text, /levain-shell-v9 '\/' status=200 names 2 of 2/);
  assert.match(text, /\/static\/cockpit_view\.js\?v=abc123 200 91234B/);
  assert.match(text, /\/static\/dashboard\.css\?v=def MISSING/);
  assert.match(renderEvidence(r.ev, { page: { error: "no answer from the page within 2000ms" } }), /window\.__loadErrors: page did not answer: no answer/);
  assert.match(renderEvidence(r.ev, { shell: { note: "no service worker target listed" } }), /SHELL CACHE: no service worker target listed/);
});

test("the block is bounded under a flood, and the ring keeps the newest requests", () => {
  const r = rig();
  const huge = "x".repeat(5000);
  for (let i = 0; i < 5000; i++) {
    r.feed(i, req(String(i), "Script", `http://h/${i}/${huge}`));
    r.feed(i, { method: "Network.loadingFailed", params: { requestId: String(i), type: "Script", errorText: huge } });
  }
  for (let i = 0; i < 100; i++) {
    r.feed(i, { method: "Page.javascriptDialogOpening", params: { type: "prompt", message: huge } });
    r.ev.dialogReplySent(100000 + i);
  }
  for (let i = 0; i < 100; i++) r.feed(i, { method: "ServiceWorker.workerVersionUpdated", params: { versions: [{ versionId: huge, runningStatus: huge, status: huge, scriptURL: huge }] } });
  const targets = { source: "Target.getTargets", list: Array.from({ length: 200 }, (_, i) => ({ type: "page", url: huge + i, title: huge, attached: true })) };
  const shell = { caches: 3, shell: Array.from({ length: 9 }, () => ({ cache: huge, root: 200, entries: Array.from({ length: 100 }, (_, i) => [huge + i, 200, 1]) })) };
  const text = renderEvidence(r.ev, { targets, shell, page: { loadErrors: Array.from({ length: 1000 }, () => huge) } });
  assert.ok(text.length <= EVIDENCE_MAX, `the block is ${text.length} characters`);
  assert.equal(r.ev.ring.size, RING_MAX);
  assert.equal(r.ev.dialogs.length, DIALOG_MAX);
  assert.ok(text.includes("/4999/"), "the newest request is kept");
  assert.ok(!text.includes("/0/xxx"), "the oldest request is dropped");
  assert.ok(text.startsWith("--- drive evidence"));
  assert.ok(text.endsWith(END_MARK), "the end marker survives the cut");
});

test("a malformed event never throws and never loses the others", () => {
  const r = rig();
  assert.doesNotThrow(() => { r.ev.onMessage({}); r.ev.onMessage({ method: "Network.loadingFailed" }); r.ev.onMessage({ method: "ServiceWorker.workerVersionUpdated", params: { versions: null } }); });
  r.feed(5, req("1", "Script", "http://h/a.js"));
  assert.match(renderEvidence(r.ev), /Script \/a\.js no response/);
});

test("the load-error listener is a capture-phase one, records script and link failures only, and is capped", () => {
  const seen = [];
  const win = {};
  vm.runInNewContext(LOAD_ERRORS_SCRIPT, { window: win, addEventListener: (type, fn, capture) => seen.push({ type, fn, capture }) });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].type, "error");
  assert.equal(seen[0].capture, true);
  seen[0].fn({ target: { tagName: "SCRIPT", src: "http://h/static/cockpit_view.js?v=abc" } });
  seen[0].fn({ target: { tagName: "LINK", href: "http://h/static/dashboard.css" } });
  seen[0].fn({ target: { tagName: "IMG", src: "http://h/x.png" } });
  seen[0].fn({ message: "a script error with no target element" });
  assert.deepEqual([...win.__loadErrors], ["SCRIPT http://h/static/cockpit_view.js?v=abc", "LINK http://h/static/dashboard.css"]);
  for (let i = 0; i < 50; i++) seen[0].fn({ target: { tagName: "SCRIPT", src: "http://h/" + "y".repeat(500) } });
  assert.equal(win.__loadErrors.length, 10);
  assert.ok(win.__loadErrors.every((s) => s.length <= 160));
});

// a cached Response stand-in that records every way a body could be read; `length` is what its content-length header says (null: no header)
const touched = [];
const headerOnly = (status, length) => ({
  status,
  headers: { get: (h) => (String(h).toLowerCase() === "content-length" ? length : null) },
  get body() { touched.push("body"); return null; },
  arrayBuffer: async () => { touched.push("arrayBuffer"); return new ArrayBuffer(1); },
  blob: async () => { touched.push("blob"); return { size: 1 }; },
  text: async () => { touched.push("text"); return ""; },
  clone: () => { touched.push("clone"); return headerOnly(status, length); },
});
// the cached '/' markup as a body stream: `first` then endless padding, counting reads and whether the reader was cancelled
const markup = (first, chunk = 1024) => {
  const log = { reads: 0, cancelled: false, bytes: 0 };
  const enc = new TextEncoder(); let sent = false;
  const body = { getReader: () => ({
    read: async () => { log.reads++; const v = sent ? new Uint8Array(chunk).fill(32) : (sent = true, enc.encode(first)); log.bytes += v.byteLength; return { done: false, value: v }; },
    cancel: async () => { log.cancelled = true; },
  }) };
  return { log, res: { status: 200, headers: { get: () => null }, body } };
};

test("the shell-cache probe lists status and the header's size for each asset the cached '/' names, reads no asset body, and says MISSING for one the cache lacks", async () => {
  touched.length = 0;
  const long = "/static/" + "q".repeat(1000) + ".js";
  const html = '<link rel="stylesheet" href="/static/dashboard.css?v=aaa"><script src="/static/cockpit_view.js?v=bbb"></script><script src="/static/boot.js?v=ccc"></script><script src="/static/nolength.js"></script><script src="' + long + '"></script><a href="/other">x</a>';
  const root = markup(html);
  const store = new Map([["/", root.res], ["/static/cockpit_view.js?v=bbb", headerOnly(200, "321")], ["/static/dashboard.css?v=aaa", headerOnly(200, "77")], ["/static/nolength.js", headerOnly(200, null)]]);
  const caches = { keys: async () => ["levain-open-v1", "levain-shell-v9"], open: async (n) => { assert.equal(n, "levain-shell-v9"); return { match: async (k) => store.get(k) }; } };
  const out = JSON.parse(await vm.runInNewContext(SHELL_CACHE_PROBE, { caches, TextDecoder }));
  assert.equal(out.caches, 1);
  const e = out.shell[0].entries;
  assert.deepEqual(e.slice(0, 3), [["/static/cockpit_view.js?v=bbb", 200, 321], ["/static/boot.js?v=ccc", "MISSING"], ["/static/nolength.js", 200, null]]);   // scripts first, in markup order
  assert.ok(e.some((x) => x[0] === "/static/dashboard.css?v=aaa" && x[1] === 200 && x[2] === 77), "the stylesheet is still checked, after the scripts");
  assert.equal(e[3][1], "MISSING");
  assert.ok(e[3][0].length <= 120, `a name is clipped: ${e[3][0].length}`);
  assert.equal(out.shell[0].root, 200);
  assert.deepEqual(touched, [], "no asset body is touched (no body, arrayBuffer, blob, text or clone)");
  assert.ok(root.log.cancelled, "the markup reader is cancelled once its cap is reached");
  assert.ok(root.log.bytes <= mod.SHELL_HTML_BYTES + 2048, `the markup read stops near its cap: ${root.log.bytes} bytes`);
});

test("the shell-cache probe looks at the NEWEST shell caches when there are more than three", async () => {
  const opened = [];
  const caches = { keys: async () => ["levain-shell-v1", "other", "levain-shell-v2", "levain-shell-v3", "levain-shell-v4"], open: async (n) => { opened.push(n); return { match: async () => undefined }; } };
  const out = JSON.parse(await vm.runInNewContext(SHELL_CACHE_PROBE, { caches, TextDecoder }));
  assert.deepEqual(opened, ["levain-shell-v2", "levain-shell-v3", "levain-shell-v4"]);
  assert.equal(out.caches, 3);
});

test("the shell-cache section has a budget of its own: at most about sixty lines, and the end marker survives", () => {
  const r = rig();
  const huge = "z".repeat(3000);
  const shell = { caches: 3, shell: Array.from({ length: 3 }, (_, k) => ({ cache: huge + k, root: 200, entries: Array.from({ length: 40 }, (_, i) => [huge + i, 200, 1]) })) };
  const text = renderEvidence(r.ev, { shell });
  const section = text.slice(text.indexOf("SHELL CACHE"), text.indexOf(END_MARK)).split("\n").filter(Boolean);
  assert.ok(section.length <= 62, `the section is ${section.length} lines`);
  assert.ok(text.endsWith(END_MARK));
  assert.match(renderEvidence(r.ev, { shell: { caches: 1, shell: [{ cache: "levain-shell-v9", root: 200, entries: [["/a.js", 200, null], ["/b.js", 200, 12]] }] } }), /\/a\.js 200 size not read\n\s+\/b\.js 200 12B/);
});

test("the evidence reads are collected under one deadline: a part that hangs is marked, the finished parts are kept, and nothing waits past the deadline", async () => {
  const never = new Promise(() => {});
  const t0 = Date.now();
  const extra = await mod.collectExtra({ targets: Promise.resolve({ source: "/json", list: [] }), page: Promise.resolve({ loadErrors: null }), shell: never }, 60);
  assert.ok(Date.now() - t0 < 1500, "returned near the deadline, not at the hung part");
  assert.deepEqual(extra.targets, { source: "/json", list: [] });
  assert.deepEqual(extra.page, { loadErrors: null });
  assert.match(extra.shell.note, /not collected in time/);
  const e2 = await mod.collectExtra({ targets: never, page: never, shell: Promise.reject(new Error("boom")) }, 40);
  assert.match(e2.targets.error, /not collected in time/);
  assert.match(e2.page.error, /not collected in time/);
  assert.match(e2.shell.note, /boom/);
  const t1 = Date.now();
  const quick = await mod.collectExtra({ targets: Promise.resolve({ source: "x", list: [] }), page: Promise.resolve({}), shell: Promise.resolve({ caches: 0, shell: [] }) }, 5000);
  assert.ok(Date.now() - t1 < 2500, "all parts done: no wait for the deadline");
  assert.equal(quick.shell.caches, 0);
});

test("a malformed websocket frame is ignored, never thrown", () => {
  assert.equal(mod.parseFrame("{not json"), null);
  assert.equal(mod.parseFrame(""), null);
  assert.equal(mod.parseFrame("null"), null);
  assert.equal(mod.parseFrame('"a string"'), null);
  assert.deepEqual(mod.parseFrame('{"id":1,"result":{}}'), { id: 1, result: {} });
});

test("a failed load survives a hundred later successes, and is printed ahead of the ordinary requests", () => {
  const r = rig();
  r.feed(1, req("bad", "Script", "http://h/static/cockpit_view.js?v=abc"));
  r.feed(2, { method: "Network.loadingFailed", params: { requestId: "bad", type: "Script", errorText: "net::ERR_CONNECTION_RESET" } });
  r.feed(3, req("gone", "Document", "http://h/"));
  r.feed(4, { method: "Network.responseReceived", params: { requestId: "gone", type: "Document", response: { url: "http://h/", status: 503 } } });
  for (let i = 0; i < 100; i++) {
    r.feed(10 + i, req("ok" + i, "Script", `http://h/ok${i}.js`));
    r.feed(10 + i, { method: "Network.responseReceived", params: { requestId: "ok" + i, type: "Script", response: { url: `http://h/ok${i}.js`, status: 200 } } });
  }
  assert.ok(!r.ev.ring.has("bad"), "the ordinary ring evicted it");
  const text = renderEvidence(r.ev);
  assert.match(text, /Script \/static\/cockpit_view\.js\?v=abc FAILED net::ERR_CONNECTION_RESET/);
  assert.match(text, /Document \/ 503/);
  assert.ok(text.indexOf("cockpit_view.js") < text.indexOf("/ok99.js"), "failed loads come first");
  assert.equal(text.split("cockpit_view.js").length - 1, 1, "an entry is printed once");
  for (let i = 0; i < 60; i++) r.feed(200 + i, { method: "Network.loadingFailed", params: { requestId: "f" + i, type: "Script", errorText: "e" } });
  assert.equal(r.ev.failed.size, mod.FAILED_MAX);
});

test("service-worker events are not the page speaking; page events and replies-to-commands are judged as before", () => {
  assert.equal(mod.pageSpoke({ method: "ServiceWorker.workerVersionUpdated", params: {} }), false);
  assert.equal(mod.pageSpoke({ method: "ServiceWorker.workerErrorReported", params: {} }), false);
  assert.equal(mod.pageSpoke({ method: "Network.requestWillBeSent", params: {} }), true);
  assert.equal(mod.pageSpoke({ method: "Page.frameNavigated", params: {} }), true);
  assert.equal(mod.pageSpoke({ id: 4, result: {} }), false);
  assert.equal(mod.pageSpoke(null), false);
});
