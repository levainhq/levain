"""levain#157 W, page side: a landed write repaints its row and refetches only the panels it touched.

dashboard_boot.js runs under node against a fake server and a fake document (the pattern of test_cockpit_ke3_page.py); no
browser, kernel or store is involved. The view's `refetchPanel` is a stand-in that issues one panel GET through the fake
fetch, held open by a gate the test releases, so the order "row repainted, then the panel GET resolves" is observable.

* touched: one write POST (no dry run: the verb is direct), a GET of ONLY the touched panel, no manifest GET, the row marked
  updated and its next write carrying the response's row version, all before the held GET resolves;
* no `touched` (an older server): the whole-board path runs (manifest GET, every panel), the view's refetch is never used;
* de-dup: a stream-style refetch of the same panel right after the write is answered by the write's read, another panel
  is read for itself;
* a panel the page cannot repaint in place (answers false) asks one passive board read, never a latched reload.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")

DRIVER = r"""
const fs = require("fs");
const View = require(process.argv[2]);
const bootSrc = fs.readFileSync(process.argv[3], "utf8");
const only = process.argv[4] || null;

const SNOOZE = { verb: "intention_surface_at", tier: "T1", gesture: "direct", escalates: [], target: { panel_id: "tray", row_id: "intention:i1", row_version: "v1" } };
const UPDATE = { verb: "intention_update", tier: "T1", gesture: "direct", escalates: [], target: { panel_id: "tray", row_id: "intention:i1", row_version: "v1" } };
const head = (id, kind, extra) => Object.assign({ id, kind, title: id, status: "ok", as_of: "2026-10-12T12:00:00+00:00" }, extra || {});

function fixture() {
  const manifest = { schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} },
    generated_at: "2026-10-12T12:00:00+00:00", regions: { header: [], zones: [{ id: "operate", title: "Operate", panels: ["tray", "inbox"] }] },
    panels: { tray: head("tray", "list"), inbox: head("inbox", "list") },
    verbs: { intention_surface_at: { label: "Snooze", tier: "T1", confirm: false, idempotent: false, job: false, fields: ["date"], effect: "moves the date", gesture: "direct" },
             intention_update: { label: "Edit", tier: "T1", confirm: false, idempotent: false, job: false, fields: ["text", "tier"], effect: "changes the row", gesture: "direct" } },
    errors: [], transport: { writable: true, write_token_required: false } };
  const row = (id, actions) => ({ id, version: "v1", title: id, body: id, group: null, badges: [], facets: {}, actions });
  const panels = {
    tray: Object.assign(head("tray", "list"), { rows: [row("intention:i1", [SNOOZE, UPDATE])], actions: [] }),
    inbox: Object.assign(head("inbox", "list"), { rows: [row("m1", [])], actions: [] }),
  };
  return { manifest, panels };
}

// opts.real: the page's own LevainCockpitView.refetchPanel (no stand-in), so a panel the view cannot repaint in place answers false as it does on the board
function makeEnv(verbAnswer, refetchAnswer, opts) {
  opts = opts || {};
  const fx = fixture();
  const posts = [], gets = [], renders = [], marks = [];
  const store = () => { const m = {}; return { getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; } }; };
  let n = 0, gate = null, gated = false, manifestGate = null;
  const listeners = {};
  const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body, text: async () => JSON.stringify(body), headers: { get: () => null }, clone() { return this; } });
  async function fetchFn(url, init) {
    if (init && init.method === "POST") {
      const body = JSON.parse(init.body);
      posts.push({ url, body });
      return resp(200, verbAnswer(body));
    }
    gets.push(url);
    if (url.indexOf("/cockpit/manifest.json") >= 0) { if (manifestGate) await manifestGate; return resp(200, fx.manifest); }
    const m = /\/cockpit\/panel\/([^?]+)\.json/.exec(url);
    const pid = m ? decodeURIComponent(m[1]) : null;
    if (pid && fx.panels[pid]) return resp(200, fx.panels[pid]);
    return resp(404, {});
  }
  // the row as the page draws it: a clause (the row's words) and the inline style and attributes a repaint may set
  const clauseEl = { textContent: "old words" };
  const attrs = {};
  const rowEl = { dataset: { rowKey: "intention:i1" }, classList: { add: (c) => marks.push(c) }, querySelectorAll: () => [],
                  querySelector: (sel) => (sel === ".clause" ? clauseEl : null), appendChild() {},
                  style: {}, title: "", attrs, setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: (k) => (k in attrs ? attrs[k] : null) };
  const ls = store(); ls.setItem("levain_write_token", "tok");
  const env = { posts, gets, renders, marks, rowEl, clauseEl, refetchCalls: [], releaseGate: () => {}, releaseManifest: () => {}, settled: [], editing: false };
  const V = Object.assign({}, View, opts.real ? {} : {
    refetchPanel: (pid) => {
      env.refetchCalls.push(pid);
      const mark = () => marks.push("get-resolved:" + pid);
      return (async () => {
        await fetchFn("/cockpit/panel/" + pid + ".json");
        if (gated) await gate;
        mark();
        return refetchAnswer(pid);
      })();
    },
  });
  const win = { LevainCockpitView: V, localStorage: ls, sessionStorage: store(), location: { search: "?source=manifest", hash: "", pathname: "/" },
    crypto: { randomUUID: () => "key-" + (++n) }, prompt: () => "",
    LevainDashboard: { render: (view, o) => renders.push({ view, opts: o }), hasUnsavedEdit: () => env.editing, jobsActive: () => false }, dispatchEvent() {} };
  const doc = { getElementById: () => null, addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); }, querySelectorAll: (sel) => (sel === "[data-row-key]" ? [rowEl] : []),
                createElement: () => ({ className: "", textContent: "" }), visibilityState: "visible", body: { dataset: {} } };
  env.V = V;
  // a board read already out: the manifest GET is held until released; the page's resume handler asks a passive load
  env.holdManifest = () => { manifestGate = new Promise((r) => { env.releaseManifest = () => { manifestGate = null; r(); }; }); };
  env.startPassiveLoad = () => { for (const f of listeners["levain:resume"] || []) f({ detail: {} }); };
  env.manifestGets = (from) => env.gets.slice(from).filter((u) => u.indexOf("/cockpit/manifest.json") >= 0).length;
  env.hold =() => { gated = true; gate = new Promise((r) => { env.releaseGate = r; }); };
  env.start = async () => {
    new Function("window", "document", "location", "history", "fetch", "console", bootSrc)(win, doc, win.location, { replaceState() {} }, fetchFn, console);
    await new Promise((r) => setTimeout(r, 30));
    return env;
  };
  env.opts = () => renders[renders.length - 1].opts;
  env.snooze = () => env.opts().commit({ kind: "intention_surface_at", intention_id: "i1", date: "2999-01-01" });
  env.edit = (fields) => env.opts().commit(Object.assign({ kind: "intention_update", intention_id: "i1" }, fields));
  env.tick = () => new Promise((r) => setTimeout(r, 20));
  return env;
}

let failed = 0;
function ok(cond, what) { if (!cond) { failed++; console.log("FAIL " + what); } }
const isManifest = (u) => u.indexOf("/cockpit/manifest.json") >= 0;
const cases = {};

cases.touched_write_repaints_the_row_then_refetches_only_the_touched_panel = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"], row: { row_version: "v2", next: "2999-01-01" } }), () => "applied").start();
  const getsBefore = env.gets.length;
  env.hold();
  const r = await env.snooze();
  ok(r && r.ok === true, "the write landed: " + JSON.stringify(r));
  const writes = env.posts.filter((p) => !p.body.dry_run);
  ok(env.posts.length === 1 && writes.length === 1 && writes[0].body.verb === "intention_surface_at", "one write POST and no dry run: " + JSON.stringify(env.posts.map((p) => p.body)));
  await env.tick();
  const mine = env.gets.slice(getsBefore);
  ok(mine.length === 1 && /\/cockpit\/panel\/tray\.json/.test(mine[0]), "a GET of only the touched panel: " + JSON.stringify(mine));
  ok(!mine.some(isManifest), "no manifest GET");
  ok(env.marks.indexOf("row-updated") >= 0, "the row is marked updated: " + JSON.stringify(env.marks));
  ok(env.rowEl.dataset.rowVersion === "v2" && env.rowEl.dataset.rowNext === "2999-01-01", "the row carries the response's version and next: " + JSON.stringify(env.rowEl.dataset));
  ok(env.marks.indexOf("get-resolved:tray") < 0, "the row repainted while the panel GET was still held");
  env.releaseGate();
  await env.tick();
  ok(env.marks.indexOf("get-resolved:tray") > env.marks.indexOf("row-updated"), "the GET resolved after the repaint");
  ok(env.gets.slice(getsBefore).length === 1, "still one GET after the refetch settled");
  // the next write on the row names the version the response gave, not the stale one
  await env.snooze();
  const second = env.posts.filter((p) => !p.body.dry_run)[1];
  ok(second && second.body.row_version === "v2", "the next write names v2: " + JSON.stringify(second && second.body));
};

// What the person SEES within the write's own answer, before the panel read (8-12 s on a loaded bridge) returns.
cases.a_snooze_into_the_future_hides_the_row_before_the_read_returns = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"], row: { id: "intention:i1", row_version: "v2", next: "2999-01-01" } }), () => "applied").start();
  env.hold();
  const r = await env.snooze();
  ok(r && r.ok === true, "the write landed");
  await env.tick();
  ok(env.marks.indexOf("get-resolved:tray") < 0, "the panel GET is still held");
  ok(env.rowEl.style.display === "none", "the snoozed row is collapsed: " + JSON.stringify(env.rowEl.style));
  ok(env.rowEl.attrs["aria-hidden"] === "true", "and hidden from assistive tech: " + JSON.stringify(env.rowEl.attrs));
  env.releaseGate();
  await env.tick();
};

cases.a_snooze_that_leaves_the_row_in_view_dims_it_with_a_title = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"], row: { id: "intention:i1", row_version: "v2", next: "2000-01-01" } }), () => "applied").start();
  env.hold();
  await env.snooze();
  await env.tick();
  ok(env.marks.indexOf("get-resolved:tray") < 0, "the panel GET is still held");
  ok(env.rowEl.style.display !== "none", "a date in the past does not hide the row");
  ok(env.rowEl.style.opacity !== undefined && env.rowEl.style.opacity !== "" && Number(env.rowEl.style.opacity) < 1, "the row is dimmed: " + JSON.stringify(env.rowEl.style));
  ok(/updated/i.test(env.rowEl.title), "with a title that says it was updated: " + JSON.stringify(env.rowEl.title));
  env.releaseGate();
  await env.tick();
};

cases.an_edit_of_the_text_shows_the_new_words_in_place = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"], row: { id: "intention:i1", row_version: "v2", text: "new words" } }), () => "applied").start();
  env.hold();
  await env.edit({ text: "new words" });
  await env.tick();
  ok(env.marks.indexOf("get-resolved:tray") < 0, "the panel GET is still held");
  ok(env.clauseEl.textContent === "new words", "the row's words are the new ones: " + env.clauseEl.textContent);
  ok(env.rowEl.style.display !== "none", "the row stays");
  env.releaseGate();
  await env.tick();
};

cases.an_edit_of_a_field_with_no_place_in_the_row_dims_it = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"], row: { id: "intention:i1", row_version: "v2", tier: "parked" } }), () => "applied").start();
  env.hold();
  await env.edit({ tier: "parked" });
  await env.tick();
  ok(env.marks.indexOf("get-resolved:tray") < 0, "the panel GET is still held");
  ok(env.clauseEl.textContent === "old words", "the words are untouched");
  ok(Number(env.rowEl.style.opacity) < 1 && /updated/i.test(env.rowEl.title), "dimmed with a title: " + JSON.stringify([env.rowEl.style, env.rowEl.title]));
  env.releaseGate();
  await env.tick();
};

cases.a_row_for_another_row_paints_nothing = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"], row: { id: "intention:other", row_version: "v9", next: "2999-01-01" } }), () => "applied").start();
  await env.snooze();
  ok(env.rowEl.style.display !== "none" && env.rowEl.dataset.rowVersion !== "v9", "a row naming another id is not painted onto this one: " + JSON.stringify([env.rowEl.style, env.rowEl.dataset]));
};

cases.a_response_without_touched_runs_the_whole_board_path = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED" }), () => "applied").start();
  const getsBefore = env.gets.length;
  const r = await env.snooze();
  ok(r && r.ok === true, "the write landed");
  const mine = env.gets.slice(getsBefore);
  ok(mine.some(isManifest), "the manifest was read again: " + JSON.stringify(mine));
  ok(mine.some((u) => /panel\/tray\.json/.test(u)) && mine.some((u) => /panel\/inbox\.json/.test(u)), "every panel was read again: " + JSON.stringify(mine));
  ok(env.refetchCalls.length === 0, "the view's refetch was not used");
  ok(env.marks.indexOf("row-updated") < 0, "no in-place mark without touched");
};

cases.an_empty_touched_is_no_touched = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: [] }), () => "applied").start();
  const getsBefore = env.gets.length;
  await env.snooze();
  ok(env.gets.slice(getsBefore).some(isManifest) && env.refetchCalls.length === 0, "the old path ran");
};

cases.the_stream_announcing_a_refetched_panel_reads_it_once = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"] }), () => "applied").start();
  env.hold();
  await env.snooze();
  await env.tick();
  const a = env.V.refetchPanel("tray");            // as cockpit_stream.js calls it, while the write's read is in flight
  await env.tick();
  ok(env.refetchCalls.filter((p) => p === "tray").length === 1, "tray read once: " + JSON.stringify(env.refetchCalls));
  env.V.refetchPanel("inbox");
  await env.tick();
  ok(env.refetchCalls.indexOf("inbox") >= 0, "another panel is read for itself: " + JSON.stringify(env.refetchCalls));
  env.releaseGate();
  ok((await a) === "applied", "the stream's call got the write's answer");
  const b = await env.V.refetchPanel("tray");     // inside the window, after the read settled
  ok(b === "applied" && env.refetchCalls.filter((p) => p === "tray").length === 1, "still one read inside the window: " + JSON.stringify(env.refetchCalls));
};

cases.a_panel_that_cannot_repaint_in_place_asks_one_passive_board_read = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"] }), () => false).start();
  const getsBefore = env.gets.length;
  await env.snooze();
  await env.tick(); await env.tick();
  const manifests = env.gets.slice(getsBefore).filter(isManifest);
  ok(manifests.length === 1, "one board read, not a latched second: " + JSON.stringify(env.gets.slice(getsBefore)));
};

// H1: the save's own re-read must rebuild the board (closing the form it came from) even though an edit is open, as the old
// whole-board reload did; a passive read is refused for an open edit and the form would sit on "saving..." forever.
cases.a_write_from_an_open_form_reads_the_board_non_passively = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"] }), () => false).start();
  const getsBefore = env.gets.length;
  env.editing = true;                 // the form's own edit state: hasUnsavedEdit is true while it is open
  env.holdManifest();                 // the board read stays out: the write must not wait on it
  const timeout = new Promise((r) => setTimeout(() => r("waited"), 500));
  const r = await Promise.race([env.snooze(), timeout]);
  ok(r && r.ok === true, "the write answered without waiting on the board read: " + JSON.stringify(r));
  await env.tick();
  ok(env.manifestGets(getsBefore) === 1, "a board read was issued although an edit is open: " + JSON.stringify(env.gets.slice(getsBefore)));
  env.releaseManifest();
  await env.tick();
};

(async () => {
  for (const name of Object.keys(cases)) {
    if (only && name !== only) continue;
    const before = failed;
    try { await cases[name](); } catch (e) { failed++; console.log("FAIL " + name + " threw " + (e && e.stack || e)); }
    if (failed === before) console.log("OK " + name);
  }
  process.exit(failed ? 1 : 0);
})();
"""

CASES = [
    "touched_write_repaints_the_row_then_refetches_only_the_touched_panel",
    "a_snooze_into_the_future_hides_the_row_before_the_read_returns",
    "a_snooze_that_leaves_the_row_in_view_dims_it_with_a_title",
    "an_edit_of_the_text_shows_the_new_words_in_place",
    "an_edit_of_a_field_with_no_place_in_the_row_dims_it",
    "a_row_for_another_row_paints_nothing",
    "a_response_without_touched_runs_the_whole_board_path",
    "an_empty_touched_is_no_touched",
    "the_stream_announcing_a_refetched_panel_reads_it_once",
    "a_panel_that_cannot_repaint_in_place_asks_one_passive_board_read",
    "a_write_from_an_open_form_reads_the_board_non_passively",
]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("case", CASES)
def test_write_refetch_page_case(case: str, tmp_path: Path) -> None:
    drv = tmp_path / "write_refetch_driver.js"
    drv.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js"), case],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and f"OK {case}" in r.stdout, r.stdout + r.stderr
