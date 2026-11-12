"""The real-home guard: a test run or a scratch run never falls back to the operator's own levain home.

Every resolver of "the levain home" reads ``$LEVAIN_HOME`` and falls back to ``~/.levain`` when it is unset. Code that
runs outside the suite's isolation (a harness script that imports a test function, a bare ``python3`` a test spawns)
has no per-test ``$LEVAIN_HOME``, so that fallback is the operator's real home, and an append-only store there cannot
be cleaned up afterwards. The check sits in the resolvers every writer goes through: ``queue_dir_for`` (the consent
directory as an environment names it, which the hook entry points, the band hook's receipt, the desk board and the
seat profile all derive the home from) and the few resolvers that read ``$LEVAIN_HOME`` themselves. It raises when
the run is a test run or a scratch run AND ``$LEVAIN_HOME`` is unset. A run that sets ``$LEVAIN_HOME`` (every test
the suite's conftest isolates) is untouched, and so is every run that is neither (the product).

This is the one home of the detection. The resolvers pass a label for the message and, where they resolve an
environment they were handed, that environment; they do not copy the check, and the guard reads ``$LEVAIN_HOME``
itself so "unset" (an empty value counts as unset, as every resolver treats it) has one definition.

A test run is pytest loaded, or the per-test variable pytest sets. A scratch run is a process the harness marked with
:data:`SCRATCH_ENV` ``=1``: an explicit marker, because a script that is not under pytest has nothing else that says
"this is an experiment", and a heuristic (a path, a name) would guess. Both are properties of the process, so they are
read from the process's own environment as well as from an environment handed in: a resolver given a made-up mapping
inside a test run is still in a test run. A harness script sets the marker AND a scratch ``$LEVAIN_HOME``; the marker
alone, with the fallback in play, is the refusal.

One narrow admission, for the tests OF the fallback itself (a test that unsets ``$LEVAIN_HOME`` on purpose to see what
``~/.levain`` resolves to, under a scratch ``$HOME``): the suite's conftest names the temp root in
:data:`TEST_SCRATCH_ROOT_ENV`, and a test run whose ``$HOME`` lies inside it may take the fallback, because that
fallback is a scratch directory. The voucher is read from the handed environment or the process's, so a test's own
mapping with a scratch ``HOME`` is admitted. The conftest sets it in the process environment, so a child a test spawns
inherits it (and pytest's per-test variable): such a child is admitted only while its ``$HOME`` is inside the root,
and refused with any other ``$HOME``. A script the suite did not start has no voucher and is refused whatever its
``$HOME``. A root that holds the account's own home (from the password database) vouches for nothing, since then the
operator's real home would pass as scratch.
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


def guarded_run(environ: Mapping[str, str] | None = None) -> str | None:
    """Why this run may not fall back to the real home (``"a test run"``, ``"a scratch run (...)"``), or ``None`` for a
    run that is neither. Read from ``environ`` and from the process's own environment."""
    env = os.environ if environ is None else environ
    if under_test_run(env) or under_test_run():
        return "a test run"
    if scratch_run(env) or scratch_run():
        return f"a scratch run ({SCRATCH_ENV}=1)"
    return None


def _account_home() -> Path | None:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (OSError, RuntimeError, KeyError, ValueError):
        return None


def vouched_scratch(path: Path | str, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``path`` resolves inside the temp root the conftest named (in ``environ`` or the process's own
    environment), and that root does not hold the account's own home. Anything that cannot be resolved is not
    vouched."""
    env = os.environ if environ is None else environ
    root = env.get(TEST_SCRATCH_ROOT_ENV) or os.environ.get(TEST_SCRATCH_ROOT_ENV)
    if not root:
        return False
    try:
        top = Path(root).resolve()
        account = _account_home()
        if account is None or account.is_relative_to(top):
            return False
        return Path(path).resolve().is_relative_to(top)
    except (OSError, RuntimeError, ValueError):
        return False


def refuse_real_home_fallback(what: str, environ: Mapping[str, str] | None = None) -> None:
    """Raise :class:`RealHomeReached` when ``what`` (the thing about to be resolved, for the message) would fall back to
    the real ``~/.levain`` in a test run or a scratch run; return otherwise. ``environ`` is the environment the
    resolver reads ``$LEVAIN_HOME`` and ``$HOME`` from (default: this process's); ``$HOME`` absent there means the
    password database's home, as ``Path.home`` reads it."""
    env = os.environ if environ is None else environ
    if env.get(HOME_ENV):
        return
    why = guarded_run(env)
    if why is None:
        return
    if (under_test_run(env) or under_test_run()) and vouched_scratch(env.get("HOME") or _account_home() or "/", env):
        return
    raise RealHomeReached(
        f"refusing to resolve {what} under {why} with {HOME_ENV} unset: it would fall back to the operator's real "
        f"levain home (~/.levain), where the stores are append-only and a stray write cannot be undone. "
        f"Set {HOME_ENV} to a scratch directory (a test gets one from the conftest's per-test isolation; a script "
        f"outside tests/ sets {HOME_ENV} itself)."
    )
