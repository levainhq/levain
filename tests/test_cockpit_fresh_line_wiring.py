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
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_places import READY, SETTLE

# The page's own fetch, wrapped in the page: the first `__fails` manifest reads fail (`__mode` "drop": the network, "401": a refused device),
# a panel head can be marked stale (a late collector), and the change stream can be refused so the page's own stream drops. `__reads` counts
# the manifest reads the PAGE makes, retries included. Every state the line shows is then the page's own code reading its own facts.
STUB2 = r"""(() => { const real = window.fetch.bind(window); window.__reads = 0; window.__mode = 'drop'; window.__fails = 0; window.__stale = null; window.__eventsDown = false;
  window.fetch = (u, o) => { const url = String(u);
    if (url.indexOf('/cockpit/events') >= 0 && window.__eventsDown) return Promise.reject(new TypeError('Load failed'));
    if (url.indexOf('/cockpit/manifest.json') < 0) return real(u, o);
    window.__reads++;
    if (window.__fails > 0) { window.__fails--; return window.__mode === '401' ? Promise.resolve(new Response('{"message":"nope"}', { status: 401, headers: { 'content-type': 'application/json' } })) : Promise.reject(new TypeError('Load failed')); }
    return real(u, o).then(async (r) => { if (!window.__stale || !r.ok || r.status === 304) return r;
      const m = await r.clone().json(), h = m.panels && m.panels[window.__stale];
      if (h) m.panels[window.__stale] = Object.assign({}, h, { status: 'stale', as_of: new Date(Date.now() - 180000).toISOString() });
      return new Response(JSON.stringify(m), { status: r.status, headers: r.headers }); }); };
  window.__panelTitle = () => document.querySelector('[data-panel-id="work_board"] .phead h2').textContent.trim();
  window.__hide = (on) => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => on }); Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => on ? 'hidden' : 'visible' });
    document.dispatchEvent(new Event('visibilitychange')); };
  return 1; })()"""
TOLD = "document.dispatchEvent(new CustomEvent('levain:fresh'))"     # what the stream says after it has applied a panel


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
    steps = [READY, LIVE, SETTLE, _run(STUB2),
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
    steps = [READY, LIVE, SETTLE, _run(STUB2),
             _sample(9000, "window.__mode = '401'; window.__fails = 5; window.LevainCockpitTransport.reload(); 1"),
             _sample(4000, "window.__fails = 0; window.dispatchEvent(new Event('online')); document.dispatchEvent(new CustomEvent('levain:resume', { detail: {} })); 1")]
    out = _open(tmp_path, steps)
    first, second = [v["rows"] for v in _vals(out) if v.get("probe") == "sample"]
    assert _at(first, 500)["refused"] is True, _at(first, 500)
    assert first[-1]["state"] == "refused" and first[-1]["words"] == "not signed in", first[-1]
    assert first[-1]["reads"] == 1, [(r["t"], r["reads"]) for r in first]                              # no retry: one read, one refusal
    assert "pair it again" in first[-1]["stamp"] and first[-1]["stamp"].startswith("not signed in"), first[-1]["stamp"]   # the remedy is on the page, not only in a title
    assert second[-1]["state"] == "live" and second[-1]["refused"] is False and second[-1]["stamp"] == "", second[-1]   # a foreground read lands again


def _title(rows_out: dict) -> str:
    (g,) = [v for v in _vals(rows_out) if v.get("probe") == "title"]
    return g["title"]


@needs_browser
def test_the_real_wiring_names_a_stale_head_a_failed_read_and_a_dropped_stream(tmp_path: Path) -> None:
    """L14: boot facts + stream facts + view heads, no seam. Each of the three exporters feeds the line a unit and a name; a mismatch fails here."""
    title = {"eval": "JSON.stringify({ probe: 'title', title: window.__panelTitle() })"}
    steps = [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, LIVE, SETTLE, _run(STUB2), title,
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
    steps = [READY, LIVE, SETTLE, _run(STUB2),
             _sample(600, "window.__fails = 1; window.LevainCockpitTransport.reload(); 1"),
             _sample(3500, "window.LevainCockpitView.refetchPanel('work_board').then(() => " + TOLD + "); 1")]
    out = _open(tmp_path, steps)
    failed, after = [v["rows"] for v in _vals(out) if v.get("probe") == "sample"]
    assert failed[-1]["stamp"].startswith("read failed: "), failed[-1]                                                  # the reason is on the page, not only in a title
    assert after[-1]["stamp"] == "" and after[-1]["state"] == "live", after[-1]                                         # a panel applied in place clears it
    assert after[-1]["reads"] == failed[-1]["reads"], (after[-1], failed[-1])                                           # and the chain stopped: no whole read followed


# ---------------------------------------------------------------------------------------------------------------- the stream's facts, in node
STREAM_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 10; i++) await tick(); };
function boot(opts) {
  const listeners = {}, fetches = [];
  const doc = { readyState: "complete", hidden: false, addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); }, dispatchEvent: () => true };
  const win = {
    LevainCockpitHeld: { a: { etag: "1" } }, location: { search: "" }, document: doc,
    setTimeout: () => ({}), clearTimeout: () => {}, setInterval: () => ({}), clearInterval: () => {},
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    CustomEvent: class { constructor(t) { this.type = t; } },
    fetch: (url) => {
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
  return { win, fetches };
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
