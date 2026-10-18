"""The conftest guard that keeps a broad pytest run from applying a live Seatbelt profile to the host."""

from __future__ import annotations

import subprocess

import pytest

import tests.conftest as conftest
from tests.conftest import LIVE_SEATBELT_ENV, _spawns_seatbelt_driver

# A path that does not exist: were the guard broken these raise FileNotFoundError and apply nothing.
MISSING = "/nonexistent/sandbox-exec"


def test_the_driver_spawn_is_refused_unless_opted_in(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "LIVE_SEATBELT_OPT_IN", False)
    with pytest.raises(pytest.skip.Exception, match=LIVE_SEATBELT_ENV):
        subprocess.Popen([MISSING, "-p", "(version 1)", "/usr/bin/true"])
    with pytest.raises(pytest.skip.Exception):   # behind sudo/env, as the hands path spells it
        subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/env", "-i", MISSING, "-p", "x"])
    with pytest.raises(pytest.skip.Exception):   # the driver as executable=, argv[0] a decoy
        subprocess.Popen(["decoy", "-p", "x"], executable=MISSING)
    with pytest.raises(pytest.skip.Exception):   # an iterator argv is judged, and judged once
        subprocess.Popen(iter([MISSING, "-f", "/tmp/p.sb", "/usr/bin/true"]))


def test_a_setenv_during_the_run_does_not_open_the_guard(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "LIVE_SEATBELT_OPT_IN", False)
    monkeypatch.setenv(LIVE_SEATBELT_ENV, "1")
    with pytest.raises(pytest.skip.Exception):
        subprocess.Popen([MISSING, "-p", "x"])


def test_the_opt_in_lets_the_spawn_through(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "LIVE_SEATBELT_OPT_IN", True)
    with pytest.raises(FileNotFoundError):       # past the guard; the path is missing, so nothing runs
        subprocess.Popen([MISSING, "-p", "x"])


def test_other_spawns_are_untouched(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "LIVE_SEATBELT_OPT_IN", False)
    assert subprocess.run(["/usr/bin/true"]).returncode == 0


def test_every_driver_option_shape_is_caught() -> None:
    # a predefined profile (-n) and a parameter before the profile (-D) apply a profile too (L3 r2 codex)
    assert _spawns_seatbelt_driver([MISSING, "-n", "no-network", "/usr/bin/true"])
    assert _spawns_seatbelt_driver(["/usr/bin/env", MISSING, "-D", "K=v", "-p", "x", "/usr/bin/true"])
    # deliberately broad: naming the driver as an operand is skipped as well
    assert _spawns_seatbelt_driver(["/usr/bin/which", "sandbox-exec"])
