"""`/?view=needs&item=<id>`: a deep link to one Needs-you card that does not depend on a sign request (a band row with fewer than
two options files no sign card, so its ping could only open the home view). The page opens the card's focus view and scrolls to
the strip's card; an id that is not waiting falls back to the plain Needs view with a one-line notice; the address is cleaned
and the link, like the other two, survives the write-token strip (the token rides the fragment)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_band_title import TITLE, _two_questions
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP

READ = ("JSON.stringify({ title: " + TITLE + " || '', modal: !!document.getElementById('levain-modal'), search: location.search, y: Math.round(window.scrollY),"
        " note: (document.querySelector('.needs-gone-note') || {}).textContent || '' })")
ROWS = "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1"
SETTLE = {"eval": "new Promise(r => setTimeout(() => r(1), 2500))"}


def _open_link(tmp_path: Path, monkeypatch, query: str) -> dict:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/?{query}#write_token=T", [DESKTOP, {"until": ROWS, "ms": 40000}, SETTLE, {"eval": READ}])["values"]
    return json.loads(out[-1])


@needs_browser
def test_an_item_link_opens_that_card_and_scrolls_to_it_through_the_token_strip(tmp_path: Path, monkeypatch) -> None:
    got = _open_link(tmp_path, monkeypatch, "view=needs&item=question:2")
    assert got["modal"] and got["title"].endswith("· codex"), got      # question 2 is codex's, not the first card
    assert got["search"] == "" and got["note"] == "", got              # the address is cleaned, nothing said
    assert got["y"] > 100, got                                          # the strip's card was scrolled to


@needs_browser
def test_flows_np_id_names_the_same_card_as_its_card_number(tmp_path: Path, monkeypatch) -> None:
    got = _open_link(tmp_path, monkeypatch, "view=needs&item=np-00000001")     # card_number("np-00000001") == 2
    assert got["modal"] and got["title"].endswith("· codex") and got["search"] == "", got


@needs_browser
def test_an_unknown_item_falls_back_to_the_needs_view_with_a_notice(tmp_path: Path, monkeypatch) -> None:
    got = _open_link(tmp_path, monkeypatch, "view=needs&item=np-deadbeef")
    assert got["modal"] is False and got["search"] == "", got
    assert got["note"] == "That item is no longer waiting.", got
    assert got["y"] > 100, got                                          # still at the Needs strip, as the plain link goes
