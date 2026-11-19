"""The p10a race-outcome helper (levain#150): a member passes, a non-member fails showing the set, and every member of
every set names a producing symbol that really is in levain/ (derived by ``git grep``, never counted by hand)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import _p10a_races as races

ROOT = Path(__file__).resolve().parent.parent
CTX = {"oid": "oc-abc123", "link": "link-1"}
ALL = [(race, m) for race, members in races.RACES.items() for m in members]


def test_a_member_passes_by_name_and_by_text():
    assert races.assert_honest("revoke_vs_abandon", races.ABANDON_FIRST) == races.ABANDON_FIRST
    ok = f"levain agents tick: {CTX['oid']} did not launch: skipped-no-input (no_input)\n"
    assert races.assert_honest("tick_duplicate_delivery", ok, **CTX) == races.TICK_WON
    assert races.assert_honest("tick_duplicate_delivery", "", **CTX) == races.TICK_SILENT
    renew = ("Signing data on standard input\nlevain agents renew: authority log link 6: no grant 'link-1'\n")
    assert races.assert_honest("revoke_vs_renew", renew, **CTX).startswith("renew refused")


@pytest.mark.parametrize("race,observed", [
    ("revoke_vs_abandon", "both landed"),
    ("tick_duplicate_delivery", "levain agents tick: oc-abc123 did not launch: launched (x)\n"),
    ("start_for_the_one_place", "levain dream start: a second card was filed\n"),
    ("revoke_vs_renew", "levain agents renew: IntegrityError\n"),
])
def test_a_non_member_fails_naming_the_race_the_observation_and_the_set(race, observed):
    with pytest.raises(AssertionError) as exc:
        races.assert_honest(race, observed, **CTX)
    msg = str(exc.value)
    assert race in msg and repr(observed) in msg
    assert all(m.name in msg for m in races.RACES[race])


def test_an_unknown_race_is_refused_not_passed():
    with pytest.raises(AssertionError, match="has no honest-outcome set"):
        races.assert_honest("a_new_race", "whatever")


@pytest.mark.parametrize("race,member", ALL, ids=[f"{r}:{m.name}" for r, m in ALL])
def test_every_member_names_a_source_symbol_that_is_in_levain(race, member):
    assert member.where, f"{race}: {member.name!r} has no producing symbol"
    for path, token in member.where:
        assert path and token, (race, member.name, path, token)
        found = subprocess.run(["git", "-C", str(ROOT), "grep", "-qF", "-e", token, "--", f"levain/{path}"])
        assert found.returncode == 0, f"{race}: {member.name!r} cites {token!r} in levain/{path}, which is not there"


def test_every_text_member_matches_its_own_text():
    for race, member in ALL:
        if member.text is None:
            continue
        want = member.text(CTX)
        if isinstance(want, str):        # a fixed text names exactly its own member (a pattern is covered by the first test)
            assert races.assert_honest(race, want, **CTX) == member.name
