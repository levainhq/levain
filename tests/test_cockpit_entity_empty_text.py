"""The empty Tray and Open loops sentences name the install's own entity, never a fixed name."""

from __future__ import annotations

import json
from pathlib import Path

from anneal_memory.spores import SporeStore

from levain.cockpit import providers
from levain.cockpit.providers import SPORE_EMPTY_ENTITY, build_default_cockpit, spore_empty
from levain.dashboard import SubstrateSource
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_scroll_keep_page import _Served
from tests.test_dashboard import _make_levain_install

NAME = "Quill"
OLD = "Sage"      # the name the fixture install and the old fixed text carried


def _install(tmp_path: Path, *, store: bool, config: bool) -> SubstrateSource:
    """An install whose entity is NAME, with a Tray and Open loops that hold nothing (a Keep note only, or no store at all)."""
    root = _make_levain_install(tmp_path)
    (root / "seed" / "origin.md").write_text(f"# Who You Are — {NAME}\n\nYou are {NAME}.\n", encoding="utf-8")
    if config:
        (root / ".levain" / "config.json").write_text(json.dumps({"entity_name": NAME}), encoding="utf-8")
    if store:
        SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="a keep note", disposition="note")
    return SubstrateSource.local(root)


def test_the_wording_names_the_entity_and_falls_back_to_the_capture_boxs_name() -> None:
    assert spore_empty("tray", NAME) == f"Nothing yet. Drop a thought in the box above and {NAME} sorts it."
    assert spore_empty("loops", NAME) == f"Nothing yet. What stays open after {NAME} sorts the Tray is listed here."
    for blank in (None, "", "   ", 3):
        assert SPORE_EMPTY_ENTITY in spore_empty("tray", blank)
    assert OLD not in "".join(providers.SPORE_EMPTY.values())


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
    src = _install(tmp_path, store=True, config=False)
    texts = ("[...document.querySelectorAll('#board .panel')].filter(p => /^\\s*(Tray|Open loops)/.test("
             "(p.querySelector('h2')||{}).textContent||'')).map(p => (p.querySelector('.empty')||{}).textContent||'')")
    with _Served(src, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": "document.querySelector('#board .panel .empty') && 1"},
            {"eval": texts},
        ])
    got = out["values"][1]
    assert len(got) == 2, got
    assert all(NAME in t and OLD not in t for t in got), got
