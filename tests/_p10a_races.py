"""The honest outcome set of each two-process race of the p10a e2e family, in ONE table (levain#150).

A race test does not assert "the loser said X". It observes what its loser did and calls :func:`assert_honest`, which
fails naming the race, the observed outcome and the whole set. So one red is not fixed by patching one test, and a new
race test has to add a RACES entry (with the producing symbols) rather than assert one ordering.

THE RULE FOR THE SETS: every member is DERIVED from what the product code can return for that race and names, in
``where``, the ``(file under levain/, token)`` pairs that produce it (``tests/test_p10a_races_helper.py`` greps every
one). A set is never widened to make a red pass: a member needs a code path. A real defect (two cards, a double debit,
a lost claim, a second revoke in the log) is in no set; those stay the tests' own non-outcome assertions.

An observation is the member's ``name`` (for a race whose outcome the test classifies from the end state) or the text a
process printed (matched in full against the member's ``text``, which may use the race's ``ctx``, e.g. the occurrence id).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

Where = tuple[str, str]          # (file under levain/, a token that file holds)
Text = Callable[[dict], "str | re.Pattern[str]"]


@dataclass(frozen=True)
class Outcome:
    name: str
    where: tuple[Where, ...]
    text: Text | None = None

    def matches(self, observed: str, ctx: dict) -> bool:
        if observed == self.name:
            return True
        if self.text is None:
            return False
        want = self.text(ctx)
        return bool(want.fullmatch(observed)) if isinstance(want, re.Pattern) else observed == want


# ------------------------------------------------------------------------------- revoke racing an abandon (c6/c8)
# classified by the test from the end state (agent_bars row, the log's ops, both exit codes and outputs)
REVOKE_FIRST = "revoke first, abandon found nothing open"
ABANDON_LOST_CLOSE = "abandon lost the close"
ABANDON_FIRST = "abandon first"
ABANDON_BETWEEN = "abandon closed between the append and the close"

_REVOKE_VS_ABANDON = (
    Outcome(REVOKE_FIRST, (("agents/cli.py", "_no_open_bar"), ("agents/cli.py", "nothing to abandon"))),
    Outcome(ABANDON_LOST_CLOSE, (("agents/cli.py", "_abandon_landed"), ("agents/cli.py", "had already landed at seq"))),
    Outcome(ABANDON_FIRST, (("agents/cli.py", "_run_abandon"), ("agent_authority.py", "_ABANDONED"))),
    Outcome(ABANDON_BETWEEN, (("agent_authority.py", "landed_bar_abandoned"), ("agent_authority.py", "def _landed"))),
)

# --------------------------------------------------------------------------------- revoke racing a renewal (c7)
RENEW_THEN_REVOKE = "renew then revoke at 7"
_MOVED = r" \(the authority log moved: nothing was appended\)"


def _renew_refusal(body: Text) -> Text:
    # ssh-keygen's own line is there when the refusal came after the signature
    return lambda ctx: re.compile("(?:Signing data on standard input\n)?levain agents renew: " + body(ctx) + "\n")


_RENEW_REFUSED = (
    Outcome("renew refused: a revoke is open", (("agents/signing.py", "_refuse_open_bar"),
                                                ("agent_authority.py", "OPEN_BAR_REFUSAL")),
            _renew_refusal(lambda c: re.escape("a revoke is open for this agent: finish or abandon it"))),
    Outcome("renew refused: the link already ended", (("agents/signing.py", "def renew_agent_link"),
                                                      ("agents/signing.py", "no agent link")),
            _renew_refusal(lambda c: "no agent link " + re.escape(repr(c["link"])))),
    Outcome("renew refused: the log moved while prepared", (("agents/signing.py", "def _sign_op"),
                                                            ("agents/signing.py", "the authority log moved while this")),
            _renew_refusal(lambda c: "the authority log moved while this was prepared: look at it again" + _MOVED)),
    Outcome("renew refused: the head moved since prepared", (("authority.py", "since this link was prepared"),),
            _renew_refusal(lambda c: r"the authority log's head moved to seq \d+ since this link was prepared" + _MOVED)),
    Outcome("renew refused: the log has no grant", (("authority.py", "def _step"), ("authority.py", "no grant {gid!r}")),
            _renew_refusal(lambda c: r"authority log link \d+: no grant " + re.escape(repr(c["link"])))),
    Outcome("renew refused: earlier than the revoke's time", (("authority.py", "def _append_only_checks"),
                                                              ("authority.py", "the log's times never go back")),
            _renew_refusal(lambda c: r"authority log link \d+: at \S+ is earlier than its predecessor's \S+; "
                                     "the log's times never go back")),
)
_REVOKE_VS_RENEW = (Outcome(RENEW_THEN_REVOKE, (("agents/signing.py", "def renew_agent_link"),
                                                ("agent_authority.py", "def _landed"))),) + _RENEW_REFUSED

# -------------------------------------------------------- two `dream start` for the one place (run_ceiling 1), loser
OVERLAP = "claim answered skipped-overlap"
_NOT_ADMITTED = ("dream/start.py", "was no longer admitted when this start reached it")


def _gone(why: str) -> Text:
    return lambda c: (f"levain dream start: {c['oid']} was no longer admitted when this start reached it ({why}): "
                      "another start launched or ended it, so this one started nothing. "
                      "`levain agents show dream` says which\n")


_START_FOR_THE_PLACE = (
    Outcome(OVERLAP, (("dream/start.py", "the claim answered {outcome}"), ("agents/controller.py", "def claim")),
            lambda c: "levain dream start: the claim answered skipped-overlap: another occurrence of the dream is "
                      "live or this second was already claimed; nothing was started\n"),
    Outcome("refused: the winner's row is launched", (("dream/start.py", "is still in flight, and the"),
                                                      ("dream/start.py", "def _refused")),
            lambda c: (f"levain dream start: {c['oid']} (manual, launched) is still in flight, and the dream runs one "
                       "occurrence at a time; nothing was claimed\n")),
    Outcome("refused: the winner's row is still admitted", (("dream/start.py", "is still in flight, and the"),),
            lambda c: (f"levain dream start: {c['oid']} (manual, admitted) is still in flight, and the dream runs one "
                       "occurrence at a time; nothing was claimed\n")),
    Outcome("resumed the winner's row, no longer admitted: launched",
            (_NOT_ADMITTED, ("agents/controller.py", "the occurrence is {occ['state']}")),
            _gone("the occurrence is launched")),
    Outcome("resumed the winner's row, no longer admitted: ended during the input check",
            (_NOT_ADMITTED, ("agents/controller.py", "the occurrence ended while its input was checked")),
            _gone("the occurrence ended while its input was checked")),
)

# ------------------------------------------- three `agents tick` processes for one slot whose input is absent (racers)
TICK_WON = "claimed it and ended it skipped-no-input"
TICK_SILENT = "saw the slot claimed and ended: says nothing"
_DID_NOT = ("agents/tick_driver.py", "did not launch: {res.outcome}")


def _tick(outcome: str, why: str) -> Text:
    return lambda c: f"levain agents tick: {c['oid']} did not launch: {outcome} ({why})\n"


_TICK_RACERS = (
    Outcome(TICK_SILENT, (("agents/tick_driver.py", "def _launch_dream_slots"),), lambda c: ""),
    Outcome(TICK_WON, (_DID_NOT, ("agents/controller.py", "def _end_after_input")), _tick("skipped-no-input", "no_input")),
    Outcome("not-admitted: ended while its input was checked",
            (_DID_NOT, ("agents/controller.py", "the occurrence ended while its input was checked")),
            _tick("not-admitted", "the occurrence ended while its input was checked")),
    Outcome("not-admitted: the row is already skipped-no-input",
            (_DID_NOT, ("agents/controller.py", "the occurrence is {occ['state']}")),
            _tick("not-admitted", "the occurrence is skipped-no-input")),
)

RACES: dict[str, tuple[Outcome, ...]] = {
    "revoke_vs_abandon": _REVOKE_VS_ABANDON,
    "revoke_vs_renew": _REVOKE_VS_RENEW,
    "start_for_the_one_place": _START_FOR_THE_PLACE,
    "tick_duplicate_delivery": _TICK_RACERS,
}


def assert_honest(race: str, observed: str, **ctx) -> str:
    """Pass when ``observed`` (a member's name, or a printed text) is in ``race``'s set; return the member's name so a
    test can check the end state that member implies. Fail naming the race, the observation and the full set."""
    if race not in RACES:
        raise AssertionError(f"p10a race {race!r} has no honest-outcome set: add it to tests/_p10a_races.py RACES "
                             f"(known: {sorted(RACES)})")
    for member in RACES[race]:
        if member.matches(observed, ctx):
            return member.name
    shown = [m.name if m.text is None else f"{m.name}: {m.text(ctx)!r}" for m in RACES[race]]
    raise AssertionError(f"p10a race {race!r}: observed {observed!r}, which is not one of its {len(shown)} honest "
                         f"outcomes:\n  " + "\n  ".join(shown))
