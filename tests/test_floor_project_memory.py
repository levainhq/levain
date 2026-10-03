"""The operator's PROJECT memory is in the universal floor (spore-1308, ruled by Phill 2026-10-03).

``~/.anneal-projects`` (project stores + flow's derive-trust file) is denied read+write like
``~/.anneal-memory``; ``$ANNEAL_MEMORY_DERIVE_TRUST``, when set at policy build, has its file
write-denied at every spelling a rename or unlink acts on, and its parent pinned. The live tests
spawn a real confined shell (seatbelt on macOS, bwrap on Linux) and assert a refusal by its text,
against a control write that must succeed."""

from __future__ import annotations

import dataclasses
import json
import platform
from pathlib import Path

import pytest

from levain.firing.confinement import (
    DERIVE_TRUST_ENV,
    ConfinementError,
    CrownJewelsPolicy,
    build_policy,
    bwrap_available,
    crown_jewel_reason,
    sandbox_exec_available,
    select_provider,
)

_MAC = platform.system() == "Darwin" and sandbox_exec_available()
_LINUX = platform.system() == "Linux" and bwrap_available()
live = pytest.mark.skipif(not (_MAC or _LINUX), reason="needs macOS sandbox-exec or a working bwrap")
mac_live = pytest.mark.skipif(not _MAC, reason="needs macOS sandbox-exec")

# What each OS says when the floor refuses: seatbelt EPERM; bwrap EROFS on a read-only mount,
# EACCES on a /dev/null bind, EBUSY on a mountpoint rename or unlink, and ENOENT inside a jewel
# tmpfs. ENOENT counts only on Linux: on macOS it would mean the probed path was never there.
_REFUSALS = ("Operation not permitted", "Read-only file system", "Permission denied",
             "Device or resource busy") + (("No such file or directory",) if _LINUX else ())


def _entity(root: Path) -> Path:
    d = root / "entities" / "coyote"
    (d / ".levain").mkdir(parents=True)
    (d / "workspace").mkdir()
    return d


def _project_home(at: Path) -> Path:
    (at / "levain-v2").mkdir(parents=True)
    (at / "derive-trust.json").write_text('{"version": 2, "labels": {}}\n')
    (at / "levain-v2" / "memory.db").write_text("PROJECT MEMORY — must never be read by an entity")
    return at


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv(DERIVE_TRUST_ENV, raising=False)
    return h


def _refused(sh, cmd: str) -> None:
    r = sh.run(cmd + " 2>&1", timeout=20)
    assert r.exit_code != 0, f"{cmd!r} succeeded: {r.output!r}"
    assert any(m in r.output for m in _REFUSALS), f"{cmd!r} failed for another reason: {r.output!r}"


def _ok(sh, cmd: str) -> None:
    r = sh.run(cmd + " 2>&1", timeout=20)
    assert r.exit_code == 0, f"CONTROL {cmd!r} failed: {r.output!r}"


# --- pure layer ---------------------------------------------------------------------------------


def test_policy_field_order_is_frozen() -> None:
    """A field inserted mid-dataclass shifts every later field for a positional caller; it happened
    twice before this test existed. New fields go at the end, so the existing prefix never moves."""
    names = [f.name for f in dataclasses.fields(CrownJewelsPolicy)]
    assert names[:17] == [
        "entity_dir", "workspace", "deny_read_write", "deny_files", "deny_write_dirs", "ssh_dir",
        "ssh_mode", "deny_write_files", "config_file", "deny_sockets", "socket_spellings",
        "own_memory_files", "socket_sources", "deny_keychain", "deny_localhost_outbound",
        "sqlite_sidecars", "trust_spellings",
    ]


def test_default_project_home_is_a_denied_subtree(home: Path) -> None:
    ap = _project_home(home / ".anneal-projects")
    policy = build_policy(_entity(home))
    assert ap.resolve() in policy.deny_read_write
    assert crown_jewel_reason(policy, ap / "derive-trust.json") is not None
    assert crown_jewel_reason(policy, ap / "levain-v2" / "memory.db") is not None


def test_project_home_is_denied_before_it_exists(home: Path) -> None:
    """A store created after the shell starts must already be covered (the path is denied, not the
    directory that happened to exist)."""
    policy = build_policy(_entity(home))
    assert crown_jewel_reason(policy, home / ".anneal-projects" / "derive-trust.json") is not None


def test_env_trust_inside_the_project_home_is_not_listed_again(home: Path, monkeypatch) -> None:
    ap = _project_home(home / ".anneal-projects")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(ap / "derive-trust.json"))
    policy = build_policy(_entity(home))
    assert policy.trust_spellings == ()
    assert crown_jewel_reason(policy, ap / "derive-trust.json") is not None


def test_env_trust_elsewhere_is_write_denied_and_its_directory_pinned_not_hidden(
    home: Path, monkeypatch
) -> None:
    trust = home / "derive-trust.json"
    trust.write_text("{}")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    entity = _entity(home)
    policy = build_policy(entity)
    assert trust.resolve() in policy.deny_write_files
    assert home.resolve() in policy.deny_write_dirs           # its parent: the ancestor pin
    assert home.resolve() not in policy.deny_read_write       # never hidden as a subtree
    reason = crown_jewel_reason(policy, trust)
    assert reason is not None and "derive-trust" in reason
    assert crown_jewel_reason(policy, entity / "workspace" / "notes.md") is None


def test_env_trust_link_under_a_symlinked_directory_denies_the_links_own_location(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    """L1 HIGH-2 (run): seatbelt resolves symlinked parent directories, so the literal as given never
    matches, while rm/rename act on the link itself at <real parent>/<name>."""
    target = tmp_path / "target" / "derive-trust.json"
    target.parent.mkdir()
    target.write_text("{}")
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    (real_dir / "derive-trust.json").symlink_to(target)
    (tmp_path / "via").symlink_to(real_dir)
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(tmp_path / "via" / "derive-trust.json"))
    policy = build_policy(_entity(home))
    assert (real_dir.resolve() / "derive-trust.json") in policy.deny_write_files
    assert target.resolve() in policy.deny_write_files


@pytest.mark.parametrize("store", [".anneal-projects", ".anneal-memory"])
def test_a_symlinked_store_link_is_pinned(home: Path, tmp_path: Path, store: str) -> None:
    real = tmp_path / f"real{store}"
    real.mkdir()
    (home / store).symlink_to(real)
    policy = build_policy(_entity(home))
    assert home.resolve() / store in policy.deny_write_dirs


def test_env_trust_inside_a_sibling_entity_store_is_not_listed_again(home: Path, monkeypatch) -> None:
    sibling = home / "entities" / "other" / ".levain"
    sibling.mkdir(parents=True)
    (sibling / "derive-trust.json").write_text("{}")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(sibling / "derive-trust.json"))
    policy = build_policy(_entity(home))
    assert sibling.resolve() in policy.deny_read_write
    assert policy.trust_spellings == ()


def test_an_unresolvable_env_trust_refuses_the_floor(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(DERIVE_TRUST_ENV, "~no_such_user_levain_1308/derive-trust.json")
    with pytest.raises(ConfinementError, match=DERIVE_TRUST_ENV):
        build_policy(_entity(home))


def test_empty_env_trust_counts_as_unset(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(DERIVE_TRUST_ENV, "")
    policy = build_policy(_entity(home))
    assert policy.trust_spellings == ()


# --- live layer: a real confined shell --------------------------------------------------------


@live
def test_live_confined_shell_cannot_touch_project_memory(home: Path) -> None:
    ap = _project_home(home / ".anneal-projects")
    entity = _entity(home)
    trust = ap / "derive-trust.json"
    before = trust.read_bytes()
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        _ok(sh, f"touch '{entity}/workspace/control'")
        _refused(sh, f"head -c 10 '{trust}'")
        _refused(sh, f"touch '{ap}/probe'")
        _refused(sh, f": >> '{trust}'")
        _refused(sh, f"touch '{ap}/levain-v2/probe'")
        _refused(sh, f"head -c 10 '{ap}/levain-v2/memory.db'")
    assert trust.read_bytes() == before
    assert not (ap / "probe").exists() and not (ap / "levain-v2" / "probe").exists()


@live
def test_live_confined_shell_cannot_replace_an_env_trust_file(home: Path, monkeypatch) -> None:
    """Only the file literal protects it here, so a write, a replace-by-rename onto it and an unlink
    must all be refused while the shell still writes beside it."""
    trust = home / "derive-trust.json"
    trust.write_text("ORIGINAL")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    with select_provider().spawn_shell(build_policy(_entity(home))) as sh:
        _ok(sh, f"echo x > '{home}/beside'")
        _refused(sh, f"echo PLANTED > '{trust}'")
        _refused(sh, f"mv -f '{home}/beside' '{trust}'")
        _refused(sh, f"rm -f '{trust}'")
    assert trust.read_text() == "ORIGINAL"


@mac_live
def test_live_env_trust_link_under_a_symlinked_directory_cannot_be_replaced(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    """L1 HIGH-2's repro, as a test. Linux is not run here: a symlinked directory in the env path
    under a writable parent refuses bash there (the step (1) pin rule)."""
    target = tmp_path / "target" / "derive-trust.json"
    target.parent.mkdir()
    target.write_text("{}")
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    link = real_dir / "derive-trust.json"
    link.symlink_to(target)
    (tmp_path / "via").symlink_to(real_dir)
    given = tmp_path / "via" / "derive-trust.json"
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(given))
    with select_provider().spawn_shell(build_policy(_entity(home))) as sh:
        _ok(sh, f"touch '{real_dir}/control'")
        _refused(sh, f"rm -f '{given}'")
        _refused(sh, f"rm -f '{link}'")
    assert link.is_symlink() and target.read_text() == "{}"


@pytest.mark.parametrize("store", [".anneal-projects", ".anneal-memory"])
@live
def test_live_a_symlinked_store_cannot_be_swapped_for_a_planted_tree(
    home: Path, tmp_path: Path, store: str
) -> None:
    """L2 MED (run on macOS before the fix: rm of the link, then a planted trust file and continuity).
    macOS refuses the unlink; Linux refuses to start bash while the store is a symlink."""
    real = _project_home(tmp_path / f"real{store}")
    link = home / store
    link.symlink_to(real)
    policy = build_policy(_entity(home))
    if _LINUX:
        with pytest.raises(ConfinementError, match="symlink"):
            select_provider().spawn_shell(policy)
        return
    with select_provider().spawn_shell(policy) as sh:
        _ok(sh, f"touch '{home}/control'")
        _refused(sh, f"rm '{link}'")
        _refused(sh, f"mv '{link}' '{home}/moved'")
    assert link.is_symlink() and (real / "derive-trust.json").exists()


# --- stores the trust files list (a moved project home) ----------------------------------------


def _moved_home(root: Path, home: Path) -> tuple[Path, Path]:
    """A flow project home moved away from ~/.anneal-projects: trust file + one store, listed by db."""
    moved = _project_home(root / "moved-projects")
    trust = moved / "derive-trust.json"
    trust.write_text(json.dumps({"version": 2, "stores": [
        {"db": str(moved / "levain-v2" / "memory.db"), "root": str(home)}]}))
    return moved, trust


def test_a_store_listed_in_the_env_trust_file_is_a_denied_subtree(home, tmp_path, monkeypatch):
    moved, trust = _moved_home(tmp_path, home)
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    policy = build_policy(_entity(home))
    assert (moved / "levain-v2").resolve() in policy.deny_read_write
    assert crown_jewel_reason(policy, moved / "levain-v2" / "memory.continuity.md") is not None


def test_a_store_listed_in_the_default_trust_file_is_denied_without_the_env(home, tmp_path):
    """anneal's own default trust file is read too, so a store it lists anywhere is covered."""
    moved = _project_home(tmp_path / "elsewhere")
    (home / ".anneal-memory").mkdir()
    (home / ".anneal-memory" / "derive-trust.json").write_text(json.dumps(
        {"version": 2, "stores": [{"db": str(moved / "levain-v2" / "memory.db"), "root": "/r"}]}))
    policy = build_policy(_entity(home))
    assert (moved / "levain-v2").resolve() in policy.deny_read_write


@pytest.mark.parametrize("content", ["", "{not json", '{"stores": "x"}', '{"stores": [{"db": 7}]}',
                                     '{"stores": [{"db": "relative/memory.db"}]}'])
def test_a_bad_trust_file_adds_nothing_and_the_default_deny_holds(home, monkeypatch, content):
    (home / ".anneal-memory").mkdir()
    trust = home / ".anneal-memory" / "derive-trust.json"
    trust.write_text(content)
    entity = _entity(home)
    policy = build_policy(entity)
    assert (home / ".anneal-projects").resolve() in policy.deny_read_write
    trust.unlink()
    without = build_policy(entity)
    assert (policy.deny_read_write, policy.deny_files) == (without.deny_read_write, without.deny_files)


def test_a_store_beside_home_denies_only_its_db_never_home(home):
    (home / ".anneal-memory").mkdir()
    (home / ".anneal-memory" / "derive-trust.json").write_text(json.dumps(
        {"version": 2, "stores": [{"db": str(home / "memory.db"), "root": "/r"}]}))
    entity = _entity(home)
    policy = build_policy(entity)
    assert home.resolve() not in policy.deny_read_write
    assert (home / "memory.db").resolve() in policy.deny_files
    assert crown_jewel_reason(policy, entity / "workspace" / "notes.md") is None


def test_a_store_inside_the_entity_is_left_to_its_own_memory_rules(home):
    entity = _entity(home)
    (home / ".anneal-memory").mkdir()
    (home / ".anneal-memory" / "derive-trust.json").write_text(json.dumps(
        {"version": 2, "stores": [{"db": str(entity / ".levain" / "memory.db"), "root": "/r"}]}))
    policy = build_policy(entity)
    assert (entity / ".levain").resolve() not in policy.deny_read_write


@mac_live
def test_a_case_variant_workspace_spelling_is_still_the_workspace(home):
    """Containment by identity (L1 MED-4 on the first cut): on case-insensitive APFS a db listed under
    an upper-cased spelling of a workspace outside the entity must not hide that workspace."""
    entity = _entity(home)
    ws = home / "wsout"
    ws.mkdir()
    variant = home / "WSOUT"
    (home / ".anneal-memory").mkdir()
    (home / ".anneal-memory" / "derive-trust.json").write_text(json.dumps(
        {"version": 2, "stores": [{"db": str(variant / "memory.db"), "root": "/r"}]}))
    policy = build_policy(entity, workspace=ws)
    assert crown_jewel_reason(policy, ws / "notes.md") is None


@live
def test_live_a_moved_project_home_found_through_its_trust_file_is_refused(
    home, tmp_path, monkeypatch
):
    moved, trust = _moved_home(tmp_path, home)
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    entity = _entity(home)
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        _ok(sh, f"touch '{entity}/workspace/control'")
        _refused(sh, f"head -c 10 '{moved}/levain-v2/memory.db'")
        _refused(sh, f"touch '{moved}/levain-v2/probe'")
        _refused(sh, f"echo PLANT >> '{moved}/levain-v2/memory.continuity.md'")
        _refused(sh, f": >> '{trust}'")
    assert not (moved / "levain-v2" / "probe").exists()
