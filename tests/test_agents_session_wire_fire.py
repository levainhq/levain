"""P7-1 S2b: a PRODUCTION fire of a session agent. No test sets ``controller.SESSION_FACTORY``: the default is
``session_wire.fire_factory`` (so no install ends `launcher-not-wired`, settled at zero, and a stub named in the install
file ends `done`). The factory admits nothing: the run was admitted by the launch (1104-8)."""
from __future__ import annotations

import shutil

import pytest

from levain.agents import controller as ctl
from levain.agents import session_wire as W
from tests.test_agents_session_launch import BINDING, Stub, rig  # noqa: F401
from tests.test_agents_session_wire import _AcceptAll, _install, _session_occurrence

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    monkeypatch.setattr(W, "LAUNCHERS", {})
    # the rig's authority log is dated in its own clock; the default factory reads real time, so pin it to the rig's
    real = W.fire_factory
    monkeypatch.setattr(W, "fire_factory", lambda s, o, e, clock=None: real(s, o, e, clock=rig_clock[0]))
    assert ctl.SESSION_FACTORY is None   # the production default: nothing monkeypatched on the controller


rig_clock: list = [None]


@pytest.fixture(autouse=True)
def _clock(rig):
    rig_clock[0] = rig.now


def _fire(rig, oid, monkeypatch):
    from levain.consent import delivery
    monkeypatch.setattr(delivery, "process_identity", lambda pid: ("2026-11-04T00:00:00", f"levain agents _fire {oid}"))
    return ctl.fire(rig.store, oid, run=ctl.run_agent, clock=rig.now)


def _journal_has(rig, oid) -> bool:
    import sqlite3
    from levain.autonomic.journal import RunJournal
    j = RunJournal(rig.store.directory)
    try:
        j.generation(BINDING)
        with j._read() as conn:
            return conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (rig.row(oid)["run_id"],)).fetchone() is not None
    except sqlite3.Error:
        return False


# --- refusals first ------------------------------------------------------------------------------------------------

def test_a_production_fire_with_no_launcher_named_ends_launcher_not_wired_at_zero(rig, monkeypatch):
    oid = _session_occurrence(rig)
    _install(rig)
    assert _fire(rig, oid, monkeypatch) == 1
    row = rig.row(oid)
    assert row["state"] == "failed" and row["reason"] == "session_refused:launcher-not-wired"
    assert rig.taken(oid)[0] == [] and all(r[0] != "open" for r in rig.taken(oid)[1])


def test_a_production_fire_with_no_install_file_builds_no_run_and_stays_launched(rig, monkeypatch):
    oid = _session_occurrence(rig)
    assert _journal_has(rig, oid)                      # admitted by the launch, before any factory
    assert _fire(rig, oid, monkeypatch) == 1
    assert rig.row(oid)["state"] == "launched"


# --- the production path ------------------------------------------------------------------------------------------

def test_a_production_fire_with_the_stub_named_ends_done(rig, monkeypatch):
    from levain.agents import gauges
    monkeypatch.setattr(gauges, "_CHECKS", _AcceptAll())
    oid = _session_occurrence(rig)
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub")
    code = _fire(rig, oid, monkeypatch)
    row = rig.row(oid)
    assert (code, row["state"], row["reason"]) == (0, "done", "completed") and len(stub.launches) == 1


def test_the_factory_admits_nothing_the_launch_admitted_the_run_once(rig):
    oid = _session_occurrence(rig)
    _install(rig)
    epoch = rig.row(oid)["launch_epoch"]
    assert _journal_has(rig, oid)
    for _ in range(2):                                 # two builds, one admission: the factory starts nothing
        W.fire_factory(rig.store, oid, epoch, clock=rig.now)
    from levain.autonomic.journal import RunJournal
    with RunJournal(rig.store.directory)._read() as conn:
        n = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id = ?", (rig.row(oid)["run_id"],)).fetchone()[0]
    assert n == 1
