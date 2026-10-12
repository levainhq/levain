"""The ambient sky (``sky.js``): served as an asset, rows modelled correctly, and its frame loop comes to rest.

The model checks run under node; the rest-state proof drives headless Chrome over CDP and is skipped
where no Chrome is installed (the screenshots are saved by ``tests/sky_replay/run_cdp.mjs`` given an outdir)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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
    out = subprocess.run([NODE, str(REPLAY / "run_cdp.mjs"), CHROME], capture_output=True, text=True, timeout=120, check=True).stdout
    lines = [ln for ln in out.splitlines() if "RESULT" in ln]
    assert len(lines) == 2, out
    for ln in lines:
        res = json.loads(ln.split("RESULT ", 1)[1])
        assert res["pass"], res
