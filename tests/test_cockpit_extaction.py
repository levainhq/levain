"""An external panel's own action is drawn only when a browser can perform it, and a zero-field one is a bare button
(1015 team look: a laptop-only accept drew an empty compose box that clipped the row naming the laptop command, and a
zero-field fetch drew an empty field bar)."""

from __future__ import annotations

from pathlib import Path

from levain.cockpit.verbs import VerbSpec
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

PANELS = [
    {"id": "joins", "title": "Joins pane", "lines": [{"meta": "run", "text": "levain team accept on the laptop"}],
     "action": {"verb": "join_accept", "fields": [{"name": "kind"}, {"name": "handle"}, {"name": "about"}]}},
    {"id": "fetchp", "title": "Fetch pane", "lines": [{"meta": "", "text": "last fetch 2m ago"}],
     "action": {"verb": "fetch_now", "fields": []}},
]


def VERBS() -> dict[str, VerbSpec]:
    return {
        "join_accept": VerbSpec("join_accept", "accept", "accepts a join", "panel", fields=("kind", "handle", "about"),
                                floor="T3", tier_fn=lambda *a: "T3", fire=lambda scope, p, target, confirm: {"summary": "x"}),
        "fetch_now": VerbSpec("fetch_now", "fetch now", "fetches", "panel", fields=(), floor="C1",
                              tier_fn=lambda *a: "C1", fire=lambda scope, p, target, confirm: {"summary": "fetched"}),
    }


def PANEL_OF(title: str) -> str:
    return ("[...document.querySelectorAll('.panel, .ck-card')].find(p => { const h = p.querySelector('h2,h3'); "
            f"return h && /^\\s*{title}/i.test(h.textContent); }})")


def _probe(title: str) -> str:
    p = PANEL_OF(title)
    return (f"(() => {{ const p = {p}; if (!p) return null; const box = p.querySelector('.tray-dump'); "
            "return {box: !!box, bare: !!(box && box.classList.contains('action-bare')), "
            "inputs: p.querySelectorAll('textarea, input').length, "
            "btn: [...p.querySelectorAll('.tray-dump-btn')].map(b => b.textContent.trim()), text: p.innerText.replace(/\\s+/g, ' ')}; })()")


def _page(tmp_path: Path) -> dict:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported",
                extra_verbs=VERBS(), extra_panels=lambda: PANELS) as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T", [
            {"until": f"{PANEL_OF('Fetch pane')}"},
            {"eval": _probe("Joins pane")},
            {"eval": _probe("Fetch pane")},
        ])["values"]


@needs_browser
def test_a_laptop_only_action_draws_no_compose_box_and_the_panel_still_reads(tmp_path: Path) -> None:
    joins = _page(tmp_path)[1]
    assert joins is not None and "levain team accept on the laptop" in joins["text"], joins
    assert joins["box"] is False and joins["inputs"] == 0 and joins["btn"] == [], joins


@needs_browser
def test_a_zero_field_action_is_a_bare_button_not_an_empty_box(tmp_path: Path) -> None:
    fetch = _page(tmp_path)[2]
    assert fetch["bare"] is True and fetch["inputs"] == 0 and fetch["btn"] == ["fetch now"], fetch
