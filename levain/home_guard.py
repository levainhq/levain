"""The real-home guard: a test run or a scratch run never falls back to the operator's own levain home, and a production
run nobody marked is told so (a later release refuses it).

Every resolver of "the levain home" reads ``$LEVAIN_HOME`` and falls back to ``~/.levain`` when it is unset. Code that
runs outside the suite's isolation (a harness script that imports a test function, a bare ``python3`` a test spawns)
has no per-test ``$LEVAIN_HOME``, so that fallback is the operator's real home, and an append-only store there cannot
be cleaned up afterwards. The check sits in the resolvers every writer goes through: ``queue_dir_for`` (the consent
directory as an environment names it, which the hook entry points, the band hook's receipt, the desk board and the
seat profile all derive the home from) and the few resolvers that read ``$LEVAIN_HOME`` themselves. It raises when
the run is a test run or a scratch run AND ``$LEVAIN_HOME`` is unset. A run that sets ``$LEVAIN_HOME`` (every test
the suite's conftest isolates) is untouched, and so is a production run a levain launcher marked live (the product).

A THIRD kind of run is the one nobody marked: a production process (not pytest, not a scratch run) that resolves the
fallback with no launcher above it, which is how a script outside the suite reached the real home. The intent is that
every levain launcher calls :func:`mark_live` first thing (:data:`LIVE_ENV` ``=1``, set in-process so no installed
hook or unit is re-rendered); ``tests/test_live_marker.py`` is what holds that, not this paragraph. An unmarked
production run is, in 1.0, WARNED (one stderr line per resolver per process) and LOGGED (one row in
``<home>/home_guard.log``), and still resolves the real home; :data:`UNMARKED_ACTION` is the one value a later
release flips to ``"refuse"``. Never a scratch fallback: a real run that lost its marker must reach its real store.

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
LIVE_ENV = "LEVAIN_LIVE"
LOG_NAME = "home_guard.log"
#: The log's size past which a row is skipped (the warning still prints): a per-call unmarked script or a crash-looping
#: unit cannot grow it forever, and the first rows, the evidence the refuse flip waits on, are kept.
LOG_MAX_BYTES = 256 * 1024

#: What an UNMARKED production run does when it resolves the real ``~/.levain``: ``"warn"`` (1.0: one stderr line and one
#: log row per resolver per process, then the real home as before) or ``"refuse"`` (a later release: ``RealHomeReached``).
#: The flip is this one value; ``tests/test_live_marker.py`` pins ``"warn"`` and the refusal under ``"refuse"``.
UNMARKED_ACTION = "warn"
UNMARKED_ACTIONS = ("warn", "refuse")

_WARNED: dict[tuple[int, str], object] = {}     # (pid, resolver label) already warned
_MARK_FAILED: str | None = None                 # why mark_live could not mark this process, when it could not


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
    the real ``~/.levain`` in a test run or a scratch run. For a production run it returns: marked live, silently;
    unmarked, per :data:`UNMARKED_ACTION` (``"warn"``: a warning and a log row, then return; ``"refuse"``: raise
    :class:`RealHomeReached`, see :func:`unmarked_production_run`). It also returns when ``$LEVAIN_HOME`` is set (or
    a test's scratch ``$HOME`` is vouched, see :func:`vouched_scratch`). ``environ`` is the environment the
    resolver reads ``$LEVAIN_HOME`` and ``$HOME`` from (default: this process's); ``$HOME`` absent there means the
    password database's home, as ``Path.home`` reads it."""
    env = os.environ if environ is None else environ
    if env.get(HOME_ENV):
        return
    why = guarded_run(env)
    if why is None:
        unmarked_production_run(what, env)
        return
    if (under_test_run(env) or under_test_run()) and vouched_scratch(env.get("HOME") or _account_home() or "/", env):
        return
    raise RealHomeReached(
        f"refusing to resolve {what} under {why} with {HOME_ENV} unset: it would fall back to the operator's real "
        f"levain home (~/.levain), where the stores are append-only and a stray write cannot be undone. "
        f"Set {HOME_ENV} to a scratch directory (a test gets one from the conftest's per-test isolation; a script "
        f"outside tests/ sets {HOME_ENV} itself)."
    )


def mark_live() -> None:
    """Called first thing by every levain launcher (each ``__main__`` block, the console entry): this process is a real
    levain run, so resolving the real ``~/.levain`` is expected. Sets :data:`LIVE_ENV` in the process environment;
    ``levain.launch.allowed`` refuses the name, so it crosses no exec levain makes and each entry marks itself (a
    model levain starts must not hand it to a script it writes). A no-op in a test run, whatever calls it: the suite never takes the real home, and an
    in-process ``main()`` call must not leak the marker into the next test. Never raises."""
    global _MARK_FAILED
    try:
        if not under_test_run():
            os.environ[LIVE_ENV] = "1"
    except Exception as exc:  # noqa: BLE001 — marking must never stop a launcher, but a failed mark is recorded:
        _MARK_FAILED = f"{type(exc).__name__}: {exc}"[:200]   # one line now, and in every unmarked row after
        _warn(f"levain: warning: could not mark this process live ({_MARK_FAILED}); it runs as unmarked.")


def live_marked(environ: Mapping[str, str] | None = None) -> bool:
    """True when a launcher marked this process live (read from ``environ`` and from the process's own)."""
    env = os.environ if environ is None else environ
    return env.get(LIVE_ENV) == "1" or os.environ.get(LIVE_ENV) == "1"


def _log_unmarked(what: str, env: Mapping[str, str]) -> None:
    """One JSON row in ``<real home>/home_guard.log``, only when that directory already exists (a resolve never creates
    the home). Best effort: a log that cannot be written never fails the run. Never blocks and never follows a link: a
    FIFO, a link or anything but a regular file at the log's path is skipped, silently. The row is ONE ``write`` on an
    ``O_APPEND`` descriptor, so concurrent processes append whole rows."""
    import json
    import stat
    import time

    try:
        base = env.get("HOME") or str(_account_home() or "")
        if not base:
            return
        home = Path(base.rstrip("/") or "/") / ".levain"
        if not home.is_dir():
            return
        row = {"ts": round(time.time(), 3), "pid": os.getpid(), "argv0": sys.argv[0] if sys.argv else "", "what": what}
        if _MARK_FAILED:
            row["mark_failed"] = _MARK_FAILED
        data = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8", "replace")
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(home / LOG_NAME, flags, 0o600)
        try:
            st = os.fstat(fd)
            if stat.S_ISREG(st.st_mode) and st.st_size < LOG_MAX_BYTES:
                os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def unmarked_production_run(what: str, environ: Mapping[str, str] | None = None) -> None:
    """The run is neither a test run nor a scratch run and ``$LEVAIN_HOME`` is unset: it is about to use the real home.
    Marked live: nothing. Unmarked: per :data:`UNMARKED_ACTION`, warn once per resolver per process and log a row
    (never a scratch fallback: a real run that lost its marker must still reach its real store), or raise
    :class:`RealHomeReached`."""
    if UNMARKED_ACTION not in UNMARKED_ACTIONS:
        raise RuntimeError(f"home_guard.UNMARKED_ACTION is {UNMARKED_ACTION!r}, not one of {UNMARKED_ACTIONS}")
    env = os.environ if environ is None else environ
    if live_marked(env):
        return
    fix = (f"start it through `levain`, call levain.home_guard.mark_live() first thing in your own launcher, "
           f"or set {HOME_ENV} to the directory you mean")
    if UNMARKED_ACTION == "refuse":
        raise RealHomeReached(
            f"refusing to resolve {what}: {HOME_ENV} is unset and this process was not started by a levain launcher "
            f"({LIVE_ENV} is not 1), so it would use your real levain home (~/.levain). Fix: {fix}.")
    me = object()
    if _WARNED.setdefault((os.getpid(), what), me) is not me:    # one atomic step (two threads cannot both pass); the
        return                                                   # pid in the key: a fork child warns for itself
    _warn(f"levain: warning: resolving {what} in your real levain home (~/.levain) from a process no levain launcher "
          f"marked ({LIVE_ENV} is not 1, {HOME_ENV} is unset). It works today; a later release refuses. Fix: {fix}.")
    _log_unmarked(what, env)


def _warn(line: str) -> None:
    """One line on descriptor 2 and nowhere else, and only when descriptor 2 is open and the process has not pointed
    stderr at stdout (``sys.stderr`` is None, or is the stdout object, or descriptor 2 is the same open file as
    descriptor 1 and not a terminal: a hook or MCP process keeps stdout for its protocol). Descriptor 2 itself, never the stream's own descriptor or buffer: a ``sys.stderr`` swapped for another
    object neither swallows the line nor carries it to stdout, and a write that fails leaves no bytes in a buffer for
    the interpreter's exit flush to fail on (that turns a clean exit into status 120). Never raises."""
    try:
        stream = sys.stderr
        if stream is None or stream is sys.stdout or stream is sys.__stdout__:
            return
        err = os.fstat(2)                                           # closed: OSError, no warning
        try:
            out = os.fstat(1)
        except OSError:
            out = None                                              # no stdout, so no protocol to corrupt
        if out is not None and (out.st_dev, out.st_ino) == (err.st_dev, err.st_ino) and not os.isatty(2):
            return                                                  # `2>&1`: descriptor 2 IS stdout's file (a terminal
                                                                    # both share is a person reading, not a protocol)
        os.write(2, (line + "\n").encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — a warning that cannot be written is not worth the run
        pass
