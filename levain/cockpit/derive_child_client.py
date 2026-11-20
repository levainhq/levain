"""The parent's side of the warm derive child (``levain.cockpit.derive_child``): spawn it, ask it to derive one panel at a
time, and keep it honest (levain#157 S4, option (c)).

``DeriveChild.derive(panel_id, timeout_s)`` is the whole surface a derive uses. It never raises into its caller: every way a
derive can not come back is a ``ChildAnswer`` with ``failure`` set, and the caller (the host that routes a panel here) turns
that into the row it publishes.

  failure None        the child answered; ``status`` / ``body`` / ``valid_until`` are what the in-process loop would have
                      made for that panel (an error answer included: the child is healthy, the derive failed).
  ``timeout``         the call ran past ``timeout_s``: waiting for its turn, for a child still starting, or (``hung`` True)
                      for the derive itself, whose child is then KILLED, not abandoned: a thread cannot be killed, a process can.
  ``died``            the child exited, closed its pipe, spoke another protocol, or could not be started or made ready.
  ``bad_reply``       the child answered something that is not a well-formed answer; it is killed, its stream cannot be trusted.
  ``refused``         no child was asked: the last failure's backoff has not elapsed, the client is closed, or ``timeout_s``
                      is not a finite number over zero. Nothing is touched: the child and the backoff stay as they were.

One deadline. ``timeout_s`` is spent from the moment of the call: the wait for the turn, the start of a child and the answer all
draw on it, none restarts it. A child still building its cockpit when the deadline comes is not the derive's fault: the call
answers ``timeout`` and the child is left starting (its own ``ready_timeout_s`` bounds the build) for the next ask.

What the child holds. The ``ready`` frame lists the panels the child's factory built; ``knows(panel_id)`` says whether a panel
is one of them. A host routes only panels the child knows, and ``start()`` / ``warm()`` bring the child up so the list is known.

Supervision. The child starts on first use, not at construction. After any failure that cost a child, the next child is not
started until a backoff has passed: ``backoff_base_s`` doubling on each consecutive failure up to ``BACKOFF_CAP_S``; a call
inside the backoff is refused at once, and one derive answered (error answers count) clears it. One derive runs at a time.

Kill. The child is its own session leader (``start_new_session=True``), so its process group is its own. Ending it is: SIGTERM
to the group, a short grace, then SIGKILL to the group BEFORE the leader is reaped, always (a leader that is a zombie still holds
the group id, so the group cannot belong to anyone else and a helper that ignored SIGTERM, or outlived a leader that exited by
itself, does not survive), then the reap and the pipes closed. Only the thread that holds the turn reaps and closes pipes;
``close()`` from another thread signals the group and leaves the rest to the holder, who ends a closed client's child before
it lets go of the turn, whatever way its own call ended. A leader still unreaped after SIGKILL and the reap bound is handed to a
reaper thread that keeps the Popen until it has been collected. The environment is the parent's own copy,
with the directory this package is imported from put first on ``PYTHONPATH``, so the child reads the same install the parent runs.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

CHILD_MODULE = "levain.cockpit.derive_child"
# The protocol the child and this client speak; the child's ``ready`` frame names it and a client talks to no other.
PROTOCOL = "levain-derive-child/1"
# The most panel ids a ready frame lists (the child truncates its sorted list there; a longer list is a bad frame).
READY_PANELS_MAX = 4096
# Every ``ChildAnswer.failure`` a caller can see.
FAILURES = ("timeout", "died", "refused", "bad_reply")
# How long a child may take to say it is ready: the factory builds the whole cockpit first (seconds).
READY_TIMEOUT_S = 30.0
# SIGTERM to SIGKILL (seconds).
TERM_GRACE_S = 1.0
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
# The longest a single poll waits (seconds): a very long deadline is waited out in slices, never as one huge timeout.
_SLICE_S = 30.0
# How long the reap of a SIGKILLed leader may take before the client gives up on it (it should be instant).
_REAP_BOUND_S = 10.0
# This package's import root: the child is put on the same one, whichever install the parent runs from.
_LEVAIN_ROOT = str(Path(__file__).resolve().parents[2])
# Held while a leader is checked for "not yet reaped" and signalled, and while one is reaped: the group id is only signalled
# while its leader is still ours, so another thread's reap cannot free the id between the check and the signal.
_SIG_LOCK = threading.Lock()


@dataclass(frozen=True)
class ChildAnswer:
    """One ask's outcome. ``failure`` is None or one of ``FAILURES``; with a failure the other fields are None except
    ``error``, which says what happened. With none, ``status`` is the derive's status ("error" included), ``body`` and
    ``valid_until`` (epoch seconds, or None) are the loop's own, ``duration_ms`` is the child's measure of the derive, and
    ``error`` is the derive's error text when it has one. ``hung`` is True only for a ``timeout`` that killed the child at
    this derive's own deadline (the panel is the one that did not come back)."""

    status: "str | None"
    body: Any
    valid_until: "float | None"
    duration_ms: "int | None"
    failure: "str | None" = None
    error: "str | None" = None
    hung: bool = False


def backoff_s(failures: int, base_s: float, cap_s: float) -> float:
    """How long the next child waits after ``failures`` consecutive failures: ``base_s``, doubling each time, never past ``cap_s``."""
    return min(cap_s, base_s * 2 ** min(max(failures, 1) - 1, 64))


class _Lost(Exception):
    """Internal: this ask cannot be answered; ``kind`` is the failure to report. ``costly``: the failure cost the child (it is
    killed and the backoff grows); ``hung``: the derive itself ran past its deadline."""

    def __init__(self, kind: str, text: str, *, costly: bool = True, hung: bool = False) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text
        self.costly = costly
        self.hung = hung


def _lost_answer(kind: str, text: str, hung: bool = False) -> ChildAnswer:
    return ChildAnswer(None, None, None, None, kind, text, hung)


def _valid_timeout(timeout_s: Any) -> bool:
    """A positive number that is finite as a float (an int too big for a float is not: ``OverflowError`` is a no)."""
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
        return False
    try:
        return math.isfinite(float(timeout_s)) and timeout_s > 0
    except (OverflowError, ValueError):
        return False


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` (``os.write`` may take fewer than it is given)."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


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
        # ``--parent-pid``: the child watches this process, even if it is started after this one is gone
        self._argv = [python or sys.executable, "-P", "-m", CHILD_MODULE, "--factory", factory, "--parent-pid", str(os.getpid())]
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
        self._turn = threading.Lock()   # one ask at a time; the holder owns the fields below
        self._proc: "subprocess.Popen[bytes] | None" = None
        self._buf = b""
        self._ready = False             # the held child has sent its ready frame
        self._spawned_at = 0.0          # the clock reading its build began at
        self._panels: "frozenset[str] | None" = None    # what the last ready frame listed; None until a child has been ready
        self._ids = itertools.count(1)
        self._failures = 0              # consecutive failures that cost a child
        self._not_before = 0.0          # the earliest clock reading the next child may start
        self._closed = False
        self._warm_lock = threading.Lock()
        self._warming = False

    # ---- what the child is

    @property
    def pid(self) -> "int | None":
        """The pid of the child this client holds (not yet reaped), or None when there is none."""
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    def knows(self, panel_id: str) -> bool:
        """Whether the child's factory built ``panel_id`` (the last ready frame's list). False until a child has been ready."""
        panels = self._panels
        return panels is not None and isinstance(panel_id, str) and panel_id in panels

    def start(self, timeout_s: float) -> bool:
        """Bring the child up and wait at most ``timeout_s`` for it to be ready. True when it is. Never raises; a failure that
        costs a child is counted like any other (the backoff applies). A ``timeout_s`` that is the client's ``ready_timeout_s``
        or more (what ``warm`` asks) waits through the child's own ready bound: a child still not ready at that bound is killed
        and backed off, as it is for any ask. That call is bounded by ``timeout_s`` for the turn, then the ready bound counted
        from the spawn, then the ending of a child that failed (see ``derive``): not by ``timeout_s`` alone. A shorter
        ``timeout_s`` is a wait only: a child still building is left to build."""
        if not _valid_timeout(timeout_s):
            return False
        acquired = False
        try:
            deadline = self._clock() + timeout_s
            acquired = self._turn.acquire(timeout=min(timeout_s, threading.TIMEOUT_MAX))
            if not acquired:
                return False
            try:
                self._ensure_ready(deadline, through_ready=timeout_s >= self._ready_s)
                return True
            except _Lost as lost:
                if lost.costly:
                    self._failed()
                return False
            except Exception:  # noqa: BLE001 - this method's promise: nothing raises
                self._failed()
                return False
        except Exception:  # noqa: BLE001
            return False
        finally:
            if acquired:
                self._release_turn()

    def warm(self) -> None:
        """Start the child in the background (a thread) so ``knows`` can answer; returns at once, and does nothing when a
        warm-up is already running, the child is ready, or the client is closed."""
        with self._warm_lock:
            if self._warming or self._closed or self._ready:
                return
            self._warming = True

        def run() -> None:
            try:
                self.start(self._ready_s)
            finally:
                self._warming = False

        threading.Thread(target=run, name="derive-child-warm", daemon=True).start()

    # ---- the ask

    def derive(self, panel_id: str, timeout_s: float) -> ChildAnswer:
        """Ask the child to derive ``panel_id``. ``timeout_s`` (a positive finite number: anything else, an integer too big
        for a float included, is ``refused`` and touches nothing) bounds the wait for the turn, the start and the answer
        together, counted from this moment. The call returns by then, except that a failure that ENDS the child takes the
        ending too: SIGTERM, up to ``term_grace_s`` for a child that will not leave, then SIGKILL and the reap, which waits at
        most ``_REAP_BOUND_S`` before the leader is left to a reaper thread. So the real worst case is ``timeout_s`` plus
        ``term_grace_s`` plus ``_REAP_BOUND_S`` (a leader that ignores SIGTERM costs the whole grace; one stuck in the kernel
        after SIGKILL costs the reap bound and then does not hold the call). Never raises."""
        if not _valid_timeout(timeout_s):
            return _lost_answer("refused", f"timeout_s must be a finite number over 0, not {timeout_s!r}")
        acquired = False
        try:
            deadline = self._clock() + timeout_s
            acquired = self._turn.acquire(timeout=min(timeout_s, threading.TIMEOUT_MAX))
            if not acquired:
                return _lost_answer("timeout", f"waited {timeout_s:g}s for the child's turn")
            try:
                return self._ask(panel_id, deadline, timeout_s)
            except _Lost as lost:
                if lost.costly:
                    self._failed()
                return _lost_answer(lost.kind, lost.text, lost.hung)
            except Exception as exc:  # noqa: BLE001 - this method's promise: nothing raises into the loop
                self._failed()
                return _lost_answer("died", f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            return _lost_answer("died", f"{type(exc).__name__}: {exc}")
        finally:
            if acquired:
                self._release_turn()

    def _ask(self, panel_id: str, deadline: float, timeout_s: float) -> ChildAnswer:
        proc = self._ensure_ready(deadline)
        if self._clock() >= deadline:       # the start used the whole call: nothing was asked, so nothing is blamed on the panel
            raise _Lost("timeout", "the child was ready only when the deadline came", costly=False)
        rid = next(self._ids)
        line = json.dumps({"id": rid, "op": "derive", "panel": panel_id}, separators=(",", ":")).encode() + b"\n"
        try:
            _write_all(proc.stdin.fileno(), line)       # type: ignore[union-attr]  # tiny: the child is idle and the pipe is empty
        except (OSError, ValueError) as exc:
            raise _Lost("died", self._exit_text(proc, f"the request could not be sent ({exc})")) from None
        try:
            frame = self._frame(deadline, f"the derive of {panel_id!r} passed {timeout_s:g}s")
        except _Lost as lost:
            if lost.kind == "timeout":
                lost.hung = True
            raise
        return self._answer(frame, rid)

    def _answer(self, frame: Any, rid: int) -> ChildAnswer:
        bad = lambda why: _Lost("bad_reply", f"the child's answer is malformed: {why}")  # noqa: E731
        if not isinstance(frame, dict) or frame.get("id") != rid:
            raise bad(f"not the answer to request {rid}")
        status, valid_until, ms, error = frame.get("status"), frame.get("valid_until"), frame.get("duration_ms"), frame.get("error")
        if not isinstance(status, str):
            raise bad("status is not a string")
        if valid_until is not None and (isinstance(valid_until, bool) or not isinstance(valid_until, (int, float))
                                        or not math.isfinite(valid_until)):
            raise bad("valid_until is not a finite number")
        if isinstance(ms, bool) or not isinstance(ms, int):
            raise bad("duration_ms is not an integer")
        if error is not None and not isinstance(error, str):
            raise bad("error is not a string")
        self._failures = 0              # a child that answers is a healthy one, whatever the derive said
        self._not_before = 0.0
        return ChildAnswer(status, frame.get("body"), None if valid_until is None else float(valid_until), ms, None, error)

    # ---- the child's life

    def _ensure_ready(self, deadline: float, through_ready: bool = False) -> "subprocess.Popen[bytes]":
        """The held child once it is ready, starting one if there is none. The caller holds the turn. ``_Lost`` when it cannot
        be had by ``deadline`` (``costly`` says whether the child is lost with it). ``through_ready``: wait for the child's
        own ready bound whatever ``deadline`` says, and a child not ready by then is a costly failure."""
        if self._closed:
            raise _Lost("refused", "the child is closed", costly=False)
        proc = self._proc
        if proc is not None and self._gone(proc):
            self._kill()                # it died between asks (killed from outside, say): reaped, and the next child starts now
            proc = None
        if proc is None:
            now = self._clock()
            if now < self._not_before:
                raise _Lost("refused", f"the child's backoff has {self._not_before - now:.1f}s left", costly=False)
            proc = self._spawn()
            if self._closed:            # close() ran while this thread was starting the child: nobody else owns it
                self._kill()
                raise _Lost("refused", "the child is closed", costly=False)
        if not self._ready:
            self._await_ready(proc, deadline, through_ready)
        return proc

    def _spawn(self) -> "subprocess.Popen[bytes]":
        try:
            proc = subprocess.Popen(
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr, env=self._env,
                close_fds=True, start_new_session=True)
        except OSError as exc:
            raise _Lost("died", f"the child could not be started: {exc}") from None
        self._proc, self._buf, self._ready, self._spawned_at = proc, b"", False, self._clock()
        return proc

    def _await_ready(self, proc: "subprocess.Popen[bytes]", deadline: float, through_ready: bool = False) -> None:
        """Read the ready frame. The call's ``deadline`` ends the WAIT but not the child: one still building is left to build
        (the next ask waits for it); the child's own ready bound ends it for good, and a ``through_ready`` call waits to it."""
        ready_deadline = self._spawned_at + self._ready_s
        try:
            frame = self._frame(ready_deadline if through_ready else min(deadline, ready_deadline),
                                f"the child was not ready in {self._ready_s:g}s")
        except _Lost as lost:
            # a ``through_ready`` wait ends at the ready bound by construction, so its timeout is that bound whatever the clock reads
            if not through_ready and lost.kind == "timeout" and self._clock() < ready_deadline:
                raise _Lost("timeout", "the child is still starting", costly=False) from None
            raise
        if not isinstance(frame, dict) or frame.get("op") not in ("ready", "fatal"):
            raise _Lost("bad_reply", "the child's first frame is not a ready frame")
        if frame["op"] == "fatal":
            raise _Lost("died", f"the child could not build its cockpit: {frame.get('error')}")
        panels = frame.get("panels")
        if frame.get("protocol") != PROTOCOL:
            raise _Lost("died", f"the child's protocol is {frame.get('protocol')!r}, this client's is {PROTOCOL!r}")
        if frame.get("pid") != proc.pid:
            raise _Lost("died", f"the child's ready frame names pid {frame.get('pid')!r}, not {proc.pid}")
        if not isinstance(panels, list) or len(panels) > READY_PANELS_MAX or not all(isinstance(p, str) for p in panels):
            raise _Lost("bad_reply", "the child's ready frame lists its panels badly")
        self._panels = frozenset(panels)
        self._ready = True

    def _gone(self, proc: "subprocess.Popen[bytes]") -> bool:
        """Whether a child that should be idle has ended, found without reaping it: a ready child says nothing between asks, so
        a readable pipe is its end (or output no child may send, which ends it as well)."""
        if proc.returncode is not None:
            return True
        if not self._ready:
            return False
        try:
            with selectors.PollSelector() as sel:
                sel.register(proc.stdout.fileno(), selectors.EVENT_READ)    # type: ignore[union-attr]
                return bool(sel.select(0))
        except (OSError, ValueError):
            return True

    def _frame(self, deadline: float, timeout_text: str) -> Any:
        """The next JSON line from the child. ``_Lost`` on timeout, EOF (the child is dead) or a line that is not JSON."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        fd = proc.stdout.fileno()
        with selectors.PollSelector() as sel:       # poll, not select(): a descriptor past FD_SETSIZE is a normal one here
            sel.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buf:
                left = deadline - self._clock()
                if left <= 0:
                    raise _Lost("timeout", timeout_text)
                if not sel.select(min(left, _SLICE_S)):
                    continue
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
        # the leader is ending (its pipe closed): its group goes now, while the leader is still unreaped, then the reap says how it left
        _signal_group(proc, signal.SIGKILL)
        code = _reap(proc, self._grace_s)
        return what if code is None else f"{what}; it exited {code}"

    def _failed(self) -> None:
        """A failure cost the child: it is ended and reaped, and the next one waits out a longer backoff."""
        self._kill()
        self._failures += 1
        self._not_before = self._clock() + backoff_s(self._failures, self._backoff_base_s, self._backoff_cap_s)

    def _kill(self) -> None:
        proc, self._proc, self._buf, self._ready = self._proc, None, b"", False
        if proc is not None:
            _terminate(proc, self._grace_s)

    def _release_turn(self) -> None:
        """Let go of the turn. A ``close`` that could not take the turn killed the group and left the reaping to the holder; a
        holder that ended normally (it had read its answer, or timed out at no cost) would never reap, so a closed client's
        child is ended here, before the turn is let go. A ``close`` that set ``_closed`` just after that check and gave up on the
        turn just before the release is caught by the second look."""
        try:
            if self._closed and self._proc is not None:
                self._kill()
        except Exception:  # noqa: BLE001 - letting go of the turn must not raise past a derive's promise
            pass
        finally:
            self._turn.release()
        if self._closed and self._proc is not None and self._turn.acquire(blocking=False):
            try:
                if self._closed and self._proc is not None:
                    self._kill()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._turn.release()

    # ---- the end

    def close(self) -> None:
        """Stop the child (asked to stop, then ended) and refuse every later ask. Idempotent. A derive running in another
        thread is cut short: its child's group is killed under it, and it ends the child and answers ``died``."""
        self._closed = True
        if self._turn.acquire(timeout=self._grace_s):
            try:
                proc = self._proc
                if proc is not None and proc.returncode is None:
                    try:
                        _write_all(proc.stdin.fileno(), b'{"op":"stop"}\n')     # type: ignore[union-attr]
                    except (OSError, ValueError):       # the pipe is closed already (the caller's doing, or the child's)
                        pass
                    _wait_exit_unreaped(proc, self._grace_s)
                self._kill()
            finally:
                self._turn.release()
        else:
            proc = self._proc            # a derive holds the turn: signal only; the holder reaps and closes the pipes
            if proc is not None:
                _signal_group(proc, signal.SIGKILL)

    def __enter__(self) -> "DeriveChild":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---- ending a process group

def _signal_group(proc: "subprocess.Popen[bytes]", sig: int) -> None:
    """Signal ``proc``'s process group while its leader has not been reaped: its id is then still the group's."""
    with _SIG_LOCK:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass


def _wait_exit_unreaped(proc: "subprocess.Popen[bytes]", grace_s: float) -> None:
    """Wait up to ``grace_s`` for the leader to exit WITHOUT reaping it (its pipe to us reaches EOF when it does), so the group
    can still be signalled afterwards. Whatever it sends meanwhile is dropped."""
    try:
        fd = proc.stdout.fileno()                   # type: ignore[union-attr]
        end = time.monotonic() + max(0.0, grace_s)
        with selectors.PollSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    return
                if sel.select(left) and not os.read(fd, 65536):
                    return
    except (OSError, ValueError, AttributeError):
        return


def _reap(proc: "subprocess.Popen[bytes]", timeout_s: float) -> "int | None":
    """The leader's exit code once it is reaped, waiting at most ``timeout_s``; None if it is still running then."""
    end = time.monotonic() + max(0.0, timeout_s)
    while True:
        with _SIG_LOCK:
            code = proc.poll()
        if code is not None:
            return code
        if time.monotonic() >= end:
            return None
        time.sleep(0.005)


_REAPER_LOCK = threading.Lock()
_REAPER_QUEUE: "list[subprocess.Popen[bytes]]" = []
_REAPER_RUNNING = False
# How often the reaper looks at the leaders it still owes a wait (seconds).
_REAPER_TICK_S = 0.25


def _reap_later(proc: "subprocess.Popen[bytes]") -> None:
    """Hand a SIGKILLed leader the bounded reap could not collect to the one reaper thread, which owns it until it is reaped
    (it holds the Popen, so the leader is never dropped as a zombie). The thread starts on demand and ends when none is left."""
    global _REAPER_RUNNING
    with _REAPER_LOCK:
        _REAPER_QUEUE.append(proc)
        if _REAPER_RUNNING:
            return
        _REAPER_RUNNING = True
    threading.Thread(target=_reaper_loop, name="derive-child-reaper", daemon=True).start()


def _reaper_loop() -> None:
    global _REAPER_RUNNING
    while True:
        with _REAPER_LOCK:
            pending = list(_REAPER_QUEUE)
            if not pending:
                _REAPER_RUNNING = False
                return
        for proc in pending:
            try:
                with _SIG_LOCK:
                    done = proc.poll() is not None
            except Exception:  # noqa: BLE001 - a Popen that cannot be polled is not coming back
                done = True
            if done:
                with _REAPER_LOCK:
                    if proc in _REAPER_QUEUE:
                        _REAPER_QUEUE.remove(proc)
        time.sleep(_REAPER_TICK_S)


def _terminate(proc: "subprocess.Popen[bytes]", grace_s: float) -> None:
    """End ``proc`` and its process group, reap it, and close its pipes: SIGTERM to the group, the grace, SIGKILL to the group
    while the leader is still unreaped (whatever the leader did meanwhile), the reap. Only the thread that holds the child's
    turn calls this. Safe to call twice."""
    try:
        _signal_group(proc, signal.SIGTERM)
        _wait_exit_unreaped(proc, grace_s)
        _signal_group(proc, signal.SIGKILL)
        if _reap(proc, _REAP_BOUND_S) is None:
            _reap_later(proc)        # SIGKILLed but still in the kernel (an uninterruptible wait): somebody must still wait for it
    finally:
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
