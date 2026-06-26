"""Unit tests for the interview engine.

Covers the pure functions plus parse_template + render_template round-trip.
Lives separate from integration tests for `levain init` because the engine
is the load-bearing piece that misbehaves invisibly when its pure functions
drift (rename canonicalization, style detection, slot extraction).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from levain.interview import (
    _BACK,
    _detect_input_style,
    _prompt_for_slot,
    _split_guidance,
    _unique_slots,
    conduct_interview,
    parse_template,
    render_template,
)
from levain.interview import _STYLE_TAG_RE, _VALID_STYLES


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


# ---------- explicit style tag (v2) ----------

def test_parse_template_captures_explicit_style_prose(tmp_path: Path):
    template = tmp_path / "explicit.md"
    template.write_text(
        "# Test\n\n"
        "## About Them\n\n"
        "<!-- interview style=prose: synthesize who they are -->\n\n"
        "Hello {{BIO}}.\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    section = [s for s in spec.sections if "BIO" in s.slots][0]
    assert section.explicit_style == "prose"
    # The `style=prose` tag is stripped from the operator-facing guidance.
    assert "style=" not in section.guidance
    assert "synthesize who they are" in section.guidance


def test_parse_template_captures_explicit_style_bullet(tmp_path: Path):
    template = tmp_path / "explicit.md"
    template.write_text(
        "# Test\n\n## Items\n\n"
        "<!-- interview style=bullet: list each item -->\n\n"
        "{{ITEMS}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    section = [s for s in spec.sections if "ITEMS" in s.slots][0]
    assert section.explicit_style == "bullet"


def test_parse_template_missing_style_tag_leaves_explicit_style_none(tmp_path: Path):
    """Legacy templates without style= must continue to set explicit_style=None
    so the keyword detection still runs."""
    template = tmp_path / "legacy.md"
    template.write_text(
        "# Test\n\n## S\n\n<!-- interview: synthesize them as a paragraph -->\n\n{{X}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    section = [s for s in spec.sections if "X" in s.slots][0]
    assert section.explicit_style is None


def test_parse_template_invalid_style_tag_treated_as_legacy(tmp_path: Path):
    """`style=foo` is not in _VALID_STYLES — the regex won't match, the
    section keeps explicit_style=None and the literal text stays in
    guidance (so an author's typo is at least visible)."""
    template = tmp_path / "typo.md"
    template.write_text(
        "# Test\n\n## S\n\n<!-- interview style=foo: my question -->\n\n{{X}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    section = [s for s in spec.sections if "X" in s.slots][0]
    assert section.explicit_style is None
    # The literal `style=foo` text remains in the guidance — surfaces the typo.
    assert "style=foo" in section.guidance


def test_conduct_interview_respects_explicit_style_over_keywords(tmp_path: Path):
    """The v2 contract: explicit_style overrides keyword-soup detection.
    Verify by setting style=line on guidance that would otherwise be
    detected as prose via the keyword 'paragraph'."""
    template = tmp_path / "override.md"
    template.write_text(
        "# Test\n\n## S\n\n"
        "<!-- interview style=line: name them in a paragraph or two -->\n\n"
        "{{X}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    section = [s for s in spec.sections if "X" in s.slots][0]
    # Without the explicit tag, keyword 'paragraph' would route to prose.
    # With the explicit tag, line style takes over.
    assert section.explicit_style == "line"

    captured_prompts: list[str] = []

    def driver(prompt: str) -> str:
        captured_prompts.append(prompt)
        return "Alex"

    answers = conduct_interview(
        [spec],
        input_fn=driver,
        output_fn=lambda s: None,
    )
    assert answers == {"X": "Alex"}
    # Prose style uses a multi-line prompt ("...blank line on its own to finish")
    # Line style uses the simple "X: " prompt. Captured prompts should match
    # line-style shape.
    assert any(p.endswith("X: ") for p in captured_prompts), (
        f"Expected line-style prompt; captured: {captured_prompts}"
    )


def test_style_tag_regex_accepts_all_valid_styles():
    for style in _VALID_STYLES:
        m = _STYLE_TAG_RE.match(f"style={style}: rest")
        assert m is not None
        assert m.group(1) == style


def test_style_tag_regex_rejects_invalid_styles():
    # `\b` correctly rejects "linebreak" — `e` (word char) → `b` (word char)
    # is not a word boundary, so the regex doesn't match the "line" prefix.
    # That's the safer behavior: any unrecognized style is treated as legacy.
    for invalid in ("foo", "Line", "PROSE", "", "linebreak", "lineish"):
        m = _STYLE_TAG_RE.match(f"style={invalid}: rest")
        assert m is None, f"unexpected match for style={invalid}"


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


# ---------- _prompt_for_slot: skip (item 1) + keep / back-nav (item 2) ----------

def test_prompt_for_slot_line_blank_first_visit_returns_empty():
    # First visit (current=""): an immediate blank scaffolds empty.
    result = _prompt_for_slot(
        "NAME", "line", input_fn=lambda p: "", output_fn=lambda s: None, current=""
    )
    assert result == ""


def test_prompt_for_slot_prose_immediate_blank_skips_no_infinite_loop():
    # The item-1 fix: prose used to `continue`-loop forever on an immediate
    # blank with no content. Now it returns `current` (= "" here). If the fix
    # regressed, this test would hang rather than fail — that's the signal.
    result = _prompt_for_slot(
        "BIO", "prose", input_fn=lambda p: "", output_fn=lambda s: None, current=""
    )
    assert result == ""


def test_prompt_for_slot_prose_collects_lines_then_blank_finishes():
    feed = iter(["first line", "second line", ""])
    result = _prompt_for_slot(
        "BIO", "prose", input_fn=lambda p: next(feed), output_fn=lambda s: None, current=""
    )
    assert result == "first line\nsecond line"


def test_prompt_for_slot_bullet_immediate_blank_returns_empty():
    result = _prompt_for_slot(
        "ITEMS", "bullet", input_fn=lambda p: "", output_fn=lambda s: None, current=""
    )
    assert result == ""


def test_prompt_for_slot_bullet_collects_then_blank_finishes():
    feed = iter(["alpha", "beta", ""])
    result = _prompt_for_slot(
        "ITEMS", "bullet", input_fn=lambda p: next(feed), output_fn=lambda s: None, current=""
    )
    assert result == "- alpha\n- beta"


def test_prompt_for_slot_line_blank_on_revisit_keeps_current():
    # Item 2: blank on a revisit (current set) keeps the prior answer.
    result = _prompt_for_slot(
        "NAME", "line", input_fn=lambda p: "", output_fn=lambda s: None, current="Alex"
    )
    assert result == "Alex"


def test_prompt_for_slot_prose_blank_on_revisit_keeps_current():
    result = _prompt_for_slot(
        "BIO", "prose", input_fn=lambda p: "", output_fn=lambda s: None, current="prior bio"
    )
    assert result == "prior bio"


def test_prompt_for_slot_line_retype_on_revisit_replaces():
    result = _prompt_for_slot(
        "NAME", "line", input_fn=lambda p: "Beta", output_fn=lambda s: None, current="Alpha"
    )
    assert result == "Beta"


@pytest.mark.parametrize("style", ["line", "prose", "bullet", "optional-line"])
@pytest.mark.parametrize("cmd", [":back", ":b"])
def test_prompt_for_slot_back_command_returns_back_sentinel(style, cmd):
    result = _prompt_for_slot(
        "X", style, input_fn=lambda p: cmd, output_fn=lambda s: None, current=""
    )
    assert result is _BACK


def test_prompt_for_slot_line_prompt_string_unchanged_for_back_compat():
    # The line-style prompt must stay exactly "  {slot}: " — the :back/keep
    # affordance is surfaced via the one-time tip + the current-value block,
    # NOT the prompt string (test_conduct_interview_respects_explicit_style_
    # over_keywords asserts on this).
    captured: list[str] = []

    def driver(prompt: str) -> str:
        captured.append(prompt)
        return "v"

    _prompt_for_slot("X", "line", input_fn=driver, output_fn=lambda s: None, current="")
    assert captured == ["  X: "]


# ---------- _prompt_for_slot: :clear / :empty (spore-070) ----------

@pytest.mark.parametrize("style", ["line", "prose", "bullet", "optional-line"])
@pytest.mark.parametrize("cmd", [":clear", ":empty"])
def test_prompt_for_slot_clear_command_discards_current(style, cmd):
    # The core behavior: on a revisit (current set) a clear command returns an
    # EXPLICIT empty string, overriding the blank=keep rule — across every style.
    result = _prompt_for_slot(
        "X", style, input_fn=lambda p: cmd, output_fn=lambda s: None,
        current="prior value",
    )
    assert result == ""


@pytest.mark.parametrize("style", ["line", "prose", "bullet", "optional-line"])
@pytest.mark.parametrize("cmd", [":clear", ":empty"])
def test_prompt_for_slot_clear_command_on_first_visit_returns_empty(style, cmd):
    # On a first visit (current="") a clear command is harmless — same "" as a
    # blank — so the token stays consistently reserved at every prompt.
    result = _prompt_for_slot(
        "X", style, input_fn=lambda p: cmd, output_fn=lambda s: None, current=""
    )
    assert result == ""


@pytest.mark.parametrize("style", ["line", "prose", "bullet", "optional-line"])
def test_prompt_for_slot_clear_hint_shown_when_current_set(style):
    # The `:clear` affordance is taught in the one-time current-value block
    # (shown for EVERY style whenever there's a value to clear) — and additionally
    # inline for the multi-line styles. Parametrized so the test matches the
    # design claim ("shown for every style"), not just the line style.
    out: list[str] = []
    _prompt_for_slot(
        "NAME", style, input_fn=lambda p: "", output_fn=out.append, current="Alex"
    )
    assert any(":clear" in line and "empty" in line for line in out)


@pytest.mark.parametrize("style", ["bullet", "prose"])
def test_prompt_for_slot_clear_hint_shown_inline_for_multiline_styles(style):
    # The multi-line styles restate :back in their own prompt line; :clear must
    # ride alongside it when there's a value to clear (no "back works here but
    # maybe clear doesn't?" ambiguity at the revisit moment).
    out: list[str] = []
    _prompt_for_slot(
        "X", style, input_fn=lambda p: "", output_fn=out.append, current="prior"
    )
    # The inline prompt line (contains "blank line") carries the :clear hint.
    assert any("blank line" in line and ":clear = empty" in line for line in out)


@pytest.mark.parametrize("style", ["bullet", "prose"])
def test_prompt_for_slot_clear_hint_absent_inline_on_first_visit(style):
    # On a first visit there's nothing to clear → no inline :clear hint (and no
    # current-value block either).
    out: list[str] = []
    _prompt_for_slot(
        "X", style, input_fn=lambda p: "", output_fn=out.append, current=""
    )
    assert not any(":clear" in line for line in out)


@pytest.mark.parametrize("style,sep", [("bullet", "- "), ("prose", "")])
def test_prompt_for_slot_clear_after_content_is_literal(style, sep):
    # LOCK the first-line-only invariant for :clear (mirrors :back): a :clear
    # typed AFTER content has begun is literal text, NOT a clear command. Pins
    # the inherited-not-locked behavior the apparatus flagged.
    feed = iter(["alpha", ":clear", ""])
    result = _prompt_for_slot(
        style, style, input_fn=lambda p: next(feed), output_fn=lambda s: None,
        current="",
    )
    assert result == f"{sep}alpha\n{sep}:clear"


def test_prompt_for_slot_clear_hint_absent_on_first_visit():
    # No current value → no current-value block → no :clear hint (nothing to clear).
    out: list[str] = []
    _prompt_for_slot(
        "NAME", "line", input_fn=lambda p: "v", output_fn=out.append, current=""
    )
    assert not any(":clear" in line for line in out)


def test_prompt_for_slot_line_prompt_string_unchanged_by_clear():
    # The clear affordance must NOT alter the line prompt string (back-compat).
    captured: list[str] = []
    _prompt_for_slot(
        "X", "line", input_fn=lambda p: (captured.append(p) or "v"),
        output_fn=lambda s: None, current="",
    )
    assert captured == ["  X: "]


def test_conduct_interview_back_then_clear_empties_prior_answer(tmp_path: Path):
    # NAME="Alpha"; at CITY :back; at the NAME revisit type :clear → NAME="";
    # then CITY="Columbus". The integration peer of the back-then-blank-keeps test.
    spec = _two_section_spec(tmp_path)
    feed = iter(["Alpha", ":back", ":clear", "Columbus"])
    answers = conduct_interview(
        [spec], input_fn=lambda p: next(feed), output_fn=lambda s: None
    )
    assert answers == {"NAME": "", "CITY": "Columbus"}


# ---------- conduct_interview: back-navigation (item 2) ----------

def _two_section_spec(tmp_path: Path):
    template = tmp_path / "twosec.md"
    template.write_text(
        "# Test\n\n"
        "## First\n\n<!-- interview: their name -->\n\n{{NAME}}\n\n"
        "## Second\n\n<!-- interview: their city -->\n\n{{CITY}}\n",
        encoding="utf-8",
    )
    return parse_template(template)


def test_conduct_interview_back_revises_prior_answer(tmp_path: Path):
    spec = _two_section_spec(tmp_path)
    # NAME="Alpha"; at CITY type :back; re-answer NAME="Beta"; CITY="Columbus".
    feed = iter(["Alpha", ":back", "Beta", "Columbus"])
    answers = conduct_interview(
        [spec], input_fn=lambda p: next(feed), output_fn=lambda s: None
    )
    assert answers == {"NAME": "Beta", "CITY": "Columbus"}


def test_conduct_interview_back_at_first_question_is_noop(tmp_path: Path):
    spec = _two_section_spec(tmp_path)
    # :back at the very first prompt does nothing; then answer through.
    feed = iter([":back", "Alpha", "Columbus"])
    answers = conduct_interview(
        [spec], input_fn=lambda p: next(feed), output_fn=lambda s: None
    )
    assert answers == {"NAME": "Alpha", "CITY": "Columbus"}


def test_conduct_interview_back_then_blank_keeps_prior_answer(tmp_path: Path):
    spec = _two_section_spec(tmp_path)
    # NAME="Alpha"; at CITY :back; at NAME revisit a blank keeps Alpha; CITY.
    feed = iter(["Alpha", ":back", "", "Columbus"])
    answers = conduct_interview(
        [spec], input_fn=lambda p: next(feed), output_fn=lambda s: None
    )
    assert answers == {"NAME": "Alpha", "CITY": "Columbus"}


def _multislot_spec(tmp_path: Path):
    template = tmp_path / "multislot.md"
    template.write_text(
        "# T\n\n## Identity\n\n<!-- interview: name; city -->\n\n{{NAME}} {{CITY}}\n",
        encoding="utf-8",
    )
    return parse_template(template)


def test_conduct_interview_forward_multislot_prints_no_revising_header(tmp_path: Path):
    # Apparatus catch (L2 MED-1): walking FORWARD through slots 2..N of a
    # multi-slot section must NOT print "(revising)" — that header is for
    # genuine back-navigation re-entry only.
    spec = _multislot_spec(tmp_path)
    out: list[str] = []
    feed = iter(["Alpha", "Columbus"])
    answers = conduct_interview([spec], input_fn=lambda p: next(feed), output_fn=out.append)
    assert answers == {"NAME": "Alpha", "CITY": "Columbus"}
    assert not any("revising" in line for line in out)
    # The section header prints exactly once, on the first slot.
    assert sum(1 for line in out if line.strip() == "## Identity") == 1


def test_conduct_interview_back_into_multislot_prints_revising_header(tmp_path: Path):
    # The flip side: a real back-nav revisit DOES re-orient with "(revising)".
    spec = _multislot_spec(tmp_path)
    out: list[str] = []
    feed = iter(["Alpha", ":back", "Beta", "Columbus"])
    answers = conduct_interview([spec], input_fn=lambda p: next(feed), output_fn=out.append)
    assert answers == {"NAME": "Beta", "CITY": "Columbus"}
    assert any("revising" in line for line in out)


def test_prompt_for_slot_prose_eof_on_empty_revisit_keeps_current():
    # Apparatus catch (L1 MED-1): EOF (Ctrl-D) on an empty field must keep the
    # prior answer on a revisit, consistent with the blank=leave-as-is rule.
    def boom(_p: str) -> str:
        raise EOFError

    result = _prompt_for_slot(
        "BIO", "prose", input_fn=boom, output_fn=lambda s: None, current="prior bio"
    )
    assert result == "prior bio"


def test_prompt_for_slot_bullet_eof_on_empty_revisit_keeps_current():
    def boom(_p: str) -> str:
        raise EOFError

    result = _prompt_for_slot(
        "ITEMS", "bullet", input_fn=boom, output_fn=lambda s: None, current="- a\n- b"
    )
    assert result == "- a\n- b"


def test_conduct_interview_back_at_optional_skip_prompt(tmp_path: Path):
    # Apparatus catch (L3/codex MED): `:back` must work AT the optional
    # "Skip this section?" prompt, not corrupt the following answers. This is
    # codex's exact failing sequence; without the fix the answers misalign to
    # {A:Alpha, B:Beta, C:"y"}.
    template = tmp_path / "opt.md"
    template.write_text(
        "# T\n\n"
        "## First\n\n<!-- interview: a -->\n\n{{A}}\n\n"
        "## Maybe\n\n<!-- optional -->\n<!-- interview: b -->\n\n{{B}}\n\n"
        "## Third\n\n<!-- interview: c -->\n\n{{C}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)
    # A="Alpha"; at the Maybe skip-prompt type :back; revise A="Beta";
    # now skip Maybe ("y"); answer C="Columbus".
    feed = iter(["Alpha", ":back", "Beta", "y", "Columbus"])
    answers = conduct_interview(
        [spec], input_fn=lambda p: next(feed), output_fn=lambda s: None
    )
    assert answers == {"A": "Beta", "B": "", "C": "Columbus"}


def test_conduct_interview_return_to_bailed_field_restores_section_context(tmp_path: Path):
    # Apparatus catch (L3/codex LOW): a field bailed out of with :back before
    # answering is "visited"; returning to it (even via forward advance after
    # revising an earlier field) must restore its section header context.
    spec = _two_section_spec(tmp_path)  # First {{NAME}}, Second {{CITY}}
    out: list[str] = []
    # NAME="a1"; at CITY type :back (bail before answering); revise NAME="a2";
    # forward to CITY="b1".
    feed = iter(["a1", ":back", "a2", "b1"])
    answers = conduct_interview([spec], input_fn=lambda p: next(feed), output_fn=out.append)
    assert answers == {"NAME": "a2", "CITY": "b1"}
    second_headers = [line for line in out if line.strip().startswith("## Second")]
    assert len(second_headers) >= 2  # first arrival + the re-orient on return
