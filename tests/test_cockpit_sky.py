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
           "const id=e=>e.kind+'@'+new Date(e.t).toISOString().slice(11,16);const by=f=>Object.fromEntries(ev.map(e=>[id(e),f(e)]));"
           "console.log(JSON.stringify({rows:m.rows.map(r=>r.id),lev:m.lev,empty:m.empty,tracks:m.tracks,counts:m.counts,"
           "where:by(e=>m.rowOf(e)),track:by(e=>e.track===undefined?null:e.track),tips:by(eventTip)}));")


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
