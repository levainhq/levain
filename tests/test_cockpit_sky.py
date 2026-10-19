"""The ambient sky (``sky.js``): served as an asset, rows modelled correctly, and its frame loop comes to rest.

The model checks run under node; the rest-state proof drives headless Chrome over CDP and is skipped
where no Chrome is installed (the screenshots are saved by ``tests/sky_replay/run_cdp.mjs`` given an outdir)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._page_drive_run import run as run_driver

ROOT = Path(__file__).resolve().parents[1]
SKY = ROOT / "levain" / "templates" / "web" / "sky.js"
REPLAY = ROOT / "tests" / "sky_replay"
NODE = shutil.which("node")
CHROME = next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                           shutil.which("google-chrome") or "", shutil.which("chromium") or "") if p and Path(p).exists()), None)


def _model(events: list[dict], opts: dict) -> dict:
    js = ("const {buildModel}=require(process.argv[1]);const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "const ev=i.events.map(e=>({t:Date.parse(e.at),kind:e.kind,repo:e.repo,agent:e.agent,ok:e.outcome!=='failed'}));"
          "const m=buildModel(ev,Object.assign({windowHours:14,topRows:5,pinnedRows:[],single:''},i.opts),i.now);"
          "console.log(JSON.stringify({rows:m.rows,single:m.single,counts:m.counts}));")
    out = subprocess.run([NODE, "-e", js, str(SKY)], input=json.dumps({"events": events, "opts": opts, "now": 1_000_000_000_000}),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _ev(kind: str, repo: str | None, mins_ago: int = 5, outcome: str = "ok") -> dict:
    import datetime as d
    at = d.datetime.fromtimestamp(1_000_000_000 - mins_ago * 60, d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"kind": kind, "at": at, "repo": repo, "agent": None, "outcome": outcome}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_rows_are_top_n_plus_other_with_folded_repos_and_no_repo_reviews():
    events = [_ev("commit", f"r{i}") for i in range(7) for _ in range(i + 1)] + [_ev("review", None)]
    m = _model(events, {"topRows": 3})
    ids = [r["id"] for r in m["rows"]]
    assert ids == ["r:r6", "r:r5", "r:r4", "other", "memory", "decisions"]
    other = m["rows"][3]
    assert other["label"] == "other" and other["noRepo"] == 1 and set(other["folded"]) == {"r0", "r1", "r2", "r3"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pinned_rows_replace_the_busiest_and_a_single_repo_rows_are_event_types():
    m = _model([_ev("commit", "a"), _ev("commit", "b")], {"pinnedRows": ["b"]})
    assert [r["id"] for r in m["rows"]][:2] == ["r:b", "other"]
    s = _model([_ev("commit", "only"), _ev("review", "only")], {})
    assert s["single"] == "only" and [r["label"] for r in s["rows"]] == ["commits", "reviews", "memory", "decisions"]


@pytest.mark.skipif(NODE is None or CHROME is None, reason="node or headless Chrome not installed")
def test_frame_loop_stops_after_the_last_meteor_settles():
    run = run_driver([NODE, str(REPLAY / "run_cdp.mjs"), CHROME], timeout=120)
    assert run.returncode == 0, "run_cdp.mjs failed:\n" + run.stdout[-2000:] + "\n" + run.stderr[-2000:]
    out = run.stdout
    lines = [ln for ln in out.splitlines() if "RESULT" in ln]
    assert len(lines) == 3, out
    for ln in lines:
        res = json.loads(ln.split("RESULT ", 1)[1])
        assert res["pass"], res


def _node(js: str, payload: dict) -> dict:
    out = subprocess.run([NODE, "-e", js, str(SKY)], input=json.dumps(payload), capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_at_without_an_offset_is_read_as_utc_whatever_the_browser_zone():
    js = ("const {normalise}=require(process.argv[1]);const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "console.log(JSON.stringify(i.map(a=>normalise({events:[{kind:'commit',at:a}]})[0].t)));")
    ats = ["2026-10-11T06:10:12", "2026-10-11 06:10:12", "2026-10-11T06:10", "2026-10-11T06:10:12Z", "2026-10-11T02:10:12-04:00"]
    for tz in ("America/New_York", "Asia/Tokyo"):
        out = subprocess.run([NODE, "-e", js, str(SKY)], input=json.dumps(ats), capture_output=True, text=True, check=True,
                             env={"TZ": tz, "PATH": "/usr/bin:/bin"}).stdout
        t = json.loads(out)
        assert t[0] == t[1] == t[3] == t[4] == 1791699012000 and t[2] == t[0] - 12000, (tz, t)


_FRESH = ("const {normalise,diffFresh}=require(process.argv[1]);const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "const st={seen:{},loaded:false};"
          "console.log(JSON.stringify(i.feeds.map(f=>diffFresh(normalise({events:f}),st).length)));")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_first_feed_is_history_even_when_empty_so_the_first_live_event_is_fresh():
    c = _ev("commit", "a")
    assert _node(_FRESH, {"feeds": [[], [c], [c]]}) == [0, 1, 0]
    assert _node(_FRESH, {"feeds": [[c], [c]]}) == [0, 0]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_equal_events_count_as_occurrences_and_an_outcome_change_is_fresh():
    c = _ev("commit", "a")
    assert _node(_FRESH, {"feeds": [[c], [c, dict(c), dict(c)]]}) == [0, 2]
    ok, bad = _ev("review", "a", outcome="ok"), _ev("review", "a", outcome="failed")
    assert _node(_FRESH, {"feeds": [[ok], [bad]]}) == [0, 1]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_repo_less_review_is_left_out_of_the_auto_single_repo_view():
    m = _model([_ev("commit", "only"), _ev("review", None, outcome="failed")], {})
    assert m["single"] == "only" and m["counts"]["review"] == 0 and m["counts"]["failed"] == 0


# ── the Levain marks: lanes, tracks, strip, empty window, tooltips; and flow's feed unchanged ──

_NOW = "2026-10-19T19:13:00Z"
_LEV_JS = ("const {buildModel,normalise,eventTip}=require(process.argv[1]);const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
           "const now=Date.parse(i.now);const ev=normalise({events:i.events});"
           "const m=buildModel(ev,Object.assign({windowHours:14,topRows:5,pinnedRows:[],single:''},i.opts),now);"
           "const seen={};const ids=ev.map(e=>{const b=e.kind+'@'+new Date(e.t).toISOString().slice(11,16);seen[b]=(seen[b]||0)+1;return seen[b]>1?b+'#'+seen[b]:b;});"
           "const by=f=>Object.fromEntries(ev.map((e,i)=>[ids[i],f(e)]));"
           "console.log(JSON.stringify({rows:m.rows.map(r=>r.id),lev:m.lev,empty:m.empty,tracks:m.tracks,counts:m.counts,"
           "where:by(e=>m.rowOf(e)),track:by(e=>m.trackOf.has(e)?m.trackOf.get(e):null),overflow:m.overflow,waiting:m.waiting.length,"
           "written:ev.filter(e=>'track' in e).length,tips:by(e=>eventTip(e,now))}));")


def _lev(events: list[dict], opts: dict | None = None) -> dict:
    return _node(_LEV_JS, {"events": events, "opts": opts or {}, "now": _NOW})


def _at(hm: str) -> str:
    return f"2026-10-19T{hm}:00Z"


def _e(kind, hm, repo=None, agent=None, outcome="ok", **extra) -> dict:
    return {"kind": kind, "at": _at(hm), "repo": repo, "agent": agent, "outcome": outcome, **extra}


_LEVAIN_FEED = [
    _e("commit", "14:00", "ledgerline"), _e("commit", "15:00", "tally"),
    _e("session", "12:00", "ledgerline", "claude_code", "ok", end=_at("13:00")),
    _e("session", "12:30", "tally", "codex", "live", end=_at("19:13")),
    _e("session", "13:30", "tally", "codex", "ok", end=_at("14:00")),
    _e("decision", "16:00", None, "claude_code", "waiting", label="Bash git push origin main"),
    _e("decision", "16:30", None, "claude_code", "denied", label="Bash rm -rf build/"),
    _e("memory", "15:30", None, "claude_code", "ok", label="observation"),
    _e("question", "17:00", None, "claude_code", "open", label="Run the backfill now?"),
]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_levain_feed_gets_repo_lanes_then_sessions_decisions_memory_questions():
    m = _lev(_LEVAIN_FEED)
    assert m["lev"] is True
    assert m["rows"] == ["r:ledgerline", "r:tally", "sessions", "decisions", "memory", "questions"]
    w = m["where"]
    assert (w["commit@14:00"], w["commit@15:00"], w["session@12:00"], w["decision@16:00"], w["memory@15:30"], w["question@17:00"]) == (
        "r:ledgerline", "r:tally", "sessions", "decisions", "memory", "questions")
    c = m["counts"]
    assert (c["commit"], c["sessions"], c["decisions"], c["memory"], c["questions"], c["waiting"]) == (2, 3, 2, 1, 1, 2)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_overlapping_sessions_take_separate_tracks_and_a_later_one_reuses_the_first():
    m = _lev(_LEVAIN_FEED)
    assert m["tracks"] == 2
    # 12:00-13:00 track 0; 12:30 (live) overlaps it -> track 1; 13:30 starts after track 0 ended (+6 min gap) -> track 0
    t = m["track"]
    assert (t["session@12:00"], t["session@12:30"], t["session@13:30"]) == (0, 1, 0)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_flow_shaped_feed_keeps_flow_rows_and_a_decision_outcome_ok_is_not_a_levain_decision():
    flow = [_e("commit", "15:00", "a"), _e("commit", "15:10", "b"), _e("review", "15:20", "a", "codex", "failed"),
            _e("memory", "15:30"), _e("decision", "15:40", None, "flow", "ok")]
    m = _lev(flow)
    assert m["lev"] is False
    assert m["rows"] == ["r:a", "r:b", "memory", "decisions"]
    assert [m["where"][k] for k in ("commit@15:00", "commit@15:10", "review@15:20", "memory@15:30", "decision@15:40")] == ["r:a", "r:b", "r:a", "memory", "decisions"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_strip_merges_the_repos_and_drops_lanes_nothing_landed_on():
    m = _lev(_LEVAIN_FEED, {"size": "strip"})
    assert m["rows"] == ["commit", "sessions", "memory", "decisions"]
    assert m["where"]["commit@14:00"] == m["where"]["commit@15:00"] == "commit"
    assert m["where"]["question@17:00"] == m["where"]["decision@16:00"] == "decisions"   # the question shares the decisions lane
    only = _lev([_e("commit", "15:00", "a")], {"size": "strip"})
    assert only["rows"] == ["commit"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_empty_window_is_flagged_so_the_page_can_say_so_and_keeps_its_lanes():
    m = _lev([])
    assert m["empty"] is True and m["rows"] == ["memory", "decisions"]   # flow's bare frame, as before
    old = _lev([_e("commit", "01:00", "a")])   # 18 h before now: outside the 14 h window
    assert old["empty"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_session_that_began_before_the_window_but_is_live_is_still_drawn():
    m = _lev([_e("session", "03:00", "a", "claude_code", "live", end=_at("19:13"))])
    assert m["empty"] is False and "sessions" in m["rows"] and m["counts"]["sessions"] == 1


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_tooltips_name_kind_repo_and_time_and_use_the_label_when_there_is_one():
    import datetime as d
    m = _lev(_LEVAIN_FEED)
    hhmm = lambda hm: d.datetime.fromisoformat(f"2026-10-19T{hm}:00+00:00").astimezone().strftime("%H:%M")   # the page shows local time
    assert m["tips"]["commit@14:00"] == f"commit · ledgerline · {hhmm('14:00')}"
    assert m["tips"]["decision@16:00"] == f"decision · Bash git push origin main · waiting · {hhmm('16:00')}"
    assert m["tips"]["decision@16:30"] == f"decision · Bash rm -rf build/ · denied · {hhmm('16:30')}"
    assert m["tips"]["memory@15:30"] == f"memory write · observation · {hhmm('15:30')}"
    assert m["tips"]["question@17:00"] == f"question · Run the backfill now? · open · {hhmm('17:00')}"
    assert m["tips"]["session@12:30"] == f"session · codex · tally · {hhmm('12:30')}–now"
    flow = _lev([_e("review", "15:00", "a", "codex", "failed")])["tips"]["review@15:00"]
    assert flow.startswith("review · a · codex · failed · ")   # flow's own tooltip is unchanged


# ── the fix round: a fake canvas runs sky.js whole (flow draws as before, strip, aria, theme, rest state) ──

_FAKE = r"""
const fs = require('fs'), cp = require('child_process');
const I = JSON.parse(fs.readFileSync(0, 'utf8'));
const all = [], log = [], mqs = [];
let observers = [];
function rnd(a) { return typeof a === 'number' ? Math.round(a * 100) / 100 : (typeof a === 'string' ? a : (a && a.__grad ? 'grad' : typeof a)); }
const ctx = new Proxy({}, {
  get(t, k) {
    if (k in t) return t[k];
    if (k === 'measureText') return s => ({ width: String(s).length * 6 });
    if (k === 'createLinearGradient') return (...a) => { log.push(['createLinearGradient', ...a.map(rnd)]); return { __grad: 1, addColorStop(...b) { log.push(['stop', ...b.map(rnd)]); } }; };
    return (...a) => { log.push([k, ...a.map(rnd)]); };
  },
  set(t, k, v) { t[k] = v; log.push(['=' + k, rnd(v)]); return true; },
});
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.style = {}; this.children = []; this.listeners = {}; this._text = ''; this.hidden = false; this.offsetWidth = 0; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'class') this.className = v; }
  append(...n) { n.forEach(x => this.children.push(x)); }
  appendChild(n) { this.children.push(n); return n; }
  insertBefore(n) { this.children.push(n); return n; }
  replaceChildren(...n) { this.children = n; }
  remove() {}
  querySelectorAll() { return []; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent || '').join(''); }
  getBoundingClientRect() { return { width: 800, height: parseFloat(this.style.height) || 320, left: 0, top: 0 }; }
  getContext() { return ctx; }
}
global.document = { createElement(tag) { const e = new El(tag); all.push(e); return e; }, createTextNode(t) { return { textContent: String(t) }; }, documentElement: new El('html') };
global.window = global;
global.addEventListener = () => {}; global.removeEventListener = () => {};
global.devicePixelRatio = I.dpr || 1;
global.getComputedStyle = () => ({ getPropertyValue: n => (I.tokens || {})[n] || '' });
global.matchMedia = () => { const m = { matches: false, n: 0, addEventListener() { mqs.push(1); }, removeEventListener() { mqs.pop(); } }; return m; };
if (I.observer) global.MutationObserver = class { constructor(cb) { this.cb = cb; this.live = true; observers.push(this); } observe() {} disconnect() { this.live = false; } };
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};
global.setInterval = () => 0; global.clearInterval = () => {};
const sky = require(I.sky);
const NOW = Date.parse(I.now);
const mk = ev => ({ events: ev, legend: 'LEGEND' });
const h = sky.mount(new El('div'), mk(I.feeds[0]), Object.assign({ now: () => NOW }, I.opts || {}));
const out = {};
for (let i = 1; i < I.feeds.length; i++) h.update(mk(I.feeds[i]));
const cvEl = all.find(e => e.tag === 'canvas');
out.log = log.slice();
out.aria = cvEl.attrs['aria-label'];
out.fillText = log.filter(l => l[0] === 'fillText').map(l => l[1]);
out.bar = all.find(e => e.attrs['class'] === 'sky-bar');
out.barDisplay = out.bar.style.display || '';
out.summary = all.find(e => e.attrs['class'] === 'sky-summary').textContent;
out.fillRects = log.filter(l => l[0] === 'fillRect').length;
out.pendingBefore = sky.stats.pending;
out.nowStops = null;
if (I.theme) { const n = log.length; observers.forEach(o => o.cb()); out.repainted = log.length > n; }
h.destroy();
out.pendingAfter = sky.stats.pending;
out.observersLive = observers.filter(o => o.live).length;
out.mqLeft = mqs.length;
delete out.bar;
console.log(JSON.stringify(out));
"""


def _fake(tmp_path: Path, sky: Path, feeds: list, now: str = _NOW, **kw) -> dict:
    script = tmp_path / "fake_sky.cjs"
    script.write_text(_FAKE)
    payload = {"sky": str(sky), "feeds": feeds, "now": now, **kw}
    out = subprocess.run([NODE, str(script)], input=json.dumps(payload), capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


_FLOW_FEED = [_e("commit", "15:00", "a"), _e("commit", "15:10", "b"), _e("commit", "18:30", "a"), _e("review", "15:20", "a", "codex", "failed"),
              _e("review", "16:20", "b", "codex", "ok"), _e("memory", "15:30"), _e("decision", "15:40", None, "flow", "ok")]


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("opts", [{}, {"single": "a"}, {"windowHours": 6}])
def test_a_flow_feed_draws_exactly_what_cockpit_ks_sky_drew(tmp_path: Path, opts: dict) -> None:
    old = tmp_path / "old_sky.js"
    shown = subprocess.run(["git", "-C", str(ROOT), "show", "origin/cockpit-k:levain/templates/web/sky.js"], capture_output=True, text=True)
    if shown.returncode:
        pytest.skip("origin/cockpit-k is not fetched here")
    old.write_text(shown.stdout)
    new = _fake(tmp_path, SKY, [_FLOW_FEED], opts=opts)
    was = _fake(tmp_path, old, [_FLOW_FEED], opts=opts)
    assert len(was["log"]) > 50 and new["log"] == was["log"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_destroy_leaves_no_frame_pending_after_a_meteor_launched(tmp_path: Path) -> None:
    r = _fake(tmp_path, SKY, [_LEVAIN_FEED, _LEVAIN_FEED + [_e("commit", "19:10", "tally")]])
    assert r["pendingBefore"] == 1 and r["pendingAfter"] == 0


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_strip_builds_no_summary_hides_the_bar_inline_and_says_nothing_yet_when_empty(tmp_path: Path) -> None:
    r = _fake(tmp_path, SKY, [_LEVAIN_FEED], opts={"size": "strip"})
    assert r["barDisplay"] == "none" and r["summary"] == ""
    e = _fake(tmp_path, SKY, [[]], opts={"size": "strip"})
    assert e["fillText"] == ["Nothing yet"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_empty_window_is_text_only_with_no_box_over_the_grid(tmp_path: Path) -> None:
    on = _fake(tmp_path, SKY, [[]])
    assert "Nothing yet in the last 14 h" in on["fillText"]
    # hairlines are fillRects too: the same empty feed drew 3 of them (rows) + the hour ticks + the now line, and no wider box
    boxes = [l for l in on["log"] if l[0] == "fillRect" and l[3] > 20 and l[4] == 20]
    assert boxes == []


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_aria_label_names_what_is_waiting_capped_at_five(tmp_path: Path) -> None:
    waits = [_e("decision", f"1{i}:00", None, "claude_code", "waiting", label=f"Bash step {i}") for i in range(2, 9)]
    r = _fake(tmp_path, SKY, [waits])
    a = r["aria"]
    assert "Waiting on you: decision Bash step 2 at " in a and "Bash step 6 at " in a and "Bash step 7" not in a
    assert "; and 2 more. Details in Needs you." in a
    q = _fake(tmp_path, SKY, [[_e("question", "17:00", None, "claude_code", "open", label="Run it?")]])["aria"]
    assert "question Run it? at " in q and "and " not in q.split("Waiting on you:")[1].split("Details")[0]
    none = _fake(tmp_path, SKY, [_LEVAIN_FEED[:2] + [_e("session", "12:00", "a", "codex", "ok", end=_at("13:00"))]])["aria"]
    assert "Waiting on you" not in none


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_theme_switch_repaints_and_destroy_disconnects_every_listener(tmp_path: Path) -> None:
    r = _fake(tmp_path, SKY, [_LEVAIN_FEED], observer=True, theme=True)
    assert r["repainted"] is True and r["observersLive"] == 0 and r["mqLeft"] == 0


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_now_sits_near_the_right_edge_for_levain_and_flow_keeps_its_margin(tmp_path: Path) -> None:
    def now_line(feed):
        r = _fake(tmp_path, SKY, [feed])
        g = [l for l in r["log"] if l[0] == "fillRect" and l[3] == 1.5]   # the now line is the 1.5px-wide rect
        return g[-1][1]
    flow, lev = now_line(_FLOW_FEED), now_line(_LEVAIN_FEED)
    assert lev - flow > 50 and lev < 800 - 24   # about 11% of the plot further right, still inside the right padding


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_levain_lanes_latch_for_the_mount_once_shown(tmp_path: Path) -> None:
    old = [_e("decision", "16:00", None, "claude_code", "waiting", label="x")]
    aged = [_e("commit", "15:00", "a")]
    r = _fake(tmp_path, SKY, [old, aged])
    assert "sessions" in r["fillText"] and "questions" in r["fillText"]
    flowonly = _fake(tmp_path, SKY, [aged, aged])
    assert "questions" not in flowonly["fillText"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_buildmodel_latch_option_keeps_the_levain_rows_with_no_levain_event_left() -> None:
    assert _lev([], {"lev": True})["rows"] == ["sessions", "decisions", "memory", "questions"]
    assert _lev([_e("commit", "15:00", "a")], {"lev": True})["lev"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_sessions_past_eight_overlapping_share_the_last_track_and_the_model_not_the_event_holds_it() -> None:
    feed = [_e("session", "12:00", "a", f"s{i}", "live", end=_at("19:13")) for i in range(10)]
    m = _lev(feed)
    assert m["tracks"] == 8 and m["overflow"] == 2 and m["written"] == 0
    assert sorted(m["track"].values()) == [0, 1, 2, 3, 4, 5, 6, 7, 7, 7]
    assert len(m["track"]) == 10   # ten events at one minute stay ten ids (the helper no longer collides them)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_session_end_before_its_start_or_not_a_date_or_in_the_future_reads_as_the_bar_is_drawn() -> None:
    import datetime as d
    hhmm = lambda hm: d.datetime.fromisoformat(f"2026-10-19T{hm}:00+00:00").astimezone().strftime("%H:%M")
    before = _e("session", "12:00", "a", "codex", "ok", end=_at("11:00"))
    nan = _e("session", "13:00", "a", "claude_code", "ok", end="not-a-date")
    future = _e("session", "14:00", "a", "gemini", "ok", end="2026-10-20T03:00:00Z")
    tips = _lev([before, nan, future])["tips"]
    assert tips["session@12:00"] == f"session · codex · a · {hhmm('12:00')}–{hhmm('12:00')}"
    assert tips["session@13:00"] == f"session · claude_code · a · {hhmm('13:00')}–{hhmm('13:00')}"
    assert tips["session@14:00"] == f"session · gemini · a · {hhmm('14:00')}–{hhmm('19:13')}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_repo_named_like_an_object_prototype_key_is_a_repo(tmp_path: Path) -> None:
    m = _lev([_e("commit", "15:00", "constructor"), _e("commit", "15:10", "__proto__"), _e("commit", "15:20", "other_one")], {"topRows": 1})
    assert m["rows"][0] in ("r:constructor", "r:__proto__", "r:other_one") and "other" in m["rows"]
