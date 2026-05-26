"""Tests for the install module — the _templates_root() context manager
and the install-path safety helpers. Integration test for run_init lives
separate because it requires the full interview-driver dance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from levain.install import (
    _is_safe_install_target,
    _substitute_hook_placeholders,
    _templates_root,
)


# ---------- _templates_root context manager ----------

def test_templates_root_yields_existing_directory():
    with _templates_root() as p:
        assert isinstance(p, Path)
        assert p.exists()
        assert p.is_dir()


def test_templates_root_contains_seed_subdir():
    with _templates_root() as p:
        assert (p / "seed").is_dir()
        assert (p / "seed" / "world.md").is_file()
        assert (p / "seed" / "origin.md").is_file()


def test_templates_root_contains_adapter_subdirs():
    with _templates_root() as p:
        assert (p / "adapters" / "claude-code").is_dir()
        assert (p / "adapters" / "codex").is_dir()
        assert (p / "adapters" / "claude-code" / "settings.template.json").is_file()
        assert (p / "adapters" / "codex" / "hooks.json.template").is_file()


def test_templates_root_contains_activation_subdir():
    with _templates_root() as p:
        assert (p / "activation").is_dir()
        assert (p / "activation" / "hooks" / "session_start.py").is_file()
        assert (p / "activation" / "hooks" / "user_prompt_submit.py").is_file()
        assert (p / "activation" / "hooks" / "_levain_hook.py").is_file()


def test_templates_root_reentry_is_safe():
    """Multiple sequential `with` blocks should each yield a valid path."""
    paths = []
    for _ in range(3):
        with _templates_root() as p:
            assert (p / "seed" / "world.md").is_file()
            paths.append(p)
    # All three invocations yielded the same package path on filesystem
    # installs (no copy). Under zipped distributions they'd be different
    # tempdirs — both shapes are correct, we just verify each one worked.
    assert all(p.is_dir() or not p.exists() for p in paths)


# ---------- _is_safe_install_target ----------

def test_is_safe_install_target_nonexistent_path(tmp_path: Path):
    target = tmp_path / "does-not-exist"
    assert _is_safe_install_target(target) is True


def test_is_safe_install_target_empty_dir(tmp_path: Path):
    target = tmp_path / "empty"
    target.mkdir()
    assert _is_safe_install_target(target) is True


def test_is_safe_install_target_non_empty_dir(tmp_path: Path):
    target = tmp_path / "has-stuff"
    target.mkdir()
    (target / "existing.txt").write_text("hello")
    assert _is_safe_install_target(target) is False


def test_is_safe_install_target_existing_file_returns_false(tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("hello")
    assert _is_safe_install_target(target) is False


# ---------- _substitute_hook_placeholders ----------

def test_substitute_hook_placeholders_replaces_in_py_files(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    target = hooks_dir / "example.py"
    target.write_text(
        "ANNEAL = '{{ANNEAL_MEMORY}}'\nPYTHON = '{{PYTHON}}'\n",
        encoding="utf-8",
    )
    _substitute_hook_placeholders(
        hooks_dir,
        {"{{ANNEAL_MEMORY}}": "/usr/local/bin/anneal-memory"},
    )
    text = target.read_text(encoding="utf-8")
    assert "/usr/local/bin/anneal-memory" in text
    assert "{{ANNEAL_MEMORY}}" not in text
    # Unmapped placeholders left alone
    assert "{{PYTHON}}" in text


def test_substitute_hook_placeholders_skips_non_py_files(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    md = hooks_dir / "README.md"
    md.write_text("`{{ANNEAL_MEMORY}}` is documentation here\n", encoding="utf-8")
    _substitute_hook_placeholders(
        hooks_dir,
        {"{{ANNEAL_MEMORY}}": "/path/to/anneal-memory"},
    )
    # README.md untouched — only *.py files get the substitution
    assert "{{ANNEAL_MEMORY}}" in md.read_text(encoding="utf-8")


def test_substitute_hook_placeholders_missing_dir_is_noop(tmp_path: Path):
    # Should not raise on a nonexistent directory.
    _substitute_hook_placeholders(
        tmp_path / "does-not-exist",
        {"{{ANNEAL_MEMORY}}": "x"},
    )


def test_substitute_hook_placeholders_handles_no_placeholder(tmp_path: Path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    target = hooks_dir / "plain.py"
    target.write_text("print('no placeholder here')\n", encoding="utf-8")
    _substitute_hook_placeholders(
        hooks_dir,
        {"{{ANNEAL_MEMORY}}": "/anywhere"},
    )
    # File should be unchanged (no rewrite when content didn't change)
    assert target.read_text(encoding="utf-8") == "print('no placeholder here')\n"
