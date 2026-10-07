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
  if (hdr["X-Levain-Token"] !== serverToken) return reply(403, { error: "launch_token", message: "this server needs the token printed when it started, sent as X-Levain-Token" });
  if (path === "/substrate.json") return reply(200, { writable: true, paths: {} });
  if (path === "/edit") return reply(200, { ok: true });
  return reply(404, {});
}
const replaced = [], kept = new Map();
const ctx = vm.createContext({ document, fetch, JSON, Promise, Array, Object, String, Date, encodeURIComponent,
  setTimeout: (f) => setImmediate(f),
  location: { hash: (MODE === "fragment" || MODE === "hashjunk") ? "#token=" + TOKEN : "", pathname: "/", search: "" },
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
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
  vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), ctx);
  await sleep(30);
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
    // the unlocked link opened over the locked page changes only the fragment; the browser does not reload
    ctx.location.hash = "#token=" + TOKEN; (winLs.hashchange || []).forEach((f) => f({})); await sleep(30);
    ok(!lockForm() && renders === 1 && ctx.location.hash === "", "a token arriving in a new fragment unlocks in place");
    console.log("PASS"); return;
  }
  ok(lockForm() && body.children.indexOf(lockForm()) === 0, "one unlock form, at the top of the page");
  ok(stamp.textContent.includes("locked"), "the status says the page is locked");
  ok(prompted === 0, "the off-box token prompt does not fire for a launch-token refusal");
  const input = find(lockForm(), (n) => n.tagName === "input");
  ok(input.type === "password", "the token field is a password input");
  input.value = "wrong"; lockForm().fire("submit", {}); await sleep(30);
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
@pytest.mark.parametrize("mode", ["fragment", "prompt", "writerefused", "hashchange", "hashjunk"])
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
