"""levain.consent.broker_client — the consent hook's client for the queue broker (F10 S2a), in a slot session.

A hook inside a slot holds no ledger and opens no local :class:`~levain.consent.queue.ConsentQueue`. Its session's
launcher puts two variables in its environment (CONTRACT c3 §3.2): :data:`SOCKET_ENV`, the absolute path of the
slot's broker socket, and :data:`CAP_ENV`, the queue cap CONTROL minted for the session. A gated call is filed with
``REQUEST {cap, request}`` and held by polling ``ANSWER? {cap, request_id}``; the broker, never this client, checks
the answer's signature, so a ``{decision}`` from the socket is the only authority the hook reads here.

Frames are :mod:`levain.qbroker.wire`'s (imported, never restated). Every socket operation has a timeout
(:data:`IO_TIMEOUT`: the connect, the send and the whole answer frame), and every call is one connection with one
frame each way, so a broker that restarts mid-hold costs one refused call, never a hung hook.

What the hold treats as "no answer yet" (it keeps polling until the hold ends): ``{pending}``, ``rate-limited``
(the broker's own instruction to its hooks) and ``answer-refused`` (an answer row that is not authority: a forged
or unsigned row must not end the hold early, since the operator's real answer can still replace it). The broker's
``request-expired`` ends the hold. Anything else (no socket or cap in the environment, an unreachable socket, a
refusal of any other name, an answer that is not exactly one of the shapes above) raises :class:`BrokerError`, and
the adapters deny on it (fail closed). Stdlib only.
"""
from __future__ import annotations

import itertools
import os
import socket
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from levain.cockpit.consent import utc_stamp
from levain.consent.record import ExternalGateRequest, redact_display, tool_input_digest
from levain.qbroker import wire

__all__ = ["CAP_ENV", "IO_TIMEOUT", "PENDING_REFUSALS", "SOCKET_ENV", "BrokerClient", "BrokerError",
           "gate_request", "hold_for_decision", "in_slot"]

SOCKET_ENV = "LEVAIN_QUEUE_SOCKET"
CAP_ENV = "LEVAIN_QUEUE_CAP"
# the longest any one socket operation may take (the connect, the send, the whole answer frame). The broker answers
# from a file read and a cached signature check; a broker slower than this is treated as down (the gate denies)
IO_TIMEOUT = 5.0
# refusals that mean "no verified answer yet": the hold keeps polling through them
PENDING_REFUSALS = frozenset({"rate-limited", "answer-refused"})
# the broker's refusal once the request's hold has ended: the hold is over, nothing was decided
EXPIRED = "request-expired"
DECISIONS = ("allow", "deny")


class BrokerError(Exception):
    """The broker could not give an answer the gate may act on; ``name`` says why (a broker refusal name, or one
    of this client's own: ``no-socket``, ``no-cap``, ``unreachable``, ``malformed``)."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"{name}: {detail}" if detail else name)
        self.name = name


def in_slot(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this hook runs in a slot session: :data:`SOCKET_ENV` is in its environment (set at all, even empty:
    an empty one is a slot whose broker cannot be reached, which denies, never the local path)."""
    return SOCKET_ENV in (os.environ if environ is None else environ)


class BrokerClient:
    def __init__(self, path: str, cap: str, *, timeout: float = IO_TIMEOUT) -> None:
        self._path = path
        self._cap = cap
        self._timeout = timeout
        self._ids = itertools.count(1)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, timeout: float = IO_TIMEOUT) -> "BrokerClient":
        env = os.environ if environ is None else environ
        path, cap = env.get(SOCKET_ENV, ""), env.get(CAP_ENV, "")
        if not path or not os.path.isabs(path):
            # a relative path would resolve against the hook's cwd, the project the agent writes
            raise BrokerError("no-socket", f"{SOCKET_ENV} is not an absolute path")
        if not cap:
            raise BrokerError("no-cap", f"{CAP_ENV} is not set")
        return cls(path, cap, timeout=timeout)

    def call(self, op: str, **fields: Any) -> dict[str, Any]:
        """One frame to the broker and its one answer, as ``{"ok": True, ...}``; a refusal raises
        :class:`BrokerError` with the broker's name for it."""
        rid = next(self._ids)
        message = {"v": wire.VERSION, "op": op, "id": rid, "cap": self._cap, **fields}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(self._path)
                wire.write_frame(sock, message)
                got = wire.read_frame(sock, timeout=self._timeout)
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

    def request(self, request: ExternalGateRequest) -> str:
        """File ``request``; its request id, which must be the id this client sealed (the broker re-seals the
        same fields, so any other id is not an answer about this request)."""
        fields = request.fields()
        fields.pop("id")
        got = self.call("REQUEST", request=fields)
        if got.get("request_id") != request.id or not isinstance(got.get("queue_id"), str):
            raise BrokerError("malformed", "REQUEST answered for another request")
        return request.id

    def answer(self, request_id: str) -> str | None:
        """``allow`` / ``deny`` once the broker verified an answer; ``None`` while there is none yet
        (:data:`PENDING_REFUSALS` included). :data:`EXPIRED` and every other refusal raise."""
        try:
            got = self.call("ANSWER?", request_id=request_id)
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


def hold_for_decision(client: BrokerClient, request_id: str, hold: float, *, poll: float,
                      sleep: Callable[[float], None] = time.sleep,
                      monotonic: Callable[[], float] = time.monotonic) -> str | None:
    """Poll ``ANSWER?`` every ``poll`` seconds for up to ``hold``: the verified decision, or ``None`` when the hold
    ended with none (the broker's :data:`EXPIRED` included). Raises :class:`BrokerError` for anything else."""
    deadline = monotonic() + hold
    while True:
        try:
            got = client.answer(request_id)
        except BrokerError as exc:
            if exc.name == EXPIRED:
                return None
            raise
        if got is not None:
            return got
        left = deadline - monotonic()
        if left <= 0:
            return None
        sleep(min(poll, left))
