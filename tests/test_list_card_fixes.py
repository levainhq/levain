"""X3 and X4 (codex MED, land 3 round 1) on the List card: a search hit that lives only in another time filter opens the focus
view on that filter, and an ordering chosen in the focus view shows in the card's select when the view closes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_list_page_browser import (TRAY, _FOCUS, _chip, _host_band, _open_focus, _reload, _seed_more, _seed_phone,
                                          _seed_time)

CLOSE = {"eval": "(() => { const x = document.querySelector('#levain-modal .modal-close'); if (x) x.click(); return 1; })()"}


def _wait(ms: int) -> dict:
    return {"eval": f"new Promise(r => setTimeout(() => r(1), {ms}))"}


def _typ(host: str, q: str) -> dict:
    return {"eval": f"(() => {{ const i = {host}.querySelector('.ep-search-input'); i.value = {json.dumps(q)}; "
                    "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"}


FOCUS = (f"JSON.stringify({{k: 'focus', timeChip: ({_FOCUS}.querySelector('.list-time-chip[aria-pressed=true]') || {{dataset: {{}}}}).dataset.time || null,"
         f" rows: [...{_FOCUS}.querySelectorAll('.sp-results .row .clause')].map(c => c.textContent.slice(0, 24))}})")
CARD_SEL = f"JSON.stringify({{k: 'sel', sel: ({TRAY}.querySelector('select.tray-dump-type') || {{value: null}}).value}})"
SET_VIEW = (f"(() => {{ const s = {_FOCUS}.querySelector('select.tray-dump-type'); s.value = s.options[1].value; "
            "s.dispatchEvent(new Event('change', {bubbles: true})); return s.value; })()")


def _vals(out: dict, key: str) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and f'"k":"{key}"' in v]


@needs_browser
def test_x3_open_it_from_a_later_hit_shows_the_matched_row(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root); _seed_phone(root); _seed_time(root)
    _host_band(monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}}, *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            # leave the focus view on Now, as the finding does
            *_open_focus(), {"eval": f"{_chip('time', 'now')}.click(); 1"}, CLOSE, _wait(700),
            _typ(TRAY, "dryer"), _wait(400),                         # a row that lives only in Later
            {"eval": f"(() => {{ const b = {TRAY}.querySelector('[data-list-open-matches]'); if (!b) return 'no open-it button'; b.click(); return 'clicked'; }})()"},
            _wait(900), {"eval": FOCUS},
        ])
    assert "clicked" in out["values"], out["values"]
    (f,) = _vals(out, "focus")
    assert f["rows"], f
    assert any("dryer" in r.lower() for r in f["rows"]), f
    assert f["timeChip"] == "later", f


@needs_browser
def test_x3_open_it_without_a_footer_still_opens_the_focus_view(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root); _seed_phone(root); _seed_time(root)
    _host_band(monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}}, *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            _typ(TRAY, "dryer"), _wait(400),
            # the card's footer is gone (a card that folded nothing and kept no count), the hint's button is not
            {"eval": f"(() => {{ const m = {TRAY}.querySelector('[data-list-more]'); if (m) m.remove(); const b = {TRAY}.querySelector('[data-list-open-matches]'); "
                     "if (!b) return 'no open-it button'; b.click(); return 'clicked'; })()"},
            _wait(900), {"eval": FOCUS},
        ])
    assert "clicked" in out["values"], out["values"]
    (f,) = _vals(out, "focus")
    assert any("dryer" in r.lower() for r in f["rows"]), f


@needs_browser
def test_x4_card_select_shows_the_ordering_chosen_in_the_focus_view(tmp_path: Path, monkeypatch: Any) -> None:
    root, src, _ids = _install(tmp_path)
    _seed_more(root); _seed_phone(root); _seed_time(root)
    _host_band(monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1000, "dpr": 1}}, *_reload(),
            {"until": f"{TRAY} && {TRAY}.querySelector('[data-list-more]') && 1"},
            {"eval": CARD_SEL},
            *_open_focus(), {"eval": SET_VIEW}, _wait(300), CLOSE, _wait(700),
            {"eval": CARD_SEL},
        ])
    before, after = _vals(out, "sel")
    chosen = [v for v in out["values"] if isinstance(v, str) and v and not v.startswith("{")]
    assert before["sel"] == "", before
    assert after["sel"] and after["sel"] in chosen, (after, out["values"])
