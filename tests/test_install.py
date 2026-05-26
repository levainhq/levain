"""Tests for the install module — the _templates_root() context manager
and the install-path safety helpers. Integration test for run_init lives
separate because it requires the full interview-driver dance.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from levain.install import (
    _checkpoint_path,
    _clear_checkpoint,
    _is_safe_install_target,
    _load_checkpoint,
    _save_checkpoint,
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


# ---------- interview checkpoint persistence ----------

def test_checkpoint_path_is_inside_dot_levain(tmp_path: Path):
    p = _checkpoint_path(tmp_path)
    assert p == tmp_path / ".levain" / "interview-checkpoint.json"


def test_save_and_load_checkpoint_round_trip(tmp_path: Path):
    answers = {"NAME": "Alex", "CITY": "Columbus"}
    _save_checkpoint(tmp_path, answers)
    loaded = _load_checkpoint(tmp_path)
    assert loaded == answers


def test_load_checkpoint_returns_none_when_absent(tmp_path: Path):
    assert _load_checkpoint(tmp_path) is None


def test_load_checkpoint_returns_none_on_corrupt_json(tmp_path: Path):
    target = _checkpoint_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not valid json {{{", encoding="utf-8")
    assert _load_checkpoint(tmp_path) is None


def test_load_checkpoint_returns_none_when_not_a_dict(tmp_path: Path):
    target = _checkpoint_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('["a", "list", "not a dict"]', encoding="utf-8")
    assert _load_checkpoint(tmp_path) is None


def test_load_checkpoint_filters_non_string_entries(tmp_path: Path):
    """A corrupted checkpoint with non-string values should be sanitized,
    not propagate type weirdness into the interview."""
    target = _checkpoint_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '{"NAME": "Alex", "NUMBER": 42, "NULL_VAL": null, "CITY": "Columbus"}',
        encoding="utf-8",
    )
    loaded = _load_checkpoint(tmp_path)
    assert loaded == {"NAME": "Alex", "CITY": "Columbus"}


def test_save_checkpoint_creates_parent_dir(tmp_path: Path):
    """`.levain/` doesn't exist yet at first checkpoint write."""
    install = tmp_path / "new-install"
    install.mkdir()
    _save_checkpoint(install, {"X": "y"})
    assert (install / ".levain" / "interview-checkpoint.json").is_file()


def test_save_checkpoint_atomic_via_temp_file(tmp_path: Path):
    """A second save should replace the first cleanly. No .tmp left behind."""
    _save_checkpoint(tmp_path, {"A": "1"})
    _save_checkpoint(tmp_path, {"A": "2", "B": "3"})
    loaded = _load_checkpoint(tmp_path)
    assert loaded == {"A": "2", "B": "3"}
    # No leftover .tmp
    tmps = list((tmp_path / ".levain").glob("*.tmp"))
    assert tmps == []


def test_clear_checkpoint_removes_existing(tmp_path: Path):
    _save_checkpoint(tmp_path, {"X": "y"})
    assert _load_checkpoint(tmp_path) is not None
    _clear_checkpoint(tmp_path)
    assert _load_checkpoint(tmp_path) is None


def test_clear_checkpoint_is_noop_when_absent(tmp_path: Path):
    # Should not raise on a missing checkpoint.
    _clear_checkpoint(tmp_path)


# ---------- conduct_interview + checkpoint integration ----------

def test_conduct_interview_calls_checkpoint_fn_after_each_section(tmp_path: Path):
    from levain.interview import conduct_interview, parse_template

    template = tmp_path / "test.md"
    template.write_text(
        "# Test\n\n"
        "## First\n\n<!-- interview: their name -->\n\n{{NAME}}\n\n"
        "## Second\n\n<!-- interview: their city -->\n\n{{CITY}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)

    checkpoints: list[dict[str, str]] = []
    answers = conduct_interview(
        [spec],
        input_fn=lambda prompt: "Alex" if "NAME" in prompt else "Columbus",
        output_fn=lambda s: None,
        checkpoint_fn=lambda a: checkpoints.append(dict(a)),
    )
    # Two sections → two checkpoint snapshots, cumulative.
    assert len(checkpoints) == 2
    assert checkpoints[0] == {"NAME": "Alex"}
    assert checkpoints[1] == {"NAME": "Alex", "CITY": "Columbus"}
    assert answers == {"NAME": "Alex", "CITY": "Columbus"}


def test_conduct_interview_with_initial_answers_skips_answered_slots(tmp_path: Path):
    """The resume contract: pre-load answers from checkpoint, only
    unasked slots should be prompted."""
    from levain.interview import conduct_interview, parse_template

    template = tmp_path / "resume.md"
    template.write_text(
        "# Resume\n\n"
        "## Identity\n\n<!-- interview: name; city -->\n\n{{NAME}} {{CITY}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)

    prompts: list[str] = []

    def driver(prompt: str) -> str:
        prompts.append(prompt)
        return "Columbus"

    answers = conduct_interview(
        [spec],
        answers={"NAME": "Alex"},  # already answered from "checkpoint"
        input_fn=driver,
        output_fn=lambda s: None,
    )
    # Should have prompted only for CITY (NAME was pre-loaded).
    assert answers == {"NAME": "Alex", "CITY": "Columbus"}
    # NAME prompt should not appear in the captured prompts.
    assert not any("NAME" in p for p in prompts)
    assert any("CITY" in p for p in prompts)


# ---------- doctor --invoke + verify-hooks improvements ----------

def test_resolve_install_python_falls_back_to_sys_executable_when_no_config(tmp_path: Path):
    from levain.verify import _resolve_install_python
    # An empty dir has no .claude/settings.json or AGENTS.md — fallback.
    assert _resolve_install_python(tmp_path) == sys.executable


def test_resolve_install_python_reads_claude_settings(tmp_path: Path):
    from levain.verify import _resolve_install_python
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": "/path/to/venv/bin/python /path/to/script.py",
                }],
            }],
        },
    }), encoding="utf-8")
    assert _resolve_install_python(tmp_path) == "/path/to/venv/bin/python"


def test_resolve_install_python_reads_codex_hooks(tmp_path: Path):
    from levain.verify import _resolve_install_python
    # AGENTS.md marks this as a Codex install.
    (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text(json.dumps({
        "session_start": [{"command": "/codex-venv/bin/python /script.py"}],
    }), encoding="utf-8")
    monkey_env = os.environ.copy()
    os.environ["CODEX_HOME"] = str(codex_home)
    try:
        assert _resolve_install_python(tmp_path) == "/codex-venv/bin/python"
    finally:
        os.environ.clear()
        os.environ.update(monkey_env)


def test_session_start_sources_constant_covers_all_four():
    from levain.verify import SESSION_START_SOURCES
    assert set(SESSION_START_SOURCES) == {"startup", "resume", "clear", "compact"}


def test_conduct_interview_checkpoint_fn_failure_does_not_break_interview(tmp_path: Path):
    """If the checkpoint write fails (full disk, permission, etc.) the
    interview must complete normally — best-effort persistence."""
    from levain.interview import conduct_interview, parse_template

    template = tmp_path / "robust.md"
    template.write_text(
        "# Robust\n\n## S\n\n<!-- interview: name -->\n\n{{NAME}}\n",
        encoding="utf-8",
    )
    spec = parse_template(template)

    def broken_checkpoint(_a):
        raise OSError("simulated disk full")

    answers = conduct_interview(
        [spec],
        input_fn=lambda prompt: "Alex",
        output_fn=lambda s: None,
        checkpoint_fn=broken_checkpoint,
    )
    # Interview completed cleanly despite checkpoint failures.
    assert answers == {"NAME": "Alex"}
