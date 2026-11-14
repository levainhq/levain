"""K10 S7: Morning's Where to begin is a card (lead, overnight; the loop fold took its loop line and Threads by repo), and Build's Activity band is the compact strip."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from levain.cockpit.providers import _where_to_begin
from datetime import timedelta

from tests.test_cockpit_activity import _session
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP

TODAY = date(2026, 10, 20)


def _sp(i, text, *, disposition="agenda", nxt="", tier="warm", domain="", seen=""):
    return SimpleNamespace(id=i, text=text, disposition=disposition, next=nxt, seen=seen, tier=tier, type="task", domain=domain,
                           salience=1, created="")


def _due(i, text):
    return _sp(i, text, nxt=TODAY.isoformat())


def _begin(tray=(), loops=(), **view_kw):
    # ``loops`` is what an embedder's view might still hold in ``open_intentions``: the card ignores it
    view = SimpleNamespace(tray=list(tray), open_intentions=list(loops), errors=view_kw.pop("errors", {}), **view_kw)
    ctx = SimpleNamespace(today=TODAY, memo=lambda k, f: view)
    return _where_to_begin(SimpleNamespace(build=lambda **kw: view))(ctx).value


def _wrap(at="2026-10-20T16:31:00+00:00"):
    return SimpleNamespace(wrapped_at=at)


def test_where_to_begin_names_the_list_as_its_source_and_has_no_loop_line_and_no_threads():
    rows = [_sp("intention-1", "cold thing", tier="cold", domain="alpha", disposition="someday"),
            _sp("intention-2", "hot thing", tier="hot", domain="beta", disposition="someday"),
            _sp("intention-3", "due today", nxt=TODAY.isoformat())]
    got = _begin(tray=rows)
    assert got["title"] == "Where to begin" and got["source"] == "from the List"
    assert "threads" not in got                                                        # the Threads column has nothing to draw
    assert [(ln["label"], ln["text"]) for ln in got["lines"]] == [("first", "due today")]   # no `loop` line: a someday never leads


def test_an_embedder_view_that_still_fills_open_intentions_gets_no_loop_line_nor_threads():
    got = _begin(loops=[_sp("intention-1", "cold thing", tier="cold", domain="alpha"), _sp("intention-2", "hot thing", tier="hot")])
    assert got["headline"] == "Nothing set for today yet" and "threads" not in got
    assert got["source"] == "from the List" and [ln["label"] for ln in got["lines"]] == ["set"]


def test_where_to_begin_draws_the_overnight_line_only_when_the_install_knows_it():
    known = _begin(tray=[_due("s", "x")], wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=11))
    assert known["overnight"] == {"episodes": 11, "since": "2026-10-20T16:31:00.000000Z"}
    zero = _begin(tray=[_due("s", "x")], wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=0))
    assert zero["overnight"]["episodes"] == 0                                           # a real zero is drawn
    for kw in (dict(),                                                                  # nothing read
               dict(wraps=[], health=SimpleNamespace(episodes_since_wrap=3)),           # no wrap: nothing to count from
               dict(wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=None)),  # count unknown: never 0
               dict(wraps=[_wrap()], health=None),
               dict(wraps=[_wrap("not a time")], health=SimpleNamespace(episodes_since_wrap=3)),
               dict(wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=3), errors={"wraps": "unreadable"})):
        assert "overnight" not in _begin(tray=[_due("s", "x")], **kw), kw


def test_where_to_begin_does_not_offer_an_undated_or_a_parked_someday():
    # TS-5a L3 / the loop fold: a dormant row is on the List, marked, and is not a place to begin; nor is an undated one
    got = _begin(tray=[_sp("intention-p", "dormant", tier="parked", disposition="someday")])
    assert got["headline"] == "Nothing set for today yet" and "threads" not in got
    mixed = _begin(tray=[_sp("intention-h", "hot", tier="hot", disposition="someday"),
                         _sp("intention-p", "dormant", tier="parked", disposition="someday")])
    assert [ln["label"] for ln in mixed["lines"]] == ["set"] and "threads" not in mixed


def test_a_parked_row_never_leads_where_to_begin_whatever_its_date_or_disposition():
    # L3 S2 r1 (codex + deepseek MED): a parked row is in the tray since the fold, so the late/due path must skip it too
    for disposition in ("agenda", "someday", "handoff"):
        for nxt in ((TODAY - timedelta(days=1)).isoformat(), TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat()):
            got = _begin(tray=[_sp("intention-p", "dormant", tier="parked", disposition=disposition, nxt=nxt)])
            assert [ln["label"] for ln in got["lines"]] == ["set"], (disposition, nxt)
            beside = _begin(tray=[_sp("intention-p", "dormant", tier="parked", disposition=disposition, nxt=nxt),
                                  _due("intention-d", "due today")])
            assert [(ln["label"], ln["text"]) for ln in beside["lines"]] == [("first", "due today")], (disposition, nxt)
    # an undated parked handoff inside its claim does not lead either
    got = _begin(tray=[_sp("intention-p", "dormant", tier="parked", disposition="handoff", seen=TODAY.isoformat())])
    assert [ln["label"] for ln in got["lines"]] == ["set"]


def test_where_to_begin_with_nothing_has_one_empty_line_and_no_threads():
    got = _begin()
    assert got["headline"] == "Nothing set for today yet" and "threads" not in got and "overnight" not in got
    assert got["title"] == "Where to begin" and got["source"]


_PROBE = r"""(() => {
  const card = document.querySelector('.brief-card');
  if (!card) return JSON.stringify({card: false});
  const rows = [...card.querySelectorAll('.brief-threads .thread')].map((r) => ({
    t: r.querySelector('.t').textContent, n: r.querySelector('.pill').textContent,
    tag: (r.querySelector('.rp-tag') || {}).textContent || null, hue: (r.querySelector('.rp-tag') || {dataset: {}}).dataset.repo || null}));
  return JSON.stringify({card: true, title: card.querySelector('.brief-head h3').textContent,
    source: (card.querySelector('.brief-head .stamp') || card.querySelector('.brief-threads .brief-src')).textContent, lead: card.querySelector('.lead-first').textContent,
    rows: rows, overnight: (card.querySelector('.brief-overnight .mono') || {}).textContent || null,
    items: card.querySelectorAll('.lead-then, .lead-item').length});
})()"""


@needs_browser
def test_the_card_draws_its_lead_from_the_list_and_has_no_threads_column(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": "document.querySelector('.brief-card')"},
                                                          {"eval": _PROBE}])["values"]
    got = json.loads(out[-1])
    assert got["card"] and got["title"] == "Next from the List" and got["source"] == "from the List", got   # the data says "Where to begin"; the card names what it holds
    assert got["lead"], got
    assert got["rows"] == [], got                                       # the kernel sends no threads since the fold: the column has nothing to draw


@needs_browser
def test_build_draws_the_activity_band_as_the_compact_strip_and_board_the_full_chart(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    # the header lens draws when the topology holds something; a key-absent intention was a task there, and is a List row now,
    # so the install holds one live session for the graph to draw
    _session(_root, "s-a", str(tmp_path), timedelta(minutes=3))
    probe = ("JSON.stringify({h: document.querySelector('.skycard').getBoundingClientRect().height,"
             " head: (document.querySelector('.sky-head-title') || {}).textContent || null,"
             " canvas: document.querySelector('.sky-canvas').getBoundingClientRect().height,"
             " win: (document.querySelector('.sky-head-win') || {}).textContent || null,"
             " legend: getComputedStyle(document.querySelector('.skycard .ck-legend') || document.body).display})")
    shown = {}
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        for tab in ("build", "board"):
            # K11 (ab3e3d36): the header's cold-open lens is the Agents graph and the Activity band is its Event graph lens, so the
            # band is looked at on that lens
            shown[tab] = json.loads(_drive(tmp_path, f"{url}/#write_token=T&{tab}", [DESKTOP,
                {"until": "document.querySelector('#ck-hlens [data-tv=events]')"}, {"eval": "document.querySelector('#ck-hlens [data-tv=events]').click(); 1"},
                {"until": "document.getElementById('ck-skycard').dataset.hlens == 'events' && document.querySelector('.sky-canvas')"}, {"eval": probe}])["values"][-1])
    b, d = shown["build"], shown["board"]
    assert b["head"] == "Activity" and b["win"] == "last 14 h" and b["legend"] == "none", b
    assert b["canvas"] < 100 and b["h"] < 160, b                       # the mock's band is ~85 px
    assert d["head"] is None and d["canvas"] >= 160 and d["h"] > b["h"] + 100, d   # Board keeps its full chart
