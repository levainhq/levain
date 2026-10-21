"""levain.qbroker.broker — the verbs a slot's hook may send, each answered from CONTROL's facts and signed rows only.

``REQUEST {cap, request}`` files a request: the cap resolves through CONTROL (:class:`Resolver`, by the LISTENER's
slot and the cap's ``sha256:`` digest, never anything the hook claims about itself); the request is re-sealed from its fields,
so its id is the sealed id; the row takes ``session``, ``slot``, ``kind`` and ``run`` from the resolve only.
Filing the same sealed request again returns the row already there (before the clock and hold checks, which bound
new rows only); a new row is refused ``queue-busy`` past :data:`PENDING_PER_SESSION` and ``quota-exhausted`` past
the lifetime quota. ``ANSWER? {cap, request_id}`` reads the answer row for a request this session filed and returns
``{decision}`` only when :func:`levain.qbroker.seam.answer_refusal` accepts it (one signed in time, even when read after
the hold ended), else ``{pending}`` while nothing is
there and the hold runs; polls past :data:`ANSWER_RATE` per slot are refused ``rate-limited`` (a REQUEST too, for about one token interval
after polls drain the bucket: the consent hook treats ``rate-limited`` as pending and retries). A verdict is reused
while the answer file and the signer snapshot are unchanged, and a resolve for :data:`RESOLVE_TTL` seconds, so a
serial poll storm costs one verify and one CONTROL round trip; concurrent misses on a cold cache may each make
the trip, bounded by the slot's rate (a REQUEST whose resolve misses the cache is charged to it too). One filing
per request id runs at a time, so identical REQUESTs on several connections link one row and charge it once.
No verb accepts an answer, a grant answer or a row from a hook. Every other op is ``verb-not-allowed``.

THE TYPED CONTRACT GRANT (F4, W2-c2; W2C_DESIGN §1, §3, §4, §8). A REQUEST may carry, beside ``request``, a ``grant``
member ``{paths, input}``. The broker recomputes the request's ``pb1:`` digest from it and refuses
``grant-digest-mismatch`` (nothing written) when it is not the sealed ``tool_input_digest``. A NEW row whose member
binds is then decided by :func:`levain.qbroker.grants.decide` (a dispatch session, the operator-signed contract the
run names, its expiry, the signature against the snapshot's contract signers, the paths under the run's
``repo_root`` and inside the signed write list with no link on the way): when it holds, the request row is written
as always and beside it the broker's own ``levain-queue-grant-allow/1`` row, and ``ANSWER?`` returns ``allow`` from
that row unless the operator's verified answer is there, which is read first and outranks it (his deny wins); such a
row is not pending (it waits on no one), but it is charged to the quota like any other. Anything the grant cannot
decide files the request as today, for the operator; a run CONTROL names without ``repo_root`` resolves as before
and never takes a grant. A replay never re-decides. The cap is hashed on arrival and never logged or
written. Stdlib only.
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.cockpit.firejournal import _TIER_NEEDS
from levain.consent.away import MAX_HOLD
from levain.consent.record import ExternalGateRequest
from levain.qbroker import grants, wire
from levain.qbroker.seam import answer_refusal, grant_allow_refusal, request_of
from levain.qbroker.signers import SignersSnapshot, SignersSource
from levain.qbroker.store import ANSWER_ROW_FORMAT, REQUEST_ROW_FORMAT, QueueStore, valid_request_id

__all__ = ["ANSWER_BURST", "ANSWER_RATE", "CAP_MAX", "KINDS", "PENDING_PER_SESSION", "QUOTA_PER_RUN",
           "RECEIVED_SKEW", "RESOLVE_TTL", "RUN_KEYS", "RUN_OPTIONAL_KEYS", "SLOT_CLASS", "Broker", "Resolved",
           "Resolver", "resolved_from", "slot_ref"]

log = logging.getLogger("levain.qbroker")

KINDS = ("dispatch", "interactive")
RUN_KEYS = frozenset({"run_id", "job_id", "run_index", "contract_digest", "model_requested"})
# CONTRACT §26 S1-c: repo_root = STORE/live/<session>/repo, the run's worktree, DERIVED by CONTROL (never the hook's
# cwd). Optional on the wire: a run without it resolves and files as before, and only the grant needs it (absent,
# every call queues)
RUN_OPTIONAL_KEYS = frozenset({"repo_root"})
# requests one run (a dispatch run's run_id) or one interactive session may file. A hook files one request per
# permission prompt it does not settle itself; past this many, the agent is looping on a gate, and every row is a
# file the operator's cockpit lists, so the run is refused rather than allowed to bury the queue.
QUOTA_PER_RUN = 200
# requests one session may hold unexpired and unanswered: the quota bounds the disk, this bounds his attention.
# One past it is refused `queue-busy` until one is answered or its hold ends
PENDING_PER_SESSION = 4
# ANSWER? polls a slot may make per second, and the burst it may save up (a REQUEST whose resolve misses the
# cache draws on the same bucket). Set above what PENDING_PER_SESSION rows polled at the hooks' fastest interval
# (GATE_POLL) need; past the rate a poll is refused `rate-limited`, which a hook must treat as no answer yet (pending,
# and deny when its hold ends)
ANSWER_RATE = 20.0
ANSWER_BURST = 40
# how long a QUEUE-RESOLVE answer (a refusal too) is reused for one (slot, cap digest): a revoked cap is served
# for at most this long after CONTROL revokes it
RESOLVE_TTL = 2.0
RESOLVE_CACHE_MAX = 256
# refusals (a guessed or revoked cap) live in their own small cache, so fresh random caps can never evict a real cap's answer
REFUSAL_CACHE_MAX = 32
VERDICT_CACHE_MAX = 1024
# how far a hook's received_at may sit from the broker's clock: the hook stamps it as it files, so only clock
# drift and the trip to this socket separate the two
RECEIVED_SKEW = timedelta(minutes=2)
CAP_MAX = 1024
# CONTRACT c5 §1.1: a slot is {class, index}, never the bare index; R/qb/slot<n>/ exists for the egress class only,
# so a listener's n is egress index n
SLOT_CLASS = "egress"
# CONTRACT c6: every *_digest is sha256: + 64 lowercase hex (the run a row carries verbatim included)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HOOK_REQUEST_KEYS = frozenset(ExternalGateRequest.__dataclass_fields__) - {"id"}
_ANSWER_ROW_KEYS = frozenset({"format", "payload", "signature"})


@dataclass(frozen=True)
class Resolved:
    """CONTROL's ``QUEUE-RESOLVE`` answer (CONTRACT c3 §3.3): the session the cap was minted for."""
    session: str
    kind: str
    run: Mapping[str, Any] | None


class Resolver(Protocol):
    def queue_resolve(self, slot: Mapping[str, Any], cap_digest: str) -> "Resolved | str":
        """The session ``cap_digest`` belongs to on ``slot`` (a :func:`slot_ref`), or a refusal name."""


def resolved_from(answer: Any) -> Resolved | None:
    """A ``QUEUE-RESOLVE`` answer's fields as :class:`Resolved`, or ``None`` when they are not exactly that."""
    if not isinstance(answer, Mapping):
        return None
    session, kind, run = answer.get("session"), answer.get("kind"), answer.get("run")
    if not isinstance(session, str) or not session or kind not in KINDS:
        return None
    if kind == "dispatch":
        if not isinstance(run, Mapping) or not RUN_KEYS <= set(run) <= RUN_KEYS | RUN_OPTIONAL_KEYS \
                or not isinstance(run["run_id"], str) or not run["run_id"] \
                or not isinstance(run["contract_digest"], str) or _DIGEST.fullmatch(run["contract_digest"]) is None \
                or ("repo_root" in run and not _root_ok(run["repo_root"])):
            return None
    elif run is not None:
        return None
    return Resolved(session=session, kind=kind, run=None if run is None else dict(run))


def _root_ok(root: Any) -> bool:
    """An absolute, normalised directory path that is not ``/`` and holds no NUL."""
    return (isinstance(root, str) and root.startswith("/") and root != "/" and "\0" not in root
            and posixpath.normpath(root) == root)


def slot_ref(index: int) -> dict[str, Any]:
    """The slot a listener serves, as CONTROL names it: ``{class, index}``."""
    return {"class": SLOT_CLASS, "index": index}


def cap_digest(cap: Any) -> str | None:
    """``sha256:`` + 64 lowercase hex over the cap's UTF-8 bytes (CONTRACT c6: every ``*_digest`` on the wire)."""
    if not isinstance(cap, str) or not cap or len(cap) > CAP_MAX:
        return None
    return "sha256:" + hashlib.sha256(cap.encode("utf-8")).hexdigest()


def _quota_key(session: str, run: Mapping[str, Any] | None) -> str:
    return f"run:{run['run_id']}" if run is not None else f"session:{session}"


class Broker:
    def __init__(self, store: QueueStore, resolver: Resolver, signers: SignersSource, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 monotonic: Callable[[], float] = time.monotonic,
                 ssh_keygen: Path | str | None = None, quota: int = QUOTA_PER_RUN,
                 pending_cap: int = PENDING_PER_SESSION, answer_rate: float = ANSWER_RATE,
                 answer_burst: int = ANSWER_BURST, resolve_ttl: float = RESOLVE_TTL,
                 contracts: Path | str | None = None, contracts_owner: int | None = None) -> None:
        """``contracts``: the directory dispatch publishes signed contracts in (``L/contracts/``, CONTRACT §26 S1-b),
        whose files must be owned by ``contracts_owner`` (dispatch's uid); without both, no grant ever applies."""
        self._store = store
        self._contracts = None if contracts is None else Path(contracts)
        self._contracts_owner = contracts_owner
        self._resolver = resolver
        self._signers = signers
        self._clock = clock
        self._monotonic = monotonic
        self._ssh_keygen = ssh_keygen
        self._quota = quota
        self._pending_cap = pending_cap
        self._rate, self._burst = answer_rate, answer_burst
        self._resolve_ttl = resolve_ttl
        # every dict below is touched under this mutex, for the update only, never across I/O
        self._mutex = threading.Lock()
        self._counts: dict[str, int] = {}
        self._pending: dict[str, dict[str, str]] = {}           # session -> {request_id: expires_at}
        self._buckets: dict[int, tuple[float, float]] = {}      # slot -> (tokens, at)
        self._filing: dict[str, threading.Event] = {}          # request_id -> set when its filing ends
        self._resolved: OrderedDict[tuple[int, str], tuple[float, Resolved | str]] = OrderedDict()
        self._refused: OrderedDict[tuple[int, str], tuple[float, Resolved | str]] = OrderedDict()
        self._verdicts: OrderedDict[str, tuple[tuple[int, ...], SignersSnapshot | None, str | None]] = OrderedDict()
        now = utc_stamp(clock())
        for row in store.rows():
            session, run = row.get("session"), row.get("run")
            if not isinstance(session, str) or (run is not None and not (
                    isinstance(run, Mapping) and isinstance(run.get("run_id"), str))):
                log.warning("qbroker: a stored row without a session or a run_id is not counted")
                continue
            key = _quota_key(session, run)
            self._counts[key] = self._counts.get(key, 0) + 1
            request = row.get("request")
            expires = request.get("expires_at") if isinstance(request, Mapping) else None
            if isinstance(expires, str) and expires > now and isinstance(request.get("id"), str) \
                    and not self._granted(request["id"]):
                self._pending.setdefault(session, {})[request["id"]] = expires

    def _granted(self, request_id: str) -> bool:
        try:
            return self._store.has_grant(request_id)
        except (ValueError, OSError):
            return False

    def handle(self, slot: int, message: Mapping[str, Any]) -> dict[str, Any]:
        """The one answer to ``message`` from the hook on ``slot``'s socket."""
        rid = message.get("id")
        v = message.get("v")
        if type(v) is not int or v != wire.VERSION or type(rid) is not int:
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

    def _resolve(self, slot: int, cap: Any, *, charge_miss: bool = False) -> Resolved | str:
        """``charge_miss``: a cache miss takes a token from the slot's bucket (REQUEST; ANSWER? took one already)."""
        digest = cap_digest(cap)
        if digest is None:
            return "cap-malformed"
        key, now = (slot, digest), self._monotonic()
        with self._mutex:
            for cache in (self._resolved, self._refused):
                hit = cache.get(key)
                if hit is not None and hit[0] > now:
                    return hit[1]
        if charge_miss and not self._take_token(slot):
            return "rate-limited"
        try:
            got = self._resolver.queue_resolve(slot_ref(slot), digest)
        except Exception:   # noqa: BLE001 - CONTROL unreachable: the hook fails closed on the refusal
            log.exception("qbroker: QUEUE-RESOLVE on slot %d failed", slot)
            got = "control-unavailable"
        if not isinstance(got, str) and not (isinstance(got, Resolved) and resolved_from(
                {"session": got.session, "kind": got.kind, "run": got.run}) is not None):
            got = "resolve-malformed"
        with self._mutex:
            cache, limit = ((self._refused, REFUSAL_CACHE_MAX) if isinstance(got, str)
                            else (self._resolved, RESOLVE_CACHE_MAX))
            cache[key] = (now + self._resolve_ttl, got)
            cache.move_to_end(key)
            while len(cache) > limit:
                cache.popitem(last=False)
        return got

    def _take_token(self, slot: int) -> bool:
        now = self._monotonic()
        with self._mutex:
            tokens, at = self._buckets.get(slot, (float(self._burst), now))
            tokens = min(float(self._burst), tokens + (now - at) * self._rate)
            if tokens < 1.0:
                self._buckets[slot] = (tokens, now)
                return False
            self._buckets[slot] = (tokens - 1.0, now)
            return True

    def _request(self, slot: int, rid: int, message: Mapping[str, Any]) -> dict[str, Any]:
        who = self._resolve(slot, message.get("cap"), charge_miss=True)
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
        member = message.get("grant") if "grant" in message else None
        if "grant" in message:
            why = grants.grant_member_refusal(request, member)
            if why is not None:
                log.info("qbroker: grant member refused on slot %d: %s", slot, why)
                return wire.refusal(rid, "grant-digest-mismatch")
        row: dict[str, Any] = {"format": REQUEST_ROW_FORMAT, "queue_id": self._store.queue_id,
                               "request": request.fields(), "session": who.session, "slot": slot_ref(slot), "kind": who.kind}
        if who.run is not None:
            row["run"] = dict(who.run)
        # a replay is answered before the clock and hold checks: those bound a NEW row, and a hook that re-files
        # after a slow trip must get the row it already has
        existing = self._store.read_request(request.id)
        if existing is None:
            now = self._clock()
            if abs(received - now) > RECEIVED_SKEW:
                return wire.refusal(rid, "request-clock")
            if expires <= now or expires - received > timedelta(seconds=MAX_HOLD):
                return wire.refusal(rid, "request-hold")
            granted = self._decide_grant(who, request, member, now) if member is not None else None
            refused = self._file(row, who, request, utc_stamp(now), granted=granted is not None)
            if refused is None:
                if granted is not None:
                    self._write_grant(who, request, granted, now)
                return wire.answer(rid, request_id=request.id, queue_id=self._store.queue_id)
            if refused != "request-exists":
                return wire.refusal(rid, refused)
            existing = self._store.read_request(request.id)
        if existing != row:
            return wire.refusal(rid, "request-exists")
        return wire.answer(rid, request_id=request.id, queue_id=self._store.queue_id)

    def _decide_grant(self, who: Resolved, request: ExternalGateRequest, member: Mapping[str, Any],
                      now: datetime) -> grants.Granted | None:
        """The grant when the signed contract covers this call, else ``None`` (the request is filed for the
        operator); the reason is logged."""
        try:
            snapshot = self._signers.current()
        except Exception as exc:   # noqa: BLE001 - an unreadable snapshot is a queued call, never a refusal
            log.info("qbroker: %s goes to the queue: the signer snapshot could not be read (%s)", request.id,
                     type(exc).__name__)
            return None
        got = grants.decide(kind=who.kind, run=who.run, request=request, paths=member["paths"],
                            directory=self._contracts, owner_uid=self._contracts_owner, snapshot=snapshot,
                            now=now, ssh_keygen=self._ssh_keygen)
        if isinstance(got, grants.Granted):
            return got
        log.info("qbroker: %s goes to the queue: %s", request.id, got.reason)
        return None

    def _write_grant(self, who: Resolved, request: ExternalGateRequest, granted: grants.Granted,
                     now: datetime) -> None:
        """The grant-allow row beside the request row just linked. A row that cannot be written leaves the request
        pending for the operator (it is entered as pending here, past the cap it was not checked against).

        Known edge (G, the L3 r1 triage): a disk that fills between the request row and this row lets a presumed
        grant enter the pending set past :data:`PENDING_PER_SESSION`. A full disk under ``L/queue`` is the machine's
        failure, and the broker is not reachable by an untrusted writer of that disk, so no reservation was added."""
        try:
            self._store.write_grant(grants.grant_row(request.id, granted, now))
            return
        except (OSError, ValueError):
            log.exception("qbroker: the grant row for %s could not be written; it waits for the operator", request.id)
        with self._mutex:
            self._pending.setdefault(who.session, {})[request.id] = request.expires_at

    def _file(self, row: dict[str, Any], who: Resolved, request: ExternalGateRequest, now: str, *,
              granted: bool = False) -> str | None:
        """Link ``row`` as a new row, charged to its session's pending cap and its quota; ``None`` when this call
        linked it, else the refusal name (``request-exists`` when another connection linked it first)."""
        # single flight per request id: a second filing waits for the first, then finds its row (or, when that
        # one failed, files it itself), so only the filing that links a row ever holds its charge
        while True:
            with self._mutex:
                flight = self._filing.get(request.id)
                if flight is None:
                    flight = self._filing[request.id] = threading.Event()
                    break
            flight.wait()
            if self._store.read_request(request.id) is not None:
                return "request-exists"
        try:
            # the row may have been linked between the caller's replay check and this flight
            if self._store.read_request(request.id) is not None:
                return "request-exists"
            return self._file_new(row, who, request, now, granted=granted)
        finally:
            with self._mutex:
                del self._filing[request.id]
            flight.set()

    def _file_new(self, row: dict[str, Any], who: Resolved, request: ExternalGateRequest, now: str, *,
                  granted: bool = False) -> str | None:
        """:meth:`_file` inside its flight: no other filing of ``request.id`` runs, and its row is not linked.
        ``granted``: the row is allowed by the contract grant, so it waits on no one: it is charged to the quota and
        neither checked against nor entered in the pending cap."""
        with self._mutex:
            held = dict(self._pending.get(who.session, {}))
        # pending = unexpired with no answer file: once the operator has written one, the row has had his
        # attention, whatever the file proves
        settled = {i for i, exp in held.items() if exp <= now or self._store.has_answer(i) or self._granted(i)}
        key = _quota_key(who.session, who.run)
        with self._mutex:
            mine = self._pending.setdefault(who.session, {})
            for i in settled:
                mine.pop(i, None)
            if not granted and len(mine) >= self._pending_cap:
                return "queue-busy"
            if self._counts.get(key, 0) >= self._quota:
                return "quota-exhausted"
            self._counts[key] = self._counts.get(key, 0) + 1
            if not granted:
                mine[request.id] = request.expires_at
        linked = False
        try:
            linked = self._store.write_request(row)
        except BaseException:
            # the link can land before the directory fsync raises: a row on disk keeps its charge
            linked = self._linked(row)
            raise
        finally:
            if not linked:                 # another process filed it first, or the write failed: no charge
                with self._mutex:
                    self._counts[key] -= 1
                    self._pending.get(who.session, {}).pop(request.id, None)
        return None if linked else "request-exists"

    def _linked(self, row: dict[str, Any]) -> bool:
        try:
            return self._store.read_request(row["request"]["id"]) == row
        except (ValueError, OSError):
            return False

    def _answer(self, slot: int, rid: int, message: Mapping[str, Any]) -> dict[str, Any]:
        if not self._take_token(slot):
            return wire.refusal(rid, "rate-limited")
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
        if row is None or row.get("session") != who.session or row.get("slot") != slot_ref(slot):
            return wire.refusal(rid, "no-such-request")
        request = request_of(row.get("request"))
        if request is None or row.get("queue_id") != self._store.queue_id:
            return wire.refusal(rid, "no-such-request")
        now = self._clock()
        # the operator's verified answer first, before the hold: one signed in time is his decision whenever it is
        # read, and it outranks the contract's grant (his deny wins). Only with none does the hold end the request,
        # and then the broker's own grant row answer (in the directory only the broker writes, read as its format)
        decision, answer_bad = self._answered(row, request_id, now)
        if decision is not None:
            return wire.answer(rid, decision=decision)
        if request.expires_at <= utc_stamp(now):
            return wire.refusal(rid, "request-expired")
        try:
            grant = self._store.read_grant(request_id)
        except (ValueError, OSError) as exc:
            log.warning("qbroker: grant row for %s refused: %s", request_id, exc)
            return wire.refusal(rid, "answer-refused")
        if grant is not None:
            why = grant_allow_refusal(row, grant, now)
            if why is not None:
                log.warning("qbroker: grant row for %s refused: %s", request_id, why)
                return wire.refusal(rid, "answer-refused")
            return wire.answer(rid, decision="allow")
        if answer_bad:
            return wire.refusal(rid, "answer-refused")
        return wire.answer(rid, pending=True)

    def _answered(self, row: Mapping[str, Any], request_id: str, now: datetime) -> tuple[str | None, bool]:
        """``(decision, False)`` for a verified answer row, ``(None, False)`` when there is none, ``(None, True)``
        when the row there is not authority (the reason is logged)."""
        try:
            got = self._store.read_answer_stamped(request_id)
        except (ValueError, OSError) as exc:
            log.warning("qbroker: answer row for %s refused: %s", request_id, exc)
            return None, True
        if got is None:
            return None, False
        answer, identity = got
        try:
            snapshot = self._signers.current()
        except Exception as exc:                      # a snapshot that cannot be read verifies nothing
            log.warning("qbroker: signers snapshot unreadable for %s: %s", request_id, exc)
            return None, True
        with self._mutex:
            hit = self._verdicts.get(request_id)
        # the verdict holds while the answer file and the snapshot are the ones it was reached on; it reads no clock
        # (the hold is the answer's signed_at), and every other input is the sealed row, which never changes
        if hit is not None and hit[0] == identity and hit[1] is snapshot:
            decision = hit[2]
        else:
            decision = self._verdict(row, request_id, answer, snapshot, now)
            with self._mutex:
                self._verdicts[request_id] = (identity, snapshot, decision)
                self._verdicts.move_to_end(request_id)
                while len(self._verdicts) > VERDICT_CACHE_MAX:
                    self._verdicts.popitem(last=False)
        return decision, decision is None

    def _verdict(self, row: Mapping[str, Any], request_id: str, answer: Mapping[str, Any],
                 snapshot: SignersSnapshot | None, now: datetime) -> str | None:
        """The verified decision ``answer`` carries, or ``None`` when it is not authority."""
        if set(answer) != _ANSWER_ROW_KEYS or answer["format"] != ANSWER_ROW_FORMAT:
            log.warning("qbroker: answer row for %s is not a %s row", request_id, ANSWER_ROW_FORMAT)
            return None
        why = answer_refusal(row, answer["payload"], answer["signature"], snapshot, now, ssh_keygen=self._ssh_keygen)
        if why is not None:
            log.warning("qbroker: answer for %s refused: %s", request_id, why)
            return None
        return answer["payload"]["decision"]
