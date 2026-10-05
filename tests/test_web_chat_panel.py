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
    assert "runBtn.addEventListener" in before[before.rindex("addEventListener(") - 12 :]   # Run them, in the confirm row
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
    this.parentNode = null; this._text = ""; this.disabled = false; this.value = ""; this.style = {}; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  get firstChild() { return this.children[0] || null; }
  focus() { globalThis.__focused = this; }
  replaceChild(n, old) { const i = this.children.indexOf(old); if (i >= 0) { this.children[i] = n; n.parentNode = this; old.parentNode = null; } return old; }
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
  if (path.startsWith("/chat/job.json?id=J-open")) return reply(200, { status: "done", result: { session: { state: "idle", workspace: "/ws/ent" } } });
  if (path === "/chat/turn" && process.argv[3] === "turn500") return reply(500, {});
  if (path === "/chat/turn" && process.argv[3] === "turn403json") return reply(403, { error: "chat_token", message: "needs token" });
  if (path === "/chat/approve" && process.argv[3] === "approve403json") return reply(403, { error: "chat_token", message: "needs token" });
  if (path.startsWith("/chat/job.json?id=J-appr") && process.argv[3] === "poll403json") return reply(403, { error: "chat_token", message: "needs token" });
  if (path === "/chat/turn" && process.argv[3] === "turn409") return reply(409, { error: "wrong_state", message: "the session is busy" });
  if (path === "/chat/turn") return reply(202, { job_id: "J-turn" });
  if (path.startsWith("/chat/job.json?id=J-turn")) return reply(200, { status: "done", result: { reply: null, gated: true, error: null, timed_out: false, tool_activity: [],
    pending: [{ tool: "bash", detail: "rm -rf x", full: "rm\u200b -rf x", reason: "destructive", recognized: true },
              { tool: "finish", detail: "FinishAction", full: "x\\u{200B}\ty\r", reason: "turn control", recognized: true },
              { tool: "t\nx", detail: "d", full: "q" + String.fromCodePoint(...TBL) + "qA\\u{41}", reason: "r", recognized: true }],
    decision_id: process.argv[3] === "nodecision" ? undefined : "D1" } });
  const M = process.argv[3];
  if ((M === "post500" || M === "ambiguousgated") && path === "/chat/approve") return reply(500, {});
  if (M === "proxy503" && path === "/chat/approve") return reply(503, null);
  if (M === "proxy503json" && path === "/chat/approve") return reply(503, { error: "upstream_timeout" });
  if (M === "evicted" && path.startsWith("/chat/job.json?id=J-appr")) return reply(200, { status: "unknown" });
  if (M === "notstarted" && path === "/chat/approve") return reply(500, {});
  if (M === "notstarted" && path.startsWith("/chat/session.json?id=S") && approvals() >= 1)
    return reply(200, { state: "idle", job_id: null, last_job: { job_id: "J-turn", kind: "turn", status: "done",
      result: { reply: "the earlier turn", tool_activity: ["\u2699 earlier"], gated: true, error: null } } });
  if (["post500", "proxy503", "proxy503json", "evicted"].includes(M) && path.startsWith("/chat/session.json?id=S") && approvals() >= 1)
    return reply(200, { state: "idle", job_id: null, last_job: { job_id: M === "evicted" ? "J-appr" : "J-other", kind: "approve", status: "done",
      result: { reply: "ran it", tool_activity: ["\u2699 terminal: rm -rf x"], gated: false, error: null } } });
  if (M === "restart404" && path === "/chat/approve") return reply(500, {});
  if (M === "restart404" && path.startsWith("/chat/session.json?id=S") && approvals() >= 1)
    return reply(404, { error: "unknown_session", message: "no such session" });
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
// Polling timers run at once; a long timer (the armed-Approve timeout) waits until the test fires it.
const longTimers = new Map(); let tid = 0;
const fireLong = () => { for (const [i, f] of [...longTimers]) { longTimers.delete(i); f(); } };
const ctx = vm.createContext({ document, fetch, encodeURIComponent, JSON, Promise, Array, Object, String,
  setTimeout: (f, ms) => { if (ms >= 2000) { const i = ++tid; longTimers.set(i, f); return i; } setImmediate(f); return 0; },
  clearTimeout: (i) => { longTimers.delete(i); } });
// Approve opens the confirm row; Run them confirms.
const runThem = () => find(body, (n) => n.tagName === "button" && n._text === "Run them");
const approveTwice = async (b) => { b.fire("click", { isTrusted: true }); await sleep(20); const r = runThem(); if (r) r.fire("click", { isTrusted: true }); };
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
  if (process.argv[3] === "sendkey") {
    // Phill 2026-10-05 (a): keyboard access to Send is kept. Enter/Space on the focused button reaches the page as a
    // TRUSTED click with detail 0, and it sends, once.
    byText(panel, "Send").fire("click", { isTrusted: true, detail: 0 }); await sleep(80);
    ok(calls.filter((c) => c.path === "/chat/turn").length === 1, "keyboard activation of Send sends the turn once");
    console.log("PASS"); return;
  }
  if (process.argv[3] === "enter") {
    // 0.6.7: Enter sends from the compose box; Shift+Enter and an IME-composing Enter do not
    area.fire("keydown", { key: "Enter", shiftKey: true, isTrusted: true }); await sleep(30);
    area.fire("keydown", { key: "Enter", isComposing: true, isTrusted: true }); await sleep(30);
    area.fire("compositionstart", {}); area.fire("keydown", { key: "Enter", keyCode: 13, isTrusted: true }); await sleep(30);
    area.fire("compositionend", {}); await sleep(30);
    area.fire("keydown", { key: "Enter", isTrusted: false }); await sleep(30);
    // a script's form.requestSubmit() fires a submit the browser marks TRUSTED: submit must send nothing either way
    find(panel, (n) => n.tagName === "form" && n.className === "chat-compose").fire("submit", { isTrusted: true }); await sleep(30);
    byText(panel, "Send").fire("click", { isTrusted: false }); await sleep(30);
    ok(!calls.some((c) => c.path === "/chat/turn"), "Shift+Enter, a composing Enter (by flag or by composition events), a synthetic Enter, any form submit and an untrusted Send click send nothing");
    area.fire("keydown", { key: "Enter", isTrusted: true }); await sleep(80);
    ok(calls.filter((c) => c.path === "/chat/turn").length === 1, "Enter sends the turn once");
    const reason = find(panel, (n) => n.tagName === "textarea" && n.attrs["aria-label"] === "reason for rejecting");
    ok(reason && reason.rows === 1, "the reject reason is a one-line growing textarea");
    reason.fire("keydown", { key: "Enter", isTrusted: true }); reason.fire("keydown", { key: "Enter", shiftKey: true, isTrusted: true }); await sleep(60);
    ok(approvals() === 0 && !calls.some((c) => c.path === "/chat/reject"), "Enter in the reason field decides nothing");
    // a raw key event on a decision button decides nothing by itself: only the button's activation (a click, which a
    // browser also delivers for Enter/Space on the focused button) does
    for (const label of ["Approve", "Reject"]) {
      const b = byText(panel, label);
      b.fire("keydown", { key: "Enter", isTrusted: true }); b.fire("keydown", { key: " ", isTrusted: true }); b.fire("keyup", { key: "Enter", isTrusted: true });
    }
    await sleep(60);
    ok(approvals() === 0 && !calls.some((c) => c.path === "/chat/reject"), "a raw key on Approve or Reject decides nothing");
    // compose is disabled while the hold waits: a trusted Enter on it (or held-down repeats) sends nothing
    area.value = "again"; area.fire("keydown", { key: "Enter", isTrusted: true }); area.fire("keydown", { key: "Enter", isTrusted: true }); await sleep(60);
    ok(area.disabled && calls.filter((c) => c.path === "/chat/turn").length === 1, "Enter on the disabled compose box sends nothing while a decision waits");
    console.log("PASS"); return;
  }
  byText(panel, "Send").fire("click", { isTrusted: true }); await sleep(80);
  if (process.argv[3] === "turn403json") {
    ok(panel.textContent.includes("may already have run") && !panel.textContent.includes("Held for your approval"), "a token refusal on a turn POST reports an unknown outcome and builds no box");
    console.log("PASS"); return;
  }
  if (process.argv[3] === "turn500" || process.argv[3] === "turn409") {
    ok(!panel.textContent.includes("Held for your approval"), "no consent box from a turn POST without a clear answer");
    ok(!calls.some((c) => c.path === "/chat/session.json"), "the session is not read on its own");
    ok(panel.textContent.includes("outcome of the last turn is unknown; the previous request may already have run"), "an ambiguous turn is reported unknown");
    ok(area.disabled, "compose is blocked");
    const rr = byText(panel, "Check what happened"); ok(rr, "a re-read button is offered");
    rr.fire("click", { isTrusted: false }); await sleep(50);
    ok(!calls.some((c) => c.path === "/chat/session.json"), "a synthetic click reads nothing");
    rr.fire("click", { isTrusted: true }); await sleep(80);
    ok(calls.filter((c) => c.path === "/chat/session.json").length === 1, "a trusted click reads the session once");
    ok(panel.textContent.includes("y-after-reload") && panel.textContent.includes("may already have run"), "the held set is shown, with the warning");
    console.log("PASS"); return;
  }
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
  if (["post500", "proxy503", "proxy503json", "evicted", "ambiguousgated", "restart404", "notstarted", "lost", "lostok", "lostloop", "resync", "stale"].includes(process.argv[3])) {
    // No clear answer to the approve (a bare 5xx, a proxy's status, an evicted job, lost polls): the decision may
    // have run. The page NEVER builds a box or claims an outcome on its own; it says so, blocks compose, and reads
    // the session only on a trusted click of "Check what happened", carrying the warning into what it shows.
    const m = process.argv[3];
    await approveTwice(approve); await sleep(300);
    ok(approvals() === 1, "one approve was sent");
    ok(panel.textContent.includes("outcome of the last decision is unknown; the previous request may already have run"), "an ambiguous decision is reported unknown");
    ok(!calls.some((c) => c.path === "/chat/session.json"), "the session is not read on its own");
    ok(!byText(panel, "Approve"), "no consent box is built on its own");
    ok(area.disabled, "compose stays blocked");
    ok(!panel.textContent.includes("done it"), "never shown as a completed turn");
    const rr = byText(panel, "Check what happened"); ok(rr, "a re-read button is offered");
    rr.fire("click", { isTrusted: false }); await sleep(50);
    ok(!calls.some((c) => c.path === "/chat/session.json"), "a synthetic click reads nothing");
    rr.fire("click", { isTrusted: true }); await sleep(120);
    ok(calls.filter((c) => c.path === "/chat/session.json").length === 1, "a trusted click reads the session once");
    if (m === "notstarted") {
      ok(panel.textContent.includes("No job has started on the server since your last confirmed request, so your last decision has not run"), "a lost request that never started is said as such");
      ok(!panel.textContent.includes("These actions ran") && !panel.textContent.includes("the earlier turn"), "the earlier job is never shown as what happened");
      ok(!byText(panel, "Check what happened"), "and no check is re-offered (a stale one could read a later request as this one)");
    } else if (m === "restart404") {
      ok(panel.textContent.includes("The server no longer has that session (it restarted, or the session ended and was cleaned up). Your last request may or may not have run"), "a session the server no longer knows: gone, outcome unknown");
      ok(panel.textContent.includes("/ws/ent"), "it says where to look");
      ok(byText(panel, "Start a new session") && !byText(panel, "Approve"), "a way forward, and no box");
    } else if (m === "ambiguousgated" || m === "resync" || m === "stale") {
      const box = find(panel, (n) => n.className === "chat-consent");
      ok(box && box.textContent.includes("may already have run") && box.textContent.includes("y-after-reload"), "the re-read box carries the warning");
      const again = byText(panel, "Approve"); ok(again && again !== approve, "a fresh box with its own Approve");
      await approveTwice(again); await sleep(80);
      ok(JSON.parse(calls.filter((c) => c.path === "/chat/approve")[1].body).expect === "D2", "it carries the id the server reported");
    } else if (m === "lostloop") {
      ok(byText(panel, "Check what happened") && area.disabled, "a busy session offers the re-read again; nothing loops");
    } else {
      ok(panel.textContent.includes("The session is idle now") && !area.disabled, "an idle session is usable again");
      if (["post500", "proxy503", "proxy503json", "evicted"].includes(m)) {
        ok(panel.textContent.includes("These actions ran:") && panel.textContent.includes("terminal: rm -rf x"), "the check shows what the lost decision ran");
        ok(panel.textContent.includes("above is what the server recorded"), "and says the record is the answer");
      } else {
        ok(panel.textContent.includes("The session is idle now. The outcome"), "with no matching record, the outcome stays unknown");
        ok(!panel.textContent.includes("These actions ran"), "and nothing is shown as what happened");
      }
    }
    console.log("PASS"); return;
  }
  if (process.argv[3] === "approve403json" || process.argv[3] === "poll403json") {
    // chat r8 L3 (codex HIGH, gemini HIGH): a chat_token 403 after the approve was sent proves nothing ran either
    await approveTwice(approve); await sleep(150);
    ok(approvals() === 1, "one approve was sent");
    ok(panel.textContent.includes("may already have run"), "the token refusal reports an unknown outcome");
    ok(!byText(panel, "Approve") && !panel.textContent.includes("Held for your approval"), "no consent box remains or is built");
    console.log("PASS"); return;
  }
  const M2 = process.argv[3];
  const row = approve.parentNode;
  ok(row.children.indexOf(reject) < row.children.indexOf(approve), "Reject comes before Approve, so Tab from the reason reaches Reject first");
  const cancelOf = () => find(panel, (n) => n.tagName === "button" && n._text === "Cancel");
  if (M2.startsWith("confirm_")) {
    approve.fire("click", { isTrusted: false }); await sleep(20);
    ok(!cancelOf(), "a script click does not open the confirm row");
    approve.fire("click", { isTrusted: true }); await sleep(20);
    const cancel = cancelOf(), run = runThem();
    ok(cancel && run && approvals() === 0, "Approve opens the confirm row and approves nothing");
    ok(panel.textContent.includes("Run the 3 held actions?"), "the row asks about the whole held set");
    ok(row.children.indexOf(cancel) === row.children.indexOf(reject) + 1 && row.children.indexOf(run) === row.children.length - 1, "Cancel takes Approve's place; Run them comes after it");
    ok(globalThis.__focused === cancel, "Cancel has the focus");
    ok(reject.disabled && reject.style.visibility === "hidden", "Reject is set aside (its space kept) while the row is open");
    if (M2 === "confirm_double") {
      // the second click of a double click lands where Approve was: on Cancel
      cancel.fire("click", { isTrusted: true, detail: 2 }); await sleep(20);
      ok(!cancelOf() && byText(panel, "Approve") && approvals() === 0, "a double click on Approve opens and cancels: nothing ran");
    } else if (M2 === "confirm_cancel") {
      cancel.fire("click", { isTrusted: true }); await sleep(20);
      ok(!cancelOf() && globalThis.__focused === approve && !reject.disabled && approvals() === 0, "Cancel closes the row, refocuses Approve");
      approve.fire("click", { isTrusted: true }); await sleep(20);
      row.fire("keydown", { key: "Escape", target: cancelOf() }); await sleep(20);
      ok(!cancelOf() && approvals() === 0, "Esc closes the row");
    } else if (M2 === "confirm_trap") {
      row.fire("keydown", { key: "Tab", target: cancel }); ok(globalThis.__focused === run, "Tab moves Cancel -> Run them");
      row.fire("keydown", { key: "Tab", target: run }); ok(globalThis.__focused === cancel, "and wraps back: Tab stays in the row");
      row.fire("keydown", { key: "ArrowRight", target: cancel }); ok(globalThis.__focused === run, "an arrow moves too");
      row.fire("keydown", { key: "Enter", target: run }); await sleep(20);
      ok(approvals() === 0, "a key event on the row decides nothing by itself (only Run them's activation does)");
      run.fire("click", { isTrusted: false }); await sleep(20);
      ok(approvals() === 0, "a script click on Run them approves nothing");
    } else if (M2 === "confirm_run") {
      run.fire("click", { isTrusted: true }); run.fire("click", { isTrusted: true }); await sleep(80);
      ok(approvals() === 1 && run.disabled && cancel.disabled && approve.disabled, "Run them approves once and locks every button");
      ok(JSON.parse(calls.find((c) => c.path === "/chat/approve").body).expect === "D1", "with the decision id");
    }
    console.log("PASS"); return;
  }
  if (M2 === "reject_key") {
    reject.fire("click", { isTrusted: true, detail: 0 }); await sleep(80);
    ok(calls.filter((c) => c.path === "/chat/reject").length === 1 && approvals() === 0, "Reject is one step, by keyboard too");
    console.log("PASS"); return;
  }
  approve.fire("click", { isTrusted: true }); approve.fire("click", { isTrusted: true });
  ok(approvals() === 0, "Approve alone, any number of times, approves nothing");
  runThem().fire("click", { isTrusted: true }); runThem().fire("click", { isTrusted: true });
  ok(approve.disabled && reject.disabled, "buttons lock while a decision is in flight");
  await sleep(80);
  ok(approvals() === 1, "a trusted click sends exactly one approve (got " + approvals() + ")");
  ok(JSON.parse(calls.find((c) => c.path === "/chat/approve").body).expect === "D1", "approve carries the decision id");
  ok(panel.textContent.includes("done it"), "the approved job's result is shown");
  console.log("PASS");
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("mode", ["", "nodecision", "resync", "stale", "lost", "lostok", "lostloop", "post500", "proxy503",
                                  "evicted", "ambiguousgated", "turn500", "turn409", "proxy503json", "turn403json", "approve403json", "poll403json", "enter", "restart404", "notstarted", "sendkey",
                                  "reject_key", "confirm_open", "confirm_double", "confirm_cancel", "confirm_trap", "confirm_run"])
def test_approve_posts_only_after_a_trusted_click(tmp_path, mode):
    # mode "nodecision": a result with no decision id must render no Approve at all (the panel fails closed)
    # modes "lost*", "post500", "proxy503", "evicted", "ambiguousgated", "turn500": no clear answer; the outcome is
    # reported unknown with compose blocked, and the session is read only on a trusted re-read click (chat r7)
    # modes "stale", "resync", "turn409", "proxy503json" (chat r7 L3, codex/gemini/complement): a JSON 4xx/503 proves
    # nothing about who wrote it, so it is ambiguous like any other non-202; the session is read only on a re-read click
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    p = subprocess.run(["node", str(h), str(JS), mode], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "PASS" in p.stdout, p.stdout + p.stderr


def test_the_consent_shows_the_held_calls_bytes_with_the_short_form_beside_them():
    # chat r7 (Phill 2026-10-05, raw tool-call frame): the panel shows the held call's arguments exactly, as `full`;
    # the bounded `detail` is a convenience parsed from the same bytes and is never shown INSTEAD of them.
    pytest.importorskip("openhands.sdk")   # the adapter is an optional extra; a plain .[dev] install skips
    import json

    from levain.chat import _turn_payload
    from levain.firing.gate import PendingEfferent
    from levain.firing.openhands.gate import _detail_for

    cmd = "echo " + "a" * 300 + "\nrm -rf ~/x"
    raw = json.dumps({"command": cmd})
    p = PendingEfferent("terminal", _detail_for("terminal", json.loads(raw), None), "bash fans in", full=raw)
    assert "rm -rf" not in p.detail                       # the bounded rendering hides the tail ...
    assert "rm -rf ~/x" in p.full and "rm -rf ~/x" in p.line()   # ... the stored bytes carry it, the REPL shows them

    class R:  # the TurnResult fields _turn_payload reads
        reply, tool_activity, error, nudged, gated, timed_out, ok, exit_code = None, [], None, False, True, False, False, 0
        pending = [p]
    assert _turn_payload(R())["pending"][0]["full"] == raw   # ... and the chat payload carries them to the panel,
    js = JS.read_text()
    assert 'if (shownInFull(p.full)) item.appendChild(el("pre", "chat-detail", visible(p.full, true)));' in js
    assert '"in short: " + visible(p.detail)' in js                     # the short form, beside and labelled
    assert "p.full : p.detail" not in js                                # never in place of the bytes


def test_the_only_key_listener_is_the_compose_box():
    # 0.6.7: Enter-to-send is wired to the compose textarea alone; nothing in the consent box reacts to a key, so no
    # keystroke can approve or reject (the decision buttons are trusted clicks).
    src = JS.read_text()
    # two keydown listeners: the compose box (Enter sends) and the open confirm row (Esc closes, Tab/arrows move
    # focus between Cancel and Run them); the row's never decides anything
    assert src.count('addEventListener("keydown"') == 2
    row_keys = src[src.index('row.addEventListener("keydown"'):src.index('runBtn.addEventListener("click"')]
    assert "closeConfirm()" in row_keys and "api(" not in row_keys and "lock()" not in row_keys
    assert "!ev.isTrusted || area.disabled || deciding" in src
    # a script's requestSubmit() fires a TRUSTED submit, so the compose form's submit only prevents default
    assert 'form.addEventListener("submit", (ev) => { ev.preventDefault(); });' in src
    assert 'sendBtn.type = "button"' in src and 'sendBtn.addEventListener("click", (ev) => { if (ev.isTrusted) sendTurn(); });' in src
    assert 'area.addEventListener("keydown"' in src
    assert not re.search(r'addEventListener\("keypress"|onkey(up|down|press)', src)
    assert 'addEventListener("keyup"' not in src
    # no time constants guard the confirm: structure separates the two presses (Phill's A)
    for gone in ("ARM_TIMEOUT_MS", "ARM_GUARD_MS", "keyHeld", "armedAt", "ev.detail"):
        assert gone not in src, gone


def test_a_check_row_is_retired_whenever_a_new_request_is_sent():
    # 0.6.7 final round (complement + glm, class R): a "Check what happened" row answers for ONE lost request; it is
    # removed the moment the page sends a turn or a decision, and the "has not run" verdict offers no new one.
    src = JS.read_text()
    send = src[src.index("function sendTurn()"):]
    assert "retireChecks();" in send[: send.index('api("POST", "/chat/turn"')]
    assert "retireChecks(); deciding = true;" in src[src.index("function lock()"):]
    judge = src[src.index("function judgeLastJob("):src.index("function resync(")]
    assert "rereadButton(" not in judge
