"""levain#159: the freshness line on the page's REAL wiring (the boot shim's facts, the stream's facts, the view's heads), no painter seam.

test_cockpit_fresh_line.py drives the painter from faked facts. These tests break the page itself (an in-page `fetch` that fails or refuses a
manifest read after the board is drawn, a panel head that says stale) and read what the line says and what the page does about it, so a
renamed field or a unit slip between the three exporters and fresh_line.js, or a read that is never retried, fails here."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._ke_dom import WEB
from tests.test_cockpit_fresh_line import LIVE, _open, _vals
from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_places import READY, SETTLE

# The page's own fetch, wrapped in the page: the first `__fails` manifest reads fail (`__mode` "drop": the network, "401": a refused device),
# a panel head can be marked stale (a late collector), and the change stream can be refused so the page's own stream drops. `__reads` counts
# the manifest reads the PAGE makes, retries included. Every state the line shows is then the page's own code reading its own facts.
STUB2 = r"""(() => { const real = window.fetch.bind(window); window.__reads = 0; window.__mode = 'drop'; window.__fails = 0; window.__stale = null; window.__eventsDown = false;
  window.__delay = 0; window.__panelFail = null; window.__wbReads = 0;
  window.fetch = (u, o) => { const url = String(u);
    if (url.indexOf('/cockpit/events') >= 0 && window.__eventsDown) return Promise.reject(new TypeError('Load failed'));
    if (url.indexOf('/cockpit/panel/work_board') >= 0) { window.__wbReads++; if (window.__panelFail === 'work_board') return Promise.reject(new TypeError('Load failed')); }
    if (url.indexOf('/cockpit/manifest.json') < 0) return real(u, o);
    window.__reads++;
    const go = () => {
    if (window.__fails > 0) { window.__fails--; return window.__mode === '401' ? Promise.resolve(new Response('{"message":"nope"}', { status: 401, headers: { 'content-type': 'application/json' } })) : Promise.reject(new TypeError('Load failed')); }
    return real(u, o).then(async (r) => { if (!window.__stale || !r.ok || r.status === 304) return r;
      const m = await r.clone().json(), h = m.panels && m.panels[window.__stale];
      if (h) m.panels[window.__stale] = Object.assign({}, h, { status: 'stale', as_of: new Date(Date.now() - 180000).toISOString() });
      return new Response(JSON.stringify(m), { status: r.status, headers: r.headers }); }); };
    return window.__delay ? new Promise((r) => setTimeout(r, window.__delay)).then(go) : go(); };
  window.__panelTitle = () => document.querySelector('[data-panel-id="work_board"] .phead h2').textContent.trim();
  window.__hide = (on) => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => on }); Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => on ? 'hidden' : 'visible' });
    document.dispatchEvent(new Event('visibilitychange')); };
  window.__edit = (on) => { window.LevainDashboard.editHoldsRefresh = () => on; };
  return 1; })()"""
TOLD = "document.dispatchEvent(new CustomEvent('levain:fresh'))"     # what the stream says after it has applied a panel
# A server that has only just started makes the first page do one corrective whole-board read after the stream's hello (the watcher's first
# build is a new generation, levain#138). Every test below injects its failure into the NEXT manifest read, so that read must not be the
# startup one: wait for the page's own read to finish, then for the settled board (the shared step: live chip, no cockpit read in flight, started or finished, nothing drawn), then
# zero the counters so a test counts only what it caused.
QUIET = [{"until": "!window.LevainBootFacts().inflight && 1", "ms": 30000}, BOARD_SETTLED, {"eval": "(window.__reads = 0, window.__wbReads = 0, 1)"}]


# sample the page every 250 ms for `ms`: what the line says and what the shim's facts say
SAMPLE = r"""(ms) => new Promise((done) => { const out = [], t0 = Date.now(), l = document.getElementById('freshline');
  const one = () => { const b = window.LevainBootFacts(); out.push({ t: Date.now() - t0, reads: window.__reads, state: l.dataset.state, words: l.querySelector('.fl-state').textContent,
      age: l.querySelector('.fl-age').textContent, title: l.title, failed: b.failed, refused: b.refused, inflight: b.inflight, stamp: (document.getElementById('stamp') || {}).textContent });
    if (Date.now() - t0 >= ms) done(out); else setTimeout(one, 250); }; one(); })"""


def _run(expr: str) -> dict:
    return {"eval": "(" + expr + ")"}


def _sample(ms: int, before: str = "1") -> dict:
    return {"eval": "(async () => { " + before + "; const rows = await (" + SAMPLE + ")(" + str(ms) + "); return JSON.stringify({ probe: 'sample', rows }); })()"}


def _rows(out: dict) -> list[dict]:
    (g,) = [v for v in _vals(out) if v.get("probe") == "sample"]
    return g["rows"]


def _at(rows: list[dict], ms: int) -> dict:
    return min(rows, key=lambda r: abs(r["t"] - ms))


@needs_browser
def test_a_read_that_fails_after_the_board_is_drawn_is_read_again_until_one_lands(tmp_path: Path) -> None:
    """FIX-1: the cockpit has no Sync key, so a failed read of a DRAWN board must retry on its own (2 s, then 4 s), say so, and recover."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET,
             _sample(12000, "window.__fails = 2; window.LevainCockpitTransport.reload(); 1")]
    rows = _rows(_open(tmp_path, steps))
    first = _at(rows, 500)
    assert first["reads"] == 1 and first["failed"], first                                           # the read failed at once
    assert _at(rows, 2800)["reads"] >= 2, [(r["t"], r["reads"]) for r in rows]                         # a retry came on the first backoff step
    assert _at(rows, 3600)["state"] == "failed" and _at(rows, 3600)["words"] == "can't reach", _at(rows, 3600)
    assert "tried again" in _at(rows, 3600)["title"]                                                 # the title's promise is kept
    assert rows[-1]["reads"] == 3, [(r["t"], r["reads"]) for r in rows]                                # a third on the second step (it ended at the good one)
    assert rows[-1]["state"] == "live" and rows[-1]["failed"] == "", rows[-1]
    assert rows[-1]["reads"] == _at(rows, 11000)["reads"]                                            # and the chain stopped there


@needs_browser
def test_a_refusal_after_the_board_is_drawn_says_not_signed_in_and_stops(tmp_path: Path) -> None:
    """FIX-1: a refusal of a drawn board is not retried (a retry only asks again), shows 'not signed in', and the next foreground reads again."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET,
             _sample(9000, "window.__mode = '401'; window.__fails = 5; window.LevainCockpitTransport.reload(); 1"),
             _sample(4000, "window.__fails = 0; window.dispatchEvent(new Event('online')); document.dispatchEvent(new CustomEvent('levain:resume', { detail: {} })); 1")]
    out = _open(tmp_path, steps)
    first, second = [v["rows"] for v in _vals(out) if v.get("probe") == "sample"]
    assert _at(first, 500)["refused"] is True, _at(first, 500)
    assert first[-1]["state"] == "refused" and first[-1]["words"] == "not signed in", first[-1]
    assert first[-1]["reads"] == 1, [(r["t"], r["reads"]) for r in first]                              # no retry: one read, one refusal
    assert "pair this device again" in first[-1]["stamp"] and first[-1]["stamp"].startswith("not signed in"), first[-1]["stamp"]   # the remedy is on the page, not only in a title
    assert second[-1]["state"] == "live" and second[-1]["refused"] is False and second[-1]["stamp"] == "", second[-1]   # a foreground read lands again


def _title(rows_out: dict) -> str:
    (g,) = [v for v in _vals(rows_out) if v.get("probe") == "title"]
    return g["title"]


@needs_browser
def test_the_real_wiring_names_a_stale_head_a_failed_read_and_a_dropped_stream(tmp_path: Path) -> None:
    """L14: boot facts + stream facts + view heads, no seam. Each of the three exporters feeds the line a unit and a name; a mismatch fails here."""
    title = {"eval": "JSON.stringify({ probe: 'title', title: window.__panelTitle() })"}
    steps = [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, LIVE, SETTLE, _run(STUB2), *QUIET, title,
             _sample(5000, "window.__stale = 'work_board'; window.LevainCockpitTransport.reload(); 1"),                                  # 1: a stale head
             _sample(1500, "window.__stale = null; window.__r = null; window.LevainCockpitView.refetchPanel('work_board').then((r) => { window.__r = r; " + TOLD + "; }); 1"),   # 2: read fresh in place
             _sample(8000, "window.__fails = 2; window.LevainCockpitTransport.reload(); 1"),                                                  # 3: a failed read
             _sample(5000, "window.__eventsDown = true; window.__hide(true); window.__hide(false); 1"),                                       # 4: the stream drops
             _sample(9000, "window.__eventsDown = false; document.dispatchEvent(new CustomEvent('levain:resume', { detail: {} })); 1")]     # 5: and comes back
    out = _open(tmp_path, steps)
    stale, fresh, failed, down, back = [v["rows"] for v in _vals(out) if v.get("probe") == "sample"]
    name = _title(out)
    s = stale[-1]
    assert s["state"] == "stale" and s["words"] == f"{name} stale" and s["age"] in ("read 3m ago", "read 4m ago"), s     # a head's status and as_of reach the line
    assert "A panel's data is old" in s["title"] and name in s["title"], s
    r = fresh[-1]
    assert r["state"] == "live" and r["reads"] == stale[-1]["reads"], (r, stale[-1])                                      # a panel read fresh in place clears it, no whole read
    f = _at(failed, 4000)
    assert f["state"] == "failed" and f["words"] == "can't reach", f
    assert f["age"].startswith("updated ") and f["age"].endswith(" ago") or f["age"] == "updated just now", f             # aged from the last read
    assert failed[-1]["state"] == "live", failed[-1]
    d = down[-1]
    assert d["state"] == "down" and d["words"] == "stream down" and d["age"].startswith("updated "), d                        # the stream's own state and bytes
    assert back[-1]["state"] == "live", back[-1]


@needs_browser
def test_a_good_read_of_another_kind_ends_the_failure_text_and_the_retry_chain(tmp_path: Path) -> None:
    """FIX-5 (and FIX-1's end): the stamp's 'read failed' text and the retry stop at a panel applied in place, without a whole read."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET,
             _sample(600, "window.__fails = 1; window.LevainCockpitTransport.reload(); 1"),
             _sample(3500, "window.LevainCockpitView.refetchPanel('work_board').then(() => " + TOLD + "); 1")]
    out = _open(tmp_path, steps)
    failed, after = [v["rows"] for v in _vals(out) if v.get("probe") == "sample"]
    assert failed[-1]["stamp"].startswith("read failed: "), failed[-1]                                                  # the reason is on the page, not only in a title
    assert after[-1]["stamp"] == "" and after[-1]["state"] == "live", after[-1]                                         # a panel applied in place clears it
    assert after[-1]["reads"] == failed[-1]["reads"], (after[-1], failed[-1])                                           # and the chain stopped: no whole read followed


def _samples(out: dict) -> list[list[dict]]:
    return [v["rows"] for v in _vals(out) if v.get("probe") == "sample"]


REFUSE = "window.__mode = '401'; window.__fails = 1; window.LevainCockpitTransport.reload(); 1"


@needs_browser
def test_a_refusal_stands_until_a_read_answers_a_foreground_that_is_deferred_or_in_flight_does_not_unsay_it(tmp_path: Path) -> None:
    """A(a): the refusal ends at a read's answer, never at the request for one: a foreground read deferred for an edit makes no request, and one
    in flight has not answered, so neither may turn 'not signed in' into 'can't reach' (the failure text of a read that never refused)."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET, _sample(3600, REFUSE),
             _sample(1500, "window.__edit(true); document.dispatchEvent(new CustomEvent('levain:resume', { detail: {} })); 1"),
             _sample(4000, "window.__edit(false); window.__fails = 1; window.__mode = 'drop'; window.__delay = 1500; document.dispatchEvent(new CustomEvent('levain:resume', { detail: {} })); 1")]
    refused, deferred, inflight = _samples(_open(tmp_path, steps))
    assert refused[-1]["state"] == "refused", refused[-1]
    assert all(r["state"] == "refused" and r["reads"] == refused[-1]["reads"] for r in deferred), [(r["t"], r["state"], r["reads"]) for r in deferred]   # no request was made
    first = [r for r in inflight if r["t"] < 1200]
    assert first and all(r["state"] == "refused" for r in first), [(r["t"], r["state"]) for r in inflight]                  # in flight: not answered yet
    assert inflight[-1]["state"] == "failed" and inflight[-1]["refused"] is False, inflight[-1]                             # its own answer is a failure that is not a refusal


@needs_browser
def test_a_failure_ended_by_a_read_of_another_kind_is_not_read_again_by_the_next_online_event(tmp_path: Path) -> None:
    """A(b): the line, the shim's facts and the `online` handler read ONE derivation: a failure stands only while no read of any kind is newer."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET,
             _sample(600, "window.__fails = 1; window.LevainCockpitTransport.reload(); 1"),
             _sample(600, "window.LevainCockpitView.refetchPanel('work_board'); 1"),
             _sample(1800, "window.dispatchEvent(new Event('online')); 1")]
    failed, healed, after = _samples(_open(tmp_path, steps))
    assert failed[-1]["failed"], failed[-1]
    assert healed[-1]["failed"] == "" and healed[-1]["state"] == "live", healed[-1]          # the facts the line reads say it is over
    assert after[-1]["reads"] == healed[-1]["reads"], [(r["t"], r["reads"]) for r in after]     # so `online` starts no whole-board read


@needs_browser
def test_a_late_read_cannot_leave_the_stamp_and_the_line_telling_different_stories_about_a_refusal(tmp_path: Path) -> None:
    """A(c): after a refusal the stamp carries the remedy and the line says 'not signed in' for as long as the same derivation says the refusal stands."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET, _sample(3600, REFUSE),
             _sample(1500, "window.LevainCockpitView.refetchPanel('work_board').then(() => " + TOLD + "); 1")]       # TOLD: a caller that says so itself
    refused, later = _samples(_open(tmp_path, steps))
    assert refused[-1]["state"] == "refused" and refused[-1]["stamp"].startswith("not signed in"), refused[-1]
    for r in later:
        assert (r["state"] == "refused") == r["stamp"].startswith("not signed in"), r          # never one without the other


@needs_browser
def test_a_panel_that_could_not_be_read_at_the_whole_draw_is_named_and_read_again(tmp_path: Path) -> None:
    """B: a panel drawn as unavailable by a whole read is a failure the line names ('<Panel> unavailable') and the stream's bounded refetch reads again."""
    title = {"eval": "JSON.stringify({ probe: 'title', title: window.__panelTitle() })"}
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET, title,
             _sample(4500, "window.__panelFail = 'work_board'; window.__wbReads = 0; window.LevainCockpitTransport.reload(); 1"),
             _sample(9000, "window.__panelFail = null; 1")]
    out = _open(tmp_path, steps)
    named, healed = _samples(out)
    name = _title(out)
    assert named[-1]["state"] == "stale" and named[-1]["words"] == f"{name} unavailable", named[-1]
    assert healed[-1]["state"] == "live" and healed[-1]["words"] == "live", healed[-1]
    # it was read again on the stream's schedule: a panel drawn as unavailable cannot be redrawn in place, so the retry is one board read (bounded, not a loop)
    assert healed[-1]["reads"] == named[-1]["reads"] + 1, (healed[-1], named[-1])


@needs_browser
def test_a_panel_read_made_by_a_direct_caller_publishes_its_own_success_and_failure(tmp_path: Path) -> None:
    """B: refetchPanel is the ONE place that says a panel read settled; a caller that does not tell the line (a write's refetch, the topology's replay) is still heard."""
    title = {"eval": "JSON.stringify({ probe: 'title', title: window.__panelTitle() })"}
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET, title,
             _sample(4500, "document.querySelector('[data-panel-id=\"work_board\"] .phead h2').textContent += ' (unavailable)'; window.__panelFail = 'work_board'; window.LevainCockpitView.refetchPanel('work_board'); 1"),
             _sample(3500, "window.__panelFail = null; window.LevainCockpitView.refetchPanel('work_board'); 1")]
    out = _open(tmp_path, steps)
    failed, healed = _samples(out)
    # the heading of an unavailable panel ends in "(unavailable)" ("Work board (unavailable)"): the line names the panel, not that suffix
    assert failed[-1]["state"] == "stale" and failed[-1]["words"] == f"{_title(out)} stale", failed[-1]
    assert healed[-1]["state"] == "live", healed[-1]


@needs_browser
def test_a_parenthesis_that_is_part_of_a_panel_title_is_kept_in_the_line(tmp_path: Path) -> None:
    """A title's own parenthesis stays in the line (a year included); only the '(unavailable)' the heading adds is removed."""
    title = {"eval": "JSON.stringify({ probe: 'title', title: window.__panelTitle() })"}
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET, title,
             _sample(4500, "document.querySelector('[data-panel-id=\"work_board\"] .phead h2').textContent += ' (remote)'; window.__panelFail = 'work_board'; window.LevainCockpitView.refetchPanel('work_board'); 1")]
    out = _open(tmp_path, steps)
    (failed,) = _samples(out)
    assert failed[-1]["state"] == "stale" and failed[-1]["words"] == f"{_title(out)} (remote) stale", failed[-1]


@needs_browser
def test_a_title_that_ends_in_a_number_in_parentheses_keeps_it(tmp_path: Path) -> None:
    """The count is a pill beside the heading, never in its text, so a trailing '(2026)' is the title's own and only '(unavailable)' is stripped."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET,
             _sample(4500, "document.querySelector('[data-panel-id=\"work_board\"] .phead h2').textContent = 'Annual plan (2026)'; window.__panelFail = 'work_board'; window.LevainCockpitView.refetchPanel('work_board'); 1")]
    (failed,) = _samples(_open(tmp_path, steps))
    assert failed[-1]["state"] == "stale" and failed[-1]["words"] == "Annual plan (2026) stale", failed[-1]


@needs_browser
def test_a_failed_read_that_waits_on_an_open_edit_is_read_again_when_the_edit_closes(tmp_path: Path) -> None:
    """F: the line is honest while an edit holds the read ('can't reach', no request), and the read comes on its own once the edit is gone, with no event to say so."""
    steps = [READY, LIVE, SETTLE, _run(STUB2), *QUIET,
             _sample(4500, "window.__fails = 1; window.LevainCockpitTransport.reload(); window.__edit(true); 1"),
             _sample(13000, "window.__edit(false); 1")]
    held, freed = _samples(_open(tmp_path, steps))
    assert held[-1]["state"] == "failed" and held[-1]["reads"] == 1, [(r["t"], r["state"], r["reads"]) for r in held]       # honest, and no request while the edit is open
    assert freed[-1]["reads"] == 2 and freed[-1]["state"] == "live" and freed[-1]["failed"] == "", [(r["t"], r["state"], r["reads"]) for r in freed]


# ---------------------------------------------------------------------------------------------------------------- the stream's facts, in node
STREAM_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 10; i++) await tick(); };
function boot(opts) {
  const listeners = {}, fetches = [], manifests = [];
  const doc = { readyState: "complete", hidden: false, addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); }, dispatchEvent: () => true };
  const win = {
    LevainCockpitHeld: { a: { etag: "1" } }, location: { search: "" }, document: doc,
    setTimeout: () => ({}), clearTimeout: () => {}, setInterval: () => ({}), clearInterval: () => {},
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    CustomEvent: class { constructor(t) { this.type = t; } },
    fetch: (url) => {
      if (url === "/cockpit/manifest.json" && opts.manual) return new Promise((r) => manifests.push(r));    // answered by the test, in the order it chooses
      if (url === "/cockpit/manifest.json") return Promise.resolve({ ok: true, status: 200, headers: { get: () => "W/m" }, json: () => Promise.resolve({ panels: { a: { etag: "1", status: "stale", as_of: "2026-11-19T11:00:00Z" } } }) });
      const f = { push: null }; fetches.push(f); let wake = null; const q = [];
      f.push = (t) => { q.push(t); if (wake) { const w = wake; wake = null; w(); } };
      return Promise.resolve({ ok: true, status: 200, body: { getReader: () => ({ read: () => { const next = () => q.length ? Promise.resolve({ done: false, value: Buffer.from(q.shift()) }) : new Promise((r) => { wake = r; }).then(next); return next(); } }) } });
    },
  };
  win.LevainCockpitTransport = { headers: () => ({}), reload: () => Promise.resolve(false), readJson: () => Promise.resolve({}) };
  if (!opts.noView) win.LevainCockpitView = { refetchPanel: async () => "applied" };
  globalThis.window = win; globalThis.document = doc; globalThis.fetch = win.fetch; globalThis.AbortController = win.AbortController; globalThis.URLSearchParams = URLSearchParams;
  new Function("window", "document", src)(win, doc);
  return { win, fetches, manifests };
}
const frame = (event, id, data) => "id: " + id + "\nevent: " + event + "\ndata: " + JSON.stringify(data) + "\n\n";
(async () => {
  const out = {};
  const none = boot({ noView: true }); await settle();
  out.noStream = none.win.LevainStreamFacts().state;                                    // a page that never starts a stream has no stream state
  const h = boot({}); await settle();
  out.before = { state: h.win.LevainStreamFacts().state, everLive: h.win.LevainStreamFacts().everLive };
  h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false })); await settle();
  out.hello = { state: h.win.LevainStreamFacts().state, everLive: h.win.LevainStreamFacts().everLive, head: h.win.LevainStreamFacts().heads.a };
  // the page read the panel in place: its record carries the panel's own status and read time, and the stream's head follows it
  h.win.LevainCockpitHeld.a = { etag: "2", at: Date.now(), status: "ok", as_of: "2026-11-19T12:00:00Z" };
  h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" })); await settle();
  out.applied = h.win.LevainStreamFacts().heads.a;
  // two manifest reads overlap (a poll and a hello's): the one that STARTED later holds the newer heads whichever answers last
  const m = boot({ manual: true }); await settle();
  const manifest = (status, asOf) => ({ ok: true, status: 200, headers: { get: () => "W/m" }, json: () => Promise.resolve({ panels: { a: { etag: "1", status, as_of: asOf } } }) });
  m.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false })); await settle();
  m.fetches[0].push(frame("hello", "E.1:1", { epoch: "E.1", resync: false })); await settle();
  m.manifests[1](manifest("ok", "2026-11-19T12:05:00Z")); await settle();
  m.manifests[0](manifest("stale", "2026-11-19T11:00:00Z")); await settle();
  out.overlap = { reads: m.manifests.length, head: m.win.LevainStreamFacts().heads.a };
  console.log(JSON.stringify(out));
})();
"""


def test_the_stream_facts_say_no_stream_when_none_runs_and_follow_a_panel_read_in_place(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    drv = tmp_path / "stream_facts_driver.js"
    drv.write_text(STREAM_DRIVER, encoding="utf-8")
    r = subprocess.run([node, str(drv), str(WEB / "cockpit_stream.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["noStream"] is None                                                       # the line then says nothing about a stream
    assert got["before"] == {"state": "connecting", "everLive": False}
    assert got["hello"]["state"] == "live" and got["hello"]["everLive"] is True
    assert got["hello"]["head"]["status"] == "stale"                                    # the hello's manifest read is the stream's heads
    assert got["applied"]["status"] == "ok" and got["applied"]["as_of"] == "2026-11-19T12:00:00Z", got["applied"]   # a panel read in place clears its stale head
    assert got["overlap"]["reads"] == 2 and got["overlap"]["head"]["status"] == "ok", got["overlap"]              # an older read answering last does not overwrite newer heads


STAMP = "((document.getElementById('stamp') || {}).textContent || '')"
TOKEN_REFUSED_INIT = ("window.prompt = () => null; window.__rej = 999; const real = window.fetch.bind(window);"
                      "window.fetch = (u, o) => String(u).indexOf('/substrate.json') >= 0 && window.__rej > 0"
                      " ? Promise.resolve(new Response('{\"message\":\"bad token\"}', { status: 403, headers: { 'content-type': 'application/json' } })) : real(u, o);")


@needs_browser
def test_a_token_refusal_on_the_stamp_ends_with_the_refusal_when_a_read_of_another_kind_lands(tmp_path: Path) -> None:
    """A wrong or rotated off-box token writes 'read refused' on the stamp; the stamp is a failure text like any other, so a good read that
    lands without a whole draw (the stream's manifest poll) ends it where it ends the line's refusal, instead of leaving it beside a live line."""
    _root, src, _ids = _install(tmp_path)
    steps = [{"init": TOKEN_REFUSED_INIT},
             {"until": STAMP + ".startsWith('read refused')", "ms": 20000},
             {"eval": STAMP},
             {"eval": "(() => { window.__rej = 0; window.LevainStreamFacts = () => ({ pollOkAt: Date.now() + 60000 }); " + TOLD + "; return " + STAMP + "; })()"}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?source=substrate#write_token=T", steps)
    refused, after = out["values"][-2], out["values"][-1]
    assert refused.startswith("read refused"), refused
    assert after == "", after


# the retry of a panel unreadable at the whole draw (no live node): one request of its own, never a manifest and every panel again
UNREAD_RETRY = r"""(async () => {
  const wait = async (f, ms) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (f()) return true; await new Promise((r) => setTimeout(r, 50)); } return false; };
  const unavailable = () => { const h = (window.LevainCockpitHeld || {}).work_board; return !!(h && h.err) && !!document.querySelector('[data-panel-id="work_board"] .err'); };
  window.__panelFail = 'work_board'; window.LevainCockpitTransport.reload();
  const drawn = await wait(unavailable, 8000);
  const log = [], real = window.fetch; let fresh = 0;
  window.fetch = (u, o) => { log.push(String(u)); return real(u, o); };
  document.addEventListener('levain:fresh', () => { fresh++; });
  const route = (l) => l.map((u) => u.replace(/^https?:\/\/[^/]+/, '').split('?')[0]);
  const first = await window.LevainCockpitView.refetchPanel('work_board');
  const failedLog = route(log.splice(0)), failedFresh = fresh; fresh = 0;
  window.__panelFail = null;
  const second = await window.LevainCockpitView.refetchPanel('work_board');
  const healedLog = route(log.splice(0));
  const stillUnavailable = unavailable();
  window.LevainCockpitTransport.reload();
  const mounted = await wait(() => !unavailable(), 8000);
  const reloadLog = route(log.splice(0));
  window.fetch = real;
  return JSON.stringify({ probe: 'unread', drawn, first, failedLog, failedFresh, second, healedLog, stillUnavailable, mounted, reloadLog });
})()"""


@needs_browser
def test_the_retry_of_a_panel_unreadable_at_the_whole_draw_is_one_read_of_that_panel_not_a_whole_reload(tmp_path: Path) -> None:
    """A panel the whole read could not read has no live node. Its retry through refetchPanel is ONE request, to its own route (answered "fault", which the
    stream bounds without a reload); answering false with no read made every retry step a whole reload of the board. A read that lands answers false."""
    out = _open(tmp_path, [READY, LIVE, SETTLE, _run(STUB2), *QUIET, {"eval": UNREAD_RETRY}])
    (g,) = [v for v in _vals(out) if v.get("probe") == "unread"]
    assert g["drawn"] is True, g
    assert g["first"] == "fault", g
    assert len(g["failedLog"]) == 1 and not any("manifest" in u for u in g["failedLog"]), g["failedLog"]   # one read, no manifest, no other panel
    assert g["failedFresh"] == 1, g                                                                          # the line is told once
    assert g["second"] is False and g["stillUnavailable"] is True, g                                          # a read that lands: only a whole draw mounts it
    assert len(g["healedLog"]) == 1 and "work_board" in g["healedLog"][0], g["healedLog"]
    assert g["mounted"] is True, g
    assert sum("manifest" in u for u in g["reloadLog"]) == 1, g["reloadLog"]                                  # the one whole reload draws it
