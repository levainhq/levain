"""The cockpit chat panel (``dashboard_chat.js``): served as an asset, and its consent surface.

The property that matters is that ``/chat/approve`` is sent only from a user's own click. It is proved
twice: statically (one call site, inside the Approve click handler, behind ``isTrusted``), and — where
node exists — by executing the script against a stubbed DOM and fetch: a pending result arriving does
not POST, a synthetic click does not POST, a trusted click POSTs exactly once even if clicked twice.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "dashboard_chat.js"


def test_the_chat_script_is_served_and_the_page_loads_it(tmp_path):
    from levain.dashboard import AnnealPaths, SubstrateSource
    from levain.web_server import make_server

    source = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"))
    httpd = make_server(source, host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(base + "/dashboard_chat.js", timeout=5) as r:  # noqa: S310 — loopback
            assert r.status == 200
            assert r.headers["Content-Type"].startswith("text/javascript")
            assert b"/chat/approve" in r.read()
        with urllib.request.urlopen(base + "/", timeout=5) as r:  # noqa: S310
            assert b'src="/dashboard_chat.js"' in r.read()
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_approve_has_one_call_site_in_a_trusted_click_handler():
    src = JS.read_text()
    posts = [m.start() for m in re.finditer(r'"/chat/approve"', src)]
    # one quoted route string in the whole file (a second is a second route to the same consent)
    assert len(posts) == 1
    # walk back from the POST to the nearest listener registration: it must be the Approve button's click
    before = src[: posts[0]]
    handler = before[before.rindex("addEventListener(") :]
    assert handler.startswith('addEventListener("click", (ev) =>')
    assert "approve.addEventListener" in before[before.rindex("addEventListener(") - 12 :]
    assert "!ev.isTrusted" in handler and handler.index("!ev.isTrusted") < handler.index("lock()")
    # nothing time- or poll-driven sits between the guard and the POST
    assert "setTimeout" not in handler and "poll(" not in handler


def test_no_innerhtml_and_no_token_persistence():
    src = JS.read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "localStorage", "sessionStorage",
                   "document.cookie", "location.hash", "location.search"):
        assert banned not in src


HARNESS = r"""
const fs = require("fs"), vm = require("vm");
class N {
  constructor(tag) { this.tagName = tag; this.className = ""; this.children = []; this.attrs = {}; this.ls = {};
    this.parentNode = null; this._text = ""; this.disabled = false; this.value = ""; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  get firstChild() { return this.children[0] || null; }
  focus() {}
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
  fire(t, ev) { (this.ls[t] || []).forEach((f) => f(Object.assign({ preventDefault() {} }, ev))); }
}
const find = (n, pred) => { if (pred(n)) return n; for (const c of n.children) { const r = find(c, pred); if (r) return r; } return null; };
const byText = (root, text) => find(root, (n) => n.tagName === "button" && n._text === text);
const body = new N("body"), bar = new N("nav"), board = new N("div");
body.appendChild(bar); body.appendChild(board);
const document = { createElement: (t) => new N(t), querySelector: (s) => (s === "nav.tabs" ? bar : null),
  getElementById: (i) => (i === "board" ? board : null) };
const TOKEN = "tok-123";
const calls = [];
const approvals = () => calls.filter((c) => c.path === "/chat/approve").length;
const reply = (status, json) => Promise.resolve({ status, ok: status < 400, json: () => Promise.resolve(json) });
function fetch(path, init) {
  init = init || {}; const hdr = init.headers || {};
  calls.push({ path: path.split("?")[0], method: init.method, token: hdr["X-Levain-Chat-Token"], body: init.body });
  if (hdr["X-Levain-Chat-Token"] !== TOKEN) return reply(403, { error: "chat_token", message: "needs token" });
  if (path === "/chat.json") return reply(200, { entities: ["ent"], model: "m", sessions: [] });
  if (path === "/chat/open") return reply(202, { session_id: "S", job_id: "J-open" });
  if (path.startsWith("/chat/job.json?id=J-open")) return reply(200, { status: "done", result: { session: { state: "idle" } } });
  if (path === "/chat/turn") return reply(202, { job_id: "J-turn" });
  if (path.startsWith("/chat/job.json?id=J-turn")) return reply(200, { status: "done", result: { reply: null, gated: true, error: null, timed_out: false, tool_activity: [],
    pending: [{ tool: "bash", detail: "rm -rf x", reason: "destructive", recognized: true }] } });
  if (path === "/chat/approve") return reply(202, { job_id: "J-appr" });
  if (path.startsWith("/chat/job.json?id=J-appr")) return reply(200, { status: "done", result: { reply: "done it", gated: false, error: null, timed_out: false, tool_activity: ["bash ok"], pending: [] } });
  return reply(404, {});
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ctx = vm.createContext({ document, fetch, setTimeout: (f) => setImmediate(f), clearTimeout() {}, encodeURIComponent, JSON, Promise, Array, Object, String });
const ok = (c, m) => { if (!c) { console.log("FAIL " + m); process.exit(1); } };
(async () => {
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
  await sleep(30);
  const panel = find(body, (n) => n.className === "panel chat-panel");
  ok(panel && body.children.indexOf(panel) === 1, "panel appears (403 chat_token) before the board");
  const pw = find(panel, (n) => n.tagName === "input");
  ok(pw.type === "password", "token field is a password input");
  // wrong token first
  pw.value = "nope"; find(panel, (n) => n.tagName === "form").fire("submit", {}); await sleep(30);
  ok(panel.textContent.includes("not accepted"), "wrong token is reported");
  const pw2 = find(panel, (n) => n.tagName === "input"); pw2.value = TOKEN;
  find(panel, (n) => n.tagName === "form").fire("submit", {}); await sleep(30);
  byText(panel, "Open session").fire("click", { isTrusted: true }); await sleep(60);
  const area = find(panel, (n) => n.tagName === "textarea"); area.value = "do the thing";
  find(panel, (n) => n.tagName === "form").fire("submit", {}); await sleep(80);
  ok(panel.textContent.includes("Held for your approval") && panel.textContent.includes("rm -rf x"), "pending is shown");
  await sleep(100);
  ok(approvals() === 0, "a pending result alone sends no approve");
  const approve = byText(panel, "Approve"), reject = byText(panel, "Reject");
  ok(approve && reject, "both decision buttons exist");
  approve.fire("click", { isTrusted: false }); await sleep(50);
  ok(approvals() === 0, "a synthetic click sends no approve");
  ok(!approve.disabled, "a synthetic click does not lock the buttons");
  approve.fire("click", { isTrusted: true }); approve.fire("click", { isTrusted: true });
  ok(approve.disabled && reject.disabled, "buttons lock while a decision is in flight");
  await sleep(80);
  ok(approvals() === 1, "a trusted click sends exactly one approve (got " + approvals() + ")");
  ok(panel.textContent.includes("done it"), "the approved job's result is shown");
  console.log("PASS");
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_approve_posts_only_after_a_trusted_click(tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    p = subprocess.run(["node", str(h), str(JS)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "PASS" in p.stdout, p.stdout + p.stderr


def test_the_consent_shows_the_whole_action_that_approving_runs():
    # L2 2026-10-05: the held command was shown cut at the display limit and with newlines flattened, while
    # approving ran all of it. The bounded `detail` stays for one-line surfaces; `full` is what runs.
    from levain.chat import _turn_payload
    from levain.firing.gate import PendingEfferent
    from levain.firing.openhands.gate import _detail_for, _full_for

    cmd = "echo " + "a" * 300 + "\nrm -rf ~/x"
    fields = {"command": cmd}
    p = PendingEfferent("terminal", _detail_for("terminal", fields, None), "bash fans in", full=_full_for(fields))
    assert "rm -rf" not in p.detail                       # the bounded rendering hides the tail ...
    assert p.full == cmd                                  # ... the whole action is carried unchanged ...
    assert "\n        rm -rf ~/x" in p.line()             # ... the REPL line shows it ...

    class R:  # the TurnResult fields _turn_payload reads
        reply, tool_activity, error, nudged, gated, timed_out, ok, exit_code = None, [], None, False, True, False, False, 0
        pending = [p]
    assert _turn_payload(R())["pending"][0]["full"] == cmd   # ... and the chat payload carries it to the panel,
    js = (Path(__file__).parent.parent / "levain" / "templates" / "web" / "dashboard_chat.js").read_text()
    assert re.search(r'const whole = \(typeof p\.full === "string" && p\.full\) \? p\.full : p\.detail;', js)
    assert 'el("pre", "chat-detail", String(whole))' in js     # which renders it in place of the detail
