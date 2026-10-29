"""C12 follow-up, session B: the `board` VALUE kind. A downstream cockpit panel (flow's) declares `kind="board"` and sends levain's
board.json value (`levain.cockpit.board.build`); the kernel validates its shape (it parses and executes nothing), lists it in the
manifest, and the page draws it grouped under column headings on the tab and as the six columns in the panel's ⤢ modal.

The shape tests are in-process; the draw test is real headless Chrome over a served cockpit holding a seeded board-kind provider
(`LEVAIN_BOARD_SHOTS=<dir>` keeps its screenshots there; the default is the test's tmp dir)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import Cockpit, CockpitRegistrationError, ProviderSpec, Read
from levain.cockpit import board
from levain.cockpit.engine import BOARD_CATEGORIES, KINDS
from levain.consent.questions import QuestionStore
from tests.test_board_grouped_browser import MODAL, ORDER, SETTLED, _seed
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_questions import lh  # noqa: F401  (fixture)
from tests.test_cockpit_scroll_keep_page import _Served

CRED = {"class": "none", "device_class": None}


def _card(cid: str, **kw: Any) -> dict[str, Any]:
    return {"id": cid, "repo": "levain", "title": f"Card {cid}", "done_line": "green", "tier": "sonnet", "depends_on": [],
            "status": "ready", "status_category": "ready", "held_reason": None, "well_formed": True, "rank": 1,
            "unknown_category": None, "journal": None, **kw}


def _value(**by_cat: list[dict[str, Any]]) -> dict[str, Any]:
    cols = [{"category": c, "count": len(by_cat.get(c.replace("-", "_"), [])), "cards": by_cat.get(c.replace("-", "_"), [])}
            for c in BOARD_CATEGORIES]
    return {"schema": 1, "generated": "2026-10-29T12:00:00Z", "queue": "ok", "unreadable": [], "note": None, "columns": cols}


def _panel(value: Any, **kw: Any) -> dict[str, Any] | None:
    ck = Cockpit()
    ck.register(ProviderSpec(id="b", kind="board", title="Board", priority="feed", read=lambda c: Read(value=value), **kw))
    return ck.panel("b")


def _refused(value: Any, *needles: str) -> None:
    p = _panel(value)
    assert p is not None and p["status"] == "error", p
    assert "'b'" in p["error"] and "(board)" in p["error"], p["error"]      # the panel and its kind are named
    for n in needles:
        assert n in p["error"], (n, p["error"])


# --- the kernel's vocabulary ---------------------------------------------------------------------

def test_the_board_kind_is_in_the_vocabulary_and_its_categories_are_the_coordinators() -> None:
    from levain.coordinator import ready
    assert "board" in KINDS
    assert BOARD_CATEGORIES == ("backlog", "ready", "running", "waiting", "needs-you", "done") == ready.CATEGORIES == board.CATEGORIES


def test_a_board_provider_registers_and_appears_in_the_manifest() -> None:
    ck = Cockpit()
    val = _value(ready=[_card("levain:a")], needs_you=[{"id": "band:1", "title": "Ship it", "band": True, "number": 3, "journal": None}])
    ck.register(ProviderSpec(id="b", kind="board", title="Board", priority="feed", read=lambda c: Read(value=val)))
    man = ck.manifest(CRED)
    assert man["panels"]["b"]["kind"] == "board"
    got = ck.panel("b")
    assert got["status"] == "ok" and got["kind"] == "board"
    assert [c["category"] for c in got["value"]["columns"]] == list(BOARD_CATEGORIES)     # the value goes out as sent
    assert got["value"]["columns"][1]["cards"][0]["id"] == "levain:a"
    # a registered board still has no row ordering, and is no widget (one answer, never a list)
    with pytest.raises(CockpitRegistrationError):
        Cockpit().register(ProviderSpec(id="b", kind="board", title="B", priority="feed", read=lambda c: Read(value={}), order="time.desc"))
    with pytest.raises(CockpitRegistrationError):
        Cockpit().register(ProviderSpec(id="b", kind="board", title="B", priority="feed", read=lambda c: Read(value={}), region="widgets"))


def test_levains_own_board_value_is_accepted_whole(tmp_path: Path, lh) -> None:  # noqa: F811
    """The shape a downstream emits is `board.build`'s: a real build over a seeded store (band card, journal lines, a held card)
    passes the validator with every extra key (band_hint, tell, journal_as_of_read) left alone."""
    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    built = board.build(lh, QuestionStore(lh))
    p = _panel(copy.deepcopy(built))
    assert p is not None and p["status"] == "ok", p
    assert p["value"]["band_hint"] == built["band_hint"] and p["value"]["tell"] == built["tell"]
    assert [c["count"] for c in p["value"]["columns"]] == [2, 1, 1, 0, 1, 1]


# --- what the validator refuses ------------------------------------------------------------------

def test_the_validator_refuses_a_bad_category_a_non_list_columns_and_a_card_with_no_id_or_title() -> None:
    good = _value(ready=[_card("levain:a")])
    assert _panel(good)["status"] == "ok"
    _refused({**good, "columns": [{"category": "icebox", "count": 0, "cards": []}]}, "value.columns[0].category", "icebox")
    _refused({**good, "columns": {"ready": []}}, "value.columns", "list")
    _refused({k: v for k, v in good.items() if k != "columns"}, "value.columns")
    _refused({**good, "columns": ["ready"]}, "value.columns[0]")
    _refused({**good, "columns": [{"category": "ready", "count": 0, "cards": "none"}]}, "value.columns[0].cards", "list")
    no_id = _value(ready=[{k: v for k, v in _card("x").items() if k != "id"}])
    _refused(no_id, "value.columns[1].cards[0].id")
    _refused(_value(ready=[_card("")]), "value.columns[1].cards[0].id")
    _refused(_value(ready=[_card(7)]), "value.columns[1].cards[0].id")                        # type: ignore[arg-type]
    _refused(_value(ready=[_card("x", title=None)]), "value.columns[1].cards[0].title")
    _refused(_value(ready=["levain:a"]), "value.columns[1].cards[0]")
    _refused(_value(ready=[_card("x", journal="moved")]), "value.columns[1].cards[0].journal")
    _refused({**good, "queue": "maybe"}, "value.queue")
    _refused({**good, "unreadable": "band"}, "value.unreadable")
    _refused({**good, "note": 5}, "value.note")
    _refused([good], "value")


def test_a_category_is_named_once_and_two_columns_of_one_category_are_refused() -> None:
    good = _value()
    _refused({**good, "columns": good["columns"] + [good["columns"][0]]}, "value.columns[6].category", "repeated")


def test_the_value_is_bounded_and_the_refusal_names_the_count() -> None:
    from levain.cockpit.engine import BOARD_CARDS_MAX
    cards = [_card(f"levain:{i}") for i in range(BOARD_CARDS_MAX + 1)]
    _refused(_value(backlog=cards), "cards", str(BOARD_CARDS_MAX))
    assert _panel(_value(backlog=cards[:BOARD_CARDS_MAX]))["status"] == "ok"


def test_a_board_with_no_card_in_any_column_is_empty_but_a_queue_that_could_not_be_read_is_not() -> None:
    assert _panel(_value())["status"] == "empty"
    assert _panel({**_value(), "queue": "none", "note": "no queue yet"})["status"] == "ok"             # the note says why: not "nothing here"
    assert _panel({**_value(), "queue": "unreadable"})["status"] == "ok"
    assert _panel({**_value(), "unreadable": ["band"]})["status"] == "ok"          # a source it could not read is named, never "nothing here"
    assert _panel(_value(done=[_card("levain:z")]))["status"] == "ok"


def test_the_board_kind_is_a_read_only_value_no_verb_versions_it() -> None:
    from levain.cockpit.engine import VERSIONED_VALUE_KINDS, VALUE_VALIDATORS, ROW_KINDS
    assert "board" in VALUE_VALIDATORS and "board" not in VERSIONED_VALUE_KINDS and "board" not in ROW_KINDS
    assert "value_version" not in _panel(_value())["value"]


# --- the page ------------------------------------------------------------------------------------

def _board_provider(value: dict[str, Any]) -> ProviderSpec:
    return ProviderSpec(id="boardkind", kind="board", title="Ready board", priority="feed", read=lambda c: Read(value=value),
                        region="operate")


@needs_browser
def test_a_board_kind_panel_draws_grouped_and_its_expand_modal_draws_the_six_columns(tmp_path: Path, lh) -> None:  # noqa: F811
    from levain.cockpit.providers import build_default_cockpit

    lh.mkdir(parents=True, exist_ok=True, mode=0o700)
    _seed(lh)
    value = board.build(lh, QuestionStore(lh))
    shots = Path(os.environ.get("LEVAIN_BOARD_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    ck.register(_board_provider(value))
    panel = "document.querySelector('[data-ck-panel=boardkind]')"
    facts = (f"JSON.stringify({{heads: [...{panel}.querySelectorAll('.grp-head')].map(h => h.textContent), "
             f"cats: [...{panel}.querySelectorAll('.grp-head')].map(h => h.dataset.category), cols: {panel}.querySelectorAll('.bd-cols').length, "
             f"colSections: {panel}.querySelectorAll('.bd-col').length, rows: {panel}.querySelectorAll('.bd-row').length, "
             f"expand: {panel}.querySelectorAll('.phead .pexpand').length, tells: {panel}.querySelectorAll('.bd-tell, textarea').length, "
             f"held: [...{panel}.querySelectorAll('.bd-held')].map(h => h.textContent)}})")
    modal = (f"JSON.stringify({{cols: [...{MODAL}.querySelectorAll('.bd-col')].map(c => c.dataset.category), "
             f"cards: {MODAL}.querySelectorAll('.bd-card').length, heads: {MODAL}.querySelectorAll('.grp-head').length, "
             f"tells: {MODAL}.querySelectorAll('.bd-tell, textarea').length, title: {MODAL}.querySelector('h2').textContent}})")
    with _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1440, "h": 1000, "dpr": 1}},
            {"until": f"{panel} && {panel}.querySelectorAll('.grp-head').length === 6"},
            {"eval": facts},
            {"eval": f"{panel}.scrollIntoView(); 1"},
            {"shot": str(shots / "board_tab_grouped_desktop-kind.png")},
            {"eval": f"{panel}.querySelector('.phead .pexpand').click(); 1"},
            {"until": f"{MODAL} && {MODAL}.querySelectorAll('.bd-col').length === 6"},
            {"eval": modal},
            {"until": SETTLED},
            {"shot": str(shots / "board_modal_columns_desktop-kind.png")},
            {"key": "Escape"},
            {"until": f"!{MODAL} && !document.body.classList.contains('modal-open')"},
            {"eval": facts},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{panel}.scrollIntoView(); 1"},
            {"until": f"{panel}.querySelectorAll('.grp-head').length === 6 && document.documentElement.scrollWidth <= window.innerWidth + 1"},
            {"shot": str(shots / "board_tab_grouped_phone-kind.png")},
            {"eval": f"{panel}.querySelector('.phead .pexpand').click(); 1"},
            {"until": f"{MODAL} && {MODAL}.querySelectorAll('.bd-col').length === 6"},
            {"until": SETTLED},
            {"shot": str(shots / "board_modal_columns_phone-kind.png")},
        ])
    v = out["values"]
    first = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"heads"')))
    assert first["heads"] == ["backlog 2", "ready 1", "running 1", "waiting 0", "needs-you 1", "done 1"], first    # one heading per column
    assert first["cats"] == ORDER and first["cols"] == 0 and first["colSections"] == 0, first                        # grouped: no columns body
    assert first["rows"] == 6 and first["held"] == ["held: depends on levain:a"], first
    assert first["expand"] == 1 and first["tells"] == 0, first      # the kind carries the focus button, and no write box
    opened = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"cols":["')))
    assert opened["cols"] == ORDER and opened["cards"] == 6 and opened["heads"] == 0 and opened["tells"] == 0, opened
    assert opened["title"] == "Ready board", opened
    closed = [json.loads(x) for x in v if isinstance(x, str) and x.startswith('{"heads"')]
    assert closed[-1] == first, closed[-1]                          # Esc closed the modal; the panel is as it was
    assert not out["posts"], out["posts"]                           # a board kind carries no write verb: nothing posted


@needs_browser
def test_a_board_kind_panel_says_so_when_the_board_view_did_not_load(tmp_path: Path) -> None:
    """With `window.LevainBoardView` absent the panel draws one plain line, never a blank panel."""
    from levain.cockpit.providers import build_default_cockpit

    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    ck.register(_board_provider(_value(ready=[_card("levain:a")])))
    panel = "document.querySelector('[data-ck-panel=boardkind]')"
    with _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{panel} && {panel}.querySelector('.grp-head')"},
            {"eval": "delete window.LevainBoardView; window.LevainCockpitView.refetchPanel('boardkind')"},
            {"until": f"{panel}.querySelector('.xnote') && {panel}.querySelector('.xnote').textContent"},
            {"eval": f"JSON.stringify({{heads: {panel}.querySelectorAll('.grp-head').length, text: {panel}.querySelector('.xbody').textContent}})"},
        ])
    got = json.loads(out["values"][-1])
    assert got["heads"] == 0 and "board view did not load" in got["text"], got
