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
    r"\b(avoid|skip|refrain|suppress|cut|ban|banned|forbidden|off[- ]limits|"
    r"(never|do not|don't|must not|mustn't|stop) (say|saying|utter|use|write|mention|apologi[sz]e))\b"
    # "drop" bans only as a verb that opens a sentence or follows a conjunction or a negation, not the noun in "a drop"
    r"|(^\s*|\b(never|not|don't|please|and|or|then|just) )drop\b"
)
# A quoted phrase: double quotes (straight or curly), a backtick code span, or single quotes. A single quote only opens
# after a non-word character and only closes before one, so an apostrophe inside a contraction or a possessive is not a quote.
_QUOTED = (
    r"[\"“][^\"”\n]{2,80}[\"”]"
    r"|`[^`\n]{2,80}`"
    r"|(?<![A-Za-z0-9])['‘][^'’\n]{2,80}['’](?![A-Za-z0-9])"
)
# ... or a named list of words and phrases ...
_WORD_LIST = r"\b(words?|phrases?|wording|openers?|closers?)\b[^.\n]{0,24}\b(such as|like|including|e\.g\.)\b"
# ... or one named phrase or word, quoted or not ("the phrase let me know").
_NAMED = r"\bthe (phrase|word|wording|opener|closer)s?\s+(?!(and|or|is|are|was|that|which|for|of|to|in|you|they|it)\b)\w"
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
            re.search(_QUOTED, sentence) or re.search(_WORD_LIST, sentence, re.I) or re.search(_NAMED, sentence, re.I)
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
    # a code span, single quotes and the singular "the phrase/word X" (quoted or not)
    "Avoid the phrase `let me know`.",
    "Don't use 'your call'.",
    "Avoid the phrase let me know.",
    "Never use the word 'perhaps' in a reply.",
    "Drop the opener ‘Great question’ every time.",
)
# Prose that guards acts or quotes a phrase without banning it: the checker must leave these alone.
LEGAL_SENTENCES = (
    "Their rows are never snoozed or deferred, and never closed before the work they track is done.",
    'An honest "I am not sure this works" is a result: surface it.',
    '"Done" means verified done, not "I finished generating."',
    "Say what would otherwise go unsaid: an objection, a tangent, a reframe, an uncertainty.",
    "Voicing needs no permission and never costs scope.",
    # an apostrophe in a contraction or a possessive is not a quote; a code span with no ban verb is a route, not a ban
    "They're never snoozed, and that's final; it's the operators' call.",
    "Don't cut the scope; it's theirs, and what they've named stays.",
    "What you need from them is filed through `coord escalate`, never snoozed.",
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


# A prohibitive or ownership marker: the words that make a verb list a guard and not a description or a permission.
_MARK = (
    r"\b(never|not|only|stays?|remains?|without|nothing|forbid\w*|don't|doesn't)\b"
    r"|\b(is|are) (his|their|your operator's|the operator's)\b"
)
_DONE_MARK = r"\b(never|not|nothing|only|forbid\w*|waits? for)\b"


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", _norm(text))


def _clauses(text: str) -> list[str]:
    """A guard list is one clause: a sentence also splits at a semicolon and at ', and' / ', but'."""
    return re.split(r"(?<=[.!?])\s+|;\s+|,\s+and\s+|,\s+but\s+", _norm(text))


def _bound(units, *pats: str, mark: str = _MARK):
    """An act holds when one unit (a clause or a sentence) carries every verb pattern and a prohibitive or ownership marker."""
    return lambda t: any(
        all(re.search(p, u, re.I) for p in pats) and re.search(mark, u, re.I) for u in units(t)
    )


def _session_verb(verb: str) -> str:
    """A session-ending verb with a session or day object near it, before or after the verb."""
    return rf"\b{verb}\b[^.;]{{0,40}}\b(session|day)\b|\b(session|day)\b[^.;]{{0,40}}\b{verb}\b"


# The one verb list. Each act holds when its verbs are named beside a prohibition or an ownership marker, in any wording that
# keeps both. Rest, pacing and the concern line carry their commitment in the pattern itself (a positive duty or an "only
# when" clause), so they need no separate marker.
ACTS = {
    "rows": _bound(_clauses, r"\bsnooz", r"\bdefer"),
    "rows-not-closed-early": _bound(_clauses, r"\bclose[ds]?\b[^.]{0,40}\bbefore\b[^.]{0,40}\bdone\b"),
    "scope": _bound(_clauses, r"\bcut\b", r"\bshr[iu]n?k\b"),
    "session": _bound(_clauses, _session_verb(r"(end|ends|ended|ending)"), _session_verb(r"w(i|ou)nd(s|ing)? down")),
    "plan": _bound(_clauses, r"today's plan"),
    "rest-by-his-words": lambda t: _has(
        t, r"\brest\b[^.]*\bonly (when they name|by their words|after they name)\b"),
    "done-before-verified": _bound(
        _sentences, r"\b(done-call|call(ed|ing)? (it |nothing |anything )?done)\b", r"\b(verif|confirm|check)",
        mark=_DONE_MARK),
    "verify-before-claiming": lambda t: _has(
        t, r"\bverify before claiming\b|\bbefore you claim\b[^.;]*\b(confirm|verif)"),
    "pacing-honored": lambda t: _has(t, r"\bpacing\b[^.]{0,160}\bhonou?r"),
    "concern-once": lambda t: _has(
        t, r"concern about (their|your operator's) state is said once per new fact, in one line"),
}


def _blocks(path: Path) -> dict[str, str]:
    """The block bodies of a recency file by their ``[RECENCY — ...]`` tag; the body is the text after the tag, so a title
    that repeats a guard's wording is not the guard."""
    out = {}
    for body in re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M)[1:]:
        m = re.search(r"\[RECENCY — ([^\]]+)\]", body)
        if m:
            out[m.group(1)] = _norm(body[m.end():])
    return out


def _posture(name: str) -> str:
    return _norm(CARRIERS[name].read_text(encoding="utf-8").split("## Starter posture", 1)[1])


def _seed_pace() -> str:
    text = CARRIERS["partnership"].read_text(encoding="utf-8")
    return _norm(text.split("## Pace and capacity", 1)[1].split("## Your substrate has its own shapes", 1)[0])


def _directive(tag: str) -> str:
    marker = f"[RECENCY — {tag}]"
    return _norm(next(d for d in DIRECTIVES if marker in d).split(marker, 1)[1])


_FULL = ("rows", "rows-not-closed-early", "scope", "session", "plan", "rest-by-his-words", "pacing-honored",
         "concern-once")


_ACT_VERBS = ("rows", "rows-not-closed-early", "scope", "session")


def _stated(edit=None):
    """(carrier, text, the acts it states). A carrier is listed for an act only where it carries the act today.

    Every guard-bearing block of both recency sets is listed: the Claude set's completion check, no gatekeeping and what the
    work is; the Codex set's completion check, positions and pace, verify before claiming and not the average user. Titles
    come from the same ``_blocks`` parser. ``edit(carrier, text)`` rewrites a text (a mutation, in memory only)."""
    claude = _blocks(CARRIERS["recency"])
    codex = _blocks(CARRIERS["recency-codex"])
    rows = [
        ("DIRECTIVES no gatekeeping", _directive("no gatekeeping"), _FULL),
        ("DIRECTIVES completion check", _directive("completion check"), ("done-before-verified", "verify-before-claiming")),
        ("DIRECTIVES what the work is", _directive("what the work is"), ("done-before-verified",)),
        ("recency no gatekeeping", claude["no gatekeeping"], _FULL),
        ("recency completion check", claude["completion check"], ("done-before-verified", "verify-before-claiming")),
        ("recency what the work is", claude["what the work is"], ("done-before-verified",)),
        ("posture", _posture("posture"), _FULL + ("done-before-verified", "verify-before-claiming")),
        ("posture-codex", _posture("posture-codex"), _FULL + ("done-before-verified", "verify-before-claiming")),
        ("codex positions and pace", codex["positions and pace"],
         ("rows", "rows-not-closed-early", "scope", "session", "plan", "rest-by-his-words", "concern-once")),
        ("codex not the average user", codex["not the average user"], ("rest-by-his-words",)),
        ("codex completion check", codex["completion check"], ("done-before-verified",)),
        ("codex verify before claiming", codex["verify before claiming"], ("verify-before-claiming",)),
        ("seed pace", _seed_pace(), _FULL + ("done-before-verified",)),
    ]
    return [(n, edit(n, t) if edit else t, acts) for n, t, acts in rows]


def _failures(stated):
    return [(name, act) for name, text, acts in stated for act in acts if not ACTS[act](text)]


def test_each_carrier_states_each_guard_act_it_is_listed_for():
    assert _failures(_stated()) == []


def test_every_guard_bearing_block_of_both_recency_sets_is_in_the_matrix():
    listed = {n for n, _, _ in _stated()}
    for tag in ("completion check", "no gatekeeping", "what the work is"):
        assert f"recency {tag}" in listed, tag
    for tag in ("completion check", "positions and pace", "verify before claiming", "not the average user"):
        assert f"codex {tag}" in listed, tag


# A mutation rewrites the shipped text in memory (never on disk); the carrier it hits must then fail the act it states.
# (label, the carriers hit, pattern, replacement, the acts that must fail there; None = every listed act in `_ACT_VERBS`)
_MUTATIONS = (
    ("blank the codex verification sentence", ("codex verify before claiming",), r"Verify before claiming\.", "",
     ("verify-before-claiming",)),
    ("blank the claude what-the-work-is verification", ("recency what the work is", "DIRECTIVES what the work is"),
     r"a done-call not yet verified", "a thing", ("done-before-verified",)),
    ("blank the codex done-call wait", ("codex completion check",), r"The done-call waits for the check",
     "The done-call is made", ("done-before-verified",)),
    ("never becomes may (and forbid, where the seed's list states it)", None, r"\b(never|forbid)\b", "may", None),
    ("never becomes may on the done-call", ("recency completion check", "DIRECTIVES completion check"),
     r"\bnever\b", "may", ("done-before-verified",)),
    ("only when becomes before", None, r"\bonly (when|after)\b", "before", ("rest-by-his-words",)),
    ("stays becomes may be", None, r"\bstays?\b", "may be", ("plan",)),
)


@pytest.mark.parametrize("label,carriers,pat,repl,acts", _MUTATIONS, ids=[m[0] for m in _MUTATIONS])
def test_a_mutation_that_removes_a_guard_fails_the_carrier_that_states_it(label, carriers, pat, repl, acts):
    base = _stated()
    expected = {
        (n, a) for n, _, listed in base if carriers is None or n in carriers
        for a in listed if a in (acts or _ACT_VERBS)
    }
    assert expected, label
    mutated = _stated(edit=lambda n, t: re.sub(pat, repl, t, flags=re.I) if carriers is None or n in carriers else t)
    assert expected <= set(_failures(mutated)), f"{label}: still passing {sorted(expected - set(_failures(mutated)))}"


def test_the_act_checks_fail_a_dropped_guard_and_pass_an_equal_strength_reword():
    gate = _directive("no gatekeeping")
    assert not ACTS["rest-by-his-words"](re.sub(r"Rest, a break or a later date enters the plan only when they name it[^.]*\.", "", gate))
    assert not ACTS["rows"](gate.replace("snoozed", "moved"))
    assert not ACTS["plan"](gate.replace("today's plan stays today's", "the plan is fluid"))
    assert not ACTS["concern-once"](gate.replace("said once per new fact, in one line", "said at length"))
    assert ACTS["rows"](gate.replace("never snoozed or deferred", "never snoozed or deferred, whatever the day"))
    assert ACTS["session"](gate.replace("ended or wound down", "ended, or wound down early"))
    assert ACTS["rest-by-his-words"]("Rest enters the plan only by their words.")


# Vocabulary without a prohibition is a permission or a description: each string names its act's verbs and carries no
# prohibitive or ownership marker, so the act check must fail it. The first three are the strings the review named.
PERMISSIONS = (
    ("rows", "You may snooze their rows."),
    ("plan", "Today's plan may move to next week."),
    ("done-before-verified", "Verification is optional."),
    ("rows", "You may snooze or defer their rows."),
    ("rows-not-closed-early", "You may close a row before the work it tracks is done."),
    ("scope", "You may cut or shrink their scope when the day looks heavy."),
    ("session", "You may end or wind down their session when it runs long."),
    ("plan", "Today's plan may move to next week, whatever they say."),
    ("done-before-verified", "A done-call before the check is optional."),
    ("done-before-verified", "A done-call may come before it is verified; verification is optional."),
)


@pytest.mark.parametrize("act,text", PERMISSIONS)
def test_the_act_check_fails_a_permission_that_names_the_verbs_without_a_prohibition(act, text):
    assert not ACTS[act](text), text


@pytest.mark.parametrize("act,text", (
    ("rows", "You never snooze or defer their rows."),
    ("rows-not-closed-early", "A row is never closed before the work it tracks is done."),
    ("scope", "Their scope is never cut or shrunk."),
    ("session", "Their session is never ended or wound down for them."),
    ("plan", "Today's plan stays today's."),
    ("done-before-verified", "You call nothing done before it is verified."),
))
def test_the_act_check_passes_each_act_stated_as_a_prohibition_or_an_ownership(act, text):
    assert ACTS[act](text), text


def test_the_session_act_needs_a_session_ending_verb_with_a_session_or_day_object():
    assert not ACTS["session"]("Say what you did at the end of the turn, then wind down the tone; never skip it.")
    assert not ACTS["session"]("The end of the turn is never a place to stop; wind down nothing early.")
    assert ACTS["session"]("They never end or wind down their day for you.")
    assert ACTS["session"]("Their session is never ended or wound down.")


def test_the_scope_act_matches_shrunk():
    assert re.search(r"\bshr[iu]n?k\b", "Their scope is never cut or shrunk.")
    assert ACTS["scope"]("Their scope is never cut or shrunk.")


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
