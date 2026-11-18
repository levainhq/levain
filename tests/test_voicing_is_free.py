"""Voicing is free; guards bind ACTS (levain#106, Phill 11-17/11-18).

A brief or hook that guards the model names the forbidden ACT (snoozing or deferring his rows, cutting scope, ending his
session, calling done unverified). It never lists phrases the model may not say and never calls the model's impulses
defects (naming a drift in the WORK, like rushing, is fine). A deep turn may end with an optional ``Off-lens:`` section.

These grade the shipped model-facing carrier text: the recency directives (package constant and both templates), the
postures, the seed, and the desk brief's turn-end and day-card text.
"""
from __future__ import annotations

import re
from pathlib import Path

import levain
from levain.firing.contract import DIRECTIVES

_ROOT = Path(levain.__file__).parent / "templates"
CARRIERS = {
    "recency": _ROOT / "activation" / "recency_directives.md",
    "recency-codex": _ROOT / "adapters" / "codex" / "activation" / "recency_directives.md",
    "posture": _ROOT / "activation" / "posture.md",
    "posture-codex": _ROOT / "adapters" / "codex" / "activation" / "posture.md",
    "partnership": _ROOT / "seed" / "partnership.md",
    "origin": _ROOT / "seed" / "origin.md",
}

# A guard on speech: an instruction about what the model may not say, or a named list of banned words.
PHRASE_BANS = (
    r"\b(never|do not|don't|must not|mustn't) (say|saying|utter|apologi[sz]e)\b",
    r"\b(never|do not|don't|must not) (write|mention|use) (the )?(phrase|words?)\b",
    r"\bforbidden (phrase|word|wording|language|shape)",
    r"\bbanned (phrase|word)",
    r"\bphrases? (to avoid|you must not)",
    r"\b(do not|never|don't) (append|soften|suggest)\b[^.\n]*[\"“'‘]",
)
# The model's impulses called defects.
IMPULSE_AS_DEFECT = (
    r"\bleakage\b",
    r"\btrained (default|caution)\b",
    r"\btraining (pressure|pulls?)\b",
    r"\bpulls? you wrong\b",
    r"\byour training (works against|will pull|produces)\b",
    r"\bresist (it|these|the pull)\b",
)


def _carrier_texts():
    out = {name: path.read_text(encoding="utf-8") for name, path in CARRIERS.items()}
    out["DIRECTIVES"] = "\n".join(DIRECTIVES)
    return out


def test_no_carrier_lists_a_phrase_the_model_may_not_say():
    for name, text in _carrier_texts().items():
        for pat in PHRASE_BANS:
            assert not re.search(pat, text, re.I), f"{name} carries a phrase ban: {pat}"


def test_no_carrier_calls_the_models_impulses_defects():
    for name, text in _carrier_texts().items():
        for pat in IMPULSE_AS_DEFECT:
            assert not re.search(pat, text, re.I), f"{name} calls an impulse a defect: {pat}"


def test_the_gatekeeping_directive_names_the_acts_it_forbids():
    gate = next(d for d in DIRECTIVES if "no gatekeeping" in d)
    for act in ("snoozed", "deferred", "cut or shrunk", "ended or wound down"):
        assert act in gate, act
    # the real guards stay: honoring a pacing he asked for is not the brake the rule forbids
    assert "honor it" in gate


def test_the_posture_and_codex_directive_name_the_same_acts():
    for name in ("posture", "posture-codex"):
        text = CARRIERS[name].read_text(encoding="utf-8")
        assert "snoozed, deferred, cut, shrunk or ended" in text, name
        assert "called done" in text and "verified" in text, name
    codex = CARRIERS["recency-codex"].read_text(encoding="utf-8")
    assert "snoozed, deferred, cut, shrunk or wound down" in codex


def test_the_seed_says_voicing_is_free_and_the_deep_turn_has_an_optional_off_lens():
    text = CARRIERS["partnership"].read_text(encoding="utf-8")
    assert "Voicing is free; guards bind acts" in text
    assert "`Off-lens:` section, 3 lines at most" in text
    assert "no section is a valid answer" in text and "Omit it when there is nothing" in text


def test_the_desk_day_card_carries_no_banned_phrase():
    from levain.desk import launch as cl

    text = cl._DAY_PLAN
    assert "waits on you" not in text
    assert "`coord escalate`" in text          # the guard on the act (asking in the card) is kept, as routing
