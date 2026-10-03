"""The operator's PROJECT memory is in the universal floor (spore-1308, ruled by Phill 2026-10-03).

``~/.anneal-projects`` (project stores + flow's derive-trust file) is denied read+write like
``~/.anneal-memory``; ``$ANNEAL_MEMORY_DERIVE_TRUST``, when set at policy build, has its file
write-denied at every spelling a rename or unlink acts on, and its parent pinned. The live tests
spawn a real confined shell (seatbelt on macOS, bwrap on Linux) and assert a refusal by its text,
against a control write that must succeed."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
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


def _trust(path: Path, *dbs: Path, root: str | None = "/r") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"db": str(d)} | ({"root": root} if root is not None else {}) for d in dbs]
    path.write_text(json.dumps({"version": 2, "stores": entries}))
    # Explicit modes: anneal (and this mirror of it) rejects a group-writable trust file or directory,
    # and a umask of 002 (Ubuntu's default) would make both group-writable.
    path.parent.chmod(0o755)
    path.chmod(0o644)
    return path


def _moved_home(root: Path, home: Path) -> tuple[Path, Path]:
    """A flow project home moved away from ~/.anneal-projects: trust file + one store, listed by db."""
    moved = _project_home(root / "moved-projects")
    return moved, _trust(moved / "derive-trust.json", moved / "levain-v2" / "memory.db", root=str(home))


def test_a_store_listed_in_the_env_trust_file_is_a_denied_subtree(home, tmp_path, monkeypatch):
    moved, trust = _moved_home(tmp_path, home)
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(trust))
    policy = build_policy(_entity(home))
    assert (moved / "levain-v2").resolve() in policy.deny_read_write
    assert crown_jewel_reason(policy, moved / "levain-v2" / "memory.continuity.md") is not None


def test_a_store_listed_in_the_default_trust_file_is_denied_without_the_env(home, tmp_path):
    moved = _project_home(tmp_path / "elsewhere")
    _trust(home / ".anneal-memory" / "derive-trust.json", moved / "levain-v2" / "memory.db")
    policy = build_policy(_entity(home))
    assert (moved / "levain-v2").resolve() in policy.deny_read_write


def test_a_large_trust_file_is_still_honoured(home, tmp_path):
    """anneal has no size cap, so neither may this (codex L3 HIGH: a skipped file left its stores open)."""
    moved = _project_home(tmp_path / "elsewhere")
    t = _trust(home / ".anneal-memory" / "derive-trust.json")
    t.write_text(json.dumps({"version": 2, "stores": [
        {"db": str(moved / "levain-v2" / "memory.db"), "root": "/r", "pad": "x" * (1 << 20)}]}))
    assert t.stat().st_size > 1 << 20
    assert (moved / "levain-v2").resolve() in build_policy(_entity(home)).deny_read_write


@pytest.mark.parametrize("content", ["", "{not json", '{"stores": "x"}', '{"stores": [{"db": 7}]}',
                                     '{"stores": [{"db": "relative/memory.db", "root": "/r"}]}'])
def test_a_bad_trust_file_adds_nothing_and_the_default_deny_holds(home, monkeypatch, content):
    trust = _trust(home / ".anneal-memory" / "derive-trust.json")   # explicit modes, then the content
    trust.write_text(content)
    entity = _entity(home)
    policy = build_policy(entity)
    assert (home / ".anneal-projects").resolve() in policy.deny_read_write
    trust.unlink()
    without = build_policy(entity)
    assert (policy.deny_read_write, policy.deny_files) == (without.deny_read_write, without.deny_files)


def test_entries_and_files_anneal_would_reject_widen_nothing(home, tmp_path):
    """Protect exactly what anneal will trust (codex L3 MED): no root, a group-writable file, a
    symlinked trust file all load no store in anneal, so they must not hide anything here."""
    store = _project_home(tmp_path / "s")
    t = _trust(home / ".anneal-memory" / "derive-trust.json", store / "levain-v2" / "memory.db", root=None)
    entity = _entity(home)
    assert (store / "levain-v2").resolve() not in build_policy(entity).deny_read_write
    _trust(t, store / "levain-v2" / "memory.db")
    t.chmod(0o664)
    assert (store / "levain-v2").resolve() not in build_policy(entity).deny_read_write
    real = _trust(tmp_path / "real-trust.json", store / "levain-v2" / "memory.db")
    t.unlink()
    t.symlink_to(real)
    assert (store / "levain-v2").resolve() not in build_policy(entity).deny_read_write


def test_a_fifo_at_a_trust_path_refuses_without_hanging(home, monkeypatch):
    """anneal would read stores from a FIFO; this cannot without blocking, so it refuses (codex L3 r2)."""
    fifo = home / "trust.fifo"
    os.mkfifo(fifo)
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(fifo))
    with pytest.raises(ConfinementError, match="not a regular file"):
        build_policy(_entity(home))


def test_a_listed_store_whose_directory_is_missing_is_denied_before_it_exists(home, tmp_path):
    """codex L3 r2: skipping it let the entity create the directory and a store anneal trusts."""
    _trust(home / ".anneal-memory" / "derive-trust.json", tmp_path / "future" / "memory.db")
    assert (tmp_path / "future").resolve() in build_policy(_entity(home)).deny_read_write


def test_a_missing_store_inside_the_workspace_still_refuses(home):
    entity = _entity(home)
    _trust(home / ".anneal-memory" / "derive-trust.json", entity / "workspace" / "future" / "memory.db")
    with pytest.raises(ConfinementError, match="directory of its own"):
        build_policy(entity)


def test_a_symlinked_db_denies_its_lexical_directory_too(home, tmp_path):
    real = _project_home(tmp_path / "real")
    lex = tmp_path / "lex"
    lex.mkdir()
    (lex / "memory.db").symlink_to(real / "levain-v2" / "memory.db")
    _trust(home / ".anneal-memory" / "derive-trust.json", lex / "memory.db")
    policy = build_policy(_entity(home))
    assert lex.resolve() in policy.deny_read_write
    assert (real / "levain-v2").resolve() in policy.deny_read_write


@pytest.mark.parametrize("where", ["home", "entity", "workspace", "tmp"])
def test_a_store_that_cannot_be_hidden_whole_refuses_the_floor(home, tmp_path, where):
    """codex L3 HIGH: denying only the db left its continuity writable; anneal writes too many names
    beside a db to deny them one by one, so the store needs a directory of its own."""
    entity = _entity(home)
    d = {"home": home, "entity": entity / "notes", "workspace": entity / "workspace" / "proj",
         "tmp": Path(tempfile.gettempdir())}[where]
    d.mkdir(parents=True, exist_ok=True)
    _trust(home / ".anneal-memory" / "derive-trust.json", d / f"levain-1308-{where}.db")
    with pytest.raises(ConfinementError, match="directory of its own"):
        build_policy(entity)


def test_the_entitys_own_canonical_store_is_left_to_its_memory_rules(home):
    entity = _entity(home)
    _trust(home / ".anneal-memory" / "derive-trust.json", entity / ".levain" / "memory.db")
    policy = build_policy(entity)
    assert (entity / ".levain").resolve() not in policy.deny_read_write


@mac_live
def test_a_case_variant_workspace_spelling_is_still_the_workspace(home):
    """Containment by identity: on case-insensitive APFS a store listed under an upper-cased spelling
    of a workspace outside the entity is the workspace, so it refuses instead of hiding it."""
    entity = _entity(home)
    ws = home / "wsout"
    ws.mkdir()
    _trust(home / ".anneal-memory" / "derive-trust.json", home / "WSOUT" / "memory.db")
    with pytest.raises(ConfinementError, match="directory of its own"):
        build_policy(entity, workspace=ws)


def test_a_store_trusted_after_the_policy_was_built_is_denied_at_the_next_spawn(home, tmp_path):
    from levain.firing.confinement import refresh_socket_denies
    entity = _entity(home)
    policy = build_policy(entity)
    late = _project_home(tmp_path / "late")
    _trust(home / ".anneal-memory" / "derive-trust.json", late / "levain-v2" / "memory.db")
    assert (late / "levain-v2").resolve() not in policy.deny_read_write
    refreshed = refresh_socket_denies(policy)
    assert (late / "levain-v2").resolve() in refreshed.deny_read_write
    assert set(policy.deny_read_write) <= set(refreshed.deny_read_write)
    (home / ".anneal-memory" / "derive-trust.json").unlink()            # the entry goes away...
    assert (late / "levain-v2").resolve() in refresh_socket_denies(refreshed).deny_read_write  # ...kept


def test_the_file_editor_floor_absorbs_a_late_store(home, tmp_path):
    """codex L3 r2: the shared floor merged only socket fields, so a store trusted mid-session was
    denied to bash and left open to the file editor."""
    from levain.firing.confinement import refresh_socket_denies
    from levain.firing.openhands.tools import _SharedFloor
    policy = build_policy(_entity(home))
    floor = _SharedFloor(policy)
    late = _project_home(tmp_path / "late")
    _trust(home / ".anneal-memory" / "derive-trust.json", late / "levain-v2" / "memory.db")
    floor.absorb(refresh_socket_denies(floor.policy))
    assert crown_jewel_reason(floor.policy, late / "levain-v2" / "memory.continuity.md") is not None


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


# --- floor L3 r2 follow-ons: a link INTO a hidden subtree; the filter's ordering ---------------


def _link_into_sibling(home: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A trust link in a directory this user cannot write, pointing at a file inside a sibling
    entity's hidden store (codex's precondition; RUN on argushub at 509a40e: the shell read it)."""
    me = _entity(home)
    sib = home / "entities" / "sibling" / ".levain"
    sib.mkdir(parents=True)
    target = sib / "derive-trust.json"
    target.write_text("SIBLING-SECRET-1308")
    opt = tmp_path / "opt"
    opt.mkdir()
    (opt / "trust.json").symlink_to(target)
    opt.chmod(0o555)
    return me, target, opt


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
def test_a_write_only_link_into_a_hidden_subtree_binds_dev_null_at_its_target(
    home, tmp_path, monkeypatch
):
    from levain.firing.confinement import _bwrap_argv
    me, target, opt = _link_into_sibling(home, tmp_path)
    try:
        monkeypatch.setenv(DERIVE_TRUST_ENV, str(opt / "trust.json"))
        argv = _bwrap_argv(build_policy(me))
        binds = [argv[i:i + 3] for i in range(len(argv) - 2) if argv[i] == "--ro-bind"]
        assert ["--ro-bind", str(target.resolve()), str(target.resolve())] not in binds
        assert ["--ro-bind", "/dev/null", str(target.resolve())] in binds
    finally:
        opt.chmod(0o755)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
@pytest.mark.skipif(not _LINUX, reason="the step (5) self-bind is Linux-only")
def test_live_a_trust_link_into_a_sibling_store_does_not_re_expose_it(home, tmp_path, monkeypatch):
    me, target, opt = _link_into_sibling(home, tmp_path)
    try:
        monkeypatch.setenv(DERIVE_TRUST_ENV, str(opt / "trust.json"))
        with select_provider().spawn_shell(build_policy(me)) as sh:
            r = sh.run(f"cat '{target}' 2>&1", timeout=20)
            assert "SIBLING-SECRET-1308" not in r.output
    finally:
        opt.chmod(0o755)


def test_a_trust_spelling_inside_a_credential_subtree_is_not_listed_again(home, monkeypatch):
    gh = home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "derive-trust.json").write_text("{}")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(gh / "derive-trust.json"))
    policy = build_policy(_entity(home), deny_standard_creds=True)
    assert gh.resolve() in policy.deny_read_write
    assert policy.trust_spellings == ()


def test_a_trust_spelling_equal_to_a_deny_file_is_not_listed_again(home, monkeypatch):
    t = home / "secrets" / "derive-trust.json"
    t.parent.mkdir()
    t.write_text("{}")
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(t))
    policy = build_policy(_entity(home), deny_files=(t,))
    assert policy.trust_spellings == ()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
def test_a_write_only_link_to_a_read_denied_file_never_self_binds_it(home, tmp_path, monkeypatch):
    """codex + glm L3 (floor-r2 r1): a trust link in a non-writable directory pointing at a deny_files
    entry was self-bound on top of that entry's /dev/null mask, making the secret readable."""
    from levain.firing.confinement import _bwrap_argv
    secret = tmp_path / "secrets" / "token"
    secret.parent.mkdir()
    secret.write_text("SECRET")
    opt = tmp_path / "opt"
    opt.mkdir()
    (opt / "trust.json").symlink_to(secret)
    opt.chmod(0o555)
    try:
        monkeypatch.setenv(DERIVE_TRUST_ENV, str(opt / "trust.json"))
        argv = _bwrap_argv(build_policy(_entity(home), deny_files=(secret,)))
        binds = [argv[i:i + 3] for i in range(len(argv) - 2) if argv[i] == "--ro-bind"]
        t = str(secret.resolve())
        assert ["--ro-bind", t, t] not in binds
        assert ["--ro-bind", "/dev/null", t] in binds
    finally:
        opt.chmod(0o755)


@pytest.mark.skipif(not _LINUX, reason="the step (5) self-bind is Linux-only")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
def test_live_a_trust_link_to_a_denied_file_does_not_expose_it(home, tmp_path, monkeypatch):
    secret = tmp_path / "secrets" / "token"
    secret.parent.mkdir()
    secret.write_text("DENIED-SECRET-1308")
    opt = tmp_path / "opt"
    opt.mkdir()
    (opt / "trust.json").symlink_to(secret)
    opt.chmod(0o555)
    try:
        monkeypatch.setenv(DERIVE_TRUST_ENV, str(opt / "trust.json"))
        with select_provider().spawn_shell(build_policy(_entity(home), deny_files=(secret,))) as sh:
            assert "DENIED-SECRET-1308" not in sh.run(f"cat '{secret}' 2>&1", timeout=20).output
            assert "DENIED-SECRET-1308" not in sh.run(f"cat '{opt}/trust.json' 2>&1", timeout=20).output
    finally:
        opt.chmod(0o755)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
def test_a_jewel_retargeted_after_its_mask_does_not_unmask_the_write_only_target(
    home, tmp_path, monkeypatch
):
    """codex L3 (floor-r2 r2): step (5) re-resolved the deny_files paths instead of using what step
    (4) emitted. A jewel path that resolves elsewhere between the two observations left the masked
    file off the guard list, so a write-only link to it was self-bound on top of its /dev/null mask."""
    from levain.firing import confinement
    a = tmp_path / "secrets" / "a"
    b = tmp_path / "secrets" / "b"
    a.parent.mkdir()
    a.write_text("SECRET-A")
    b.write_text("SECRET-B")
    jewel = a.resolve()   # build_policy stores deny_files resolved
    opt = tmp_path / "opt"
    opt.mkdir()
    (opt / "trust.json").symlink_to(a)
    opt.chmod(0o555)
    real_target = confinement._bwrap_file_target
    real_resolve = Path.resolve
    masked = []

    def target(f):
        out = real_target(f)
        if f == jewel:
            masked.append(out)
        return out

    def resolve(self, *args, **kwargs):
        if masked and self == jewel:
            return b.resolve()   # the jewel's path now leads elsewhere (swapped mid-plan)
        return real_resolve(self, *args, **kwargs)

    try:
        monkeypatch.setenv(DERIVE_TRUST_ENV, str(opt / "trust.json"))
        policy = build_policy(_entity(home), deny_files=(jewel,))
        monkeypatch.setattr(confinement, "_bwrap_file_target", target)
        monkeypatch.setattr(Path, "resolve", resolve)
        argv = confinement._bwrap_argv(policy)
        monkeypatch.undo()
        assert masked == [jewel]
        binds = [argv[i:i + 3] for i in range(len(argv) - 2) if argv[i] == "--ro-bind"]
        t = str(a.resolve())
        assert ["--ro-bind", t, t] not in binds
        assert ["--ro-bind", "/dev/null", t] in binds
    finally:
        opt.chmod(0o755)


def _case_insensitive(d: Path) -> bool:
    probe = d / "CaseProbe"
    probe.write_text("")
    try:
        return (d / "caseprobe").exists()
    finally:
        probe.unlink()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
def test_a_case_variant_link_to_a_read_denied_file_never_self_binds_it(home, tmp_path, monkeypatch):
    """codex L3 (floor-r2 r2): on a case-insensitive volume a write-only link spelled `TOKEN` names
    the same dentry as the deny_files entry `token`; an exact compare missed it and self-bound it."""
    from levain.firing.confinement import _bwrap_argv
    secret = tmp_path / "secrets" / "token"
    secret.parent.mkdir()
    if not _case_insensitive(secret.parent):
        pytest.skip("needs a case-insensitive volume")
    secret.write_text("SECRET")
    variant = secret.parent / "TOKEN"
    opt = tmp_path / "opt"
    opt.mkdir()
    (opt / "trust.json").symlink_to(variant)
    opt.chmod(0o555)
    try:
        monkeypatch.setenv(DERIVE_TRUST_ENV, str(opt / "trust.json"))
        argv = _bwrap_argv(build_policy(_entity(home), deny_files=(secret,)))
        binds = [argv[i:i + 3] for i in range(len(argv) - 2) if argv[i] == "--ro-bind"]
        assert not any(s == d and s.casefold().endswith("/secrets/token") for _, s, d in binds), binds
    finally:
        opt.chmod(0o755)
