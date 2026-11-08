"""The Linux undo stops the boundary's systemd unit before it deletes the unit file, file present or not.

1107-26 (Ubuntu 24.04 guest, tests/ci/isolation_e2e.sh): after the final ``--undo`` the unit stayed loaded and
active (exited) with no file on disk, because the unit is ``RemainAfterExit`` and ``disable`` alone stops nothing.
A re-run over that stranded state (file already gone) must stop it too, so the stop is not gated on the file.

L3 verify round on that fix: a best-effort stop swallowed every failure (D-Bus, permission, timeout) and undo then
reported the boundary gone with its unit still active. Only systemd's "not loaded" counts as already stopped now;
a host with no systemd (setup refuses those, so no unit can exist there) is not a failure either.
"""
import os
import subprocess
from pathlib import Path

import pytest

from levain.firing import hands

USER = "_levain_coyote_847f6b"
UNIT = hands.egress_unit_name(USER)
SYSTEMCTL = "/usr/bin/systemctl"


def _stop_step(steps):
    return next(s for s in steps if s.why.startswith("stop the network boundary unit"))


def _rm_unit(steps):
    return next(s for s in steps if s.argv[:1] == ("/bin/rm",) and str(hands.egress_unit_path(USER)) in s.argv)


def test_undo_has_a_stop_not_gated_on_the_unit_file_and_not_best_effort():
    steps = hands.egress_undo_steps(USER)
    stop = _stop_step(steps)
    assert stop.skip_if == ()             # runs whether or not the file exists (the 1107-26 re-run)
    assert not stop.allow_fail            # a genuine stop failure stops the undo
    assert stop.call is not None
    disable = next(s for s in steps if s.argv[1:2] == ("disable",) and s.argv[-1] == UNIT)
    assert steps.index(stop) < steps.index(disable) < steps.index(_rm_unit(steps))
    assert disable.skip_if[-2:] == ("-e", str(hands.egress_unit_path(USER)))   # disable stays file-gated


class _Systemd:
    """Stands in for systemctl: ``stop`` and ``is-active`` answer as configured; records every argv."""

    def __init__(self, stop=(0, ""), active=(3, "inactive\n"), reload=(0, "")):
        self.answers = {"stop": stop, "is-active": active, "daemon-reload": reload}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        assert argv[0] == SYSTEMCTL, argv          # nothing else may run in these tests
        rc, out = self.answers[argv[1]]
        stream = {"stdout": out, "stderr": ""} if argv[1] == "is-active" else {"stdout": "", "stderr": out}
        return subprocess.CompletedProcess(argv, rc, **stream)


@pytest.fixture
def systemd(monkeypatch):
    def install(*, present=True, running=True, **answers):
        fake = _Systemd(**answers)
        monkeypatch.setattr(hands, "_abs", lambda name: SYSTEMCTL if (name == "systemctl" and present) else name)
        real_isdir = os.path.isdir
        monkeypatch.setattr(hands.os.path, "isdir",
                            lambda p: running if p == "/run/systemd/system" else real_isdir(p))
        monkeypatch.setattr(hands.subprocess, "run", fake)
        return fake
    return install


def test_the_stranded_1107_26_unit_is_stopped(systemd):
    # Unit loaded and active (exited), file already gone: stop succeeds through PID 1, is-active says inactive.
    fake = systemd(stop=(0, ""), active=(3, "inactive\n"))
    assert hands._egress_unit_stopped(USER) == (True, "")
    assert [c[1:] for c in fake.calls] == [["stop", UNIT], ["is-active", UNIT]]


def test_a_unit_that_is_not_loaded_is_already_stopped(systemd):
    systemd(stop=(5, f"Failed to stop {UNIT}: Unit {UNIT} not loaded."), active=(3, "inactive\n"))
    ok, why = hands._egress_unit_stopped(USER)
    assert ok and "not loaded" in why


@pytest.mark.parametrize("said", [
    f"Failed to stop {UNIT}: Access denied",
    f"Failed to stop {UNIT}: Connection timed out",
    "Failed to connect to bus: No such file or directory",
    "",
])
def test_a_genuine_stop_failure_fails_the_step(systemd, said):
    fake = systemd(stop=(1, said))
    ok, why = hands._egress_unit_stopped(USER)
    assert not ok and why
    assert [c[1] for c in fake.calls] == ["stop"]


def test_a_stop_that_leaves_the_unit_active_fails_the_step(systemd):
    systemd(stop=(0, ""), active=(0, "active\n"))
    ok, why = hands._egress_unit_stopped(USER)
    assert not ok and "not confirmed stopped" in why


def test_an_is_active_that_gives_no_state_is_not_proof_of_stopped(systemd):
    # D-Bus unreachable after a stop that exited 0: no state printed, nonzero exit.
    systemd(stop=(0, ""), active=(1, ""))
    ok, why = hands._egress_unit_stopped(USER)
    assert not ok and "not confirmed stopped" in why


def test_a_failed_stop_halts_the_undo_before_the_unit_file_goes(systemd):
    fake = systemd(stop=(1, f"Failed to stop {UNIT}: Access denied"))
    stop = _stop_step(hands.egress_undo_steps(USER))
    sentinel = hands.Step("would remove the unit file", ("/bin/rm", "-f", "/nonexistent"))
    plan = hands.Plan(host="linux", operator="op", hands_user=USER, hands_id=0, entity_dir=Path("/"),
                      workspace=Path("/"), steps=(stop, sentinel))
    assert hands.run_plan(plan, dry_run=False, emit=lambda _: None) == 1
    assert all(c[0] == SYSTEMCTL for c in fake.calls)       # the rm never ran


@pytest.mark.parametrize("present,running", [(False, False), (True, False)])
def test_a_host_without_systemd_has_nothing_to_stop_or_reload(systemd, present, running):
    fake = systemd(present=present, running=running)
    assert hands._egress_unit_stopped(USER)[0]
    assert hands._systemd_reloaded()[0]
    assert fake.calls == []


def test_daemon_reload_still_fails_closed_where_systemd_runs(systemd):
    systemd(reload=(1, "Failed to reload daemon: Access denied"))
    assert hands._systemd_reloaded() == (False, "Failed to reload daemon: Access denied")
