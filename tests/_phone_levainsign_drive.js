// Drives phone.js's ``window.levainSign`` (K8) in a node vm: a stub DOM, a scripted fetch and a counting WebAuthn.
// Usage: node _phone_levainsign_drive.js <phone.js>. Prints one JSON object, one key per scenario.
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(process.argv[2], "utf8");

function memStore() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); } };
}

function elem() {
  return { textContent: "", className: "", disabled: false, hidden: false, style: {}, children: [], dataset: {},
           classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {}, setAttribute() {},
           appendChild(c) { this.children.push(c); return c; }, querySelectorAll() { return []; }, remove() {},
           replaceChildren() {} };
}

// one page: ``pending`` is GET /cockpit/pending/<id>'s body, ``signed`` the POST .../signature answer's body, or a
// list of [status, body] answers taken one per POST; ``gate``, when given, is a promise each POST's answer waits on
function page(pending, signed, gate, opts) {
  opts = opts || {};
  const log = { issued: 0, webauthn: 0, reviewed: 0, posts: 0, said: [], done: [] };
  const answer = (status, body) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
  const fetch = (route, opts) => {
    if (route === "/consent.json") return answer(503, { message: "no queue here" });
    if (/^\/cockpit\/pending\/[^/]+$/.test(route)) return answer(200, pending);
    if (/\/signature$/.test(route)) {
      log.posted = JSON.parse(opts.body);
      const [status, body] = Array.isArray(signed) ? signed[log.posts] : [200, signed];
      log.posts++;
      return Promise.resolve(gate).then(() => answer(status, body));
    }
    return answer(404, { message: "no route " + route });
  };
  const buf = (n) => new Uint8Array([n]).buffer;
  const navigator = { credentials: { get: () => {
    log.webauthn++;
    if (opts.refuse) return Promise.reject(Object.assign(new Error("cancel"), { name: opts.refuse }));
    return Promise.resolve({ response: { authenticatorData: buf(1), clientDataJSON: buf(2), signature: buf(3) } });
  } } };
  const window = { isSecureContext: true, PublicKeyCredential: function () {}, localStorage: memStore(), addEventListener() {},
                   sessionStorage: memStore() };
  // ``mine``: this browser's record of its passkeys (fingerprint -> credential id), as enrolment leaves it
  if (opts.mine) window.localStorage.setItem("levain-passkeys:localhost", JSON.stringify(opts.mine));
  // the document keeps its listeners, so a test can hide the page (visibilitychange) and count what is registered
  const listeners = new Map();
  const document = { getElementById: () => elem(), createElement: () => elem(), querySelectorAll: () => [], hidden: false,
                     addEventListener(t, f) { if (!listeners.has(t)) listeners.set(t, []); listeners.get(t).push(f); },
                     removeEventListener(t, f) { const l = listeners.get(t) || []; const i = l.indexOf(f); if (i >= 0) l.splice(i, 1); } };
  const visibility = (hidden) => { document.hidden = hidden; (listeners.get("visibilitychange") || []).slice().forEach((f) => f()); };
  const vcount = () => (listeners.get("visibilitychange") || []).length;
  const ctx = { window, navigator, fetch, document,
                location: { hash: "", hostname: "localhost", pathname: "/" }, history: { replaceState() {} },
                setInterval: () => 0, btoa, atob };
  window.window = window;
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const say = (text, kind) => { log.said.push([text, kind || ""]); };
  const issue = () => { log.issued++; return Promise.resolve({ ok: true, status: 200,
                                                               body: { state: "WAITING", pending_id: "cp" + "0".repeat(32) } }); };
  // probe(): does a second write get filed right now? It files only when the page is not held (busy is 0); its own
  // review dismisses at once, so a probe that got in leaves the page as it found it
  const probe = () => {
    const before = log.issued;
    window.levainSign(["SHA256:a"], say, elem(), issue, (_b, _s, dismiss) => dismiss(), () => {});
    return tick().then(() => log.issued > before);
  };
  const mapping = () => JSON.parse(window.localStorage.getItem("levain-passkeys:localhost") || "{}");
  return { window, log, say, issue, probe, visibility, vcount, mapping };
}

const tick = () => new Promise((r) => setTimeout(r, 30));
const WAITING = { state: "WAITING", verb: "spore_update", tier: "C1", challenge: "AAAA", rp_id: "localhost" };

async function main() {
  const out = {};

  // B6-B: no review sheet, no signature: nothing is filed and no passkey prompt is asked
  {
    const p = page(WAITING, { state: "COMMITTED" });
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, undefined, (a) => p.log.done.push(a));
    await tick();
    out.no_review = p.log;
  }
  // L-1: the stored pending is no longer WAITING (expired, or decided elsewhere): no review, no passkey prompt
  {
    const p = page({ state: "EXPIRED", verb: "spore_update", tier: "C1", rp_id: "localhost" }, { state: "EXPIRED" });
    const review = (_body, sign) => { p.log.reviewed++; sign(); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    out.not_waiting = p.log;
  }
  // L-1: the outcome names the signer the server persisted, never the fingerprint this page submitted
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:persisted" });
    const review = (_body, sign) => { p.log.reviewed++; sign(); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    out.signed = p.log;
  }
  // K-5: a double tap on Sign runs one ceremony and tells one outcome
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    const review = (_body, sign) => { p.log.reviewed++; sign(); sign(); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    out.double_tap = p.log;
  }
  // K-6: a failure after the signature was sent (here the caller's done throws) says the result is unknown, never
  // "not applied", and leaves no unhandled rejection
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    const review = (_body, sign) => { p.log.reviewed++; sign(); };
    let first = true;
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => {
      p.log.done.push(a);
      if (first) { first = false; throw new TypeError("caller fault"); }
    });
    await tick();
    out.after_send = p.log;
  }
  // K-7: an event-driven review (the Sign tap comes later) keeps the page held until sign settles: a second write
  // started meanwhile files nothing; once signed, the next one files
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    const held = {};
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.sign = sign; held.dismiss = dismiss; };
    const done = (a) => p.log.done.push(a);
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, done);
    await tick();
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, done);
    await tick();
    p.log.issued_while_open = p.log.issued;
    held.sign();
    await tick();
    p.log.webauthn_after_tap = p.log.webauthn;
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, done);
    await tick();
    out.async_review = p.log;
  }
  // K-7: a review dismissed without signing releases the page, with no passkey prompt
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    const held = {};
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.dismiss = dismiss; };
    const done = (a) => p.log.done.push(a);
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, done);
    await tick();
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, done);
    await tick();
    p.log.issued_while_open = p.log.issued;
    held.dismiss();
    await tick();
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, done);
    await tick();
    out.dismissed = p.log;
  }
  // one state machine per review (L3 r2): a Sign tap that comes after dismiss is a no-op, however late
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    const held = {};
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.sign = sign; held.dismiss = dismiss; };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    held.dismiss();
    await tick();
    held.sign();
    await tick();
    p.log.free_after = await p.probe();
    out.stale_sign = p.log;
  }
  // a dismiss while a sign is in flight keeps the page held until that sign settles, then releases it once
  {
    let open;
    const gate = new Promise((r) => { open = r; });
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" }, gate);
    const held = {};
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.sign = sign; held.dismiss = dismiss; };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    held.sign();
    await tick();
    held.dismiss();
    await tick();
    p.log.free_in_flight = await p.probe();
    open();
    await tick();
    p.log.free_after = await p.probe();
    p.log.free_again = await p.probe();   // a double release would leave busy below 0, which holds the page forever
    out.dismiss_in_flight = p.log;
  }
  // a 4xx re-arms Sign for a retry and the page stays held across both attempts while the sheet is open
  {
    const p = page(WAITING, [[409, { message: "stale" }], [200, { state: "COMMITTED", signer_fp: "SHA256:a" }]]);
    const held = {};
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.sign = sign; held.dismiss = dismiss; };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    held.sign();
    await tick();
    p.log.free_after_4xx = await p.probe();
    held.sign();
    await tick();
    p.log.free_after_retry = await p.probe();
    p.log.free_again = await p.probe();
    out.retry_held = p.log;
  }
  // L3 r3: a hidden page (a switched tab, a backgrounded PWA) dismisses the open review, so its hold never freezes
  // the list; the hide listener lives only as long as the review and goes once it is released
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    const base = p.vcount();
    const held = {};
    // the sheet answers review() with its closed(reason): phone.js calls it when the review ends, so Sign goes dead
    p.log.closed = [];
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.sign = sign; held.dismiss = dismiss;
                                               return (why) => p.log.closed.push(why); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    p.log.listeners_open = p.vcount() - base;
    p.visibility(true);
    await tick();
    p.log.said_at_hide = p.log.said.slice(-1)[0];   // read before a probe, whose own write says on the same line
    p.visibility(false);
    p.log.free_after_hide = await p.probe();
    p.log.listeners_after = p.vcount() - base;
    p.visibility(true);          // a later hide finds no listener of this review: nothing is released twice
    await tick();
    p.visibility(false);
    p.log.free_again = await p.probe();
    held.sign();                 // the sheet is gone: a stale tap signs nothing, and says the review was dismissed
    await tick();
    out.hidden_review = p.log;
  }
  // hidden while a sign is in flight: the existing rule holds, the page stays held until that sign settles, then
  // it is released once and the listener is gone
  {
    let open;
    const gate = new Promise((r) => { open = r; });
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" }, gate);
    const base = p.vcount();
    const held = {};
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.sign = sign; held.dismiss = dismiss; };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    held.sign();
    await tick();
    p.visibility(true);
    await tick();
    p.visibility(false);
    p.log.free_in_flight = await p.probe();
    p.log.listeners_in_flight = p.vcount() - base;
    open();
    await tick();
    p.log.free_after = await p.probe();
    p.log.listeners_after = p.vcount() - base;
    p.visibility(true);
    await tick();
    p.visibility(false);
    p.log.free_again = await p.probe();
    out.hidden_in_flight = p.log;
  }
  // the view-change hook point: the phone app calls window.levainDismissReviews() when its view changes, and every
  // open review is dismissed (the same rule: a sign in flight defers the release)
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    const base = p.vcount();
    const review = (_body, _sign, _dismiss) => { p.log.reviewed++; };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    p.log.has_hook = typeof p.window.levainDismissReviews === "function";
    p.log.free_before = await p.probe();
    if (p.log.has_hook) p.log.dismissed_count = p.window.levainDismissReviews();
    await tick();
    p.log.free_after = await p.probe();
    p.log.listeners_after = p.vcount() - base;
    if (p.log.has_hook) p.log.dismissed_again = p.window.levainDismissReviews();
    out.view_change = p.log;
  }
  // a hide while a sign is in flight whose answer is NOT terminal (a 4xx): the result is said, then the review is
  // dismissed once it settles, the sheet is told, and Sign stays dead
  {
    let open;
    const gate = new Promise((r) => { open = r; });
    const p = page(WAITING, [[409, { message: "stale" }]], gate);
    const held = {};
    p.log.closed = [];
    const review = (_body, sign, dismiss) => { p.log.reviewed++; held.sign = sign; held.dismiss = dismiss;
                                               return (why) => p.log.closed.push(why); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    held.sign();
    await tick();
    p.visibility(true);
    await tick();
    p.visibility(false);
    p.log.closed_in_flight = p.log.closed.length;
    open();
    await tick();
    p.log.free_after = await p.probe();
    held.sign();
    await tick();
    out.hidden_in_flight_4xx = p.log;
  }
  // the passkey refused (NotAllowedError: a cancelled Face ID or a passkey gone from the phone, which this browser
  // cannot tell apart): this browser's record of its passkey is kept (the one "don't forget" rule, 1022+19 Q2)
  {
    const p = page(WAITING, { state: "COMMITTED" }, undefined, { refuse: "NotAllowedError", mine: { "SHA256:a": "cmF3aWQ" } });
    const review = (_body, sign) => { p.log.reviewed++; sign(); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    p.log.mapping = p.mapping();
    out.refused_keeps = p.log;
  }
  // L3 r1 (codex MED): a done()/say()/closed() that throws during a hide must never keep the hold
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    p.log.closed = [];
    const review = (_body, _sign, _dismiss) => { p.log.reviewed++; return (why) => p.log.closed.push(why); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, () => { throw new Error("done fault"); });
    await tick();
    p.visibility(true);
    await tick();
    p.visibility(false);
    p.log.free_after_done_fault = await p.probe();
    out.done_throws = p.log;
  }
  // L3 r1 (codex LOW): a sheet that dismisses synchronously inside review() and returns its closed handler is told once
  {
    const p = page(WAITING, { state: "COMMITTED", signer_fp: "SHA256:a" });
    p.log.closed = [];
    const review = (_body, _sign, dismiss) => { p.log.reviewed++; dismiss(); return (why) => p.log.closed.push(why); };
    p.window.levainSign(["SHA256:a"], p.say, elem(), p.issue, review, (a) => p.log.done.push(a));
    await tick();
    p.log.free_after = await p.probe();
    out.sync_dismiss = p.log;
  }
  out.unhandled = unhandled;
  process.stdout.write(JSON.stringify(out));
}

const unhandled = [];
process.on("unhandledRejection", (e) => { unhandled.push(String((e && e.message) || e)); });

main().catch((e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
