"""The operator's PROJECT memory is in the universal floor (spore-1308, ruled by Phill 2026-10-03).

``~/.anneal-projects`` (project stores + flow's derive-trust file) is denied read+write like
``~/.anneal-memory``; ``$ANNEAL_MEMORY_DERIVE_TRUST``, when set at policy build, has its file
write-denied and its parent denied as a subtree unless that would swallow the entity's own area.
The live tests spawn a real confined shell (seatbelt on macOS, bwrap on Linux) and assert refusal
against a control write that must succeed."""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from levain.firing.confinement import (
    DERIVE_TRUST_ENV,
    build_policy,
    bwrap_available,
    crown_jewel_reason,
    sandbox_exec_available,
    select_provider,
)

_SANDBOX = (platform.system() == "Darwin" and sandbox_exec_available()) or (
    platform.system() == "Linux" and bwrap_available()
)
live = pytest.mark.skipif(not _SANDBOX, reason="needs macOS sandbox-exec or a working Linux bwrap")


def _entity(root: Path) -> Path:
    d = root / "entities" / "coyote"
    (d / ".levain").mkdir(parents=True)
    (d / "workspace").mkdir()
    return d


def _project_home(home: Path) -> Path:
    ap = home / ".anneal-projects"
    (ap / "levain-v2").mkdir(parents=True)
    (ap / "derive-trust.json").write_text('{"version": 2, "labels": {}}\n')
    (ap / "levain-v2" / "memory.db").write_text("PROJECT MEMORY — must never be read by an entity")
    return ap


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv(DERIVE_TRUST_ENV, raising=False)
    return h


# --- pure layer ---------------------------------------------------------------------------------


def test_default_project_home_is_a_denied_subtree(home: Path) -> None:
    ap = _project_home(home)
    policy = build_policy(_entity(home))
    assert ap.resolve() in policy.deny_read_write
    assert crown_jewel_reason(policy, ap / "derive-trust.json") is not None
    assert crown_jewel_reason(policy, ap / "levain-v2" / "memory.db") is not None


def test_project_home_is_denied_before_it_exists(home: Path) -> None:
    """A store created after the shell starts must already be covered (the path is denied, not the
    directory that happened to exist)."""
    policy = build_policy(_entity(home))
    assert crown_jewel_reason(policy, home / ".anneal-projects" / "derive-trust.json") is not None


def test_env_trust_moves_the_project_home_and_its_parent_is_denied(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    moved = tmp_path / "elsewhere" / "projects"
    (moved / "levain-v2").mkdir(parents=True)
    trust = moved / "derive-trust.json"
    trust.write_text("{}")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    policy = build_policy(_entity(home))
    assert moved.resolve() in policy.deny_read_write
    assert crown_jewel_reason(policy, moved / "levain-v2" / "memory.db") is not None
    assert crown_jewel_reason(policy, trust) is not None
    # covered both ways by the subtree, so it is not listed again as a write-only file
    assert trust.resolve() not in policy.deny_write_files


def test_env_trust_beside_home_denies_the_file_but_never_home(home: Path, monkeypatch) -> None:
    """A parent that contains the entity's own area is not denied as a subtree (the shell could not
    write its workspace); the file itself is still write-denied."""
    trust = home / "derive-trust.json"
    trust.write_text("{}")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    entity = _entity(home)
    policy = build_policy(entity)
    assert home.resolve() not in policy.deny_read_write
    assert trust.resolve() in policy.deny_write_files
    reason = crown_jewel_reason(policy, trust)
    assert reason is not None and "derive-trust" in reason
    assert crown_jewel_reason(policy, entity / "workspace" / "notes.md") is None


def test_env_trust_symlink_denies_its_lexical_spelling(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    """Replacing the link at the lexical path re-points what anneal reads, so the lexical spelling is
    denied even when the target's directory is a denied subtree."""
    real = tmp_path / "real-projects" / "derive-trust.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = home / "derive-trust.json"
    link.symlink_to(real)
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(link))
    policy = build_policy(_entity(home))
    assert link in policy.deny_write_files
    assert real.parent.resolve() in policy.deny_read_write


def test_empty_env_trust_counts_as_unset(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(DERIVE_TRUST_ENV, "")
    unset = build_policy(_entity(home))
    assert unset.trust_spellings == ()
    assert [p for p in unset.deny_read_write if p.name == ".anneal-projects"]


# --- live layer: a real confined shell --------------------------------------------------------


def _rc(sh, cmd: str) -> int:
    return sh.run(cmd, timeout=20).exit_code


@live
def test_live_confined_shell_cannot_touch_project_memory(home: Path) -> None:
    ap = _project_home(home)
    entity = _entity(home)
    trust = ap / "derive-trust.json"
    before = trust.read_bytes()
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        assert _rc(sh, f"touch '{entity}/workspace/control'") == 0   # CONTROL: the shell writes
        assert _rc(sh, f"head -c 10 '{trust}'") != 0
        assert _rc(sh, f"touch '{ap}/probe'") != 0
        assert _rc(sh, f": >> '{trust}'") != 0
        assert _rc(sh, f"touch '{ap}/levain-v2/probe'") != 0
        assert _rc(sh, f"head -c 10 '{ap}/levain-v2/memory.db'") != 0
    assert trust.read_bytes() == before
    assert not (ap / "probe").exists() and not (ap / "levain-v2" / "probe").exists()


@live
def test_live_confined_shell_cannot_replace_an_env_trust_file(home: Path, monkeypatch) -> None:
    """The fallback case: only the file literal protects it, so a write AND a replace-by-rename onto
    it must both be refused while the shell still writes beside it."""
    trust = home / "derive-trust.json"
    trust.write_text("ORIGINAL")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    entity = _entity(home)
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        assert _rc(sh, f"echo x > '{home}/beside'") == 0              # CONTROL
        assert _rc(sh, f"echo PLANTED > '{trust}'") != 0
        assert _rc(sh, f"mv -f '{home}/beside' '{trust}'") != 0
    assert trust.read_text() == "ORIGINAL"
