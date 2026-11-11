"""The empty Tray and Open loops sentences name the install's own entity, never a fixed name."""

from __future__ import annotations

import json
from pathlib import Path

from anneal_memory.spores import IntentionStore

from levain.cockpit import providers
from levain.cockpit.providers import INTENTION_EMPTY_ENTITY, build_default_cockpit, intention_empty
from levain.dashboard import SubstrateSource
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_scroll_keep_page import _Served
from tests.test_dashboard import _make_levain_install

NAME = "Quill"
OLD = "Sage"      # the name the fixture install and the old fixed text carried


def _install(tmp_path: Path, *, store: bool, config: bool) -> SubstrateSource:
    """An install whose entity is NAME, with a Tray and Open loops that hold nothing (a store whose only intention is done, or no store at all; a legacy note now renders in the Tray)."""
    root = _make_levain_install(tmp_path)
    (root / "seed" / "origin.md").write_text(f"# Who You Are — {NAME}\n\nYou are {NAME}.\n", encoding="utf-8")
    if config:
        (root / ".levain" / "config.json").write_text(json.dumps({"entity_name": NAME}), encoding="utf-8")
    if store:
        st = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")     # a store file that holds nothing live: one intention, already descended
        st.descend(st.add(type="task", text="a done item", disposition="seed")["id"], kind="done")
    return SubstrateSource.local(root)


def test_the_wording_names_the_entity_and_falls_back_to_the_capture_boxs_name() -> None:
    assert intention_empty("tray", NAME) == f"Nothing yet. Drop a line in the box above and {NAME} sorts it."
    assert intention_empty("loops", NAME) == f"Nothing yet. What stays open after {NAME} sorts the List is listed here."
    for blank in (None, "", "   ", 3):
        assert INTENTION_EMPTY_ENTITY in intention_empty("tray", blank)
    assert OLD not in "".join(providers.INTENTION_EMPTY.values())


def test_an_empty_store_names_the_entity_the_install_resolves(tmp_path: Path) -> None:
    ck = build_default_cockpit(_install(tmp_path, store=True, config=False))
    for pid in ("tray", "loops"):
        p = ck.panel(pid, credential_class="token")
        assert p["status"] == "empty", p["status"]
        assert NAME in p["empty"] and OLD not in p["empty"], p["empty"]


def test_a_store_that_does_not_exist_yet_names_the_configured_entity(tmp_path: Path) -> None:
    ck = build_default_cockpit(_install(tmp_path, store=False, config=True))
    for pid in ("tray", "loops"):
        p = ck.panel(pid, credential_class="token")
        assert NAME in p["empty"] and OLD not in p["empty"], p["empty"]


def test_a_store_that_does_not_exist_yet_names_the_origin_h1_entity(tmp_path: Path) -> None:
    ck = build_default_cockpit(_install(tmp_path, store=False, config=False))
    for pid in ("tray", "loops"):
        p = ck.panel(pid, credential_class="token")
        assert p["status"] == "empty", p["status"]
        assert NAME in p["empty"] and OLD not in p["empty"], p["empty"]


def test_a_rename_with_no_store_shows_on_the_next_read(tmp_path: Path) -> None:
    src = _install(tmp_path, store=False, config=True)
    ck = build_default_cockpit(src)
    assert NAME in ck.panel("tray", credential_class="token")["empty"]
    (src.install_root / ".levain" / "config.json").write_text(json.dumps({"entity_name": "Sol"}), encoding="utf-8")
    p = ck.panel("tray", credential_class="token")
    assert "Sol" in p["empty"] and NAME not in p["empty"], p["empty"]


@needs_browser
def test_the_page_names_the_entity_in_the_empty_tray_and_loops(tmp_path: Path) -> None:
    # no store at all: a store holding one done intention makes the List card say "Nothing due for you." instead (a done row is
    # the focus view's, levain/templates/web/dashboard_core.js `apart`), which is that card's own text and not the entity-named empty
    src = _install(tmp_path, store=False, config=True)
    texts = ("[...document.querySelectorAll('#board .panel')].filter(p => /^\\s*(List|Open loops)/.test("
             "(p.querySelector('h2')||{}).textContent||'')).map(p => (p.querySelector('.empty')||{}).textContent||'')")
    with _Served(src, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": "document.querySelector('#board .panel .empty') && 1"},
            {"eval": texts},
        ])
    got = out["values"][1]
    assert len(got) == 2, got
    assert all(NAME in t and OLD not in t for t in got), got
