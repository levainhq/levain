"""The autonomic engine's store directory is in the universal floor.

The binding registry, the run journal, their SQLite sidecars and the journal's effect leases live in
one directory (``levain.autonomic.db``), ``<levain home>/autonomic``. The floor denies that directory
read and write like ``~/.anneal-memory``, in every policy the drive modes build, before it exists, and
at ``$LEVAIN_HOME`` when that is set. Because the jewel is a DIRECTORY, bash is not refused on its
account (the SQLite plant refusal is for a database that sits in a writable directory)."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
from pathlib import Path

import pytest

from levain.firing.confinement import (
    AUTONOMIC_STORE_DIR,
    COCKPIT_KEY_DIR,
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


def _covered(p: Path, policy) -> bool:
    """``p`` lies under a subtree the floor denies read and write (the levain home since 2026-10-16)."""
    return any(p.resolve().is_relative_to(d) for d in policy.deny_read_write)


def _denied(policy, store: Path) -> None:
    assert _covered(store, policy)
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
    assert _covered(default_store_dir(), policy)


@pytest.mark.parametrize("levain_home", [False, True])
def test_the_floor_denies_the_cockpit_signing_key_directory(home, tmp_path, monkeypatch, levain_home):
    """Diogenes MEDIUM 2026-10-13: ``levain cockpit key create`` writes the T2/T3 signing keys under
    ``<levain home>/keys`` and the floor did not name it, so bash could copy a passphrase key or swap
    the key, ``.pub`` and sidecar before ``enroll`` (RUN: crown_jewel_reason returned None)."""
    from levain.cockpit.custody import default_key_dir

    if levain_home:
        monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))
    keys = default_key_dir()
    assert keys.name == COCKPIT_KEY_DIR
    policy = build_policy(_entity(home))
    assert _covered(keys, policy)
    for name in ("levain-x", "levain-x.pub", "levain-x.levain-key.json"):
        assert crown_jewel_reason(policy, keys / name) is not None, name


def test_a_relative_levain_home_names_one_store_whatever_the_cwd_does(home, tmp_path, monkeypatch):
    # complement LOW (slice L3 r1): the floor resolved a relative $LEVAIN_HOME at policy time, the store
    # left it relative, so a later chdir moved the store away from the directory the floor denies
    from levain.autonomic.db import default_store_dir

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", "rel-home")
    policy = build_policy(_entity(home))
    store = default_store_dir()
    monkeypatch.chdir(home)
    assert store.is_absolute() and _covered(store, policy)


def test_both_enforcers_render_the_store_as_a_denied_tree(home):
    _store(home / ".levain" / AUTONOMIC_STORE_DIR)
    lh = (home / ".levain").resolve()       # the store is denied as part of the whole levain home
    policy = build_policy(_entity(home))
    assert f'(subpath "{lh}")' in SeatbeltProvider().render_profile(policy)
    argv = _bwrap_argv(policy)
    assert any(argv[i] == "--tmpfs" and argv[i + 1] == str(lh) for i in range(len(argv) - 1))


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


# ── the whole levain home (B1 L2 item 3, 2026-10-16) ───────────────────────────────────────────────────
# The floor named <levain home>/autonomic and /keys one at a time and missed /consent, so a confined
# entity could read every consent request and rewrite claude_code.json's ``cockpit_data_root``. The
# floor now denies the levain home as ONE subtree, so a new operator-state directory is covered the day
# it is created.




@pytest.mark.parametrize("levain_home", [False, True])
def test_the_consent_queue_is_denied_read_and_write(home, tmp_path, monkeypatch, levain_home):
    from levain.consent.adapters.claude_code import SETTINGS_NAME
    from levain.consent.queue import DB_NAME, default_queue_dir

    if levain_home:
        monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))
    queue = default_queue_dir()
    policy = build_policy(_entity(home))
    for name in (SETTINGS_NAME, DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"):
        assert crown_jewel_reason(policy, queue / name) is not None, name
    assert _covered(queue, policy)


def test_a_directory_levain_adds_later_is_denied_by_the_class(home):
    """The positive control for the class: a subdirectory name no module uses yet is covered too."""
    policy = build_policy(_entity(home))
    for name in ("some-future-state/thing.json", "consent/claude_code.json", "anything.db"):
        assert crown_jewel_reason(policy, home / ".levain" / name) is not None, name
    assert (home / ".levain").resolve() in policy.deny_read_write


@pytest.mark.parametrize("spelling", ["unset", "empty", "absolute", "relative"])
def test_the_levain_home_resolves_as_its_readers_do(home, tmp_path, monkeypatch, spelling):
    from levain.autonomic.db import default_store_dir
    from levain.consent.queue import default_queue_dir

    monkeypatch.chdir(tmp_path)
    if spelling == "empty":
        monkeypatch.setenv("LEVAIN_HOME", "")
    elif spelling == "absolute":
        monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "abs-home"))
    elif spelling == "relative":
        monkeypatch.setenv("LEVAIN_HOME", "rel-home")
    policy = build_policy(_entity(home))
    queue, store = default_queue_dir(), default_store_dir()
    monkeypatch.chdir(home)
    assert queue.parent == store.parent
    assert queue.parent in policy.deny_read_write and _covered(store, policy)


def test_both_enforcers_render_the_levain_home_as_a_denied_tree(home):
    lh = (home / ".levain")
    lh.mkdir()
    policy = build_policy(_entity(home))
    assert f'(subpath "{lh.resolve()}")' in SeatbeltProvider().render_profile(policy)
    argv = _bwrap_argv(policy)
    assert any(argv[i] == "--tmpfs" and argv[i + 1] == str(lh.resolve()) for i in range(len(argv) - 1))


@pytest.mark.parametrize("where", ["entity", "workspace", "home"])
def test_a_levain_home_that_overlaps_the_entity_falls_back_to_the_named_dirs(home, tmp_path, monkeypatch, where):
    """A levain home holding the entity (an install living AT ~/.levain), its workspace or $HOME cannot be
    hidden whole: the floor names the state dirs instead (the consent queue among them) and leaves the
    entity, its workspace and $HOME visible, rather than refusing a layout that ran before this change."""
    from levain.firing.confinement import LEVAIN_HOME_STATE_DIRS

    lh = tmp_path / "lh"
    ent = _entity(lh) if where == "entity" else _entity(home)
    ws = (lh / "ws") if where == "workspace" else None
    if ws is not None:
        ws.mkdir(parents=True)
    root = home if where == "home" else lh
    monkeypatch.setenv("LEVAIN_HOME", str(root))
    policy = build_policy(ent, workspace=ws)
    assert "consent" in LEVAIN_HOME_STATE_DIRS
    for name in LEVAIN_HOME_STATE_DIRS:
        assert (root / name).resolve() in policy.deny_read_write, name
        assert crown_jewel_reason(policy, root / name / "x.json") is not None, name
    assert root.resolve() not in policy.deny_read_write
    for visible in (ent, home, *( [ws] if ws is not None else [])):
        assert not _covered(visible, policy), visible


def test_an_install_at_dot_levain_run_as_an_entity_still_builds_its_floor(home, monkeypatch):
    """The real layout (an install at ~/.levain, launched from ~): the entity IS the levain home."""
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    ent = home / ".levain"
    (ent / ".levain").mkdir(parents=True)
    (ent / "workspace").mkdir()
    policy = build_policy(ent)
    assert crown_jewel_reason(policy, ent / "consent" / "claude_code.json") is not None
    assert crown_jewel_reason(policy, ent / "keys" / "levain-x") is not None
    assert not _covered(ent, policy) and not _covered(ent / "workspace", policy)


def test_a_named_dir_that_is_the_entity_refuses_the_floor(home, tmp_path, monkeypatch):
    from levain.firing.confinement import ConfinementError

    lh = tmp_path / "lh"
    ent = _entity(lh / "consent")
    monkeypatch.setenv("LEVAIN_HOME", str(lh))
    with pytest.raises(ConfinementError, match="levain directory"):
        build_policy(ent)


def test_a_levain_home_inside_the_entity_is_denied_whole(home, monkeypatch):
    """Hiding a directory inside the entity hides nothing the entity needs, so the whole home is denied
    (L3 r1: the stricter trust-store test had dropped this layout to the by-name fallback)."""
    ent = _entity(home)
    monkeypatch.setenv("LEVAIN_HOME", str(ent / "state"))
    policy = build_policy(ent)
    assert (ent / "state").resolve() in policy.deny_read_write
    assert not _covered(ent, policy) and not _covered(ent / "workspace", policy)


def test_every_levain_home_reader_is_named_for_the_fallback():
    """The fallback names directories one at a time, so it must know every one: each module that reads
    ``$LEVAIN_HOME`` (outside the floor itself) builds one directory under it, and that directory's name
    must be in :data:`LEVAIN_HOME_STATE_DIRS`. A new reader fails here until it is added."""
    import re
    import levain
    from levain.autonomic.db import default_store_dir
    from levain.cockpit.custody import default_key_dir
    from levain.consent.queue import default_queue_dir
    from levain.firing.confinement import LEVAIN_HOME_STATE_DIRS

    pkg = Path(levain.__file__).parent
    readers = sorted(
        str(p.relative_to(pkg)) for p in pkg.rglob("*.py")
        if p.name != "confinement.py" and re.search(r"""(?:environ(?:\.get\(|\[)|getenv\()\s*["']LEVAIN_HOME["']""",
                                                    p.read_text(encoding="utf-8"))
    )
    assert readers == ["autonomic/db.py", "cockpit/custody.py", "consent/queue.py"], readers
    names = {default_store_dir().name, default_key_dir().name, default_queue_dir().name}
    assert names == set(LEVAIN_HOME_STATE_DIRS)


def test_a_symlinked_levain_home_is_denied_at_the_link_and_its_target(home, tmp_path):
    real = tmp_path / "real-levain"
    real.mkdir()
    (home / ".levain").symlink_to(real)
    policy = build_policy(_entity(home))
    assert (home / ".levain") in policy.deny_read_write          # the link itself: rm/mv of it
    assert real.resolve() in policy.deny_read_write
    assert crown_jewel_reason(policy, real / "consent" / "claude_code.json") is not None


@live
def test_live_confined_shell_cannot_plant_an_absent_levain_home(home):
    """While the levain home does not exist, the entity cannot create it (or a case variant of its name,
    which a case-insensitive volume would hand to the operator's later lookups) and plant settings."""
    assert not (home / ".levain").exists()
    entity = _entity(home)
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        ok = sh.run(f"touch '{entity}/workspace/control' 2>&1", timeout=20)
        assert ok.exit_code == 0, ok.output
        for spelling in (".levain", ".Levain", ".LEVAIN"):
            r = sh.run(f"mkdir -p '{home}/{spelling}/consent' 2>&1", timeout=20)
            assert r.exit_code != 0, f"{spelling}: {r.output!r}"
    assert not any(p.name.lower() == ".levain" for p in home.iterdir())


def _name_cockpit(queue: Path, settings_name: str, root: Path) -> None:
    queue.mkdir(parents=True, exist_ok=True)
    (queue / settings_name).write_text(json.dumps({"cockpit_data_root": str(root)}), encoding="utf-8")


@pytest.mark.parametrize("adapter", ["claude_code", "codex"])
def test_the_cockpit_root_the_consent_settings_name_is_denied(home, tmp_path, adapter):
    """The authority log and its anchor live in the install's cockpit data root, outside the levain home;
    a writer who replaces both forges the authority (measured 2026-10-16), so the floor denies the root."""
    import importlib
    from levain.consent.queue import default_queue_dir

    mod = importlib.import_module(f"levain.consent.adapters.{adapter}")
    root = tmp_path / "proj" / ".levain" / "cockpit"
    (root / "anchor").mkdir(parents=True)
    _name_cockpit(default_queue_dir(), mod.SETTINGS_NAME, root)
    policy = build_policy(_entity(home))
    for name in ("cockpit.db", "cockpit.db-wal", "anchor/authority.anchor"):
        assert crown_jewel_reason(policy, root / name) is not None, name


def test_no_consent_settings_name_no_cockpit_root(home, tmp_path):
    policy = build_policy(_entity(home))
    assert crown_jewel_reason(policy, tmp_path / "proj" / ".levain" / "cockpit" / "cockpit.db") is None


def test_a_cockpit_root_that_holds_the_entity_refuses_the_floor(home, tmp_path):
    from levain.consent.adapters.claude_code import SETTINGS_NAME
    from levain.consent.queue import default_queue_dir
    from levain.firing.confinement import ConfinementError

    ent = _entity(tmp_path / "proj")
    _name_cockpit(default_queue_dir(), SETTINGS_NAME, tmp_path / "proj")
    with pytest.raises(ConfinementError, match="cockpit data root"):
        build_policy(ent)


@pytest.mark.parametrize("shape", ["fifo", "huge", "symlink"])
def test_a_consent_settings_file_levain_did_not_write_refuses_the_floor(home, tmp_path, shape):
    """An entity that wrote the queue directory before this floor existed may have left a FIFO (the build
    would hang), a huge file, or a link the hook would follow: the floor refuses rather than read it."""
    from levain.consent.adapters.claude_code import SETTINGS_NAME
    from levain.consent.queue import default_queue_dir
    from levain.firing.confinement import ConfinementError

    q = default_queue_dir()
    q.mkdir(parents=True)
    target = q / SETTINGS_NAME
    if shape == "fifo":
        os.mkfifo(target)
    elif shape == "huge":
        target.write_text(json.dumps({"cockpit_data_root": str(tmp_path), "pad": "x" * 70000}), encoding="utf-8")
    else:
        real = tmp_path / "elsewhere.json"
        real.write_text(json.dumps({"cockpit_data_root": str(tmp_path / "c")}), encoding="utf-8")
        target.symlink_to(real)
    with pytest.raises(ConfinementError, match="regular file"):
        build_policy(_entity(home))


def test_a_cockpit_root_spelled_through_a_symlink_refuses_the_floor(home, tmp_path):
    from levain.consent.adapters.claude_code import SETTINGS_NAME
    from levain.consent.queue import default_queue_dir
    from levain.firing.confinement import ConfinementError

    real = tmp_path / "real-proj"
    (real / ".levain" / "cockpit").mkdir(parents=True)
    (tmp_path / "link-proj").symlink_to(real)
    _name_cockpit(default_queue_dir(), SETTINGS_NAME, tmp_path / "link-proj" / ".levain" / "cockpit")
    with pytest.raises(ConfinementError, match="symbolic link"):
        build_policy(_entity(home))


def test_malformed_consent_settings_name_nothing(home):
    from levain.consent.adapters.claude_code import SETTINGS_NAME
    from levain.consent.queue import default_queue_dir

    q = default_queue_dir()
    q.mkdir(parents=True)
    (q / SETTINGS_NAME).write_text("{not json", encoding="utf-8")
    build_policy(_entity(home))   # the hook reads it as naming no root, so the floor adds none


@live
def test_live_confined_shell_cannot_replace_the_cockpit_authority(home, tmp_path):
    from levain.consent.adapters.claude_code import SETTINGS_NAME
    from levain.consent.queue import default_queue_dir

    root = tmp_path / "proj" / ".levain" / "cockpit"
    (root / "anchor").mkdir(parents=True)
    (root / "cockpit.db").write_bytes(b"real")
    _name_cockpit(default_queue_dir(), SETTINGS_NAME, root)
    entity = _entity(home)
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        ok = sh.run(f"touch '{entity}/workspace/control' 2>&1", timeout=20)
        assert ok.exit_code == 0, ok.output
        for cmd in (f"cat '{root}/cockpit.db'", f"rm -r '{root}/anchor'", f"printf x > '{root}/cockpit.db'",
                    f"mv '{tmp_path}/proj' '{tmp_path}/proj-moved'"):
            r = sh.run(cmd + " 2>&1", timeout=20)
            assert r.exit_code != 0, f"{cmd!r} succeeded: {r.output!r}"
    assert (root / "cockpit.db").read_bytes() == b"real" and (root / "anchor").is_dir()
    assert not (tmp_path / "proj-moved").exists()


@live
def test_live_confined_shell_cannot_read_or_rewrite_the_consent_settings(home):
    from levain.consent.adapters.claude_code import SETTINGS_NAME
    from levain.consent.queue import default_queue_dir

    queue = default_queue_dir()
    queue.mkdir(parents=True)
    settings = queue / SETTINGS_NAME
    settings.write_text('{"cockpit_data_root": "/real/root"}', encoding="utf-8")
    entity = _entity(home)
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        ok = sh.run(f"touch '{entity}/workspace/control' 2>&1", timeout=20)
        assert ok.exit_code == 0, ok.output
        for cmd in (f"cat '{settings}'", f"ls '{queue}'",
                    f"printf '{{}}' > '{settings}'", f"touch '{home}/.levain/new-dir-file'"):
            r = sh.run(cmd + " 2>&1", timeout=20)
            assert r.exit_code != 0, f"{cmd!r} succeeded: {r.output!r}"
            assert any(m in r.output for m in _REFUSALS), f"{cmd!r} failed for another reason: {r.output!r}"
    assert settings.read_text(encoding="utf-8") == '{"cockpit_data_root": "/real/root"}'
