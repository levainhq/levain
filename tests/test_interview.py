"""Unit tests for the interview engine.

Covers the pure functions plus parse_template + render_template round-trip.
Lives separate from integration tests for `levain init` because the engine
is the load-bearing piece that misbehaves invisibly when its pure functions
drift (rename canonicalization, style detection, slot extraction).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from levain.interview import (
    _detect_input_style,
    _split_guidance,
    _unique_slots,
    conduct_interview,
    parse_template,
    render_template,
)


# ---------- _unique_slots ----------

def test_unique_slots_extracts_in_order():
    text = "Hello {{NAME}} from {{CITY}}, age {{AGE}}."
    assert _unique_slots(text) == ["NAME", "CITY", "AGE"]


def test_unique_slots_dedupes_preserving_first_occurrence():
    text = "{{A}} {{B}} {{A}} {{C}} {{B}}"
    assert _unique_slots(text) == ["A", "B", "C"]


def test_unique_slots_empty_when_no_slots():
    assert _unique_slots("plain markdown with no slots") == []


def test_unique_slots_ignores_lowercase_and_partial_braces():
    text = "{{lower}} {{ALSO_OK}} {{}} {NOT_DOUBLE} {{ALSO_OK}}"
    # Lowercase-only slot names are skipped by SLOT_RE (\w+ matches but
    # uppercase convention is enforced at usage sites — here we just verify
    # what the regex actually captures).
    captured = _unique_slots(text)
    assert "ALSO_OK" in captured
    assert "NOT_DOUBLE" not in captured


# ---------- _split_guidance ----------

def test_split_guidance_single_slot_returns_empty_dict():
    # The convention: callers fall back to whole guidance for single-slot
    # sections. Empty dict is the signal to fall back.
    assert _split_guidance("just one clause", ["ONLY_SLOT"]) == {}


def test_split_guidance_splits_on_top_level_semicolons():
    result = _split_guidance("first slot; second slot; third slot", ["A", "B", "C"])
    assert result == {"A": "first slot", "B": "second slot", "C": "third slot"}


def test_split_guidance_preserves_semicolons_inside_parens():
    # Parenthetical-semicolons must NOT split — they're inside a single clause.
    result = _split_guidance(
        "first (with a; nested clause); second", ["A", "B"]
    )
    assert result == {"A": "first (with a; nested clause)", "B": "second"}


def test_split_guidance_fallback_when_too_few_semicolons():
    # Multi-slot section but author forgot a separator → fallback (empty dict).
    # This is the case the parse-time warning surfaces.
    assert _split_guidance("one clause for two slots", ["A", "B"]) == {}


def test_split_guidance_more_parts_than_slots_uses_first_n():
    # Extra trailing clauses get dropped — pair sub-clauses with slots in
    # order, ignore the tail.
    result = _split_guidance("a; b; c; d", ["X", "Y"])
    assert result == {"X": "a", "Y": "b"}


def test_split_guidance_handles_unbalanced_parens_gracefully():
    # max(0, depth - 1) means stray `)` doesn't underflow.
    result = _split_guidance("orphan ) here; second", ["A", "B"])
    assert "A" in result and "B" in result


# ---------- _detect_input_style ----------

def test_detect_input_style_defaults_to_line():
    assert _detect_input_style("NAME", "their name") == "line"


def test_detect_input_style_returns_prose_on_keyword():
    assert _detect_input_style("BIO", "synthesize into a paragraph") == "prose"
    assert _detect_input_style("BIO", "give a prose summary") == "prose"
    assert _detect_input_style("BIO", "a short paragraph") == "prose"


def test_detect_input_style_returns_bullet_on_keyword():
    assert _detect_input_style("ITEMS", "one per line, as a bulleted list") == "bullet"


def test_detect_input_style_returns_optional_line_on_inline_optional():
    # The slot itself is marked optional in the guidance — return optional-line
    # so the interview can prompt for "blank to skip".
    guidance = "full name; age (optional — omit if not given); city"
    assert _detect_input_style("AGE", guidance) == "optional-line"


# ---------- parse_template + render_template round-trip ----------

def test_parse_template_round_trip_renders_no_leftover_slots(tmp_path: Path):
    template = tmp_path / "test.md"
    template.write_text(
        "# Test\n\n"
        "## Section\n\n"
        "<!-- interview: who they are -->\n\n"
        "Hello {{NAME}}.\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    assert len(spec.sections) >= 1
    answers = {"NAME": "Alex"}
    rendered = render_template(spec, answers)
    assert "{{" not in rendered
    assert "Alex" in rendered


def test_parse_template_multi_slot_section_carries_guidance_across_slots(tmp_path: Path):
    template = tmp_path / "multi.md"
    template.write_text(
        "# Test\n\n"
        "## Identity\n\n"
        "<!-- interview: full name; city of residence -->\n\n"
        "Name: {{NAME}}\nCity: {{CITY}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    section = [s for s in spec.sections if "NAME" in s.slots][0]
    assert section.slots == ["NAME", "CITY"]
    # _split_guidance should pair correctly with semicolons.
    split = _split_guidance(section.guidance, section.slots)
    assert split == {"NAME": "full name", "CITY": "city of residence"}


# ---------- parse-time warning ----------

def test_parse_template_warns_on_multi_slot_without_semicolons(tmp_path: Path, capsys):
    template = tmp_path / "broken.md"
    template.write_text(
        "# Broken\n\n"
        "## Bad\n\n"
        "<!-- interview: this guidance has no semicolons but multiple slots -->\n\n"
        "Hello {{A}} {{B}} {{C}}.\n",
        encoding="utf-8",
    )
    parse_template(template)
    captured = capsys.readouterr()
    assert "WARN" in captured.err
    assert "Bad" in captured.err
    assert "A" in captured.err
    assert "B" in captured.err
    assert "C" in captured.err


def test_parse_template_no_warning_on_canonical_multi_slot(tmp_path: Path, capsys):
    template = tmp_path / "good.md"
    template.write_text(
        "# Good\n\n"
        "## Identity\n\n"
        "<!-- interview: full name; city; age -->\n\n"
        "{{NAME}} {{CITY}} {{AGE}}\n",
        encoding="utf-8",
    )
    parse_template(template)
    captured = capsys.readouterr()
    assert "WARN" not in captured.err


def test_parse_template_no_warning_on_single_slot(tmp_path: Path, capsys):
    template = tmp_path / "single.md"
    template.write_text(
        "# Single\n\n"
        "## Solo\n\n"
        "<!-- interview: one piece of info -->\n\n"
        "{{INFO}}\n",
        encoding="utf-8",
    )
    parse_template(template)
    captured = capsys.readouterr()
    assert "WARN" not in captured.err


# ---------- conduct_interview with injected driver ----------

def test_conduct_interview_with_injected_input_fn(tmp_path: Path):
    template = tmp_path / "simple.md"
    template.write_text(
        "# Simple\n\n"
        "## A Section\n\n"
        "<!-- interview: who they are -->\n\n"
        "{{NAME}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    answers = conduct_interview(
        [spec],
        input_fn=lambda prompt: "Alex",
        output_fn=lambda s: None,
    )
    assert answers.get("NAME") == "Alex"


def test_conduct_interview_shares_answers_across_specs(tmp_path: Path):
    """Cross-spec slot sharing: if both world.md and origin.md have
    `{{OPERATOR_NAME}}`, the second spec must not re-prompt."""
    t1 = tmp_path / "first.md"
    t1.write_text(
        "# First\n\n## S\n\n<!-- interview: their name -->\n\n{{OPERATOR_NAME}}\n",
        encoding="utf-8",
    )
    t2 = tmp_path / "second.md"
    t2.write_text(
        "# Second\n\n## S\n\n<!-- interview: their name -->\n\n{{OPERATOR_NAME}} {{NEW_SLOT}}\n",
        encoding="utf-8",
    )
    spec1 = parse_template(t1)
    spec2 = parse_template(t2)
    prompts_seen: list[str] = []

    def driver(prompt: str) -> str:
        prompts_seen.append(prompt)
        return "Alex" if "OPERATOR" in prompt else "value"

    answers = conduct_interview(
        [spec1, spec2],
        input_fn=driver,
        output_fn=lambda s: None,
    )
    # OPERATOR_NAME should have been asked exactly once.
    operator_prompts = [p for p in prompts_seen if "OPERATOR" in p]
    assert len(operator_prompts) == 1
    assert answers["OPERATOR_NAME"] == "Alex"
    assert answers["NEW_SLOT"] == "value"


# ---------- canonical seed templates parse cleanly ----------

def test_shipped_seed_templates_parse_without_warnings(capsys):
    """Sanity check the templates we actually ship don't trigger the
    parse-time warning."""
    from importlib.resources import as_file, files

    with as_file(files("levain") / "templates" / "seed") as seed_dir:
        for name in ("world.md", "origin.md"):
            parse_template(Path(seed_dir) / name)
    captured = capsys.readouterr()
    assert "WARN" not in captured.err, (
        f"Shipped seed templates triggered parse-time warning:\n{captured.err}"
    )
