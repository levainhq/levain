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
MAX_REM = 36    # the box is min(72vh, 36rem) tall at most


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
    cap = min(0.72 * many["vh"], MAX_REM * REM) + 2
    assert many["box"] <= cap, many                           # the box stops at its fixed height
    assert many["box"] >= cap - 4, many                     # and it IS that tall: a revert to 60vh / 28rem fails (cap carries +2)
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


# --- the box scrolls: cards stay inside it, a repaint keeps the scroll, no sideways bar at a desktop width ---

ST = TV + ".querySelector('.tv-stage')"
WAIT = {"eval": "new Promise(r => setTimeout(r, 600)).then(() => 1)"}
NOPAGE = {"eval": "window.LevainAgentPage = undefined; 1"}      # no agent page in the page: a click pins the card at the node
LAST = ("(() => { const gs = [...%s.querySelectorAll('[data-node]')]; const g = gs[gs.length - 1]; const ring = g.querySelector('.tv-ring') || g; "
        "const rc = ring.getBoundingClientRect(); return {g, x: rc.left + rc.width / 2, y: rc.top + rc.height / 2}; })()") % TV
INSIDE = ("(() => { const t = %s.querySelector('.tv-tip'), s = %s.getBoundingClientRect(), r = t.getBoundingClientRect(); "
          "return JSON.stringify({hidden: t.hidden, top: r.top - s.top, bottom: s.bottom - r.bottom, left: r.left - s.left, right: s.right - r.right, "
          "st: %s.scrollTop}); })()") % (TV, ST, ST)


def _fire(kind: str) -> str:
    ev = "new PointerEvent('pointermove', o)" if kind == "hover" else "new MouseEvent('click', o)"
    return ("(() => { const L = %s; %s.scrollTop = 100000; return new Promise(r => setTimeout(r, 150)).then(() => { const L2 = %s; "
            "const o = {bubbles: true, clientX: L2.x, clientY: L2.y}; L2.g.dispatchEvent(%s); return 1; }); })()") % (LAST, ST, LAST, ev)


@needs_browser
def test_a_card_over_a_lower_node_stays_inside_the_scrolled_box(tmp_path, monkeypatch):
    for kind in ("hover", "pin"):
        (tmp_path / kind).mkdir()
        _root, src = _sessions_fixture(tmp_path / kind, monkeypatch)
        out = _run(tmp_path / kind, src, [*PRIME, NOPAGE, {"eval": _paint(_many(80), "agents")}, WAIT, {"eval": _fire(kind)}, {"eval": INSIDE}], first=DESKTOP)
        m = json.loads([v for v in out if isinstance(v, str) and '"bottom"' in v][-1])
        assert m["st"] > 0 and not m["hidden"], (kind, m)
        assert m["top"] >= -1 and m["bottom"] >= -1 and m["left"] >= -1 and m["right"] >= -1, (kind, m)   # the tip lies inside the visible box


@needs_browser
def test_a_repaint_with_new_data_keeps_the_boxs_scroll(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v2 = _many(80)
    for n in v2["nodes"]:
        n["rate"] = 3.0                                           # a rate change: the renderer replaces the stage
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_many(80), "agents")}, WAIT,
                               {"eval": ST + ".scrollTop = 150; new Promise(r => setTimeout(r, 200)).then(() => " + ST + ".scrollTop)"},
                               {"eval": _paint(v2, "agents")}, WAIT, {"eval": "JSON.stringify({after: " + ST + ".scrollTop})"}], first=DESKTOP)
    before = [v for v in out if isinstance(v, (int, float)) and v >= 100][-1]
    after = json.loads([v for v in out if isinstance(v, str) and '"after"' in v][-1])["after"]
    assert before >= 100 and abs(after - before) <= 1, (before, after)


@needs_browser
def test_the_box_has_no_horizontal_overflow_at_a_desktop_width(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_many(80), "agents")}, WAIT,
                               {"eval": "JSON.stringify({sw: %s.scrollWidth, cw: %s.clientWidth, ox: getComputedStyle(%s).overflowX, oy: getComputedStyle(%s).overflowY, ob: getComputedStyle(%s).overscrollBehaviorY})" % ((ST,) * 5)}],
               first=DESKTOP)
    m = json.loads([v for v in out if isinstance(v, str) and '"sw"' in v][-1])
    assert m["sw"] <= m["cw"] and m["ox"] == "hidden" and m["oy"] == "auto" and m["ob"] == "auto", m
