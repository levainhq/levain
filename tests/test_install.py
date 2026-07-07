"""Tests for the install module — the _templates_root() context manager,
the install-path safety helpers, and apply_init (the shared write-half).

`run_init`'s orchestration half (the interactive safety refusal, checkpoint
resume / --force, interview wiring, manifest) is NOT covered here — it needs a
full interview-driver dance and has no automated test yet. The apply_init tests
below cover the write-half it now delegates to.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from levain.install import (
    InitError,
    _base_activation_root,
    _checkpoint_path,
    _clear_checkpoint,
    _compose_activation_layers,
    _copy_activation_tree,
    _fill_seed_imports,
    _init_store,
    _is_safe_install_target,
    _load_checkpoint,
    _print_manifest,
    _save_checkpoint,
    _substitute_hook_placeholders,
    _templates_root,
    _write_brand_config,
    apply_init,
)
from levain.packs import (
    PackBrand,
    SeedEntry,
    compose_roster,
    load_pack_manifest,
    order_activation_roots,
    render_entries,
    verbatim_entries,
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


# ---------- apply_init: the shared write-half (spore-181 Slice B1) ----------

def test_apply_init_writes_seed_adapter_and_inits_store(tmp_path: Path, monkeypatch):
    """apply_init is the write-half extracted from run_init so the CLI and the
    web POST install IDENTICALLY. Drive it directly (no interview/form) against
    the REAL shipped templates + a real claude-code adapter install (pure fs),
    mocking only the anneal-memory store subprocess. This is the write-half's
    first direct coverage — run_init had none."""
    from levain.interview import build_field_plan, parse_template

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _Result())

    install = tmp_path / "install"
    install.mkdir()

    with _templates_root() as templates_root:
        spec_world = parse_template(templates_root / "seed" / "world.md")
        spec_origin = parse_template(templates_root / "seed" / "origin.md")
        # Fill every slot via the shared field-plan seam so no optional section
        # is dropped and every {{SLOT}} resolves.
        answers = {
            f.slot: f"VAL_{f.slot}"
            for f in build_field_plan([spec_world, spec_origin])
        }
        result = apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", [spec_world, spec_origin],
            [
                SeedEntry(n, templates_root / "seed" / n, "verbatim")
                for n in ("partnership.md", "memory.md", "spore_instructions.md",
                          "continuity.md", "README.md")
            ],
        )
        # "Verbatim" means byte-identical to the template source — pin it.
        src_partnership = (templates_root / "seed" / "partnership.md").read_bytes()

    assert result.store_ok is True
    assert result.install == install
    assert result.adapter == "claude-code"
    assert result.complete is True
    # Seed rendered from answers — values substituted, no leftover slots.
    world = (install / "seed" / "world.md").read_text(encoding="utf-8")
    assert "{{" not in world
    assert "VAL_" in world
    assert (install / "seed" / "origin.md").is_file()
    # Verbatim seed files copied — byte-for-byte, not just present.
    assert (install / "seed" / "partnership.md").read_bytes() == src_partnership
    assert (install / "seed" / "spore_instructions.md").is_file()
    # claude-code adapter wiring laid down.
    assert (install / "CLAUDE.md").is_file()
    assert (install / ".claude" / "settings.json").is_file()
    assert (install / ".mcp.json").is_file()


def test_apply_init_persists_answers_json(tmp_path: Path, monkeypatch):
    """init persists the interview answers to .levain/answers.json (gitignored) so a
    later `levain update` can re-render a changed pack render-template with the same
    answers — the render-slot reconcile root-cause fix. Written unconditionally
    (best-effort), independent of store success."""
    import json as _json

    from levain.install import read_answers
    from levain.interview import build_field_plan, parse_template

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _Result())
    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        spec_world = parse_template(templates_root / "seed" / "world.md")
        spec_origin = parse_template(templates_root / "seed" / "origin.md")
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan([spec_world, spec_origin])}
        apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", [spec_world, spec_origin],
            [SeedEntry(n, templates_root / "seed" / n, "verbatim")
             for n in ("partnership.md", "memory.md", "spore_instructions.md",
                       "continuity.md", "README.md")],
        )
    stored = _json.loads((install / ".levain" / "answers.json").read_text(encoding="utf-8"))
    assert stored == answers
    assert read_answers(install) == answers
    # the operator's personal answers must be gitignored
    assert "answers.json" in (install / ".levain" / ".gitignore").read_text(encoding="utf-8")


def test_apply_init_returns_store_failure(tmp_path: Path, monkeypatch):
    """Store-init failure propagates as the return value (run_init maps it to a
    non-zero exit; the web POST will surface it as an error)."""
    from levain.interview import parse_template

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _Fail())

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        spec_world = parse_template(templates_root / "seed" / "world.md")
        spec_origin = parse_template(templates_root / "seed" / "origin.md")
        result = apply_init(
            install, "claude-code", {}, templates_root,
            "/usr/bin/python3", "anneal-memory", [spec_world, spec_origin],
            [
                SeedEntry(n, templates_root / "seed" / n, "verbatim")
                for n in ("partnership.md", "memory.md", "spore_instructions.md",
                          "continuity.md", "README.md")
            ],
        )
    assert result.store_ok is False
    assert result.complete is False
    # Even on store failure, the seed + adapter were written (the store is the
    # last step; partial-install reporting is the caller's job).
    assert (install / "seed" / "world.md").is_file()


def test_apply_init_codex_adapter_path(tmp_path: Path, monkeypatch):
    """The codex adapter path through apply_init — the higher-risk adapter (it
    mutates global ~/.codex). apply_init is the shared entry a web POST can hit
    with chosen='codex', so it needs direct coverage. CODEX_HOME is redirected
    to a tmp dir so the real ~/.codex is never touched."""
    from levain.interview import build_field_plan, parse_template

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _Result())
    codex_home = tmp_path / "codex_home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        spec_world = parse_template(templates_root / "seed" / "world.md")
        spec_origin = parse_template(templates_root / "seed" / "origin.md")
        answers = {
            f.slot: f"VAL_{f.slot}"
            for f in build_field_plan([spec_world, spec_origin])
        }
        result = apply_init(
            install, "codex", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", [spec_world, spec_origin],
            [
                SeedEntry(n, templates_root / "seed" / n, "verbatim")
                for n in ("partnership.md", "memory.md", "spore_instructions.md",
                          "continuity.md", "README.md")
            ],
        )

    assert result.store_ok is True
    assert result.adapter == "codex"
    # codex adapter wiring: AGENTS.md in the install, the activation tree, and
    # the global codex config under the redirected CODEX_HOME.
    assert (install / "AGENTS.md").is_file()
    assert (install / "activation").is_dir()
    assert (codex_home / "hooks.json").is_file()
    assert (codex_home / "config.toml").is_file()
    # The redirected CODEX_HOME means the real ~/.codex was never touched.
    config = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "mcp_servers.anneal_memory" in config


def test_roster_to_apply_init_produces_byte_identical_seed(tmp_path: Path, monkeypatch):
    """End-to-end behavior-preservation guard: discover_roster → apply_init must
    produce a byte-identical seed — every verbatim file equal to its template
    source, every render file slot-filled. The partition test (test_packs) plus
    the apply_init byte-pin (above) together imply this; wiring them into ONE
    test makes the byte-identity claim self-contained, so the exact regression
    this roster refactor must never reintroduce is locked in a single place."""
    from levain.interview import build_field_plan, parse_template

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OK())

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        roster = compose_roster([templates_root])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", specs, verbatim,
        )
        src = {
            p.name: (templates_root / "seed" / p.name).read_bytes()
            for p in (templates_root / "seed").glob("*.md")
        }

    seed = install / "seed"
    # full partition installed
    assert sorted(p.name for p in seed.glob("*.md")) == sorted(src)
    # verbatim files byte-identical to their template source
    for entry in verbatim:
        assert (seed / entry.name).read_bytes() == src[entry.name], f"{entry.name} not byte-identical"
    # render files slot-filled, no leftover placeholders
    for entry in render_entries(roster):
        text = (seed / entry.name).read_text(encoding="utf-8")
        assert "{{" not in text and "VAL_" in text


def test_apply_init_installs_layered_pack_files(tmp_path: Path, monkeypatch):
    """Multi-root end-to-end (Slice 2): a pack-layer's verbatim file installs from
    the PACK's path (not a reconstructed base path), and a pack override replaces
    the base file's content. Proves apply_init copies each verbatim entry from its
    winning layer."""
    from levain.interview import build_field_plan, parse_template

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OK())

    # A pack that ADDS a verbatim file and OVERRIDES the base partnership.md.
    pack = tmp_path / "pack"
    (pack / "seed").mkdir(parents=True)
    (pack / "pack.toml").write_text('name = "dom"\norder = 10\n', encoding="utf-8")
    (pack / "seed" / "audit_method.md").write_text("PRESSABLE AUDIT DOCTRINE\n", encoding="utf-8")
    (pack / "seed" / "partnership.md").write_text("PACK PARTNERSHIP OVERRIDE\n", encoding="utf-8")

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        roster = compose_roster([templates_root, pack])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", specs, verbatim,
        )

    seed = install / "seed"
    # the pack's NEW verbatim file installed — copied from the pack, not base
    assert (seed / "audit_method.md").read_text(encoding="utf-8") == "PRESSABLE AUDIT DOCTRINE\n"
    # the pack OVERRODE the base partnership.md (last-layer-wins by filename)
    assert (seed / "partnership.md").read_text(encoding="utf-8") == "PACK PARTNERSHIP OVERRIDE\n"
    # base render files still present + filled
    world = (seed / "world.md").read_text(encoding="utf-8")
    assert "{{" not in world and "VAL_" in world


def test_apply_init_renders_pack_overridden_template(tmp_path: Path, monkeypatch):
    """A pack overriding (and re-listing) a base RENDER file: the installed,
    rendered output comes from the PACK's template — proving render reads each
    spec from its winning layer, not just the base templates."""
    from levain.interview import build_field_plan, parse_template

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OK())

    pack = tmp_path / "pack"
    (pack / "seed").mkdir(parents=True)
    (pack / "pack.toml").write_text(
        'name = "dom"\norder = 10\nrender = ["world.md"]\n', encoding="utf-8"
    )
    (pack / "seed" / "world.md").write_text("PACK WORLD {{OPERATOR_NAME}}\n", encoding="utf-8")

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        roster = compose_roster([templates_root, pack])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", specs, verbatim,
        )

    world = (install / "seed" / "world.md").read_text(encoding="utf-8")
    assert "PACK WORLD" in world          # content from the pack's overriding template
    assert "{{" not in world              # slots filled
    assert "VAL_OPERATOR_NAME" in world


def test_apply_init_raises_on_missing_verbatim_source(tmp_path: Path, monkeypatch):
    """A verbatim entry whose source vanished must FAIL LOUD — never a silent
    skip that yields an install missing a seed file (codex L3, Slice 2 MED)."""
    from levain.interview import parse_template

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OK())

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        specs = [parse_template(templates_root / "seed" / "world.md")]
        bogus = [SeedEntry("ghost.md", tmp_path / "nope" / "ghost.md", "verbatim")]
        with pytest.raises(FileNotFoundError):
            apply_init(
                install, "claude-code", {}, templates_root,
                "/usr/bin/python3", "anneal-memory", specs, bogus,
            )


# --- Slice 3: roster-driven adapter @import generation ------------------------

class _OKRun:
    returncode = 0
    stdout = ""
    stderr = ""


def _base_apply(install: Path, adapter: str, templates_root: Path) -> None:
    """Run a BASE (no-pack) apply_init for `adapter` into `install`."""
    from levain.interview import build_field_plan, parse_template

    roster = compose_roster([templates_root])
    specs = [parse_template(e.path) for e in render_entries(roster)]
    verbatim = verbatim_entries(roster)
    answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
    apply_init(
        install, adapter, answers, templates_root,
        "/usr/bin/python3", "anneal-memory", specs, verbatim,
    )


def test_apply_init_generates_claude_seed_imports_byte_identical(tmp_path: Path, monkeypatch):
    """Slice 3: the claude-code CLAUDE.md @seed import list is GENERATED from the
    roster, but a base install reproduces the authored curriculum block exactly
    (behavior-preserving) — continuity.md + README.md excluded."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        _base_apply(install, "claude-code", templates_root)
        template = (
            templates_root / "adapters" / "claude-code" / "CLAUDE.md.template"
        ).read_text(encoding="utf-8")

    expected_block = (
        "@seed/origin.md\n@seed/partnership.md\n@seed/world.md\n"
        "@seed/memory.md\n@seed/spore_instructions.md"
    )
    expected = template.replace("{{SEED_IMPORTS}}", expected_block)
    got = (install / "CLAUDE.md").read_text(encoding="utf-8")
    assert got == expected
    assert "@seed/continuity.md" not in got
    assert "@seed/README.md" not in got
    # the surrounding template prose survives — not just the generated block
    assert "anneal://continuity resource" in got


def test_apply_init_generates_codex_seed_imports_byte_identical(tmp_path: Path, monkeypatch):
    """Slice 3: the codex AGENTS.md read-list is generated; a base install
    reproduces the authored numbered+described list exactly (descriptions
    self-source from each seed H1), continuity.md + README.md excluded."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        _base_apply(install, "codex", templates_root)
        template = (
            templates_root / "adapters" / "codex" / "AGENTS.md.template"
        ).read_text(encoding="utf-8")

    expected_block = (
        "1. `seed/origin.md` — Who You Are\n"
        "2. `seed/partnership.md` — How We Work\n"
        "3. `seed/world.md` — Who Your Operator Is\n"
        "4. `seed/memory.md` — Your Memory\n"
        "5. `seed/spore_instructions.md` — Your Open Loops"
    )
    expected = template.replace("{{SEED_IMPORTS}}", expected_block)
    got = (install / "AGENTS.md").read_text(encoding="utf-8")
    assert got == expected
    # The backtick-wrapped form is the import-LIST entry — absent here (continuity
    # / README never load). The bare names DO appear in the trailing explanatory
    # comment, so assert the list form, not the substring.
    assert "`seed/continuity.md`" not in got
    assert "`seed/README.md`" not in got
    # the surrounding template prose survives — not just the generated block
    assert "anneal://continuity resource" in got


def test_apply_init_pack_seed_file_loads_in_both_adapters(tmp_path: Path, monkeypatch):
    """THE load-bearing Slice 3 catch: a pack's NEW seed file must LOAD, not just
    install to disk. It appears in the claude-code @seed imports AND the codex
    read-list, appended after the base curriculum; the codex description
    self-sources from the file's H1. A pack OVERRIDE without an H1 falls back to a
    bare codex entry, curriculum position preserved."""
    from levain.interview import build_field_plan, parse_template

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))

    pack = tmp_path / "pack"
    (pack / "seed").mkdir(parents=True)
    (pack / "pack.toml").write_text('name = "dom"\norder = 10\n', encoding="utf-8")
    (pack / "seed" / "audit_method.md").write_text(
        "# Pressable Audit Method\n\nbody\n", encoding="utf-8"
    )
    (pack / "seed" / "partnership.md").write_text("PACK OVERRIDE no h1\n", encoding="utf-8")

    def _apply(install: Path, adapter: str) -> None:
        with _templates_root() as templates_root:
            roster = compose_roster([templates_root, pack])
            specs = [parse_template(e.path) for e in render_entries(roster)]
            verbatim = verbatim_entries(roster)
            answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
            apply_init(
                install, adapter, answers, templates_root,
                "/usr/bin/python3", "anneal-memory", specs, verbatim,
            )

    cc = tmp_path / "cc"
    cc.mkdir()
    _apply(cc, "claude-code")
    claude = (cc / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@seed/audit_method.md" in claude
    # appended AFTER the base curriculum (the last base import is spore_instructions)
    assert claude.index("@seed/spore_instructions.md") < claude.index("@seed/audit_method.md")

    cx = tmp_path / "cx"
    cx.mkdir()
    _apply(cx, "codex")
    agents = (cx / "AGENTS.md").read_text(encoding="utf-8")
    assert "6. `seed/audit_method.md` — Pressable Audit Method" in agents
    # the no-H1 override falls back to a BARE entry, curriculum position 2 kept
    assert "2. `seed/partnership.md`\n" in agents


def test_fill_seed_imports_requires_exactly_one_placeholder():
    """Honesty floor: a template with ZERO or MULTIPLE {{SEED_IMPORTS}}
    placeholders fails loud (a multi-placeholder template would duplicate the
    import block), never silently produces a wrong adapter file."""
    with pytest.raises(InitError, match="SEED_IMPORTS"):
        _fill_seed_imports("no placeholder here\n", "@seed/origin.md")
    with pytest.raises(InitError, match="exactly one"):
        _fill_seed_imports("a {{SEED_IMPORTS}} b {{SEED_IMPORTS}} c", "X")
    # happy path: exactly one placeholder substitutes
    assert _fill_seed_imports("a {{SEED_IMPORTS}} b", "X") == "a X b"


def test_seed_role_title_extraction(tmp_path: Path):
    """The codex description self-sources from the H1, stripping a ' — <suffix>'
    tail, requiring a column-0 H1, and dropping an empty/placeholder title (a bare
    entry beats leaking raw {{...}})."""
    from levain.install import _seed_role_title

    def title_of(text: str) -> str | None:
        p = tmp_path / "s.md"
        p.write_text(text, encoding="utf-8")
        return _seed_role_title(p)

    assert title_of("# Who You Are — {{ENTITY_NAME}}\n\nbody\n") == "Who You Are"
    assert title_of("# Who Your Operator Is\n") == "Who Your Operator Is"
    assert title_of("# Audit Method — Pressable — v2\n") == "Audit Method"
    # placeholder BEFORE any em-dash -> meaningless description -> bare entry
    assert title_of("# {{ENTITY_NAME}}'s Origin\n") is None
    # empty H1 -> bare entry
    assert title_of("# \n\nbody\n") is None
    # indented "    # cmd" is a code line, not an H1
    assert title_of("    # not a heading\n\n# Real Title\n") == "Real Title"
    # no H1 at all -> None (graceful)
    assert title_of("just prose, no heading\n") is None


def test_apply_init_pack_render_file_loads_in_import_list(tmp_path: Path, monkeypatch):
    """A pack-added RENDER file (not just verbatim) also loads — the import list is
    mode-agnostic (a seed file is render XOR verbatim, and both import)."""
    from levain.interview import build_field_plan, parse_template

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())

    pack = tmp_path / "pack"
    (pack / "seed").mkdir(parents=True)
    (pack / "pack.toml").write_text(
        'name = "dom"\norder = 10\nrender = ["method.md"]\n', encoding="utf-8"
    )
    (pack / "seed" / "method.md").write_text("# Audit Method\n\n{{DETAIL}}\n", encoding="utf-8")

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        roster = compose_roster([templates_root, pack])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", specs, verbatim,
        )

    claude = (install / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@seed/method.md" in claude
    # the render file was actually rendered AND imported
    rendered = (install / "seed" / "method.md").read_text(encoding="utf-8")
    assert "{{" not in rendered and "VAL_DETAIL" in rendered


def test_apply_init_missing_base_seed_fails_loud(tmp_path: Path, monkeypatch):
    """Honesty floor (base half): a corrupt roster missing a base methodology seed
    fails loud, naming it — never silently generates an adapter without that
    import (the invisible-infrastructure failure for a BASE seed)."""
    from levain.interview import parse_template

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        spec_world = parse_template(templates_root / "seed" / "world.md")
        spec_origin = parse_template(templates_root / "seed" / "origin.md")
        # verbatim DELIBERATELY omits spore_instructions.md (the corrupt-wheel case)
        verbatim = [
            SeedEntry(n, templates_root / "seed" / n, "verbatim")
            for n in ("partnership.md", "memory.md", "continuity.md", "README.md")
        ]
        with pytest.raises(InitError, match="spore_instructions.md"):
            apply_init(
                install, "claude-code", {}, templates_root,
                "/usr/bin/python3", "anneal-memory", [spec_world, spec_origin], verbatim,
            )


def test_fill_seed_imports_empty_block_raises():
    """Honesty floor: an EMPTY generated block must fail loud too (it would
    otherwise write an import-less adapter) — self-contained, not relying on a
    caller preflight."""
    with pytest.raises(InitError, match="import-less"):
        _fill_seed_imports("x {{SEED_IMPORTS}} y", "")
    with pytest.raises(InitError, match="import-less"):
        _fill_seed_imports("x {{SEED_IMPORTS}} y", "   \n  ")


# --- Slice 4a: activation-tree multi-root layering ---------------------------
#
# A pack's activation/ tree composes ON TOP of the adapter's base activation tree
# with the SAME order/last-wins semantics as the seed roster (a pack's
# activation/posture.md overrides base's). The two adapters layer onto DIFFERENT
# base trees (claude = templates/activation; codex = adapters/codex/activation).

def _make_activation_pack(
    root: Path, *, order: int, activation: dict[str, str],
    seed: dict[str, str] | None = None,
) -> Path:
    """A valid pack layer (pack.toml + seed/) carrying an activation/ tree.
    `activation` maps a relative posix path -> file body."""
    (root / "seed").mkdir(parents=True)
    (root / "pack.toml").write_text(f'name = "dom{order}"\norder = {order}\n', encoding="utf-8")
    for name, body in (seed or {"dom_doctrine.md": "DOMAIN\n"}).items():
        (root / "seed" / name).write_text(body, encoding="utf-8")
    for rel, body in activation.items():
        target = root / "activation" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def _apply_with_packs(install: Path, adapter: str, packs: list[Path]) -> None:
    """Run apply_init for `adapter` composing `packs` on top of base (the CLI path
    — resolves the activation-tree layer stack up-front, as run_init does)."""
    from levain.interview import build_field_plan, parse_template

    with _templates_root() as templates_root:
        roster = compose_roster([templates_root, *packs])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        activation_roots = order_activation_roots(
            templates_root, _base_activation_root(adapter, templates_root), packs
        )
        apply_init(
            install, adapter, answers, templates_root,
            "/usr/bin/python3", "anneal-memory", specs, verbatim,
            activation_roots=activation_roots,
        )


def test_activation_pack_overrides_posture_claude(tmp_path: Path, monkeypatch):
    """A pack's activation/posture.md overrides base's (claude-code); base hooks
    that the pack does NOT override survive."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    pack = _make_activation_pack(
        tmp_path / "pack", order=10, activation={"posture.md": "PACK POSTURE OVERRIDE\n"}
    )
    install = tmp_path / "install"
    install.mkdir()
    _apply_with_packs(install, "claude-code", [pack])

    assert (install / "activation" / "posture.md").read_text(encoding="utf-8") == (
        "PACK POSTURE OVERRIDE\n"
    )
    # base hooks (not overridden) still present + anneal-substituted
    hook = install / "activation" / "hooks" / "session_start.py"
    assert hook.is_file()
    assert "{{ANNEAL_MEMORY}}" not in hook.read_text(encoding="utf-8")


def test_activation_pack_overrides_posture_codex(tmp_path: Path, monkeypatch):
    """The codex adapter layers a pack onto its OWN base activation tree
    (adapters/codex/activation) — the override lands and codex base hooks survive."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    pack = _make_activation_pack(
        tmp_path / "pack", order=10, activation={"posture.md": "PACK POSTURE CODEX\n"}
    )
    install = tmp_path / "install"
    install.mkdir()
    _apply_with_packs(install, "codex", [pack])

    assert (install / "activation" / "posture.md").read_text(encoding="utf-8") == (
        "PACK POSTURE CODEX\n"
    )
    assert (install / "activation" / "hooks" / "session_start.py").is_file()


def test_activation_pack_adds_new_file(tmp_path: Path, monkeypatch):
    """A pack ADDS an activation file base does not have — it installs, and base's
    own posture.md (not overridden) is untouched."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    pack = _make_activation_pack(
        tmp_path / "pack", order=10, activation={"domain_directives.md": "EXTRA DIRECTIVES\n"}
    )
    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        base_posture = (templates_root / "activation" / "posture.md").read_bytes()
    _apply_with_packs(install, "claude-code", [pack])

    assert (install / "activation" / "domain_directives.md").read_text(encoding="utf-8") == (
        "EXTRA DIRECTIVES\n"
    )
    assert (install / "activation" / "posture.md").read_bytes() == base_posture


def test_activation_base_only_byte_identical_and_pyc_excluded(tmp_path: Path, monkeypatch):
    """No packs → the composed activation tree is the base tree: posture.md is
    byte-identical to the template and __pycache__/*.pyc are excluded (the
    pre-layering copytree's ignore_patterns contract)."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        _base_apply(install, "claude-code", templates_root)
        base_posture = (templates_root / "activation" / "posture.md").read_bytes()

    assert (install / "activation" / "posture.md").read_bytes() == base_posture
    assert not (install / "activation" / "hooks" / "__pycache__").exists()
    assert list((install / "activation").rglob("*.pyc")) == []


def test_activation_seed_only_pack_leaves_base_activation(tmp_path: Path, monkeypatch):
    """A seed-only pack (no activation/ tree) → the install's activation tree is
    base-only; the pack's seed file still installs (the two layerings are
    independent)."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    pack = tmp_path / "pack"
    (pack / "seed").mkdir(parents=True)
    (pack / "pack.toml").write_text('name = "dom"\norder = 10\n', encoding="utf-8")
    (pack / "seed" / "audit_method.md").write_text("AUDIT\n", encoding="utf-8")
    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        base_posture = (templates_root / "activation" / "posture.md").read_bytes()
    _apply_with_packs(install, "claude-code", [pack])

    assert (install / "activation" / "posture.md").read_bytes() == base_posture
    assert (install / "seed" / "audit_method.md").read_text(encoding="utf-8") == "AUDIT\n"


def test_activation_operator_edit_backed_up_against_winning_source(tmp_path: Path, monkeypatch):
    """On a re-install, an operator-edited posture.md is backed up when it differs
    from the WINNING (pack-overridden) version about to be written."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    pack = _make_activation_pack(
        tmp_path / "pack", order=10, activation={"posture.md": "PACK POSTURE v1\n"}
    )
    install = tmp_path / "install"
    install.mkdir()
    _apply_with_packs(install, "claude-code", [pack])          # lays down the pack's posture.md
    (install / "activation" / "posture.md").write_text("OPERATOR EDIT\n", encoding="utf-8")
    _apply_with_packs(install, "claude-code", [pack])          # re-install over the edit

    assert (install / "activation" / "posture.md").read_text(encoding="utf-8") == "PACK POSTURE v1\n"
    backups = list((install / ".levain" / "backups" / "activation").rglob("posture.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "OPERATOR EDIT\n"


def test_activation_no_backup_when_current_matches_winning_pack(tmp_path: Path, monkeypatch):
    """No spurious backup when the installed posture.md already matches the WINNING
    pack override (even though it DIFFERS from base) — proves the backup compares
    against the winning source, not base."""
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OKRun())
    pack = _make_activation_pack(
        tmp_path / "pack", order=10, activation={"posture.md": "PACK POSTURE\n"}
    )
    install = tmp_path / "install"
    install.mkdir()
    _apply_with_packs(install, "claude-code", [pack])          # installs pack posture.md
    _apply_with_packs(install, "claude-code", [pack])          # re-install, no operator edit

    backups = list((install / ".levain" / "backups" / "activation").rglob("posture.md"))
    assert backups == []


# --- Slice 4a hardening: _copy_activation_tree / _compose_activation_layers ----
# Direct tests of the layer-composition guards the L3 apparatus (codex + L1 +
# nemotron) surfaced: empty-composition refusal, fail-loud operator-edit
# preservation (incl. the winning=None deletion case), hook override + nested-hook
# substitution, empty-dir preservation, and per-component pyc exclusion.

def _mk_layer(root: Path, files: dict[str, str]) -> Path:
    """An activation-tree layer root; `files` maps relative posix path -> content."""
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def test_copy_activation_empty_composition_raises(tmp_path: Path):
    """An empty composition (missing/absent base activation tree) must FAIL LOUD
    BEFORE wiping dst — never rmtree-then-write-nothing (codex/L1/nemotron HIGH)."""
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "posture.md").write_text("operator edit\n", encoding="utf-8")
    missing = tmp_path / "nonexistent_base"  # not a dir → base contributes nothing
    with pytest.raises(InitError, match="contributes no files"):
        _copy_activation_tree([missing], dst, base_activation=missing)
    # dst was NOT wiped — the operator's file survives (raise came before rmtree).
    assert (dst / "posture.md").read_text(encoding="utf-8") == "operator edit\n"


def test_copy_activation_winning_none_operator_file_backed_up(tmp_path: Path):
    """An operator-editable file present at dst that NO layer provides (winning is
    None → rmtree would DELETE it) is backed up before the wipe (nemotron HIGH-2)."""
    base = _mk_layer(tmp_path / "base", {"posture.md": "BASE POSTURE\n"})  # no recency
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "posture.md").write_text("BASE POSTURE\n", encoding="utf-8")
    (dst / "recency_directives.md").write_text("OPERATOR RECENCY\n", encoding="utf-8")
    _copy_activation_tree([base], dst, base_activation=base)
    # recency is gone from the install (no layer provides it) BUT preserved in backups.
    assert not (dst / "recency_directives.md").exists()
    backups = list((tmp_path / "install" / ".levain" / "backups" / "activation").rglob("recency_directives.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "OPERATOR RECENCY\n"


def test_copy_activation_unbackuppable_operator_edit_raises(tmp_path: Path, monkeypatch):
    """If an operator edit can't be backed up (copy2 fails), refuse — don't rmtree
    over it (codex HIGH-2). Simulated by making shutil.copy2 raise during backup."""
    base = _mk_layer(tmp_path / "base", {"posture.md": "NEW BASE POSTURE\n"})
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "posture.md").write_text("OPERATOR EDIT\n", encoding="utf-8")  # differs from winning

    import shutil as _shutil
    real_copy2 = _shutil.copy2

    def _boom(src, d, *a, **k):
        if str(d).endswith("posture.md") and "backups" in str(d):
            raise OSError("disk full")
        return real_copy2(src, d, *a, **k)

    monkeypatch.setattr("levain.install.shutil.copy2", _boom)
    with pytest.raises(InitError, match="could not be backed up"):
        _copy_activation_tree([base], dst, base_activation=base)
    # The operator's edit was NOT destroyed (raise came before rmtree).
    assert (dst / "posture.md").read_text(encoding="utf-8") == "OPERATOR EDIT\n"


def test_copy_activation_pack_overrides_base_hook(tmp_path: Path):
    """A pack layer overriding a base hook .py wins (the docstring's explicit
    claim — only posture.md override was exercised before; L1 test gap)."""
    base = _mk_layer(tmp_path / "base", {
        "posture.md": "P\n", "hooks/session_start.py": "BASE HOOK\n",
    })
    pack = _mk_layer(tmp_path / "pack" / "activation", {"hooks/session_start.py": "PACK HOOK\n"})
    dst = tmp_path / "install" / "activation"
    _copy_activation_tree([base, pack], dst, base_activation=base)  # pack later → wins
    assert (dst / "hooks" / "session_start.py").read_text(encoding="utf-8") == "PACK HOOK\n"
    assert (dst / "posture.md").read_text(encoding="utf-8") == "P\n"  # base survives


def test_copy_activation_nested_hook_substituted(tmp_path: Path):
    """{{ANNEAL_MEMORY}} substitution reaches a NESTED pack hook (recursive — the
    composition supports nested subtrees, so the substitution must too; L1 MED)."""
    base = _mk_layer(tmp_path / "base", {
        "posture.md": "P\n",
        "hooks/session_start.py": "anneal = '{{ANNEAL_MEMORY}}'\n",
        "hooks/sub/nested.py": "anneal = '{{ANNEAL_MEMORY}}'\n",
    })
    dst = tmp_path / "install" / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/abs/anneal-memory")
    assert "{{ANNEAL_MEMORY}}" not in (dst / "hooks" / "session_start.py").read_text(encoding="utf-8")
    nested = (dst / "hooks" / "sub" / "nested.py").read_text(encoding="utf-8")
    assert "{{ANNEAL_MEMORY}}" not in nested
    assert "/abs/anneal-memory" in nested


def test_copy_activation_preserves_empty_dir(tmp_path: Path):
    """An empty directory in a layer is recreated in dst (copytree did — byte-for-
    byte structural equivalence; nemotron/codex/L1 empty-dir finding)."""
    base = _mk_layer(tmp_path / "base", {"posture.md": "P\n"})
    (base / "hooks" / "extensions").mkdir(parents=True)  # intentionally empty
    dst = tmp_path / "install" / "activation"
    _copy_activation_tree([base], dst, base_activation=base)
    assert (dst / "hooks" / "extensions").is_dir()


def test_compose_excludes_pycache_and_pyc_dir(tmp_path: Path):
    """Per-component exclusion matches copytree's ignore_patterns: a __pycache__
    dir, a *.pyc file, AND a directory NAMED *.pyc are all excluded (codex LOW —
    closer to copytree than the old file-suffix-only check)."""
    base = _mk_layer(tmp_path / "base", {
        "posture.md": "P\n",
        "hooks/real.py": "x\n",
        "hooks/__pycache__/real.cpython-312.pyc": "junk\n",
        "stale.pyc/inside.txt": "junk\n",  # a DIR named *.pyc
    })
    composed = _compose_activation_layers([base])
    assert "posture.md" in composed
    assert "hooks/real.py" in composed
    assert not any("__pycache__" in k for k in composed)
    assert not any(k.endswith(".pyc") for k in composed)
    assert "stale.pyc/inside.txt" not in composed  # the *.pyc DIR's contents excluded


def test_copy_activation_staging_preserves_dst_on_build_failure(tmp_path: Path, monkeypatch):
    """A mid-build failure (e.g. a source that vanishes between compose and copy)
    leaves the EXISTING dst untouched — the atomic staging swap never leaves a
    partial activation tree after the old one is gone (codex L3 re-verify HIGH).
    Simulated by failing copy2 partway through the staged build."""
    base = _mk_layer(tmp_path / "base", {"posture.md": "NEW\n", "hooks/h.py": "x\n"})
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "sentinel.md").write_text("PRE-EXISTING\n", encoding="utf-8")

    import shutil as _shutil
    real_copy2 = _shutil.copy2

    def _boom(src, d, *a, **k):
        if str(d).endswith("h.py"):  # fail PART-WAY through the build
            raise OSError("simulated mid-build copy failure")
        return real_copy2(src, d, *a, **k)

    monkeypatch.setattr("levain.install.shutil.copy2", _boom)
    with pytest.raises(OSError, match="simulated mid-build"):
        _copy_activation_tree([base], dst, base_activation=base)
    # dst was NOT replaced — the pre-existing tree survives intact (built in staging).
    assert (dst / "sentinel.md").read_text(encoding="utf-8") == "PRE-EXISTING\n"
    assert not (dst / "posture.md").exists()  # the half-built tree never swapped in
    # the staging dir was cleaned up (no orphan under the install root).
    assert not list(dst.parent.glob(".levain-activation-new-*"))


def test_copy_activation_empty_base_masked_by_pack_raises(tmp_path: Path):
    """An EMPTY base activation tree fails loud even when a pack contributes files
    — the aggregate composition is non-empty but base contributes nothing, so a
    pack must not mask it (codex L3 re-verify round-2 HIGH)."""
    base = tmp_path / "base_activation"
    base.mkdir()  # exists but EMPTY
    pack = _mk_layer(tmp_path / "pack", {"posture.md": "PACK\n"})
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "sentinel.md").write_text("PRE\n", encoding="utf-8")
    with pytest.raises(InitError, match="contributes no files"):
        _copy_activation_tree([base, pack], dst, base_activation=base)
    assert (dst / "sentinel.md").read_text(encoding="utf-8") == "PRE\n"  # untouched


def test_copy_activation_swap_failure_restores_dst(tmp_path: Path, monkeypatch):
    """If the atomic swap (new → dst) fails, the original tree is RESTORED — dst is
    never left missing/partial (codex L3 re-verify round-2 MED rollback)."""
    base = _mk_layer(tmp_path / "base", {"posture.md": "NEW\n"})
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "sentinel.md").write_text("ORIGINAL\n", encoding="utf-8")

    import os as _os
    real_replace = _os.replace

    def _fail_new_into_place(src, d, *a, **k):
        # fail only the new_tree → dst rename; let the move-aside + restore through.
        if ".levain-activation-new-" in str(src):
            raise OSError("simulated swap failure")
        return real_replace(src, d, *a, **k)

    monkeypatch.setattr("levain.install.os.replace", _fail_new_into_place)
    with pytest.raises(OSError, match="simulated swap"):
        _copy_activation_tree([base], dst, base_activation=base)
    # original restored (not left missing); new tree never landed; no orphan dirs.
    assert (dst / "sentinel.md").read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (dst / "posture.md").exists()
    assert not list(dst.parent.glob(".levain-activation-*"))


def test_copy_activation_backup_cleaned_on_build_failure(tmp_path: Path, monkeypatch):
    """A build failure after operator-edit backups were staged cleans those backups
    — the originals still live in the untouched dst, so leftover backups would be
    misleading (codex L3 re-verify round-2 LOW)."""
    base = _mk_layer(tmp_path / "base", {"posture.md": "NEW\n", "hooks/h.py": "x\n"})
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "posture.md").write_text("OPERATOR EDIT\n", encoding="utf-8")  # differs → backed up

    import shutil as _shutil
    real_copy2 = _shutil.copy2

    def _boom(src, d, *a, **k):
        if str(d).endswith("h.py"):  # fail mid-build, AFTER the posture.md backup ran
            raise OSError("mid-build fail")
        return real_copy2(src, d, *a, **k)

    monkeypatch.setattr("levain.install.shutil.copy2", _boom)
    with pytest.raises(OSError, match="mid-build fail"):
        _copy_activation_tree([base], dst, base_activation=base)
    # operator edit preserved in the untouched dst; the misleading backup was cleaned.
    assert (dst / "posture.md").read_text(encoding="utf-8") == "OPERATOR EDIT\n"
    backups_root = dst.parent / ".levain" / "backups" / "activation"
    assert not backups_root.exists() or not list(backups_root.iterdir())


def test_copy_activation_cross_layer_file_dir_collision_fails_loud(tmp_path: Path):
    """A path that is a FILE in one layer and a DIRECTORY in another fails loud (not
    a silently malformed tree), and the staging keeps dst untouched (codex L3
    re-verify round-3 MED)."""
    base = _mk_layer(tmp_path / "base", {"posture.md": "P\n", "x": "FILE\n"})
    pack = _mk_layer(tmp_path / "pack", {"x/inner.txt": "DIR\n"})  # x is a DIR here
    dst = tmp_path / "install" / "activation"
    dst.mkdir(parents=True)
    (dst / "sentinel.md").write_text("ORIGINAL\n", encoding="utf-8")
    with pytest.raises(InitError, match="layer conflict"):
        _copy_activation_tree([base, pack], dst, base_activation=base)
    assert (dst / "sentinel.md").read_text(encoding="utf-8") == "ORIGINAL\n"  # untouched
    assert not list(dst.parent.glob(".levain-activation-*"))  # staging cleaned


# ---------- pack white-labeling: brand → .levain/config.json (build→runtime) ----------

def _read_config(install: Path) -> dict:
    return json.loads((install / ".levain" / "config.json").read_text(encoding="utf-8"))


def test_write_brand_config_bakes_brand(tmp_path: Path):
    (tmp_path / ".levain").mkdir()
    _write_brand_config(
        tmp_path, PackBrand(surface_name="Acme Harness", subtitle="the tagline"), lambda m: None
    )
    assert _read_config(tmp_path) == {"surface_name": "Acme Harness", "subtitle": "the tagline"}


def test_write_brand_config_merges_preserving_entity_name(tmp_path: Path):
    # An operator-set entity_name (a governed rename) must survive a brand write.
    lev = tmp_path / ".levain"
    lev.mkdir()
    (lev / "config.json").write_text('{"entity_name": "Athena"}\n', encoding="utf-8")
    _write_brand_config(tmp_path, PackBrand(surface_name="Acme"), lambda m: None)
    assert _read_config(tmp_path) == {"entity_name": "Athena", "surface_name": "Acme"}


def test_write_brand_config_clear_on_drop_preserves_entity_name(tmp_path: Path):
    # A re-install WITHOUT a brand (brand=None) clears stale brand keys but keeps
    # the operator's entity_name — the IP-boundary discipline _copy_pack_docs follows.
    lev = tmp_path / ".levain"
    lev.mkdir()
    (lev / "config.json").write_text(
        '{"entity_name": "Athena", "surface_name": "Old Co", "subtitle": "stale"}\n',
        encoding="utf-8",
    )
    _write_brand_config(tmp_path, None, lambda m: None)
    assert _read_config(tmp_path) == {"entity_name": "Athena"}


def test_write_brand_config_noop_writes_nothing(tmp_path: Path):
    # No brand now, none before → no file created (no spurious mtime churn).
    (tmp_path / ".levain").mkdir()
    _write_brand_config(tmp_path, None, lambda m: None)
    assert not (tmp_path / ".levain" / "config.json").exists()


def test_apply_init_bakes_pack_brand_into_config(tmp_path: Path, monkeypatch):
    """End-to-end (both install entry points share apply_init): a pack's
    pack.toml [brand] lands in the operator-facing .levain/config.json, the same
    runtime channel entity_name travels."""
    from levain.interview import build_field_plan, parse_template

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OK())

    pack = tmp_path / "pack"
    (pack / "seed").mkdir(parents=True)
    (pack / "pack.toml").write_text(
        'name = "dom"\norder = 10\n[brand]\n'
        'surface_name = "Pressable Solutions Harness"\nsubtitle = "your team\'s memory"\n',
        encoding="utf-8",
    )
    (pack / "seed" / "doctrine.md").write_text("DOCTRINE\n", encoding="utf-8")

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        roster = compose_roster([templates_root, pack])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", specs, verbatim,
            packs=[(load_pack_manifest(pack), pack)],
        )
    assert _read_config(install) == {
        "surface_name": "Pressable Solutions Harness",
        "subtitle": "your team's memory",
    }


def test_apply_init_no_pack_writes_no_brand_config(tmp_path: Path, monkeypatch):
    # A base-only install (no --pack) leaves no brand config — surfaces show the
    # Levain default. (No config.json is created for a fresh brand-less install.)
    from levain.interview import build_field_plan, parse_template

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _OK())

    install = tmp_path / "install"
    install.mkdir()
    with _templates_root() as templates_root:
        roster = compose_roster([templates_root])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, "claude-code", answers, templates_root,
            "/usr/bin/python3", "anneal-memory", specs, verbatim, packs=[],
        )
    assert not (install / ".levain" / "config.json").exists()


def test_write_brand_config_refuses_to_clobber_unreadable_config(tmp_path: Path):
    # A pre-corrupt config.json (hand-edit typo) fails soft to {} on read; writing
    # brand onto {} would ERASE a recoverable entity_name. Refuse to overwrite it.
    lev = tmp_path / ".levain"
    lev.mkdir()
    corrupt = '{"entity_name": "Athena", BROKEN'
    (lev / "config.json").write_text(corrupt, encoding="utf-8")
    notes: list[str] = []
    _write_brand_config(tmp_path, PackBrand(surface_name="Acme"), notes.append)
    # untouched — the operator's entity_name stays recoverable by hand
    assert (lev / "config.json").read_text(encoding="utf-8") == corrupt
    assert any("unreadable" in n for n in notes)
