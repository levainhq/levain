"""Tests for `doctor`'s seed-content sanity check (`_check_seed_content`).

The interview-desync class Chip's Jul-1 rehearsal caught — a seed file rendered
without the operator's identity while wiring/layout stay green — needs a CONTENT
check, not just a wiring one. This locks that check.
"""

from __future__ import annotations

from pathlib import Path

from levain.doctor import _check_seed_content


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
