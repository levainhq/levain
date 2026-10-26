// Drives phone.js's Grants tab (M17 S-b) in a node vm over a small DOM that keeps a real tree, so a test can find a card,
// tap its buttons and read what the page says. Two modes:
//   node _phone_grants_drive.js <phone.js> scripted
//       one JSON object, one key per scenario, against a scripted fetch and a counting WebAuthn
//   node _phone_grants_drive.js <phone.js> live <base url> <token> <pkcs8 pem file> <rp id> <origin> <grant id>
//       the page against a real phone port: the revoke of <grant id> is tapped through, and WebAuthn signs with the
//       P-256 key in the pem file as an authenticator does (UP and UV set). Prints what the page said and sent.
"use strict";
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");

const src = fs.readFileSync(process.argv[2], "utf8");
const mode = process.argv[3];

function memStore() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); },
           removeItem: (k) => { m.delete(k); } };
}

// ---- a small DOM: a tree, text, classes, listeners and the simple selectors phone.js uses ("tag", ".a", "tag.a.b",
// "tag.a:not(:disabled)", ".a .b" is not needed) --------------------------------------------------------------------
class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parent = null; this._text = ""; this.className = "";
    this.hidden = false; this.disabled = false; this.readOnly = false; this.value = ""; this.dataset = {}; this.style = {};
    this.attrs = {}; this.listeners = {}; this.title = ""; this.type = ""; this.rows = 0; this.maxLength = 0;
    this.placeholder = ""; this.tabIndex = -1;
    const self = this;
    this.classList = { add(c) { if (!self.classes().includes(c)) self.className = (self.className + " " + c).trim(); },
                       remove(c) { self.className = self.classes().filter((x) => x !== c).join(" "); },
                       contains(c) { return self.classes().includes(c); }, toggle() {} };
  }
  classes() { return String(this.className || "").split(/\s+/).filter(Boolean); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = v == null ? "" : String(v); this.children.forEach((c) => { c.parent = null; }); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  _adopt(c) { if (typeof c === "string") { const t = new Node("#text"); t._text = c; c = t; } if (c.parent) c.remove(); c.parent = this; return c; }
  appendChild(c) { c = this._adopt(c); this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  replaceChildren(...cs) { this.children.forEach((c) => { c.parent = null; }); this.children = []; this._text = ""; this.append(...cs); }
  remove() { if (this.parent) { const p = this.parent; p.children = p.children.filter((c) => c !== this); this.parent = null; } }
  after(c) { const p = this.parent; if (!p) return; c = p._adopt(c); p.children.splice(p.children.indexOf(this) + 1, 0, c); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  click() { if (this.disabled) return false; (this.listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); return true; }
  focus() {} scrollIntoView() {}
  matches(sel) {
    const m = /^([a-z]*)((?:\.[\w-]+)*)(:not\(:disabled\))?$/i.exec(sel.trim());
    if (!m) throw new Error("selector not supported by the drive: " + sel);
    if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
    if (m[3] && this.disabled) return false;
    return m[2].split(".").filter(Boolean).every((c) => this.classes().includes(c));
  }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) { return this.all().filter((n) => n.tagName !== "#TEXT" && n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148";
const ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36";
const IDS = ["status", "requests", "enrol", "enrol-note", "enrol-btn", "enrol-out", "refresh", "grants", "grants-status",
             "grants-list"];

function page(fetchImpl, opts) {
  const log = { webauthn: [], posts: [], gets: 0 };
  const body = new Node("body");
  const byId = {};
  for (const id of IDS) { const n = new Node(id === "refresh" || id === "enrol-btn" ? "button" : "div"); n.id = id; byId[id] = n; body.appendChild(n); }
  const document = {
    hidden: false, activeElement: null, listeners: {},
    getElementById: (id) => byId[id] || null, createElement: (t) => new Node(t),
    querySelectorAll: (sel) => body.querySelectorAll(sel),
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
  };
  // an iPhone unless a scenario says otherwise: the page names Face ID there and "your screen lock" elsewhere
  const navigator = { userAgent: opts.userAgent === undefined ? IPHONE : opts.userAgent,
                      credentials: { get: (o) => { log.webauthn.push(o.publicKey); return opts.webauthn(o.publicKey); } } };
  const window = { isSecureContext: true, PublicKeyCredential: function () {}, localStorage: memStore(),
                   sessionStorage: memStore(), addEventListener() {} };
  window.window = window;
  if (opts.token !== null) window.sessionStorage.setItem("levain-phone-token", opts.token || "T");
  if (opts.mine) window.localStorage.setItem("levain-passkeys:" + opts.rpId, JSON.stringify(opts.mine));
  const fetch = (route, init) => {
    if ((init && init.method) === "POST") log.posts.push([route, JSON.parse(init.body)]);
    else log.gets++;
    return fetchImpl(route, init);
  };
  const timers = [];
  const ctx = { window, navigator, fetch, document, location: { hash: "", hostname: opts.rpId, pathname: "/" },
                history: { replaceState() {} }, setInterval: () => 0, setTimeout: (fn, ms) => { timers.push(setTimeout(fn, Math.min(ms, 50))); return 0; },
                btoa, atob, Uint8Array, JSON, Promise, Array, Object, Number, String, Map, Set, Date, isNaN, encodeURIComponent,
                crypto: globalThis.crypto, TextEncoder };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const cards = () => byId["grants-list"].children;
  const card = (id) => cards().find((c) => c.dataset.grant === id) || null;
  const button = (root, re) => root.querySelectorAll("button").find((b) => re.test(b.textContent)) || null;
  return { log, byId, document, cards, card, button, timers, window };
}

const tick = (ms) => new Promise((r) => setTimeout(r, ms || 120));
const ok = (status, body) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });

const FP = "SHA256:phonekeyphonekeyphonekeyphonekeyphonekey0";
const RAW = "cmF3aWQ";            // a credential id this browser made (base64url of "rawid")
const call = (at, tool, outcome) => ({ request_id: "r-" + at, at: "2026-10-22T1" + at + ":00:00.000000+00:00", tool: tool,
                                       cwd: "/w/proj", outcome: outcome });
const QUIET = { id: "cg-aaaaaaaaaaaa", id_shown: "cg-aaaaaaaaaaaa", kind: "consent", state: "live", agent: "claude_code",
                tools: ["Write"], workspace: "/w/proj", postures: ["default"], expires_at: "2026-10-29T12:00:00.000000+00:00",
                link_seq: 3, instance: "3:aaaa", signers: [FP],
                answered: { count: 2, last_at: "2026-10-22T12:00:00.000000+00:00", recent: [call(2, "Write", "accepted"), call(1, "Write", "accepted")] } };
const DEMOTE = { id: "cg-bbbbbbbbbbbb", id_shown: "cg-bbbbbbbbbbbb", kind: "consent", state: "live", agent: "codex",
                 tools: ["Edit", "Write"], workspace: "/w/other", postures: ["default", "auto"],
                 expires_at: "2026-10-30T12:00:00.000000+00:00", link_seq: 4, instance: "4:bbbb", signers: [FP],
                 answered: { count: 7, last_at: "2026-10-22T15:00:00.000000+00:00",
                             recent: [call(5, "Edit", "accepted"), call(4, "Write", "refused"), call(3, "Edit", "unknown")] } };
const sshStr = (b) => { const n = Buffer.alloc(4); n.writeUInt32BE(b.length); return Buffer.concat([n, b]); };
const CHAL = (prev, line) => Buffer.concat([Buffer.from("SSHSIG"), sshStr(Buffer.from("levain-authority")), sshStr(Buffer.alloc(0)),
                                            sshStr(Buffer.from("sha512")),
                                            sshStr(crypto.createHash("sha512").update(prev + "\n" + line).digest())])
  .toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const PREV = "9f".repeat(32);
// a revoke-challenge reply as the server sends it: the line, the head it chains to and the challenge over both
const REPLY = (gid, extra) => Object.assign({ grant_id: gid, rp_id: "phone.example", signer_fp: FP, line: LINE(gid), prev_hash: PREV,
                                              challenge: CHAL(PREV, LINE(gid)) }, extra || {});
const LINE = (gid) => JSON.stringify({ at: "2026-10-22T20:00:00.000000Z", format: 1, grant_id: gid, op: "revoke_grant", seq: 9, store_id: "s" });

function scriptedFetch(listing, answers) {
  return (route, init) => {
    if (route === "/consent.json") return ok(200, listing);
    if (/\/revoke-challenge$/.test(route)) return answers.challenge(route, JSON.parse(init.body));
    if (/\/revoke$/.test(route)) return answers.revoke(route, JSON.parse(init.body));
    return ok(404, { message: "no route " + route });
  };
}
const fakeCred = () => Promise.resolve({ response: { authenticatorData: new Uint8Array([1]).buffer,
                                                     clientDataJSON: new Uint8Array([2]).buffer, signature: new Uint8Array([3]).buffer } });
const listing = (grants, extra) => Object.assign({ rp_id: "phone.example", pending: [], enrolled: [FP], grants: grants,
                                                   grants_note: "", inert_note: "held inert: answers nothing here" }, extra || {});
const read = (n) => n ? n.textContent : null;
const notesOf = (c) => c ? c.querySelectorAll("div.note").map((n) => n.textContent) : [];

async function scripted() {
  const out = {};
  const base = { rpId: "phone.example", mine: { [FP]: RAW } };

  // the platform's prompt is named by platform: Android's is the screen lock or a fingerprint, never Face ID
  {
    const names = {};
    for (const [label, userAgent] of [["ios", IPHONE], ["android", ANDROID], ["none", undefined]]) {
      const ans = { challenge: (route, b) => ok(200, REPLY(DEMOTE.id, { signer_fp: b.signer_fp })), revoke: () => ok(200, {}) };
      const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred, userAgent: label === "none" ? "" : userAgent }, base));
      await tick();
      p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
      await tick();
      const c = p.card(DEMOTE.id);
      names[label] = { buttons: c.querySelectorAll("button").map((b) => b.textContent), text: c.textContent };
    }
    out.platform = names;
  }

  // the list: the grant with a refused call comes first, and inside it the refused call leads
  {
    const p = page(scriptedFetch(listing([QUIET, DEMOTE]), {}), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    const first = p.cards()[0];
    out.render = { order: p.cards().map((c) => c.dataset.grant), status: read(p.byId["grants-status"]),
                   calls: first ? first.querySelectorAll("div.call").map((c) => c.textContent) : [],
                   refusedMarked: first ? first.querySelectorAll("div.call.refused").length : -1,
                   text: read(first), revoke: !!(first && p.button(first, /revoke/i)), disabled: !!(first && p.button(first, /revoke/i).disabled) };
  }
  // the ceremony: the challenge names the instance shown, the link is shown BEFORE any passkey prompt, and only the
  // sheet's own tap asks for Face ID; the revoke sends the assertion and nothing else
  {
    const ans = { challenge: (route, b) => ok(200, REPLY(DEMOTE.id, { signer_fp: b.signer_fp })),
                  revoke: () => ok(200, { revoked: DEMOTE.id, seq: 9, warning: "" }) };
    const p = page(scriptedFetch(listing([QUIET, DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await tick();
    const c = p.card(DEMOTE.id);
    const shownBefore = { webauthn: p.log.webauthn.length, line: c ? c.querySelectorAll("pre.line").map((n) => n.textContent) : [],
                          faceId: !!(c && p.button(c, /Face ID/)) };
    const gets = p.log.gets;
    p.byId.refresh.click();                                 // a refresh tap while the sheet is up
    await tick(200);
    const heldStill = p.log.gets === gets;                  // the open sheet holds the list still
    p.button(c, /Face ID/).click();
    await tick();
    out.ceremony = { shownBefore, heldStill, posts: p.log.posts, webauthn: p.log.webauthn.length,
                     challenge: p.log.webauthn[0] ? Buffer.from(p.log.webauthn[0].challenge).toString("base64").replace(/\+/g, "-")
                       .replace(/\//g, "_").replace(/=+$/, "") : "", want: CHAL(PREV, LINE(DEMOTE.id)),
                     allow: p.log.webauthn[0] ? (p.log.webauthn[0].allowCredentials || []).length : -1,
                     uv: p.log.webauthn[0] ? p.log.webauthn[0].userVerification : "", rpId: p.log.webauthn[0] ? p.log.webauthn[0].rpId : "",
                     notes: notesOf(p.card(DEMOTE.id)), status: read(p.byId["grants-status"]) };
  }
  // a link that does not name this grant is never put to the passkey
  {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id, { line: LINE("cg-other"), challenge: CHAL(PREV, LINE("cg-other")) })),
                  revoke: () => ok(200, {}) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await tick();
    out.mismatch = { webauthn: p.log.webauthn.length, faceId: !!p.button(p.card(DEMOTE.id), /Face ID/),
                     posts: p.log.posts.map((x) => x[0]), notes: notesOf(p.card(DEMOTE.id)) };
  }
  // the server's own refusal of the signed revoke (the log moved) is shown as its words: nothing was revoked
  {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id)),
                  revoke: () => ok(409, { error: "stale", message: "the authority log moved since this revoke was asked for: revoke again" }) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await tick();
    p.button(p.card(DEMOTE.id), /Face ID/).click();
    await tick();
    out.stale = { notes: notesOf(p.card(DEMOTE.id)) };
  }
  // a reply that is not the server's refusal (no reply, a 5xx) after the signature was sent: not knowable, said so
  {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id)),
                  revoke: () => ok(502, {}) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await tick();
    p.button(p.card(DEMOTE.id), /Face ID/).click();
    await tick();
    out.unknown = { notes: notesOf(p.card(DEMOTE.id)) };
  }
  // cancel at the sheet: no passkey prompt, no revoke sent, and the list moves again
  {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id)),
                  revoke: () => ok(200, {}) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await tick();
    p.button(p.card(DEMOTE.id), /^cancel$/).click();
    const gets = p.log.gets;
    p.byId.refresh.click();
    await tick();
    out.cancel = { webauthn: p.log.webauthn.length, posts: p.log.posts.map((x) => x[0]), moved: p.log.gets > gets,
                   sheet: p.card(DEMOTE.id) ? p.card(DEMOTE.id).querySelectorAll("pre.line").length : -1 };
  }
  // the page put away with the sheet up dismisses it (it would otherwise hold every consent card still)
  {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id)),
                  revoke: () => ok(200, {}) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await tick();
    p.document.hidden = true;
    (p.document.listeners.visibilitychange || []).forEach((fn) => fn());
    p.document.hidden = false;
    await tick();
    out.hidden = { sheet: p.card(DEMOTE.id) ? p.card(DEMOTE.id).querySelectorAll("pre.line").length : -1, posts: p.log.posts.length };
  }
  // no passkey may revoke here (a localhost port, none enrolled): the button is off and the server's note is shown
  {
    const why = "a passkey made on localhost cannot revoke a grant";
    const p = page(scriptedFetch(listing([Object.assign({}, DEMOTE, { signers: [] })], { grants_note: why }), {}),
                   Object.assign({ webauthn: fakeCred }, base));
    await tick();
    const c = p.card(DEMOTE.id);
    out.nosigner = { disabled: c ? p.button(c, /revoke/i).disabled : null, notes: notesOf(c), status: read(p.byId["grants-status"]) };
  }
  // nothing granted: said, never an empty list with no words
  {
    const p = page(scriptedFetch(listing([]), {}), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    out.empty = { status: read(p.byId["grants-status"]), cards: p.cards().length };
  }
  // ---- L3 r1 (M-1, M-3..M-9): each scenario reproduced one finding on 7ae3def0's phone.js ----------------------------
  const flush = async () => { await tick(); await tick(); };
  const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };
  const leave = (p, v) => { if (typeof p.window.levainViewChanged === "function") p.window.levainViewChanged(v); };
  // M-1: a line for this grant, a challenge over ANOTHER line: the shown line is not what Face ID would sign, so the page
  // refuses before any prompt. A reply without the head the line chains to cannot be checked, so it is refused too
  for (const [name, reply] of [["bound", REPLY(DEMOTE.id, { challenge: CHAL(PREV, LINE("cg-other")) })],
                               ["noprev", REPLY(DEMOTE.id, { prev_hash: undefined })]]) {
    const ans = { challenge: () => ok(200, reply), revoke: () => ok(200, { revoked: DEMOTE.id, seq: 9, warning: "" }) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await flush();
    const faceId = !!p.button(p.card(DEMOTE.id), /Face ID/);
    if (faceId) { p.button(p.card(DEMOTE.id), /Face ID/).click(); await flush(); }
    out[name] = { faceId, webauthn: p.log.webauthn.length, posts: p.log.posts.map((x) => x[0]), notes: notesOf(p.card(DEMOTE.id)) };
  }
  // M-3: the view changed while the challenge was in flight, then while the sheet was up: the ask is dropped, nothing is
  // signed, and the list moves again (busy released)
  {
    const d = deferred();
    const ans = { challenge: () => d.promise, revoke: () => ok(200, {}) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await tick();
    leave(p, "board");
    d.resolve(ok(200, REPLY(DEMOTE.id)));
    await flush();
    const gets = p.log.gets;
    p.byId.refresh.click();
    await tick();
    out.viewMid = { sheet: p.card(DEMOTE.id) ? p.card(DEMOTE.id).querySelectorAll("pre.line").length : -1,
                    webauthn: p.log.webauthn.length, moved: p.log.gets > gets, posts: p.log.posts.length };
  }
  {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id)), revoke: () => ok(200, {}) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await flush();
    const up = p.card(DEMOTE.id).querySelectorAll("pre.line").length;
    leave(p, "board");
    const gets = p.log.gets;
    p.byId.refresh.click();
    await tick();
    out.viewSheet = { up, sheet: p.card(DEMOTE.id) ? p.card(DEMOTE.id).querySelectorAll("pre.line").length : -1,
                      webauthn: p.log.webauthn.length, moved: p.log.gets > gets };
  }
  // M-4: a read that fails after a good one (a revoked device token: 403) says so on the Grants tab and turns its revoke
  // buttons off until a read succeeds; a page with no token says it on the Grants tab too
  {
    let fail = false;
    const fetchImpl = (route, init) => (route === "/consent.json" && fail ? ok(403, { message: "this page's token is needed" })
                                                                         : scriptedFetch(listing([DEMOTE]), {})(route, init));
    const p = page(fetchImpl, Object.assign({ webauthn: fakeCred }, base));
    await tick();
    const before = p.button(p.card(DEMOTE.id), /revoke with passkey/i).disabled;
    fail = true;
    p.byId.refresh.click();
    await tick();
    const c = p.card(DEMOTE.id);
    const during = { status: read(p.byId["grants-status"]), disabled: c ? p.button(c, /revoke with passkey/i).disabled : null };
    fail = false;
    p.byId.refresh.click();
    await tick();
    out.failRead = { before, during, after: p.button(p.card(DEMOTE.id), /revoke with passkey/i).disabled };
  }
  {
    const p = page(scriptedFetch(listing([DEMOTE]), {}), Object.assign({ webauthn: fakeCred, token: null }, base));
    await tick();
    out.noToken = { status: read(p.byId["grants-status"]), gets: p.log.gets };
  }
  // M-5: a refusal older than the calls the server sends still leads the list and is said on the card
  {
    // PLAIN answered nothing refused; OLD's two refusals are older than its two newest (accepted) calls
    const PLAIN = Object.assign({}, QUIET, { id: "cg-cccccccccccc", id_shown: "cg-cccccccccccc",
                                             answered: Object.assign({}, QUIET.answered, { refused_count: 0 }) });
    const OLD = Object.assign({}, QUIET, { answered: Object.assign({}, QUIET.answered, { count: 9, refused_count: 2 }) });
    const p = page(scriptedFetch(listing([PLAIN, OLD]), {}), Object.assign({ webauthn: fakeCred }, base));
    await tick();
    out.refusedCount = { order: p.cards().map((c) => c.dataset.grant), status: read(p.byId["grants-status"]),
                         text: read(p.card(QUIET.id)), demote: p.card(QUIET.id).classList.contains("demote") };
  }
  // M-6: a stored credential id that does not decode throws at the Face ID tap: the sheet goes, nothing is signed, the
  // list moves again
  {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id)), revoke: () => ok(200, {}) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({}, base, { webauthn: fakeCred, mine: { [FP]: "%" } }));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await flush();
    let threw = "";
    try { p.button(p.card(DEMOTE.id), /Face ID/).click(); } catch (e) { threw = String((e && e.name) || e); }
    await flush();
    const gets = p.log.gets;
    p.byId.refresh.click();
    await tick();
    out.badRaw = { threw, sheet: p.card(DEMOTE.id).querySelectorAll("pre.line").length, webauthn: p.log.webauthn.length,
                   posts: p.log.posts.map((x) => x[0]), notes: notesOf(p.card(DEMOTE.id)), moved: p.log.gets > gets };
  }
  // M-7: a note belongs to the grant INSTANCE it was said on: the same id made again (a new instance) starts clean
  {
    let shown = listing([DEMOTE]);
    const ans = { challenge: () => ok(409, { error: "grant_changed", message: "the grant was renewed; nothing was signed" }),
                  revoke: () => ok(200, {}) };
    const fetchImpl = (route, init) => scriptedFetch(shown, ans)(route, init);
    const p = page(fetchImpl, Object.assign({ webauthn: fakeCred }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await flush();
    const first = notesOf(p.card(DEMOTE.id));
    shown = listing([Object.assign({}, DEMOTE, { instance: "11:cccc" })]);
    p.byId.refresh.click();
    await flush();
    await tick(1700);                                     // past the re-read after the outcome
    out.notes = { first, renewed: notesOf(p.card(DEMOTE.id)) };
  }
  // M-8: a cancelled Face ID (NotAllowedError, what a cancel and a missing key both raise) keeps this browser's mapping
  // M-9: a passkey that resolves with nothing sends nothing and says nothing was revoked
  for (const [name, webauthn] of [["cancelKeep", () => Promise.reject(Object.assign(new Error("cancel"), { name: "NotAllowedError" }))],
                                  ["nullCred", () => Promise.resolve(null)]]) {
    const ans = { challenge: () => ok(200, REPLY(DEMOTE.id)), revoke: () => ok(200, { revoked: DEMOTE.id, seq: 9, warning: "" }) };
    const p = page(scriptedFetch(listing([DEMOTE]), ans), Object.assign({ webauthn }, base));
    await tick();
    p.button(p.card(DEMOTE.id), /revoke with passkey/i).click();
    await flush();
    p.button(p.card(DEMOTE.id), /Face ID/).click();
    await flush();
    out[name] = { mapping: JSON.parse(p.window.localStorage.getItem("levain-passkeys:phone.example") || "{}"),
                  posts: p.log.posts.map((x) => x[0]), notes: notesOf(p.card(DEMOTE.id)) };
  }
  // 1022+19 Q2: the same "don't forget" rule on every other passkey ceremony on the page: a consent answer, a late
  // ruling, a sign request, a dispatch contract and a question's push. Each refuses with NotAllowedError, and this
  // browser's record of its passkey is kept
  {
    const CARD = (id) => ({ id, tool: "Write", tier: "T1", agent: "claude_code", display: "write a.txt", signers: [FP] });
    const asks = [
      ["consent", { pending: [CARD("c-answer")] }, /^allow with passkey/],
      ["ruling", { undecided: [Object.assign(CARD("c-rule"), { gap: 1, gap_label: "#1" })] }, /^rule allow with passkey/],
      ["sign", { sign_pending: [{ id: "s-sign", title: "a statement", namespace: "levain-sign", message: "sign me",
                                  sha256: "h", signers: [FP] }] }, /^sign with Face ID/],
      ["dispatch", { dispatch_pending: [{ id: "j-job", state: "filed", can_sign: true, signers: [FP], template: {}, budget: {},
                                          read: [], write: [], history: [] }] }, /^Sign with passkey/],
      ["question", { questions: [{ n: 1, id: "q-one", agent: "claude_code", what: "which?", blocks: "the push",
                                   options: [{ key: "a", label: "A" }],
                                   link: { option: "a", card: CARD("c-push") } }] }, /allow the push with passkey/],
    ];
    const refuse = () => Promise.reject(Object.assign(new Error("cancel"), { name: "NotAllowedError" }));
    const challenge = (route) => (/challenge$/.test(route)
      ? ok(200, { challenge: "AAAA", rp_id: "phone.example", payload: "p", line: "l" }) : null);
    out.keepAll = {};
    for (const [name, extra, re] of asks) {
      const shown = listing([], extra);
      const f = (route, init) => (route === "/consent.json" ? ok(200, shown) : challenge(route) || ok(404, { message: "no route " + route }));
      const p = page(f, Object.assign({ webauthn: refuse }, base));
      await tick();
      const b = p.button(p.byId.requests, re);
      if (b) b.click();
      await flush();
      out.keepAll[name] = { tapped: !!b, webauthn: p.log.webauthn.length,
                            mapping: JSON.parse(p.window.localStorage.getItem("levain-passkeys:phone.example") || "{}"),
                            said: p.byId.requests.querySelectorAll("div.note").map((n) => n.textContent).filter(Boolean) };
    }
  }
  return out;
}

async function live() {
  const [baseUrl, token, pemPath, rpId, origin, gid] = process.argv.slice(4);
  const key = crypto.createPrivateKey(fs.readFileSync(pemPath, "utf8"));
  let counter = 0;
  const b64u = (b) => Buffer.from(b).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const webauthn = (pk) => {
    counter++;
    const authData = Buffer.concat([crypto.createHash("sha256").update(pk.rpId).digest(), Buffer.from([0x05]),
                                    Buffer.from([0, 0, 0, counter])]);
    const cdj = Buffer.from(JSON.stringify({ type: "webauthn.get", challenge: b64u(pk.challenge), origin: origin, crossOrigin: false }));
    const sig = crypto.sign("sha256", Buffer.concat([authData, crypto.createHash("sha256").update(cdj).digest()]), { key, dsaEncoding: "der" });
    const ab = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
    return Promise.resolve({ response: { authenticatorData: ab(authData), clientDataJSON: ab(cdj), signature: ab(sig) } });
  };
  const p = page((route, init) => fetch(baseUrl + route, init), { webauthn, token, rpId });
  const until = async (fn, what) => { for (let i = 0; i < 100; i++) { if (fn()) return; await tick(100); } throw new Error("never: " + what); };
  await until(() => p.card(gid), "the grant's card");
  const before = { order: p.cards().map((c) => c.dataset.grant), calls: p.card(gid).querySelectorAll("div.call").map((c) => c.textContent) };
  p.button(p.card(gid), /revoke with passkey/i).click();
  await until(() => p.card(gid) && p.button(p.card(gid), /Face ID/), "the revoke sheet");
  const line = p.card(gid).querySelectorAll("pre.line").map((n) => n.textContent);
  const promptsBeforeTap = p.log.webauthn.length;
  p.button(p.card(gid), /Face ID/).click();
  await until(() => notesOf(p.card(gid)).some((t) => /revoked|not revoked|unknown|did not sign/.test(t)) || !p.card(gid), "an outcome");
  const said = notesOf(p.card(gid));
  await tick(1800);   // the page re-reads the list 1.5 s after an outcome
  return { before, line, promptsBeforeTap, said, posts: p.log.posts, status: read(p.byId["grants-status"]),
           after: p.cards().map((c) => c.dataset.grant) };
}

(mode === "live" ? live() : scripted()).then((out) => { process.stdout.write(JSON.stringify(out)); process.exit(0); },
  (e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
