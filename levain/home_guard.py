"""The real-home guard: a test run or a scratch run never falls back to the operator's own levain home.

Every resolver of "the levain home" reads ``$LEVAIN_HOME`` and falls back to ``~/.levain`` when it is unset. Code that
runs outside the suite's isolation (a harness script that imports a test function, a bare ``python3`` a test spawns)
has no per-test ``$LEVAIN_HOME``, so that fallback is the operator's real home, and an append-only store there cannot
be cleaned up afterwards. A resolver that can lead to a write calls :func:`refuse_real_home_fallback` first; it
raises when the run is a test run or a scratch run AND ``$LEVAIN_HOME`` is unset. A run that sets ``$LEVAIN_HOME``
(every test the suite's conftest isolates) is untouched, and so is every run that is neither (the product).

This is the one home of the detection. The resolvers pass nothing but a label for the message; they do not copy the
check, and the guard reads ``$LEVAIN_HOME`` itself so "unset" (an empty value counts as unset, as every resolver
treats it) has one definition.

A test run is pytest loaded, or the per-test variable pytest sets. A scratch run is a process the harness marked with
:data:`SCRATCH_ENV` ``=1``: an explicit marker, because a script that is not under pytest has nothing else that says
"this is an experiment", and a heuristic (a path, a name) would guess. A harness script sets the marker AND a scratch
``$LEVAIN_HOME``; the marker alone, with the fallback in play, is the refusal.

One narrow admission, for the tests OF the fallback itself (a test that unsets ``$LEVAIN_HOME`` on purpose to see what
``~/.levain`` resolves to, under a scratch ``$HOME``): the suite's conftest names the temp root in
:data:`TEST_SCRATCH_ROOT_ENV`, and a test run whose ``$HOME`` lies inside it may take the fallback, because that
fallback is a scratch directory. A process the conftest did not start has no such variable, so a spawned bare script
(whatever its ``$HOME``) and a run with the operator's real ``$HOME`` are refused.
"""

from __future__ import annotations

import os
import pwd
import sys
from collections.abc import Mapping
from pathlib import Path

SCRATCH_ENV = "LEVAIN_SCRATCH_RUN"
HOME_ENV = "LEVAIN_HOME"
TEST_SCRATCH_ROOT_ENV = "LEVAIN_TEST_SCRATCH_ROOT"


class RealHomeReached(RuntimeError):
    """A test run or a scratch run reached for the operator's real levain home. ``str()`` names the fix."""


def under_test_run(environ: Mapping[str, str] | None = None) -> bool:
    """True inside a pytest run: the per-test variable pytest sets while a test runs, or pytest loaded at all."""
    env = os.environ if environ is None else environ
    return "PYTEST_CURRENT_TEST" in env or "pytest" in sys.modules


def scratch_run(environ: Mapping[str, str] | None = None) -> bool:
    """True when the harness marked this process a scratch run (:data:`SCRATCH_ENV` is ``1``)."""
    env = os.environ if environ is None else environ
    return env.get(SCRATCH_ENV) == "1"


def _home_is_vouched_scratch(env: Mapping[str, str]) -> bool:
    """True when the conftest named a scratch root and ``$HOME`` (the password database's home when unset, as
    ``Path.home`` reads it) resolves inside it. Anything that cannot be resolved is not vouched."""
    root = env.get(TEST_SCRATCH_ROOT_ENV)
    if not root:
        return False
    try:
        home = env.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
        return Path(home).resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError, KeyError, ValueError):
        return False


def refuse_real_home_fallback(what: str, environ: Mapping[str, str] | None = None) -> None:
    """Raise :class:`RealHomeReached` when ``what`` (the thing about to be resolved, for the message) would fall back to
    the real ``~/.levain`` in a test run or a scratch run; return otherwise. ``environ`` defaults to this process's."""
    env = os.environ if environ is None else environ
    if env.get(HOME_ENV):
        return
    if under_test_run(env):
        if _home_is_vouched_scratch(env):
            return
        why = "a test run"
    elif scratch_run(env):
        why = f"a scratch run ({SCRATCH_ENV}=1)"
    else:
        return
    raise RealHomeReached(
        f"refusing to resolve {what} under {why} with {HOME_ENV} unset: it would fall back to the operator's real "
        f"levain home (~/.levain), where the stores are append-only and a stray write cannot be undone. "
        f"Set {HOME_ENV} to a scratch directory (a test gets one from the conftest's per-test isolation; a script "
        f"outside tests/ sets {HOME_ENV} itself)."
    )
