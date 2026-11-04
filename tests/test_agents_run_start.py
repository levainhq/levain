"""The fire admits its own run in the agent RunJournal (key 1104-8): ``run_agent`` starts the run before any body runs,
for the occurrence this fire holds and no other. Nothing here starts a run by hand."""
from __future__ import annotations

import shutil

import pytest

from levain.agents import controller as ctl
from levain.agents import supervise
from levain.autonomic.journal import RunJournal
from levain.coordinator.store import CoordStore
from levain.dream import agent as da
from tests.test_agents_launch import Rig, rig  # noqa: F401
from tests.test_agents_supervise_fire import Ex

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _rename(rig: Rig, name: str) -> None:
    with rig.store.write() as c:
        c.execute("UPDATE agents SET name = ? WHERE agent_id = ?", (name, rig.agent))


def _runs(rig):
    import sqlite3
    conn = sqlite3.connect(str(RunJournal(rig.store.directory).db.path))
    try:
        return conn.execute("SELECT * FROM runs").fetchall()
    finally:
        conn.close()


def _started(rig: Rig, oid: str) -> bool:
    return any(rig.row(oid)["run_id"] in map(str, r) for r in _runs(rig))


def _launched(rig: Rig) -> str:
    rig.link()
    oid = rig.claim("k")
    assert rig.launch(oid).outcome == "launched"
    return oid


# --- refusals first -----------------------------------------------------------------------------------------------

def test_a_fire_for_an_occurrence_not_launched_starts_nothing_and_never_reaches_the_body(rig, capsys):
    seen = []
    oid = rig.claim("k")    # claimed, never launched
    _rename(rig, "x")
    assert ctl.run_agent(rig.store, oid, 0, bodies={"x": lambda: (lambda s, o, e: seen.append(o) or 0)}) \
        == ctl.FIRE_NOT_HELD
    assert seen == [] and not _started(rig, oid)
    assert "no run was admitted" in capsys.readouterr().err


def test_a_fire_at_a_launch_epoch_that_is_not_the_rows_starts_nothing(rig):
    seen = []
    oid = _launched(rig)
    _rename(rig, "x")
    bodies = {"x": lambda: (lambda s, o, e: seen.append(o) or 0)}
    assert ctl.run_agent(rig.store, oid, 7, bodies=bodies) == ctl.FIRE_NOT_HELD
    assert seen == [] and not _started(rig, oid)


def test_a_row_whose_epoch_a_revoke_raised_past_its_launch_epoch_starts_nothing(rig):
    seen = []
    oid = _launched(rig)
    _rename(rig, "x")
    with rig.store.write() as c:
        c.execute("UPDATE agent_occurrences SET epoch = launch_epoch + 1 WHERE occurrence_id = ?", (oid,))
    bodies = {"x": lambda: (lambda s, o, e: seen.append(o) or 0)}
    assert ctl.run_agent(rig.store, oid, 0, bodies=bodies) == ctl.FIRE_NOT_HELD
    assert seen == [] and not _started(rig, oid)


def test_an_ended_row_starts_nothing(rig):
    oid = _launched(rig)
    with rig.store.write() as c:
        c.execute("UPDATE agent_occurrences SET state = 'done', ended_at = launched_at, reason = 'x' WHERE occurrence_id = ?", (oid,))
    assert ctl.admit_run(rig.store, oid, 0) is False and not _started(rig, oid)


# --- the reproduction: the dream's fire, nothing started by hand ----------------------------------------------------

def test_the_dream_fire_with_no_hand_started_run_reaches_its_effect_and_ends_done(rig, tmp_path, monkeypatch):
    rig.link()
    oid = _launched(rig)
    _rename(rig, "dream")
    calls = []

    def factory(store, occurrence_id, epoch):    # the real factory's Supervisor: the DEFAULT journal, no start
        sup = supervise.Supervisor(store, authority=rig.authority, clock=rig.now,
                                   hooks=supervise.Hooks(verify=lambda end: None))
        q = tmp_path / "q"
        q.mkdir(mode=0o700)
        return {"sup": sup, "store_path": tmp_path / "memory.db", "occ_dir": tmp_path / "occ", "executor": Ex(),
                "consolidate": lambda: calls.append(occurrence_id) or {"outcome": "empty"},
                "review_store": CoordStore(q)}

    monkeypatch.setattr(da, "fire_factory", factory)
    assert ctl.run_agent(rig.store, oid, 0) == 0
    assert calls == [oid] and rig.row(oid)["state"] == "done"


# --- idempotence: two admitters of one run compose --------------------------------------------------------------------

def test_a_second_start_of_the_same_run_is_a_no_op_and_keeps_the_first_admission(rig):
    oid = _launched(rig)
    row = rig.row(oid)
    journal = RunJournal(rig.store.directory)
    assert ctl.admit_run(rig.store, oid, 0, journal=journal) is True
    first = _runs(rig)
    journal.start(row["run_id"], binding_id=row["binding_id"])    # S2b's start in the session factory
    assert ctl.admit_run(rig.store, oid, 0, journal=journal) is True
    assert _runs(rig) == first and len(first) == 1
