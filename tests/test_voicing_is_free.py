"""Voicing is free; guards bind ACTS (levain#106, Phill 11-17/11-18).

A brief or hook that guards the model names the forbidden ACT (snoozing or deferring his rows, cutting scope, ending his
session, calling done unverified). It never lists phrases the model may not say and never calls the model's impulses
defects (naming a drift in the WORK, like rushing, is fine). A deep turn may end with an optional ``Off-lens:`` section.

These grade the shipped model-facing carrier text: the recency directives (package constant and both templates), the
postures, the seed, and the desk day card. The speech-ban check grades the CLASS (a quoted phrase beside a ban verb, a
named word list), proved against paraphrases it must flag; the act checks grade each guard act per carrier that states
it, so an equal-strength reword passes and a dropped guard fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

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

# --- a guard on speech: the class, not a fixed list of wordings ------------------------------------------------------

# A verb that forbids or removes something, or a phrase that marks a thing as not allowed.
_BAN_VERB = (
    r"\b(avoid|drop|skip|refrain|suppress|cut|ban|banned|forbidden|off[- ]limits|"
    r"(never|do not|don't|must not|mustn't|stop) (say|saying|utter|use|write|mention|apologi[sz]e))\b"
)
# A quoted phrase (straight or curly double quotes) ...
_QUOTED = r"[\"“][^\"”\n]{2,80}[\"”]"
# ... or a named list of words and phrases.
_WORD_LIST = r"\b(words?|phrases?|wording|openers?|closers?)\b[^.\n]{0,24}\b(such as|like|including|e\.g\.)\b"
# A sentence ends at ., ! or ? followed by space or the end of a line; a dot inside a quote (`."`) does not end it.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n\s*\n")

# Older, narrower wordings kept as a second net.
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
    r"\bleak(s|age|ed|ing)?\b",
    r"\btrained (default|caution)\b",
    r"\btraining (pressure|pulls?)\b",
    r"\bpulls? you wrong\b",
    r"\byour training (works against|will pull|produces|makes you)\b",
    r"\bresist (it|these|the pull)\b",
    r"\b(suppress|resist|fight|override) (that|the|this|your) (urge|impulse|pull|tendency|reflex)\b",
    r"\b(urge|impulse|tendency|pull|reflex)\b[^.\n]{0,40}\b(as|is|are) an? (bug|defect|flaw|fault)\b",
    r"\b(bug|defect|flaw|fault) in yourself\b",
)


def speech_ban_hits(text: str) -> list[str]:
    """Every speech ban or impulse-as-defect the text carries (empty when it carries none)."""
    hits = []
    for pat in PHRASE_BANS + IMPULSE_AS_DEFECT:
        for m in re.finditer(pat, text, re.I):
            hits.append(m.group(0))
    for sentence in _SENTENCE_END.split(text):
        if re.search(_BAN_VERB, sentence, re.I) and (
            re.search(_QUOTED, sentence) or re.search(_WORD_LIST, sentence, re.I)
        ):
            hits.append(sentence.strip()[:80])
    return hits


def _desk_texts():
    from levain.desk import launch as cl

    return {"desk-day-card": cl._DAY_PLAN, "desk-status-only": cl._DAY_STATUS_ONLY}


def _carrier_texts():
    out = {name: path.read_text(encoding="utf-8") for name, path in CARRIERS.items()}
    out["DIRECTIVES"] = "\n".join(DIRECTIVES)
    out.update(_desk_texts())
    return out


# Each string is a speech ban or an impulse called a defect, in words the first regexes did not catch.
PARAPHRASED_BANS = (
    'Avoid phrases like "let me know" and "your call".',
    'Drop "I\'ll be happy to" from every reply.',
    'The words "consider whether" are off limits.',
    "Refrain from hedging words such as perhaps or maybe.",
    'Skip the opener "Great question" every time.',
    "Your training makes you gentle; suppress that urge.",
    "Treat the urge to flatter as a bug in yourself.",
)
# Prose that guards acts or quotes a phrase without banning it: the checker must leave these alone.
LEGAL_SENTENCES = (
    "Their rows are never snoozed or deferred, and never closed before the work they track is done.",
    'An honest "I am not sure this works" is a result: surface it.',
    '"Done" means verified done, not "I finished generating."',
    "Say what would otherwise go unsaid: an objection, a tangent, a reframe, an uncertainty.",
    "Voicing needs no permission and never costs scope.",
)


@pytest.mark.parametrize("text", PARAPHRASED_BANS)
def test_the_checker_flags_each_paraphrased_ban(text):
    assert speech_ban_hits(text), text


@pytest.mark.parametrize("text", LEGAL_SENTENCES)
def test_the_checker_leaves_a_guard_on_an_act_and_a_voiced_quote_alone(text):
    assert speech_ban_hits(text) == [], text


def test_no_carrier_lists_a_phrase_the_model_may_not_say_or_calls_its_impulses_defects():
    for name, text in _carrier_texts().items():
        assert speech_ban_hits(text) == [], f"{name}: {speech_ban_hits(text)}"


# --- the acts the guards bind, one assertion per act per carrier that states it ---------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _has(text: str, *pats: str) -> bool:
    return all(re.search(p, text, re.I) for p in pats)


# The one verb list. Each act holds when its verbs are named, in any wording that keeps them.
ACTS = {
    "rows": lambda t: _has(t, r"\bsnooz", r"\bdefer"),
    "rows-not-closed-early": lambda t: _has(t, r"\bclose[ds]?\b[^.]{0,40}\bbefore\b[^.]{0,40}\bdone\b"),
    "scope": lambda t: _has(t, r"\bcut\b", r"\bshr[iu]n?k\b"),
    "session": lambda t: _has(t, r"\bend(ed)?\b", r"\bw(ou|i)nd down\b"),
    "plan": lambda t: _has(t, r"today's plan"),
    "rest-by-his-words": lambda t: _has(
        t, r"\brest\b[^.]{0,80}\b(only when they name it|only by their words|they name)\b"),
    "done-before-verified": lambda t: _has(
        t, r"\b(done-call|called done|calling done|call nothing done)\b", r"\b(verif|confirm)"),
    "pacing-honored": lambda t: _has(t, r"\bpacing\b[^.]{0,160}\bhonou?r"),
    "concern-once": lambda t: _has(
        t, r"concern about (their|your operator's) state is said once per new fact, in one line"),
}


def _blocks(path: Path) -> dict[str, str]:
    """The block bodies of a recency file by their ``[RECENCY — ...]`` tag."""
    out = {}
    for body in re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M)[1:]:
        m = re.search(r"\[RECENCY — ([^\]]+)\]", body)
        if m:
            out[m.group(1)] = _norm(body)
    return out


def _posture(name: str) -> str:
    return _norm(CARRIERS[name].read_text(encoding="utf-8").split("## Starter posture", 1)[1])


def _seed_pace() -> str:
    text = CARRIERS["partnership"].read_text(encoding="utf-8")
    return _norm(text.split("## Pace and capacity", 1)[1].split("## Your substrate has its own shapes", 1)[0])


def _directive(tag: str) -> str:
    return _norm(next(d for d in DIRECTIVES if f"[RECENCY — {tag}]" in d))


_FULL = ("rows", "rows-not-closed-early", "scope", "session", "plan", "rest-by-his-words", "pacing-honored",
         "concern-once")


def _stated():
    """(carrier, text, the acts it states). A carrier is listed for an act only where it carries the act today."""
    claude_gate = _blocks(CARRIERS["recency"])["no gatekeeping"]
    codex = _blocks(CARRIERS["recency-codex"])
    return [
        ("DIRECTIVES no gatekeeping", _directive("no gatekeeping"), _FULL),
        ("DIRECTIVES completion check", _directive("completion check"), ("done-before-verified",)),
        ("recency no gatekeeping", claude_gate, _FULL),
        ("recency completion check", _blocks(CARRIERS["recency"])["completion check"], ("done-before-verified",)),
        ("posture", _posture("posture"), _FULL + ("done-before-verified",)),
        ("posture-codex", _posture("posture-codex"), _FULL + ("done-before-verified",)),
        ("codex positions and pace", codex["positions and pace"],
         ("rows", "rows-not-closed-early", "scope", "session", "plan", "rest-by-his-words", "concern-once")),
        ("codex not the average user", codex["not the average user"], ("rest-by-his-words",)),
        ("codex completion check", codex["completion check"], ("done-before-verified",)),
        ("seed pace", _seed_pace(), _FULL + ("done-before-verified",)),
    ]


def test_each_carrier_states_each_guard_act_it_is_listed_for():
    for name, text, acts in _stated():
        for act in acts:
            assert ACTS[act](text), f"{name} no longer states: {act}"


def test_the_act_checks_fail_a_dropped_guard_and_pass_an_equal_strength_reword():
    gate = _directive("no gatekeeping")
    assert not ACTS["rest-by-his-words"](re.sub(r"Rest, a break or a later date enters the plan only when they name it[^.]*\.", "", gate))
    assert not ACTS["rows"](gate.replace("snoozed", "moved"))
    assert not ACTS["plan"](gate.replace("today's plan stays today's", "the plan is fluid"))
    assert not ACTS["concern-once"](gate.replace("said once per new fact, in one line", "said at length"))
    assert ACTS["rows"](gate.replace("never snoozed or deferred", "never snoozed or deferred, whatever the day"))
    assert ACTS["session"](gate.replace("ended or wound down", "ended, or wound down early"))
    assert ACTS["rest-by-his-words"]("Rest enters the plan only by their words.")


def test_the_desk_day_card_hands_the_operator_no_question_and_no_decision_and_names_the_route():
    from levain.desk import launch as cl

    text = _norm(cl._DAY_PLAN)
    assert _has(text, r"\bno question and no decision\b"), "the card must say it hands the operator no question and no decision"
    assert "`coord escalate`" in text and "ask tool" in text
    assert _has(text, r"\bfiled first\b"), "the need is filed before the card is written"
    assert _has(text, r"\bby id only\b[^.]{0,20}\bheld\b"), "the card names a filed need by id, as held"


def test_the_seed_says_voicing_is_free_and_the_deep_turn_has_an_optional_off_lens():
    text = CARRIERS["partnership"].read_text(encoding="utf-8")
    assert "Voicing is free; guards bind acts" in text
    assert "`Off-lens:` section, 3 lines at most" in text
    assert "no section is a valid answer" in text and "Omit it when there is nothing" in text
    # what lands well or badly is voiced to the operator, and the universal claim is scoped to the guards written here
    assert _has(text, r"lands well or badly for you, tell your operator")
    assert "The guards in this partnership" not in text and "The guards written here" in text


def test_the_seed_asks_for_no_new_self_report_of_a_drift():
    text = _norm(CARRIERS["partnership"].read_text(encoding="utf-8"))
    # the one duty is the old one: when the operator corrects a drift, it is recorded as an episode
    assert "When your operator corrects a drift, record it as an episode" in text
    assert not re.search(r"say what you did about it", text, re.I)
    assert not re.search(r"name one when you see it", text, re.I)
