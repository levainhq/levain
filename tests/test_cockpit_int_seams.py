"""Cockpit-k integration seams (I1-I5): the lanes' pieces meet on one page. Node against the page's own source (tests/_ke_dom.py), the
CSS text for the layout claims, and glance.py for the doctor tile."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests._ke_dom import NODE, WEB, run_node

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

LEV = [{"kind": "memory", "at": "2026-10-19T12:00:00Z", "repo": None, "agent": None, "outcome": None, "label": "an episode"}]
FLOW = [{"kind": "commit", "at": "2026-10-19T12:00:00Z", "repo": "ledgerline", "agent": None, "outcome": None}]


def test_i1_a_levain_feed_draws_the_levain_key_and_a_flow_feed_keeps_its_own(tmp_path: Path) -> None:
    out = run_node(r"""
const S = require(%s);
Object.defineProperty(N.prototype, "style", { get() { return this._s || (this._s = {}); } });
globalThis.window = globalThis; globalThis.LevainSky = S;
const mk = (events) => { const h = new N("div"); K.paintLegend(h, { events, legend: "commit · review" }); return h; };
const lev = mk(%s), flow = mk(%s);
out.lev = lev.textContent; out.flow = flow.textContent;
""" % (json.dumps(str(WEB / "sky.js")), json.dumps(LEV), json.dumps(FLOW)), tmp_path)
    assert "waiting on you" in out["lev"] and "Hover a mark for its time, kind and repo." in out["lev"]
    assert "waiting on you" not in out["flow"] and "review that failed" in out["flow"] and "decision made" in out["flow"]


def test_i2_the_build_tab_mounts_the_sky_large_the_others_compact(tmp_path: Path) -> None:
    out = run_node(r"""
const sizes = {};
for (const m of ["morning", "board", "build", "memory"]) { globalThis.document.body.dataset.mode = m; sizes[m] = K.skySize(); }
out.sizes = sizes;
""", tmp_path)
    assert out["sizes"] == {"morning": "compact", "board": "compact", "build": "large", "memory": "compact"}
    css = (WEB / "dashboard.css").read_text(encoding="utf-8")
    assert 'body.ke[data-mode="build"] .sky-hero .plot' not in css      # no fixed floor under the large plot: it was the dead space
    assert re.search(r"size: skySize\(\)", (WEB / "cockpit_view.js").read_text(encoding="utf-8"))


def test_i3_the_events_line_never_says_zero_repos(tmp_path: Path) -> None:
    out = run_node(r"""
out.none = K.skyCounts({ events: %s }).text;
out.some = K.skyCounts({ events: %s }).text;
out.one = K.skyCounts({ events: [%s[0]] }).text;
""" % (json.dumps(LEV * 2), json.dumps(FLOW * 2), json.dumps(LEV)), tmp_path)
    assert out["none"] == "2 events" and out["some"] == "2 events from 1 repo" and out["one"] == "1 event"


def test_i4_more_than_four_tiles_wrap_into_rows_of_at_most_four(tmp_path: Path) -> None:
    out = run_node(r"""
globalThis.getComputedStyle = () => ({ display: "block" });
globalThis.document.body.dataset.mode = "board";
const run = (n) => {
  const props = {};
  const w = Object.assign(new N("section"), { style: { setProperty: (k, v) => { props[k] = v; } }, children: Array.from({ length: n }, () => Object.assign(new N("div"), { dataset: {} })) });
  globalThis.document.querySelectorAll = (sel) => (sel === ".ck-widgets" ? [w] : []);
  K.fillGrids();
  return [props["--ck-n"], w.dataset.shape];
};
out.r = [3, 4, 5, 6, 7, 8].map(run);
""", tmp_path)
    assert out["r"] == [["4", "strip"], ["4", "strip"], ["4", "tiles"], ["3", "tiles"], ["4", "tiles"], ["4", "tiles"]], out["r"]


def test_i5_the_failing_doctor_check_rides_in_the_tooltip_not_the_row() -> None:
    from types import SimpleNamespace

    from levain.cockpit.glance import _dot

    bad = _dot("hooks", [SimpleNamespace(name="activation/hooks/", ok=False, warn=False)])
    assert bad["value"] == "1 failing" and bad["read"] is None and bad["detail"] == "activation/hooks/ · levain doctor"
    warn = _dot("hooks", [SimpleNamespace(name="stale pin", ok=True, warn=True)])
    assert warn["value"] == "1 warning" and warn["detail"].startswith("stale pin")
    assert "/Users" not in json.dumps(bad)                                   # display-safe: no absolute path
    ok = _dot("hooks", [SimpleNamespace(name="x", ok=True, warn=False)])
    assert "detail" not in ok


def test_i5_a_dot_row_carries_the_detail_as_its_title(tmp_path: Path) -> None:
    src = (WEB / "cockpit_view.js").read_text(encoding="utf-8")
    assert 'row.title = m.label + ": " + m.detail' in src
    assert "m.detail == null || typeof m.detail" in src                       # the payload check admits it
