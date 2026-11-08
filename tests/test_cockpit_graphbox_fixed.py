"""Row 138: the cockpit's Agents-graph box has a fixed height and scrolls inside itself, so adding nodes never grows the page.
Real headless Chrome through the topology renderer, at a desktop width and at a phone's 390. `LEVAIN_GRAPHBOX_SHOTS=<dir>` and
`LEVAIN_GRAPHBOX_PREFIX` (before / after) keep a PNG of each width for the desk to look at."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_f12b_topology_view import NARROW, TV, _paint, _run
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k10_agents import PRIME, _graph_value
from tests.test_cockpit_k7_build import DESKTOP, _sessions_fixture

REM = 16
MAX_REM = 28    # the box is min(60vh, 28rem) tall at most


def _many(n: int) -> dict:
    """The Agents-graph value with `n` live nodes spread over four groups."""
    v = _graph_value()
    base = v["nodes"][0]
    v["groups"] = [{"id": f"g{k}", "label": f"repo{k}", "repo": f"repo{k}"} for k in range(4)]
    v["nodes"] = [{**base, "id": f"x{i}", "group": f"g{i % 4}", "parent": None, "label": f"agent {i}", "state": "running", "card": None, "rate": 1.0}
                  for i in range(n)]
    v["edges"] = []
    return v


MEASURE = ("JSON.stringify({doc: document.documentElement.scrollHeight, vh: window.innerHeight, "
           "box: %s.querySelector('.tv-stage').clientHeight, inner: %s.querySelector('.tv-stage').scrollHeight, "
           "oy: getComputedStyle(%s.querySelector('.tv-stage')).overflowY, nodes: %s.querySelectorAll('.tv-gnode').length})") % ((TV,) * 4)


def _shot(name: str) -> list:
    d = os.environ.get("LEVAIN_GRAPHBOX_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    pre = os.environ.get("LEVAIN_GRAPHBOX_PREFIX", "after")
    return [{"eval": "document.querySelector('#ck-hlens').scrollIntoView({block: 'start'}); 1"},
            {"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"},
            {"shot": str(Path(d) / f"{pre}-{name}.png"), "full": False}]


def _case(tmp_path, monkeypatch, first, name):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_many(4), "agents")}, {"eval": MEASURE},
                               {"eval": _paint(_many(80), "agents")}, {"eval": "new Promise(r => setTimeout(r, 600)).then(() => 1)"},
                               {"eval": MEASURE}, *_shot(name)], first=first)
    ms = [json.loads(v) for v in out if isinstance(v, str) and v.startswith("{") and '"oy"' in v]
    assert len(ms) == 2, out
    return ms


def _check(few: dict, many: dict) -> None:
    assert many["nodes"] == 80 and few["nodes"] == 4, (few, many)
    cap = min(0.6 * many["vh"], MAX_REM * REM) + 2
    assert many["box"] <= cap, many                           # the box stops at its fixed height
    assert many["inner"] > many["box"], many                  # and the rest of the graph is inside it, scrolled
    assert many["oy"] in ("auto", "scroll"), many
    assert many["doc"] - few["doc"] <= cap, (few, many)       # eighty nodes grew the page by no more than the box's own height


@needs_browser
def test_many_nodes_scroll_inside_the_graph_box_at_a_desktop_width(tmp_path, monkeypatch):
    few, many = _case(tmp_path, monkeypatch, DESKTOP, "desktop")
    _check(few, many)


@needs_browser
def test_many_nodes_scroll_inside_the_graph_box_at_a_phone_width(tmp_path, monkeypatch):
    few, many = _case(tmp_path, monkeypatch, NARROW, "phone")
    _check(few, many)
