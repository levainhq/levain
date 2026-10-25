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
    INCONSISTENT = "inconsistent"  # a combination C0 never asks for (a bug or a tampered store)


PENDING = frozenset({State.DECIDING, State.AWAITING_BODY, State.CARD_HELD})
TERMINAL = frozenset({State.DENIED, State.OUTCOME, State.ENDED, State.CLOSED})


@dataclass(frozen=True, slots=True)
class Step:
    """One transition's result. `verdict` is set exactly when WRITE_ATTEMPT is in `effects`. `stray` marks an event
    that cannot occur in that state under the protocol (a bug upstream). It is diagnostic only: a stray step changes
    nothing and asks for nothing."""
    state: State
    effects: tuple[Effect, ...] = ()
    verdict: Verdict | None = None
    stray: bool = False


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


def settle(verdict: Verdict, has_outcome: bool, has_ended: bool) -> Settled:
    """What a request's rows read as. Read side: records and the card call this on stored rows, never on the
    machine."""
    if type(verdict) is not Verdict:
        raise ValueError(f"not a lifecycle verdict: {verdict!r}")
    if has_outcome and has_ended:
        return Settled.INCONSISTENT
    if verdict is Verdict.DENY:
        return Settled.DENIED if not (has_outcome or has_ended) else Settled.INCONSISTENT
    if verdict is Verdict.ALLOW:
        return Settled.COMPLETED if has_outcome else Settled.ENDED if has_ended else Settled.AMBIGUOUS
    if has_outcome:
        return Settled.INCONSISTENT          # nothing was forwarded, so there can be no outcome
    return Settled.ENDED if has_ended else Settled.UNDECIDED


class RequestLifecycle:
    """One request's machine. It starts in DECIDING, when the decider receives srt's request frame."""

    __slots__ = ("_lock", "_state")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = State.DECIDING

    @property
    def state(self) -> State:
        return self._state

    def fire(self, event: Event, perform: Callable[[Step], None] | None = None) -> Step:
        """Apply `event` atomically. If `perform` is given and the step has effects, it runs under the lock after the
        state has advanced. It must run the effects in order and must not fire this machine again (the lock is not
        reentrant). If `perform` raises, the state stays advanced and the exception propagates. The caller then owes
        srt an answer on its own: an attempt write that failed must not be followed by a SEND_ALLOW."""
        with self._lock:
            result = step(self._state, event)
            self._state = result.state
            if perform is not None and result.effects:
                perform(result)
            return result
