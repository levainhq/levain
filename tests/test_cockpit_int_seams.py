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


def test_i2_the_build_tab_mounts_the_sky_as_the_compact_strip_the_others_full(tmp_path: Path) -> None:
    out = run_node(r"""
const sizes = {};
for (const m of ["morning", "board", "build", "memory"]) { globalThis.document.body.dataset.mode = m; sizes[m] = K.skySize(); }
out.sizes = sizes;
""", tmp_path)
    assert out["sizes"] == {"morning": "compact", "board": "compact", "build": "strip", "memory": "compact"}
    css = (WEB / "dashboard.css").read_text(encoding="utf-8")
    assert 'body.ke[data-mode="build"] .sky-hero .plot { min-height: 0; }' in css      # no floor under the strip's plot: the mock's band is ~85 px
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


def test_i6_a_status_dots_detail_reaches_the_lines_and_the_focus_filter(tmp_path: Path) -> None:
    out = run_node(r"""
const m = [{ label: "hooks", value: "1 failing", detail: "activation/hooks/ · levain doctor", status: "bad" }];
const v = V.fromManifest(fixture({ ops: head("ops", "status-dots", "Ops") }, { ops: head("ops", "status-dots", "Ops", { value: { metrics: m } }) }));
const d = (v.extra_panels || {}).ops; out.ep = JSON.stringify(v.extra_panels).slice(0, 300);
out.line = (d.lines || [])[0] && d.lines[0].text;
""", tmp_path)
    assert "activation/hooks/" in (out.get("line") or ""), out
    src = (WEB / "cockpit_view.js").read_text(encoding="utf-8")
    assert "m.read, m.detail, m.status" in src      # paintFiltered's haystack names the detail


def test_i7_the_sky_counts_and_legend_survive_a_malformed_feed(tmp_path: Path) -> None:
    out = run_node(r"""
out.counts = K.skyCounts({ events: undefined, legend: undefined }).text;
const h = new N("div"); h.title = "x"; K.paintLegend(h, { events: [], legend: undefined }); out.title = h.title;
""", tmp_path)
    assert out["counts"] == "0 events" and out["title"] == ""


def test_i8_the_payloads_dialect_decides_the_key_and_the_lanes_not_the_current_window(tmp_path: Path) -> None:
    aged = [{"kind": "commit", "at": "2026-10-19T12:00:00Z", "repo": "r", "agent": None, "outcome": None}]    # the labelled events aged out
    out = run_node(r"""
const S = require(%s);
Object.defineProperty(N.prototype, "style", { get() { return this._s || (this._s = {}); } });
globalThis.window = globalThis; globalThis.LevainSky = S;
const legend = (sky) => { const h = new N("div"); K.paintLegend(h, sky); return h.textContent; };
out.levainNoLabelled = legend({ events: %s, legend: "l", dialect: "levain" });
out.noField = legend({ events: %s, legend: "l" });
out.flowField = legend({ events: %s, legend: "l", dialect: "flow" });
out.api = [S.isLevainFeed({ events: %s, dialect: "levain" }), S.isLevainFeed({ events: %s }), S.isLevainFeed({ events: %s, dialect: "flow" })];
const now = Date.parse("2026-10-19T13:00:00Z");
out.lanes = [S.buildModel(S.normalise({ events: %s }), { windowHours: 14, topRows: 5, pinnedRows: [], single: "", size: "compact", dialect: "levain" }, now).lev,
             S.buildModel(S.normalise({ events: %s }), { windowHours: 14, topRows: 5, pinnedRows: [], single: "", size: "compact" }, now).lev];
""" % ((json.dumps(str(WEB / "sky.js")),) + (json.dumps(aged),) * 8), tmp_path)
    assert "waiting on you" in out["levainNoLabelled"] and "waiting on you" not in out["noField"] and "waiting on you" not in out["flowField"]
    assert out["api"] == [True, False, False] and out["lanes"] == [True, False]


def test_i9_the_native_activity_value_says_levain_and_the_validator_takes_only_a_known_dialect() -> None:
    from levain.cockpit.engine import ProviderSpec, _v_ambient

    spec = ProviderSpec("a", "ambient", "A", "feed", lambda c: None)
    base = {"events": [], "legend": "l"}
    assert _v_ambient(spec, dict(base))["legend"] == "l"                                  # flow's own feed has no field
    assert _v_ambient(spec, {**base, "dialect": "levain"}) and _v_ambient(spec, {**base, "dialect": "flow"})
    with pytest.raises(Exception, match="dialect"):
        _v_ambient(spec, {**base, "dialect": "other"})
    assert '"dialect": "levain"' in (Path(__file__).resolve().parent.parent / "levain" / "cockpit" / "activity.py").read_text(encoding="utf-8")


from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_cockpit_k2a import _install, _serve  # noqa: E402


@needs_browser
def test_i10_a_levain_feed_keeps_its_key_when_its_labelled_events_aged_out_also_across_a_tab_switch(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from levain.cockpit.engine import ProviderSpec
    from levain.cockpit.providers import build_default_cockpit
    from levain.cockpit.results import Read

    _root, src, _ids = _install(tmp_path)
    now = datetime.now(timezone.utc)
    # the labelled events have aged out of the payload: what is left carries no Levain-only kind, label or outcome
    value = {"events": [{"kind": "commit", "at": (now - timedelta(minutes=5)).isoformat(), "repo": "r", "agent": None, "outcome": "ok"}],
             "legend": "levain", "dialect": "levain"}
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec("sky", "ambient", "Sky", "feed", lambda c: Read(value=value), region="hero"))
    key = "(document.querySelector('.legendbar') || {textContent: ''}).textContent.indexOf('waiting on you') >= 0"
    with _serve(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            {"until": f"document.querySelector('.legendbar') && 1"},
            {"eval": key},
            {"eval": "document.querySelector('[data-mode-tab=build]').click(); 1"},
            {"eval": f"new Promise(r => setTimeout(() => r([{key}, document.body.dataset.mode]), 600))"},
            {"eval": "document.querySelector('[data-mode-tab=board]').click(); 1"},
            {"eval": f"new Promise(r => setTimeout(() => r({key}), 600))"},
        ])["values"]
    assert out[1] is True, f"the Levain key is missing on first paint: {out}"
    assert out[3] == [True, "build"], f"the Levain key was lost across Board -> Build: {out}"
    assert out[5] is True, out
