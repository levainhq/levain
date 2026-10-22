"""A check made right after `launchctl bootout` waits for launchd to finish unloading the job (macOS VM, 10-22:
a fresh setup then an immediate undo failed "check com.levain.reaper is stopped" with the job gone seconds later)."""
from __future__ import annotations

from types import SimpleNamespace

from levain.slot import units as U


class _Launchd:
    """`launchctl print` answers loaded for the first ``loaded_for`` calls, then not loaded."""

    def __init__(self, loaded_for: int) -> None:
        self.loaded_for, self.calls = loaded_for, 0

    def __call__(self, argv, **kw):
        self.calls += 1
        return SimpleNamespace(returncode=0 if self.calls <= self.loaded_for else 113)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_a_job_still_unloading_is_absent_once_launchd_lets_it_go():
    run, c = _Launchd(loaded_for=3), _Clock()
    assert U._launchd_job_absent("com.levain.reaper", run, wait_s=U.LAUNCHD_UNLOAD_WAIT_S,
                                 sleep=c.sleep, clock=c.now) == (True, "")
    assert run.calls == 4 and c.t < U.LAUNCHD_UNLOAD_WAIT_S


def test_a_job_that_never_unloads_is_still_loaded_at_the_bound():
    run, c = _Launchd(loaded_for=10**6), _Clock()
    absent, why = U._launchd_job_absent("com.levain.reaper", run, wait_s=U.LAUNCHD_UNLOAD_WAIT_S,
                                        sleep=c.sleep, clock=c.now)
    assert not absent and "still loaded" in why
    assert U.LAUNCHD_UNLOAD_WAIT_S <= c.t <= U.LAUNCHD_UNLOAD_WAIT_S + U._LAUNCHD_POLL_S


def test_the_precheck_does_not_wait():
    run, c = _Launchd(loaded_for=10**6), _Clock()
    assert U._launchd_job_absent("com.levain.reaper", run, sleep=c.sleep, clock=c.now)[0] is False
    assert run.calls == 1 and c.t == 0.0


def test_undos_stop_check_is_the_waiting_one():
    steps = [s for s in U.uninstall_steps("darwin") if s.why.startswith("check ") and s.why.endswith(" is stopped")]
    assert steps, "undo has a stop check per launchd job"
    seen: list[float] = []
    real = U._launchd_job_absent
    try:
        U._launchd_job_absent = lambda label, run=None, **kw: (seen.append(kw.get("wait_s", 0.0)), (True, ""))[1]
        for s in steps:
            s.call()
    finally:
        U._launchd_job_absent = real
    assert seen and all(w == U.LAUNCHD_UNLOAD_WAIT_S for w in seen)


def test_only_launchds_no_such_service_answer_means_absent():
    c = _Clock()
    for rc in (1, 5, 37, None):
        def run(argv, rc=rc, **kw):
            return SimpleNamespace(returncode=rc)

        absent, why = U._launchd_job_absent("com.levain.reaper", run, sleep=c.sleep, clock=c.now)
        assert not absent and "could not be checked" in why, (rc, why)


def test_a_hung_launchctl_print_is_bounded_and_fails_closed():
    import subprocess

    c, timeouts = _Clock(), []

    def run(argv, **kw):
        timeouts.append(kw["timeout"])
        c.t += kw["timeout"]
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    absent, why = U._launchd_job_absent("com.levain.reaper", run, wait_s=U.LAUNCHD_UNLOAD_WAIT_S,
                                        sleep=c.sleep, clock=c.now)
    assert not absent and "timed out" in why
    assert timeouts and all(t >= U._LAUNCHD_PRINT_MIN_S for t in timeouts)
    assert c.t <= U.LAUNCHD_UNLOAD_WAIT_S + U._LAUNCHD_PRINT_MIN_S + U._LAUNCHD_POLL_S
    assert U._launchd_job_absent("x", run, sleep=c.sleep, clock=c.now)[0] is False   # the no-wait check is bounded too
