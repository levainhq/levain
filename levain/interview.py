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

    for spec in specs:
        output_fn("")
        output_fn(f"=== {spec.path.name} ===")
        for section in spec.sections:
            unasked = [s for s in section.slots if s not in answers]
            if not unasked:
                continue

            output_fn("")
            output_fn(f"## {section.title}" if section.title else "[preamble]")

            if section.optional:
                if section.optional_reason:
                    output_fn(f"  (optional — {section.optional_reason})")
                else:
                    output_fn("  (optional)")
                response = input_fn("  Skip this section? [y/N] ").strip().lower()
                if response in ("y", "yes"):
                    for slot in unasked:
                        answers[slot] = ""
                    continue

            if section.guidance:
                output_fn("")
                output_fn(f"  Guidance: {section.guidance}")
                output_fn("")

            sub_guidance = _split_guidance(section.guidance, section.slots)
            for slot in unasked:
                # Style precedence: explicit `style=X` tag on the section
                # (interview engine v2) > keyword-soup detection on the
                # section's per-slot clause (single-slot inherits section
                # hints; multi-slot uses its per-slot sub-clause).
                if section.explicit_style is not None:
                    style = section.explicit_style
                else:
                    clause = sub_guidance.get(slot)
                    style_basis = clause if clause is not None else section.hints
                    style = _detect_input_style(slot, style_basis)
                answers[slot] = _prompt_for_slot(slot, style, input_fn, output_fn)

            # Persist progress after each completed section — Ctrl+C between
            # sections then resumes from this point on next levain init.
            if checkpoint_fn is not None:
                try:
                    checkpoint_fn(answers)
                except Exception:
                    # Checkpoint persistence is best-effort; never fail the
                    # interview on a filesystem hiccup.
                    pass

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
) -> str:
    if style == "bullet":
        output_fn(f"  {slot} (one per line, blank line to finish):")
        items: list[str] = []
        while True:
            try:
                line = input_fn("    - ").strip()
            except EOFError:
                break
            if not line:
                break
            items.append(f"- {line}")
        return "\n".join(items)

    if style == "prose":
        output_fn(f"  {slot} (multi-line prose, blank line on its own to finish):")
        lines: list[str] = []
        while True:
            try:
                line = input_fn("    ")
            except EOFError:
                break
            if line.strip() == "" and lines:
                break
            if line.strip() == "" and not lines:
                continue
            lines.append(line.rstrip())
        return "\n".join(lines).strip()

    if style == "optional-line":
        return input_fn(f"  {slot} (optional, blank to skip): ").strip()

    return input_fn(f"  {slot}: ").strip()


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
