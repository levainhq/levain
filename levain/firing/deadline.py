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

   The accepted cost of layer 2, stated precisely because the first version of this paragraph got
   it WRONG (codex L3, HIGH): a hard exit skips confinement teardown, and the sandboxed bash is
   **NOT** reaped by the OS. ``os._exit`` ends only THIS process; the SIGTERM-then-SIGKILL of the
   shell's process GROUP lives in ``confinement.SandboxedShell.close`` and is simply never reached.
   So a bash child mid-command is ORPHANED — reparented to init/launchd and left running, possibly
   while launchd starts the next seat turn. "The OS will clean it up" was a comfortable assumption,
   not a fact, and stating it in a report an operator reads would have made this module lie about
   its own blast radius.

   That cost is still worth paying — an orphaned child is recoverable, a seat that never runs again
   is not — and it is bounded in practice by layer 1 now being the path that actually fires (the
   ``BaseException`` fix below). But it is a REAL residual, so the hard report says so in those
   words rather than reassuring words.

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

import math
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


def format_timeout_report(
    seconds: float, *, hard: bool, captured: bool | None = False,
) -> str:
    """The operator-facing explanation of a timeout — the log line that names it.

    Both layers must SAY SO in the log, because the whole defect being closed is a seat that stopped
    with nothing recording why. ``hard`` distinguishes the backstop firing, which also means layer 1
    failed — a fact worth surfacing rather than smoothing over, since "this entity's stalls are not
    interruptible by signal" changes what a supervisor should investigate.

    ``captured`` is TRI-STATE, and it exists because the obvious wording was FALSE in one real window
    (codex L3, MED). ``False`` — the turn was cut down before capture, which the session itself can
    vouch for. ``None`` — WE DO NOT KNOW: the bound wraps rendering and teardown as well as the turn,
    so an alarm can land *after* ``_drive`` already captured a completed turn. Asserting "nothing was
    captured to memory" there tells an operator their episode store is missing work it actually holds,
    and invites a supervisor to re-run work that already succeeded and was recorded. The same
    authority-versus-description discipline as :attr:`levain.session.TurnResult.gated`: only claim
    what the caller can actually vouch for.
    """
    if captured is None:
        memory = (
            "     Whether this turn reached memory is UNKNOWN — the bound also covers output and "
            "teardown, so it may\n"
            "     have been captured before the stop. Check the entity's episodes rather than "
            "assuming either way."
        )
    elif captured:
        memory = (
            "     The turn HAD already been captured to memory before the bound fired; that episode "
            "stands."
        )
    else:
        memory = (
            "     Nothing was captured to memory: a turn killed mid-flight has no completed work to "
            "record, and an\n"
            "     episode asserting otherwise would be memory recording a fiction."
        )

    if hard:
        return (
            f"  ⏱ WALL-CLOCK BOUND EXCEEDED ({seconds:g}s) — and the graceful stop did NOT take, "
            f"so the process was terminated outright after a {HARD_EXIT_GRACE_SECONDS:g}s grace "
            f"period.\n"
            f"{memory}\n"
            f"     ⚠ CLEANUP WAS SKIPPED, and the confined bash is NOT reaped for you: a child "
            f"mid-command is ORPHANED\n"
            f"     (reparented to init), because the process-group kill lives in the teardown this "
            f"exit skipped.\n"
            f"     That the backstop was needed is itself a finding — the stall was not "
            f"interruptible by signal\n"
            f"     (a blocking C call, or a non-main thread).\n"
            f"     The turn is over; the next scheduled run is free to start."
        )
    return (
        f"  ⏱ WALL-CLOCK BOUND EXCEEDED ({seconds:g}s) — the turn was terminated.\n"
        f"{memory}\n"
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
        # A NON-FINITE BOUND IS REFUSED HERE, LOUDLY (codex L3, HIGH — the nastiest of the set,
        # because it produces a run that LOOKS bounded and is not). `nan` passes a `< 0` guard
        # (every nan comparison is False), then fails `> 0` in `enabled`, so NOTHING arms — while the
        # seat's plist carries `--max-seconds nan` and the banner prints `nans wall-clock`. An
        # operator reading either would conclude the seat is bounded. `inf` is worse in the other
        # direction: it reaches `signal.setitimer`, which raises `OverflowError` ("timestamp out of
        # range for platform time_t") — a type that was NOT in the caught set, so it escaped
        # `__enter__` as a traceback. Both are silent-or-crashing failures of the exact mechanism
        # this class exists to guarantee, so neither may be representable.
        if seconds is not None and not math.isfinite(seconds):
            raise ValueError(
                f"a wall-clock bound must be a finite number of seconds, got {seconds!r}. "
                f"Pass None (or 0 at the CLI) to run unbounded — deliberately and visibly."
            )
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
        return self.seconds is not None and math.isfinite(self.seconds) and self.seconds > 0

    def __enter__(self) -> TurnDeadline:
        # RE-ENTRY IS REFUSED, not merely documented as unsupported (codex L3, MED). The class doc
        # said "re-usable, not re-entrant" and nothing enforced it, and the failure is silent in the
        # dangerous direction: a nested `with` overwrites `self._watchdog`, then the INNER `__exit__`
        # clears the single `_active` flag and cancels only the inner timer — so the original
        # watchdog survives but returns immediately (`_active` is False), the itimer is cancelled,
        # and the OUTER body runs with NO layer 1 and NO effective layer 2. An unbounded turn wearing
        # a bounded turn's syntax. `a_policy_is_not_real_until_the_thing_that_enforces_it_exists`.
        if self._active:
            raise RuntimeError(
                "TurnDeadline is not re-entrant — this instance is already bounding a turn. "
                "Nesting would silently leave the OUTER turn unbounded. Use a separate instance."
            )
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
            # SET THE INSTANT THE HANDLER IS INSTALLED, BEFORE the timer (codex L3, MED), and
            # deliberately NOT cleared by the handler below.
            #
            # ⚠ THE FLAG MEANS "WE TOUCHED SIGALRM AND OWE A RESTORE" — not "layer 1 is live". That
            # distinction is the whole fix, and the first attempt at it still failed its own test:
            # setting the flag early is useless if the `except` clause then resets it, because
            # `_disarm_signal` gates on it and would return early, leaving OUR handler installed on a
            # host that never asked for it. Restoring an un-armed timer costs a no-op `setitimer(0)`;
            # failing to restore an installed handler means a later host alarm raises `TurnTimeout`
            # into unrelated code. The asymmetry decides it.
            self._armed_signal = True
            signal.setitimer(signal.ITIMER_REAL, seconds)
        except (ValueError, OSError, OverflowError, AttributeError):
            # Off the main thread, or a platform without SIGALRM. NOT fatal and NOT silent-green:
            # layer 2 is already armed above, so the turn remains bounded — only the CLEANLINESS of
            # the stop is lost, which is the correct thing to degrade. `_armed_signal` is left ALONE
            # (see above): whatever we installed still has to be handed back.
            pass

    def _on_alarm(self, signum: int, frame: FrameType | None) -> None:
        if not self._active:
            return  # a late delivery racing __exit__ — the turn already finished
        assert self.seconds is not None
        raise TurnTimeout(self.seconds)

    def _disarm_signal(self) -> None:
        if not self._armed_signal:
            return
        # TWO INDEPENDENT TEARDOWN ACTIONS, TWO SEPARATE `try` BLOCKS — and the separation is a
        # defect fix, not tidiness. Sharing one `try` meant the FIRST failure skipped the SECOND, and
        # the realistic way `setitimer` fails is *the same way it failed at arming time* (a platform
        # or thread that will not have it). So exactly when the handler most needs putting back, the
        # cancel raised and the restore never ran — leaving Levain's handler installed on a host that
        # never asked for it, which is the failure this method exists to prevent.
        #
        # Found by the regression test written for the codex MED above, which failed against the
        # first fix. `an_invariant_catches_the_defect_it_was_not_written_for`.
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except (ValueError, OSError):
            pass  # a timer we could not cancel must not cost us the handler restore below
        try:
            if callable(self._previous_handler) or isinstance(self._previous_handler, int):
                signal.signal(signal.SIGALRM, self._previous_handler)  # type: ignore[arg-type]
            else:
                # `signal.getsignal` returns None when the previous handler was installed from C and
                # is not introspectable from Python — it CANNOT be restored (codex L3, MED). Falling
                # back to SIG_DFL is the honest choice: leaving OUR handler installed would mean a
                # later host alarm raises `TurnTimeout` into unrelated code, a failure we would have
                # caused; SIG_DFL is merely the platform default we cannot improve on.
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
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
            # `captured=None`: a watchdog thread cannot know what the main thread reached.
            print(
                format_timeout_report(self.seconds, hard=True, captured=None),
                file=stream, flush=True,
            )
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
