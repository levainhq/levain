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
    _init_store,
    _is_safe_install_target,
    _load_checkpoint,
    _print_manifest,
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


def _seed_install_hooks(install: Path) -> Path:
    """Helper for resolver tests — write a dummy session_start.py inside
    install/activation/hooks/ so the anti-foreign-hook filter accepts
    commands pointing at it."""
    hooks_dir = install / "activation" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "session_start.py"
    target.write_text("# dummy\n", encoding="utf-8")
    return target


def test_resolve_install_python_reads_claude_settings(tmp_path: Path):
    from levain.verify import _resolve_install_python
    # Tag file CLAUDE.md must exist for the Claude Code branch to fire.
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    script = _seed_install_hooks(tmp_path)
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": f"/path/to/venv/bin/python {script}",
                }],
            }],
        },
    }), encoding="utf-8")
    assert _resolve_install_python(tmp_path) == "/path/to/venv/bin/python"


def test_resolve_install_python_reads_codex_hooks(tmp_path: Path):
    from levain.verify import _resolve_install_python
    # AGENTS.md marks this as a Codex install.
    (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
    script = _seed_install_hooks(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    # Codex hooks.json shape matches Claude Code's settings.json — both use
    # the nested {"hooks": {<event>: [{"hooks": [{"command": "..."}]}]}} form.
    (codex_home / "hooks.json").write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": f"/codex-venv/bin/python {script}",
                }],
            }],
        },
    }), encoding="utf-8")
    monkey_env = os.environ.copy()
    os.environ["CODEX_HOME"] = str(codex_home)
    try:
        assert _resolve_install_python(tmp_path) == "/codex-venv/bin/python"
    finally:
        os.environ.clear()
        os.environ.update(monkey_env)


def test_resolve_install_python_prefers_claude_over_codex_when_both_present(tmp_path: Path):
    """Mixed-install regression test — operators with both adapters wired
    should get Claude Code's python (interactive harness takes precedence)."""
    from levain.verify import _resolve_install_python
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
    script = _seed_install_hooks(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": f"/claude/python {script}",
        }]}]},
    }), encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": f"/codex/python {script}",
        }]}]},
    }), encoding="utf-8")
    monkey_env = os.environ.copy()
    os.environ["CODEX_HOME"] = str(codex_home)
    try:
        assert _resolve_install_python(tmp_path) == "/claude/python"
    finally:
        os.environ.clear()
        os.environ.update(monkey_env)


def test_resolve_install_python_skips_foreign_hooks(tmp_path: Path):
    """Anti-foreign-hook: a hook command targeting a script OUTSIDE the
    install's hooks dir must NOT contribute its interpreter to verify-hooks."""
    from levain.verify import _resolve_install_python
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    script = _seed_install_hooks(tmp_path)
    foreign = tmp_path / "foreign.py"
    foreign.write_text("# someone else's hook\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                # Foreign hook first — should be SKIPPED.
                {"hooks": [{
                    "type": "command",
                    "command": f"/foreign/python {foreign}",
                }]},
                # Then Levain hook — should be PICKED.
                {"hooks": [{
                    "type": "command",
                    "command": f"/levain/python {script}",
                }]},
            ],
        },
    }), encoding="utf-8")
    assert _resolve_install_python(tmp_path) == "/levain/python"


def test_session_start_sources_constant_covers_all_four():
    from levain.verify import SESSION_START_SOURCES
    assert set(SESSION_START_SOURCES) == {"startup", "resume", "clear", "compact"}


def test_session_start_representatives_cover_both_equivalence_classes():
    """The hook branches on (startup,clear) vs (resume,compact). The actual
    invocation set should pick one from each class, not iterate all four."""
    from levain.verify import SESSION_START_REPRESENTATIVES, SESSION_START_SOURCES
    fresh_class = {"startup", "clear"}
    continuing_class = {"resume", "compact"}
    reps = set(SESSION_START_REPRESENTATIVES)
    assert reps & fresh_class, "missing representative from (startup, clear)"
    assert reps & continuing_class, "missing representative from (resume, compact)"
    assert reps <= set(SESSION_START_SOURCES)


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


# ---------- _init_store: Bucket-2 partnership schema wiring ----------


def test_init_store_requests_partnership_schema(tmp_path: Path, monkeypatch):
    """The install MUST persist the 6-section partnership schema (anneal
    `init --schema partnership`), not the default ops schema — otherwise the
    felt-layer proportion-gate and schema-aware budget silently never fire."""
    captured = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr("levain.install.subprocess.run", fake_run)
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)

    ok = _init_store(store, "anneal-memory")

    assert ok is True
    cmd = captured["cmd"]
    i = cmd.index("init")
    assert cmd[i:i + 3] == ["init", "--schema", "partnership"]


def test_init_store_existing_ops_store_migrates_via_set_schema(tmp_path: Path, monkeypatch):
    """An existing NON-partnership store is NOT re-init'd (memory preserved) but
    its schema IS migrated via `set-schema` (auto-migrate — no silently-ops
    entity under a partnership seed on the upgrade path). The preflight `status
    --json` reports a non-partnership schema, so the migration runs."""
    cmds = []

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)

        class _R:
            returncode = 0
            # status --json preflight: report the existing store as ops/default.
            stdout = '{"schema": "default"}' if "status" in cmd else ""
            stderr = ""

        return _R()

    monkeypatch.setattr("levain.install.subprocess.run", fake_run)
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"existing store bytes")

    ok = _init_store(store, "anneal-memory")

    assert ok is True
    assert any("set-schema" in cmd and "partnership" in cmd for cmd in cmds)
    assert all("init" not in cmd for cmd in cmds)  # never re-init'd


def test_init_store_existing_partnership_store_skips_migration(tmp_path: Path, monkeypatch):
    """An already-partnership store: the preflight detects it and runs NO
    `set-schema` (codex L3 LOW-1 — avoids the redundant audit entry + the
    wrap-guard edge where a no-op migrate could spuriously fail)."""
    cmds = []

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)

        class _R:
            returncode = 0
            stdout = '{"schema": "partnership"}' if "status" in cmd else ""
            stderr = ""

        return _R()

    monkeypatch.setattr("levain.install.subprocess.run", fake_run)
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"existing store bytes")

    ok = _init_store(store, "anneal-memory")

    assert ok is True
    assert all("set-schema" not in cmd for cmd in cmds)  # no redundant migration
    assert all("init" not in cmd for cmd in cmds)


def test_init_store_existing_store_migration_fails_loud(tmp_path: Path, monkeypatch):
    """If `set-schema` is unsupported (old anneal), the existing-store path
    fails loud (returns False) rather than leaving a silently-ops store."""
    class _R:
        returncode = 2
        stdout = ""
        stderr = "error: argument command: invalid choice: 'set-schema'"

    monkeypatch.setattr("levain.install.subprocess.run", lambda *a, **k: _R())
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"existing store bytes")

    assert _init_store(store, "anneal-memory") is False


def test_init_store_both_candidates_carry_schema(tmp_path: Path, monkeypatch):
    """Candidate 1 fails (e.g. `anneal-memory` not on PATH) -> fall through to
    candidate 2 (`python -m anneal_memory`). BOTH must carry `--schema
    partnership`, so a future edit can't silently drop it from the fallback."""
    cmds = []

    class _R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = "boom" if rc else ""

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)
        return _R(1 if len(cmds) == 1 else 0)  # first fails, second succeeds

    monkeypatch.setattr("levain.install.subprocess.run", fake_run)
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)

    ok = _init_store(store, "anneal-memory")

    assert ok is True
    assert len(cmds) == 2  # fell through to the second candidate
    for cmd in cmds:
        i = cmd.index("init")
        assert cmd[i:i + 3] == ["init", "--schema", "partnership"]


def test_init_store_fails_loud_no_bare_init_fallback(tmp_path: Path, monkeypatch):
    """Fail-loud contract (codex L3): if BOTH candidates reject (e.g. an anneal
    too old to know `--schema`), `_init_store` returns False and NEVER falls back
    to a bare `init` that would create a silently-ops store."""
    cmds = []

    class _R:
        returncode = 2
        stdout = ""
        stderr = "error: unrecognized arguments: --schema"

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)
        return _R()

    monkeypatch.setattr("levain.install.subprocess.run", fake_run)
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)

    ok = _init_store(store, "anneal-memory")

    assert ok is False
    assert len(cmds) == 2  # both candidates tried
    for cmd in cmds:
        assert "--schema" in cmd  # never a bare `init` fallback


def test_init_store_migrates_real_ops_store_preserving_content(tmp_path: Path):
    """Integration (needs anneal-memory on path): a real ops-schema store with an
    episode gets migrated to partnership by `_init_store`, and the memory CONTENT
    survives — only the schema metadata changes."""
    pytest.importorskip("anneal_memory")
    import shutil
    from anneal_memory import Store, name_for_schema, DEFAULT_SCHEMA

    store_path = tmp_path / ".levain" / "memory.db"
    store_path.parent.mkdir(parents=True)
    s = Store(str(store_path), section_schema=DEFAULT_SCHEMA)
    s.record("an existing memory worth preserving", "observation")
    s.close()
    assert name_for_schema(Store(str(store_path)).section_schema) == "default"

    ok = _init_store(store_path, shutil.which("anneal-memory") or "anneal-memory")

    assert ok is True
    s2 = Store(str(store_path))
    assert name_for_schema(s2.section_schema) == "partnership"   # migrated
    assert s2.status().total_episodes == 1                       # content preserved
    s2.close()


# ---------- _print_manifest (item 3 — end-of-init file manifest) ----------


def test_print_manifest_lists_created_files_and_says_hand_editable(tmp_path: Path, capsys):
    install = tmp_path / "inst"
    (install / "seed").mkdir(parents=True)
    (install / "seed" / "world.md").write_text("x", encoding="utf-8")
    (install / "seed" / "origin.md").write_text("x", encoding="utf-8")
    (install / "CLAUDE.md").write_text("x", encoding="utf-8")
    (install / ".claude").mkdir()
    (install / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (install / ".mcp.json").write_text("{}", encoding="utf-8")
    (install / "activation").mkdir()
    store = install / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"x")

    _print_manifest(install, "claude-code", store, store_ok=True)
    out = capsys.readouterr().out

    assert "hand-edit" in out.lower()
    assert "world.md" in out
    assert "CLAUDE.md" in out
    assert "settings.json" in out
    assert "memory.db" in out


def test_print_manifest_omits_a_failed_store(tmp_path: Path, capsys):
    install = tmp_path / "inst"
    (install / "seed").mkdir(parents=True)
    (install / "seed" / "world.md").write_text("x", encoding="utf-8")
    store = install / ".levain" / "memory.db"  # never created (init failed)

    _print_manifest(install, "claude-code", store, store_ok=False)
    out = capsys.readouterr().out

    assert "world.md" in out
    assert "memory.db" not in out


def test_print_manifest_codex_includes_global_files(tmp_path: Path, capsys, monkeypatch):
    install = tmp_path / "inst"
    (install / "seed").mkdir(parents=True)
    (install / "seed" / "world.md").write_text("x", encoding="utf-8")
    (install / "AGENTS.md").write_text("x", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    store = install / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"x")

    _print_manifest(install, "codex", store, store_ok=True)
    out = capsys.readouterr().out

    assert "AGENTS.md" in out
    assert "hooks.json" in out
    assert "config.toml" in out


def test_print_manifest_expands_activation_files(tmp_path: Path, capsys):
    # Apparatus catch (L3/codex LOW): the manifest should list the actual files
    # under activation/, not just the directory ("every file" must be true).
    install = tmp_path / "inst"
    (install / "seed").mkdir(parents=True)
    (install / "seed" / "world.md").write_text("x", encoding="utf-8")
    hooks = install / "activation" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "session_start.py").write_text("x", encoding="utf-8")
    (install / "activation" / "posture.md").write_text("x", encoding="utf-8")
    store = install / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"x")

    _print_manifest(install, "claude-code", store, store_ok=True)
    out = capsys.readouterr().out

    assert "session_start.py" in out
    assert "posture.md" in out
