"""levain.qbroker.broker — the verbs a slot's hook may send, each answered from CONTROL's facts and signed rows only.

``REQUEST {cap, request}`` files a request: the cap resolves through CONTROL (:class:`Resolver`, by the LISTENER's
slot and the cap's sha256, never anything the hook claims about itself); the request is re-sealed from its fields,
so its id is the sealed id; the row takes ``session``, ``slot``, ``kind`` and ``run`` from the resolve only.
Filing the same sealed request again returns the row already there. ``ANSWER? {cap, request_id}`` reads the answer
row for a request this session filed and returns ``{decision}`` only when :func:`levain.qbroker.seam.answer_refusal`
accepts it, else ``{pending}`` while nothing is there and the hold runs. No verb accepts an answer, a grant answer
or a row from a hook. Every other op is ``verb-not-allowed``. The cap is hashed on arrival and never logged or
written. Stdlib only.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.cockpit.firejournal import _TIER_NEEDS
from levain.consent.away import MAX_HOLD
from levain.consent.record import ExternalGateRequest
from levain.qbroker import wire
from levain.qbroker.seam import answer_refusal, request_of
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import ANSWER_ROW_FORMAT, REQUEST_ROW_FORMAT, QueueStore, valid_request_id

__all__ = ["CAP_MAX", "KINDS", "QUOTA_PER_RUN", "RECEIVED_SKEW", "RUN_KEYS", "Broker", "Resolved", "Resolver",
           "resolved_from"]

log = logging.getLogger("levain.qbroker")

KINDS = ("dispatch", "interactive")
RUN_KEYS = frozenset({"run_id", "job_id", "run_index", "contract_digest", "model_requested"})
# requests one run (a dispatch run's run_id) or one interactive session may file. A hook files one request per
# permission prompt it does not settle itself; past this many, the agent is looping on a gate, and every row is a
# file the operator's cockpit lists, so the run is refused rather than allowed to bury the queue.
QUOTA_PER_RUN = 200
# how far a hook's received_at may sit from the broker's clock: the hook stamps it as it files, so only clock
# drift and the trip to this socket separate the two
RECEIVED_SKEW = timedelta(minutes=2)
CAP_MAX = 1024
_HOOK_REQUEST_KEYS = frozenset(ExternalGateRequest.__dataclass_fields__) - {"id"}
_ANSWER_ROW_KEYS = frozenset({"format", "payload", "signature"})


@dataclass(frozen=True)
class Resolved:
    """CONTROL's ``QUEUE-RESOLVE`` answer (CONTRACT c3 §3.3): the session the cap was minted for."""
    session: str
    kind: str
    run: Mapping[str, Any] | None


class Resolver(Protocol):
    def queue_resolve(self, slot: int, cap_digest: str) -> "Resolved | str":
        """The session ``cap_digest`` belongs to on ``slot``, or a refusal name."""


def resolved_from(answer: Any) -> Resolved | None:
    """A ``QUEUE-RESOLVE`` answer's fields as :class:`Resolved`, or ``None`` when they are not exactly that."""
    if not isinstance(answer, Mapping):
        return None
    session, kind, run = answer.get("session"), answer.get("kind"), answer.get("run")
    if not isinstance(session, str) or not session or kind not in KINDS:
        return None
    if kind == "dispatch":
        if not isinstance(run, Mapping) or set(run) != RUN_KEYS or not isinstance(run["run_id"], str) \
                or not run["run_id"]:
            return None
    elif run is not None:
        return None
    return Resolved(session=session, kind=kind, run=None if run is None else dict(run))


def cap_digest(cap: Any) -> str | None:
    if not isinstance(cap, str) or not cap or len(cap) > CAP_MAX:
        return None
    return hashlib.sha256(cap.encode("utf-8")).hexdigest()


def _quota_key(session: str, run: Mapping[str, Any] | None) -> str:
    return f"run:{run['run_id']}" if run is not None else f"session:{session}"


class Broker:
    def __init__(self, store: QueueStore, resolver: Resolver, signers: SignersSource, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ssh_keygen: Path | str | None = None, quota: int = QUOTA_PER_RUN) -> None:
        self._store = store
        self._resolver = resolver
        self._signers = signers
        self._clock = clock
        self._ssh_keygen = ssh_keygen
        self._quota = quota
        self._counts: dict[str, int] = {}
        self._counts_mutex = threading.Lock()   # held for a dict update only, never across I/O
        for row in store.rows():
            if isinstance(row.get("session"), str):
                key = _quota_key(row["session"], row.get("run") if isinstance(row.get("run"), Mapping) else None)
                self._counts[key] = self._counts.get(key, 0) + 1

    def handle(self, slot: int, message: Mapping[str, Any]) -> dict[str, Any]:
        """The one answer to ``message`` from the hook on ``slot``'s socket."""
        rid = message.get("id")
        if message.get("v") != wire.VERSION or type(rid) is not int:
            return wire.refusal(rid if type(rid) is int else None, "frame-malformed")
        op = message.get("op")
        try:
            if op == "REQUEST":
                return self._request(slot, rid, message)
            if op == "ANSWER?":
                return self._answer(slot, rid, message)
        except Exception:   # noqa: BLE001 - one request's failure is its refusal, never the listener's
            log.exception("qbroker: %s on slot %d failed", op, slot)
            return wire.refusal(rid, "broker-error")
        return wire.refusal(rid, "verb-not-allowed")

    def _resolve(self, slot: int, cap: Any) -> Resolved | str:
        digest = cap_digest(cap)
        if digest is None:
            return "cap-malformed"
        try:
            got = self._resolver.queue_resolve(slot, digest)
        except Exception:   # noqa: BLE001 - CONTROL unreachable: the hook fails closed on the refusal
            log.exception("qbroker: QUEUE-RESOLVE on slot %d failed", slot)
            return "control-unavailable"
        if isinstance(got, str):
            return got
        return got if isinstance(got, Resolved) and resolved_from(
            {"session": got.session, "kind": got.kind, "run": got.run}) is not None else "resolve-malformed"

    def _request(self, slot: int, rid: int, message: Mapping[str, Any]) -> dict[str, Any]:
        who = self._resolve(slot, message.get("cap"))
        if isinstance(who, str):
            return wire.refusal(rid, who)
        fields = message.get("request")
        if not isinstance(fields, Mapping) or not _HOOK_REQUEST_KEYS <= set(fields) <= _HOOK_REQUEST_KEYS | {"id"}:
            return wire.refusal(rid, "request-malformed")
        try:
            request = ExternalGateRequest.create(**{k: fields[k] for k in _HOOK_REQUEST_KEYS})
            received, expires = parse_stamp(request.received_at), parse_stamp(request.expires_at)
        except (ConsentError, TypeError, ValueError):
            return wire.refusal(rid, "request-malformed")
        if request.tier not in _TIER_NEEDS:
            return wire.refusal(rid, "request-malformed")
        now = self._clock()
        if abs(received - now) > RECEIVED_SKEW:
            return wire.refusal(rid, "request-clock")
        if expires <= now or expires - received > timedelta(seconds=MAX_HOLD):
            return wire.refusal(rid, "request-hold")
        row: dict[str, Any] = {"format": REQUEST_ROW_FORMAT, "queue_id": self._store.queue_id,
                               "request": request.fields(), "session": who.session, "slot": slot, "kind": who.kind}
        if who.run is not None:
            row["run"] = dict(who.run)
        existing = self._store.read_request(request.id)
        if existing is None:
            key = _quota_key(who.session, who.run)
            with self._counts_mutex:
                if self._counts.get(key, 0) >= self._quota:
                    return wire.refusal(rid, "quota-exhausted")
                self._counts[key] = self._counts.get(key, 0) + 1
            if self._store.write_request(row):
                return wire.answer(rid, request_id=request.id, queue_id=self._store.queue_id)
            with self._counts_mutex:           # another connection filed it first: this one counts nothing
                self._counts[key] -= 1
            existing = self._store.read_request(request.id)
        if existing != row:
            return wire.refusal(rid, "request-exists")
        return wire.answer(rid, request_id=request.id, queue_id=self._store.queue_id)

    def _answer(self, slot: int, rid: int, message: Mapping[str, Any]) -> dict[str, Any]:
        who = self._resolve(slot, message.get("cap"))
        if isinstance(who, str):
            return wire.refusal(rid, who)
        request_id = message.get("request_id")
        if not valid_request_id(request_id):
            return wire.refusal(rid, "no-such-request")
        try:
            row = self._store.read_request(request_id)
        except ValueError:
            row = None
        # one name for "no row" and "another session's row": a hook learns nothing about other sessions' ids
        if row is None or row.get("session") != who.session or row.get("slot") != slot:
            return wire.refusal(rid, "no-such-request")
        request = request_of(row.get("request"))
        if request is None or row.get("queue_id") != self._store.queue_id:
            return wire.refusal(rid, "no-such-request")
        now = self._clock()
        if request.expires_at <= utc_stamp(now):
            return wire.refusal(rid, "request-expired")
        try:
            answer = self._store.read_answer(request_id)
        except (ValueError, OSError) as exc:
            log.warning("qbroker: answer row for %s refused: %s", request_id, exc)
            return wire.refusal(rid, "answer-refused")
        if answer is None:
            return wire.answer(rid, pending=True)
        if set(answer) != _ANSWER_ROW_KEYS or answer["format"] != ANSWER_ROW_FORMAT:
            log.warning("qbroker: answer row for %s is not a %s row", request_id, ANSWER_ROW_FORMAT)
            return wire.refusal(rid, "answer-refused")
        why = answer_refusal(row, answer["payload"], answer["signature"], self._signers.current(), now,
                             ssh_keygen=self._ssh_keygen)
        if why is not None:
            log.warning("qbroker: answer for %s refused: %s", request_id, why)
            return wire.refusal(rid, "answer-refused")
        return wire.answer(rid, decision=answer["payload"]["decision"])
