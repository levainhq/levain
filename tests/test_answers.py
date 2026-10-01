"""Tests for `levain.answers` — the non-interactive answer file.

The class under test is SILENT CORRUPTION: an answer file that is accepted,
renders cleanly, leaves no placeholder behind, and produces an entity that does
not know its operator. Every test here pins a way that used to be able to happen
(or a way a future edit could make it happen again).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from levain.answers import (
    SHAPE_IMPOSSIBLE,
    SHAPE_UNUSUAL,
    AnswersError,
    answers_template_json,
    field_guide,
    is_required,
    load_answers_file,
    shape_violations,
    validate_answers,
)


@dataclass
class F:
    """A stand-in for `interview.InterviewField` (the module reads it structurally)."""

    slot: str
    style: str = "line"
    optional: bool = False
    guidance: str = ""
    section_guidance: str = ""
    section_title: str = "Identity"
    spec_name: str = "world.md"
    first_in_section: bool = False


PLAN = [
    F("OPERATOR_NAME", first_in_section=True, section_guidance="Who you are"),
    F("AGE", style="optional-line"),
    F("BIO", style="prose", section_title="History", optional=True),
    F("ENTITY_NAME", spec_name="origin.md"),
]
FULL = {"OPERATOR_NAME": "Chris", "AGE": "", "BIO": "", "ENTITY_NAME": "Ada"}


# --- load ------------------------------------------------------------------


def test_loads_a_slot_keyed_object(tmp_path: Path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"OPERATOR_NAME": "Chris"}), encoding="utf-8")
    assert load_answers_file(p) == {"OPERATOR_NAME": "Chris"}


def test_rejects_a_json_list_and_says_why(tmp_path: Path):
    # The ordered form IS the original bug — a list must never be silently
    # zipped onto the field plan by position.
    p = tmp_path / "a.json"
    p.write_text(json.dumps(["Chris", "Ada"]), encoding="utf-8")
    with pytest.raises(AnswersError) as e:
        load_answers_file(p)
    assert "keyed by slot name" in str(e.value)
    assert "never by position" in str(e.value)


def test_rejects_non_string_values(tmp_path: Path):
    # A JSON null/number would be substituted into the seed as a Python repr.
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"OPERATOR_NAME": None, "AGE": 46}), encoding="utf-8")
    with pytest.raises(AnswersError) as e:
        load_answers_file(p)
    assert "OPERATOR_NAME" in str(e.value) and "AGE" in str(e.value)


def test_rejects_invalid_json_and_missing_file(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(AnswersError):
        load_answers_file(bad)
    with pytest.raises(AnswersError):
        load_answers_file(tmp_path / "nope.json")


# --- validate --------------------------------------------------------------


def test_a_complete_file_validates_clean():
    assert validate_answers(PLAN, FULL) == []


def test_unknown_slot_is_an_error_not_ignored():
    # A silently-ignored typo leaves the REAL slot unfilled — the whole defect.
    bad = dict(FULL)
    bad["OPERATOR_NMAE"] = bad.pop("OPERATOR_NAME")
    errs = validate_answers(PLAN, bad)
    assert any("OPERATOR_NMAE" in e and "unknown" in e for e in errs)


def test_missing_slot_is_an_error_not_blank_filled():
    bad = {k: v for k, v in FULL.items() if k != "ENTITY_NAME"}
    errs = validate_answers(PLAN, bad)
    assert any("ENTITY_NAME" in e and "missing" in e for e in errs)


def test_empty_identity_slot_is_an_error():
    errs = validate_answers(PLAN, {**FULL, "ENTITY_NAME": "   "})
    assert any("ENTITY_NAME" in e and "empty" in e for e in errs)


def test_a_blank_non_identity_field_is_allowed():
    """The cross-surface coherence fix (L3: codex read init_server, glm read doctor).

    The terminal interview and the web form BOTH accept a blank answer for a
    non-optional field. A provisioning file held to a stricter rule than its own
    siblings would reject configurations those siblings create happily — so blank is
    legal everywhere except identity, where absence is a different kind of failure.
    """
    plan = PLAN + [F("COGNITION", style="prose", section_title="How You Think")]
    assert validate_answers(plan, {**FULL, "COGNITION": ""}) == []


def test_empty_is_allowed_for_optional_section_and_optional_line():
    # "" on an optional-section slot is the file-shaped equivalent of answering
    # `y` to the terminal's "Skip this section?".
    assert validate_answers(PLAN, {**FULL, "AGE": "", "BIO": ""}) == []


def test_reports_every_problem_at_once():
    # A provisioning surface that reveals complaints one at a time is one nobody
    # can script against.
    bad = {"OPERATOR_NMAE": "Chris", "AGE": "", "BIO": "", "ENTITY_NAME": ""}
    errs = validate_answers(PLAN, bad)
    assert len(errs) == 3  # unknown + missing + empty-identity


def test_is_required_honors_both_ways_of_being_optional():
    assert is_required(F("X"))
    assert not is_required(F("X", style="optional-line"))
    assert not is_required(F("X", optional=True))


# --- shape -----------------------------------------------------------------


def test_multiline_value_in_a_line_slot_is_impossible_not_merely_unusual():
    # The desync signature: a `line` slot is prompted with one input() and stored
    # stripped, so it CANNOT hold a newline unless the value came from elsewhere.
    out = shape_violations(PLAN, {**FULL, "OPERATOR_NAME": "line one\nline two"})
    assert [sev for sev, _ in out] == [SHAPE_IMPOSSIBLE]
    assert "OPERATOR_NAME" in out[0][1]


def test_overlong_line_is_only_unusual():
    # Legal but atypical — a heuristic may raise its hand, never condemn.
    out = shape_violations(PLAN, {**FULL, "OPERATOR_NAME": "x" * 500})
    assert [sev for sev, _ in out] == [SHAPE_UNUSUAL]


def test_multiline_prose_and_bullets_are_never_flagged():
    # The asymmetry is the design: only the single-line styles carry signal.
    plan = [F("BIO", style="prose"), F("ROLES", style="bullet")]
    assert shape_violations(plan, {"BIO": "a\nb\nc", "ROLES": "- a\n- b"}) == []


def test_short_answer_in_a_prose_slot_is_not_flagged():
    assert shape_violations([F("BIO", style="prose")], {"BIO": "Terse."}) == []


# --- template + guide ------------------------------------------------------


def test_template_is_valid_json_covering_every_slot_in_plan_order():
    text = answers_template_json(PLAN)
    data = json.loads(text)
    assert list(data) == ["OPERATOR_NAME", "AGE", "BIO", "ENTITY_NAME"]
    assert set(data.values()) == {""}


def test_the_emitted_template_validates_against_its_own_plan_once_filled():
    # The round trip that makes the surface usable: skeleton -> fill -> accepted.
    data = json.loads(answers_template_json(PLAN))
    data.update({"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada"})
    assert validate_answers(PLAN, data) == []


def test_guide_names_every_slot_and_marks_only_the_identity_ones_required():
    g = field_guide(PLAN)
    for slot in ("OPERATOR_NAME", "AGE", "BIO", "ENTITY_NAME"):
        assert slot in g
    # Diogenes 2026-09-18: the guide must agree with the validator and the help —
    # blank is accepted everywhere except the identity slots.
    assert sum(l.endswith("[required]") for l in g.splitlines()) == 2
    for line in g.splitlines():
        if line.strip().startswith(("OPERATOR_NAME", "ENTITY_NAME")):
            assert line.endswith("[required]")
        elif line.startswith("    ") and "(" in line and ")" in line:
            assert not line.endswith("[required]")
    assert "BY SLOT NAME" in g


def test_guide_header_names_only_identity_slots_the_plan_has():
    g = field_guide([F("AGE", style="optional-line"), F("ENTITY_NAME")])
    assert "except ENTITY_NAME (marked" in g
    assert "OPERATOR_NAME" not in g
    assert "except" not in field_guide([F("AGE", style="optional-line")])


def test_guide_does_not_say_skip_for_an_optional_section_holding_an_identity_slot():
    # The validator refuses a blank identity slot even inside an optional section.
    g = field_guide(
        [F("OPERATOR_NAME", first_in_section=True, section_title="Who", optional=True)]
    )
    assert "optional section" not in g
    assert "OPERATOR_NAME  (line)  [required]" in g


def test_missing_slot_error_does_not_imply_only_optional_slots_can_be_blank():
    errs = validate_answers(PLAN, {"OPERATOR_NAME": "C", "ENTITY_NAME": "A"})
    assert any("missing slot" in e and "except OPERATOR_NAME" in e for e in errs)


def test_impossible_shape_is_a_validation_ERROR_not_just_a_note():
    """The gate must be the gate (L3/glm, medium).

    `--answers` provisions entities with nobody watching, so a fleet reads exit 0 as
    "provisioned". A corruption signal that only WARNED at init and FAILED later at
    `doctor` would hand the fleet a green light and the operator a broken seat.
    """
    errs = validate_answers(PLAN, {**FULL, "OPERATOR_NAME": "Chris\nand a bio"})
    assert any("OPERATOR_NAME" in e and "wrong slot" in e for e in errs)


def test_unusual_shape_stays_a_warning_and_never_rejects_a_file():
    # The asymmetry survives: a long-but-legal answer is somebody's real answer,
    # and a heuristic does not get to overrule them about it.
    assert validate_answers(PLAN, {**FULL, "OPERATOR_NAME": "x" * 500}) == []


# ---------------------------------------------------------------------------
# extract_answers (spore-423): read the interview answers back out of a render.
# ---------------------------------------------------------------------------
import random as _random  # noqa: E402

from levain.answers import AdoptRefused, extract_answers  # noqa: E402
from levain.interview import parse_template, render_template  # noqa: E402

_SEED = Path(__file__).resolve().parent.parent / "levain" / "templates" / "seed"


def _case(name: str):
    spec = parse_template(_SEED / name)
    slots = list(dict.fromkeys(s for sec in spec.sections for s in sec.slots))
    optional = [sec.slots for sec in spec.sections if sec.title and sec.optional]
    return spec, slots, optional, (lambda a: render_template(spec, a))


@pytest.mark.parametrize("name", ["world.md", "origin.md"])
def test_any_answer_set_reads_back_exactly(name):
    spec, slots, optional, render = _case(name)
    rnd = _random.Random(7)
    words = ["Chris", "Riverton", "Plays first.", "- Builder\n- Maker", "Baking, poker",
             "Line one.\nLine two.", "a tea shop"]
    for _ in range(60):
        a = {s: (rnd.choice(words) if rnd.random() < 0.6 else "") for s in slots}
        for s in ("OPERATOR_NAME", "ENTITY_NAME"):
            if s in a:
                a[s] = rnd.choice(["Chris", "Ada"])
        if a.get("AGE"):
            a["AGE"] = str(rnd.randint(20, 80))
        assert extract_answers(render(a), slots=slots, optional_sections=optional,
                               render=render) == a


def test_a_filled_age_is_not_read_as_part_of_the_name():
    # "Chris. 46. Riverton." also renders from a name of "Chris. 46" and a blank AGE.
    spec, slots, optional, render = _case("world.md")
    a = {s: "" for s in slots}
    a.update(OPERATOR_NAME="Chris", AGE="46", LOCATION="Riverton")
    got = extract_answers(render(a), slots=slots, optional_sections=optional, render=render)
    assert (got["OPERATOR_NAME"], got["AGE"]) == ("Chris", "46")


def test_a_value_holding_a_later_heading_is_read_whole():
    spec, slots, optional, render = _case("world.md")
    a = {s: "" for s in slots}
    a.update(OPERATOR_NAME="Chris", INTERESTS="Baking", WORK="Levain",
             COGNITION="Plays first.\n\n## Work & Projects\nnot a heading")
    got = extract_answers(render(a), slots=slots, optional_sections=optional, render=render)
    assert got == a


def test_an_edit_outside_the_fields_is_refused():
    spec, slots, optional, render = _case("world.md")
    a = {s: "" for s in slots}
    a.update(OPERATOR_NAME="Chris")
    text = render(a).replace("## ", "## Renamed ", 1)
    with pytest.raises(AdoptRefused, match="outside the interview's fields"):
        extract_answers(text, slots=slots, optional_sections=optional, render=render)


def test_a_genuine_tie_is_refused(tmp_path):
    tpl = tmp_path / "t.md"
    tpl.write_text("# T\n\n{{A}} {{B}}\n", encoding="utf-8")
    spec = parse_template(tpl)
    render = (lambda a: render_template(spec, a))  # noqa: E731
    # "x y z" is A="x", B="y z" or A="x y", B="z": the same text, nothing to anchor on.
    with pytest.raises(AdoptRefused, match="nothing but whitespace"):
        extract_answers(render({"A": "x y", "B": "z"}), slots=["A", "B"],
                        optional_sections=[], render=render)


def test_too_many_optional_sections_are_refused_not_searched():
    with pytest.raises(AdoptRefused, match="not bounded"):
        extract_answers("x\n", slots=["A"], optional_sections=[["A"]] * 13,
                        render=lambda a: "x\n")


def test_two_readings_of_equal_weight_are_refused(tmp_path):
    tpl = tmp_path / "t.md"
    tpl.write_text("# T\n\n{{A}}-{{B}}\n", encoding="utf-8")
    spec = parse_template(tpl)
    render = (lambda a: render_template(spec, a))  # noqa: E731
    # "x-y-z": A="x", B="y-z" (first "-") or A="x-y", B="z" (last "-"), same weight.
    with pytest.raises(AdoptRefused, match="more than one answer set"):
        extract_answers(render({"A": "x-y", "B": "z"}), slots=["A", "B"],
                        optional_sections=[], render=render)


def test_an_optional_section_emptied_but_left_in_place_is_refused():
    # The "kept" skeleton matches with a blank value, but a blank value makes render DROP
    # the section, so that record would not reproduce the seed: only the round trip sees it.
    spec, slots, optional, render = _case("world.md")
    a = {s: "" for s in slots}
    a.update(OPERATOR_NAME="Chris", PERSONAL_HISTORY="Grew up by the sea.")
    text = render(a).replace("Grew up by the sea.", "")
    assert "## Personal History" in text
    with pytest.raises(AdoptRefused):
        extract_answers(text, slots=slots, optional_sections=optional, render=render)


def test_a_side_by_side_pair_in_a_dropped_section_does_not_abort(tmp_path):
    # complement L3: the whitespace refusal from the all-filled skeleton escaped before
    # the skeleton with that section dropped could prove the seed.
    tpl = tmp_path / "t.md"
    tpl.write_text("# T {{A}}\n\n## Extra\n<!-- optional: extra -->\n{{B}} {{C}}\n",
                   encoding="utf-8")
    spec = parse_template(tpl)
    render = (lambda a: render_template(spec, a))  # noqa: E731
    optional = [sec.slots for sec in spec.sections if sec.title and sec.optional]
    got = extract_answers(render({"A": "x", "B": "", "C": ""}), slots=["A", "B", "C"],
                          optional_sections=optional, render=render)
    assert got == {"A": "x", "B": "", "C": ""}
