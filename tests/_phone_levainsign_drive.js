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

// one page: ``pending`` is GET /cockpit/pending/<id>'s body, ``signed`` the POST .../signature answer's body
function page(pending, signed) {
  const log = { issued: 0, webauthn: 0, reviewed: 0, said: [], done: [] };
  const answer = (status, body) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
  const fetch = (route, opts) => {
    if (route === "/consent.json") return answer(503, { message: "no queue here" });
    if (/^\/cockpit\/pending\/[^/]+$/.test(route)) return answer(200, pending);
    if (/\/signature$/.test(route)) { log.posted = JSON.parse(opts.body); return answer(200, signed); }
    return answer(404, { message: "no route " + route });
  };
  const buf = (n) => new Uint8Array([n]).buffer;
  const navigator = { credentials: { get: () => {
    log.webauthn++;
    return Promise.resolve({ response: { authenticatorData: buf(1), clientDataJSON: buf(2), signature: buf(3) } });
  } } };
  const window = { isSecureContext: true, PublicKeyCredential: function () {}, localStorage: memStore(), addEventListener() {},
                   sessionStorage: memStore() };
  const ctx = { window, navigator, fetch, document: { getElementById: () => elem(), createElement: () => elem(), querySelectorAll: () => [], addEventListener() {}, hidden: false },
                location: { hash: "", hostname: "localhost", pathname: "/" }, history: { replaceState() {} },
                setInterval: () => 0, btoa, atob };
  window.window = window;
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const say = (text, kind) => { log.said.push([text, kind || ""]); };
  const issue = () => { log.issued++; return Promise.resolve({ ok: true, status: 200,
                                                               body: { state: "WAITING", pending_id: "cp" + "0".repeat(32) } }); };
  return { window, log, say, issue };
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
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
