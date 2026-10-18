"""The conftest guard that keeps a broad pytest run from applying a live Seatbelt profile to the host."""

from __future__ import annotations

import subprocess

import pytest

from tests.conftest import LIVE_SEATBELT_ENV


def test_the_driver_spawn_is_refused_unless_opted_in(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_SEATBELT_ENV, raising=False)
    # A path that does not exist: were the guard broken this raises FileNotFoundError and applies nothing.
    with pytest.raises(pytest.skip.Exception, match=LIVE_SEATBELT_ENV):
        subprocess.Popen(["/nonexistent/sandbox-exec", "-p", "(version 1)", "/usr/bin/true"])
    with pytest.raises(pytest.skip.Exception):   # behind sudo/env, as the hands path spells it
        subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/env", "-i", "/nonexistent/sandbox-exec", "-p", "x"])


def test_the_opt_in_lets_the_spawn_through(monkeypatch) -> None:
    monkeypatch.setenv(LIVE_SEATBELT_ENV, "1")
    with pytest.raises(FileNotFoundError):       # past the guard; the path is missing, so nothing runs
        subprocess.Popen(["/nonexistent/sandbox-exec", "-p", "x"])


def test_other_spawns_are_untouched(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_SEATBELT_ENV, raising=False)
    assert subprocess.run(["/usr/bin/true"]).returncode == 0
