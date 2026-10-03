"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest


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
    # Same shape for the floor: `ANNEAL_MEMORY_DERIVE_TRUST` adds write-denies to every policy
    # build_policy assembles (spore-1308), so a shell that sets it would grade the floor tests.
    monkeypatch.delenv("ANNEAL_MEMORY_DERIVE_TRUST", raising=False)


@pytest.fixture(autouse=True)
def _reset_entity_process_latch():
    """`$LEVAIN_ENTITY_PROCESS` is a one-way latch by design (spore-438): opening an entity sets it
    and nothing in levain clears it, because nothing should. A test process opens many entities, so
    without this every later test would run as an entity process. Both halves (the in-process flag
    and the env var) are reset here, and only here. Popped by hand both sides:
    `monkeypatch.delenv` records nothing for an ABSENT var, so a latch set mid-test would survive."""
    import levain.firing.binding as _binding

    os.environ.pop("LEVAIN_ENTITY_PROCESS", None)
    _binding._LATCHED = False
    yield
    os.environ.pop("LEVAIN_ENTITY_PROCESS", None)
    _binding._LATCHED = False
