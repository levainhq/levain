"""The launch token in the cockpit page (``token.js`` + ``dashboard_boot.js``), run under node with a stubbed DOM.

np-ebb8a399: every route but the page shell needs the token. The page must take it from the URL fragment and send
it, and when it holds none (or a wrong one) it must show ONE unlock form instead of an empty board, then render as
soon as the right token is entered. The off-box write-token prompt (``window.prompt``) must never fire for a
launch-token refusal, though both messages contain the word "token".
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
class N {
  constructor(tag) { this.tagName = tag; this.className = ""; this.children = []; this.attrs = {}; this.ls = {};
    this.parentNode = null; this._text = ""; this.disabled = false; this.value = ""; this.hidden = false; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  get firstChild() { return this.children[0] || null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
  fire(t, ev) { (this.ls[t] || []).forEach((f) => f(Object.assign({ preventDefault() {} }, ev))); }
}
const find = (n, pred) => { if (pred(n)) return n; for (const c of n.children) { const r = find(c, pred); if (r) return r; } return null; };
const body = new N("body"), store = new N("p"), stamp = new N("span"), refresh = new N("button");
const stamps = [];
Object.defineProperty(stamp, "textContent", { set(v) { stamps.push(String(v)); this._text = String(v); }, get() { return this._text; } });
body.appendChild(store);
const byId = { store: store, stamp: stamp, refresh: refresh };
const document = { body: body, visibilityState: "visible", createElement: (t) => new N(t),
  getElementById: (i) => byId[i] || null, addEventListener() {} };
const TOKEN = "tok-launch-1";
let serverToken = TOKEN;   // what the server accepts; a restart changes it
const MODE = process.argv[4];
const calls = [];
let prompted = 0;
const reply = (status, json) => Promise.resolve({ status, ok: status < 400, json: () => Promise.resolve(json) });
function fetch(path, init) {
  init = init || {}; const hdr = init.headers || {};
  calls.push({ path: path, method: init.method || "GET", token: hdr["X-Levain-Token"], url: path, body: init.body });
  if (MODE === "stalesubstrate" && !hdr["X-Levain-Token"] && !globalThis.__staleSent) {
    globalThis.__staleSent = true;
    return new Promise((res) => { globalThis.__releaseStale = () => res(null); })
      .then(() => reply(403, { error: "launch_token", message: "needs token" }));
  }
  if (path === "/unlock") {
    const good = MODE === "code" && hdr["X-Levain-Link-Code"] === "C1" && init.method === "POST";
    return good ? reply(200, { token: TOKEN }) : reply(403, { error: "link_code", message: "that link was already used or has expired" });
  }
  if (hdr["X-Levain-Token"] !== serverToken) return reply(403, { error: "launch_token", message: "this server needs the token printed when it started, sent as X-Levain-Token" });
  if (path === "/substrate.json") return reply(200, { writable: true, paths: {} });
  if (path === "/edit") return reply(200, { ok: true });
  return reply(404, {});
}
const replaced = [], kept = new Map();
if (MODE === "codespentstored") kept.set("levain.token", TOKEN);
const ctx = vm.createContext({ document, fetch, JSON, Promise, Array, Object, String, Date, encodeURIComponent,
  setTimeout: (f) => setImmediate(f),
  location: { hash: (MODE === "fragment" || MODE === "hashjunk") ? "#token=" + TOKEN
                  : (MODE === "code" || MODE === "codespent" || MODE === "codespentstored") ? "#code=C1" : "",
              pathname: "/", search: "" },
  history: { replaceState: (s, t, u) => { replaced.push(u); ctx.location.hash = ""; } },
  sessionStorage: { getItem: (k) => (kept.has(k) ? kept.get(k) : null), setItem: (k, v) => kept.set(k, v), removeItem: (k) => kept.delete(k) },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
});
ctx.window = ctx;
const winLs = {};
ctx.addEventListener = (t, f) => { (winLs[t] = winLs[t] || []).push(f); };
ctx.window.prompt = () => { prompted++; return ""; };
let renders = 0, transports = null;
ctx.LevainDashboard = { render: (view, t) => { renders++; transports = t; }, hasUnsavedEdit: () => false };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ok = (c, m) => { if (!c) { console.log("FAIL " + m); process.exit(1); } };
const lockForm = () => find(body, (n) => n.className === "levain-lock");
(async () => {
  if (MODE !== "notokenjs") vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
  vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), ctx);
  await sleep(30);
  if (MODE === "codespentstored") {
    // L1 2026-10-07: a spent link reloaded from history must not throw away the good token the tab still holds
    ok(renders >= 1 && !lockForm() && ctx.LevainToken.get() === TOKEN, "the stored token is kept and used");
    console.log("PASS"); return;
  }
  if (MODE === "code" || MODE === "codespent") {
    // head ruling 2026-10-07 (L2 #2, measured): the link carries a single-use code, traded once for the token
    ok(replaced.length === 1 && ctx.location.hash === "", "the code is stripped from the address bar");
    ok(calls.some((c) => c.path === "/unlock"), "the page trades it at /unlock");
    if (MODE === "code") {
      ok(renders === 1 && !lockForm() && kept.get("levain.token") === TOKEN, "the board renders, no form left up");
    } else {
      ok(lockForm() && lockForm().textContent.includes("already used"), "a spent link shows the form and says why");
      ok(renders === 0 && ctx.LevainToken.get() === null, "nothing renders, nothing kept");
    }
    console.log("PASS"); return;
  }
  if (MODE === "notokenjs") {
    // glm L3 r2: with token.js missing, a launch-token 403 (whose message says "token") must not open the off-box
    // write-token prompt
    ok(prompted === 0, "no off-box prompt for a launch-token refusal");
    console.log("PASS"); return;
  }
  if (MODE === "badinput") {
    // codex L3 r2: a value that cannot be a header made fetch throw locally, so no refusal ever brought the form back
    const before = calls.length;
    find(lockForm(), (n) => n.tagName === "input").value = "\u{1F512}"; lockForm().fire("submit", {}); await sleep(20);
    ok(lockForm() && lockForm().textContent.includes("not a token"), "the form stays and says why");
    find(lockForm(), (n) => n.tagName === "input").value = "a".repeat(300); lockForm().fire("submit", {}); await sleep(20);
    ok(lockForm() && ctx.LevainToken.get() === null, "a token too long for a header is refused too (codex L3)");
    ok(calls.length === before && ctx.LevainToken.get() === null, "nothing is sent or kept");
    console.log("PASS"); return;
  }
  if (MODE === "stalesubstrate") {
    // complement L3 r1: the first, untokened read stalls; the operator unlocks; then that read's refusal lands. The
    // page must render with the new token and never report the stale 403 as a failure.
    ctx.LevainToken.lock(null, null);
    find(lockForm(), (n) => n.tagName === "input").value = TOKEN; lockForm().fire("submit", {}); await sleep(20);
    globalThis.__releaseStale(); await sleep(60);
    ok(renders === 1 && !lockForm(), "the board renders with the new token");
    ok(!stamps.some((t) => /failed|403/.test(t)), "no stale failure was shown: " + JSON.stringify(stamps));
    console.log("PASS"); return;
  }
  if (MODE === "hashjunk") {
    // L1 2026-10-07: a page holding a reference to this tab can change its fragment. An unlocked page keeps its token.
    ctx.location.hash = "#token=junk"; (winLs.hashchange || []).forEach((f) => f({})); await sleep(30);
    ok(ctx.LevainToken.get() === TOKEN && kept.get("levain.token") === TOKEN, "the held token is kept");
    ok(ctx.location.hash === "" && !lockForm(), "the junk fragment is stripped and nothing locks");
    console.log("PASS"); return;
  }
  if (MODE === "fragment") {
    ok(replaced.length === 1 && ctx.location.hash === "", "the fragment is stripped from the address bar");
    ok(calls[0].token === TOKEN && renders === 1 && !lockForm(), "the first read carries the token and renders");
    ok(calls.every((c) => !c.url.includes(TOKEN)), "the token is never in a request URL");
    // a write carries it too
    const r = await transports.commit({ kind: "x" });
    ok(r.ok && calls[calls.length - 2].path === "/edit" && calls[calls.length - 2].token === TOKEN, "a write sends the token");
    console.log("PASS"); return;
  }
  ok(renders === 0, "nothing renders without the token");
  if (MODE === "hashchange") {
    // head ruling 2026-10-07: a token is taken from the fragment only at load, never from a later change of it
    // (another page can change this one's fragment). The fragment is still removed from the address bar.
    ctx.location.hash = "#token=" + TOKEN; (winLs.hashchange || []).forEach((f) => f({})); await sleep(30);
    ok(lockForm() && renders === 0 && ctx.LevainToken.get() === null, "a later fragment does not unlock");
    ok(ctx.location.hash === "", "and it is stripped from the address bar");
    console.log("PASS"); return;
  }
  if (MODE === "authheaders") {
    ok(typeof ctx.levainAuthHeaders === "function", "the page exposes levainAuthHeaders");
    const h0 = ctx.levainAuthHeaders();
    ok(JSON.stringify(h0) === "{}", "no token held: an empty header object");
    find(lockForm(), (n) => n.tagName === "input").value = TOKEN; lockForm().fire("submit", {}); await sleep(30);
    const h1 = ctx.levainAuthHeaders();
    ok(h1["X-Levain-Token"] === TOKEN && h1 !== ctx.levainAuthHeaders(), "the header, in a fresh object each call");
    console.log("PASS"); return;
  }
  ok(lockForm() && body.children.indexOf(lockForm()) === 0, "one unlock form, at the top of the page");
  ok(stamp.textContent.includes("locked"), "the status says the page is locked");
  ok(prompted === 0, "the off-box token prompt does not fire for a launch-token refusal");
  const input = find(lockForm(), (n) => n.tagName === "input");
  ok(input.type === "password", "the token field is a password input");
  input.value = "wrong-token"; lockForm().fire("submit", {}); await sleep(30);
  ok(lockForm() && lockForm().textContent.includes("not accepted"), "a wrong token is reported");
  ok(renders === 0 && prompted === 0, "still nothing rendered, still no off-box prompt");
  find(lockForm(), (n) => n.tagName === "input").value = TOKEN; lockForm().fire("submit", {}); await sleep(30);
  ok(!lockForm() && renders === 1, "the right token unlocks and the board renders without another click");
  ok(kept.get("levain.token") === TOKEN, "kept in this tab's sessionStorage");
  if (MODE === "writerefused") {
    // the server restarted (a new token): a write sent with the old token is refused, the form comes back, and the
    // off-box prompt does not fire
    serverToken = "tok-after-restart";
    const before = calls.length;
    const r = await transports.commit({ kind: "x" });
    ok(calls[before].token === TOKEN, "the write carried the token the page held");
    ok(!r.ok && r.error === "launch_token", "a refused write reports launch_token");
    ok(lockForm() && prompted === 0 && calls.length === before + 1, "the form is back; no off-box prompt");
    ok(ctx.LevainToken.get() === null && !kept.has("levain.token"), "the refused token is dropped");
  }
  console.log("PASS");
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("mode", ["fragment", "prompt", "writerefused", "hashchange", "hashjunk", "stalesubstrate", "notokenjs", "badinput", "authheaders", "code", "codespent", "codespentstored"])
def test_the_cockpit_page_sends_the_launch_token_and_unlocks_in_place(tmp_path, mode):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    p = subprocess.run(["node", str(h), str(WEB / "token.js"), str(WEB / "dashboard_boot.js"), mode],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "PASS" in p.stdout, p.stdout + p.stderr


@pytest.mark.parametrize("page,script", [("dashboard.html", "dashboard_core.js"), ("init.html", "init.js"),
                                         ("docs.html", "markdown.js")])
def test_every_page_loads_token_js_before_its_other_scripts(page, script):
    html = (WEB / page).read_text()
    assert html.index('src="/token.js"') < html.index(f'src="/{script}"')


# --- init.js and docs.js unlock flows (head ruling 2026-10-07, L1 #8) --------------------------------------------

PAGE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
class N {
  constructor(tag) { this.tagName = tag; this.className = ""; this.children = []; this.attrs = {}; this.ls = {};
    this.parentNode = null; this._text = ""; this.disabled = false; this.value = ""; this.hidden = false; this.id = "";
    this.classList = { add() {}, remove() {}, toggle() { return false; } }; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  get firstChild() { return this.children[0] || null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
  fire(t, ev) { (this.ls[t] || []).forEach((f) => f(Object.assign({ preventDefault() {} }, ev))); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  scrollIntoView() {}
}
const find = (n, pred) => { if (pred(n)) return n; for (const c of n.children) { const r = find(c, pred); if (r) return r; } return null; };
const all = (n, pred, acc) => { acc = acc || []; if (pred(n)) acc.push(n); n.children.forEach((c) => all(c, pred, acc)); return acc; };
const body = new N("body");
const IDS = process.argv[3] === "init"
  ? ["status", "target-banner", "target", "packs-note", "adapters", "sections", "submit", "submit-note", "result", "init-form"]
  : ["status", "toc-list", "chapters", "nav-toggle"];
IDS.forEach((i) => { const n = new N(i === "init-form" ? "form" : "div"); n.id = i; body.appendChild(n); });
if (process.argv[3] === "init") {
  find(body, (n) => n.id === "submit").disabled = true;              // as init.html ships it
  // the checked adapter: the first radio rendered checked, or what the test picked
  find(body, (n) => n.id === "init-form").querySelector = () => {
    const r = all(body, (n) => n.tagName === "input" && n.type === "radio" && n.checked)[0];
    return globalThis.__picked ? { value: globalThis.__picked } : (r || { value: "claude-code" });
  };
}
const document = { body: body, createElement: (t) => new N(t),
  getElementById: (i) => find(body, (n) => n.id === i) };
const MODE = process.argv[4];
const TOKEN = "tok-page-1";
let serverToken = TOKEN, target = "/inst/A";
const calls = [];
const reply = (status, json) => Promise.resolve({ status, ok: status < 400, json: () => Promise.resolve(json) });
function plan() {
  return { install: target, force: false, target_status: "empty", packs: [], adapters: ["claude-code", "codex"], default_adapter: "claude-code",
    fields: [{ slot: "OPERATOR_NAME", section_index: 0, section_title: "Identity", spec_name: "world.md", style: "line", current: "" },
             { slot: "AGE", section_index: 0, section_title: "Identity", spec_name: "world.md", style: "line", current: "" }] };
}
function fetch(path, init) {
  init = init || {}; const hdr = init.headers || {};
  calls.push({ path: path, token: hdr["X-Levain-Token"] });
  if (path === "/init" && MODE === "initinflight" && !globalThis.__postHeld) {
    globalThis.__postHeld = true;
    return new Promise((res) => { globalThis.__releasePost = () => res(null); })
      .then(() => reply(403, { error: "launch_token", message: "needs token" }));
  }
  if (MODE.endsWith("late") && !hdr["X-Levain-Token"] && !globalThis.__staleSent) {
    globalThis.__staleSent = true;
    return new Promise((res) => { globalThis.__releaseStale = () => res(null); })
      .then(() => reply(403, { error: "launch_token", message: "needs token" }));
  }
  if (hdr["X-Levain-Token"] !== serverToken) return reply(403, { error: "launch_token", message: "needs token" });
  if (path === "/init-plan.json") return reply(200, plan());
  if (path === "/init") return reply(200, { ok: true, messages: [] });
  if (path === "/docs.json") return reply(200, { chapters: [{ title: "One", markdown: "x", source: "base" }] });
  return reply(404, {});
}
const kept = new Map();
const ctx = vm.createContext({ document, fetch, JSON, Promise, Array, Object, String, Date, encodeURIComponent,
  setTimeout: (f) => setImmediate(f),
  location: { hash: "", pathname: "/", search: "" },
  history: { replaceState: () => {} },
  sessionStorage: { getItem: (k) => (kept.has(k) ? kept.get(k) : null), setItem: (k, v) => kept.set(k, v), removeItem: (k) => kept.delete(k) },
});
ctx.window = ctx;
ctx.LevainMD = { renderMarkdown: () => new N("div") };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ok = (c, m) => { if (!c) { console.log("FAIL " + m); process.exit(1); } };
const lockForm = () => find(body, (n) => n.className === "levain-lock");
const unlock = async (t) => { find(lockForm(), (n) => n.tagName === "input").value = t; lockForm().fire("submit", {}); await sleep(40); };
const byId = (i) => document.getElementById(i);
(async () => {
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
  vm.runInContext(fs.readFileSync(process.argv[5], "utf8"), ctx);
  await sleep(30);
  if (MODE === "initlock") {
    ok(lockForm() && /locked/.test(byId("status").textContent) && byId("submit").disabled, "locked, nothing rendered");
    await unlock(TOKEN);
    ok(!lockForm() && byId("f_OPERATOR_NAME") && /2 fields/.test(byId("status").textContent) && !byId("submit").disabled,
       "the unlock loads and renders the plan");
  } else if (MODE === "initrestart") {
    await unlock(TOKEN);
    byId("f_OPERATOR_NAME").value = "Kept";
    serverToken = "tok-page-2"; target = "/inst/B";   // the server restarted on the same port, another target
    byId("init-form").fire("submit", {}); await sleep(40);
    ok(lockForm() && byId("submit").disabled, "the refused install locks the page and keeps Install off");
    ok(!calls.some((c) => c.path === "/init" && c.token === "tok-page-2"), "nothing was installed");
    await unlock("tok-page-2");
    ok(byId("target").textContent === "/inst/B", "the plan was read again: the new target shows");
    ok(byId("f_OPERATOR_NAME").value === "Kept" && /2 of 2 answers kept/.test(byId("status").textContent), "answers kept");
    ok(!byId("submit").disabled, "Install is back once the plan is current");
  } else if (MODE === "initadapter") {
    // codex L3: a plan read again after a refused install must keep the adapter the operator chose
    await unlock(TOKEN);
    globalThis.__picked = "codex";
    serverToken = "tok-page-2"; target = "/inst/B";
    byId("init-form").fire("submit", {}); await sleep(40);
    globalThis.__picked = null;
    await unlock("tok-page-2");
    const codex = find(body, (n) => n.tagName === "input" && n.type === "radio" && n.value === "codex");
    ok(codex && codex.checked === true, "the chosen adapter is still selected");
  } else if (MODE === "initinflight") {
    // complement L3: an unlock lands while the install is in flight; that install's refusal (for the old token)
    // must still bring the plan back, not leave Install off with a token held
    await unlock(TOKEN);
    serverToken = "tok-page-2";
    byId("init-form").fire("submit", {}); await sleep(20);    // the POST, carrying TOKEN, is held
    ctx.LevainToken.lock(null, TOKEN);                         // the page learns its token is stale another way
    await unlock("tok-page-2");
    globalThis.__releasePost(); await sleep(60);
    ok(!byId("submit").disabled && /read again/.test(byId("status").textContent), "the plan is read again; Install is back");
  } else if (MODE === "initlate") {
    ctx.LevainToken.lock(null, null);
    await unlock(TOKEN);
    globalThis.__releaseStale(); await sleep(60);
    ok(all(body, (n) => n.id === "f_OPERATOR_NAME").length === 1, "the plan renders once");
    ok(/2 fields/.test(byId("status").textContent) && !lockForm(), "and the page is not left locked");
  } else if (MODE === "docslock") {
    ok(lockForm() && /Locked/.test(byId("status").textContent), "locked");
    await unlock(TOKEN);
    ok(!lockForm() && byId("chapters").children.length === 1, "the unlock renders the manual");
  } else if (MODE === "docslate") {
    ctx.LevainToken.lock(null, null);
    await unlock(TOKEN);
    ok(byId("chapters").children.length === 1, "rendered with the token");
    globalThis.__releaseStale(); await sleep(60);
    ok(byId("chapters").children.length === 1 && !lockForm(), "a late refusal of the first read changes nothing");
  }
  console.log("PASS");
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("page,mode", [("init", "initlock"), ("init", "initrestart"), ("init", "initlate"),
                                       ("init", "initadapter"), ("init", "initinflight"),
                                       ("docs", "docslock"), ("docs", "docslate")])
def test_init_and_docs_pages_unlock_and_reload_in_place(tmp_path, page, mode):
    h = tmp_path / "harness.js"
    h.write_text(PAGE_HARNESS)
    p = subprocess.run(["node", str(h), str(WEB / "token.js"), page, mode, str(WEB / f"{page}.js")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "PASS" in p.stdout, p.stdout + p.stderr
