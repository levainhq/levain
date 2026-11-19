"""levain#141 polish, the Machine section: Calm and Ambient live in the Machine card's control row, the graph selector is hidden until
"Show the graphs", the open card stacks control row / selector / graph at every width, the stats box exists, and the 1101-1180 rows fill.

What these hold: the markup (one control row inside #ck-skycol, Calm moved out of the masthead, no second phone Calm), and in a real
browser the closed and open states at 1440 and 390 px, the stored lens coming back on open, Calm and Ambient working from the row, and the
touch size. What they cannot hold (judged by looking at his phone and laptop): whether the row reads well beside the stats."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _serve
from tests.test_cockpit_k7_build import _sessions_fixture
from tests.test_cockpit_narrow_layout_rig import drive_touch
from tests.test_cockpit_places import READY, SETTLE, _parse

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

STATE = ("(() => { const q = (s) => document.querySelector(s); const box = (n) => { if (!n) return null; const r = n.getBoundingClientRect();"
         " return {shown: n.getClientRects().length > 0, top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right), h: Math.round(r.height)}; };"
         " const row = q('#ck-mctl'); const amb = [...document.querySelectorAll('#ck-mctl .sky-toggle')].filter(b => !b.classList.contains('sky-toggle-ghost'))[0];"
         " return JSON.stringify({open: document.body.classList.contains('ia-graph-open'), btn: q('[data-ia-graph-toggle]').textContent, expanded: q('[data-ia-graph-toggle]').getAttribute('aria-expanded'),"
         " row: box(row), toggle: box(q('[data-ia-graph-toggle]')), calm: box(q('#calmbtn')), calmIn: !!(row && row.contains(q('#calmbtn'))), calmPressed: q('#calmbtn').getAttribute('aria-pressed'),"
         " amb: box(amb), ambIn: !!(amb && row.contains(amb)), ambPressed: amb ? amb.getAttribute('aria-pressed') : null, mastCalm: !!q('header.masthead #calmbtn'), phoneCalm: !!q('#calmphone'),"
         " seg: box(q('#ck-hlens .tv-seg')), hlens: box(q('#ck-hlens')), sky: box([q('#ck-hlens .tv-lensbody'), q('#ck-sky')].find(n => n && n.getClientRects().length) || q('#ck-sky')), card: box(q('#ck-skycard')), jarbox: box(q('#ck-jarbox')),"
         " pressedLens: (q('#ck-hlens .tv-seg-b[aria-pressed=true]') || {}).dataset ? q('#ck-hlens .tv-seg-b[aria-pressed=true]').dataset.tv : null}); })()")


def _states(out: dict) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"open"')]


def test_the_control_row_sits_in_the_machine_card_with_calm_moved_out_of_the_masthead() -> None:
    """One #ck-mctl inside #ck-skycol holds Calm and the stats box; the masthead no longer carries Calm and the band's phone Calm is folded into the row."""
    tags = _parse().tags
    row = [(t, a, st) for t, a, st in tags if a.get("id") == "ck-mctl"]
    assert len(row) == 1 and any(s[1].get("id") == "ck-skycol" for s in row[0][2]), row
    calm = [(t, a, st) for t, a, st in tags if a.get("id") == "calmbtn"]
    assert len(calm) == 1 and any(s[1].get("id") == "ck-mctl" for s in calm[0][2]), calm
    assert "data-calm-toggle" in calm[0][1] and not any(s[0] == "header" for s in calm[0][2]), calm
    jar = [(t, a, st) for t, a, st in tags if a.get("id") == "ck-jarbox"]
    assert len(jar) == 1 and any(s[1].get("id") == "ck-mctl" for s in jar[0][2]), jar
    assert not [1 for _t, a, _s in tags if a.get("id") == "calmphone"]
    assert "(pointer: coarse)" in (WEB / "dashboard.css").read_text().split("levain#141 polish: Machine")[1]


def test_the_graph_button_says_the_graphs_and_the_ambient_key_is_hosted_by_the_row() -> None:
    places = (WEB / "dashboard_places.js").read_text()
    assert "Show the graphs" in places and "Hide the graphs" in places and "the graph\"" not in places
    view = (WEB / "cockpit_view.js").read_text()
    assert 'toggleHost: document.getElementById("ck-mctl")' in view


@needs_browser
def test_closed_shows_no_selector_open_stacks_row_selector_graph_and_remembers_the_lens(tmp_path: Path, monkeypatch) -> None:
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        for w in (1440, 390):
            d = tmp_path / str(w); d.mkdir(); out = _drive(d, f"{url}/#write_token=T", [
                {"emulate": {"w": w, "h": 900, "dpr": 1}},
                READY, {"until": "document.querySelector('#ck-hlens .tv-seg') && 1"}, SETTLE, SETTLE,
                {"eval": STATE},                                                                      # 0: closed
                {"eval": "document.querySelector('[data-ia-graph-toggle]').click(); 1"}, SETTLE,
                {"eval": STATE},                                                                      # 1: open
                {"eval": "document.querySelector('#ck-hlens .tv-seg-b[data-tv=work]').click(); 1"}, SETTLE,
                {"eval": "location.reload(); 1"}, READY, {"until": "document.querySelector('#ck-hlens .tv-seg') && 1"}, SETTLE, SETTLE,
                {"eval": STATE},                                                                      # 2: reloaded (closed again)
                {"eval": "document.querySelector('[data-ia-graph-toggle]').click(); 1"}, SETTLE,
                {"eval": STATE},                                                                      # 3: open with the stored lens
            ])
            shut, opened, again, back = _states(out)
            assert shut["open"] is False and shut["btn"] == "Show the graphs" and shut["expanded"] == "false", (w, shut)
            assert not (shut["seg"] and shut["seg"]["shown"]), (w, shut)                              # the selector is not drawn while closed
            assert not (shut["sky"] and shut["sky"]["shown"]), (w, shut)
            assert shut["calmIn"] and shut["ambIn"] and not shut["mastCalm"] and not shut["phoneCalm"], (w, shut)
            assert shut["row"]["bottom"] <= shut["card"]["top"] + 1, (w, shut)                        # the row is above the card, nothing over it
            assert opened["open"] is True and opened["btn"] == "Hide the graphs" and opened["expanded"] == "true", (w, opened)
            assert opened["seg"]["shown"] and opened["sky"]["shown"], (w, opened)
            # no overlap: the control row ends where the selector begins, and the graph is below the selector
            assert opened["toggle"]["bottom"] <= opened["seg"]["top"] + 1 and opened["calm"]["bottom"] <= opened["seg"]["top"] + 1, (w, opened)
            assert opened["seg"]["bottom"] <= opened["sky"]["top"] + 1, (w, opened)
            assert opened["toggle"]["right"] <= opened["calm"]["left"] + 1, (w, opened)               # one row: the button, then Calm
            assert again["open"] is False and not (again["seg"] and again["seg"]["shown"]), (w, again)
            assert back["pressedLens"] == "work", (w, back)                                           # the last selected graph type comes back
            assert back["seg"]["shown"], (w, back)


@needs_browser
def test_calm_and_ambient_work_from_the_machine_row(tmp_path: Path, monkeypatch) -> None:
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    AMB = "document.querySelector('#ck-mctl .sky-toggle:not(.sky-toggle-ghost)').click(); 1"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, {"until": "document.querySelector('#ck-hlens .tv-seg') && 1"}, SETTLE, SETTLE,
            {"eval": STATE},
            {"eval": "document.getElementById('calmbtn').click(); 1"}, SETTLE,
            {"eval": AMB}, SETTLE,
            {"eval": STATE},
            {"eval": "JSON.stringify({open: 0, x: [document.documentElement.classList.contains('calm'), document.getElementById('calmbtn').textContent]})"},
        ])
    s = _states(out)
    assert s[0]["calmPressed"] == "false" and s[0]["ambPressed"] == "true", s[0]
    assert s[1]["calmPressed"] == "true" and s[1]["ambPressed"] == "false", s[1]
    assert s[2]["x"] == [True, "Calm on"], s[2]


@needs_browser
def test_the_row_targets_are_touch_sized_and_the_stats_box_hides_while_empty(tmp_path: Path, monkeypatch) -> None:
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = drive_touch(tmp_path, f"{url}/#write_token=T", [{"emulate": {"w": 390, "h": 900, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE, SETTLE, {"eval": STATE}])
    (s,) = _states(out)
    assert s["toggle"]["h"] >= 44 and s["calm"]["h"] >= 44 and s["amb"]["h"] >= 44, s
    assert not (s["jarbox"] and s["jarbox"]["shown"] and s["jarbox"]["bottom"] <= s["jarbox"]["top"]), s      # drawn into by the jar now (renderJar); an empty box would be hidden by :empty, never an empty strip


@needs_browser
def test_the_rows_at_1101_to_1180_fill_and_the_wider_widths_keep_their_shape(tmp_path: Path, monkeypatch) -> None:
    COLS = ("(() => { const tops = (sel) => { const m = new Map(); for (const e of document.querySelectorAll(sel)) { if (!e.getClientRects().length) continue;"
            " const t = Math.round(e.getBoundingClientRect().top); m.set(t, (m.get(t) || 0) + 1); } return [...m.entries()].sort((a, b) => a[0] - b[0]).map(x => x[1]); };"
            " return JSON.stringify({open: 0, machine: tops('.panel[data-ck-panel=roster], .panel[data-ck-panel=agents], .panel[data-ck-panel=agent_calls], .panel[data-ck-panel=stream]'),"
            " glance: tops('#ck-widgets > *'), edge: (() => { const c = document.getElementById('ck-widgets').getBoundingClientRect().right; const rows = new Map(); for (const e of document.querySelectorAll('#ck-widgets > *')) { if (!e.getClientRects().length) continue; const r = e.getBoundingClientRect(); const t = Math.round(r.top); rows.set(t, Math.max(rows.get(t) || 0, r.right)); } return [...rows.values()].map(x => Math.round(c - x)); })()}); })()")
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        seen = {}
        for w in (1101, 1180, 1440, 1920):
            d = tmp_path / str(w); d.mkdir(); out = _drive(d, f"{url}/#write_token=T", [{"emulate": {"w": w, "h": 900, "dpr": 1}}, READY, SETTLE, SETTLE, {"eval": COLS}])
            (seen[w],) = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"open"')]
    for w in (1101, 1180):
        assert all(n >= 2 for n in seen[w]["machine"]), (w, seen[w])                                    # no lone panel in the four-up row
        assert all(abs(g) <= 2 for g in seen[w]["edge"]), (w, seen[w])                                  # every glance row runs to the right edge: a short last row grows to fill it
    for w in (1440, 1920):
        assert all(n >= 3 for n in seen[w]["machine"]), (w, seen[w])                                    # the wide shape is unchanged: four across
