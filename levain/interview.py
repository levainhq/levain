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
# `:clear`/`:empty` forces an EXPLICIT empty value, overriding the blank=keep
# rule on a revisit (blank keeps `current`; `:clear` discards it — the only way
# to empty an already-entered field).
# These exact strings are reserved — an operator cannot enter ":back"/":b"/
# ":clear"/":empty" as a literal field value (acceptable: seed slots are
# names/bios/roles, not commands).
_BACK_COMMANDS = (":back", ":b")
_CLEAR_COMMANDS = (":clear", ":empty")


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


@dataclass
class InterviewField:
    """One resolved interview field — the slot, its input style, the operator-
    facing guidance, and its section context. The PUBLIC, surface-agnostic unit
    of the field plan (`build_field_plan`), consumed by BOTH the terminal
    `conduct_interview` walk and the web init form so the two onboarding
    surfaces share one derivation and never drift. Every attribute is a JSON
    primitive (str/int/bool) — no live template/section object — so the plan
    projects cleanly to JSON for the web surface.

    Shape ratified by the spore-181 Slice-A seam-lens review (still provisional
    until the live web form consumes it). Two guidance fields by design:
    `section_guidance` is the FULL section guidance, shown ONCE under the section
    header (mirroring the terminal); `guidance` is this slot's split sub-clause
    for multi-slot sections (empty for single-slot, where the section guidance
    is the whole story). `section_index` is a STABLE per-section identity for
    grouping — group by it, never by `section_title`, which can collide across
    duplicate `## headers`."""

    slot: str
    style: str             # exactly one of _VALID_STYLES
    guidance: str          # this slot's split sub-clause; "" for single-slot sections
    section_guidance: str  # the FULL section guidance (shown once, on first_in_section)
    section_title: str     # "" for the preamble section
    section_index: int     # stable per-section grouping key (NOT the title — titles collide)
    first_in_section: bool # the field that should print the section header + guidance
    optional: bool         # section-level optional flag
    optional_reason: str
    current: str           # pre-fill value ("" if none) — attached, never excluded
    spec_name: str         # source template filename (for grouping/display)


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
        # The preamble can carry its own `<!-- interview ... -->` comment too
        # (origin.md has no `## ` sections — its entity fields live in the
        # preamble), so extract guidance for it exactly as a `## ` section does.
        guidance, hints, explicit_style = _parse_interview(preamble)
        sections.append(
            Section(
                title="",
                slots=preamble_slots,
                guidance=guidance,
                hints=hints,
                optional=False,
                explicit_style=explicit_style,
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
        guidance, hints, explicit_style = _parse_interview(section_body)

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


def _parse_interview(body: str) -> tuple[str, str, str | None]:
    """Extract ``(guidance, hints, explicit_style)`` from a section/preamble
    body's ``<!-- interview ... -->`` comment.

    The single extraction used by BOTH a ``## `` section and the preamble (so
    origin.md's title-less entity fields get guidance the same way a titled
    section does). ``hints`` is the full comment body (kept for style detection,
    which keys off the whole text); ``guidance`` is the operator-facing text with
    a leading ``style=X`` tag and a leading meta-parenthetical stripped;
    ``explicit_style`` is the ``style=X`` tag if present. Returns
    ``("", "", None)`` when the body has no interview comment."""
    m = INTERVIEW_RE.search(body)
    if not m:
        return "", "", None
    hints = re.sub(r"\s+", " ", m.group(1)).strip()
    explicit_style: str | None = None
    # Strip a leading `style=X` tag before anything else so it doesn't leak into
    # the operator-facing guidance.
    style_match = _STYLE_TAG_RE.match(hints)
    if style_match:
        explicit_style = style_match.group(1)
        hints = hints[style_match.end():].strip()
    # Drop a leading meta-parenthetical (`(elicit ... synthesize ...)`) + its
    # colon so the operator sees only the question(s).
    guidance = _LEADING_PAREN_COLON_RE.sub("", hints).lstrip(": ").strip()
    return guidance, hints, explicit_style


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

    # Build the flat, ordered field plan via the shared derivation (so the
    # terminal walk and the web init form can never drift). Optional-section
    # skip is resolved interactively in the walk below.
    plan = _plan_fields(specs, set(answers.keys()))

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

        style = _resolve_style(section, field.slot)

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


def _plan_fields(specs: list[TemplateSpec], planned: set[str]) -> list[_Field]:
    """The flat, ordered, deduplicated field walk shared by `conduct_interview`
    and `build_field_plan`.

    `planned` is the set of already-known slots to skip — a slot already
    answered, or already planned by an earlier section/spec, is not re-asked,
    reproducing the cumulative cross-spec/-section dedup. Copied internally so
    the caller's set is not mutated.
    """
    planned = set(planned)
    plan: list[_Field] = []
    for spec in specs:
        for section in spec.sections:
            for slot in section.slots:
                if slot in planned:
                    continue
                planned.add(slot)
                plan.append(_Field(spec=spec, section=section, slot=slot))
    return plan


def _resolve_style(section: Section, slot: str) -> str:
    """Resolve the input style for one slot. Precedence: an explicit `style=X`
    section tag (interview engine v2) > keyword detection on the per-slot
    guidance clause (multi-slot) or the section hints (single-slot inherits)."""
    if section.explicit_style is not None:
        return section.explicit_style
    sub_guidance = _split_guidance(section.guidance, section.slots)
    clause = sub_guidance.get(slot)
    style_basis = clause if clause is not None else section.hints
    return _detect_input_style(slot, style_basis)


def build_field_plan(
    specs: list[TemplateSpec],
    values: dict[str, str] | None = None,
) -> list[InterviewField]:
    """The ORDERED interview field plan as flat `InterviewField`s.

    The shared, surface-agnostic derivation behind both the terminal
    `conduct_interview` walk and the web init form — pure (no I/O, no prompting),
    so a downstream surface renders the SAME questions, in the SAME order, with
    the SAME resolved styles the CLI would ask.

    `values` PRE-FILLS each field's `current` (a checkpoint / resume map); it
    does NOT exclude — the web form renders EVERY field, editable. Cross-template
    shared-slot dedup is automatic (a slot defined in two specs is planned once,
    on its first occurrence) and independent of `values`. Sections appear as
    contiguous runs; `section_index` + `first_in_section` mark their boundaries
    so a section-grouped renderer never has to infer them from the (collidable)
    title.
    """
    values = values or {}
    fields: list[InterviewField] = []
    section_indices: dict[int, int] = {}  # id(section) -> stable section_index
    for field in _plan_fields(specs, set()):
        section = field.section
        sid = id(section)
        first_in_section = sid not in section_indices
        if first_in_section:
            section_indices[sid] = len(section_indices)
        sub_guidance = _split_guidance(section.guidance, section.slots)
        fields.append(
            InterviewField(
                slot=field.slot,
                style=_resolve_style(section, field.slot),
                guidance=sub_guidance.get(field.slot, ""),
                section_guidance=section.guidance,
                section_title=section.title,
                section_index=section_indices[sid],
                first_in_section=first_in_section,
                optional=section.optional,
                optional_reason=section.optional_reason,
                current=values.get(field.slot, ""),
                spec_name=field.spec.path.name,
            )
        )
    return fields


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
    back-navigation command (`:back`/`:b`), an explicit empty string if they
    typed a clear command (`:clear`/`:empty`), or `current` if they submitted
    an immediate blank. The blank rule — *blank = leave as-is* — covers both
    skip-and-scaffold-empty on a first visit (`current == ""`) and keep-the-
    prior-answer on a back-navigation revisit (`current` is the old value);
    `:clear` is the explicit override that DISCARDS `current`, the only way to
    empty an already-entered field on a revisit (where blank keeps it).
    """
    if current:
        output_fn(f"  {slot} — current value:")
        for line in current.splitlines():
            output_fn(f"      | {line}")
        output_fn(
            "      (blank = keep, retype = replace, :clear/:empty = empty, "
            ":back = previous)"
        )

    blank_action = "keep" if current else "skip"
    # `:clear` is only meaningful on a revisit (where blank keeps `current`) —
    # surfaced inline for the multi-line styles so it sits alongside their
    # restated `:back`, matching `:back`'s own block+inline parity. The single-
    # line styles get it from the current-value block directly above (and the
    # `line` prompt string is held byte-exact by a back-compat test).
    clear_hint = ", :clear = empty" if current else ""

    if style == "bullet":
        output_fn(
            f"  {slot} (one per line; blank line = {blank_action}{clear_hint}, "
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
            if not items and line in _CLEAR_COMMANDS:
                return ""
            if not line:
                if items:
                    break
                return current  # immediate blank = leave as-is
            items.append(f"- {line}")
        return "\n".join(items)

    if style == "prose":
        output_fn(
            f"  {slot} (multi-line; blank line = {blank_action}{clear_hint}, "
            f"write then blank line to finish, :back = previous):"
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
            if not lines and stripped in _CLEAR_COMMANDS:
                return ""
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
        if raw in _CLEAR_COMMANDS:
            return ""
        return current if raw == "" else raw

    raw = input_fn(f"  {slot}: ").strip()
    if raw in _BACK_COMMANDS:
        return _BACK
    if raw in _CLEAR_COMMANDS:
        return ""
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
