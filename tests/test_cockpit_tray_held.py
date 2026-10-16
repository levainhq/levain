"""The Tray's held rows: spores whose surface date has not come. The default Tray still shows the due rows only
and says how many are held back; the held rows ride beside them (the `held` key) so a page can list them on
request and a reader that does not know the key sees the panel it always saw."""

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

HELD_REASON = "surface date not reached"


def _two_held(rig: Rig) -> dict[str, str]:
    """The fixture's one held seed (5 days out) plus an earlier one (2 days out), added later so the order is
    by surface date and not by arrival."""
    soon = rig.store.add(type="task", text="a sooner held seed", disposition="seed",
                         next=(TODAY + timedelta(days=2)).isoformat(), today=TODAY)["id"]
    return {"soon": soon}


def _panel(rig: Rig, **kw: Any) -> dict[str, Any]:
    return rig.ck.panel("tray", credential_class="token", **kw)  # type: ignore[return-value]


# --- provider and engine --------------------------------------------------------------------------


def test_the_held_rows_ride_beside_the_due_rows_in_surface_order(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    extra = _two_held(rig)
    p = _panel(rig)
    assert [r["id"] for r in p["rows"]] == [f"spore:{rig.ids['seed']}"]
    assert [r["id"] for r in p["held"]] == [f"spore:{extra['soon']}", f"spore:{rig.ids['held']}"]
    assert all(r["facets"]["held"] is True for r in p["held"])
    assert [r["facets"]["due"] for r in p["held"]] == sorted(r["facets"]["due"] for r in p["held"])
    assert not any("held" in r["facets"] for r in p["rows"])


def test_the_count_and_the_held_back_line_are_what_they_were(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _two_held(rig)
    p = _panel(rig)
    assert p["filtered"] == [{"count": 2, "reason": HELD_REASON}]
    assert p["count"] == 1 and p["status"] == "ok"
    assert p["title"] == "Tray (1)"
    assert p["groups"] is not None and sum(g["count"] for g in p["groups"]) == 1
    assert all(rid not in ids for ids in p["view_rows"].values() for rid in [r["id"] for r in p["held"]])


def test_a_held_row_carries_the_verbs_any_tray_row_carries(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    p = _panel(rig)
    held = {r["id"]: r for r in p["held"]}[f"spore:{rig.ids['held']}"]
    due = p["rows"][0]
    assert {a["verb"] for a in held["actions"]} == {a["verb"] for a in due["actions"]}
    assert {"spore_surface_at", "spore_update"} <= {a["verb"] for a in held["actions"]}


def test_a_panel_without_held_rows_is_byte_for_byte_what_it_was(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    # the other panels never gain the key, and the Tray gains it only while it holds a held spore
    assert "held" not in rig.ck.panel("loops", credential_class="token")
    assert "held" not in rig.ck.panel("keep", credential_class="token")
    assert "held" in _panel(rig)
    rig.clock.advance(6)       # the surface date has come
    p = _panel(rig)
    assert "held" not in p and p["filtered"] == []


def test_the_etag_covers_the_held_rows(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    before = _panel(rig)["etag"]
    # the held count, the due rows and every head field stay the same: only a held row's own tier moves
    assert rig.post("spore_update", "tray", "held", {"tier": "cold"}).get("ok") is True
    p = _panel(rig)
    assert p["filtered"] == [{"count": 1, "reason": HELD_REASON}] and p["etag"] != before


def test_the_write_path_still_finds_a_held_row(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    got = rig.row("tray", "held")
    assert got["id"] == f"spore:{rig.ids['held']}"


def test_a_reschedule_of_a_held_row_to_a_later_date_lands_and_it_stays_held(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    later = (TODAY + timedelta(days=30)).isoformat()
    assert rig.post("spore_surface_at", "tray", "held", {"surface_at": later}).get("ok") is True
    p = _panel(rig)
    held = {r["id"]: r for r in p["held"]}[f"spore:{rig.ids['held']}"]
    assert held["facets"]["due"] == later


def test_a_held_row_reaching_its_date_moves_from_the_held_rows_to_the_due_rows(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rid = f"spore:{rig.ids['held']}"
    assert rid in [r["id"] for r in _panel(rig)["held"]]
    rig.clock.advance(6)
    p = _panel(rig)
    assert rid in [r["id"] for r in p["rows"]] and "held" not in p


def test_a_search_filters_the_held_rows_with_the_rest(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _two_held(rig)
    p = _panel(rig, q="sooner")
    assert [r["id"].split(":")[1] for r in p["held"]] == [p["held"][0]["id"].split(":")[1]]
    assert len(p["held"]) == 1 and p["held"][0]["body"] == "a sooner held seed"
    assert p["rows"] == []


def test_a_single_row_read_finds_a_held_row(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rid = f"spore:{rig.ids['held']}"
    p = _panel(rig, row=rid)
    assert [r["id"] for r in p["rows"]] == [rid]


def test_held_rows_have_their_own_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path)
    _two_held(rig)
    monkeypatch.setattr(providers, "SPORE_CAP", 1)
    p = _panel(rig)
    assert len(p["rows"]) == 1 and len(p["held"]) == 1
    assert p["status"] == "partial"
    assert {"count": 1, "reason": "held rows over the display cap of 1"} in p["skipped"]
    assert p["filtered"] == [{"count": 2, "reason": HELD_REASON}]


# --- the text surfaces ----------------------------------------------------------------------------


def test_the_text_surface_still_counts_the_held_rows_and_never_lists_them(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _two_held(rig)
    snap = cktext.snapshot(rig.ck, CRED)
    lines = [t for _tone, t in cktext.render_lines(snap, 120)]
    assert any("2 held back — surface date not reached" in t for t in lines), lines
    blob = "\n".join(lines)
    assert "a sooner held seed" not in blob and "a held seed" not in blob


# --- the page -------------------------------------------------------------------------------------

BOX = "document.querySelector('[data-ck-zone] .sp-results') || document.querySelector('.sp-results')"
TRAY = ("[...document.querySelectorAll('#board .panel')].find(p => /^\\s*Tray/.test("
        "(p.querySelector('h2')||{}).textContent||''))")
TOGGLE = f"{TRAY}.querySelector('[data-held-toggle]')"
HELD_ROWS = f"[...{TRAY}.querySelectorAll('.row[data-held]')]"
ALL_ROWS = f"[...{TRAY}.querySelectorAll('.sp-results .row')]"
WAIT = f"{TRAY} && {TRAY}.querySelector('.sp-results') && 1"
OLD_ROWS_TEXT = f"{ALL_ROWS}.map(r => r.textContent)"


def _served(tmp_path: Path, *, keep_seed: bool = True):
    """A served install on the real clock: the page reads today from the server, so the held dates are
    relative to it (the rig's fixed day would put them in the past)."""
    _root, src, ids = _install(tmp_path)
    store = SporeStore(_root / ".levain" / "memory.spores.json")
    real = date.today()
    held_ids = [
        store.add(type="task", text="a sooner held seed", disposition="seed", next=(real + timedelta(days=3)).isoformat())["id"],
        store.add(type="task", text="a later held seed", disposition="seed", next=(real + timedelta(days=10)).isoformat())["id"],
    ]
    if not keep_seed:
        # only held rows are left in the Tray: the due seed and the fixture's now-due held seed leave it
        for key in ("seed", "held"):
            assert store.get(ids[key])
            store.update(ids[key], disposition="note")
    served = _Served(src, surface_tokens={"browser": "T"}, install_class="reported")
    served.store, served.held_ids = store, held_ids + [ids["held"]]  # type: ignore[attr-defined]
    return real, served


def _label(d: date) -> str:
    return d.strftime("%b ") + str(d.day)


@needs_browser
def test_the_page_hides_the_held_rows_by_default_and_a_toggle_shows_them(tmp_path: Path) -> None:
    real, served = _served(tmp_path)
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": f"[{HELD_ROWS}.length, {ALL_ROWS}.length, {TOGGLE} && {TOGGLE}.textContent, {TRAY}.textContent.includes('2 held back')]"},
            {"eval": f"{TOGGLE}.click(); 1"},
            {"eval": f"[{HELD_ROWS}.length, {ALL_ROWS}.length, {TOGGLE}.textContent, {TOGGLE}.getAttribute('aria-expanded')]"},
            {"eval": f"{HELD_ROWS}.map(r => r.textContent)"},
            {"eval": f"{TOGGLE}.click(); {HELD_ROWS}.length"},
        ])
    v = out["values"]
    assert v[1] == [0, 2, "show", True], v
    assert v[3] == [2, 4, "hide", "true"], v
    assert f"surfaces {_label(real + timedelta(days=3))}" in v[4][0] and "a sooner held seed" in v[4][0], v
    assert f"surfaces {_label(real + timedelta(days=10))}" in v[4][1] and "a later held seed" in v[4][1], v
    assert v[5] == 0, v


@needs_browser
def test_the_choice_survives_a_reload_and_the_filter_covers_held_rows_while_shown(tmp_path: Path) -> None:
    _real, served = _served(tmp_path)
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": f"{TOGGLE}.click(); 1"},
            {"eval": "location.reload(); 1"},
            {"until": f"{WAIT} && {TOGGLE} && 1"},
            {"eval": f"new Promise(r => setTimeout(() => r([{HELD_ROWS}.length, {TOGGLE}.textContent]), 500))"},
            {"eval": f"(() => {{ const i = {TRAY}.querySelector('.ep-search-input'); i.value = 'sooner'; "
                     "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"},
            {"eval": f"[{ALL_ROWS}.length, {HELD_ROWS}.length]"},
        ])
    v = out["values"]
    assert v[4] == [2, "hide"], f"the shown choice did not survive a reload: {v}"
    assert v[6] == [1, 1], f"the filter did not cover the held rows: {v}"


@needs_browser
def test_a_blocked_local_storage_leaves_the_toggle_working_for_the_page(tmp_path: Path) -> None:
    _real, served = _served(tmp_path)
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": "Storage.prototype.setItem = function () { throw new Error('blocked'); }; 1"},
            {"eval": f"{TOGGLE}.click(); {HELD_ROWS}.length"},
        ])
    assert out["values"][2] == 2, out


@needs_browser
def test_a_held_row_on_the_page_carries_the_same_verbs_as_a_due_row(tmp_path: Path) -> None:
    _real, served = _served(tmp_path)
    names = "[...%s.querySelectorAll('button')].map(b => b.textContent.trim().toLowerCase()).sort().join(',')"
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": f"{TOGGLE}.click(); 1"},
            {"eval": names % f"{HELD_ROWS}[0]"},
            {"eval": names % f"{ALL_ROWS}.find(r => !r.dataset.held)"},
        ])
    held, due = out["values"][2], out["values"][3]
    assert held and held == due, out


@needs_browser
def test_a_tray_with_only_held_rows_still_offers_them(tmp_path: Path) -> None:
    _real, served = _served(tmp_path, keep_seed=False)
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": f"[{TOGGLE} && 1, {ALL_ROWS}.length, {TRAY}.textContent.includes('nothing waiting')]"},
            {"eval": f"{TOGGLE}.click(); {HELD_ROWS}.length"},
        ])
    assert out["values"][1][0] == 1 and out["values"][2] == 2, out


@needs_browser
def test_a_filter_matching_only_hidden_held_rows_says_so_and_offers_to_show_them(tmp_path: Path) -> None:
    _real, served = _served(tmp_path)
    type_q = (f"(() => {{ const i = {TRAY}.querySelector('.ep-search-input'); i.value = 'sooner'; "
              "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": type_q},
            {"eval": f"[{TRAY}.querySelector('.sp-results .empty').textContent, {ALL_ROWS}.length]"},
            {"eval": f"{TRAY}.querySelector('[data-held-show-matches]').click(); [{HELD_ROWS}.length, {TOGGLE}.textContent]"},
        ])
    v = out["values"]
    assert "no items match" in v[2][0] and "1 held row matches" in v[2][0] and v[2][1] == 0, v
    assert v[3] == [1, "hide"], v


def _filter(q: str) -> str:
    return (f"(() => {{ const i = {TRAY}.querySelector('.ep-search-input'); i.value = {json.dumps(q)}; "
            "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()")


@needs_browser
def test_a_read_with_no_held_rows_does_not_forget_that_they_are_shown(tmp_path: Path) -> None:
    """A read that carries no held rows (a compact read, a filter matching none) must leave the stored choice alone."""
    _real, served = _served(tmp_path)
    strip = ("window.__fetch = window.fetch; window.fetch = async (...a) => { const r = await window.__fetch(...a); "
             "if (!/panel\\/tray/.test(String(a[0] && a[0].url || a[0]))) return r; const j = await r.json(); "
             "delete j.held; j.filtered = []; return new Response(JSON.stringify(j), {status: r.status, headers: r.headers}); }; 1")
    pause = "new Promise(r => setTimeout(() => r(1), 800))"
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": f"{TOGGLE}.click(); {HELD_ROWS}.length"},
            {"eval": strip},
            {"eval": "window.LevainCockpitTransport.reload(); 1"},
            {"eval": pause},
            {"eval": f"[{TOGGLE} && 1, {HELD_ROWS}.length]"},
            {"eval": "window.fetch = window.__fetch; window.LevainCockpitTransport.reload(); 1"},
            {"eval": pause},
            {"eval": f"[{HELD_ROWS}.length, {TOGGLE}.textContent, localStorage.getItem('levain.tray.showHeld')]"},
        ])
    v = out["values"]
    assert v[1] == 2 and v[5] == [None, 0], v    # the stripped read really carried no held rows
    assert v[8] == [2, "hide", "1"], v


@needs_browser
def test_toggling_in_the_focus_modal_redraws_the_board_tray_too(tmp_path: Path) -> None:
    _real, served = _served(tmp_path)
    modal_toggle = "document.querySelector('#levain-modal [data-held-toggle]')"
    with served as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": WAIT},
            {"eval": f"{TRAY}.querySelector('.pexpand').click(); 1"},
            {"until": f"{modal_toggle} && 1"},
            {"eval": f"{modal_toggle}.click(); document.querySelectorAll('#levain-modal .row[data-held]').length"},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
            {"eval": f"[{HELD_ROWS}.length, {TOGGLE}.textContent]"},
        ])
    v = out["values"]
    assert v[3] == 2 and v[5] == [2, "hide"], v
