"""CH-D2 S5a-1: C0, the per-request machine (`levain.chat_egress.lifecycle`).

Rows (classification: `project_memory/1024-20/chd2_s5a/ROWS.md`): C0 (one red per (state, event) pair; read per
request, plan ruling P-2), C9 / R15-12-B7 / R15-14-5 (a client that leaves mid-hold: the card ENDED at the close, his
later allow forwards nothing), R15-10-A3 (the 100 s hold), R15-10-A8 (attempt before the allow verdict; AMBIGUOUS is an
allowed attempt with neither), R15-26-3 / R15-26-4 / R15-28-4 (outcome XOR ended-before-outcome, at most one; the race
is one transition), R15-27-3 (no ended row after an outcome), R15-29-1 with R15-30-4 / R15-30-5 (exactly when the
ended row is written).

The expected table below is written out pair by pair. It does not import or derive anything from the module's own
table.
"""

from __future__ import annotations

import ast
import itertools
import threading
from pathlib import Path

import pytest

from levain.chat_egress import lifecycle as lc
from levain.chat_egress.lifecycle import Effect as F
from levain.chat_egress.lifecycle import Event as E
from levain.chat_egress.lifecycle import Settled
from levain.chat_egress.lifecycle import State as S
from levain.chat_egress.lifecycle import Failure, Step, Verdict as V

A_ALLOW = (F.WRITE_ATTEMPT, F.SEND_ALLOW)
A_DENY = (F.WRITE_ATTEMPT, F.SEND_DENY)
ENDED_PENDING = (F.WRITE_ATTEMPT, F.WRITE_ENDED, F.RELEASE)


def _stray(s):
    return Step(s, (), None, True)


def _absorb(s):
    return Step(s, (), None, False)


# (state, event) -> the Step it must give. 9 states x 8 events, every pair listed.
EXPECTED: dict[tuple[S, E], Step] = {
    # DECIDING: the decider holds the frame and has not answered.
    (S.DECIDING, E.NEED_BODY): Step(S.AWAITING_BODY),
    (S.DECIDING, E.BODY): _stray(S.DECIDING),
    (S.DECIDING, E.CARD): Step(S.CARD_HELD),
    (S.DECIDING, E.ALLOW): Step(S.ALLOWED, A_ALLOW, V.ALLOW),
    (S.DECIDING, E.DENY): Step(S.DENIED, A_DENY, V.DENY),
    (S.DECIDING, E.RESPONSE): _stray(S.DECIDING),
    (S.DECIDING, E.ENDED): Step(S.ENDED, ENDED_PENDING, V.UNDECIDED),        # R15-30-5
    (S.DECIDING, E.CLOSE): Step(S.CLOSED, (F.WRITE_ATTEMPT,), V.UNDECIDED),
    # AWAITING_BODY: a need-body frame was sent; srt's body frame has not come.
    (S.AWAITING_BODY, E.NEED_BODY): _stray(S.AWAITING_BODY),
    (S.AWAITING_BODY, E.BODY): Step(S.DECIDING),
    (S.AWAITING_BODY, E.CARD): _stray(S.AWAITING_BODY),
    (S.AWAITING_BODY, E.ALLOW): _stray(S.AWAITING_BODY),
    (S.AWAITING_BODY, E.DENY): Step(S.DENIED, A_DENY, V.DENY),               # body_timeout, a cap
    (S.AWAITING_BODY, E.RESPONSE): _stray(S.AWAITING_BODY),
    (S.AWAITING_BODY, E.ENDED): Step(S.ENDED, ENDED_PENDING, V.UNDECIDED),   # R15-30-5
    (S.AWAITING_BODY, E.CLOSE): Step(S.CLOSED, (F.WRITE_ATTEMPT,), V.UNDECIDED),
    # CARD_HELD: an operator card is up; the decision waits on him (or the hold's expiry, a DENY).
    (S.CARD_HELD, E.NEED_BODY): _stray(S.CARD_HELD),
    (S.CARD_HELD, E.BODY): _stray(S.CARD_HELD),
    (S.CARD_HELD, E.CARD): _stray(S.CARD_HELD),
    (S.CARD_HELD, E.ALLOW): Step(S.ALLOWED, A_ALLOW, V.ALLOW),
    (S.CARD_HELD, E.DENY): Step(S.DENIED, A_DENY, V.DENY),
    (S.CARD_HELD, E.RESPONSE): _stray(S.CARD_HELD),
    (S.CARD_HELD, E.ENDED): Step(S.ENDED, (F.CARD_ENDED,) + ENDED_PENDING, V.UNDECIDED),  # C9, B7, R15-29-1 (a)
    (S.CARD_HELD, E.CLOSE): Step(S.CLOSED, (F.CARD_ENDED, F.WRITE_ATTEMPT), V.UNDECIDED),
    # ALLOWED: the allow was sent; `onResponse` has not fired.
    (S.ALLOWED, E.NEED_BODY): _stray(S.ALLOWED),
    (S.ALLOWED, E.BODY): _stray(S.ALLOWED),
    (S.ALLOWED, E.CARD): _stray(S.ALLOWED),
    (S.ALLOWED, E.ALLOW): _stray(S.ALLOWED),
    (S.ALLOWED, E.DENY): _stray(S.ALLOWED),
    (S.ALLOWED, E.RESPONSE): Step(S.OUTCOME, (F.WRITE_OUTCOME,)),
    (S.ALLOWED, E.ENDED): Step(S.ENDED, (F.WRITE_ENDED,)),                   # R15-29-1 (b), R15-30-4
    (S.ALLOWED, E.CLOSE): Step(S.CLOSED),                                    # settles AMBIGUOUS from the rows
    # DENIED: owed no outcome. An ended report here is the deny-vs-abort race: absorbed (R15-29-1: none after a deny).
    (S.DENIED, E.NEED_BODY): _absorb(S.DENIED),
    (S.DENIED, E.BODY): _absorb(S.DENIED),
    (S.DENIED, E.CARD): _absorb(S.DENIED),
    (S.DENIED, E.ALLOW): _absorb(S.DENIED),
    (S.DENIED, E.DENY): _absorb(S.DENIED),
    (S.DENIED, E.RESPONSE): _stray(S.DENIED),                                # srt never fires onResponse on a deny
    (S.DENIED, E.ENDED): _absorb(S.DENIED),
    (S.DENIED, E.CLOSE): _absorb(S.DENIED),
}
for _ev in E:
    EXPECTED[(S.OUTCOME, _ev)] = _absorb(S.OUTCOME)    # R15-27-3 / R15-28-4: no ended row after an outcome
    EXPECTED[(S.ENDED, _ev)] = _absorb(S.ENDED)        # C9: an allow on an ENDED card forwards nothing
    EXPECTED[(S.CLOSED, _ev)] = _absorb(S.CLOSED)
    EXPECTED[(S.FAILED, _ev)] = _absorb(S.FAILED)      # L3 r1 F1: a failed effect absorbs everything after it


def test_the_expected_table_is_whole():
    assert set(EXPECTED) == set(itertools.product(S, E))
    assert len(EXPECTED) == 72


# --- C0: one red per (state, event) pair ------------------------------------------------------------------------

@pytest.mark.parametrize(("state", "event"), sorted(EXPECTED, key=lambda k: (list(S).index(k[0]), list(E).index(k[1]))),
                         ids=lambda v: str(v))
def test_c0_pair(state, event):
    assert lc.step(state, event) == EXPECTED[(state, event)]


def test_c0_step_refuses_a_non_member():
    with pytest.raises(ValueError):
        lc.step("deciding", E.ALLOW)          # type: ignore[arg-type]  # a str with the same value is not a State
    with pytest.raises(ValueError):
        lc.step(S.DECIDING, "allow")          # type: ignore[arg-type]


def test_pending_and_terminal_partition_the_states():
    assert lc.PENDING == {S.DECIDING, S.AWAITING_BODY, S.CARD_HELD}
    assert lc.TERMINAL == {S.DENIED, S.OUTCOME, S.ENDED, S.CLOSED, S.FAILED}
    assert lc.PENDING | lc.TERMINAL | {S.ALLOWED} == set(S)


def test_a_terminal_state_never_leaves():
    assert len(lc.TERMINAL) == 5
    for st, ev in itertools.product(lc.TERMINAL, E):
        r = lc.step(st, ev)
        assert r.state is st and r.effects == () and r.verdict is None


def test_verdict_is_set_exactly_when_an_attempt_is_written():
    for st, ev in itertools.product(S, E):
        r = lc.step(st, ev)
        assert (r.verdict is not None) == (F.WRITE_ATTEMPT in r.effects), (st, ev)


def test_a_stray_step_has_no_effect_and_keeps_its_state():
    for st, ev in itertools.product(S, E):
        r = lc.step(st, ev)
        if r.stray:
            assert r.state is st and r.effects == ()


# --- R15-10-A8: the attempt is committed before the allow verdict is sent ----------------------------------------

@pytest.mark.parametrize("st", [S.DECIDING, S.CARD_HELD])
def test_r15_10_a8_attempt_precedes_the_allow(st):
    eff = lc.step(st, E.ALLOW).effects
    assert eff.index(F.WRITE_ATTEMPT) < eff.index(F.SEND_ALLOW)


def test_the_attempt_precedes_every_verdict_and_every_other_row():
    for st, ev in itertools.product(S, E):
        eff = lc.step(st, ev).effects
        if F.WRITE_ATTEMPT in eff:
            i = eff.index(F.WRITE_ATTEMPT)
            for later in (F.SEND_ALLOW, F.SEND_DENY, F.WRITE_ENDED, F.RELEASE):
                if later in eff:
                    assert i < eff.index(later), (st, ev)


def test_c9_the_card_ends_before_anything_else_at_the_close():
    for ev in (E.ENDED, E.CLOSE):
        eff = lc.step(S.CARD_HELD, ev).effects
        assert eff[0] is F.CARD_ENDED


# --- C9 / R15-12-B7 / R15-14-5: the client leaves mid-hold, then his allow ---------------------------------------

def _run(events):
    m = lc.RequestLifecycle()
    out = [m.fire(e) for e in events]
    return m, out


def _all_effects(steps):
    return [f for s in steps for f in s.effects]


def test_c9_b7_client_leaves_mid_hold_then_his_allow_forwards_nothing():
    m, steps = _run([E.CARD, E.ENDED, E.ALLOW])
    eff = _all_effects(steps)
    assert F.CARD_ENDED in eff                       # ENDED at the close, not at the hold's expiry
    assert F.SEND_ALLOW not in eff                   # his allow forwards nothing
    assert steps[-1] == Step(S.ENDED)                # absorbed, not a glue bug
    assert eff.count(F.WRITE_ATTEMPT) == 1 and eff.count(F.WRITE_ENDED) == 1
    assert steps[1].verdict is V.UNDECIDED
    assert m.state is S.ENDED


def test_c9_a_retry_raises_a_second_card_and_the_first_still_forwards_nothing():
    first, second = lc.RequestLifecycle(), lc.RequestLifecycle()
    first.fire(E.CARD)
    second.fire(E.CARD)                              # the client's retry, while the first is still up
    first.fire(E.ENDED)                              # the first client timed out
    assert first.fire(E.ALLOW).effects == ()
    assert second.fire(E.ALLOW).effects == A_ALLOW   # the retry is its own request
    assert first.state is S.ENDED and second.state is S.ALLOWED


def test_c9_hold_expiry_is_a_deny_and_writes_no_ended_row():
    _, steps = _run([E.CARD, E.DENY, E.ENDED])       # expiry denies; the client then leaves
    eff = _all_effects(steps)
    assert eff == list(A_DENY)
    assert steps[0].verdict is None and steps[1].verdict is V.DENY


# --- R15-10-A3: the hold -----------------------------------------------------------------------------------------

def test_r15_10_a3_hold_is_100s_under_claude_codes_120s_bash_default():
    assert lc.EGRESS_CARD_HOLD_S == 100
    assert lc.EGRESS_CARD_HOLD_S < 120


# --- R15-29-1 with R15-30-4 / R15-30-5: exactly when the ended row is written ------------------------------------

ROW_CASES = {
    # the ruling's reds, as the decider sees them
    "denied GET -> attempt only": ([E.DENY], V.DENY, False, False, Settled.DENIED),
    "held card, client leaves -> attempt + ended": ([E.CARD, E.ENDED], V.UNDECIDED, False, True, Settled.ENDED),
    "allowed, srt 403/502 locally after the wrapper's allow -> attempt + ended (R15-30-4)":
        ([E.ALLOW, E.ENDED], V.ALLOW, False, True, Settled.ENDED),
    "normal 200 -> attempt + outcome, no ended row (R15-27-3)":
        ([E.ALLOW, E.RESPONSE, E.ENDED], V.ALLOW, True, False, Settled.COMPLETED),
    # fold readings
    "carded non-read denied, then the client leaves -> attempt only":
        ([E.CARD, E.DENY, E.ENDED], V.DENY, False, False, Settled.DENIED),
    "client leaves while the decider is still deciding -> ended (R15-30-5)":
        ([E.ENDED], V.UNDECIDED, False, True, Settled.ENDED),
    "client leaves while the body is awaited -> ended (R15-30-5)":
        ([E.NEED_BODY, E.ENDED], V.UNDECIDED, False, True, Settled.ENDED),
    "body read, then allowed, then 200": ([E.NEED_BODY, E.BODY, E.ALLOW, E.RESPONSE], V.ALLOW, True, False,
                                          Settled.COMPLETED),
    "allowed, launch ends before any report -> AMBIGUOUS (R15-10-A8)":
        ([E.ALLOW, E.CLOSE], V.ALLOW, False, False, Settled.AMBIGUOUS),
    "held card at launch end -> card ended, attempt undecided":
        ([E.CARD, E.CLOSE], V.UNDECIDED, False, False, Settled.UNDECIDED),
}


@pytest.mark.parametrize("case", list(ROW_CASES), ids=list(ROW_CASES))
def test_r15_29_1_rows_per_request(case):
    events, verdict, outcome, ended, settled = ROW_CASES[case]
    _, steps = _run(events)
    eff = _all_effects(steps)
    assert eff.count(F.WRITE_ATTEMPT) == 1
    assert [s.verdict for s in steps if s.verdict is not None] == [verdict]
    assert eff.count(F.WRITE_OUTCOME) == int(outcome)
    assert eff.count(F.WRITE_ENDED) == int(ended)
    assert lc.settle(verdict, outcome, ended) is settled


def test_r15_29_1_never_an_ended_row_after_a_deny():
    for pre in ([], [E.CARD], [E.NEED_BODY]):
        _, steps = _run(pre + [E.DENY, E.ENDED, E.CLOSE])
        assert F.WRITE_ENDED not in _all_effects(steps)


# --- R15-26-4 / R15-28-4: up to two rows per request, over every sequence ----------------------------------------

def test_r15_26_4_at_most_two_rows_over_every_sequence_up_to_length_5():
    n = 0
    for k in range(1, 6):
        for seq in itertools.product(list(E), repeat=k):
            st, attempts, terminal_rows = S.DECIDING, 0, 0
            verdict = None
            for ev in seq:
                r = lc.step(st, ev)
                st = r.state
                attempts += r.effects.count(F.WRITE_ATTEMPT)
                terminal_rows += r.effects.count(F.WRITE_OUTCOME) + r.effects.count(F.WRITE_ENDED)
                if r.verdict is not None:
                    verdict = r.verdict
                # a row of either outcome kind is only ever written once an attempt exists
                if terminal_rows:
                    assert attempts == 1, seq
            assert attempts <= 1 and terminal_rows <= 1, seq
            assert (verdict is None) == (attempts == 0), seq
            n += 1
    assert n == sum(8 ** k for k in range(1, 6))


def test_a_verdict_is_sent_at_most_once_over_every_sequence_up_to_length_5():
    for k in range(1, 6):
        for seq in itertools.product(list(E), repeat=k):
            st, sent = S.DECIDING, 0
            for ev in seq:
                r = lc.step(st, ev)
                st = r.state
                sent += sum(r.effects.count(f) for f in (F.SEND_ALLOW, F.SEND_DENY, F.RELEASE))
            assert sent <= 1, seq


# --- R15-26-3: the outcome-vs-ended race is ONE transition --------------------------------------------------------

@pytest.mark.parametrize("first", [E.RESPONSE, E.ENDED])
def test_r15_26_3_the_second_report_is_absorbed_not_stray(first):
    second = E.ENDED if first is E.RESPONSE else E.RESPONSE
    _, steps = _run([E.ALLOW, first, second])
    assert steps[2].effects == () and steps[2].stray is False
    rows = _all_effects(steps)
    assert rows.count(F.WRITE_OUTCOME) + rows.count(F.WRITE_ENDED) == 1


class _SeamLock:
    """The transition-lock seam (L3 r1 F3; S5a-1 L3 r2 F3): it wraps the machine's real lock and says, per thread,
    when that thread found the lock CONTENDED. "reached" is set only after a non-blocking acquire failed, so a thread
    that signalled is about to block in the acquire on a lock another transition holds; a thread that got the lock
    at once never signals. With the lock removed (nothing contended), no thread signals and the race tests fail.
    No sleep decides anything."""

    def __init__(self, real):
        self.real = real
        self.reached: dict[str, threading.Event] = {}

    def reached_by(self, name):
        return self.reached.setdefault(name, threading.Event())

    def __enter__(self):
        if not self.real.acquire(blocking=False):
            self.reached_by(threading.current_thread().name).set()
            self.real.acquire()
        return self

    def __exit__(self, *exc):
        self.real.release()
        return False


def _held_race(pre, first, second):
    """Fire `first` on one thread with its first effect held open, then `second` on another. It returns (rows in the
    order they were performed, whether `second` finished while `first` was still held). Deterministic: the test waits
    until the second thread has reached the transition lock, then checks it has not got past it."""
    m = lc.RequestLifecycle()
    for e in pre:
        m.fire(e)
    seam = _SeamLock(m._transition_lock)
    m._transition_lock = seam
    rows: list[F] = []
    entered, release = threading.Event(), threading.Event()

    def held(effect, s):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        rows.append(effect)

    t1 = threading.Thread(target=lambda: m.fire(first, perform=held), name="first")
    t1.start()
    assert entered.wait(5)
    done = threading.Event()

    def second_fire():
        m.fire(second, perform=lambda effect, s: rows.append(effect))
        done.set()

    t2 = threading.Thread(target=second_fire, name="second")
    t2.start()
    reached = seam.reached_by("second").wait(5)      # the second fire is at the acquire...
    finished_early = done.is_set() or not reached    # ...and, with the lock held by the first, has not got past it
    release.set()
    t1.join(5)
    t2.join(5)
    assert not t1.is_alive() and not t2.is_alive()
    assert reached, "the second fire never reached the transition lock"
    return rows, finished_early


@pytest.mark.parametrize(("first", "second"), [(E.RESPONSE, E.ENDED), (E.ENDED, E.RESPONSE)], ids=str)
def test_r15_26_3_race_after_allow_writes_exactly_one_row(first, second):
    """The obs drain's RESPONSE and the abort report race on one allowed request: whichever transition goes first
    writes its row, and the other writes nothing (one transition, not a glue bug)."""
    rows, early = _held_race([E.ALLOW], first, second)
    assert rows == [F.WRITE_OUTCOME if first is E.RESPONSE else F.WRITE_ENDED]
    assert early is False                            # the second waited on the first's transition


def test_r15_10_a8_an_ended_row_racing_the_allow_waits_for_the_attempt_commit():
    """His allow is mid-commit (attempt row, then the allow frame) when the client's abort report comes. The ended row
    is performed AFTER the attempt and the allow, never interleaved before the attempt commits."""
    rows, early = _held_race([E.CARD], E.ALLOW, E.ENDED)
    assert rows == [F.WRITE_ATTEMPT, F.SEND_ALLOW, F.WRITE_ENDED]
    assert early is False


def test_c9_an_allow_racing_the_client_leaving_forwards_nothing_when_the_abort_goes_first():
    rows, early = _held_race([E.CARD], E.ENDED, E.ALLOW)
    assert rows == [F.CARD_ENDED] + list(ENDED_PENDING)
    assert F.SEND_ALLOW not in rows
    assert early is False


def test_fire_runs_perform_under_the_machines_lock():
    m = lc.RequestLifecycle()
    held = []

    def perform(effect, s):
        held.append((effect, m._transition_lock.locked()))

    m.fire(E.ALLOW, perform=perform)
    assert held == [(F.WRITE_ATTEMPT, True), (F.SEND_ALLOW, True)]   # once per effect, in order, under the lock


def test_fire_does_not_call_perform_for_an_effectless_step():
    m = lc.RequestLifecycle()
    calls = []
    m.fire(E.CARD, perform=lambda *a: calls.append(a))
    m.fire(E.BODY, perform=lambda *a: calls.append(a))   # stray
    assert calls == []
    assert m.state is S.CARD_HELD


# --- L3 r1 F1 / F2 / F4 / F5: an effect that raises --------------------------------------------------------------

class _Boom(Exception):
    def __init__(self, effect):
        super().__init__(f"{effect} failed")
        self.effect = effect


class _Perform:
    """A composer's perform that records every effect it was asked for and raises on the ones named."""

    def __init__(self, *raise_on):
        self.raise_on = set(raise_on)
        self.ran: list[F] = []

    def __call__(self, effect, s):
        self.ran.append(effect)
        if effect in self.raise_on:
            raise _Boom(effect)


# How to reach each live state, and every (path, event) whose step has effects, with each effect position.
_PATHS = {S.DECIDING: [], S.AWAITING_BODY: [E.NEED_BODY], S.CARD_HELD: [E.CARD], S.ALLOWED: [E.ALLOW]}
_EFFECT_POSITIONS = [
    (st, ev, i)
    for st in _PATHS for ev in E
    for i in range(len(lc.step(st, ev).effects))
]


@pytest.mark.parametrize(("st", "ev", "i"), _EFFECT_POSITIONS,
                         ids=[f"{st}-{ev}-{lc.step(st, ev).effects[i]}" for st, ev, i in _EFFECT_POSITIONS])
def test_f1_a_raise_at_each_effect_position_fails_the_request(st, ev, i):
    """F1 / F5: the effect at position `i` raises. The exception propagates, the effects after it are skipped (F2: a
    RELEASE still runs), the request is FAILED rather than the successor, every later event is absorbed with no
    effect, and the rows that exist, read with the failed effect, follow the L2-7 precedence: an end row that landed
    reads as itself, an allow whose frame raised reads AMBIGUOUS, anything else FAILED."""
    m, pre = _run(_PATHS[st])
    want = lc.step(st, ev)
    bad = want.effects[i]
    p = _Perform(bad)
    with pytest.raises(_Boom) as got:
        m.fire(ev, perform=p)
    assert got.value.effect is bad
    released = F.RELEASE in want.effects and bad is not F.RELEASE
    assert p.ran == list(want.effects[:i + 1]) + ([F.RELEASE] if released else [])
    for later in E:                                  # no orphan ended / outcome row, no second verdict
        q = _Perform()
        assert m.fire(later, perform=q) == Step(S.FAILED) and q.ran == []
    assert m.state is S.FAILED
    done = want.effects[:i] + ((F.RELEASE,) if released else ())
    assert m.failure == Failure(want, bad, done)
    rows = _all_effects(pre) + list(done)
    verdict = next((s.verdict for s in pre if s.verdict), None) or (want.verdict if F.WRITE_ATTEMPT in done else None)
    outcome, ended = F.WRITE_OUTCOME in rows, F.WRITE_ENDED in rows
    reads = (Settled.COMPLETED if outcome else Settled.ENDED if ended
             else Settled.AMBIGUOUS if verdict is V.ALLOW and bad is F.SEND_ALLOW else Settled.FAILED)
    assert lc.settle(verdict, outcome, ended, failed_effect=bad) is reads


def test_f1_the_reviewers_two_cases():
    """complement 1 / codex 1, as written: a failed attempt write on an allow, then the client leaves (no ended row
    without its attempt); a failed outcome write, then the abort (reads FAILED, never AMBIGUOUS)."""
    m = lc.RequestLifecycle()
    with pytest.raises(_Boom):
        m.fire(E.ALLOW, perform=_Perform(F.WRITE_ATTEMPT))
    q = _Perform()
    m.fire(E.ENDED, perform=q)
    m.fire(E.RESPONSE, perform=q)
    assert q.ran == [] and m.state is S.FAILED
    assert lc.settle(None, False, False, failed_effect=F.WRITE_ATTEMPT) is Settled.FAILED

    m = lc.RequestLifecycle()
    m.fire(E.ALLOW, perform=_Perform())
    with pytest.raises(_Boom):
        m.fire(E.RESPONSE, perform=_Perform(F.WRITE_OUTCOME))
    q = _Perform()
    m.fire(E.ENDED, perform=q)
    assert q.ran == [] and m.state is S.FAILED
    assert lc.settle(V.ALLOW, False, False, failed_effect=F.WRITE_OUTCOME) is Settled.FAILED
    assert lc.settle(V.ALLOW, False, False) is Settled.AMBIGUOUS     # what it read before F1 (no failed mark)


@pytest.mark.parametrize("st", [S.DECIDING, S.AWAITING_BODY, S.CARD_HELD], ids=str)
def test_f2_release_runs_when_everything_before_it_raises(st):
    """F2: the client left while srt still waits on a decision. Every effect before RELEASE raises; RELEASE is still
    performed, once. When RELEASE raises too, the FIRST exception propagates and carries RELEASE's as a note."""
    m, _ = _run(_PATHS[st])
    effects = lc.step(st, E.ENDED).effects
    before = [f for f in effects if f is not F.RELEASE]
    p = _Perform(*before)
    with pytest.raises(_Boom) as got:
        m.fire(E.ENDED, perform=p)
    assert got.value.effect is before[0]
    assert p.ran == [before[0], F.RELEASE]
    assert m.failure.done == (F.RELEASE,)

    m, _ = _run(_PATHS[st])
    p = _Perform(before[0], F.RELEASE)
    with pytest.raises(_Boom) as got:
        m.fire(E.ENDED, perform=p)
    assert got.value.effect is before[0] and p.ran.count(F.RELEASE) == 1
    assert any("release" in n for n in getattr(got.value, "__notes__", []))
    assert m.failure.done == () and m.state is S.FAILED


def test_f4_only_committed_states_are_visible():
    """F4: while ALLOW's effects run, `state` still reads the state before it, from another thread and from inside
    perform itself (the getter takes no lock, so the callback does not deadlock). It reads ALLOWED only after both
    effects returned."""
    m = lc.RequestLifecycle()
    m.fire(E.CARD)
    seen_inside: list[S] = []
    entered, release = threading.Event(), threading.Event()

    def perform(effect, s):
        seen_inside.append(m.state)
        if effect is F.WRITE_ATTEMPT:
            entered.set()
            assert release.wait(5)

    t = threading.Thread(target=lambda: m.fire(E.ALLOW, perform=perform))
    t.start()
    assert entered.wait(5)
    seen_outside = m.state
    release.set()
    t.join(5)
    assert not t.is_alive()
    assert seen_outside is S.CARD_HELD
    assert seen_inside == [S.CARD_HELD, S.CARD_HELD]
    assert m.state is S.ALLOWED


# --- settle: the read side (records and the card read rows, not the machine) --------------------------------------

SETTLE = {
    (V.DENY, False, False): Settled.DENIED,
    (V.DENY, True, False): Settled.INCONSISTENT,
    (V.DENY, False, True): Settled.INCONSISTENT,
    (V.DENY, True, True): Settled.INCONSISTENT,
    (V.ALLOW, False, False): Settled.AMBIGUOUS,
    (V.ALLOW, True, False): Settled.COMPLETED,
    (V.ALLOW, False, True): Settled.ENDED,
    (V.ALLOW, True, True): Settled.INCONSISTENT,
    (V.UNDECIDED, False, False): Settled.UNDECIDED,
    (V.UNDECIDED, True, False): Settled.INCONSISTENT,
    (V.UNDECIDED, False, True): Settled.ENDED,
    (V.UNDECIDED, True, True): Settled.INCONSISTENT,
}


@pytest.mark.parametrize("key", list(SETTLE), ids=lambda k: f"{k[0]}-o{int(k[1])}-e{int(k[2])}")
def test_settle(key):
    assert lc.settle(*key) is SETTLE[key]


def test_settle_refuses_a_non_member_verdict():
    with pytest.raises(ValueError):
        lc.settle("allow", False, False)            # type: ignore[arg-type]


# settle(verdict, outcome, ended, failed_effect=) -> what the rows read as (the head's S5a-2 L2-7 ruling): the
# INCONSISTENT checks first, then a stored end row beats the mark, then SEND_ALLOW on an allow is AMBIGUOUS, then
# any other failed effect is FAILED.
SETTLE_FAILED = {
    (None, False, False, F.CARD_ENDED): Settled.FAILED,           # the card's end raised before any row
    (None, False, False, F.WRITE_ATTEMPT): Settled.FAILED,        # the attempt write itself failed
    (None, True, False, F.SEND_ALLOW): Settled.INCONSISTENT,      # rows without their attempt: no failure leaves them
    (None, False, True, F.RELEASE): Settled.INCONSISTENT,
    (V.DENY, False, False, F.SEND_DENY): Settled.FAILED,          # the deny frame failed after its attempt row
    (V.DENY, False, True, F.SEND_DENY): Settled.INCONSISTENT,
    (V.ALLOW, True, False, F.SEND_ALLOW): Settled.COMPLETED,      # the outcome proves the frame left
    (V.ALLOW, True, False, F.RELEASE): Settled.COMPLETED,
    (V.ALLOW, False, True, F.SEND_ALLOW): Settled.ENDED,          # the ended row proves the request reached srt
    (V.ALLOW, False, True, F.RELEASE): Settled.ENDED,
    (V.ALLOW, False, False, F.SEND_ALLOW): Settled.AMBIGUOUS,     # unknown whether the frame left
    (V.ALLOW, False, False, F.WRITE_OUTCOME): Settled.FAILED,     # any other failed effect, no end row
    (V.ALLOW, False, False, F.CARD_ENDED): Settled.FAILED,
    (V.ALLOW, True, True, F.SEND_ALLOW): Settled.INCONSISTENT,
    (V.UNDECIDED, False, False, F.CARD_ENDED): Settled.FAILED,
    (V.UNDECIDED, False, False, F.RELEASE): Settled.FAILED,
    (V.UNDECIDED, False, True, F.RELEASE): Settled.ENDED,         # the release raised after attempt + ended landed
    (V.UNDECIDED, False, True, F.CARD_ENDED): Settled.ENDED,
    (V.UNDECIDED, True, False, F.RELEASE): Settled.INCONSISTENT,
}


@pytest.mark.parametrize("key", list(SETTLE_FAILED), ids=lambda k: f"{k[0]}-o{int(k[1])}-e{int(k[2])}-{k[3]}")
def test_settle_with_a_failed_effect(key):
    *rows, effect = key
    assert lc.settle(*rows, failed_effect=effect) is SETTLE_FAILED[key]


def test_settle_without_a_failed_effect_is_the_clean_table():
    for key, want in SETTLE.items():                 # failed_effect=None is the table above, unchanged
        assert lc.settle(*key, failed_effect=None) is want, key
    assert lc.settle(None, False, False) is Settled.INCONSISTENT   # no attempt row and no failure: never written


def test_settle_refuses_a_failed_effect_that_is_not_an_effect():
    for bad in (True, "send-allow", 1):
        with pytest.raises(ValueError):
            lc.settle(V.ALLOW, False, False, failed_effect=bad)   # type: ignore[arg-type]
    with pytest.raises(TypeError):
        lc.settle(V.ALLOW, False, False, failed=True)            # type: ignore[call-arg]  # the old keyword is gone


def test_every_machine_end_settles_consistently():
    """Whatever sequence ran, the rows the machine asked for settle to a non-INCONSISTENT value."""
    for k in range(1, 5):
        for seq in itertools.product(list(E), repeat=k):
            st, verdict, outcome, ended = S.DECIDING, None, False, False
            for ev in seq:
                r = lc.step(st, ev)
                st = r.state
                verdict = r.verdict or verdict
                outcome |= F.WRITE_OUTCOME in r.effects
                ended |= F.WRITE_ENDED in r.effects
            if verdict is not None:
                assert lc.settle(verdict, outcome, ended) is not Settled.INCONSISTENT, seq


# --- C0's census, as far as lifecycle goes (the decider's census after a close is S5b's, P-2) ---------------------

def test_c0_lifecycle_starts_no_thread_and_no_task():
    """Lifecycle owns no thread, task, timer or socket; a close cannot leak one here. Structural, so it can fail."""
    src = Path(lc.__file__).read_text()
    tree = ast.parse(src)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"asyncio", "socket", "selectors", "subprocess", "concurrent", "multiprocessing"}
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not called & {"Thread", "Timer", "create_task", "ensure_future", "start_new_thread"}


@pytest.mark.parametrize("st", list(S), ids=str)
def test_c0_census_after_a_close_in_each_state(st):
    """Drive a machine to each state, close it: the thread set is unchanged. This passes on any pure machine, so it is
    a smoke check, not the census row; the row's census (the decider's threads and tasks) is S5b's."""
    paths = {
        S.DECIDING: [], S.AWAITING_BODY: [E.NEED_BODY], S.CARD_HELD: [E.CARD], S.ALLOWED: [E.ALLOW],
        S.DENIED: [E.DENY], S.OUTCOME: [E.ALLOW, E.RESPONSE], S.ENDED: [E.ENDED], S.CLOSED: [E.CLOSE], S.FAILED: [],
    }
    before = set(threading.enumerate())
    m, _ = _run(paths[st])
    if st is S.FAILED:
        with pytest.raises(_Boom):
            m.fire(E.ALLOW, perform=_Perform(F.WRITE_ATTEMPT))
    assert m.state is st
    m.fire(E.CLOSE)
    assert set(threading.enumerate()) == before


def test_the_package_docstring_names_lifecycle():
    import levain.chat_egress as pkg
    assert "lifecycle" in (pkg.__doc__ or "")
