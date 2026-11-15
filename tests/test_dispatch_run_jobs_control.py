"""S14 C P2 (design C, D2): the runner's control read is EXPLICIT. `build_runner`'s `active()` reads the control store of
its own store directory (`L/dispatch/control.db`), verified against the authority replica (`L/dispatch/authority`), both
passed in; it never resolves a directory or a cockpit from the process environment (T1).

The first test is the RUN of the design's unverified premise: `authority_source(<replica>)` reads a replica built from a
signed ledger, and a `ControlStore` verifies a signed item against it. Keys are real ssh-keygen keys in a real signed
ledger (the consent queue tests' `Key` and `Log`); the replica is the real `AuthorityReplica`.

UNVERIFIED here (VM): the replica as `_levain_dispatch` owns it, with the real peer-uid SYNC that fills it.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.agents import authority_replica as AR
from levain.authority import anchor_path
from levain.consent.queue import authority_source
from levain.dispatch import DispatchError
from levain.dispatch import cli as dcli
from levain.dispatch import control as ctl
from levain.dispatch.jobs import JobStore
from levain.dispatch.run_jobs import build_runner
from tests.test_consent_queue import Key, Log
from tests.test_dispatch_control import BB_ENTRY, IP_ENTRY, ventry, vbody

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("cp2-keys")
    return {"laptop": Key(d, "laptop")}


@pytest.fixture(scope="module")
def log(keys, tmp_path_factory):
    lg = Log(tmp_path_factory.mktemp("cp2-auth"), keys)
    lg.enrol("laptop", "laptop-presence")
    return lg


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _replica(log, root: Path) -> AR.AuthorityReplica:
    """A replica of ``log``'s signed ledger at ``root`` (what the run server's SYNC leaves)."""
    conn = sqlite3.connect(log.path)
    try:
        rows = [AR.ReplicaRow(*r) for r in conn.execute(
            "SELECT seq, prev_hash, record, signer_fp, signature, hash FROM authority_log ORDER BY seq")]
    finally:
        conn.close()
    replica = AR.AuthorityReplica(root)
    replica.append(rows)
    return replica


def _accept_verifier(directory: Path, key: Key, authority, body: dict) -> None:
    ctl.ControlStore.init(directory, key.fp, authority=authority)
    store = ctl.ControlStore(directory, authority=authority)
    item = ctl.item_bytes("verifier", 1, body, datetime.now(timezone.utc) - timedelta(minutes=1), prev="",
                          store_id=store.store_id())
    store.accept(item, key.sign(item, ctl.CONFIG_NAMESPACE))


@pytest.fixture
def placed(tmp_path, log, keys, monkeypatch):
    """A placed store: ``<L>/dispatch`` (the job store directory) holding ``control.db`` and ``authority/`` (the
    replica), the layout's STORE pointed at ``<L>``."""
    from levain.slot import layout
    L = _private(tmp_path / "L")
    monkeypatch.setitem(layout.STORE, "linux", L)
    d = _private(L / "dispatch")
    replica = _replica(log, d / "authority")
    src = authority_source(replica.db_path, anchor=anchor_path(replica.root))
    return d, src


def _runner(d: Path):
    return build_runner(JobStore(d), str(d / "control.sock"), "linux")


# --- the premise, RUN ------------------------------------------------------------------------------------------------

def test_authority_source_over_a_replica_verifies_a_control_store(placed, keys):
    d, src = placed
    assert src().seq >= 1                                       # the replica replays through the source
    _accept_verifier(d, keys["laptop"], src, vbody(BB_ENTRY))
    got = ctl.ControlStore(d, authority=src).active()
    assert set(got) == {"verifier"}


# --- _control_read takes the authority ------------------------------------------------------------------------------

def test_control_read_with_an_explicit_authority_names_no_cockpit_from_the_directory(placed, keys):
    d, src = placed
    _accept_verifier(d, keys["laptop"], src, vbody(BB_ENTRY))
    # no consent hooks in `d`, so the old path would raise CockpitUnnamed; the explicit authority needs none
    cfg = dcli._control_read(d, None, nothing="run", authority=src)
    assert cfg is not None and set(cfg.active()) == {"verifier"}


# --- T1: nothing from the environment ------------------------------------------------------------------------------

def test_active_answers_the_store_dirs_control_not_the_environments(placed, keys, log, tmp_path, monkeypatch):
    d, src = placed
    _accept_verifier(d, keys["laptop"], src, vbody(BB_ENTRY))
    # a hostile home whose consent dir holds a control.db naming ANOTHER verifier
    home = tmp_path / "hostile"
    hostile = _private(home / "consent")
    _accept_verifier(hostile, keys["laptop"], log.source, vbody(IP_ENTRY))
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    got = _runner(d).active()
    ours = ctl.ControlStore(d, authority=src).active()["verifier"].body
    assert got["verifier"].body == ours
    assert got["verifier"].body != ctl.ControlStore(hostile, authority=log.source).active()["verifier"].body


def test_a_control_db_only_in_the_environment_is_not_read(placed, keys, log, tmp_path, monkeypatch):
    d, _ = placed
    home = tmp_path / "hostile"
    hostile = _private(home / "consent")
    _accept_verifier(hostile, keys["laptop"], log.source, vbody(IP_ENTRY))
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    with pytest.raises(DispatchError) as exc:
        _runner(d).active()
    assert str(d / "control.db") in str(exc.value)


def test_no_control_db_in_the_store_dir_names_the_path(placed):
    d, _ = placed
    with pytest.raises(DispatchError) as exc:
        _runner(d).active()
    assert str(d / "control.db") in str(exc.value)


def test_a_missing_replica_fails_the_read_closed(tmp_path, log, keys, monkeypatch):
    from levain.slot import layout
    L = _private(tmp_path / "L")
    monkeypatch.setitem(layout.STORE, "linux", L)
    d = _private(L / "dispatch")
    _accept_verifier(d, keys["laptop"], log.source, vbody(BB_ENTRY))
    with pytest.raises(DispatchError):                    # ControlStateUnavailable is a DispatchError
        _runner(d).active()


def test_the_replica_may_be_named_apart_from_the_store_dir(tmp_path, log, keys, monkeypatch):
    from levain.slot import layout
    L = _private(tmp_path / "L")
    monkeypatch.setitem(layout.STORE, "linux", L)
    d = _private(L / "dispatch")
    ctl_dir = _private(tmp_path / "ctl")
    replica = _replica(log, tmp_path / "rep")
    src = authority_source(replica.db_path, anchor=anchor_path(replica.root))
    _accept_verifier(ctl_dir, keys["laptop"], src, vbody(BB_ENTRY))
    runner = build_runner(JobStore(d), str(d / "control.sock"), "linux", control_dir=ctl_dir,
                          authority_root=replica.root)
    assert set(runner.active()) == {"verifier"}


def test_a_replica_keeps_the_empty_meta_table_authority_source_reads(log, tmp_path):
    """The premise RUN found: `authority_source` over a replica with only `authority_log` raised `no such table:
    meta` (capture reads the cockpit's `uninitialised` marker). The replica keeps the table, empty."""
    replica = _replica(log, tmp_path / "rep")
    conn = sqlite3.connect(replica.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM meta").fetchone()[0] == 0
    finally:
        conn.close()
