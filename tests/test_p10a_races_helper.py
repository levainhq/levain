"""The p10a race-outcome helper (levain#150): a member passes, a non-member fails showing the set, a name and a printed
text are different observations, and every member of every set names a producing symbol that really is in levain/ (read
from the files, never counted by hand). A token's presence is not proof the path is reachable: that is known."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import _p10a_races as races

ROOT = Path(__file__).resolve().parent.parent
CTX = {"oid": "oc-abc123", "link": "link-1"}
ALL = [(race, m) for race, members in races.RACES.items() for m in members]

# one representative line per pattern-bodied member, built from the product's own refusal text (levain/agents/signing.py,
# levain/agent_authority.py, levain/authority.py); keyed by member name
_PREFIX = "Signing data on standard input\nlevain agents renew: "
_MOVED = " (the authority log moved: nothing was appended)"
SAMPLES = {
    "renew refused: a revoke is open": _PREFIX + "a revoke is open for this agent: finish or abandon it\n",
    "renew refused: the link already ended": "levain agents renew: no agent link 'link-1'\n",
    "renew refused: the log moved while prepared":
        _PREFIX + "the authority log moved while this was prepared: look at it again" + _MOVED + "\n",
    "renew refused: the head moved since prepared":
        "levain agents renew: the authority log's head moved to seq 7 since this link was prepared" + _MOVED + "\n",
    "renew refused: the log has no grant": _PREFIX + "authority log link 6: no grant 'link-1'\n",
    "renew refused: earlier than the revoke's time":
        "levain agents renew: authority log link 7: at 2026-11-19T10:00:00Z is earlier than its predecessor's "
        "2026-11-19T10:00:01Z; the log's times never go back\n",
}


def test_a_member_passes_by_name_and_by_text():
    assert races.assert_honest("revoke_vs_abandon", name=races.ABANDON_FIRST) == races.ABANDON_FIRST
    ok = f"levain agents tick: {CTX['oid']} did not launch: skipped-no-input (no_input)\n"
    assert races.assert_honest("tick_duplicate_delivery", text=ok, **CTX) == races.TICK_WON
    assert races.assert_honest("tick_duplicate_delivery", text="", **CTX) == races.TICK_SILENT
    assert races.assert_honest("revoke_vs_renew", name=races.RENEW_THEN_REVOKE, **CTX) == races.RENEW_THEN_REVOKE
    assert races.assert_honest("revoke_vs_renew", text=SAMPLES["renew refused: the log has no grant"],
                               **CTX).startswith("renew refused")


@pytest.mark.parametrize("race,kw", [
    ("revoke_vs_abandon", {"name": "both landed"}),
    ("tick_duplicate_delivery", {"text": "levain agents tick: oc-abc123 did not launch: launched (x)\n"}),
    ("start_for_the_one_place", {"text": "levain dream start: a second card was filed\n"}),
    ("revoke_vs_renew", {"text": "levain agents renew: IntegrityError\n"}),
])
def test_a_non_member_fails_naming_the_race_the_observation_and_the_set(race, kw):
    with pytest.raises(AssertionError) as exc:
        races.assert_honest(race, **kw, **CTX)
    msg = str(exc.value)
    assert race in msg and repr(next(iter(kw.values()))) in msg
    assert all(m.name in msg for m in races.RACES[race])


def test_an_unknown_race_is_refused_not_passed():
    with pytest.raises(AssertionError, match="has no honest-outcome set"):
        races.assert_honest("a_new_race", text="whatever")


def test_a_name_and_a_printed_text_are_different_observations():
    # a process that printed a member's own name did not do what the member says
    for race, member in ALL:
        if member.text is not None:
            with pytest.raises(AssertionError):
                races.assert_honest(race, text=member.name, **CTX)
            with pytest.raises(AssertionError):
                races.assert_honest(race, name=member.name, **CTX)      # a text-bodied member refuses a name
        else:
            with pytest.raises(AssertionError):
                races.assert_honest(race, text=member.name, **CTX)      # a name-only member refuses text
    with pytest.raises(AssertionError, match="exactly one of"):
        races.assert_honest("revoke_vs_abandon")
    with pytest.raises(AssertionError, match="exactly one of"):
        races.assert_honest("revoke_vs_abandon", name=races.REVOKE_FIRST, text="x")


@pytest.mark.parametrize("race,kw", [
    ("revoke_vs_renew", {"text": "bad"}),
    ("start_for_the_one_place", {"text": "bad"}),
    ("tick_duplicate_delivery", {"text": "bad", "link": "x"}),
])
def test_a_missing_ctx_key_is_an_assertion_naming_the_race_the_key_and_the_set(race, kw):
    with pytest.raises(AssertionError) as exc:
        races.assert_honest(race, **kw)
    msg = str(exc.value)
    assert race in msg and "missing ctx key" in msg
    assert str(list(races.NEEDS[race])) in msg and all(k in msg for k in kw if k != "text")


@pytest.mark.parametrize("race,member", ALL, ids=[f"{r}:{m.name}" for r, m in ALL])
def test_every_member_names_a_source_symbol_that_is_in_levain(race, member):
    assert member.where, f"{race}: {member.name!r} has no producing symbol"
    for path, token in member.where:
        assert path and token, (race, member.name, path, token)
        source = ROOT / "levain" / path
        assert source.is_file(), f"{race}: {member.name!r} cites levain/{path}, which is not a file"
        assert token in source.read_text(encoding="utf-8"), \
            f"{race}: {member.name!r} cites {token!r} in levain/{path}, which is not there"


def test_every_text_member_matches_its_own_text():
    for race, member in ALL:
        if member.text is None:
            continue
        want = member.text(CTX)
        if isinstance(want, str):        # a fixed text names exactly its own member
            assert races.assert_honest(race, text=want, **CTX) == member.name


def test_every_pattern_member_matches_its_own_sample_and_no_other_member():
    patterned = [m for m in races._RENEW_REFUSED if isinstance(m.text(CTX), re.Pattern)]
    assert patterned and {m.name for m in patterned} == set(SAMPLES), "every pattern member needs one sample"
    for member in patterned:
        sample = SAMPLES[member.name]
        assert [m.name for m in races.RACES["revoke_vs_renew"] if m.matches_text(sample, CTX)] == [member.name], member.name
        assert races.assert_honest("revoke_vs_renew", text=sample, **CTX) == member.name


def test_a_pattern_member_binds_its_own_body_not_a_shared_one():
    # the six refusals are built from one factory, not a loop: each closes over its own body, so two different links
    # give two different patterns and the member that names the link follows it
    one = next(m for m in races._RENEW_REFUSED if m.name.endswith("the link already ended"))
    assert one.matches_text("levain agents renew: no agent link 'link-1'\n", {"link": "link-1"})
    assert not one.matches_text("levain agents renew: no agent link 'link-1'\n", {"link": "link-2"})
