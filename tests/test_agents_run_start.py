"""The agent run's admission (key 1104-8): the LAUNCH admits the occurrence's run in the agent RunJournal, in the launch
transaction that judged the agent fireable, so the run carries the launch-time governance generation; the fire admits
nothing and refuses, with a named reason, a row it does not hold or a run a fence has passed since the launch. Nothing
here starts a run by hand."""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from levain.agents import controller as ctl
from levain.agents import supervise
from levain.autonomic.binding import BindingStore
from levain.autonomic.journal import EffectStatus, RunJournal
from levain.coordinator.store import CoordStore
from levain.dream import agent as da
from tests.test_agents_launch import BINDING, Rig, rig  # noqa: F401
from tests.test_agents_supervise_fire import Ex

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _rename(rig: Rig, name: str) -> None:
    with rig.store.write() as c:
        c.execute("UPDATE agents SET name = ? WHERE agent_id = ?", (name, rig.agent))


def _runs(rig):
    conn = sqlite3.connect(str(RunJournal(rig.store.directory).db.path))
    try:
        return conn.execute("SELECT run_id, binding_id, generation FROM runs").fetchall()
    finally:
        conn.close()


def _run_of(rig: Rig, oid: str):
    return next((r for r in _runs(rig) if r[0] == rig.row(oid)["run_id"]), None)


def _launched(rig: Rig, key: str = "k") -> str:
    rig.link()
    oid = rig.claim(key)
    assert rig.launch(oid).outcome == "launched"
    return oid


def _fire(rig: Rig, oid: str, epoch: int = 0):
    """``run_agent`` for ``oid`` with a body that records that it ran; returns (exit code, ran)."""
    seen = []
    _rename(rig, "x")
    code = ctl.run_agent(rig.store, oid, epoch, bodies={"x": lambda: (lambda s, o, e: seen.append(o) or 0)})
    return code, seen == [oid]


# --- refusals first -----------------------------------------------------------------------------------------------

def test_a_fence_raised_between_launch_and_fire_refuses_the_fire_by_name(rig, capsys):
    oid = _launched(rig)
    RunJournal(rig.store.directory).fence(BINDING)     # a fence after the launch, before the fire
    code, ran = _fire(rig, oid)
    assert (code, ran) == (ctl.FIRE_NOT_HELD, False)
    assert "fenced" in capsys.readouterr().err
    assert _run_of(rig, oid)[2] == 0                    # the run keeps its launch generation; the fence is not adopted


def test_a_fence_between_launch_and_the_first_effect_stops_the_effect(rig):
    # the run carries the launch generation to the journal's own barrier, whoever reaches the effect
    oid = _launched(rig)
    RunJournal(rig.store.directory).fence(BINDING)
    ran = []
    sup = supervise.Supervisor(rig.store, authority=rig.authority, clock=rig.now)
    got = sup.effect(oid, "e1", digest="d", fn=lambda: ran.append(1) or {"ok": True}, executor=Ex())
    assert got.outcome.status is EffectStatus.FENCED and ran == []


def test_a_row_whose_epoch_a_revoke_raised_past_its_launch_epoch_is_refused_by_name(rig, capsys):
    oid = _launched(rig)
    with rig.store.write() as c:
        c.execute("UPDATE agent_occurrences SET epoch = launch_epoch + 1 WHERE occurrence_id = ?", (oid,))
    assert _fire(rig, oid) == (ctl.FIRE_NOT_HELD, False)
    assert "epoch_moved" in capsys.readouterr().err


def test_a_fire_at_a_launch_epoch_that_is_not_the_rows_is_refused(rig):
    oid = _launched(rig)
    assert _fire(rig, oid, 7) == (ctl.FIRE_NOT_HELD, False)
    assert ctl.fire_refusal(rig.store, oid, 7) == "epoch_moved"


def test_a_fire_for_an_occurrence_not_launched_admits_nothing_and_never_reaches_the_body(rig, capsys):
    oid = rig.claim("k")    # claimed, never launched
    assert _fire(rig, oid) == (ctl.FIRE_NOT_HELD, False)
    assert _run_of(rig, oid) is None
    assert "not_launched" in capsys.readouterr().err


def test_an_ended_row_is_refused(rig):
    oid = _launched(rig)
    with rig.store.write() as c:
        c.execute("UPDATE agent_occurrences SET state = 'done', ended_at = launched_at, reason = 'x'"
                  " WHERE occurrence_id = ?", (oid,))
    assert ctl.fire_refusal(rig.store, oid, 0) == "not_launched"


def test_a_launched_row_with_no_admitted_run_is_refused_never_admitted_late(rig, capsys):
    # a row launched before the launch admitted its run: the fire does not admit it at whatever generation is current
    oid = _launched(rig)
    with rig.store.write() as c:
        c.execute("DELETE FROM runs WHERE run_id = ?", (rig.row(oid)["run_id"],))
    assert _fire(rig, oid) == (ctl.FIRE_NOT_HELD, False)
    assert "run_not_admitted" in capsys.readouterr().err and _run_of(rig, oid) is None


def test_a_refused_launch_admits_no_run(rig):
    oid = rig.claim("k")                                 # no link: the launch stops the row
    assert rig.launch(oid).outcome == "stopped"
    assert _run_of(rig, oid) is None


# --- the admission is the launch's, at the launch generation -----------------------------------------------------------

def test_the_launch_admits_the_run_under_the_generation_current_at_launch(rig):
    RunJournal(rig.store.directory).fence(BINDING, generation=2)    # a fence BEFORE the launch is not a refusal
    oid = _launched(rig)
    assert _run_of(rig, oid) == (rig.row(oid)["run_id"], BINDING, 2)
    assert ctl.fire_refusal(rig.store, oid, 0) is None
    assert _fire(rig, oid) == (0, True)


def test_the_named_binding_is_not_a_registry_row_so_binding_store_admit_cannot_judge_it(rig):
    # A0 r8 section 2.3: named Bindings are never written to ``bindings``; admit() would refuse every agent fire
    oid = _launched(rig)
    store = BindingStore(rig.store.directory, journal=RunJournal(rig.store.directory))
    assert store.get(BINDING) is None
    assert store.admit(BINDING, rig.row(oid)["run_id"]) is None


# --- the reproduction: the dream's fire, nothing started by hand ----------------------------------------------------

def test_the_dream_fire_with_no_hand_started_run_reaches_its_effect_and_ends_done(rig, tmp_path, monkeypatch):
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


# --- idempotence: a later start of the same run (S2b's session factory) composes ----------------------------------------

def test_a_later_start_of_the_same_run_is_a_no_op_and_keeps_the_launch_admission(rig):
    oid = _launched(rig)
    row = rig.row(oid)
    first = _runs(rig)
    journal = RunJournal(rig.store.directory)
    journal.fence(BINDING)
    journal.start(row["run_id"], binding_id=row["binding_id"])    # S2b's start in the session factory
    assert _runs(rig) == first and len(first) == 1
    assert ctl.fire_refusal(rig.store, oid, 0) == "fenced"
