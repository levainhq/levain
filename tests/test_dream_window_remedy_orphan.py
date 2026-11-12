"""Integration e3: after D1-O the dream clears a wrap in two cases, so the wrap-open card text says who clears what
(the dream its own stale wrap, a seat's only when the install's ``orphan_floor_hours`` is set) and still prints the two
anneal commands for the rest. Every `levain ...` command the text prints is an argv the CLI accepts."""
from __future__ import annotations

import re
from pathlib import Path

from levain.dream import agent as da
from tests.test_cockpit_anchor_reset import _verdict

DB = Path("/tmp/some store/memory.db")


def _text() -> str:
    return da.window_remedy("wrap-open", DB)


def test_the_text_no_longer_says_nothing_in_the_dream_clears_a_wrap():
    t = _text()
    assert "nothing in the dream clears" not in t
    assert "The dream clears its own stale wrap by itself" in t
    assert "it clears a seat's wrap only when this install's `orphan_floor_hours` is set" in t
    assert "until then, or while the wrap is younger, it stays until you clear it" in t


def test_the_text_keeps_the_two_anneal_commands_and_the_hand_start():
    t = _text()
    assert " wrap-status`" in t and "wrap-cancel --wrap-token <token>`" in t
    assert "--db '/tmp/some store/memory.db'" in t
    assert "levain dream start" in t


def test_the_floor_command_names_the_installs_real_store_directory():
    t = _text()
    assert "`levain dream enable --store-dir '/tmp/some store' --orphan-floor-hours <hours>`" in t


def test_every_levain_command_the_text_prints_parses():
    cache: dict = {}
    cmds = [m.group(1).strip() for m in re.finditer(r"`(levain [^`]+)`", _text())]
    assert any(c.startswith("levain dream enable ") for c in cmds) and any(c.startswith("levain dream start") for c in cmds)
    assert [c for c in cmds if _verdict(c, cache) is not None] == []


def test_the_other_classes_are_unchanged():
    for cls in ("baton", "failed", "laptop-slept", "attempt-spent", "ceiling", "in-flight"):
        assert "orphan_floor_hours" not in da.window_remedy(cls, DB)
