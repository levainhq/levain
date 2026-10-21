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
not exactly one of the shapes above) raises :class:`BrokerError`, and the adapters deny on it (fail closed), an ungated
grantable call included.

THE TYPED CONTRACT GRANT (F4, W2-c3; W2C_DESIGN §1, §4; head ruling 1020+49). The gate files only agent config, and a
gated call carries no grant (the irreversible tier stays the operator's). A call it does NOT gate whose tool a
dispatch contract may grant (a Write inside the repository) is sent anyway (:func:`file_ungated`), with its ``grant``
member and ``"ungated": true``, so a dispatch run's signed contract can allow it. Outside a dispatch session the
broker answers :data:`GO`, and GO IS NOT ALLOW: the adapter gives no decision and the session's own mode decides,
exactly as before the broker was asked. Only an explicit GO does that: a failure is a deny, as for a gated call.
:func:`grant_member` reads the paths with :func:`levain.dispatch.grant.grant_paths`; a call that is not
grantable, or whose parse is not total, gets no member and is not sent. :func:`gate_request` given the member seals
the request with the ``pb1:`` digest of the same paths and input (:func:`levain.dispatch.grant.input_bound_digest`:
one function for the digest, so the hook and the broker cannot compute it two ways). The hook does not know its
session's kind and does not need to: whether a member grants anything is the broker's decision, from CONTROL's word
on the session and the run's worktree root. The root a Codex patch's paths join to is the hook's ``cwd`` (Claude
Code's paths are absolute), which is where Codex itself resolves them; by the design (W2C_DESIGN §7) a Codex call
whose cwd is not the run's worktree root goes to the queue.

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

__all__ = ["BROKER_ACCOUNT", "CAP_ENV", "FAST_POLLS", "GO", "IO_TIMEOUT", "PENDING_REFUSALS", "REQUEST_RETRY",
           "SLOW_POLL", "SOCKET_ENV", "BrokerClient", "BrokerError", "UNGATED_TIER", "broker_uid", "file_and_hold", "file_ungated", "gate_request",
           "grant_member", "hold_for_decision", "in_slot"]

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
# the broker's answer to an ungated REQUEST outside a dispatch session: no decision, the session's mode decides
GO = "go"
# the tier an ungated call is filed at when a dispatch run queues it: a file write inside the repository (agent config
# is gated, and filed at levain.consent.policy.AGENT_CONFIG_TIER)
UNGATED_TIER = "T2"
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

    def request(self, request: ExternalGateRequest, *, timeout: float | None = None,
                grant: Mapping[str, Any] | None = None, ungated: bool = False) -> str:
        """File ``request``; its request id, which must be the id this client sealed (the broker re-seals the
        same fields, so any other id is not an answer about this request). ``grant``: :func:`grant_member`'s
        member, sent beside ``request`` (``request`` must be sealed with it, :func:`gate_request`). ``ungated``
        (with a member only): the call is one the gate does not gate; then :data:`GO` is an answer too."""
        fields = request.fields()
        fields.pop("id")
        extra: dict[str, Any] = {} if grant is None else {"grant": dict(grant)}
        if ungated:
            if grant is None:
                raise ValueError("an ungated request carries its grant member")
            extra["ungated"] = True
        got = self.call("REQUEST", timeout=timeout, request=fields, **extra)
        if ungated and set(got) - {"id", "ok"} == {"go"} and got["go"] is True:
            return GO
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


def grant_member(agent: str, tool_name: str, tool_input: Any, root: str) -> dict[str, Any] | None:
    """The REQUEST's ``grant`` member ``{"paths", "input"}`` for a call a dispatch contract may grant: ``paths``
    from :func:`levain.dispatch.grant.grant_paths` (``root``: the patch paths' base, the hook's ``cwd``), ``input``
    the :func:`levain.cockpit.consent.value_sha256` of ``tool_input``. ``None`` when the call is not grantable or its
    parse is not total. Never raises: a call it cannot read sends no member."""
    try:
        # here, not at import: an import cycle (levain.dispatch.grant -> the Codex adapter -> this module)
        from levain.cockpit.consent import value_sha256
        from levain.dispatch.grant import grant_paths

        paths = grant_paths(agent, tool_name, tool_input, root)
        if paths is None:
            return None
        return {"paths": list(paths), "input": value_sha256(tool_input)}
    except Exception:  # noqa: BLE001 - no member is today's request, never a failed gate
        return None


def gate_request(*, agent: str, session: str, tool_name: str, tool_input: Any, cwd: str, event_id: str | None,
                 tier: str, hold: float, now: datetime | None = None,
                 grant: Mapping[str, Any] | None = None) -> ExternalGateRequest:
    """The request a slot's gate files: the fields :meth:`ConsentQueue.file` would seal for the same call (its
    tool input digest and redacted display, ``hook:<event id>`` or an unpredictable ``rand:`` one, the hold as
    ``expires_at``). With ``grant`` (:func:`grant_member`'s member for this call), the digest is instead the
    ``pb1:`` digest of the member's paths and input hash (:func:`levain.dispatch.grant.input_bound_digest`)."""
    now = now if now is not None else datetime.now(timezone.utc)
    event = f"hook:{event_id}" if event_id else "rand:" + os.urandom(16).hex()
    if grant is None:
        digest = tool_input_digest(tool_input)
    else:
        from levain.dispatch.grant import input_bound_digest

        digest = input_bound_digest(tool_name, grant["paths"], grant["input"])
    return ExternalGateRequest.create(
        agent=agent, agent_session_id=session, event_id=event, tool_name=tool_name,
        tool_input_digest=digest, display=redact_display(tool_name, tool_input), cwd=cwd,
        tier=tier, received_at=utc_stamp(now), expires_at=utc_stamp(now + timedelta(seconds=hold)))


def file_ungated(client: BrokerClient | None, *, agent: str, session: str, tool_name: str, tool_input: Any, cwd: str,
                 event_id: str | None, hold: float, poll: float, expected_uid: int | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> tuple[str | None, str] | None:
    """A call the gate does not gate, sent to the broker when a dispatch contract may grant it: ``None`` (nothing
    sent) when :func:`grant_member` gives no member; else ``(outcome, request id)``, the outcome :data:`GO`, a
    verified decision, or ``None`` when the hold ended with none. Filed at :data:`UNGATED_TIER`; ``client``
    defaults to :meth:`BrokerClient.from_env`. Failures raise as :func:`file_and_hold`'s do."""
    member = grant_member(agent, tool_name, tool_input, cwd)
    if member is None:
        return None
    client = client if client is not None else BrokerClient.from_env(expected_uid=expected_uid)
    request = gate_request(agent=agent, session=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                           event_id=event_id, tier=UNGATED_TIER, hold=hold, grant=member)
    return file_and_hold(client, request, hold, poll=poll, sleep=sleep, monotonic=monotonic, grant=member,
                         ungated=True), request.id


def file_and_hold(client: BrokerClient, request: ExternalGateRequest, hold: float, *, poll: float,
                  sleep: Callable[[float], None] = time.sleep,
                  monotonic: Callable[[], float] = time.monotonic,
                  grant: Mapping[str, Any] | None = None, ungated: bool = False) -> str | None:
    """File ``request`` (with its ``grant`` member, when it was sealed with one; ``ungated`` as
    :meth:`BrokerClient.request`) and hold for its decision, all of it within ``hold`` (the REQUEST included, each
    socket operation capped at the time left): the verified decision, :data:`GO` when the broker answered so (no
    hold: nothing was filed), or ``None`` when the hold ended with none. A ``rate-limited`` REQUEST is sent once
    more after :data:`REQUEST_RETRY`; a second refusal, or any other failure, raises :class:`BrokerError`."""
    deadline = monotonic() + hold
    extra: dict[str, Any] = {} if grant is None else {"grant": grant}
    if ungated:
        extra["ungated"] = True
    try:
        got = client.request(request, timeout=max(deadline - monotonic(), 0.001), **extra)
    except BrokerError as exc:
        if exc.name != "rate-limited":
            raise
        left = deadline - monotonic()
        if left <= REQUEST_RETRY:
            raise
        sleep(REQUEST_RETRY)
        got = client.request(request, timeout=max(deadline - monotonic(), 0.001), **extra)
    if got == GO:
        return GO
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
