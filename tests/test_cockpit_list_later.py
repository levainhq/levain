"""The List's Later rows: spores (and band rows, drops and talks) whose wake date has not come. They are rows of the
panel's `later` group, in wake order, each saying where it lands when it wakes; the card names them in one control and the
focus view lists them under the Later time chip. (This file held the Tray's `held` list; that wire key is gone: the word is
Later and it is a group like every other, so a text surface and every reader see the rows.)"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

import levain.cockpit.providers as providers
from levain.cockpit import text as cktext
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import CRED, TODAY, Rig, _install
from tests.test_cockpit_scroll_keep_page import _Served


def _two_later(rig: Rig) -> dict[str, str]:
    """The fixture's one dated seed (5 days out) plus an earlier one (2 days out), added later so the order is
    by wake date and not by arrival."""
    soon = rig.store.add(type="task", text="a sooner later seed", disposition="seed",
                         next=(TODAY + timedelta(days=2)).isoformat(), today=TODAY)["id"]
    return {"soon": soon}


def _panel(rig: Rig, **kw: Any) -> dict[str, Any]:
    return rig.ck.panel("tray", credential_class="token", **kw)  # type: ignore[return-value]


def _later(p: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in p["rows"] if r["group"] == "later"]


# --- provider and engine --------------------------------------------------------------------------


def test_the_later_rows_are_rows_in_wake_order(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    extra = _two_later(rig)
    p = _panel(rig)
    assert [r["id"] for r in _later(p)] == [f"spore:{extra['soon']}", f"spore:{rig.ids['held']}"]
    assert all(r["facets"]["time_state"] == "later" for r in _later(p))
    assert [r["facets"]["due"] for r in _later(p)] == sorted(r["facets"]["due"] for r in _later(p))
    assert "held" not in p and not any("held" in r["facets"] for r in p["rows"])


def test_the_count_is_what_is_due_and_later_is_not_counted(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _two_later(rig)
    p = _panel(rig)
    assert p["filtered"] == []
    assert p["count"] == 2 and p["title"] == "List (2)"    # the undated seed (Someday) and the legacy note count; later does not
    assert {g["id"]: g["count"] for g in p["groups"]}["later"] == 2


def test_a_later_row_carries_the_verbs_any_row_carries(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    p = _panel(rig)
    later = {r["id"]: r for r in p["rows"]}[f"spore:{rig.ids['held']}"]
    due = next(r for r in p["rows"] if r["group"] != "later" and r["id"].startswith("spore:"))
    assert {a["verb"] for a in later["actions"]} == {a["verb"] for a in due["actions"]}
    assert {"spore_surface_at", "spore_update"} <= {a["verb"] for a in later["actions"]}


def test_the_held_key_is_on_no_panel(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert "held" not in rig.ck.panel("loops", credential_class="token")
    assert "held" not in _panel(rig)


def test_the_etag_covers_the_later_rows(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    before = _panel(rig)["etag"]
    assert rig.post("spore_update", "tray", "held", {"tier": "cold"}).get("ok") is True
    assert _panel(rig)["etag"] != before


def test_the_write_path_still_finds_a_later_row(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    got = rig.row("tray", "held")
    assert got["id"] == f"spore:{rig.ids['held']}"


def test_a_reschedule_of_a_later_row_to_a_later_date_lands_and_it_stays_later(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    later = (TODAY + timedelta(days=30)).isoformat()
    assert rig.post("spore_surface_at", "tray", "held", {"surface_at": later}).get("ok") is True
    row = {r["id"]: r for r in _panel(rig)["rows"]}[f"spore:{rig.ids['held']}"]
    assert row["facets"]["due"] == later and row["group"] == "later" and row["facets"]["wake_bucket"] == "later"


def test_a_snooze_moves_a_due_row_to_later_on_the_very_next_read(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rid = f"spore:{rig.ids['seed']}"
    assert {r["id"]: r for r in _panel(rig)["rows"]}[rid]["group"] != "later"
    assert rig.post("spore_surface_at", "tray", "seed", {"surface_at": (TODAY + timedelta(days=3)).isoformat()}).get("ok") is True
    row = {r["id"]: r for r in _panel(rig)["rows"]}[rid]
    assert row["group"] == "later" and row["facets"]["wake_line"].endswith("\u2192 list"), row


def test_a_later_row_reaching_its_date_moves_to_the_due_rows(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rid = f"spore:{rig.ids['held']}"
    assert rid in [r["id"] for r in _later(_panel(rig))]
    rig.clock.advance(6)
    p = _panel(rig)
    assert rid in [r["id"] for r in p["rows"]] and rid not in [r["id"] for r in _later(p)]


def test_a_search_filters_the_later_rows_with_the_rest(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _two_later(rig)
    p = _panel(rig, q="sooner")
    assert [r["body"] for r in p["rows"]] == ["a sooner later seed"]


def test_a_single_row_read_finds_a_later_row(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rid = f"spore:{rig.ids['held']}"
    p = _panel(rig, row=rid)
    assert [r["id"] for r in p["rows"]] == [rid]


def test_later_rows_have_their_own_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path)
    _two_later(rig)
    monkeypatch.setattr(providers, "SPORE_CAP", 1)
    p = _panel(rig)
    assert len(_later(p)) == 1 and len([r for r in p["rows"] if r["group"] == "someday"]) == 1


# --- the text surfaces ----------------------------------------------------------------------------


def test_the_text_surface_lists_the_later_rows_under_their_heading(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _two_later(rig)
    snap = cktext.snapshot(rig.ck, CRED)
    lines = [t for _tone, t in cktext.render_lines(snap, 120)]
    assert any("Later (2)" in t for t in lines), lines
    blob = "\n".join(lines)
    assert "a sooner later seed" in blob and "a held seed" in blob


# --- the page -------------------------------------------------------------------------------------

TRAY = ("[...document.querySelectorAll('#board .panel')].find(p => /^\\s*List/.test("
        "(p.querySelector('h2')||{}).textContent||''))")
LATER_ROWS = f"[...{TRAY}.querySelectorAll('.row[data-time-state=\"later\"]')]"
ALL_ROWS = f"[...{TRAY}.querySelectorAll('.sp-results .row')]"
WAIT = f"{TRAY} && {TRAY}.querySelector('.sp-results') && 1"


def _served(tmp_path: Path, *, keep_seed: bool = True):
    """A served install on the real clock: the page reads today from the server, so the later dates are
    relative to it (the rig's fixed day would put them in the past)."""
    _root, src, ids = _install(tmp_path)
    store = SporeStore(_root / ".levain" / "memory.spores.json")
    real = date.today()
    store.update(ids["note"], disposition="loop")    # the fixture's legacy note would show in the Tray (the Keep is gone)
    later_ids = [
        store.add(type="task", text="a sooner later seed", disposition="seed", next=(real + timedelta(days=3)).isoformat())["id"],
        store.add(type="task", text="a farther later seed", disposition="seed", next=(real + timedelta(days=10)).isoformat())["id"],
    ]
    if not keep_seed:
        # only later rows are left in the List: the due seed and the fixture's now-due dated seed move to Open loops
        for key in ("seed", "held"):
            assert store.get(ids[key])
            store.update(ids[key], disposition="loop")
    served = _Served(src, surface_tokens={"browser": "T"}, install_class="reported")
    served.store, served.later_ids = store, later_ids + [ids["held"]]  # type: ignore[attr-defined]
    return real, served


# The List's card names what it leaves out in ONE control and the focus view lists the later rows under the Later time chip.
MORE = f"{TRAY}.querySelector('[data-list-more]')"
FOCUS = "document.querySelector('#levain-modal .modal-body')"
FOCUS_LATER = f"[...{FOCUS}.querySelectorAll('.row[data-time-state=\"later\"]')]"


def _open_focus() -> list[dict[str, Any]]:
    return [{"eval": f"{MORE}.click(); 1"},
            {"until": f"{FOCUS} && {FOCUS}.querySelector('.sp-results') && 1"}]


LATER_CHIP = f"{FOCUS}.querySelector('.list-time-chip[data-time=\"later\"]')"


def _open_later() -> list[dict[str, Any]]:
    """The focus view lists the rows of every time-state under their own headings; the Later chip narrows it to them."""
    return [*_open_focus(), {"eval": f"{FOCUS_LATER}.length"}, {"eval": f"{LATER_CHIP}.click(); 1"},
            {"until": f"{FOCUS}.querySelectorAll('.sp-results .row').length === {FOCUS_LATER}.length && 1"}]


@needs_browser
def test_the_card_hides_the_later_rows_and_names_them_in_one_control_and_the_focus_view_lists_them(tmp_path: Path) -> None:
    real, served = _served(tmp_path)
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{WAIT} && {MORE} && 1"},
            {"eval": f"[{LATER_ROWS}.length, {MORE}.textContent.trim()]"},
            *_open_later(),
            {"eval": f"{FOCUS_LATER}.map(r => r.textContent)"},
            {"eval": f"[...{FOCUS}.querySelectorAll('.grp-sub')].map(h => h.textContent)"},
        ])
    v = out["values"]
    assert v[1][0] == 0 and v[1][1] == "later 2", v          # the card draws no later row, and names them
    rows = v[-2]
    assert len(rows) == 2 and "a sooner later seed" in rows[0] and "a farther later seed" in rows[1], v
    assert f"wakes " in rows[0] and "\u2192 list" in rows[0], v                # each says where it lands
    assert v[-1] == ["Next 7 days", "Later dates"], v                            # grouped by the day it wakes


@needs_browser
def test_a_later_row_in_the_focus_view_carries_the_same_verbs_as_a_due_row(tmp_path: Path) -> None:
    _real, served = _served(tmp_path)
    names = "[...%s.querySelectorAll('button')].map(b => b.textContent.trim().toLowerCase()).sort().join(',')"
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{WAIT} && {MORE} && 1"},
            *_open_later(),
            {"eval": names % f"{FOCUS_LATER}[0]"},
            {"eval": f"{LATER_CHIP}.click(); 1"},
            {"eval": names % f"[...{FOCUS}.querySelectorAll('.sp-results .row[data-time-state=\"someday\"]')][0]"},
        ])
    later, due = out["values"][-3], out["values"][-1]
    assert later and later == due, out


@needs_browser
def test_a_list_with_only_later_rows_still_offers_them(tmp_path: Path) -> None:
    _real, served = _served(tmp_path, keep_seed=False)
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{WAIT} && {MORE} && 1"},
            {"eval": f"[{MORE}.textContent.trim(), {ALL_ROWS}.length]"},
            *_open_later(),
            {"eval": f"{FOCUS_LATER}.length"},
        ])
    assert out["values"][1] == ["later 2", 0] and out["values"][-1] == 2, out


@needs_browser
def test_the_focus_views_filter_covers_the_later_rows(tmp_path: Path) -> None:
    _real, served = _served(tmp_path)
    q = (f"(() => {{ const i = {FOCUS}.querySelector('.ep-search-input'); i.value = 'sooner'; "
         "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": f"{WAIT} && {MORE} && 1"},
            *_open_focus(),
            {"eval": q},
            {"eval": f"[{FOCUS}.querySelectorAll('.sp-results .row').length, {FOCUS_LATER}.length]"},
        ])
    assert out["values"][-1] == [1, 1], out
