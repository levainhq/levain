"""Tests for `doctor`'s seed-content sanity check (`_check_seed_content`).

The interview-desync class Chip's Jul-1 rehearsal caught — a seed file rendered
without the operator's identity while wiring/layout stay green — needs a CONTENT
check, not just a wiring one. This locks that check.
"""

from __future__ import annotations

import json
from pathlib import Path

from levain.doctor import _check_recorded_answers, _check_seed_content


def _seed(install: Path, files: dict[str, str]) -> None:
    seed = install / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (seed / name).write_text(content, encoding="utf-8")


def test_passes_when_render_targets_are_filled(tmp_path: Path):
    _seed(tmp_path, {"world.md": "# Me\nChris. 40. Ohio.\n", "origin.md": "You are Ada.\n"})
    results = _check_seed_content(tmp_path)
    assert [r.name for r in results] == ["seed content (world.md)", "seed content (origin.md)"]
    assert all(r.ok for r in results)


def test_fails_and_names_the_unfilled_slot(tmp_path: Path):
    # The exact failure mode: a template copied but never rendered.
    _seed(tmp_path, {"world.md": "# Me\n{{OPERATOR_NAME}}. {{AGE}}.\n", "origin.md": "You are Ada.\n"})
    results = _check_seed_content(tmp_path)
    world = next(r for r in results if r.name == "seed content (world.md)")
    assert not world.ok
    assert "{{OPERATOR_NAME}}" in world.detail
    assert "{{AGE}}" in world.detail


def test_ignores_code_formatted_documentation_placeholder(tmp_path: Path):
    # A rendered file may legitimately KEEP a code-formatted `{{X}}` as doc
    # (render preserves it) — that is not an unfilled slot.
    _seed(tmp_path, {"world.md": "Chris. See the `{{SLOTS}}` note.\n", "origin.md": "Ada.\n"})
    results = _check_seed_content(tmp_path)
    assert all(r.ok for r in results)


def test_skips_missing_files(tmp_path: Path):
    # A missing required seed is the layout check's job — content check stays silent.
    _seed(tmp_path, {"world.md": "Chris.\n"})  # no origin.md
    results = _check_seed_content(tmp_path)
    assert [r.name for r in results] == ["seed content (world.md)"]


def test_binary_seed_fails_gracefully_not_crash(tmp_path: Path):
    # A binary / invalid-UTF-8 seed is corrupt install, not a doctor crash —
    # read_text raises UnicodeDecodeError (a UnicodeError, not OSError).
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    (seed / "world.md").write_bytes(b"\xff{{OPERATOR_NAME}}\n")
    (seed / "origin.md").write_text("Ada\n", encoding="utf-8")
    results = _check_seed_content(tmp_path)  # must not raise
    world = next(r for r in results if r.name == "seed content (world.md)")
    assert not world.ok
    assert "unreadable" in world.detail


def test_does_not_check_verbatim_seeds(tmp_path: Path):
    # continuity.md / README.md are verbatim and carry `{{...}}` docs — they must
    # NOT be content-checked (only the render targets are).
    _seed(
        tmp_path,
        {
            "world.md": "Chris.\n",
            "origin.md": "Ada.\n",
            "continuity.md": "# Continuity — {{ENTITY_NAME}}\n",
            "README.md": "onboarding fills `{{SLOTS}}`\n",
        },
    )
    checked = {r.name for r in _check_seed_content(tmp_path)}
    assert checked == {"seed content (world.md)", "seed content (origin.md)"}


# --- `_check_recorded_answers` — the CONTENT half (K2) ----------------------
#
# `_check_seed_content` above proves the render RAN (no `{{SLOT}}` survived). It
# cannot see whether the render ran over the RIGHT values, so a seed whose answers
# landed one slot off passes every wiring check and `doctor` prints green over an
# entity that does not know its operator. These lock the check that sees it.

def _install(tmp_path: Path, answers: dict[str, str], seeds: dict[str, str] | None = None):
    """An install with a recorded interview. By default the seed files are rendered
    HONESTLY from the answers (each value present), so a test only has to introduce
    the one divergence it is about."""
    (tmp_path / ".levain").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".levain" / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    if seeds is None:
        world = "\n".join(v for k, v in answers.items() if k != "ENTITY_NAME" and v)
        origin = answers.get("ENTITY_NAME", "")
        seeds = {"world.md": world + "\n", "origin.md": origin + "\n"}
    _seed(tmp_path, seeds)
    return tmp_path


def _by(results, needle):
    return next(r for r in results if needle in r.name)


def test_absent_record_fails_rather_than_passing_quietly(tmp_path: Path):
    # "Cannot verify" must never render as a green tick — that is the exact habit
    # (absence of signal shown as health) this check exists to break.
    _seed(tmp_path, {"world.md": "Chris\n", "origin.md": "Ada\n"})
    results = _check_recorded_answers(tmp_path)
    assert len(results) == 1 and not results[0].ok
    assert "cannot be verified" in results[0].detail


def test_healthy_install_passes_every_content_check(tmp_path: Path):
    _install(tmp_path, {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada", "COGNITION": "Plays first."})
    assert all(r.ok for r in _check_recorded_answers(tmp_path))


def test_empty_identity_answer_fails(tmp_path: Path):
    # Renders to nothing and leaves NO placeholder, so it is invisible to the
    # unfilled-slot check.
    _install(tmp_path, {"OPERATOR_NAME": "", "ENTITY_NAME": "Ada"})
    r = _by(_check_recorded_answers(tmp_path), "answers present")
    assert not r.ok and "OPERATOR_NAME" in r.detail


def test_a_blank_non_identity_field_is_reported_but_not_failed(tmp_path: Path):
    # Both interactive surfaces let an operator leave this blank; doctor must not
    # condemn an install for a choice the product offered.
    _install(tmp_path, {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada", "COGNITION": ""})
    r = _by(_check_recorded_answers(tmp_path), "answers present")
    assert r.ok and "COGNITION" in r.detail


def test_a_record_sharing_no_fields_with_the_base_seed_cannot_be_verified(tmp_path: Path):
    """Caught at L3 by codex: this branch used to return a cheerful OK.

    A record holding only a typo'd key matched zero base slots, so the check
    reported "pack-layered install" and passed — reopening the very hole ("the
    record exists but proves nothing") it was written to close.
    """
    _install(tmp_path, {"OPERATOR_NMAE": "Chris"})
    results = _check_recorded_answers(tmp_path)
    assert len(results) == 1 and not results[0].ok
    assert "cannot be verified" in results[0].detail


def test_optional_line_may_be_empty(tmp_path: Path):
    # AGE is `optional-line` in the shipped world.md — empty is a real answer.
    _install(tmp_path, {"OPERATOR_NAME": "Chris", "AGE": "", "ENTITY_NAME": "Ada"})
    assert _by(_check_recorded_answers(tmp_path), "answers present").ok


def test_seed_edited_after_init_is_reported_but_never_failed(tmp_path: Path):
    """A hand-edited seed is REPORTED, not condemned — and the reason is structural.

    The install banner says "Files created (you can hand-edit any of these)" and
    lists the seed. Failing doctor for accepting that invitation would be a trap
    with no exit: the offered remedy (`levain update`) "fixes" the failure by
    re-rendering from the record, i.e. by destroying the edit.

    It is also not corruption. THE ENTITY READS THE SEED — an edited seed is the
    operator's intent, correctly delivered; the record is only the re-render input.
    So the only real consequence is forward-looking, and the check says so.
    """
    _install(
        tmp_path,
        {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada"},
        seeds={"world.md": "Chris\n", "origin.md": "You are Rebranded.\n"},
    )
    r = _by(_check_recorded_answers(tmp_path), "seed matches record")
    assert r.ok
    assert "ENTITY_NAME" in r.detail
    assert "levain update" in r.detail  # names the overwrite risk


def test_a_hand_edited_seed_does_not_fail_the_install_overall(tmp_path: Path):
    # The property that matters to an operator: editing your own seed keeps doctor
    # green. Pinned separately so a future edit cannot quietly re-fail it via some
    # other check in this group.
    _install(
        tmp_path,
        {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada"},
        seeds={"world.md": "Christopher\n", "origin.md": "You are Ada.\n"},
    )
    assert all(r.ok for r in _check_recorded_answers(tmp_path))


def test_scrambled_seed_fails_while_every_older_check_still_passes(tmp_path: Path):
    """THE REGRESSION THAT NAMES THE WHOLE POINT.

    A multi-line answer in a one-line slot — what a positional answer file used to
    produce. The unfilled-placeholder check is GREEN on this install; without the
    content check, `doctor` reports a healthy entity that has a paragraph where its
    name should be.
    """
    scrambled = {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Plays first.\nHates grinding."}
    _install(tmp_path, scrambled)

    assert all(r.ok for r in _check_seed_content(tmp_path))  # the OLD check: green

    r = _by(_check_recorded_answers(tmp_path), "answer shapes")
    assert not r.ok and "ENTITY_NAME" in r.detail


def test_overlong_single_line_is_reported_but_does_not_condemn(tmp_path: Path):
    # A heuristic may raise its hand about an unusual answer; it may not fail an
    # install over one. Long-but-legal is somebody's real answer.
    _install(tmp_path, {"OPERATOR_NAME": "x" * 500, "ENTITY_NAME": "Ada"})
    r = _by(_check_recorded_answers(tmp_path), "answer shapes")
    assert r.ok and "note" in r.detail
