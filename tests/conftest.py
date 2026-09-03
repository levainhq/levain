"""Shared pytest fixtures.

The drive-mode binding (`$LEVAIN_DRIVE_MODE`) is PROCESS-GLOBAL, and `bind_drive_mode` refuses a
rebind that would WIDEN the credential floor. pytest runs the whole suite in one process, so
without this a test that opens an unattended session would poison every later test that opens an
interactive one — cross-test contamination that is really the multi-session limitation showing up
in miniature (see `levain.firing.drive`). Each test gets a clean binding, i.e. simulates the fresh
process that a real `levain run` invocation always is.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_drive_mode_binding():
    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV

    os.environ.pop(LEVAIN_DRIVE_MODE_ENV, None)
    try:
        yield
    finally:
        os.environ.pop(LEVAIN_DRIVE_MODE_ENV, None)


@pytest.fixture(autouse=True)
def _isolate_activation_env(monkeypatch):
    """`LEVAIN_SCOPE` and `CLAUDE_CONFIG_DIR` are AMBIENT INPUTS to doctor, so a
    test that does not clear them is graded by the reviewer's shell.

    Both were measured breaking this suite in opposite directions (Diogenes
    2026-09-03). `LEVAIN_SCOPE=global` — the value `doctor`'s own hint and the
    0.4.2 notes tell operators to set — turned
    `TestActivationScopeCheck::test_the_dark_configuration_FAILS` RED, because
    `_check_activation_scope` correctly declines to fail an install whose gate
    the environment has opened. `CLAUDE_CONFIG_DIR` was worse for being quiet:
    `_user_level_wiring` prefers it over `Path.home()` while every test in
    `TestUserLevelWiring` monkeypatches `Path.home` only, so the four cases
    asserting `== []` went on PASSING for the wrong reason and the
    two-installs-on-one-machine discrimination stopped being exercised at all.

    A guard that passes vacuously under the configuration the docs prescribe is
    not a guard. Tests that MEAN to exercise either variable set it explicitly
    (see `TestActivationScopeEnvOverride`, `TestUserLevelWiringHonorsConfigDir`)
    — this fixture removes the ambient value, it does not forbid the deliberate
    one.
    """
    monkeypatch.delenv("LEVAIN_SCOPE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
