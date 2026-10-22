"""The repair/resumed-setup stop (`install_helper._stop_steps`) checks a job right after `launchctl bootout` the same way
undo does: it waits for launchd to finish unloading it. The check before the bootout does not wait (anansi L3 on the
undorace CHANGELOG, 10-22: the fix had reached undo's stop check only)."""
from __future__ import annotations

from types import SimpleNamespace

from levain.slot import install_helper as IH
from levain.slot import units as U


def test_the_check_after_bootout_waits_and_the_precheck_does_not(monkeypatch):
    seen: list[float] = []

    def absent(label, run=None, **kw):
        seen.append(kw.get("wait_s", 0.0))
        return (len(seen) % 2 == 0, "" if len(seen) % 2 == 0 else f"{label} is still loaded")

    monkeypatch.setattr(U, "_launchd_job_absent", absent)
    monkeypatch.setattr(IH.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout=""))
    services = list(U.services_for("darwin"))
    steps = IH._stop_steps("darwin", services, {}, lambda: 0.0)
    assert steps, "a stop step per launchd job"
    for s in steps:
        assert s.call() == (True, "")
    assert seen[0::2] == [0.0] * len(steps)
    assert seen[1::2] == [U.LAUNCHD_UNLOAD_WAIT_S] * len(steps)
