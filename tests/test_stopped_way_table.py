"""One table owns every kind's way back (harness proposal intention-1712; Diogenes 11-15, levain 663009fbf).

A stopped grant's way-back wording AND its clears-only class live in ``authority._STOPPED_WAYS`` and nowhere else:
``stopped_way``, ``stopped_clears_only`` and ``stopped_way_with`` read it, and every surface (the revoke notice, a list
line, the renew refusal) goes through them. Leg 6 added a verb that makes a hands entry again and two hand-written
copies of the old classification kept saying "can only be cleared"."""
from __future__ import annotations

import re
from pathlib import Path

from levain import authority as A
from levain.consent.grants import stopped_line, way_back, ways_back

ROOT = Path(__file__).resolve().parent.parent / "levain"


def test_a_hands_entry_is_made_again_with_a_key_you_hold_and_says_so():
    kind = A.HANDS_MCP_KIND
    assert not A.stopped_clears_only(kind)
    assert "levain hands mcp register" in A.stopped_way(kind)
    assert way_back(kind).endswith("to use it")
    assert ways_back([("h-1", kind)])[0][0].endswith("with a key you still hold")
    assert stopped_line("a key was revoked", kind).endswith("to use it)")


def test_a_graduation_policy_can_only_be_cleared_and_names_no_key():
    for kind in (A.GRADUATION_KIND, A.GRADUATION_PHONE_KIND):
        assert A.stopped_clears_only(kind)
        assert way_back(kind) == A.stopped_way(kind)
        assert ways_back([("g-1", kind)])[0][0] == A.stopped_way(kind)
        assert A.stopped_way_with(kind, "with a key you hold") == A.stopped_way(kind)


def test_two_kinds_that_share_words_share_a_class():
    """ways_back groups by words, so a way written for two kinds must classify them the same."""
    by_way: dict[str, set[bool]] = {}
    for way, clears in [*A._STOPPED_WAYS.values(), A._STOPPED_WAY_DEFAULT]:
        by_way.setdefault(way, set()).add(clears)
    assert all(len(classes) == 1 for classes in by_way.values()), by_way


def test_an_unnamed_kind_is_created_again_with_a_key():
    assert A.stopped_way(None) == "create it again" and not A.stopped_clears_only(None)
    assert A.stopped_way_with("no-such-kind", "with a key you hold") == "create it again with a key you hold"


def test_no_second_copy_of_a_way_back_is_written_outside_the_table():
    """Every way-back wording is written in authority.py's table and in no other module of levain/."""
    words = {w for w, _ in A._STOPPED_WAYS.values()}
    for path in ROOT.rglob("*.py"):
        if path.name == "authority.py" and path.parent == ROOT:
            continue
        text = path.read_text(encoding="utf-8")
        hit = [w for w in words if w in text]
        assert not hit, f"{path} writes a way back the authority table owns: {hit}"
