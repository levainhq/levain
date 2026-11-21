// Node tests of the driver's passive evidence record (tests/_drive_evidence.mjs) against a stub CDP event stream on a virtual clock: no Chrome,
// no real time. By hand: node --test tests/_drive_evidence.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { createEvidence, renderEvidence, LOAD_ERRORS_SCRIPT, SHELL_CACHE_PROBE, RING_MAX, EVIDENCE_MAX, DIALOG_MAX } from "./_drive_evidence.mjs";

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
  assert.match(text, /levain-shell-v9 '\/' status=200 names 2/);
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

test("the shell-cache probe lists status and byte length for each asset the cached '/' names, and says MISSING for one the cache lacks", async () => {
  const html = '<link rel="stylesheet" href="/static/dashboard.css?v=aaa"><script src="/static/cockpit_view.js?v=bbb"></script><script src="/static/boot.js?v=ccc"></script><a href="/other">x</a>';
  const store = new Map([["/", { status: 200, text: async () => html }], ["/static/cockpit_view.js?v=bbb", { status: 200, arrayBuffer: async () => new ArrayBuffer(321) }],
    ["/static/dashboard.css?v=aaa", { status: 200, arrayBuffer: async () => new ArrayBuffer(77) }]]);
  const caches = { keys: async () => ["levain-open-v1", "levain-shell-v9"], open: async (n) => { assert.equal(n, "levain-shell-v9"); return { match: async (k) => store.get(k) }; } };
  const out = JSON.parse(await vm.runInNewContext(SHELL_CACHE_PROBE, { caches }));
  assert.equal(out.caches, 1);
  assert.deepEqual(out.shell[0].entries, [["/static/dashboard.css?v=aaa", 200, 77], ["/static/cockpit_view.js?v=bbb", 200, 321], ["/static/boot.js?v=ccc", "MISSING"]]);
  assert.equal(out.shell[0].root, 200);
});
