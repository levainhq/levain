"""levain.firing.deadline — the WALL-CLOCK BOUND on one turn (K4a ⑥, ``spore-434``).

**The defect this closes.** ``--max-iterations`` bounds agent STEPS, not time. A turn that hangs
inside ONE step — a stalled model call to a cloud endpoint, a socket with no read timeout — never
exits. And launchd **COALESCES per label** (measured 2026-07-29: ``StartInterval=5`` with a 12s run
produces strictly serial runs), so launchd will never start the next turn while that one is alive.
The seat then stops running **FOREVER**, behind a unit still reporting installed and loaded, with
nothing in either log saying so — ``absence_of_signal_rendered_as_health`` inside the machinery
built to prevent exactly that.

**Why the bound is IN-PROCESS, which is load-bearing rather than incidental.** launchd has no
``RuntimeMaxSec`` equivalent and macOS ships no ``timeout`` binary. Enforcing this in the unit file
would therefore be possible on Linux (systemd *does* have ``RuntimeMaxSec``) and impossible on
macOS — two enforcement models for one requirement, which is the precise re-derivation trap
``K4c`` is already warned about for confinement. One in-process bound covers both platforms and
leaves ``RuntimeMaxSec`` available as optional defence-in-depth.

TWO LAYERS, AND THE SECOND IS THE POINT
=======================================

1. **SIGALRM — the graceful bound.** A real-time timer raises :class:`TurnTimeout` inside the main
   thread, interrupting a blocked socket read. The exception unwinds the driver's EXISTING error
   path, so the session closes normally: confinement torn down, the sandboxed bash process group
   killed, a distinct exit code returned. This is the layer that leaves a clean machine behind.

2. **A hard-exit watchdog — the backstop, because layer 1 can silently fail to fire.** Signal
   handlers only run in the **main thread**, and CPython delivers them only between bytecodes: a
   stall inside a C extension that holds the GIL, or an uninterruptible wait, will consume the
   signal and continue. **A wall-clock bound that can itself hang is not a bound** — shipping only
   layer 1 would produce a timeout that reports itself armed and does nothing, which is the same
   defect shape as the seat this closes, one level in. So a daemon thread armed at
   ``seconds + grace`` writes the report and calls :func:`os._exit` unconditionally, depending on
   none of the machinery it is bounding. It is deliberately the crudest possible mechanism: no
   locks, no imports, no cleanup that could itself block.

   The accepted cost of layer 2, stated rather than discovered later: a hard exit skips the
   confinement teardown, so the sandboxed bash subprocess is reaped by the OS rather than by our
   own process-group kill. That is strictly better than a seat that never runs again, and it only
   happens when layer 1 has already failed.

**WHO MAY ARM LAYER 2 — the scoping is a security/architecture decision, not a detail.** A hard
process exit belongs to whoever OWNS the process, so :class:`TurnDeadline` is armed by the headless
driver (:func:`levain.run.run_task`), never by :meth:`levain.session.EntitySession.run_turn` on
every caller's behalf. ``EntitySession`` is the SHARED long-lived API — K1 part 2's ``/turn`` route
will hold many sessions in ONE process, and a session object that can kill the interpreter because
one of N turns stalled is exactly the process-global footgun ``spore-438`` exists to name. A
threaded server bounds a turn by cancelling that conversation, not by taking the process down; the
session's only job here is to CLASSIFY a timeout correctly when one reaches it.

Pure stdlib, no ``levain`` imports — a dependency-isolated leaf like
:mod:`levain.firing.drive` and :mod:`levain.firing.gate`, so ``K4c``'s Linux work inherits it
rather than re-deriving it.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType
from typing import Literal

__all__ = [
    "HARD_EXIT_GRACE_SECONDS",
    "TurnDeadline",
    "TurnTimeout",
    "format_timeout_report",
]


HARD_EXIT_GRACE_SECONDS = 30.0
"""Seconds layer 1 gets to unwind before layer 2 takes the process down.

Sized to cover a normal teardown (closing the session kills a sandboxed bash process group and
releases the store lock), not to be generous. It is a grace period for an ORDERLY exit, so it must
be long enough that a healthy unwind is never truncated and short enough that a seat whose graceful
bound has failed still frees its slot well inside a typical cadence."""


class TurnTimeout(BaseException):
    """The turn exceeded its wall-clock bound and was terminated by layer 1.

    **``BaseException``, NOT ``Exception``, and this was measured rather than reasoned.** The first
    L4-live run of this bound against a genuinely stalled endpoint terminated at ``bound + grace``
    via layer 2, with layer 1 leaving no trace at all — because the agent SDK's LLM path and
    conversation loop are dotted with broad ``except Exception`` handlers (``llm.py`` alone has five)
    that caught the raise, classified it as an LLM error, and RETRIED straight back into the same
    dead socket. A bound whose signal is swallowed by the machinery it is bounding is not a bound.

    This is the exact reason :class:`KeyboardInterrupt`, :class:`SystemExit` and
    :class:`GeneratorExit` are ``BaseException`` in the standard library: an OUT-OF-BAND
    INTERRUPTION is not a program error, and code written to handle program errors must not be able
    to eat it. A wall-clock timeout is the same species of event — the operator's "stop", delivered
    by a clock instead of a keypress.

    A distinct TYPE on purpose, too. The driver classifies a timeout by catching this class, never by
    matching text in an error message: an outcome recognised from a *description* silently
    reclassifies the moment the wording changes, which is the same authority-versus-description
    collapse that :attr:`levain.session.TurnResult.gated` is a field rather than a derivation to
    avoid.

    ⚠ Consequence for callers, stated because it is easy to get wrong: ``except Exception`` will NOT
    catch this, deliberately. Every handler that must see it names it explicitly, and the ordering
    matters where both appear — see :meth:`levain.session.EntitySession._drive`.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(
            f"the turn exceeded its wall-clock bound of {seconds:g}s and was terminated"
        )
        self.seconds = seconds


def format_timeout_report(seconds: float, *, hard: bool) -> str:
    """The operator-facing explanation of a timeout — the log line that names it.

    Both layers must SAY SO in the log, because the whole defect being closed is a seat that
    stopped with nothing recording why. ``hard`` distinguishes the backstop firing (which also
    means layer 1 failed, a fact worth surfacing rather than smoothing over — it is the signal
    that this entity's stalls are not interruptible by signal, which changes what a supervisor
    should investigate).
    """
    if hard:
        return (
            f"  ⏱ WALL-CLOCK BOUND EXCEEDED ({seconds:g}s) — and the graceful stop did NOT take, "
            f"so the process was terminated outright after a {HARD_EXIT_GRACE_SECONDS:g}s grace "
            f"period.\n"
            f"     Nothing was captured to memory, and cleanup was SKIPPED: the confined bash "
            f"subprocess is left to the OS to reap.\n"
            f"     That the backstop was needed is itself a finding — the stall was not "
            f"interruptible by signal (a blocking C call, or a non-main thread).\n"
            f"     The turn is over; the next scheduled run is free to start."
        )
    return (
        f"  ⏱ WALL-CLOCK BOUND EXCEEDED ({seconds:g}s) — the turn was terminated.\n"
        f"     Nothing was captured to memory: a turn killed mid-flight has no completed work to "
        f"record, and an episode\n"
        f"     asserting otherwise would be memory recording a fiction.\n"
        f"     This is an ENVIRONMENT stall (a hung model call or socket), not a failed task — "
        f"the next scheduled run is free to start."
    )


class TurnDeadline:
    """Bound one turn in wall-clock time. A context manager; re-usable, not re-entrant.

    ``with TurnDeadline(600): result = session.run_turn(task)`` raises :class:`TurnTimeout` out of
    ``run_turn`` at 600s, and hard-exits the process at 630s if that raise did not land.

    ``seconds=None`` (or a non-positive value) arms NOTHING and is a transparent no-op, so a caller
    need not branch. That is deliberate: the alternative — a caller-side ``if`` around the
    ``with`` — duplicates the whole call site, and duplicated call sites drift.

    **Fails SOFT to the backstop, never open.** Arming layer 1 raises ``ValueError`` off the main
    thread (a CPython rule: ``signal.signal`` is main-thread-only). That is not an error here — the
    watchdog is thread-agnostic and still armed, so the turn stays bounded by the layer that does
    not care which thread it is on. The reverse asymmetry is what must never happen: layer 2 is the
    one that cannot be allowed to silently not arm, so its failure is reported loudly rather than
    swallowed.
    """

    def __init__(
        self,
        seconds: float | None,
        *,
        grace: float = HARD_EXIT_GRACE_SECONDS,
        hard_exit_code: int = 5,
        stream=None,
    ) -> None:
        self.seconds = seconds
        self.grace = grace
        self.hard_exit_code = hard_exit_code
        self._stream = stream
        self._active = False
        self._armed_signal = False
        self._previous_handler: object = None
        self._watchdog: threading.Timer | None = None

    # -- the guarded region --------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether this deadline bounds anything at all."""
        return self.seconds is not None and self.seconds > 0

    def __enter__(self) -> TurnDeadline:
        if not self.enabled:
            return self
        assert self.seconds is not None  # narrowed by `enabled`
        self._active = True
        # Layer 2 FIRST, and the order is deliberate: the backstop must already be armed before
        # the graceful layer is attempted, so a failure to arm layer 1 can never leave a window in
        # which the turn is running unbounded.
        self._arm_watchdog(self.seconds)
        self._arm_signal(self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        # `_active` is cleared FIRST so a signal delivered in the window between the body
        # finishing and the timer being cancelled is recognised as spurious and dropped, rather
        # than raising TurnTimeout out of some arbitrary line of the teardown below.
        self._active = False
        self._disarm_signal()
        self._disarm_watchdog()
        return False  # never suppress — a TurnTimeout is the caller's to classify

    # -- layer 1: SIGALRM ----------------------------------------------------

    def _arm_signal(self, seconds: float) -> None:
        try:
            self._previous_handler = signal.signal(signal.SIGALRM, self._on_alarm)
            signal.setitimer(signal.ITIMER_REAL, seconds)
            self._armed_signal = True
        except (ValueError, OSError, AttributeError):
            # Off the main thread, or a platform without SIGALRM. NOT fatal and NOT silent-green:
            # layer 2 is already armed above, so the turn remains bounded — only the CLEANLINESS
            # of the stop is lost, which is the correct thing to degrade.
            self._armed_signal = False

    def _on_alarm(self, signum: int, frame: FrameType | None) -> None:
        if not self._active:
            return  # a late delivery racing __exit__ — the turn already finished
        assert self.seconds is not None
        raise TurnTimeout(self.seconds)

    def _disarm_signal(self) -> None:
        if not self._armed_signal:
            return
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if callable(self._previous_handler) or isinstance(self._previous_handler, int):
                signal.signal(signal.SIGALRM, self._previous_handler)  # type: ignore[arg-type]
        except (ValueError, OSError):
            pass  # teardown must never mask the turn's own outcome
        finally:
            self._armed_signal = False

    # -- layer 2: the hard-exit watchdog -------------------------------------

    def _arm_watchdog(self, seconds: float) -> None:
        timer = threading.Timer(seconds + self.grace, self._hard_exit)
        # DEFENCE-IN-DEPTH, and deliberately NOT test-covered — recorded rather than faked.
        # `Timer.cancel()` sets the timer's finished event, so a cancelled timer returns at once
        # whether or not it is a daemon; since `__exit__` always disarms, a `daemon=False`
        # regression is currently UNOBSERVABLE (a mutation of this line survives the suite). It is
        # kept because the property it guarantees — never hold the interpreter open — must not
        # depend on teardown having run, and a test would have to manufacture a call sequence the
        # context-manager API does not permit. `the_instrument_needs_its_own_oracle`: the mutation
        # pass is what established this, rather than the comment asserting it.
        timer.daemon = True
        timer.start()
        self._watchdog = timer

    def _hard_exit(self) -> None:
        """Terminate the process. Called only when layer 1 has already failed to stop the turn."""
        if not self._active:
            return
        assert self.seconds is not None
        try:
            stream = self._stream if self._stream is not None else sys.stderr
            print(format_timeout_report(self.seconds, hard=True), file=stream, flush=True)
        except Exception:  # noqa: BLE001 — reporting must never prevent the termination
            pass
        # `os._exit`, not `sys.exit`: this runs on a watchdog THREAD, where SystemExit would only
        # unwind that thread and leave the stalled main thread running — the exact hang being
        # bounded. No atexit hooks, no buffered-output flush beyond the explicit one above, no
        # chance for a shutdown path to block.
        os._exit(self.hard_exit_code)

    def _disarm_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
