"""levain.answers — the non-interactive answer file (load · validate · template).

Backs ``levain init --answers FILE.json``: the only way to create an entity with
no human at a terminal, and therefore the gate every fleet-provisioned seat and
every unattended seat passes through.

THE FORMAT IS SLOT-KEYED, AND THAT IS THE WHOLE DESIGN DECISION.
An answer file is a JSON object mapping SLOT NAME -> answer::

    {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada", ...}

NOT an ordered list of answers. The ordered form is what a scripted ``levain init``
actually reached for first, and it silently produced a CORRUPTED seed: the terminal
interview walks ``world.md`` BEFORE ``origin.md``, so an origin-first answer list
maps every answer onto the WRONG slot — and nothing downstream notices. The render
succeeds, no ``{{PLACEHOLDER}}`` survives, and ``doctor`` reports green over an
entity that does not know who its operator is. Keying by slot does not DOCUMENT
that hazard away, it DELETES it: there are no positions left to get wrong, so the
field order stops being load-bearing information the operator has to discover.
(The same shape is already what ``.levain/answers.json`` persists, so the file you
hand ``--answers`` and the file the install records are one format, not two.)

Validation is STRICT and runs BEFORE any write, because every loose reading of this
file degrades back into that same silent-corruption class:

  - an UNKNOWN slot key is an ERROR, never ignored — an ignored typo is an
    unfilled slot wearing an answer's clothes, which is exactly the failure the
    keyed format exists to make impossible.
  - a MISSING slot is an ERROR, never blank-filled — ``render_template``
    substitutes a missing slot with ``""`` without complaint, so silence here
    ships a hollow seed.
  - an EMPTY value for an IDENTITY slot (``OPERATOR_NAME``, ``ENTITY_NAME``) is an
    ERROR. Blank is allowed everywhere else — including other non-optional fields —
    because the terminal interview and the web form both permit a blank answer, and
    a surface that silently held its siblings to a stricter rule would condemn
    installs those siblings were happy to create. Identity is the exception on its
    own merits: an entity that does not know who it is partnering with is the exact
    failure this product claims cannot happen, not merely a thinner seed.
  - a value that COULD NOT have come from its own prompt (a multi-line answer in a
    one-line slot) is an ERROR, because this surface provisions entities nobody is
    watching: a fleet reads exit 0 as success, so the corruption signal has to stop
    the install rather than merely annotate it.

WHAT THIS MODULE CANNOT DO, said plainly so no caller assumes otherwise: nothing
here can tell whether an answer is TRUE, or even whether it answers the question
that slot asked. A confident, well-shaped, entirely wrong biography validates
clean. What is mechanically checkable is presence, emptiness, and SHAPE — and
shape is checkable only in the one asymmetric direction that carries signal (see
``shape_violations``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Protocol

# A `line`/`optional-line` field is prompted with a single `input()` and stored
# `.strip()`ed, so a stored newline cannot come from that prompt — it can only come
# from a hand-written answer file carrying a `prose`/`bullet` answer in a one-line
# slot. That is the DESYNC SIGNATURE (an answer landing one slot off from where it
# belongs), and it is the one shape check with real signal.
#
# The length bound is a much softer signal than the newline one — a genuinely long
# single-line answer is legal, just unusual — so it is set well past any plausible
# name/role/handle rather than at a tidy round number, and it is reported as a
# shape finding for a human to judge, never used to REJECT an answer file.
_MAX_LINE_CHARS = 400

# Styles whose value is legitimately multi-line (see `_prompt_for_slot`): `bullet`
# stores its own "- " prefixes joined by newlines, `prose` stores newline-joined
# lines. Only the single-line styles are shape-checkable.
_SINGLE_LINE_STYLES = ("line", "optional-line")


class AnswersError(Exception):
    """A malformed or unusable answer file. Carries an operator-facing message —
    callers print it as a `FAIL:` line, they do not re-word it."""


class _Field(Protocol):
    """The subset of `levain.interview.InterviewField` this module reads.

    Deliberately structural (a Protocol, not an import): this module stays free of
    levain imports so it is pure, trivially testable, and safe for `doctor` to pull
    in under its no-top-level-levain-imports discipline.
    """

    slot: str
    style: str
    optional: bool
    guidance: str
    section_guidance: str
    section_title: str
    spec_name: str
    first_in_section: bool


def is_required(field: _Field) -> bool:
    """A field the seed EXPECTS filled — not a field init refuses blank.

    Two independent ways to be optional, and both are honored: the SECTION carries
    `<!-- optional -->` (answering every slot in it empty drops the whole section at
    render), or the FIELD resolves to the `optional-line` style (the template's own
    "blank to skip" affordance). Everything else is expected filled, and `doctor`
    reports a blank one as a thin seed. Init still ACCEPTS a blank there; only the
    identity slots (`IDENTITY_SLOTS`) are refused blank (see `validate_answers`).
    """
    return not field.optional and field.style != "optional-line"


def load_answers_file(path: Path) -> dict[str, str]:
    """Read + type-validate an answer file. Raises `AnswersError` with an
    operator-facing message; never returns a partially-valid dict.

    Type strictness is not pedantry: a JSON `null`/number/list reaching
    `render_template` would be substituted as a Python repr into the operator's
    seed (`None`, `42`, `['a']`), which renders green and reads as nonsense.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise AnswersError(f"cannot read --answers file {path}: {e}") from e
    try:
        data: Any = json.loads(raw)
    except ValueError as e:
        raise AnswersError(f"--answers file {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AnswersError(
            f"--answers file {path} must be a JSON OBJECT keyed by slot name "
            f'(e.g. {{"OPERATOR_NAME": "Chris"}}), not a '
            f"{type(data).__name__}. An ordered list cannot be used: answers are "
            f"matched by slot name, never by position."
        )
    bad = sorted(
        str(k) for k, v in data.items() if not isinstance(k, str) or not isinstance(v, str)
    )
    if bad:
        raise AnswersError(
            f"--answers file {path}: every value must be a JSON string; "
            f"non-string value(s) for: {', '.join(bad)}"
        )
    return dict(data)


def validate_answers(fields: Iterable[_Field], answers: dict[str, str]) -> list[str]:
    """Every reason this answer file cannot drive this install's interview.

    Returns ALL findings (never short-circuits on the first) so one edit-run round
    trip fixes the whole file — a provisioning surface that reveals its complaints
    one at a time is a provisioning surface nobody scripts against.

    Empty list == this file fills every slot the interview would ask, and no more.
    """
    plan = list(fields)
    known = {f.slot for f in plan}
    errors: list[str] = []

    unknown = sorted(set(answers) - known)
    if unknown:
        errors.append(
            f"unknown slot(s) not asked by this install's interview: "
            f"{', '.join(unknown)} "
            f"(a typo here would otherwise leave the real slot unfilled)"
        )

    missing = [f.slot for f in plan if f.slot not in answers]
    if missing:
        planned_identity = [s for s in IDENTITY_SLOTS if s in known]
        exception = (
            f" except {' and '.join(planned_identity)}" if planned_identity else ""
        )
        errors.append(
            f"missing slot(s): {', '.join(missing)} "
            f"(every slot must be present; \"\" is accepted for any slot{exception})"
        )

    empty_identity = [
        f.slot
        for f in plan
        if f.slot in IDENTITY_SLOTS and f.slot in answers and not answers[f.slot].strip()
    ]
    if empty_identity:
        errors.append(
            f"empty value for identity slot(s): {', '.join(empty_identity)} "
            f"(an entity must know its own name and its operator's; every other "
            f'field may be "")'
        )

    # A can't-have-happened SHAPE finding is an ERROR here, not a warning, and the
    # asymmetry with `SHAPE_UNUSUAL` is the point. This surface exists to provision
    # entities with no human watching: a fleet reads exit 0 as "provisioned", so a
    # corruption signal that only WARNS at the gate and FAILS later at `doctor`
    # hands the fleet a green light and the operator a broken seat. The gate has to
    # be the gate. `SHAPE_UNUSUAL` stays a warning precisely because it is a
    # judgment call about someone's own answer, and this one is not.
    impossible = [m for sev, m in shape_violations(plan, answers) if sev == SHAPE_IMPOSSIBLE]
    errors.extend(impossible)

    return errors


#: `shape_violations` severities. IMPOSSIBLE = the value could not have come from
#: the prompt that owns this slot, so something authored it elsewhere — a real
#: corruption signal a checker may act on. UNUSUAL = legal but atypical; it may be
#: SHOWN to a human and must never, on its own, condemn an install or reject a file.
SHAPE_IMPOSSIBLE = "impossible"
SHAPE_UNUSUAL = "unusual"

#: The slots whose emptiness is a DIFFERENT KIND of problem from a thin seed.
#: Every other blank makes an entity that knows less; a blank here makes an entity
#: that does not know who it is or who it is partnering with, which is the failure
#: the product's own pitch says cannot happen. `doctor` singles out the same two
#: slots for the same reason (see its `_RENDER_TARGET_SEEDS` note), so the two
#: surfaces enforce one rule rather than two that drift.
IDENTITY_SLOTS = ("OPERATOR_NAME", "ENTITY_NAME")


def shape_violations(
    fields: Iterable[_Field], answers: dict[str, str]
) -> list[tuple[str, str]]:
    """Structural implausibility findings as `(severity, message)` — the desync detector.

    ASYMMETRIC BY DESIGN, and the asymmetry IS the signal. A short answer in a
    `prose` slot is perfectly legal (a terse person), so it carries no information
    and is not reported. A MULTI-LINE answer in a `line` slot is different in kind:
    that slot is prompted with a single `input()` and stored `.strip()`ed, so the
    prompt PHYSICALLY CANNOT produce a newline — its presence proves the value was
    authored somewhere else and landed in a slot that never asked for it.

    The severity split exists so a heuristic can never overreach. Only the
    can't-have-happened finding is strong enough for a caller to fail on;
    "unusually long" is a nudge to a human's judgment, because a genuinely long
    one-line answer is somebody's real answer and no checker gets to overrule it.

    Note what is NOT claimed: this finds answers in the WRONG SLOT, never answers
    that are wrong. A well-shaped lie is invisible here and always will be.
    """
    out: list[tuple[str, str]] = []
    for f in fields:
        value = answers.get(f.slot)
        if not value or f.style not in _SINGLE_LINE_STYLES:
            continue
        if "\n" in value:
            first = value.splitlines()[0][:60]
            out.append((
                SHAPE_IMPOSSIBLE,
                f"{f.slot} is a single-line field but holds "
                f"{len(value.splitlines())} lines (starts: {first!r}) — a multi-line "
                f"answer in a one-line slot means answers landed in the wrong slots",
            ))
        elif len(value) > _MAX_LINE_CHARS:
            out.append((
                SHAPE_UNUSUAL,
                f"{f.slot} is a single-line field holding {len(value)} characters "
                f"— unusually long; check it is the intended answer",
            ))
    return out


def answers_template_json(fields: Iterable[_Field]) -> str:
    """A blank, ready-to-edit answer file: every slot the interview asks, mapped to "".

    Emitted in INTERVIEW ORDER — not because order means anything (it does not; the
    file is keyed) but so the file reads top-to-bottom like the questions a terminal
    install would ask, which is what makes it fillable by hand.
    """
    body = {f.slot: "" for f in fields}
    return json.dumps(body, indent=2, sort_keys=False) + "\n"


def field_guide(fields: Iterable[_Field]) -> str:
    """The human-readable companion to `answers_template_json` — what each slot is
    asking, grouped the way the terminal interview groups it.

    Written to STDERR by the CLI while the JSON skeleton goes to STDOUT, so
    `levain init --answers-template > answers.json` yields a valid file AND the
    operator still sees the questions.
    """
    plan = list(fields)
    lines: list[str] = []
    specs = sorted({f.spec_name for f in plan})
    lines.append(
        f"Levain interview — {len(plan)} field(s) from {', '.join(specs)}."
    )
    lines.append(
        "Answers are matched BY SLOT NAME; the order below is the order the "
        "terminal interview asks, not a requirement."
    )
    present_identity = [s for s in IDENTITY_SLOTS if s in {f.slot for f in plan}]
    if present_identity:
        lines.append(
            f"Every slot may be \"\" except {' and '.join(present_identity)} "
            "(marked [required])."
        )
    lines.append("")

    # An identity slot inside an optional section must not be told to "skip":
    # the validator refuses it blank, so the section hint would contradict it.
    identity_sections: set[int] = set()
    section_no = -1
    for f in plan:
        if f.first_in_section:
            section_no += 1
        if f.slot in IDENTITY_SLOTS:
            identity_sections.add(section_no)

    current_spec: str | None = None
    section_no = -1
    for f in plan:
        if f.spec_name != current_spec:
            current_spec = f.spec_name
            lines.append(f"=== {current_spec} ===")
        if f.first_in_section:
            section_no += 1
            title = f.section_title or "[preamble]"
            opt = (
                "  (optional section — leave its slots \"\" to skip)"
                if f.optional and section_no not in identity_sections
                else ""
            )
            lines.append("")
            lines.append(f"  ## {title}{opt}")
            if f.section_guidance:
                lines.append(f"     {f.section_guidance}")
        req = "  [required]" if f.slot in IDENTITY_SLOTS else ""
        lines.append(f"    {f.slot}  ({f.style}){req}")
        if f.guidance:
            lines.append(f"        {f.guidance}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# adopt-answers (spore-423): a hand-edited seed becomes the record.
# ---------------------------------------------------------------------------

class AdoptRefused(AnswersError):
    """The seed cannot be turned into a complete, exact record. Nothing is written."""


def _sentinel(slot: str) -> str:
    # Private-use code points: no template or answer carries them, and render's
    # whitespace normalisation leaves them alone.
    return f"{slot}"


def _normalise(text: str) -> str:
    """The tail of ``render_template``'s own normalisation, so a seed that differs from
    a render only by trailing whitespace on a line, or a missing final newline, still
    counts as that render."""
    import re

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip() + "\n"


def _split(target: str, skeleton: str, *, from_right: bool) -> dict[str, str] | None:
    """Read each slot's value out of ``target`` by anchoring on the literal text between
    the sentinels in ``skeleton``: each literal is found with ``find`` (left to right,
    first occurrence) or ``rfind`` (right to left, last occurrence). Whitespace touching a
    slot is not anchored on, since render's clean-up moves it when a value is blank or
    multi-line. A slot seen twice must read the same both times. Linear in the text, per
    skeleton; ``None`` when the literals are not there in order."""
    parts = skeleton.split("\ue000")
    lits = [parts[0]]
    names: list[str] = []
    for piece in parts[1:]:
        name, _, lit = piece.partition("\ue001")
        names.append(name)
        lits.append(lit)
    if not names:
        return {} if target == skeleton else None
    first, last = lits[0].rstrip(), lits[-1].lstrip()
    if not (target.startswith(first) and target.endswith(last)
            and len(first) + len(last) <= len(target)):
        return None
    lo, hi = len(first), len(target) - len(last)
    inner = [lit.strip() for lit in lits[1:-1]]
    for i, core in enumerate(inner):
        if not core:
            # Nothing but whitespace between two fields: "x y z" splits as x|y z or
            # x y|z, and both re-render the same. No anchor, so no honest answer.
            raise AdoptRefused(f"the fields {names[i]} and {names[i + 1]} have nothing but "
                               f"whitespace between them in this template, so where one "
                               f"ends and the next begins cannot be read back.")
    values: list[str] = []
    if not from_right:
        pos = lo
        for core in inner:
            idx = target.find(core, pos, hi)
            if idx < 0:
                return None
            values.append(target[pos:idx])
            pos = idx + len(core)
        values.append(target[pos:hi])
    else:
        end = hi
        for core in reversed(inner):
            idx = target.rfind(core, lo, end)
            if idx < 0:
                return None
            values.append(target[idx + len(core):end])
            end = idx
        values.append(target[lo:end])
        values.reverse()
    out: dict[str, str] = {}
    for name, value in zip(names, values):
        value = value.strip()
        if out.setdefault(name, value) != value:
            return None
    return out


def extract_answers(
    text: str,
    *,
    slots: Iterable[str],
    optional_sections: Iterable[Iterable[str]],
    render: Any,
    prior: dict[str, str] | None = None,
    max_combinations: int = 4096,
) -> dict[str, str]:
    """The ONE answer set that ``render`` turns into ``text``, or raise
    :class:`AdoptRefused`. ALL-OR-REFUSE (spore-423): a partial record is worse than
    none, because doctor then goes green over it and the next re-render blank-fills
    the gaps.

    ``render(answers) -> str`` is the template's own renderer, which is lossy: it
    drops an optional section whose slots are all blank and collapses a blank inline
    ``{{AGE}}``. So every combination of {dropped optional sections} x {AGE blank} is
    rendered with a sentinel per slot, giving the exact skeleton of that case, and the
    seed is matched against each. A candidate counts only if ``render`` reproduces the
    seed from it (up to render's own whitespace normalisation): the round trip is the
    proof, not the parse. Of the proven answer sets, the one closest to ``prior`` (the
    current record: an edit changes few answers) wins, then the one leaving the most text
    to the template; none, or a tie on both, is a refusal."""
    import itertools

    slot_list = list(dict.fromkeys(slots))
    groups = [list(g) for g in optional_sections]
    age_options = (False, True) if "AGE" in slot_list else (False,)
    combos = 2 ** len(groups) * len(age_options)
    if combos > max_combinations:
        raise AdoptRefused(f"this template has {len(groups)} optional sections; trying every "
                           f"combination of them is not bounded enough to be safe.")
    target = _normalise(text)
    unsplittable: AdoptRefused | None = None
    proven: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    for dropped in itertools.product((False, True), repeat=len(groups)):
        blank = {s for g, d in zip(groups, dropped) if d for s in g}
        for age_blank in age_options:
            fill = {s: ("" if s in blank or (age_blank and s == "AGE") else _sentinel(s))
                    for s in slot_list}
            skeleton = _normalise(render(fill))
            for from_right in (False, True):
                try:
                    found = _split(target, skeleton, from_right=from_right)
                except AdoptRefused as e:
                    # This combination puts two fields side by side; another (one of them
                    # blank, its section dropped) may still prove the seed.
                    unsplittable = e
                    continue
                if found is None:
                    continue
                got = {s: "" for s in slot_list}
                got.update(found)
                if _normalise(render(got)) == target:
                    proven[tuple(sorted(got.items()))] = got
    if not proven and unsplittable is not None:
        raise unsplittable
    if not proven:
        raise AdoptRefused("the seed differs from the template outside the interview's "
                           "fields (or a field's text was split across a heading), so no "
                           "answer set reproduces it.")
    # Several answer sets can render the same text: a blank AGE lets "Chris. 46." read
    # as a name of "Chris. 46", and a dropped optional section lets the previous field
    # swallow its heading. Each such reading moves template text INTO a value, so the
    # reading that leaves the most text to the template (the least in the values) is the
    # template's own. A tie at that minimum is a real ambiguity, and refuses.
    # The current record ranks first: "Op. St. Louis." also reads as AGE="St",
    # LOCATION="Louis" (L2, reproduced), and only the record says AGE was blank.
    def rank(a: dict[str, str]) -> tuple[int, int]:
        moved = sum(a[s] != prior.get(s, "") for s in slot_list) if prior else 0
        return moved, sum(len(v) for v in a.values())

    best = min(rank(a) for a in proven.values())
    winners = [a for a in proven.values() if rank(a) == best]
    if len(winners) > 1:
        diff = sorted({s for a in winners for b in winners for s in slot_list if a[s] != b[s]})
        raise AdoptRefused(f"the seed reads as more than one answer set; the field(s) "
                           f"{', '.join(diff)} cannot be told apart from the text alone.")
    return winners[0]
