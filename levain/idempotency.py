"""levain.idempotency — the at-most-once dedup keystore for irreversible governed actions.

A ``POST /action`` confirm gate is a FAT-FINGER guard, NOT an at-most-once guard: a replayed
``confirm: true`` (a tailnet retry of a non-idempotent POST, a proxy/browser auto-retry, a
double-action that slips the confirm) re-runs the handler. For ``send_inbox`` that is a harmless
duplicate message; for the FIRST irreversible verb (relay / consult) it is a double-send. This
store is the at-most-once gate an irreversible :class:`~levain.writes.ActionVerb` routes through:
a CLIENT-SUPPLIED ``idempotency_key`` is recorded with the request's content fingerprint and
deduped within a window, so a replay returns the ORIGINAL response WITHOUT re-firing.

It mirrors the vagus efferent gate's at-most-once primitive (``vagus.efferent.pending`` —
atomic ``claim()`` + a content-fingerprint integrity seal) but with the inverse state machine:
the pending store is a propose→resolve QUEUE (``claim`` removes-and-returns, exactly one of N
resolvers wins); this is a dedup KEYSTORE (a key persists for the window so a later replay still
finds it). Same locking discipline (an ``flock``-serialized read-modify-write + atomic tmp+replace
so a concurrent claim/finalize can't tear the file or lose a record).

**The READ POLARITY is INVERTED from the pending store — and that is load-bearing** (L2 HIGH-1).
The pending queue can read fail-SOFT (a corrupt file → ``[]`` → nothing to fire → SAFE: drop).
This keystore must NOT: ``[]`` here means "no record proves this key already fired" → a fresh
claim → a RE-FIRE. So the GATE read (:meth:`_read_strict`) fails CLOSED: a present-but-
untrustworthy file (bad JSON, a non-list, a non-dict element, or a record with a non-string key)
is a ``corrupt`` outcome → refuse (500), never a silent re-fire, and the corrupt file is NOT
overwritten. Only a genuinely MISSING file reads ``[]`` (legitimate first use).

The safe direction for an irreversible action governs the other failure modes too:
  - a fresh key is reserved (``in_flight``) BEFORE the handler fires; on success it is
    ``finalize``d to ``done`` with the recorded response;
  - a pre-side-effect refusal (the handler raised an ``EditError`` — input validation, by the
    ActionVerb contract BEFORE any side effect) ``release``s the key so a corrected retry can
    re-attempt (nothing fired);
  - an execution fault (any other exception — AMBIGUOUS: the send may have happened mid-fire)
    transitions the key to ``faulted`` (``mark_faulted``), which never expires and refuses a
    retry, so a replay does NOT re-fire an action that may have happened (at-most-once — a
    fault drops to "do not retry", never to "re-send"; the operator re-composes a fresh key);
  - a matching record that is present-but-UNPARSEABLE is ``corrupt`` → fail CLOSED (refuse).

Stdlib-only (``fcntl`` is POSIX — macOS/Linux, the stack's targets).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

__all__ = [
    "IdempotencyRecord",
    "IdempotencyStore",
    "ClaimOutcome",
    "StoreCorruptError",
    "request_fingerprint",
    "DEFAULT_WINDOW_S",
]

_log = logging.getLogger("levain.idempotency")

# The dedup window: how long a key dedupes after it was first claimed. Longer than any plausible
# retry interval (a tailnet/proxy/browser retry fires within seconds–minutes) but bounded so the
# keystore self-prunes. 24h is a generous safety margin; an operator who genuinely wants to re-send
# the SAME content after the window simply gets a fresh key (a new compose). A `faulted` record is
# EXEMPT (it never expires — an ambiguous reservation must not silently become re-fireable).
DEFAULT_WINDOW_S = 24 * 3600

_STATUSES = ("in_flight", "done", "faulted")


class StoreCorruptError(Exception):
    """The store file is present but UNTRUSTWORTHY (bad JSON, a non-list top level, a non-dict
    element, or a record with a non-string key). For an irreversible-action dedup keystore the
    safe direction is REFUSE, not the pending-queue's lenient skip: a corrupt store could be
    hiding a ``done`` record, so the gate fails CLOSED (a ``corrupt`` outcome → 500) rather than
    treat the file as empty and RE-FIRE. Distinct from a MISSING file (legitimate first use → [])."""


def request_fingerprint(verb: str, params: dict[str, Any]) -> str:
    """The content fingerprint over a governed action request — sha256 of canonical JSON of
    ``{verb, params}`` → 16 hex. Lets the store detect KEY REUSE: a replay carrying the same
    ``idempotency_key`` but a DIFFERENT ``(verb, params)`` is a client bug (a key reused for a
    new request) → a ``collision``, never a silent serve-of-the-old-result or fire-of-the-new.

    The fingerprint is REUSE-DETECTION only, never the dedup key (the client ``idempotency_key``
    is) — so 64 bits is ample (harm needs a client to reuse one key for two genuinely different
    requests AND those two to collide at 2⁻⁶⁴, and a reused key is already a client bug).

    Canonical JSON (sorted keys, tight separators), NOT a delimiter-join: a delimiter-join lets a
    field carrying the delimiter byte reshuffle into an equal hash input for a different field
    tuple — the verified collision class the vagus seal's L3 codex ship-gate caught. Canonical
    JSON encodes field boundaries + types unambiguously, closing it.

    Raises ``ValueError`` on a params shape that would fingerprint AMBIGUOUSLY (L3 codex LOW — the
    same ``json.dumps`` non-string-key class codex caught in the vagus seal): a non-string dict key
    (``json.dumps`` coerces ``{1:"x"}`` and ``{"1":"x"}`` to the SAME JSON → two different requests
    would share a fingerprint → a wrong replay/collision), or a non-finite float (``allow_nan=False``
    rejects NaN/Infinity, which are non-standard JSON another parser could canonicalize differently).
    Unreachable via the HTTP route (JSON parsing yields string keys), but ``apply_action`` is also a
    Python API; the caller (``_idempotency_claim``) maps this to a clean 400."""
    _assert_canonical_safe(params)
    canonical = json.dumps(
        {"verb": verb, "params": params},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _assert_canonical_safe(obj: Any) -> None:
    """Recursively reject anything that would canonicalize ambiguously: a non-string dict key
    (would collide ``{1:'x'}`` with ``{'1':'x'}``) or a non-finite float (NaN/Infinity)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"non-string dict key {k!r} — would fingerprint ambiguously")
            _assert_canonical_safe(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _assert_canonical_safe(v)
    elif isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):  # noqa: PLR0124
        raise ValueError("non-finite float (NaN/Infinity) — not canonical JSON")


def _require_str(d: dict[str, Any], key: str) -> str:
    v = d[key]
    if not isinstance(v, str):
        raise TypeError(f"{key} must be a string, got {type(v).__name__}")
    return v


@dataclass(frozen=True)
class IdempotencyRecord:
    """One reserved/completed/faulted idempotency key. ``status`` is ``in_flight`` between claim
    and resolution, ``done`` after a successful fire (carrying the ``result`` to replay), or
    ``faulted`` after an execution fault (ambiguous — never re-fired, never expired).
    ``fingerprint`` is the request content fingerprint (:func:`request_fingerprint`); ``created_at``
    (ISO) is the claim time — the window is measured from it (a ``faulted`` record is exempt)."""

    key: str
    fingerprint: str
    status: Literal["in_flight", "done", "faulted"]
    created_at: str
    result: dict[str, Any] | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "created_at": self.created_at,
            "result": self.result,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IdempotencyRecord":
        """Reconstruct from a stored record, STRICTLY. Raises ``KeyError``/``TypeError``/
        ``ValueError`` on a malformed record — for a record whose key MATCHES the one being claimed
        the store fails CLOSED (``corrupt``, never re-fire). ``status`` must be a known value;
        ``result`` must be a JSON object or null, and a ``done`` record MUST carry a dict result
        (a done-with-null can't be served → malformed → fail closed rather than re-fire)."""
        status = _require_str(d, "status")
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
        result = d.get("result")
        if result is not None and not isinstance(result, dict):
            raise TypeError(f"result must be a dict or null, got {type(result).__name__}")
        if status == "done" and not isinstance(result, dict):
            raise ValueError("a 'done' record must carry a dict result")
        return cls(
            key=_require_str(d, "key"),
            fingerprint=_require_str(d, "fingerprint"),
            status=status,  # type: ignore[arg-type]  # narrowed by the membership check above
            created_at=_require_str(d, "created_at"),
            result=result,
            completed_at=d.get("completed_at"),
        )


@dataclass(frozen=True)
class ClaimOutcome:
    """The result of :meth:`IdempotencyStore.claim_or_replay`.

    - ``fresh`` — the key was unseen (or its window expired); it is now reserved ``in_flight`` and
      the caller OWNS executing the action (then must ``finalize`` / ``release`` / ``mark_faulted``).
    - ``replay`` — the key is ``done``; ``result`` is the original response, returned WITHOUT
      re-firing.
    - ``in_flight`` — a concurrent request with this key is still executing; the caller must NOT
      fire. 409.
    - ``faulted`` — a prior attempt with this key faulted (it may have partially fired); the key is
      poisoned (never re-fired, never expired) — re-compose with a fresh key. 409.
    - ``collision`` — the key is present with a DIFFERENT fingerprint (reused for another
      request). 422.
    - ``corrupt`` — the store (or a record matching the key) is present but unreadable; fail
      CLOSED (refuse, never re-fire). 500.
    """

    kind: Literal["fresh", "replay", "in_flight", "faulted", "collision", "corrupt"]
    result: dict[str, Any] | None = None


class IdempotencyStore:
    """A mutable JSON-list dedup keystore, ``flock``-serialized + atomically written. The public
    surface is the at-most-once operations: :meth:`claim_or_replay` (the atomic gate),
    :meth:`finalize` (record the response after a successful fire), :meth:`release` (drop a
    reservation after a pre-fire refusal), :meth:`mark_faulted` (poison a reservation after an
    execution fault). Every mutation is a locked read-modify-write so a concurrent operation can't
    lose a record or tear the file."""

    def __init__(self, path: str | Path, *, window_s: int = DEFAULT_WINDOW_S) -> None:
        if window_s <= 0:
            # a non-positive window makes _expired always True → done records immediately
            # reclaimable → double-fire; refuse the footgun (L3 nemotron LOW).
            raise ValueError(f"window_s must be positive, got {window_s}")
        self.path = Path(path)
        self.window_s = window_s
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # --- locking -----------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on a sidecar lockfile for a read-modify-write. A sidecar
        (not the data file) so the lock survives the ``os.replace`` that swaps the data inode.
        Created on first use; never removed (a stable lock target). Each acquisition is a fresh
        ``os.open`` → an independent open-file-description, so the flock contends across this
        process's request threads (the ThreadingHTTPServer model) — NOT a per-FD record lock that
        wouldn't (L2-confirmed: the choice is load-bearing for same-process at-most-once)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # --- raw IO ------------------------------------------------------------------------
    def _read_strict(self) -> list[dict[str, Any]]:
        """The GATE read (claim path) — fail CLOSED. A MISSING file → ``[]`` (legitimate first
        use). A present-but-untrustworthy file raises :class:`StoreCorruptError`: an OS read error,
        bad JSON, a non-list top level, a non-dict element, OR any record with a non-string ``key``
        (a record whose key was mangled would no longer MATCH its own key → its done-ness would be
        invisible → a re-fire; refusing the whole store is the at-most-once-safe direction). The
        caller maps this to a ``corrupt`` outcome and does NOT overwrite the file."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as e:
            raise StoreCorruptError(f"unreadable ({type(e).__name__}): {e}") from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise StoreCorruptError(f"corrupt JSON: {e}") from e
        if not isinstance(data, list):
            raise StoreCorruptError(f"top level is {type(data).__name__}, not a list")
        seen: set[str] = set()
        for r in data:
            if not isinstance(r, dict):
                raise StoreCorruptError(f"a non-dict element ({type(r).__name__}) — untrustworthy")
            k = r.get("key")
            if not isinstance(k, str):
                raise StoreCorruptError("a record has a non-string key — untrustworthy")
            # A DUPLICATE key is a state this store never writes (claim drops the existing key before
            # appending; finalize/mark_faulted update in place) — so a duplicate means external
            # corruption/tampering. claim_or_replay stops at the FIRST match + the fresh path drops
            # ALL records for the key, so an expired-first duplicate could HIDE a live done/faulted
            # later one → re-fire. Refuse the whole store (fail closed) [L3 codex HIGH].
            if k in seen:
                raise StoreCorruptError(f"a duplicate key {k!r} — untrustworthy")
            seen.add(k)
        return data

    def _read_raw(self) -> list[dict[str, Any]]:
        """The DIAGNOSTIC / best-effort read (finalize, release, get) — fail-soft. A missing /
        corrupt file → ``[]`` with a warning; non-dict elements dropped. Safe for these callers:
        finalize + release only AFFECT the cache/reservation (the side effect already happened or
        never did), and a corrupt file there is caught fail-CLOSED by the NEXT claim's strict read."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as e:
            _log.warning("idempotency store: read failed (%s): %s — returning []", type(e).__name__, e)
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            _log.warning("idempotency store: corrupt JSON (%s) — returning []", e)
            return []
        if not isinstance(data, list):
            _log.warning("idempotency store: top level is %s, not a list — returning []",
                         type(data).__name__)
            return []
        return [r for r in data if isinstance(r, dict)]

    def _write_raw(self, records: list[dict[str, Any]]) -> None:
        """Atomically AND durably replace the file with ``records`` — a uuid-suffixed tmp in the
        same dir, fsync the bytes, then ``os.replace`` (atomic on POSIX), then best-effort fsync
        the dir so the rename itself survives a crash. DURABILITY matters for an at-most-once gate
        (L3 codex MED): a fresh reservation must survive a power loss between the claim write and
        the side effect firing, else a restart re-claims fresh → re-fire. Mirrors
        ``writes._atomic_write``. (macOS ``os.fsync`` is weaker than ``F_FULLFSYNC`` — same caveat
        the rest of the kernel carries.) Call only under ``_locked``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
        tmp = self.path.with_suffix(self.path.suffix + f".{uuid.uuid4().hex[:12]}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                view = memoryview(data)
                while view:                       # os.write may short-write (POSIX) — loop
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)           # never leave a partial tmp on failure
            raise
        try:                                      # fsync the dir so the rename is durable too
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass                                  # some platforms/filesystems refuse a dir fsync

    # --- window helpers ----------------------------------------------------------------
    @staticmethod
    def _aware(ts: str) -> datetime:
        """Parse an ISO timestamp to an AWARE datetime — a tz-naive value is assumed UTC (the
        store always writes ``datetime.now(timezone.utc)``; a hand-edited / cross-version naive
        value is normalized rather than left to raise on the aware/naive subtraction → which would
        permanently brick + never-prune that key [L2 LOW-2]). Raises ``ValueError`` on garbage."""
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    def _expired(self, created_at: str, now: str) -> bool | None:
        """True iff ``created_at`` is older than the window relative to ``now``. ``None`` only when
        a timestamp is genuinely unparseable (the caller treats that on a MATCHING record as
        ``corrupt`` — fail closed). A tz-naive value is normalized to UTC (does not brick)."""
        try:
            c = self._aware(created_at)
            n = self._aware(now)
        except (ValueError, TypeError):
            return None
        return (n - c).total_seconds() > self.window_s

    def _prune(self, records: list[dict[str, Any]], now: str) -> list[dict[str, Any]]:
        """Drop records whose window has expired (lazy GC, run on the write path). ONLY an expired
        ``done`` record is dropped. ``faulted`` (ambiguous) AND ``in_flight`` (UNRESOLVED) records
        are NEVER pruned — neither may silently age into a re-fireable state (L3 codex HIGH: an
        orphaned in_flight that's pruned would re-claim fresh → re-fire). A record with an
        UNPARSEABLE created_at is KEPT — never silently aged out; a matching query later fails
        closed on it."""
        kept: list[dict[str, Any]] = []
        for r in records:
            if r.get("status") != "done":      # keep faulted + in_flight + any odd status
                kept.append(r)
                continue
            ca = r.get("created_at")
            exp = self._expired(ca, now) if isinstance(ca, str) else None
            if exp is True:
                continue
            kept.append(r)
        return kept

    # --- public API --------------------------------------------------------------------
    def claim_or_replay(self, key: str, fingerprint: str, now: str) -> ClaimOutcome:
        """The atomic at-most-once gate. In ONE locked read-modify-write, classify ``key``:

        the GATE read fails CLOSED on a corrupt store (→ ``corrupt``); then find a record matching
        ``key`` — ``corrupt`` (unparseable), ``faulted`` (poisoned — never expires), ``collision``
        (different fingerprint), ``replay`` (``done``, in-window), or ``in_flight``; an EXPIRED
        non-faulted record is treated as absent. If absent (or only an expired record) → reserve a
        fresh ``in_flight`` record (pruning expired records in the same write) → ``fresh``.

        Exactly one of N concurrent callers with the same fresh key wins ``fresh``; the losers get
        ``in_flight``/``replay``. A replay/in_flight/faulted/collision/corrupt never (re-)fires."""
        with self._locked():
            try:
                records = self._read_strict()
            except StoreCorruptError as e:
                # fail CLOSED — a corrupt store could be hiding a `done` record; never re-fire,
                # and do NOT overwrite the file (preserve the forensic evidence).
                _log.error("idempotency store: %s — failing closed (refuse, never re-fire)", e)
                return ClaimOutcome("corrupt")
            for i, r in enumerate(records):
                if r.get("key") != key:
                    continue
                try:
                    rec = IdempotencyRecord.from_dict(r)
                except (KeyError, TypeError, ValueError) as e:
                    _log.warning("idempotency store: matching record %r is malformed (%s) — "
                                 "failing closed (corrupt), NOT re-firing", key, type(e).__name__)
                    return ClaimOutcome("corrupt")
                if rec.status == "faulted":
                    # poisoned (ambiguous prior fire) — never expires, fingerprint-agnostic refuse.
                    return ClaimOutcome("faulted")
                exp = self._expired(rec.created_at, now)
                if exp is None:
                    _log.warning("idempotency store: matching record %r has an unusable created_at "
                                 "(%r) — failing closed (corrupt)", key, rec.created_at)
                    return ClaimOutcome("corrupt")
                if rec.status == "in_flight":
                    # An in_flight record is UNRESOLVED — we do NOT know whether it fired. It must
                    # NEVER silently age into a fresh re-claim (L3 codex HIGH): a process death AFTER
                    # the side effect landed but BEFORE finalize/mark_faulted leaves in_flight, and a
                    # >window-old in_flight is exactly that orphan (a real handler resolves in
                    # seconds). Within the window → a live concurrent duplicate (409). Past it →
                    # orphaned/AMBIGUOUS → POISON it to `faulted` (under the lock) and refuse —
                    # same safe direction as a caught execution fault.
                    if exp is True:
                        records[i] = IdempotencyRecord(
                            key=key, fingerprint=rec.fingerprint, status="faulted",
                            created_at=rec.created_at, result=None, completed_at=now).to_dict()
                        self._write_raw(records)
                        return ClaimOutcome("faulted")
                    if rec.fingerprint != fingerprint:
                        return ClaimOutcome("collision")
                    return ClaimOutcome("in_flight")
                # status == done → window-bounded (the legit "re-send the same content after the
                # window" case re-claims fresh).
                if exp is True:
                    break  # window passed → fall through to a fresh re-claim (prune removes it)
                if rec.fingerprint != fingerprint:
                    return ClaimOutcome("collision")
                return ClaimOutcome("replay", result=rec.result)
            # not found (or only an expired match) → reserve fresh, pruning expired records.
            kept = [r for r in self._prune(records, now) if r.get("key") != key]
            kept.append(IdempotencyRecord(
                key=key, fingerprint=fingerprint, status="in_flight", created_at=now,
            ).to_dict())
            self._write_raw(kept)
            return ClaimOutcome("fresh")

    def finalize(self, key: str, result: dict[str, Any], now: str) -> bool:
        """Mark a reserved key ``done`` with the response to replay. Returns True iff a matching
        ``in_flight`` record was promoted. A no-op (False) when the key is absent (pruned / never
        claimed) or already resolved (``done``/``faulted``) — finalize is best-effort and
        idempotent: the action already fired + audited, so a failed/duplicate finalize must never
        change what happened (it only affects whether a FUTURE replay can be served the cache).

        Reads fail-CLOSED (``_read_strict``): a MUTATION must NOT silently "heal" a corrupt store by
        dropping records (which would erase the fail-closed signal + a different key's done-record
        → re-fire; L3 nemotron). On a corrupt store it raises ``StoreCorruptError`` — the caller
        catches it best-effort (the key stays in_flight → a replay 409s; the corrupt file is
        preserved)."""
        with self._locked():
            records = self._read_strict()
            changed = False
            for i, r in enumerate(records):
                if r.get("key") != key:
                    continue
                if r.get("status") == "in_flight":
                    records[i] = IdempotencyRecord(
                        key=key, fingerprint=str(r.get("fingerprint", "")), status="done",
                        created_at=str(r.get("created_at", now)), result=result, completed_at=now,
                    ).to_dict()
                    changed = True
                break
            if changed:
                self._write_raw(records)
            return changed

    def mark_faulted(self, key: str, now: str) -> bool:
        """Poison a reserved key after an EXECUTION FAULT — the AMBIGUOUS terminal (the side effect
        may have fired mid-flight). A ``faulted`` record never expires (it must not silently become
        re-fireable) and a retry of the same key is refused; the operator re-composes with a fresh
        key. Returns True iff an ``in_flight`` record was transitioned. Best-effort (mirrors
        ``finalize``): the fault already happened, so a failed mark only affects the retry response
        (worst case the kept ``in_flight`` blocks a retry the same way — also no re-fire). Reads
        fail-CLOSED (a mutation; raises ``StoreCorruptError`` on a corrupt store — the caller catches
        it; the record stays in_flight, which also refuses a re-fire)."""
        with self._locked():
            records = self._read_strict()
            changed = False
            for i, r in enumerate(records):
                if r.get("key") != key:
                    continue
                if r.get("status") == "in_flight":
                    records[i] = IdempotencyRecord(
                        key=key, fingerprint=str(r.get("fingerprint", "")), status="faulted",
                        created_at=str(r.get("created_at", now)), result=None, completed_at=now,
                    ).to_dict()
                    changed = True
                break
            if changed:
                self._write_raw(records)
            return changed

    def release(self, key: str) -> bool:
        """Drop a reservation (a pre-fire refusal — nothing fired, so a corrected retry should be
        able to re-attempt with the same key). Returns True iff a record was removed. Only removes
        an ``in_flight`` record: a ``done`` (already fired) or ``faulted`` (ambiguous) record must
        never be released, since that would let a replay re-fire an action that may have happened.
        Reads fail-CLOSED (a mutation; raises ``StoreCorruptError`` on a corrupt store — the caller
        catches it; the key stays in_flight, which 409s, the safe direction)."""
        with self._locked():
            records = self._read_strict()
            kept = [r for r in records
                    if not (r.get("key") == key and r.get("status") == "in_flight")]
            if len(kept) == len(records):
                return False
            self._write_raw(kept)
            return True

    def get(self, key: str) -> IdempotencyRecord | None:
        """Read a record by key (no lock — atomic-replace writes mean a read sees a whole
        old-or-new file). A malformed matching record → ``None`` (logged). For diagnostics/tests;
        the gate uses :meth:`claim_or_replay`."""
        for r in self._read_raw():
            if r.get("key") == key:
                try:
                    return IdempotencyRecord.from_dict(r)
                except (KeyError, TypeError, ValueError) as e:
                    _log.warning("idempotency store: malformed record %r (%s) — treating as absent",
                                 key, type(e).__name__)
                    return None
        return None
