"""K10 S7: Morning's Where to begin is a card (lead, overnight, Threads by repo), and Build's Activity band is the compact strip."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from levain.cockpit.providers import _where_to_begin
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP, READY

TODAY = date(2026, 10, 20)


def _sp(i, text, *, disposition="agenda", nxt="", tier="warm", domain="", seen=""):
    return SimpleNamespace(id=i, text=text, disposition=disposition, next=nxt, seen=seen, tier=tier, type="task", domain=domain,
                           salience=1, created="")


def _begin(tray=(), loops=(), **view_kw):
    view = SimpleNamespace(tray=list(tray), open_spores=list(loops), errors=view_kw.pop("errors", {}), **view_kw)
    ctx = SimpleNamespace(today=TODAY, memo=lambda k, f: view)
    return _where_to_begin(SimpleNamespace(build=lambda **kw: view))(ctx).value


def _wrap(at="2026-10-20T16:31:00+00:00"):
    return SimpleNamespace(wrapped_at=at)


def test_where_to_begin_names_its_sources_and_hands_the_open_loops_to_the_card():
    loops = [_sp("spore-1", "cold thing", tier="cold", domain="alpha"), _sp("spore-2", "hot thing", tier="hot", domain="beta")]
    got = _begin(loops=loops)
    assert got["title"] == "Where to begin" and got["source"] == "from the Tray and Open loops"
    assert [t["text"] for t in got["threads"]] == ["hot thing", "cold thing"]          # hottest first, none dropped
    assert [t["domain"] for t in got["threads"]] == ["beta", "alpha"]
    assert [ln["label"] for ln in got["lines"]] == ["loop"]                            # every item still on the wire


def test_where_to_begin_draws_the_overnight_line_only_when_the_install_knows_it():
    known = _begin(loops=[_sp("s", "x")], wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=11))
    assert known["overnight"] == {"episodes": 11, "since": "2026-10-20T16:31:00.000000Z"}
    zero = _begin(loops=[_sp("s", "x")], wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=0))
    assert zero["overnight"]["episodes"] == 0                                           # a real zero is drawn
    for kw in (dict(),                                                                  # nothing read
               dict(wraps=[], health=SimpleNamespace(episodes_since_wrap=3)),           # no wrap: nothing to count from
               dict(wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=None)),  # count unknown: never 0
               dict(wraps=[_wrap()], health=None),
               dict(wraps=[_wrap("not a time")], health=SimpleNamespace(episodes_since_wrap=3)),
               dict(wraps=[_wrap()], health=SimpleNamespace(episodes_since_wrap=3), errors={"wraps": "unreadable"})):
        assert "overnight" not in _begin(loops=[_sp("s", "x")], **kw), kw


def test_where_to_begin_with_nothing_has_one_empty_line_and_no_threads():
    got = _begin()
    assert got["headline"] == "Nothing set for today yet" and "threads" not in got and "overnight" not in got


_PROBE = r"""(() => {
  const card = document.querySelector('.brief-card');
  if (!card) return JSON.stringify({card: false});
  const rows = [...card.querySelectorAll('.brief-threads .thread')].map((r) => ({
    t: r.querySelector('.t').textContent, n: r.querySelector('.pill').textContent,
    tag: (r.querySelector('.rp-tag') || {}).textContent || null, hue: (r.querySelector('.rp-tag') || {dataset: {}}).dataset.repo || null}));
  return JSON.stringify({card: true, title: card.querySelector('.brief-head h3').textContent,
    source: card.querySelector('.brief-head .stamp').textContent, lead: card.querySelector('.lead-first').textContent,
    rows: rows, overnight: (card.querySelector('.brief-overnight .mono') || {}).textContent || null,
    items: card.querySelectorAll('.lead-then, .lead-item').length});
})()"""


@needs_browser
def test_the_card_groups_threads_by_repo_loudest_first(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY}, {"until": "document.querySelector('.brief-card')"},
                                                          {"eval": _PROBE}])["values"]
    got = json.loads(out[-1])
    assert got["card"] and got["title"] == "Where to begin" and got["source"] == "from the Tray and Open loops", got
    assert got["lead"]
    counts = [int(r["n"].split()[0]) for r in got["rows"]]
    assert counts == sorted(counts, reverse=True), got                  # loudest first
    assert all((r["tag"] is None) == (r["hue"] is None) for r in got["rows"]), got   # a tag always wears its repo hue


@needs_browser
def test_build_draws_the_activity_band_as_the_compact_strip_and_board_the_full_chart(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    probe = ("JSON.stringify({h: document.querySelector('.skycard').getBoundingClientRect().height,"
             " head: (document.querySelector('.sky-head-title') || {}).textContent || null,"
             " canvas: document.querySelector('.sky-canvas').getBoundingClientRect().height,"
             " win: (document.querySelector('.sky-head-win') || {}).textContent || null,"
             " legend: getComputedStyle(document.querySelector('.skycard .ck-legend') || document.body).display})")
    shown = {}
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        for tab in ("build", "board"):
            shown[tab] = json.loads(_drive(tmp_path, f"{url}/#write_token=T&{tab}", [DESKTOP, {"until": READY},
                {"until": "document.querySelector('.sky-canvas')"}, {"eval": probe}])["values"][-1])
    b, d = shown["build"], shown["board"]
    assert b["head"] == "Activity" and b["win"] == "last 14 h" and b["legend"] == "none", b
    assert b["canvas"] < 100 and b["h"] < 160, b                       # the mock's band is ~85 px
    assert d["head"] is None and d["canvas"] >= 160 and d["h"] > b["h"] + 100, d   # Board keeps its full chart
