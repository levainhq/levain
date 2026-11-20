"""The parent's side of the warm derive child (``levain.cockpit.derive_child``): spawn it, ask it to derive one panel at a
time, and keep it honest (levain#157 S4, option (c)).

``DeriveChild.derive(panel_id, timeout_s)`` is the whole surface. It never raises into its caller: every way a derive can
not come back is a ``ChildAnswer`` with ``failure`` set, and the caller (the host that routes a panel here) turns that into
the row it publishes.

  failure None        the child answered; ``status`` / ``body`` / ``valid_until`` are what the in-process loop would have
                      made for that panel (an error answer included: the child is healthy, the derive failed).
  ``timeout``         the derive ran past ``timeout_s`` (or a caller waited that long for its turn). A derive that passed its
                      deadline has its child KILLED, not abandoned: a thread cannot be killed, a process can.
  ``died``            the child exited, closed its pipe, or could not be started or made ready.
  ``bad_reply``       the child answered something that is not a well-formed answer; it is killed, its stream cannot be trusted.
  ``refused``         no child was asked: the last failure's backoff has not elapsed (or the client is closed).

Supervision. The child starts on the first call, not at construction. After any failure that cost a child, the next child is
not started until a backoff has passed: ``backoff_base_s`` doubling on each consecutive failure up to ``BACKOFF_CAP_S``; a
call inside the backoff is refused at once, and one derive answered (error answers count) clears it. One derive runs at a
time; a second caller waits its turn for at most its own ``timeout_s``.

Kill. The child is its own session leader (``start_new_session=True``), so its process group is its own: it gets SIGTERM, a
short grace, then SIGKILL, and is always reaped (no zombie), and a subprocess a provider started in that group is not left
behind by the escalation. The group is signalled only while the leader is not yet reaped, so the group id cannot have been
reused. The environment is the parent's own copy, with the directory this package is imported from put first on
``PYTHONPATH``, so the child reads the same install the parent runs.
"""

from __future__ import annotations

import itertools
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

CHILD_MODULE = "levain.cockpit.derive_child"
# Every ``ChildAnswer.failure`` a caller can see.
FAILURES = ("timeout", "died", "refused", "bad_reply")
# How long a child may take to say it is ready: the factory builds the whole cockpit first (seconds).
READY_TIMEOUT_S = 30.0
# SIGTERM to SIGKILL (seconds).
TERM_GRACE_S = 1.0
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
# This package's import root: the child is put on the same one, whichever install the parent runs from.
_LEVAIN_ROOT = str(Path(__file__).resolve().parents[2])


@dataclass(frozen=True)
class ChildAnswer:
    """One ask's outcome. ``failure`` is None or one of ``FAILURES``; with a failure the other fields are None except
    ``error``, which says what happened. With none, ``status`` is the derive's status ("error" included), ``body`` and
    ``valid_until`` (epoch seconds, or None) are the loop's own, ``duration_ms`` is the child's measure of the derive, and
    ``error`` is the derive's error text when it has one."""

    status: "str | None"
    body: Any
    valid_until: "float | None"
    duration_ms: "int | None"
    failure: "str | None" = None
    error: "str | None" = None


def backoff_s(failures: int, base_s: float, cap_s: float) -> float:
    """How long the next child waits after ``failures`` consecutive failures: ``base_s``, doubling each time, never past ``cap_s``."""
    return min(cap_s, base_s * 2 ** min(max(failures, 1) - 1, 64))


class _Lost(Exception):
    """Internal: this ask cannot be answered; ``kind`` is the failure to report."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text


def _lost_answer(kind: str, text: str) -> ChildAnswer:
    return ChildAnswer(None, None, None, None, kind, text)


class DeriveChild:
    """One warm child for one factory. Thread safe: ``derive`` calls take turns."""

    def __init__(
        self,
        factory: str,
        *,
        syspath: Sequence[str] = (),
        python: "str | None" = None,
        env: "Mapping[str, str] | None" = None,
        stderr: Any = None,
        ready_timeout_s: float = READY_TIMEOUT_S,
        term_grace_s: float = TERM_GRACE_S,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_cap_s: float = BACKOFF_CAP_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._argv = [python or sys.executable, "-P", "-m", CHILD_MODULE, "--factory", factory]
        for d in syspath:
            self._argv += ["--syspath", str(d)]
        base = dict(os.environ if env is None else env)
        base["PYTHONPATH"] = os.pathsep.join([_LEVAIN_ROOT] + ([base["PYTHONPATH"]] if base.get("PYTHONPATH") else []))
        self._env = base
        self._stderr = stderr           # None: the child's stderr is this process's own (the serve's log)
        self._ready_s = ready_timeout_s
        self._grace_s = term_grace_s
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s
        self._clock = clock
        self._turn = threading.Lock()   # one ask at a time; also guards the fields below
        self._proc: "subprocess.Popen[bytes] | None" = None
        self._buf = b""
        self._ids = itertools.count(1)
        self._failures = 0              # consecutive failures that cost a child
        self._not_before = 0.0          # the earliest clock reading the next child may start
        self._closed = False

    # ---- the ask

    @property
    def pid(self) -> "int | None":
        """The live child's pid, or None when no child is running."""
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def derive(self, panel_id: str, timeout_s: float) -> ChildAnswer:
        """Ask the child to derive ``panel_id``; wait at most ``timeout_s`` for the turn and then at most ``timeout_s`` for
        the answer. Never raises."""
        if not self._turn.acquire(timeout=max(0.0, timeout_s)):
            return _lost_answer("timeout", f"waited {timeout_s:g}s for the child's turn")
        try:
            try:
                return self._ask(panel_id, timeout_s)
            except _Lost as lost:
                self._failed()
                return _lost_answer(lost.kind, lost.text)
            except Exception as exc:  # noqa: BLE001 - this method's promise: nothing raises into the loop
                self._failed()
                return _lost_answer("died", f"{type(exc).__name__}: {exc}")
        finally:
            self._turn.release()

    def _ask(self, panel_id: str, timeout_s: float) -> ChildAnswer:
        if self._closed:
            return _lost_answer("refused", "the child is closed")
        if self._proc is not None and self._proc.poll() is not None:
            self._kill()                # it died between asks (killed from outside, say): reaped, and the next child starts now
        if self._proc is None:
            now = self._clock()
            if now < self._not_before:
                return _lost_answer("refused", f"the child's backoff has {self._not_before - now:.1f}s left")
            self._spawn()
        proc = self._proc
        assert proc is not None
        rid = next(self._ids)
        line = json.dumps({"id": rid, "op": "derive", "panel": panel_id}, separators=(",", ":")).encode() + b"\n"
        try:
            os.write(proc.stdin.fileno(), line)     # type: ignore[union-attr]  # tiny: the child is idle and the pipe is empty
        except OSError as exc:
            raise _Lost("died", self._exit_text(proc, f"the request could not be sent ({exc})")) from None
        frame = self._frame(self._clock() + timeout_s, f"the derive of {panel_id!r} passed {timeout_s:g}s")
        return self._answer(frame, rid)

    def _answer(self, frame: Any, rid: int) -> ChildAnswer:
        bad = lambda why: _Lost("bad_reply", f"the child's answer is malformed: {why}")  # noqa: E731
        if not isinstance(frame, dict) or frame.get("id") != rid:
            raise bad(f"not the answer to request {rid}")
        status, valid_until, ms, error = frame.get("status"), frame.get("valid_until"), frame.get("duration_ms"), frame.get("error")
        if not isinstance(status, str):
            raise bad("status is not a string")
        if valid_until is not None and (isinstance(valid_until, bool) or not isinstance(valid_until, (int, float))):
            raise bad("valid_until is not a number")
        if isinstance(ms, bool) or not isinstance(ms, int):
            raise bad("duration_ms is not an integer")
        if error is not None and not isinstance(error, str):
            raise bad("error is not a string")
        self._failures = 0              # a child that answers is a healthy one, whatever the derive said
        self._not_before = 0.0
        return ChildAnswer(status, frame.get("body"), None if valid_until is None else float(valid_until), ms, None, error)

    # ---- the child's life

    def _spawn(self) -> None:
        try:
            proc = subprocess.Popen(
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr, env=self._env,
                close_fds=True, start_new_session=True)
        except OSError as exc:
            raise _Lost("died", f"the child could not be started: {exc}") from None
        self._proc, self._buf = proc, b""
        frame = self._frame(self._clock() + self._ready_s, f"the child was not ready in {self._ready_s:g}s")
        if not isinstance(frame, dict) or frame.get("op") not in ("ready", "fatal"):
            raise _Lost("bad_reply", "the child's first frame is not a ready frame")
        if frame["op"] == "fatal":
            raise _Lost("died", f"the child could not build its cockpit: {frame.get('error')}")

    def _frame(self, deadline: float, timeout_text: str) -> Any:
        """The next JSON line from the child. ``_Lost`` on timeout, EOF (the child is dead) or a line that is not JSON."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        fd = proc.stdout.fileno()
        while b"\n" not in self._buf:
            left = deadline - self._clock()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise _Lost("timeout", timeout_text)
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise _Lost("died", self._exit_text(proc, "the child closed its pipe"))
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            raise _Lost("bad_reply", "the child sent a line that is not JSON") from None

    def _exit_text(self, proc: "subprocess.Popen[bytes]", what: str) -> str:
        try:
            code = proc.wait(timeout=self._grace_s)
        except subprocess.TimeoutExpired:
            return what
        return f"{what}; it exited {code}"

    def _failed(self) -> None:
        """A failure cost the child: it is killed and reaped, and the next one waits out a longer backoff."""
        self._kill()
        self._failures += 1
        self._not_before = self._clock() + backoff_s(self._failures, self._backoff_base_s, self._backoff_cap_s)

    def _kill(self) -> None:
        proc, self._proc, self._buf = self._proc, None, b""
        if proc is not None:
            _terminate(proc, self._grace_s)

    # ---- the end

    def close(self) -> None:
        """Stop the child (asked to stop, then killed) and refuse every later ask. Idempotent. A derive running in another
        thread is cut short: it sees its child die and answers ``died``."""
        self._closed = True
        if self._turn.acquire(timeout=self._grace_s):
            try:
                proc = self._proc
                if proc is not None and proc.poll() is None:
                    try:
                        os.write(proc.stdin.fileno(), b'{"op":"stop"}\n')     # type: ignore[union-attr]
                        proc.wait(timeout=self._grace_s)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                self._kill()
            finally:
                self._turn.release()
        else:
            proc = self._proc            # a derive holds the turn: end the child under it
            if proc is not None:
                _terminate(proc, self._grace_s)

    def __enter__(self) -> "DeriveChild":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _terminate(proc: "subprocess.Popen[bytes]", grace_s: float) -> None:
    """End ``proc`` and its process group, reap it, and close its pipes. Safe to call twice and from any thread."""
    try:
        if proc.poll() is None:
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                _signal_group(proc.pid, signal.SIGKILL)     # the leader is not reaped yet: its group id is still ours
                proc.wait()
    finally:
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
