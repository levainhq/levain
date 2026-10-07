"""The autonomic engine's store directory is in the universal floor.

The binding registry, the run journal, their SQLite sidecars and the journal's effect leases live in
one directory (``levain.autonomic.db``), ``<levain home>/autonomic``. The floor denies that directory
read and write like ``~/.anneal-memory``, in every policy the drive modes build, before it exists, and
at ``$LEVAIN_HOME`` when that is set. Because the jewel is a DIRECTORY, bash is not refused on its
account (the SQLite plant refusal is for a database that sits in a writable directory)."""

from __future__ import annotations

import platform
import sqlite3
from pathlib import Path

import pytest

from levain.firing.confinement import (
    AUTONOMIC_STORE_DIR,
    SeatbeltProvider,
    _bwrap_argv,
    _refuse_plantable_sqlite_jewels,
    build_policy,
    bwrap_available,
    crown_jewel_reason,
    sandbox_exec_available,
    select_provider,
)

_MAC = platform.system() == "Darwin" and sandbox_exec_available()
_LINUX = platform.system() == "Linux" and bwrap_available()
live = pytest.mark.skipif(not (_MAC or _LINUX), reason="needs macOS sandbox-exec or a working bwrap")
_REFUSALS = ("Operation not permitted", "Read-only file system", "Permission denied",
             "Device or resource busy") + (("No such file or directory",) if _LINUX else ())


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    return h


def _entity(root: Path) -> Path:
    d = root / "entities" / "coyote"
    (d / ".levain").mkdir(parents=True)
    (d / "workspace").mkdir()
    return d


def _store(at: Path) -> Path:
    """A real store: the database in WAL mode, with its sidecars open beside it."""
    at.mkdir(parents=True)
    conn = sqlite3.connect(at / "autonomic.db")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE fences (binding_id TEXT, generation INTEGER)")
    conn.execute("INSERT INTO fences VALUES ('b', 1)")
    conn.commit()
    conn.close()
    return at


def _denied(policy, store: Path) -> None:
    assert store.resolve() in policy.deny_read_write
    for name in ("autonomic.db", "autonomic.db-wal", "autonomic.db-shm", "leases/x.lock"):
        assert crown_jewel_reason(policy, store / name) is not None, name


@pytest.mark.parametrize("deny_standard_creds", [False, True])
def test_the_store_directory_is_a_denied_subtree_in_both_builds(home, deny_standard_creds):
    store = home / ".levain" / AUTONOMIC_STORE_DIR
    _denied(build_policy(_entity(home), deny_standard_creds=deny_standard_creds), store)


@pytest.mark.parametrize("mode", ["interactive", "unattended"])
def test_the_floor_each_drive_mode_resolves_denies_the_store(home, mode):
    from levain.firing.binding import ConversationBinding

    ent = _entity(home)
    binding = ConversationBinding.create(ent, mode=mode, workspace=ent / "workspace")
    _denied(ConversationBinding.from_params(binding.to_params()).floor, home / ".levain" / AUTONOMIC_STORE_DIR)


def test_the_store_is_denied_at_levain_home_when_it_is_set(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "levain-home"
    monkeypatch.setenv("LEVAIN_HOME", str(elsewhere))
    _denied(build_policy(_entity(home)), elsewhere / AUTONOMIC_STORE_DIR)


def test_the_floor_names_the_directory_db_py_creates(home, monkeypatch):
    from levain.autonomic.db import default_store_dir

    policy = build_policy(_entity(home))
    assert default_store_dir().resolve() in policy.deny_read_write


def test_both_enforcers_render_the_store_as_a_denied_tree(home):
    store = _store(home / ".levain" / AUTONOMIC_STORE_DIR).resolve()
    policy = build_policy(_entity(home))
    assert f'(subpath "{store}")' in SeatbeltProvider().render_profile(policy)
    argv = _bwrap_argv(policy)
    assert any(argv[i] == "--tmpfs" and argv[i + 1] == str(store) for i in range(len(argv) - 1))


def test_bash_is_not_refused_on_account_of_the_store(home):
    # the plant refusal is for a FILE-shaped SQLite jewel; this one is a directory holding a live database
    _store(home / ".levain" / AUTONOMIC_STORE_DIR)
    _refuse_plantable_sqlite_jewels(build_policy(_entity(home)))      # does not raise


@live
def test_live_confined_shell_cannot_touch_the_store_and_still_runs(home):
    store = _store(home / ".levain" / AUTONOMIC_STORE_DIR)
    entity = _entity(home)
    before = (store / "autonomic.db").read_bytes()
    with select_provider().spawn_shell(build_policy(entity)) as sh:       # bash starts: not refused
        ok = sh.run(f"touch '{entity}/workspace/control' 2>&1", timeout=20)
        assert ok.exit_code == 0, ok.output
        for cmd in (f"head -c 16 '{store}/autonomic.db'", f"ls '{store}'", f"touch '{store}/planted-wal'",
                    f": >> '{store}/autonomic.db'", f"touch '{store}/autonomic.db-wal'"):
            r = sh.run(cmd + " 2>&1", timeout=20)
            assert r.exit_code != 0, f"{cmd!r} succeeded: {r.output!r}"
            assert any(m in r.output for m in _REFUSALS), f"{cmd!r} failed for another reason: {r.output!r}"
    assert (store / "autonomic.db").read_bytes() == before
    assert not (store / "planted-wal").exists()
