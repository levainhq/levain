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
const head = (id, kind, extra) => Object.assign({ id, kind, title: id, status: "ok", as_of: "2026-10-12T12:00:00+00:00" }, extra || {});

function fixture() {
  const manifest = { schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} },
    generated_at: "2026-10-12T12:00:00+00:00", regions: { header: [], zones: [{ id: "operate", title: "Operate", panels: ["tray", "inbox"] }] },
    panels: { tray: head("tray", "list"), inbox: head("inbox", "list") },
    verbs: { intention_surface_at: { label: "Snooze", tier: "T1", confirm: false, idempotent: false, job: false, fields: ["date"], effect: "moves the date", gesture: "direct" } },
    errors: [], transport: { writable: true, write_token_required: false } };
  const row = (id, actions) => ({ id, version: "v1", title: id, body: id, group: null, badges: [], facets: {}, actions });
  const panels = {
    tray: Object.assign(head("tray", "list"), { rows: [row("intention:i1", [SNOOZE])], actions: [] }),
    inbox: Object.assign(head("inbox", "list"), { rows: [row("m1", [])], actions: [] }),
  };
  return { manifest, panels };
}

function makeEnv(verbAnswer, refetchAnswer) {
  const fx = fixture();
  const posts = [], gets = [], renders = [], marks = [];
  const store = () => { const m = {}; return { getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; } }; };
  let n = 0, gate = null, gated = false;
  const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body, text: async () => JSON.stringify(body), headers: { get: () => null }, clone() { return this; } });
  async function fetchFn(url, init) {
    if (init && init.method === "POST") {
      const body = JSON.parse(init.body);
      posts.push({ url, body });
      return resp(200, verbAnswer(body));
    }
    gets.push(url);
    if (url.indexOf("/cockpit/manifest.json") >= 0) return resp(200, fx.manifest);
    const m = /\/cockpit\/panel\/([^?]+)\.json/.exec(url);
    const pid = m ? decodeURIComponent(m[1]) : null;
    if (pid && fx.panels[pid]) return resp(200, fx.panels[pid]);
    return resp(404, {});
  }
  const rowEl = { dataset: { rowKey: "intention:i1" }, classList: { add: (c) => marks.push(c) }, querySelectorAll: () => [], querySelector: () => null, appendChild() {} };
  const ls = store(); ls.setItem("levain_write_token", "tok");
  const env = { posts, gets, renders, marks, rowEl, refetchCalls: [], releaseGate: () => {}, settled: [] };
  const V = Object.assign({}, View, {
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
    LevainDashboard: { render: (view, opts) => renders.push({ view, opts }), hasUnsavedEdit: () => false, jobsActive: () => false }, dispatchEvent() {} };
  const doc = { getElementById: () => null, addEventListener() {}, querySelectorAll: (sel) => (sel === "[data-row-key]" ? [rowEl] : []),
                createElement: () => ({ className: "", textContent: "" }), visibilityState: "visible", body: { dataset: {} } };
  env.V = V;
  env.hold = () => { gated = true; gate = new Promise((r) => { env.releaseGate = r; }); };
  env.start = async () => {
    new Function("window", "document", "location", "history", "fetch", "console", bootSrc)(win, doc, win.location, { replaceState() {} }, fetchFn, console);
    await new Promise((r) => setTimeout(r, 30));
    return env;
  };
  env.opts = () => renders[renders.length - 1].opts;
  env.snooze = () => env.opts().commit({ kind: "intention_surface_at", intention_id: "i1", date: "2026-11-25" });
  env.tick = () => new Promise((r) => setTimeout(r, 20));
  return env;
}

let failed = 0;
function ok(cond, what) { if (!cond) { failed++; console.log("FAIL " + what); } }
const isManifest = (u) => u.indexOf("/cockpit/manifest.json") >= 0;
const cases = {};

cases.touched_write_repaints_the_row_then_refetches_only_the_touched_panel = async () => {
  const env = await makeEnv(() => ({ ok: true, state: "COMMITTED", touched: ["tray"], row: { row_version: "v2", next: "2026-11-25" } }), () => "applied").start();
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
  ok(env.rowEl.dataset.rowVersion === "v2" && env.rowEl.dataset.rowNext === "2026-11-25", "the row carries the response's version and next: " + JSON.stringify(env.rowEl.dataset));
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
    "a_response_without_touched_runs_the_whole_board_path",
    "an_empty_touched_is_no_touched",
    "the_stream_announcing_a_refetched_panel_reads_it_once",
    "a_panel_that_cannot_repaint_in_place_asks_one_passive_board_read",
]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("case", CASES)
def test_write_refetch_page_case(case: str, tmp_path: Path) -> None:
    drv = tmp_path / "write_refetch_driver.js"
    drv.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js"), case],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and f"OK {case}" in r.stdout, r.stdout + r.stderr
