"""The WALL-CLOCK BOUND on a turn (K4a ⑥, ``spore-434``).

**Every test here is a FALSIFICATION PAIR, not an assertion that the timeout fired.** The spore is
explicit about why: K3's glm L3 HIGH was a test that stipulated its own success — it asserted the
rejection path worked by arranging for it to work. So the shape throughout is *same code, same
call, the BOUND is the only variable*: the short-work run must complete and the long-work run must
be terminated. A test that only ever ran the long case would pass just as happily against a bound
that fired unconditionally, which is a different and equally broken product.

The blocking work under test is a real ``time.sleep`` — a genuine blocking syscall — because that is
the failure being bounded (a stalled model call parked in a socket read). Proving the bound can
interrupt a Python loop that checks a flag would prove nothing about it.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time

import pytest

from levain.firing.deadline import (
    HARD_EXIT_GRACE_SECONDS,
    TurnDeadline,
    TurnTimeout,
    format_timeout_report,
)

# Short enough to keep the suite fast, long enough not to be flaky on a loaded machine.
_BOUND = 0.4
_LONG_WORK = 10.0     # must be terminated
_SHORT_WORK = 0.05    # must complete


# ---------- layer 1: the graceful bound, falsified against a control ----------

def test_the_bound_terminates_work_that_outruns_it() -> None:
    """THE HALF THAT MUST FIRE. A blocking sleep longer than the bound is interrupted.

    ``time.sleep`` is deliberate: it blocks in a syscall, which is exactly the shape of the stalled
    model call this exists for. That it is interrupted at all is the whole claim — SIGALRM reaching
    a thread parked in the kernel."""
    started = time.monotonic()
    with pytest.raises(TurnTimeout) as caught:
        with TurnDeadline(_BOUND):
            time.sleep(_LONG_WORK)
    elapsed = time.monotonic() - started

    assert caught.value.seconds == _BOUND
    # The point is not merely "it raised" — it raised EARLY. Without the bound this would have
    # taken _LONG_WORK; a generous ceiling still proves the work was cut short.
    assert elapsed < _LONG_WORK / 2, f"took {elapsed:.2f}s — the bound did not cut the work short"


def test_the_bound_SURVIVES_a_broad_except_exception_in_the_code_it_bounds() -> None:
    """THE REGRESSION TEST FOR THE DEFECT L4-LIVE FOUND, AND THE ONE THIS SUITE ORIGINALLY LACKED.

    The first live run of this bound against a genuinely stalled endpoint died at ``bound + grace``
    by hard kill, with layer 1 leaving no trace — because the agent SDK's LLM path wraps its work in
    broad ``except Exception`` handlers (five in ``llm.py`` alone), which caught the raise, treated it
    as an LLM error, and RETRIED into the same dead socket. Every test in this file was GREEN
    throughout, because they all bound a bare ``time.sleep`` and nothing was there to swallow
    anything. `the_device_is_the_oracle`.

    The fix is that ``TurnTimeout`` derives from ``BaseException``, for exactly the reason
    ``KeyboardInterrupt`` does. This test pins it BEHAVIOURALLY rather than asserting the base class,
    because what must hold is the property (a bound that escapes hostile handling), not the mechanism
    — assert the class and someone can satisfy the test while re-breaking the product with a
    ``except BaseException`` retry loop."""
    swallowed: list[str] = []

    def sdk_like_worker():
        """Stands in for the SDK: broad handling around a blocking call, exactly as shipped."""
        try:
            time.sleep(_LONG_WORK)
        except Exception as exc:  # noqa: BLE001 — the POINT of the test is that this is here
            swallowed.append(type(exc).__name__)

    started = time.monotonic()
    with pytest.raises(TurnTimeout):
        with TurnDeadline(_BOUND):
            sdk_like_worker()
    elapsed = time.monotonic() - started

    assert swallowed == [], f"the bound was swallowed by a broad handler: {swallowed}"
    assert elapsed < _LONG_WORK / 2, f"took {elapsed:.2f}s — the bound did not escape"


def test_the_bound_leaves_work_that_finishes_inside_it_ALONE() -> None:
    """THE CONTROL, and it is the half that makes the test above mean something.

    Identical construction, identical call, only the duration differs. A bound that fired
    unconditionally — or a `TurnTimeout` raised on exit regardless — would pass the test above and
    fail this one. Without this pair, neither result is evidence."""
    started = time.monotonic()
    with TurnDeadline(_BOUND):
        time.sleep(_SHORT_WORK)
    elapsed = time.monotonic() - started

    assert elapsed < _BOUND, "the control run should not have waited for the bound at all"


def test_the_bound_does_not_leak_into_the_code_that_follows_the_turn() -> None:
    """A spent-but-uncancelled timer would raise TurnTimeout in the middle of UNRELATED work.

    The real-world shape: a fast turn finishes at 2s under a 600s bound, the driver goes on to
    render output and close the session, and 598s later an orphaned ITIMER fires into whatever is
    running then. Cancellation is not hygiene here, it is correctness — and this is the test that
    would catch `__exit__` forgetting to disarm."""
    with TurnDeadline(_BOUND):
        pass  # finish immediately, well inside the bound

    # Outlive the bound OUTSIDE the guarded region. No exception may arrive.
    time.sleep(_BOUND * 3)


def test_a_late_signal_racing_the_exit_is_dropped_not_raised() -> None:
    """The window between the body finishing and the timer being cancelled.

    ``__exit__`` clears ``_active`` FIRST for exactly this: a signal already queued when teardown
    begins must be recognised as spent, not raised out of some arbitrary line of the teardown. Here
    the handler is invoked DIRECTLY after the region closed, which is the worst case of that race."""
    deadline = TurnDeadline(_BOUND)
    with deadline:
        pass
    # Simulating the delivery, not the timing: after the region, the handler must be inert.
    deadline._on_alarm(signal.SIGALRM, None)  # must NOT raise


def test_the_previous_sigalrm_handler_is_restored() -> None:
    """A library that permanently steals SIGALRM breaks its host.

    ``levain run`` owns its process, but ``EntitySession`` is the shared long-lived API and an
    embedding application may well use SIGALRM itself. Leaving our handler installed would turn a
    later unrelated alarm into a spurious `TurnTimeout`."""
    sentinel_called: list[bool] = []

    def sentinel(signum, frame):  # pragma: no cover — installed, not invoked
        sentinel_called.append(True)

    previous = signal.signal(signal.SIGALRM, sentinel)
    try:
        with TurnDeadline(_BOUND):
            pass
        assert signal.getsignal(signal.SIGALRM) is sentinel
    finally:
        signal.signal(signal.SIGALRM, previous)


# ---------- the disarmed cases: a non-bound must be a TRANSPARENT no-op ----------

@pytest.mark.parametrize("seconds", [None, 0, 0.0, -1, -0.5])
def test_a_non_positive_bound_arms_nothing(seconds) -> None:
    """``None`` / zero / negative all mean "not bounded", and must not half-arm anything.

    The negative case is the one with teeth. ``--max-seconds -1`` is a plausible typo, and a
    deadline that treated it as "already expired" would terminate every turn instantly, while one
    that armed a negative timer would be undefined. Both are worse than the CLI's answer, which is
    to refuse it outright (see ``test_cli``) — this pins the library's own behaviour underneath."""
    deadline = TurnDeadline(seconds)
    assert deadline.enabled is False
    with deadline:
        time.sleep(_SHORT_WORK)
    assert deadline._armed_signal is False
    assert deadline._watchdog is None


def test_disarmed_deadline_does_not_touch_the_sigalrm_handler() -> None:
    """A no-op bound must not even briefly install a handler — a caller passing ``None`` (every
    hand-run ``levain run --task``) should be indistinguishable from not using this module."""
    def sentinel(signum, frame):  # pragma: no cover
        pass

    previous = signal.signal(signal.SIGALRM, sentinel)
    try:
        with TurnDeadline(None):
            pass
        assert signal.getsignal(signal.SIGALRM) is sentinel
    finally:
        signal.signal(signal.SIGALRM, previous)


# ---------- layer 2: the hard-exit backstop, in a real subprocess ----------
#
# The backstop calls os._exit, so it CANNOT be exercised in-process — a test that could would be
# killing the test runner. It is driven as a subprocess, and the stall is made deliberately
# un-interruptible by layer 1 in the most honest way available: run the deadline OFF THE MAIN
# THREAD, where CPython refuses to install a signal handler at all. That is not a contrivance — it
# is precisely the documented degradation path ("fails SOFT to the backstop, never open"), so this
# tests the shipped claim rather than a mock of it.

_BACKSTOP_SCRIPT = textwrap.dedent(
    """
    import sys, threading, time
    sys.path.insert(0, {repo!r})
    from levain.firing.deadline import TurnDeadline

    def work():
        # Off the main thread: signal.signal() raises ValueError here, so layer 1 CANNOT arm.
        with TurnDeadline({bound!r}, grace={grace!r}, hard_exit_code=5):
            time.sleep({work!r})
        print("BODY-COMPLETED", flush=True)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    print("JOINED", flush=True)
    """
)


def _run_backstop(repo: str, *, work: float, bound: float = 0.4, grace: float = 0.4):
    script = _BACKSTOP_SCRIPT.format(repo=repo, bound=bound, grace=grace, work=work)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture()
def repo_root() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parent.parent)


def test_the_backstop_kills_a_stall_layer_one_cannot_reach(repo_root) -> None:
    """THE HALF THAT MUST FIRE. Off the main thread the graceful bound cannot arm — and the turn
    is still bounded, because the watchdog does not care which thread it is on.

    This is the test that makes the two-layer design worth its complexity. A single-layer SIGALRM
    bound would leave this process running forever while reporting itself bounded — the same
    `absence_of_signal_rendered_as_health` shape as the seat the whole slice exists to fix."""
    started = time.monotonic()
    proc = _run_backstop(repo_root, work=30.0)
    elapsed = time.monotonic() - started

    assert proc.returncode == 5, (
        f"expected the hard-exit code 5, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert elapsed < 15.0, f"the backstop took {elapsed:.1f}s — it did not bound the stall"
    # It must SAY SO. The entire defect being closed is a seat that stopped with nothing in any log
    # explaining why, so a silent kill would only relocate the problem.
    assert "WALL-CLOCK BOUND EXCEEDED" in proc.stderr
    # And it must not claim the turn finished.
    assert "BODY-COMPLETED" not in proc.stdout


def test_the_backstop_leaves_a_fast_turn_alone(repo_root) -> None:
    """THE CONTROL. Same script, same threading, same unarmed layer 1 — short work instead of long.

    A watchdog that fired regardless, or a daemon thread that held the interpreter open, would show
    up here as a non-zero exit or a hang. This is what proves the backstop is a bound and not a
    guillotine."""
    proc = _run_backstop(repo_root, work=0.05)

    assert proc.returncode == 0, (
        f"a fast turn must exit cleanly, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "BODY-COMPLETED" in proc.stdout
    assert "JOINED" in proc.stdout
    assert "WALL-CLOCK BOUND EXCEEDED" not in proc.stderr


def test_a_fast_turn_exits_promptly_under_a_long_bound(repo_root) -> None:
    """A 2-second turn under a 30-minute bound must exit in 2 seconds, not 30 minutes.

    ⚠ WHAT THIS DOES *NOT* PROVE, stated because the first version of this test claimed it did:
    it does not exercise ``timer.daemon = True``. ``Timer.cancel()`` sets the timer's finished
    event, so the thread wakes and returns immediately whether or not it is a daemon — meaning a
    ``daemon=False`` regression is INVISIBLE here, and a mutation run confirmed it (that mutation
    SURVIVED). The claim was corrected rather than the mutation explained away.

    ``daemon=True`` is therefore genuine defence-in-depth against a path where ``__exit__`` never
    runs, and it is deliberately NOT test-covered: with the context-manager form the only reachable
    path always disarms, so any test of it would have to fake a call sequence the API does not
    permit. Recorded here and in the source rather than papered over with a test that manufactures
    its own premise. What this test DOES pin is the property an operator feels — the process leaves
    when the work is done."""
    started = time.monotonic()
    proc = _run_backstop(repo_root, work=0.05, bound=20.0, grace=20.0)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0
    assert elapsed < 10.0, (
        f"exit took {elapsed:.1f}s against a 20s bound — something is holding the interpreter open"
    )


# ---------- the report: it must NAME the timeout ----------

def test_both_reports_name_the_bound_and_refuse_to_claim_a_capture() -> None:
    graceful = format_timeout_report(90.0, hard=False)
    hard = format_timeout_report(90.0, hard=True)

    for report in (graceful, hard):
        assert "90s" in report, "the report must name the bound that fired"
        assert "WALL-CLOCK BOUND EXCEEDED" in report
        # A timed-out turn captures nothing; saying so is what stops an operator assuming the
        # episode store holds a record of what happened.
        assert "captured" in report.lower()

    # The hard report must DISCLOSE that layer 1 failed rather than presenting itself as a normal
    # timeout — it changes what a supervisor should investigate (an un-interruptible stall).
    assert "did NOT take" in hard
    assert f"{HARD_EXIT_GRACE_SECONDS:g}s" in hard
    assert "did NOT take" not in graceful


def test_the_backstop_refuses_to_fire_after_the_turn_finished() -> None:
    """The ``_active`` guard on ``_hard_exit``, tested DIRECTLY — because it and the timer
    cancellation are defence-in-depth for each other, and either one alone is enough to keep a fast
    turn alive. That mutual cover is desirable in the product and a hole in the tests: remove either
    guard and every integration test still passes. So each is pinned on its own.

    ``os._exit`` is intercepted rather than allowed to run. That is not test convenience — a test
    that could actually perform the guarded act would take the test runner down with it, which is
    precisely the discipline K4a [7] paid for when a mutation run installed a real launchd agent."""
    from unittest import mock

    import levain.firing.deadline as mod

    deadline = TurnDeadline(_BOUND)
    with deadline:
        pass  # region closed — the backstop must now be inert

    with mock.patch.object(mod.os, "_exit") as exiter:
        deadline._hard_exit()
    assert exiter.call_count == 0, "the backstop fired AFTER the turn had already finished"


def test_the_backstop_does_fire_while_the_turn_is_live() -> None:
    """THE CONTROL for the guard above. Without it, ``_hard_exit`` hardcoded to return immediately
    would pass that test and ship a backstop that never fires — which is the whole defect."""
    from unittest import mock

    import levain.firing.deadline as mod

    deadline = TurnDeadline(_BOUND)
    with deadline:
        with mock.patch.object(mod.os, "_exit") as exiter:
            deadline._hard_exit()
    exiter.assert_called_once_with(5)


def test_the_grace_period_is_finite_and_leaves_room_for_teardown() -> None:
    """Both directions matter. Too short truncates a healthy unwind (killing a session that was
    already stopping, skipping the confined-shell teardown for no reason); too long reinstates the
    starvation being fixed, since the seat's slot stays held for bound+grace."""
    assert 0 < HARD_EXIT_GRACE_SECONDS <= 60


# ---------- the session classifies a timeout as a TIMEOUT, not a generic failure ----------
#
# `TurnTimeout` IS an `Exception`, so `_drive`'s broad handler will swallow it into an ordinary
# failed turn unless the narrow clause comes first. That collapse is invisible in normal operation —
# the turn still ends, the driver still exits non-zero — and it costs the one distinction a
# supervisor acts on: exit 3 says "read the traceback, the work is broken", exit 5 says "the
# endpoint stalled, the next scheduled run is fine".


class _CountingBinding:
    def __init__(self, tmp_path):
        self.entity_dir = tmp_path
        self.store_path = tmp_path / "store"
        self.crystal_path = tmp_path / "crystal.json"
        self.captures = 0

    def capture_turn(self, conversation):
        self.captures += 1

    def wrap_nudge(self, **kwargs):
        return None


class _State:
    events: list = []
    agent_state: dict = {}


class _StallingConversation:
    """A conversation whose ``run()`` is interrupted by the wall-clock bound, as a real one is."""

    def __init__(self, raise_on: str = "run") -> None:
        self.state = _State()
        self.raise_on = raise_on
        self.runs = 0

    def send_message(self, message):
        if self.raise_on == "send":
            raise TurnTimeout(_BOUND)

    def run(self):
        self.runs += 1
        if self.raise_on == "run":
            raise TurnTimeout(_BOUND)


class _FailingConversation:
    """The CONTROL: an ordinary exception, which must NOT be classified as a timeout."""

    def __init__(self) -> None:
        self.state = _State()

    def send_message(self, message):
        pass

    def run(self):
        raise RuntimeError("the model returned garbage")


def _session(tmp_path, conv):
    from levain.session import EntitySession

    return EntitySession(
        entity_dir=tmp_path,
        binding=_CountingBinding(tmp_path),
        conversation=conv,
        workspace=tmp_path / "workspace",
        model_label="ollama/glm-5.2:cloud",
        with_tools=True,
        bash_ok=True,
        gate_mode="ungated",
    )


@pytest.mark.parametrize("raise_on", ["run", "send"])
def test_a_timed_out_turn_is_classified_as_a_timeout_not_a_raise(tmp_path, raise_on) -> None:
    """THE HALF THAT MUST FIRE — and both statements of the turn are covered, because a bound whose
    meaning depends on which line it lands in is a bound nobody can reason about."""
    from levain.session import EXIT_TIMEOUT

    sess = _session(tmp_path, _StallingConversation(raise_on=raise_on))

    result = sess.run_turn("summarise the overnight findings")

    assert result.timed_out is True
    assert result.exit_code == EXIT_TIMEOUT == 5
    assert result.ok is False


def test_an_ordinary_failure_is_NOT_classified_as_a_timeout(tmp_path) -> None:
    """THE CONTROL. Same session, same call, a different exception — and it must still be exit 3.

    Without this, `timed_out=True` hardcoded on every error would pass the test above. This is also
    the test that fails if someone ever classifies a timeout by matching text in the error message:
    broaden that match and ordinary failures start reporting as stalls."""
    from levain.session import EXIT_TURN_FAILED

    sess = _session(tmp_path, _FailingConversation())

    result = sess.run_turn("summarise the overnight findings")

    assert result.timed_out is False
    assert result.exit_code == EXIT_TURN_FAILED == 3
    assert "garbage" in (result.error or "")


def test_a_timed_out_turn_is_NEVER_captured_to_memory(tmp_path) -> None:
    """Same discipline as the gated halt, for the same reason. The turn was killed mid-flight, so
    what completed is unknowable — and an episode asserting a completed turn would be a store
    perfectly grounded in a perfectly recorded fiction."""
    sess = _session(tmp_path, _StallingConversation())

    sess.run_turn("summarise the overnight findings")

    assert sess.binding.captures == 0, "a timed-out turn must not be written to memory"


def test_the_timeout_code_beats_the_error_code_when_BOTH_are_set() -> None:
    """The ordering inside ``exit_code``, pinned directly.

    A timed-out turn ALWAYS carries an error too (it did not complete), so checking ``error`` first
    would collapse every timeout into 3. This is the one-line regression that would silently undo
    the whole distinction, so it gets its own test rather than riding on the integration ones."""
    from levain.session import EXIT_TIMEOUT, TurnResult

    both = TurnResult(reply=None, tool_activity=[], error="bound exceeded", timed_out=True)

    assert both.exit_code == EXIT_TIMEOUT
    assert both.ok is False


def test_a_timed_out_turn_is_not_ok_even_with_a_partial_reply() -> None:
    """A reply produced before the bound fired does not make the turn complete — the work it was
    still doing was cut off, and reporting `ok` would hand a caller a half-turn as a whole one."""
    from levain.session import EXIT_TIMEOUT, TurnResult

    partial = TurnResult(reply="I'll start by reading the", tool_activity=[], timed_out=True)

    assert partial.ok is False
    assert partial.exit_code == EXIT_TIMEOUT


# ---------- the L3 findings (codex), each pinned ----------
#
# All seven were real, and two were opened BY the BaseException fix above — the change that made the
# graceful layer work at the turn boundary punched a hole at the teardown boundary. That pairing is
# the reason these are tests and not just fixes: the same trade will look tempting again.


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_NON_FINITE_bound_is_refused_LOUDLY(bad) -> None:
    """codex L3, HIGH — the nastiest finding of the set, because it produces a run that LOOKS bounded.

    `nan` defeats every comparison guard (`nan < 0` is False, so a `>= 0` validator passes it; `nan > 0`
    is also False, so `enabled` then arms NOTHING) while the seat's plist carries `--max-seconds nan`
    and the banner prints `nans wall-clock`. An operator auditing either would conclude the seat is
    time-bounded. `inf` fails the other way: it reaches `signal.setitimer`, which raises OverflowError
    — a type that was not in the caught set, so it escaped `__enter__` as a traceback.

    Refused in the CONSTRUCTOR, so an unbounded-but-configured deadline is unrepresentable rather than
    merely validated-against at the CLI."""
    with pytest.raises(ValueError, match="finite"):
        TurnDeadline(bad)


def test_None_is_still_a_legal_unbounded_deadline() -> None:
    """THE CONTROL for the guard above — "no bound" must stay expressible, or every hand-run
    `levain run --task` breaks. The rejection is of INCOHERENCE, not of running unbounded."""
    d = TurnDeadline(None)
    assert d.enabled is False
    with d:
        pass


def test_the_deadline_REFUSES_re_entry_instead_of_silently_unbounding_the_outer_turn() -> None:
    """codex L3, MED. The class doc said "re-usable, not re-entrant" and nothing enforced it.

    Nesting overwrites `self._watchdog`, then the INNER `__exit__` clears the single `_active` flag
    and cancels only the inner timer — leaving the original watchdog alive but inert (`_active` is
    False), the itimer cancelled, and the OUTER body running with no layer 1 AND no effective layer 2.
    An unbounded turn wearing a bounded turn's syntax, which is the worst possible failure shape for
    this class. `a_policy_is_not_real_until_the_thing_that_enforces_it_exists`."""
    d = TurnDeadline(_BOUND)
    with pytest.raises(RuntimeError, match="not re-entrant"):
        with d:
            with d:
                pass


def test_the_deadline_instance_is_REUSABLE_in_sequence() -> None:
    """THE CONTROL. Refusing re-entry must not refuse re-USE — a driver looping over turns with one
    deadline object has to keep working, and over-tightening the guard would break it silently."""
    d = TurnDeadline(_BOUND)
    for _ in range(3):
        with d:
            time.sleep(_SHORT_WORK)
    assert d._armed_signal is False
    assert d._watchdog is None


def test_the_host_sigalrm_handler_is_restored_even_when_ARMING_FAILS(monkeypatch) -> None:
    """codex L3, MED. `_armed_signal` used to be set AFTER `setitimer`, so a `setitimer` failure left
    OUR handler installed with the flag False — `_disarm_signal` then returned early and never
    restored the host's handler, and Levain silently owned SIGALRM for the rest of the process.

    The flag now flips the instant the handler is installed, which is what makes teardown honest."""
    import levain.firing.deadline as mod

    def sentinel(signum, frame):  # pragma: no cover
        pass

    def boom(which, value):
        raise OSError("setitimer refused")

    previous = signal.signal(signal.SIGALRM, sentinel)
    try:
        monkeypatch.setattr(mod.signal, "setitimer", boom)
        with TurnDeadline(_BOUND):
            pass
        assert signal.getsignal(signal.SIGALRM) is sentinel, (
            "arming failed and the host's handler was not put back"
        )
    finally:
        signal.signal(signal.SIGALRM, previous)


def test_the_report_does_not_CLAIM_a_capture_it_cannot_vouch_for() -> None:
    """codex L3, MED. The bound wraps rendering and teardown as well as the turn, so an alarm can land
    AFTER `_drive` already captured a completed turn — and the report asserted "nothing was captured to
    memory" in that window too. That tells an operator their store is missing work it actually holds,
    and invites a supervisor to re-run work that already succeeded.

    Tri-state, same authority-versus-description discipline as `TurnResult.gated`."""
    definitely_not = format_timeout_report(30.0, hard=False, captured=False)
    unknown = format_timeout_report(30.0, hard=False, captured=None)
    did = format_timeout_report(30.0, hard=False, captured=True)

    assert "Nothing was captured" in definitely_not
    assert "UNKNOWN" in unknown and "Nothing was captured" not in unknown
    assert "stands" in did and "Nothing was captured" not in did


def test_the_hard_report_admits_the_bash_child_is_ORPHANED_not_reaped() -> None:
    """codex L3, HIGH — a false claim in a report an operator reads.

    `os._exit` ends only this process; the SIGTERM-then-SIGKILL of the shell's process GROUP lives in
    `SandboxedShell.close` and is never reached. So a bash child mid-command is reparented to init and
    LEFT RUNNING — possibly while launchd starts the next seat turn. The original wording said the
    subprocess "is left to the OS to reap", which is a comfortable assumption dressed as a fact.

    This project treats a claim the implementation does not enforce as a defect in itself, so the
    honest wording is pinned."""
    hard = format_timeout_report(30.0, hard=True, captured=None)

    assert "ORPHANED" in hard
    assert "NOT reaped" in hard
    assert "reap" not in hard.replace("NOT reaped", ""), "no residual 'the OS reaps it' claim"


def test_an_INFINITE_bound_assigned_after_construction_still_arms_nothing() -> None:
    """`seconds` is a PUBLIC attribute, so the constructor guard is not the only door a non-finite
    bound can come through — a caller can assign one afterwards, which is what makes `enabled`'s own
    `isfinite` check reachable rather than dead defence-in-depth.

    ⚠ IT HAS TO BE `inf`, AND THE FIRST VERSION OF THIS TEST USED `nan` AND PROVED NOTHING. `nan > 0`
    is already False, so the plain `> 0` comparison disarms a nan by itself and a mutation deleting
    `isfinite` SURVIVED. `inf > 0` is True, so without `isfinite` an infinite bound reports itself
    ENABLED, `__enter__` arms, and `setitimer(inf)` raises OverflowError — layer 1 silently unarmed on
    a deadline that claims to be on.

    The mutation harness caught the weak TEST, not weak code. `the_instrument_needs_its_own_oracle`
    applies to the test suite as much as to the product."""
    d = TurnDeadline(5.0)
    assert d.enabled is True
    d.seconds = float("inf")
    assert d.enabled is False, "an infinite bound must never report itself as enabled"
    # And the disarmed path must be a real no-op, not a half-armed one.
    with d:
        pass
    assert d._armed_signal is False and d._watchdog is None


def test_the_session_close_COMPLETES_when_the_bound_fires_during_teardown() -> None:
    """codex L3, HIGH — the hole the BaseException fix itself opened, now pinned.

    The bound stays armed through teardown deliberately (launchd coalesces on PROCESS lifetime). But
    `TurnTimeout` is a `BaseException` so the SDK cannot swallow it — which means an alarm landing
    inside `conversation.close()` sails through an `except Exception`, aborting teardown AFTER
    `_closed` was already set, so nothing ever retries it, and the OS-sandboxed shell LEAKS.

    Added because a mutation narrowing `close()` back to `except Exception` SURVIVED the suite: the
    fix was right and completely uncovered."""
    from levain.session import EntitySession

    class _ExplodingOnClose:
        def __init__(self):
            self.state = _State()
            self.close_attempts = 0

        def close(self):
            self.close_attempts += 1
            raise TurnTimeout(1.0)

    conv = _ExplodingOnClose()
    sess = EntitySession(
        entity_dir=None, binding=None, conversation=conv, workspace=None,
        model_label="m", with_tools=False, bash_ok=False, gate_mode="ungated",
    )

    sess.close()  # must NOT raise — that is the whole contract

    assert conv.close_attempts == 1
    assert sess.closed is True


# ======================================================================================
# SEQUENTIAL deadlines in one process — new with K4a [6], flagged by codex at L3 as an
# assumption it could not verify. A seat now bounds its TURN and then, separately, its
# CONSOLIDATE, so two TurnDeadline instances live in one process back to back.
# ======================================================================================

def test_two_sequential_deadlines_each_restore_the_handler_they_found():
    """The second deadline must not inherit or corrupt the first's signal state."""
    import signal

    from levain.firing.deadline import TurnDeadline

    original = signal.getsignal(signal.SIGALRM)
    with TurnDeadline(60):
        first_installed = signal.getsignal(signal.SIGALRM)
        assert first_installed is not original
    assert signal.getsignal(signal.SIGALRM) is original, "first deadline did not restore"

    with TurnDeadline(60):
        second_installed = signal.getsignal(signal.SIGALRM)
        assert second_installed is not original
        # Each instance installs ITS OWN bound handler — the second must not still be routing
        # into the first instance's state, whose `_active` is now False and whose `seconds`
        # would be the wrong number to report.
        assert second_installed is not first_installed
    assert signal.getsignal(signal.SIGALRM) is original, "second deadline did not restore"


def test_a_stale_alarm_from_a_FINISHED_deadline_is_dropped_not_raised():
    """A SIGALRM delivered after its own deadline exited must be recognised as spurious.

    THE CROSS-DEADLINE CASE THIS PROTECTS: `setitimer(0)` cancels a pending TIMER, but a signal
    already delivered is still queued, and CPython runs it at the next bytecode check — by which
    time a SECOND deadline may be the one installed. If the finished instance's handler could still
    fire, a stale alarm from the TURN would surface as a spurious CONSOLIDATE timeout, reporting a
    stall that never happened.
    """
    from levain.firing.deadline import TurnDeadline

    finished = TurnDeadline(60)
    with finished:
        pass
    # Deliver the stale alarm by hand — `_active` is False, so it must be dropped silently.
    finished._on_alarm(14, None)  # noqa: SLF001 — invoking the handler IS the test


def test_a_stale_handler_cannot_report_a_second_deadlines_bound():
    """Belt-and-braces on the above: even mid-second-deadline, the FIRST instance stays inert."""
    from levain.firing.deadline import TurnDeadline

    first = TurnDeadline(11)
    with first:
        pass
    with TurnDeadline(22):
        first._on_alarm(14, None)  # noqa: SLF001 — must not raise 11s into the 22s region
