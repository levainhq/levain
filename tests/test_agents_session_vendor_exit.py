"""P7-1 row 7 "agents show done half": does the PRODUCT write ``done`` from the vendor session's exit?

The fire is the production one (``controller.fire`` -> ``run_agent`` -> ``session_wire.fire_factory`` -> ``run_session``
-> ``Supervisor.finish``), with the production verify chain (the REAL ``session_completed`` win check, not an
accept-all) and the production ``on_end`` episode hook writing into a real episodic store. The only fakes are the
launcher (it hands back a REAL ``GatedChild``: the real gate process runs a tiny python "vendor" that exits with a
chosen status) and the S1 END proof, which is the launcher layer's (``_SlotProc`` below has its own cases). What is
read back is ``agents show --json``'s own reader (``cli._show_json``): the occurrence rows and the episodes under the
agent id. Nothing here starts a real vendor or a VM.

Each case names the line whose revert turns it red, in its docstring.
"""
from __future__ import annotations

import json
import shutil
import sys

import pytest

from levain.agents import controller as ctl
from levain.agents import gauges
from levain.agents import session_launcher as L
from levain.agents import session_wire as W
from levain.cli import main
from tests.test_agents_launch import SPECS
from tests.test_agents_run_by_executor import _session_occurrence as _signed_occurrence
from tests.test_agents_session_launch import Stub, rig  # noqa: F401

NAME = "job_feeder_downstream"

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    monkeypatch.setattr(W, "LAUNCHERS", {})


@pytest.fixture(autouse=True)
def _factory(rig, monkeypatch):
    monkeypatch.setattr(ctl, "SESSION_FACTORY", lambda s, o, e: W.fire_factory(s, o, e, clock=rig.now))


@pytest.fixture(autouse=True)
def _real_win_check(monkeypatch):
    """Production registry, fresh: only what the fire itself registers. The spec names ``session_completed``."""
    monkeypatch.setattr(gauges, "_CHECKS", {})
    monkeypatch.setitem(SPECS[NAME], "win", {"kind": "verifier", "check": W.SESSION_WIN_CHECK})


class VendorLauncher(Stub):
    """A launcher whose child is a REAL gated child: the real ``python -I`` gate runs ``vendor`` and exits its status."""

    def __init__(self, vendor_code: str):
        super().__init__()
        self.vendor_code = vendor_code

    def spawn(self, launch):
        self.launches.append(launch)
        child = ctl.GatedChild([sys.executable, "-c", self.vendor_code])
        self.children.append(child)
        return child


def _fire_once(rig, monkeypatch, tmp_path, vendor_code):
    """Fire one session occurrence against a real gate running ``vendor_code``; return (fire code, oid)."""
    from levain.consent import delivery
    oid = _signed_occurrence(rig, name=NAME)
    launcher = VendorLauncher(vendor_code)
    W.register_launcher("vendor", lambda store: launcher)
    from anneal_memory import Store
    Store(tmp_path / "episodes.db", audit=False).close()
    W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher="vendor",
                    episodic_path=tmp_path / "episodes.db")
    monkeypatch.setattr(delivery, "process_identity",
                        lambda pid: ("2026-11-04T00:00:00", f"levain agents _fire {oid}"))
    return ctl.fire(rig.store, oid, run=ctl.run_agent, clock=rig.now), oid


def _shown(rig, oid, capsys):
    """What ``levain agents show <agent> --json`` prints: (this occurrence's entry, the episodes under the agent id)."""
    capsys.readouterr()
    code = main(["agents", "--store-dir", str(rig.store.directory), "show", rig.row(oid)["agent_id"], "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    (occ,) = [o for o in doc["occurrences"] if o["occurrence_id"] == oid]
    return occ, doc["episodes"] or []


# --- the exit paths ---------------------------------------------------------------------------------------------------

def test_a_vendor_exit_0_is_written_done_with_its_episode(rig, monkeypatch, tmp_path, capsys):
    """Revert ``ok = outcome == "exited" and result.get("returncode") == 0`` in ``session.run_session`` (make it
    False) and this reads ``failed``/``session_exit_0``."""
    code, oid = _fire_once(rig, monkeypatch, tmp_path, "import sys; sys.exit(0)")
    occ, eps = _shown(rig, oid, capsys)
    assert (code, occ["state"], occ["reason"]) == (0, "done", "completed")
    assert occ["ended_at"] is not None
    assert [e["content"] for e in eps if oid in (e["content"] or "")] == \
        [f"session run {oid} of agent {rig.row(oid)['agent_id']} ended done: completed"]


@pytest.mark.parametrize("vendor,expect", [
    ("import sys; sys.exit(1)", "session_exit_1"),
    ("import sys; sys.exit(3)", "session_exit_3"),
    ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", "session_exit_137"),     # 128 + 9, the gate's own
    ("import sys; sys.exit(127)", "session_exit_127"),
])
def test_a_vendor_that_exits_nonzero_or_dies_by_signal_is_written_failed_never_done(rig, monkeypatch, tmp_path,
                                                                                    capsys, vendor, expect):
    """Revert ``reason = None if ok else (f"session_exit_{...}")`` and ``ok`` as above: flipping ``ok`` to
    ``outcome == "exited"`` alone writes ``done`` here."""
    code, oid = _fire_once(rig, monkeypatch, tmp_path, vendor)
    occ, eps = _shown(rig, oid, capsys)
    assert (code, occ["state"], occ["reason"]) == (1, "failed", expect)
    assert any(f"ended failed: {expect}" in (e["content"] or "") for e in eps)


def test_a_vendor_that_exits_the_unproven_status_writes_nothing_and_keeps_its_slot(rig, monkeypatch, tmp_path, capsys):
    """Revert ``if outcome == "exited" and not result.get("proven")`` in ``run_session`` (the early return): the row
    would end ``failed``/``session_exit_76`` and free its slot."""
    code, oid = _fire_once(rig, monkeypatch, tmp_path, f"import sys; sys.exit({ctl.GATE_UNPROVEN})")
    occ, eps = _shown(rig, oid, capsys)
    assert (code, occ["state"], occ["reason"], occ["ended_at"]) == (1, "launched", None, None)
    assert not any(oid in (e["content"] or "") for e in eps)          # no episode for a run that did not end
    slots_held, reservations = rig.taken(oid)
    assert slots_held and reservations == [("open",)]


def test_a_clean_exit_is_not_done_when_the_specs_win_check_is_unregistered(rig, monkeypatch, tmp_path, capsys):
    """Revert the ``if verdict is not None and state == "done"`` demotion in ``Supervisor.finish``: the clean exit
    would then end ``done`` though its win was never checked (fail closed by design)."""
    monkeypatch.setitem(SPECS[NAME], "win", {"kind": "verifier", "check": "compute_anomalies"})
    code, oid = _fire_once(rig, monkeypatch, tmp_path, "import sys; sys.exit(0)")
    occ, _ = _shown(rig, oid, capsys)
    assert (code, occ["state"], (occ["reason"] or "").split(":")[0]) == (1, "failed", "win_unverified")


# --- the launcher layer between the gate and run_session (``_SlotProc``): END proof decides done vs launched -----------

class _Inner:
    """The inner gate process: exits ``rc`` at once."""

    def __init__(self, rc):
        self.rc = rc

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        return self.rc

    def terminate(self):
        pass


class _Launched:
    """Just enough of ``session_launcher._Launched`` for ``_SlotProc``: an END attempt that is proven or not."""

    def __init__(self, proven):
        self.proven = proven
        self.calls = []

    def end_once(self, *, polling=False):
        self.calls.append(polling)
        return self.proven


@pytest.mark.parametrize("rc,proven,expect", [(0, True, 0), (3, True, 3), (0, False, ctl.GATE_UNPROVEN),
                                              (3, False, ctl.GATE_UNPROVEN)])
def test_the_slot_proc_answers_the_vendors_exit_only_once_end_proved_the_group_gone(rc, proven, expect):
    """Revert ``return rc if self._l.end_once(polling=polling) else _ctl.GATE_UNPROVEN`` in ``_SlotProc._settle``:
    a clean exit with END unproven would read ``0`` and run_session would write ``done``."""
    launched = _Launched(proven)
    p = L._SlotProc(_Inner(rc), launched)
    assert p.poll() == expect and launched.calls == [True]
    assert p.wait() == expect and launched.calls == [True, False]


def test_the_slot_proc_poll_answers_none_while_the_vendor_runs():
    launched = _Launched(True)
    p = L._SlotProc(_Inner(None), launched)
    assert p.poll() is None and launched.calls == []
