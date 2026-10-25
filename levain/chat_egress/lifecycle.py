"""C0, read per request (CH-D2 plan ruling P-2): the decider's state machine for ONE egress request.

srt (sandbox-runtime 0.0.81) owns the connection, so its per-connection states are not Levain's. Levain owns this
per-request machine. S5b's decider drives it from its threads: the decision thread, the card answer, the
observation-channel drain. S5a-2's records write the rows it asks for, and S7's card reads its states. Design
§3.8.5(o) R-10 and *At close*, as amended through R15-30. Row classification:
`project_memory/1024-20/chd2_s5a/ROWS.md`.

The rule it holds: a request has its `egress_attempt` row plus AT MOST ONE of {`egress_outcome`,
`egress_ended_before_outcome`} (R15-26-4, R15-28-4). One transition decides which (R15-26-3: the race between the
two is never a glue bug).
  * The attempt row is asked for once, at the transition that settles the decision. On an allow it comes BEFORE the
    allow verdict is sent (R15-10-A8).
  * An ended row is asked for exactly when the glue's abort report (`wire.ObsEnded`) comes (a) while the decision is
    pending, which means until it resolves: deciding, awaiting the body, or a card held (R15-30-5), or (b) after the
    wrapper returned allow and before `onResponse` (R15-29-1, R15-30-4). That includes srt's local 403 / 502 after
    an allow, because the row names no cause.
  * Never after a deny (the deny-vs-abort race is absorbed), never after an outcome (srt aborts the signal at every
    normal close too: R15-27-3), and never for a glue wall, which never reaches the decider.
  * A card whose client left is ENDED at that report, and an allow that arrives later forwards nothing (C9, R15-12-B7,
    R15-14-5). A card's hold expiry is a DENY.

AMBIGUOUS (R15-10-A8) is not a state of this machine. It is what an ALLOWED attempt with neither later row reads as:
the service died, or the launch closed before any report. `settle` reads the rows and gives it.

Lifecycle owns no thread, task or socket. `RequestLifecycle.fire` serialises the transitions of one request under a
lock, and it can run the composer's effects under that same lock. That way the attempt row commits before an
ended row that races it, and a card answer and an abort report cannot both act.

A transition commits only when every one of its effects succeeded (L3 r1 F1). If an effect raises, the request moves
to FAILED instead of the successor: a terminal state that absorbs every later event with no effect, so no row is
ever asked for on top of a failed one (no ended or outcome row without its attempt, no outcome lost behind a state
that already reads OUTCOME). The exception still propagates. Unless `failure.done` holds a verdict frame or the
RELEASE, the caller then owes srt an answer itself. The rows read FAILED through `settle(..., failed=True)`, never
AMBIGUOUS and never a clean outcome. A RELEASE in a failed step is still performed (F2).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

#: A card's hold (R15-10-A3). It is under Claude Code Bash's 120 s default tool timeout, and the card says the tool
#: call ends at its timeout. Node's server timeouts (the glue, S4) and the decider's timeout (S5b) must exceed it.
EGRESS_CARD_HOLD_S = 100


class State(StrEnum):
    DECIDING = "deciding"            # the decider holds srt's request frame and has not answered
    AWAITING_BODY = "awaiting-body"  # a need-body frame was sent; srt's body frame has not come
    CARD_HELD = "card-held"          # an operator card is up
    ALLOWED = "allowed"              # the allow verdict was sent; `onResponse` has not fired
    DENIED = "denied"                # a deny verdict was sent: owed no outcome
    OUTCOME = "outcome"              # `onResponse` fired: the outcome row
    ENDED = "ended"                  # the abort report came first: the ended-before-outcome row
    CLOSED = "closed"                # the launch / service closed the request with no report
    FAILED = "failed"                # an effect raised: the transition did not commit (`RequestLifecycle.failure`)


class Event(StrEnum):
    NEED_BODY = "need-body"  # the decider asks srt for the body
    BODY = "body"            # srt's body frame arrived
    CARD = "card"            # the decider raised an operator card
    ALLOW = "allow"          # an allow: the decider's own, or his on the card
    DENY = "deny"            # a deny: policy, a cap, body_timeout, the card's hold expiry, or his on the card
    RESPONSE = "response"    # `wire.ObsResponse` for this request
    ENDED = "ended"          # `wire.ObsEnded` for this request (the glue's abort report)
    CLOSE = "close"          # launch end / service stop reached the request (fire it after the obs channel drained)


class Effect(StrEnum):
    """What the composer must do, in the order given."""
    CARD_ENDED = "card-ended"        # mark the request's card ENDED (it can no longer be answered)
    WRITE_ATTEMPT = "write-attempt"  # the `egress_attempt` row, with `Step.verdict`
    SEND_ALLOW = "send-allow"        # the allow verdict frame to srt
    SEND_DENY = "send-deny"          # the deny verdict frame to srt
    WRITE_OUTCOME = "write-outcome"  # the `egress_outcome` row
    WRITE_ENDED = "write-ended"      # the `egress_ended_before_outcome` row
    RELEASE = "release"              # finish srt's still-pending decision for a request whose client left


class Verdict(StrEnum):
    """The attempt row's verdict. UNDECIDED: the request ended or closed before any verdict."""
    ALLOW = "allow"
    DENY = "deny"
    UNDECIDED = "undecided"


class Settled(StrEnum):
    """What a request's rows read as (records, the cockpit card)."""
    DENIED = "denied"            # attempt (deny), nothing else
    COMPLETED = "completed"      # attempt (allow) + outcome
    ENDED = "ended"              # attempt (allow or undecided) + ended-before-outcome
    AMBIGUOUS = "ambiguous"      # attempt (allow), neither: the outcome is unknown (R15-10-A8)
    UNDECIDED = "undecided"      # attempt (undecided), neither: closed before a verdict; nothing was forwarded
    FAILED = "failed"            # an effect failed: the rows are a prefix of what C0 asked for (maybe no attempt)
    INCONSISTENT = "inconsistent"  # a combination C0 never asks for (a bug or a tampered store)


PENDING = frozenset({State.DECIDING, State.AWAITING_BODY, State.CARD_HELD})
TERMINAL = frozenset({State.DENIED, State.OUTCOME, State.ENDED, State.CLOSED, State.FAILED})


@dataclass(frozen=True, slots=True)
class Step:
    """One transition's result. `verdict` is set exactly when WRITE_ATTEMPT is in `effects`. `stray` marks an event
    that cannot occur in that state under the protocol (a bug upstream). It is diagnostic only: a stray step changes
    nothing and asks for nothing."""
    state: State
    effects: tuple[Effect, ...] = ()
    verdict: Verdict | None = None
    stray: bool = False


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a request is FAILED. `step` is the transition that did not commit, `effect` the first of its effects that
    raised, `done` the effects that completed, in the order performed. A RELEASE that ran after the failure is in
    `done`; one that raised too is not."""
    step: Step
    effect: Effect
    done: tuple[Effect, ...]


_ATTEMPT_ALLOW = (Effect.WRITE_ATTEMPT, Effect.SEND_ALLOW)
_ATTEMPT_DENY = (Effect.WRITE_ATTEMPT, Effect.SEND_DENY)
_ENDED_PENDING = (Effect.WRITE_ATTEMPT, Effect.WRITE_ENDED, Effect.RELEASE)

_ALLOW = Step(State.ALLOWED, _ATTEMPT_ALLOW, Verdict.ALLOW)
_DENY = Step(State.DENIED, _ATTEMPT_DENY, Verdict.DENY)

# The live states' transitions. A pair not listed for a live state is stray. A terminal state absorbs every event
# with no effect, except DENIED + RESPONSE, which is stray.
_LIVE: dict[State, dict[Event, Step]] = {
    State.DECIDING: {
        Event.NEED_BODY: Step(State.AWAITING_BODY),
        Event.CARD: Step(State.CARD_HELD),
        Event.ALLOW: _ALLOW,
        Event.DENY: _DENY,
        Event.ENDED: Step(State.ENDED, _ENDED_PENDING, Verdict.UNDECIDED),
        Event.CLOSE: Step(State.CLOSED, (Effect.WRITE_ATTEMPT,), Verdict.UNDECIDED),
    },
    State.AWAITING_BODY: {
        Event.BODY: Step(State.DECIDING),
        Event.DENY: _DENY,
        Event.ENDED: Step(State.ENDED, _ENDED_PENDING, Verdict.UNDECIDED),
        Event.CLOSE: Step(State.CLOSED, (Effect.WRITE_ATTEMPT,), Verdict.UNDECIDED),
    },
    State.CARD_HELD: {
        Event.ALLOW: _ALLOW,
        Event.DENY: _DENY,
        Event.ENDED: Step(State.ENDED, (Effect.CARD_ENDED,) + _ENDED_PENDING, Verdict.UNDECIDED),
        Event.CLOSE: Step(State.CLOSED, (Effect.CARD_ENDED, Effect.WRITE_ATTEMPT), Verdict.UNDECIDED),
    },
    State.ALLOWED: {
        Event.RESPONSE: Step(State.OUTCOME, (Effect.WRITE_OUTCOME,)),
        Event.ENDED: Step(State.ENDED, (Effect.WRITE_ENDED,)),
        Event.CLOSE: Step(State.CLOSED),
    },
}
_STRAY_IN_TERMINAL = frozenset({(State.DENIED, Event.RESPONSE)})


def step(state: State, event: Event) -> Step:
    """C0's one transition function. It is pure. It raises ValueError for anything that is not a `State` / `Event`."""
    if type(state) is not State or type(event) is not Event:
        raise ValueError(f"not a lifecycle state/event: {state!r}, {event!r}")
    if state in TERMINAL:
        return Step(state, stray=(state, event) in _STRAY_IN_TERMINAL)
    got = _LIVE[state].get(event)
    return got if got is not None else Step(state, stray=True)


def settle(verdict: Verdict | None, has_outcome: bool, has_ended: bool, *, failed: bool = False) -> Settled:
    """What a request's rows read as. Read side: records and the card call this on stored rows, never on the
    machine. `verdict` is the attempt row's, or None when there is no attempt row. `failed` says the request reached
    FAILED (an effect raised); it reads FAILED unless the rows are a combination no failure can leave, which stays
    INCONSISTENT. A request with no attempt row reads FAILED only when it failed and has no other row."""
    if verdict is not None and type(verdict) is not Verdict:
        raise ValueError(f"not a lifecycle verdict: {verdict!r}")
    if has_outcome and has_ended:
        return Settled.INCONSISTENT
    if verdict is None:
        # the attempt write itself failed (FAILED absorbs everything after it), or rows without their attempt
        return Settled.FAILED if failed and not (has_outcome or has_ended) else Settled.INCONSISTENT
    if verdict is Verdict.DENY:
        if has_outcome or has_ended:
            return Settled.INCONSISTENT
        return Settled.FAILED if failed else Settled.DENIED
    if verdict is Verdict.UNDECIDED and has_outcome:
        return Settled.INCONSISTENT          # nothing was forwarded, so there can be no outcome
    if failed:
        return Settled.FAILED
    if verdict is Verdict.ALLOW:
        return Settled.COMPLETED if has_outcome else Settled.ENDED if has_ended else Settled.AMBIGUOUS
    return Settled.ENDED if has_ended else Settled.UNDECIDED


class RequestLifecycle:
    """One request's machine. It starts in DECIDING, when the decider receives srt's request frame."""

    # `_transition_lock` is not reentrant, on purpose: `perform` must not fire this machine. It is also the test seam
    # the race tests replace with an instrumented lock, to see a second `fire` reach the acquire (L3 r1 F3).
    __slots__ = ("_transition_lock", "_state", "_failure")

    def __init__(self) -> None:
        self._transition_lock = threading.Lock()
        self._state = State.DECIDING
        self._failure: Failure | None = None

    @property
    def state(self) -> State:
        """The last COMMITTED state. While a transition's effects run it still reads the state before it, so no
        reader ever sees a state whose effects have not happened (F4). It does not take the lock, so a `perform`
        callback can read it without deadlocking."""
        return self._state

    @property
    def failure(self) -> Failure | None:
        """What failed, once the request is FAILED; None before. It is in place before `state` reads FAILED."""
        return self._failure

    def fire(self, event: Event, perform: Callable[[Effect, Step], None] | None = None) -> Step:
        """Apply `event` atomically under the transition lock. If `perform` is given and the step has effects, it is
        called once per effect, in the step's order, as `perform(effect, step)`, and must not fire this machine again
        (the lock is not reentrant). The successor state commits only after every call returned.

        If a call raises, the effects after it are skipped, except a RELEASE, which is still performed (F2). The
        request then commits to FAILED, `failure` records what happened, and the first exception propagates (a
        RELEASE that raised as well is added to it as a note). Unless `failure.done` holds SEND_ALLOW, SEND_DENY or
        RELEASE, the caller owes srt an answer on its own. Without
        `perform` the step commits at once and the caller runs no effects."""
        with self._transition_lock:
            result = step(self._state, event)
            if perform is None or not result.effects:
                self._state = result.state
                return result
            done: list[Effect] = []
            first: tuple[BaseException, Effect] | None = None
            for effect in result.effects:
                if first is not None and effect is not Effect.RELEASE:
                    continue
                try:
                    perform(effect, result)
                except BaseException as exc:  # noqa: BLE001 (any raise fails the request; it is re-raised below)
                    if first is None:
                        first = (exc, effect)
                    else:
                        first[0].add_note(f"{effect} also raised: {exc!r}")
                else:
                    done.append(effect)
            if first is not None:
                self._failure = Failure(result, first[1], tuple(done))
                self._state = State.FAILED
                raise first[0]
            self._state = result.state
            return result
