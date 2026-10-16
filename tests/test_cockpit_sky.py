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
