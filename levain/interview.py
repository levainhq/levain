"""Scripted interview engine.

Derives questions from `seed/world.md` + `seed/origin.md` — per-slot HTML-comment
guidance IS the interview spec. The templates own the question text; the
engine extracts and renders it.

Public surface:
  parse_template(path) -> TemplateSpec
  conduct_interview(specs, answers=None, ...) -> dict[slot, answer]
  render_template(spec, answers) -> str
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SLOT_RE = re.compile(r"\{\{(\w+)\}\}")
SECTION_SPLIT_RE = re.compile(r"(?m)^## ")
OPTIONAL_RE = re.compile(r"<!--\s*optional\s*[:\s](.*?)-->", re.DOTALL)
# Match the full `<!-- interview ... -->` comment body, parenthetical and all.
INTERVIEW_RE = re.compile(r"<!--\s*interview\b(.*?)-->", re.DOTALL)
# Strip a leading meta-parenthetical `(...)`: from the captured body so the
# operator-facing guidance starts at the actual interview questions.
_LEADING_PAREN_COLON_RE = re.compile(r"^\s*\([^)]*\)\s*:\s*")
# Recognize an explicit style tag at the very start of the interview body:
# `<!-- interview style=prose: question? -->`. Strips after capture so the
# style word doesn't leak into the guidance.
_STYLE_TAG_RE = re.compile(r"^\s*style\s*=\s*(line|bullet|prose|optional-line)\b\s*")
_VALID_STYLES = ("line", "bullet", "prose", "optional-line")
# Also consume the preceding empty `>` blockquote line so the surrounding
# blockquote stays well-formed after the stripped instruction.
_ONBOARDING_BLURB_RE = re.compile(
    r"^>\s*\n> \*Onboarding fills the.*?\*\n",
    flags=re.MULTILINE | re.DOTALL,
)

# Operator commands recognized at any interview prompt. `:back` re-opens the
# previous answer for editing (composable — repeat to step further back).
# These exact strings are reserved — an operator cannot enter ":back"/":b" as a
# literal field value (acceptable: seed slots are names/bios/roles, not commands).
_BACK_COMMANDS = (":back", ":b")


class _Back:
    """Sentinel returned by `_prompt_for_slot` when the operator typed a
    back-navigation command instead of an answer."""


_BACK = _Back()


@dataclass
class Section:
    """A `## Header` section of a seed template plus its slots + guidance.

    A section with title="" carries the preamble — for templates whose title
    line itself holds a slot (origin.md's `# Who You Are — {{ENTITY_NAME}}`).

    `guidance` is the operator-facing interview text (sub-questions). `hints`
    is the full comment body including any meta-parenthetical that affects
    style detection (e.g. "synthesize into one coherent picture" → prose).
    `explicit_style`, when set, overrides keyword-based style detection in
    `_detect_input_style` — from `<!-- interview style=prose: ... -->`.
    """

    title: str
    slots: list[str]
    guidance: str
    hints: str
    optional: bool
    optional_reason: str = ""
    explicit_style: str | None = None


@dataclass
class TemplateSpec:
    path: Path
    sections: list[Section]
    raw: str


@dataclass
class _Field:
    """One slot to prompt, tagged with its spec + section. The flat list of
    these is what `conduct_interview` walks by index so back-navigation can
    cross section and spec boundaries."""

    spec: TemplateSpec
    section: Section
    slot: str


def parse_template(path: Path) -> TemplateSpec:
    text = path.read_text(encoding="utf-8")
    sections: list[Section] = []

    parts = SECTION_SPLIT_RE.split(text)
    preamble = parts[0]

    # Strip the onboarding-instructions blurb from preamble BEFORE slot
    # extraction — its literal `{{SLOTS}}` reference is documentation, not
    # an interview slot.
    preamble_for_slots = _ONBOARDING_BLURB_RE.sub("", preamble)
    preamble_slots = _unique_slots(preamble_for_slots)
    if preamble_slots:
        sections.append(
            Section(
                title="",
                slots=preamble_slots,
                guidance="",
                hints="",
                optional=False,
            )
        )

    for body in parts[1:]:
        if "\n" in body:
            first_nl = body.index("\n")
            title = body[:first_nl].strip()
            section_body = body[first_nl:]
        else:
            title = body.strip()
            section_body = ""

        slots = _unique_slots(section_body)
        if not slots:
            continue

        optional_match = OPTIONAL_RE.search(section_body)
        interview_match = INTERVIEW_RE.search(section_body)
        hints = ""
        guidance = ""
        explicit_style: str | None = None
        if interview_match:
            hints = re.sub(r"\s+", " ", interview_match.group(1)).strip()
            # Strip a leading `style=X` tag before any other processing so
            # it doesn't leak into the operator-facing guidance.
            style_match = _STYLE_TAG_RE.match(hints)
            if style_match:
                explicit_style = style_match.group(1)
                hints = hints[style_match.end():].strip()
            # Drop the meta-parenthetical (`(elicit ... synthesize ...)`) +
            # its colon so the operator sees only the question(s).
            guidance = _LEADING_PAREN_COLON_RE.sub("", hints).lstrip(": ").strip()

        sections.append(
            Section(
                title=title,
                slots=slots,
                guidance=guidance,
                hints=hints,
                optional=optional_match is not None,
                optional_reason=(
                    optional_match.group(1).strip().rstrip(".")
                    if optional_match
                    else ""
                ),
                explicit_style=explicit_style,
            )
        )

    spec = TemplateSpec(path=path, sections=sections, raw=text)
    _warn_on_unsplittable_multislot_sections(spec)
    return spec


def _warn_on_unsplittable_multislot_sections(spec: TemplateSpec) -> None:
    """Surface multi-slot interview sections whose guidance would silently
    fall back to applying the entire guidance to each slot.

    `_split_guidance` returns `{}` (the fallback signal) when a multi-slot
    section's guidance has fewer top-level semicolons than slots-1. That's
    almost always an authoring mistake — the seed-template author wrote
    multiple slots intending per-slot guidance but forgot the separators.
    Warn at parse time so template authors catch it before an operator
    hits the interview. Emits to stderr; never raises.
    """
    for section in spec.sections:
        if len(section.slots) <= 1 or not section.guidance:
            continue
        if _split_guidance(section.guidance, section.slots):
            continue  # split succeeded → no warning
        title = section.title or "<preamble>"
        slot_list = ", ".join(section.slots)
        print(
            f"WARN: {spec.path.name}: section {title!r} has "
            f"{len(section.slots)} slots [{slot_list}] but interview "
            f"guidance is missing the `;` separators needed to split it "
            f"per-slot. Each slot will receive the full guidance text. "
            f"Add semicolons between per-slot clauses if that's not "
            f"intended.",
            file=sys.stderr,
        )


def _unique_slots(text: str) -> list[str]:
    """All slot names in `text`, in order of first occurrence, deduplicated."""
    seen: dict[str, None] = {}
    for name in SLOT_RE.findall(text):
        seen.setdefault(name, None)
    return list(seen.keys())


def conduct_interview(
    specs: list[TemplateSpec],
    answers: dict[str, str] | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    checkpoint_fn: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, str]:
    """Walk every spec, prompt for each slot, return slot -> answer dict.

    `answers` carries cumulatively across specs — if origin.md and world.md
    both define `{{OPERATOR_NAME}}`, the second spec skips re-asking. This is
    the structural mechanism for shared cross-template slots.

    `input_fn` and `output_fn` are dependency-injected so the engine is
    testable without a real terminal.

    `checkpoint_fn`, when provided, is called with the running `answers`
    dict after each section completes. install.py uses this to persist
    progress so a Ctrl+C mid-interview can be resumed on the next
    `levain init` against the same path.
    """
    answers = dict(answers or {})

    # Build the flat, ordered field plan. Cross-spec/-section dedup is
    # simulated here (a slot already answered, or already planned by an
    # earlier section/spec, is not re-asked) — reproducing the cumulative
    # contract the old nested loop got from checking `answers`. Optional-
    # section skip is resolved interactively in the walk below.
    plan: list[_Field] = []
    planned: set[str] = set(answers.keys())
    for spec in specs:
        for section in spec.sections:
            for slot in section.slots:
                if slot in planned:
                    continue
                planned.add(slot)
                plan.append(_Field(spec=spec, section=section, slot=slot))

    if not plan:
        return answers

    output_fn("")
    output_fn("  (Tip: type :back at any prompt to revise your previous answer.)")

    seen_specs: set[int] = set()         # id(spec) — spec header shown
    entered_sections: set[int] = set()   # id(section) — section header shown
    skipped_sections: set[int] = set()   # id(section) — optional skip chosen
    visited_fields: set[int] = set()     # plan index — field already prompted
    #   nav-state (visited) is deliberately separate from value-state (answers):
    #   a field bailed out of with `:back` before answering is still "visited".

    i = 0
    total = len(plan)
    while i < total:
        field = plan[i]
        section = field.section
        sid = id(section)

        if id(field.spec) not in seen_specs:
            seen_specs.add(id(field.spec))
            output_fn("")
            output_fn(f"=== {field.spec.path.name} ===")

        # A section decided-skipped earlier (its slots already set ""): fill
        # transparently and advance — keeps skipped sections invisible to the
        # walk in the forward direction.
        if sid in skipped_sections:
            answers.setdefault(field.slot, "")
            i += 1
            continue

        if sid not in entered_sections:
            entered_sections.add(sid)
            output_fn("")
            output_fn(f"## {section.title}" if section.title else "[preamble]")

            if section.optional:
                if section.optional_reason:
                    output_fn(f"  (optional — {section.optional_reason})")
                else:
                    output_fn("  (optional)")
                response = input_fn("  Skip this section? [y/N] ").strip().lower()
                if response in _BACK_COMMANDS:
                    # `:back` at the skip prompt — the tip promises "any
                    # prompt". Un-commit this section's entry (so re-arrival
                    # re-asks the skip) and step back to the previous prompted
                    # field. Never land INSIDE the section, which would let the
                    # forward auto-skip bounce straight back here.
                    entered_sections.discard(sid)
                    j = i - 1
                    while j >= 0 and id(plan[j].section) in skipped_sections:
                        j -= 1
                    if j >= 0:
                        i = j
                    else:
                        output_fn(
                            "  (Already at the first question — nothing to go back to.)"
                        )
                    continue
                if response in ("y", "yes"):
                    skipped_sections.add(sid)
                    while i < total and id(plan[i].section) == sid:
                        answers[plan[i].slot] = ""
                        i += 1
                    continue

            if section.guidance:
                output_fn("")
                output_fn(f"  Guidance: {section.guidance}")
                output_fn("")
        elif i in visited_fields:
            # Re-arrived at a field we already prompted (back-navigation):
            # re-orient with a "(revising)" header. Using visited-state (not
            # `answers`) means a field bailed out of with `:back` before being
            # answered still restores its section context on return. A forward
            # walk through slots 2..N of a multi-slot section is NOT yet visited
            # → falls through here with no header (shown once on the first slot).
            output_fn("")
            output_fn(f"## {section.title or '[preamble]'}  (revising)")

        visited_fields.add(i)

        # Style precedence: explicit `style=X` tag on the section (interview
        # engine v2) > keyword-soup detection on the section's per-slot clause
        # (single-slot inherits section hints; multi-slot uses its sub-clause).
        sub_guidance = _split_guidance(section.guidance, section.slots)
        if section.explicit_style is not None:
            style = section.explicit_style
        else:
            clause = sub_guidance.get(field.slot)
            style_basis = clause if clause is not None else section.hints
            style = _detect_input_style(field.slot, style_basis)

        result = _prompt_for_slot(
            field.slot, style, input_fn, output_fn,
            current=answers.get(field.slot, ""),
        )

        if isinstance(result, _Back):
            # Step back to the previous prompted field, hopping over any
            # skipped optional section so back-navigation is symmetric.
            j = i - 1
            while j >= 0 and id(plan[j].section) in skipped_sections:
                j -= 1
            if j < 0:
                output_fn("  (Already at the first question — nothing to go back to.)")
                continue
            i = j
            continue

        answers[field.slot] = result

        # Persist progress after each completed section (matches the old
        # per-section cadence) — Ctrl+C then resumes from here on next init.
        last_in_section = (i + 1 >= total) or (id(plan[i + 1].section) != sid)
        if last_in_section and checkpoint_fn is not None:
            try:
                checkpoint_fn(answers)
            except Exception:
                # Checkpoint persistence is best-effort; never fail the
                # interview on a filesystem hiccup.
                pass

        i += 1

    return answers


def _split_guidance(guidance: str, slots: list[str]) -> dict[str, str]:
    """For multi-slot sections, split guidance into per-slot sub-clauses.

    Convention: when a section has N slots and guidance contains ≥ N-1
    semicolons, split on `;` (outside parentheses) and pair sub-clauses with
    slots in order. Otherwise return {} — callers fall back to the whole
    guidance, which is correct for single-slot sections.
    """
    if len(slots) <= 1:
        return {}

    parts: list[str] = []
    depth = 0
    current = []
    for ch in guidance:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == ";" and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)

    if len(parts) < len(slots):
        return {}

    return {slot: parts[i] for i, slot in enumerate(slots)}


def _detect_input_style(slot: str, guidance: str) -> str:
    """Choose input UI per slot: line, optional-line, bullet, prose."""
    g = guidance.lower()

    slot_inline_optional = re.compile(
        rf"\b{re.escape(slot.lower())}\b[^.]*?\(\s*optional", re.IGNORECASE
    )
    if slot_inline_optional.search(guidance):
        return "optional-line"

    if "bulleted list" in g or "bullet list" in g:
        return "bullet"
    if "one line each" in g and "list" in g:
        return "bullet"

    if "prose" in g or "paragraph" in g or "synthesize" in g:
        return "prose"

    return "line"


def _prompt_for_slot(
    slot: str,
    style: str,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    current: str = "",
) -> str | _Back:
    """Prompt for one slot's value.

    Returns the entered value, the `_BACK` sentinel if the operator typed a
    back-navigation command (`:back`/`:b`), or `current` if they submitted an
    immediate blank. That one rule — *blank = leave as-is* — covers both
    skip-and-scaffold-empty on a first visit (`current == ""`) and keep-the-
    prior-answer on a back-navigation revisit (`current` is the old value).
    """
    if current:
        output_fn(f"  {slot} — current value:")
        for line in current.splitlines():
            output_fn(f"      | {line}")
        output_fn("      (blank = keep, retype = replace, :back = previous)")

    blank_action = "keep" if current else "skip"

    if style == "bullet":
        output_fn(
            f"  {slot} (one per line; blank line = {blank_action}, "
            f":back = previous):"
        )
        items: list[str] = []
        while True:
            try:
                line = input_fn("    - ").strip()
            except EOFError:
                if not items:
                    return current  # EOF on an empty field = leave as-is
                break
            if not items and line in _BACK_COMMANDS:
                return _BACK
            if not line:
                if items:
                    break
                return current  # immediate blank = leave as-is
            items.append(f"- {line}")
        return "\n".join(items)

    if style == "prose":
        output_fn(
            f"  {slot} (multi-line; blank line = {blank_action}, write then "
            f"blank line to finish, :back = previous):"
        )
        lines: list[str] = []
        while True:
            try:
                line = input_fn("    ")
            except EOFError:
                if not lines:
                    return current  # EOF on an empty field = leave as-is
                break
            stripped = line.strip()
            if not lines and stripped in _BACK_COMMANDS:
                return _BACK
            if stripped == "":
                if lines:
                    break  # blank after content = finish
                return current  # immediate blank = leave as-is
            lines.append(line.rstrip())
        return "\n".join(lines).strip()

    if style == "optional-line":
        raw = input_fn(f"  {slot} (optional, blank to skip, :back = previous): ").strip()
        if raw in _BACK_COMMANDS:
            return _BACK
        return current if raw == "" else raw

    raw = input_fn(f"  {slot}: ").strip()
    if raw in _BACK_COMMANDS:
        return _BACK
    return current if raw == "" else raw


# --- Rendering --------------------------------------------------------------


def render_template(spec: TemplateSpec, answers: dict[str, str]) -> str:
    """Render: drop empty optional sections, substitute slots, strip comments."""
    text = spec.raw

    for section in spec.sections:
        if not (section.title and section.optional):
            continue
        if any(answers.get(s, "").strip() for s in section.slots):
            continue
        pattern = re.compile(
            r"(?ms)^## " + re.escape(section.title) + r"\b.*?(?=^## |\Z)"
        )
        text = pattern.sub("", text)

    # Inline-optional cleanup: empty AGE in Identity leaves ". {{AGE}}. " —
    # collapse the slot AND its trailing period per the template's own
    # interview note. Regex-anchored so future slots ending in "AGE" aren't
    # partially matched. Per-slot rule today; future inline-optional slots
    # add their own regex pair.
    if not answers.get("AGE", "").strip():
        text = re.sub(r"\.\s*\{\{AGE\}\}\.", ".", text)
        text = re.sub(r"\{\{AGE\}\}\.\s*", "", text)

    for slot in SLOT_RE.findall(text):
        text = text.replace(f"{{{{{slot}}}}}", answers.get(slot, ""))

    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = _ONBOARDING_BLURB_RE.sub("", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip() + "\n"
