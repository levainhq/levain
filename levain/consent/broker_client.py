"""levain.consent.broker_client — the consent hook's client for the queue broker (F10 S2a), in a slot session.

A hook inside a slot holds no ledger and opens no local :class:`~levain.consent.queue.ConsentQueue`. Its session's
launcher puts two variables in its environment (CONTRACT c3 §3.2): :data:`SOCKET_ENV`, the absolute path of the
slot's broker socket, and :data:`CAP_ENV`, the queue cap CONTROL minted for the session. A gated call is filed with
``REQUEST {cap, request}`` and held by polling ``ANSWER? {cap, request_id}``; the broker, never this client, checks
the answer's signature, so a ``{decision}`` from the socket is the only authority the hook reads here.

The client authenticates the server (L3 r1 A): after every connect it reads the socket's PEER uid from the kernel
(:func:`levain.qbroker.peercred.peer_uid`, the listener's own primitive; for a connecting socket it is the uid that
called ``listen``) and refuses (``peer-not-broker``) unless it is the uid of :data:`BROKER_ACCOUNT`, looked up with
:func:`pwd.getpwnam`. A machine without that account (``no-broker-account``) reaches no broker at all. The variable
names WHERE to connect; it can never name WHO answers, so a socket an untrusted repository's own process listens
on (its settings file deleted after the session inherited the variable) is refused before a frame is sent. The
expected uid is never read from the environment or a file the session can write; callers other than the hook
(tests, a run) pass it as ``expected_uid``.

Frames are :mod:`levain.qbroker.wire`'s (imported, never restated). Every socket operation has a timeout (the
connect, the send and the whole answer frame): :data:`IO_TIMEOUT`, or less, the time left in the hold
(:func:`file_and_hold`), so the hook ends within its hold however slow the broker is. Every call is one connection
with one frame each way, so a broker that restarts mid-hold costs one refused call, never a hung hook.

What the hold treats as "no answer yet" (it keeps polling until the hold ends): ``{pending}``, ``rate-limited``
(the broker's own instruction to its hooks) and ``answer-refused`` (an answer row that is not authority: a forged
or unsigned row must not end the hold early, since the operator's real answer can still replace it). The broker's
``request-expired`` ends the hold. A ``rate-limited`` REQUEST is retried once after :data:`REQUEST_RETRY`. The
poll is the caller's interval for the first :data:`FAST_POLLS` polls, then :data:`SLOW_POLL` (a 300 s hold is then
about 300 connections, not 1,200, well under the broker's per-slot rate). Anything else (no socket or cap in the
environment, an unreachable socket, a peer that is not the broker, a refusal of any other name, an answer that is
not exactly one of the shapes above) raises :class:`BrokerError`, and the adapters deny on it (fail closed).

Known edge (L3 r1 D): the same hook event delivered twice files two requests (the event id is sealed into the
request, but this client keeps no record between hook processes). The harness delivers one PreToolUse per call; a
duplicate costs one extra pending row, which expires. Stdlib only.
"""
from __future__ import annotations

import itertools
import os
import pwd
import socket
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from levain.cockpit.consent import utc_stamp
from levain.consent.record import ExternalGateRequest, redact_display, tool_input_digest
from levain.qbroker import wire

__all__ = ["BROKER_ACCOUNT", "CAP_ENV", "FAST_POLLS", "IO_TIMEOUT", "PENDING_REFUSALS", "REQUEST_RETRY",
           "SLOW_POLL", "SOCKET_ENV", "BrokerClient", "BrokerError", "broker_uid", "file_and_hold", "gate_request",
           "hold_for_decision", "in_slot"]

SOCKET_ENV = "LEVAIN_QUEUE_SOCKET"
CAP_ENV = "LEVAIN_QUEUE_CAP"
# the account the queue broker runs as (CONTRACT c3 §1.3; one of levain.slot.ids.SINGLETONS, which holds no
# separate constant for it): the only peer uid this client accepts
BROKER_ACCOUNT = "_levain_qbroker"
# the longest any one socket operation may take (the connect, the send, the whole answer frame). The broker answers
# from a file read and a cached signature check; a broker slower than this is treated as down (the gate denies)
IO_TIMEOUT = 5.0
# refusals that mean "no verified answer yet": the hold keeps polling through them
PENDING_REFUSALS = frozenset({"rate-limited", "answer-refused"})
# the broker's refusal once the request's hold has ended: the hold is over, nothing was decided
EXPIRED = "request-expired"
DECISIONS = ("allow", "deny")
# the hold polls at the caller's interval this many times (an answer given at once lands fast), then every SLOW_POLL
FAST_POLLS = 8
SLOW_POLL = 1.0
# a REQUEST refused rate-limited is sent once more after this long (well past the broker's token interval)
REQUEST_RETRY = 1.0
# a socket timeout and time.monotonic read different clocks: a call that ran out this close to the hold's end ended
# the hold (undecided), it did not fail to run
DEADLINE_SLOP = 0.05


class BrokerError(Exception):
    """The broker could not give an answer the gate may act on; ``name`` says why (a broker refusal name, or one
    of this client's own: ``no-socket``, ``no-cap``, ``no-broker-account``, ``peer-not-broker``, ``unreachable``,
    ``malformed``)."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"{name}: {detail}" if detail else name)
        self.name = name


def in_slot(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this hook runs in a slot session: :data:`SOCKET_ENV` is in its environment (set at all, even empty:
    an empty one is a slot whose broker cannot be reached, which denies, never the local path)."""
    return SOCKET_ENV in (os.environ if environ is None else environ)


def broker_uid() -> int:
    """The uid of :data:`BROKER_ACCOUNT` from the account database; :class:`BrokerError` ``no-broker-account`` when
    there is none (a machine without slots: no socket is the broker's)."""
    try:
        return pwd.getpwnam(BROKER_ACCOUNT).pw_uid
    except KeyError:
        raise BrokerError("no-broker-account", f"no {BROKER_ACCOUNT} account on this machine") from None


class BrokerClient:
    def __init__(self, path: str, cap: str, *, timeout: float = IO_TIMEOUT, expected_uid: int | None = None) -> None:
        """``expected_uid``: the peer uid the socket must have; ``None`` (the hook) is :func:`broker_uid`, looked
        up at the first call."""
        self._path = path
        self._cap = cap
        self._timeout = timeout
        self._expected_uid = expected_uid
        self._ids = itertools.count(1)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, timeout: float = IO_TIMEOUT,
                 expected_uid: int | None = None) -> "BrokerClient":
        env = os.environ if environ is None else environ
        path, cap = env.get(SOCKET_ENV, ""), env.get(CAP_ENV, "")
        if not path or not os.path.isabs(path):
            # a relative path would resolve against the hook's cwd, the project the agent writes
            raise BrokerError("no-socket", f"{SOCKET_ENV} is not an absolute path")
        if not cap:
            raise BrokerError("no-cap", f"{CAP_ENV} is not set")
        return cls(path, cap, timeout=timeout, expected_uid=expected_uid)

    def _broker_uid(self) -> int:
        if self._expected_uid is None:
            self._expected_uid = broker_uid()
        return self._expected_uid

    def call(self, op: str, *, timeout: float | None = None, **fields: Any) -> dict[str, Any]:
        """One frame to the broker and its one answer, as ``{"ok": True, ...}``; a refusal raises
        :class:`BrokerError` with the broker's name for it. ``timeout`` (at most this client's own) bounds each
        call, connect to answer, under one deadline (each step gets only the time left)."""
        from levain.qbroker.peercred import peer_uid   # here, not at import: the local path loads nothing of it

        expected = self._broker_uid()
        limit = self._timeout if timeout is None else min(self._timeout, timeout)
        deadline = time.monotonic() + limit
        rid = next(self._ids)
        message = {"v": wire.VERSION, "op": op, "id": rid, "cap": self._cap, **fields}

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the call's time ran out")
            return remaining

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(left())
                sock.connect(self._path)
                try:
                    peer = peer_uid(sock)
                except Exception as exc:  # noqa: BLE001 - getsockopt's OSError, a struct.error: not the broker
                    raise BrokerError("peer-not-broker",
                                      f"the peer's uid could not be read ({type(exc).__name__})") from None
                if peer != expected:
                    # not a byte is sent: the cap and the request never reach a socket the broker does not own
                    raise BrokerError("peer-not-broker", f"the socket's peer is uid {peer}, not {BROKER_ACCOUNT}")
                sock.settimeout(left())
                wire.write_frame(sock, message)
                got = wire.read_frame(sock, timeout=left())
        except BrokerError:
            raise
        except wire.FrameError as exc:
            raise BrokerError("malformed", exc.name) from None
        except (OSError, ValueError) as exc:   # TimeoutError is an OSError; a path too long for sun_path a ValueError
            raise BrokerError("unreachable", type(exc).__name__) from None
        if got is None:     # closed unanswered: the listener refuses a peer of the wrong uid so
            raise BrokerError("unreachable", "the broker closed the connection unanswered")
        # exact types: 0 == False and True == 1 in Python, and neither is the broker's answer
        if not isinstance(got, dict) or type(got.get("id")) is not int or got["id"] != rid \
                or type(got.get("ok")) is not bool:
            raise BrokerError("malformed", "not an answer to this frame")
        if got["ok"] is False:
            name = got.get("refused")
            raise BrokerError(name if isinstance(name, str) and name else "malformed", "refused")
        return got

    def request(self, request: ExternalGateRequest, *, timeout: float | None = None) -> str:
        """File ``request``; its request id, which must be the id this client sealed (the broker re-seals the
        same fields, so any other id is not an answer about this request)."""
        fields = request.fields()
        fields.pop("id")
        got = self.call("REQUEST", timeout=timeout, request=fields)
        if got.get("request_id") != request.id or not isinstance(got.get("queue_id"), str):
            raise BrokerError("malformed", "REQUEST answered for another request")
        return request.id

    def answer(self, request_id: str, *, timeout: float | None = None) -> str | None:
        """``allow`` / ``deny`` once the broker verified an answer; ``None`` while there is none yet
        (:data:`PENDING_REFUSALS` included). :data:`EXPIRED` and every other refusal raise."""
        try:
            got = self.call("ANSWER?", timeout=timeout, request_id=request_id)
        except BrokerError as exc:
            if exc.name in PENDING_REFUSALS:
                return None
            raise
        keys = set(got) - {"id", "ok"}
        if keys == {"pending"} and got["pending"] is True:
            return None
        if keys == {"decision"} and got["decision"] in DECISIONS:
            return got["decision"]
        raise BrokerError("malformed", "ANSWER? answered neither a decision nor pending")


def gate_request(*, agent: str, session: str, tool_name: str, tool_input: Any, cwd: str, event_id: str | None,
                 tier: str, hold: float, now: datetime | None = None) -> ExternalGateRequest:
    """The request a slot's gate files: the fields :meth:`ConsentQueue.file` would seal for the same call (its
    tool input digest and redacted display, ``hook:<event id>`` or an unpredictable ``rand:`` one, the hold as
    ``expires_at``)."""
    now = now if now is not None else datetime.now(timezone.utc)
    event = f"hook:{event_id}" if event_id else "rand:" + os.urandom(16).hex()
    return ExternalGateRequest.create(
        agent=agent, agent_session_id=session, event_id=event, tool_name=tool_name,
        tool_input_digest=tool_input_digest(tool_input), display=redact_display(tool_name, tool_input), cwd=cwd,
        tier=tier, received_at=utc_stamp(now), expires_at=utc_stamp(now + timedelta(seconds=hold)))


def file_and_hold(client: BrokerClient, request: ExternalGateRequest, hold: float, *, poll: float,
                  sleep: Callable[[float], None] = time.sleep,
                  monotonic: Callable[[], float] = time.monotonic) -> str | None:
    """File ``request`` and hold for its decision, all of it within ``hold`` (the REQUEST included, each socket
    operation capped at the time left): the verified decision, or ``None`` when the hold ended with none. A
    ``rate-limited`` REQUEST is sent once more after :data:`REQUEST_RETRY`; a second refusal, or any other
    failure, raises :class:`BrokerError`."""
    deadline = monotonic() + hold
    try:
        client.request(request, timeout=max(deadline - monotonic(), 0.001))
    except BrokerError as exc:
        if exc.name != "rate-limited":
            raise
        left = deadline - monotonic()
        if left <= REQUEST_RETRY:
            raise
        sleep(REQUEST_RETRY)
        client.request(request, timeout=max(deadline - monotonic(), 0.001))
    return _hold(client, request.id, deadline, poll=poll, sleep=sleep, monotonic=monotonic)


def hold_for_decision(client: BrokerClient, request_id: str, hold: float, *, poll: float,
                      sleep: Callable[[float], None] = time.sleep,
                      monotonic: Callable[[], float] = time.monotonic) -> str | None:
    """Poll ``ANSWER?`` for up to ``hold`` (every ``poll`` seconds for :data:`FAST_POLLS` polls, then every
    :data:`SLOW_POLL`): the verified decision, or ``None`` when the hold ended with none (the broker's
    :data:`EXPIRED` included). Raises :class:`BrokerError` for anything else."""
    return _hold(client, request_id, monotonic() + hold, poll=poll, sleep=sleep, monotonic=monotonic)


def _hold(client: BrokerClient, request_id: str, deadline: float, *, poll: float,
          sleep: Callable[[float], None], monotonic: Callable[[], float]) -> str | None:
    for n in itertools.count():
        left = deadline - monotonic()
        if left <= 0:
            return None
        try:
            got = client.answer(request_id, timeout=left)
        except BrokerError as exc:
            if exc.name == EXPIRED:
                return None
            if exc.name == "unreachable" and monotonic() >= deadline - DEADLINE_SLOP:
                return None   # the call ran out of hold (its timeout was the time left): the hold ended undecided
            raise
        if got is not None:
            return got
        left = deadline - monotonic()
        if left <= 0:
            return None
        sleep(min(poll if n < FAST_POLLS else max(poll, SLOW_POLL), left))
    return None  # pragma: no cover - itertools.count never ends
