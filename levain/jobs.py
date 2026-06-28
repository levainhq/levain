"""levain.jobs — the async-job runtime for I/O-BOUND governed actions (the propose→job→poll seam).

A ``POST /action`` handler runs INSIDE the server's request-gate slot (``apply_action`` calls it
inline). That is correct for a fast verb (``send_inbox`` / ``send_relay`` finish in ~1s), but a
verb whose work is I/O-bound for 30s–3min (the flow Bridge's ``consult`` — it spawns reviewer
subprocesses) cannot run there: it would pin a request thread for minutes and starve the cockpit.
The :class:`ActionVerb` docstring already names the contract — *"a long action must be async/job-
based, not a blocking call"*. This module is that async path:

  - **propose** — ``apply_action`` (for a ``job=True`` verb) creates a job record (``pending``) and
    submits the slow handler to this runtime's bounded executor, then returns a job HANDLE
    (``{job_id, status}``) IMMEDIATELY — the request thread is freed in milliseconds;
  - **job** — a worker thread claims ``pending``→``running`` (atomic, single-winner) and runs the
    handler OUT OF BAND, then writes ``done`` (with the result) or ``failed`` (with the error);
  - **poll** — ``GET /job.json?id=<job_id>`` reads the job's status/result (a repeatable read).

**The kernel/bridge split** (``dogfood_discriminator``): this whole module is GENERIC — it never
mentions ``consult``. The slow handler (run a ``consult`` subprocess) lives BRIDGE-side; the job
registry + bounded executor + the poll route are the kernel seam any future job-verb reuses.

**The :class:`JobStore` mirrors :mod:`levain.idempotency`'s discipline** (an ``flock``-serialized
read-modify-write + atomic tmp+replace+fsync), DELIBERATELY DUPLICATED rather than refactored into
a shared base: ``idempotency.py`` is shipped + live under ``send_relay``, and a sibling primitive
on a parallel build day earns a fresh, independently-reviewable module over a risky refactor of
live code (rule-of-three not yet hit). The lifecycles differ enough to keep separate anyway: the
idempotency store is a dedup KEYSTORE (a key persists a window so a replay finds it); this is a
JOB registry (records transition through a state machine, then prune by TTL).

**Read polarity (load-bearing — the safe direction for each caller):**
  - the **poll read** (:meth:`JobStore.read_status`) fails CLOSED: a present-but-untrustworthy
    store raises :class:`JobStoreCorruptError` → the route 500s, never a false ``unknown`` (which
    would make the operator re-propose → a duplicate EXPENSIVE job) nor a false ``done``;
  - **mutations** (create / claim / finish / sweep) fail CLOSED too (strict read): a mutation must
    not silently "heal" a corrupt store by dropping records (it could erase a live job's status);
  - only a genuinely MISSING file reads ``[]`` (legitimate first use).

**Crash-mid-run:** a fresh process has NO live worker threads, so any non-terminal record on disk
is an orphan. :meth:`JobStore.sweep` (run once when the runtime is built) marks every
``pending``/``running`` record ``failed`` ("interrupted") — the operator re-proposes (a fresh key)
to re-run. A live-but-hung job (a wedged subprocess that outlives its own timeout) is caught by the
LEASE: a record older than :data:`JOB_LEASE_S` from ``created_at`` reads as ``failed`` at poll and
is pruned on the next write. (The handler bounds its own I/O too — the lease is a backstop.)

Stdlib-only (``fcntl`` is POSIX — macOS/Linux, the stack's targets).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

__all__ = [
    "JobRecord",
    "JobStore",
    "JobRuntime",
    "JobStoreCorruptError",
    "JOB_LEASE_S",
    "RESULT_TTL_S",
    "DEFAULT_MAX_CONCURRENT",
]

_log = logging.getLogger("levain.jobs")

# A job (pending+running) older than this from its ``created_at`` is an ORPHAN (a crash-killed
# worker / a wedged subprocess that outlived its own timeout) → it reads as ``failed`` at poll and
# is pruned on the next write. Generously beyond a real consult's ~3min ceiling so a legitimately
# slow review is never falsely declared dead; a job that genuinely needs >10min is pathological.
JOB_LEASE_S = 600
# How long a TERMINAL (done/failed) result stays readable before it is pruned. MATCHED to the
# idempotency dedup window (24h) DELIBERATELY (L3 codex LOW): the idempotency store caches the
# propose HANDLE for 24h, so a replayed propose within that window replays the same ``job_id`` — if
# the RESULT were pruned sooner, that replay would poll ``unknown`` (a dead handle the operator
# can't re-run with the same key). Keeping the result as long as the handle keeps a replay
# resolvable. Affordable because the result text is CAPPED (the bridge consult handler truncates),
# so a day of terminal records is small. (A NON-terminal orphan still reaps at the short lease.)
RESULT_TTL_S = 24 * 3600
# Concurrent jobs cap. consult spawns reviewer subprocesses (codex/claude — heavy), so the runtime
# REJECTS (503, back-pressure) past this rather than queue an unbounded pile of expensive jobs. A
# distinct concern from the server's request_gate (which bounds request threads — a job does NOT
# hold one; that is the whole point of this module).
DEFAULT_MAX_CONCURRENT = 2

_STATUSES = ("pending", "running", "done", "failed")
_NON_TERMINAL = ("pending", "running")


class JobStoreCorruptError(Exception):
    """The job store file is present but UNTRUSTWORTHY (bad JSON, a non-list top level, a non-dict
    element, a record with a non-string ``job_id``, or a duplicate ``job_id``). The safe direction
    for a job registry is REFUSE, not a lenient skip: a corrupt store could be hiding a live or
    ``done`` record, so the poll read fails CLOSED (→ 500) rather than report a false ``unknown``
    (which would make the operator re-propose a duplicate expensive job). Distinct from a MISSING
    file (legitimate first use → [])."""


def _utcnow_iso() -> str:
    """The wall-clock now, ISO-8601, for the async worker (the store methods take ``now`` from the
    caller so tests can pin it; the worker has no caller, so it stamps its own time)."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class JobRecord:
    """One async job. ``status`` walks ``pending`` (created at propose, queued) → ``running`` (a
    worker claimed it) → ``done`` (handler returned a dict ``result``) | ``failed`` (handler raised,
    or swept at restart, or lease-expired — ``error`` carries the reason). ``created_at`` (ISO) is
    the propose time (the LEASE is measured from it); ``started_at`` / ``finished_at`` are stamped
    on the claim / terminal transitions."""

    job_id: str
    verb: str
    status: Literal["pending", "running", "done", "failed"]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "verb": self.verb,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobRecord":
        """Reconstruct STRICTLY. Raises ``KeyError``/``TypeError``/``ValueError`` on a malformed
        record — a matching malformed record fails the read CLOSED (corrupt), never a false status.
        ``status`` must be known; ``result`` must be a dict or null and a ``done`` record MUST carry
        a dict result (a done-with-null can't be served → malformed → fail closed)."""
        job_id = d.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        status = d.get("status")
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
        created_at = d.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("created_at must be a non-empty string")
        result = d.get("result")
        if result is not None and not isinstance(result, dict):
            raise TypeError(f"result must be a dict or null, got {type(result).__name__}")
        if status == "done" and not isinstance(result, dict):
            raise ValueError("a 'done' record must carry a dict result")
        verb = d.get("verb")
        return cls(
            job_id=job_id,
            verb=verb if isinstance(verb, str) else "",
            status=status,  # type: ignore[arg-type]  # narrowed by the membership check above
            created_at=created_at,
            started_at=d.get("started_at") if isinstance(d.get("started_at"), str) else None,
            finished_at=d.get("finished_at") if isinstance(d.get("finished_at"), str) else None,
            result=result,
            error=d.get("error") if isinstance(d.get("error"), str) else None,
        )


class JobStore:
    """A mutable JSON-list job registry, ``flock``-serialized + atomically written (the
    :mod:`levain.idempotency` discipline, duplicated — see the module docstring). The public
    surface is the state-machine transitions: :meth:`create` (propose → ``pending``),
    :meth:`claim_running` (a worker takes it ``running``, single-winner), :meth:`finish` (terminal),
    :meth:`read_status` (the poll — fail-closed), :meth:`sweep` (restart recovery). Every mutation
    is a locked read-modify-write so a concurrent transition can't lose a record or tear the file."""

    def __init__(
        self, path: str | Path, *, lease_s: int = JOB_LEASE_S, result_ttl_s: int = RESULT_TTL_S
    ) -> None:
        if lease_s <= 0:
            raise ValueError(f"lease_s must be positive, got {lease_s}")
        if result_ttl_s <= 0:
            raise ValueError(f"result_ttl_s must be positive, got {result_ttl_s}")
        self.path = Path(path)
        self.lease_s = lease_s
        self.result_ttl_s = result_ttl_s
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # --- locking -----------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on a sidecar lockfile for a read-modify-write — a sidecar
        (not the data file) so the lock survives the ``os.replace`` that swaps the data inode. Each
        acquisition is a fresh ``os.open`` → an independent open-file-description, so the flock
        contends across this process's request/worker threads (mirrors ``idempotency.py``)."""
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
        """Read the store, failing CLOSED. A MISSING file → ``[]`` (legitimate first use). A
        present-but-untrustworthy file raises :class:`JobStoreCorruptError`: an OS read error, bad
        JSON, a non-list top level, a non-dict element, a record with a non-string ``job_id``, OR a
        DUPLICATE ``job_id`` (a state this store never writes — create drops the id first; a
        duplicate means external corruption that could hide a live record behind a stale one)."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as e:
            raise JobStoreCorruptError(f"unreadable ({type(e).__name__}): {e}") from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise JobStoreCorruptError(f"corrupt JSON: {e}") from e
        if not isinstance(data, list):
            raise JobStoreCorruptError(f"top level is {type(data).__name__}, not a list")
        seen: set[str] = set()
        for r in data:
            if not isinstance(r, dict):
                raise JobStoreCorruptError(f"a non-dict element ({type(r).__name__}) — untrustworthy")
            jid = r.get("job_id")
            if not isinstance(jid, str) or not jid:
                raise JobStoreCorruptError("a record has a non-string job_id — untrustworthy")
            if jid in seen:
                raise JobStoreCorruptError(f"a duplicate job_id {jid!r} — untrustworthy")
            seen.add(jid)
        return data

    def _write_raw(self, records: list[dict[str, Any]]) -> None:
        """Atomically AND durably replace the file (uuid-tmp → fsync bytes → ``os.replace`` →
        best-effort dir fsync). Durability matters: a fresh ``pending`` record must survive a power
        loss between the propose write and the worker firing (else a restart's sweep can't recover
        it as an orphan — it would simply be gone). Mirrors ``idempotency._write_raw``. Call only
        under ``_locked``."""
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
        """Parse an ISO timestamp to an AWARE datetime — a tz-naive value is assumed UTC (the store
        always writes ``datetime.now(timezone.utc)``; a hand-edited/cross-version naive value is
        normalized rather than left to raise on the aware/naive subtraction). Raises on garbage."""
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    def _age_s(self, created_at: str, now: str) -> float | None:
        """Seconds between ``created_at`` and ``now``; ``None`` only if a timestamp is unparseable
        (the caller treats that on a non-terminal record as expired → fail to ``failed``, the safe
        direction — an un-aged orphan must not live forever)."""
        try:
            return (self._aware(now) - self._aware(created_at)).total_seconds()
        except (ValueError, TypeError):
            return None

    def _lease_expired(self, created_at: str, now: str) -> bool:
        age = self._age_s(created_at, now)
        return age is None or age > self.lease_s

    def _prune(self, records: list[dict[str, Any]], now: str) -> list[dict[str, Any]]:
        """Lazy GC on the write path. Drop a TERMINAL (done/failed) record whose result has aged
        past ``result_ttl_s``, AND a NON-TERMINAL (pending/running) record whose LEASE has expired
        (an orphan a crashed worker left behind, that no live thread will ever resolve). A record
        with an unparseable timestamp is treated as expired (never lives forever)."""
        kept: list[dict[str, Any]] = []
        for r in records:
            status = r.get("status")
            ts = r.get("finished_at") if status in ("done", "failed") else r.get("created_at")
            if not isinstance(ts, str):
                ts = r.get("created_at") if isinstance(r.get("created_at"), str) else None
            ttl = self.result_ttl_s if status in ("done", "failed") else self.lease_s
            age = self._age_s(ts, now) if isinstance(ts, str) else None
            if age is None or age > ttl:
                continue  # expired (or un-ageable) → drop
            kept.append(r)
        return kept

    # --- public API: state-machine transitions ----------------------------------------
    def create(self, job_id: str, verb: str, now: str) -> None:
        """Reserve ``job_id`` as ``pending`` (the propose write). Fails CLOSED on a corrupt store
        (raises :class:`JobStoreCorruptError` — the caller maps it to a 500 and RELEASES the
        idempotency key, since nothing fired). Prunes expired records in the same write. Raises
        ``ValueError`` if ``job_id`` already exists (the caller mints a fresh uuid, so a collision
        is a bug, never silently overwriting a live job)."""
        with self._locked():
            records = self._read_strict()
            if any(r.get("job_id") == job_id for r in records):
                raise ValueError(f"job_id {job_id!r} already exists")
            kept = self._prune(records, now)
            kept.append(JobRecord(job_id=job_id, verb=verb, status="pending", created_at=now).to_dict())
            self._write_raw(kept)

    def claim_running(self, job_id: str, now: str) -> bool:
        """Transition ``pending``→``running`` atomically (a worker starting). Returns True iff a
        ``pending`` record was claimed — exactly one of any concurrent claimers wins; a record
        already ``running``/terminal/missing → False (do NOT run). Fails CLOSED on a corrupt store
        (raises — the worker catches it + does not run; the poll then 500s)."""
        with self._locked():
            records = self._read_strict()
            for i, r in enumerate(records):
                if r.get("job_id") != job_id:
                    continue
                if r.get("status") != "pending":
                    return False
                rec = JobRecord.from_dict(r)
                records[i] = JobRecord(
                    job_id=rec.job_id, verb=rec.verb, status="running",
                    created_at=rec.created_at, started_at=now).to_dict()
                self._write_raw(records)
                return True
            return False

    def finish(self, job_id: str, status: str, result: dict[str, Any] | None,
               error: str | None, now: str) -> bool:
        """Terminal transition ``running``→``done``/``failed`` (the worker, after the handler).
        Returns True iff a ``running`` record was promoted. Best-effort + idempotent (the work
        already happened — a failed/duplicate finish only affects whether a future poll can read the
        result; the lease is the backstop). Fails CLOSED on a corrupt store (raises — the caller
        catches it best-effort; the kept ``running`` record then lease-expires to failed at poll)."""
        if status not in ("done", "failed"):
            raise ValueError(f"finish status must be done/failed, got {status!r}")
        with self._locked():
            records = self._read_strict()
            for i, r in enumerate(records):
                if r.get("job_id") != job_id:
                    continue
                if r.get("status") != "running":
                    return False
                rec = JobRecord.from_dict(r)
                records[i] = JobRecord(
                    job_id=rec.job_id, verb=rec.verb, status=status,  # type: ignore[arg-type]
                    created_at=rec.created_at, started_at=rec.started_at,
                    finished_at=now, result=result if status == "done" else None,
                    error=error).to_dict()
                self._write_raw(records)
                return True
            return False

    def read_status(self, job_id: str, now: str) -> dict[str, Any]:
        """The POLL read — fail-CLOSED. Returns ``{job_id, status, ...}`` for a known job, or
        ``{job_id, status:"unknown"}`` for a missing/pruned id. Raises :class:`JobStoreCorruptError`
        on a corrupt store (the route 500s — never a false ``unknown`` that would trigger a
        duplicate re-propose). A non-terminal record past its LEASE reads as ``failed``
        ("interrupted") WITHOUT persisting (a pure read; the next write's prune collects it)."""
        records = self._read_strict()  # fail-closed; no lock (atomic-replace ⇒ whole old-or-new file)
        for r in records:
            if r.get("job_id") != job_id:
                continue
            rec = JobRecord.from_dict(r)  # a matching malformed record → raises → corrupt (500)
            if rec.status in _NON_TERMINAL and self._lease_expired(rec.created_at, now):
                return {"job_id": job_id, "status": "failed",
                        "error": rec.error or "interrupted"}
            out: dict[str, Any] = {"job_id": job_id, "status": rec.status}
            if rec.status == "done":
                out["result"] = rec.result
            elif rec.status == "failed":
                out["error"] = rec.error or "failed"
            return out
        return {"job_id": job_id, "status": "unknown"}

    def sweep(self, now: str) -> int:
        """Restart recovery: mark every non-terminal (``pending``/``running``) record ``failed``
        ("interrupted"). Run ONCE when the runtime is built — a fresh process has no live worker
        threads, so any non-terminal record is a crash orphan. Returns the count swept. Best-effort:
        a corrupt store can't be safely rewritten, so it is LEFT intact (logged) — the poll then
        fails CLOSED on it, which is the safe direction."""
        try:
            with self._locked():
                records = self._read_strict()
                swept = 0
                for i, r in enumerate(records):
                    if r.get("status") in _NON_TERMINAL:
                        try:
                            rec = JobRecord.from_dict(r)
                        except (KeyError, TypeError, ValueError):
                            continue  # a malformed non-terminal record — leave it; poll fails closed
                        records[i] = JobRecord(
                            job_id=rec.job_id, verb=rec.verb, status="failed",
                            created_at=rec.created_at, started_at=rec.started_at,
                            finished_at=now, error="interrupted by restart").to_dict()
                        swept += 1
                if swept:
                    self._write_raw(records)
                return swept
        except JobStoreCorruptError as e:
            _log.error("job store: sweep skipped — store is corrupt (%s); poll fails closed", e)
            return 0


class JobRuntime:
    """The async executor over a :class:`JobStore`: a bounded set of daemon worker threads + the
    store. :meth:`submit` accepts a job iff a slot is free (else False → the caller 503s, back-
    pressure — NO unbounded pile of expensive jobs); the worker claims ``running``, runs the slow
    handler, writes the terminal record, fires the completion callback, and releases the slot. The
    workers are DAEMON threads: a process exit kills a running job mid-flight, which is exactly the
    crash case :meth:`JobStore.sweep` recovers at the next start.

    CONCURRENCY-SLOT CONTRACT (L3 codex+nemotron MED — a real constraint, not a watchdog): a slot is
    freed ONLY when the worker thread RETURNS (the ``finally`` release). A Python thread can't be
    safely force-killed (a kernel watchdog that "reclaimed" a slot from a still-running thread would
    over-subscribe — and a later real release would raise on the BoundedSemaphore), so a handler
    that BLOCKS FOREVER would hold its slot forever. The handler therefore MUST bound its own I/O
    (the :class:`~levain.writes.ActionVerb` contract). The LEASE (:data:`JOB_LEASE_S`) makes the
    job READ as failed at poll, but it does NOT reclaim the live slot — the handler's own timeout
    does. The flow ``run_consult`` complies: its subprocess timeout (300s) is strictly under the
    600s lease, so the worker always returns + frees its slot well before the lease declares the job
    dead. (A handler's subprocess timeout kills its DIRECT child; if that child spawns its own
    detached subprocesses, those self-terminate via their own timeouts — consult.py's per-agent
    perl-alarm. An operational note, not a slot leak: the worker still returns.)"""

    def __init__(self, store: JobStore, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> None:
        if max_concurrent <= 0:
            raise ValueError(f"max_concurrent must be positive, got {max_concurrent}")
        self.store = store
        self.max_concurrent = max_concurrent
        self._slots = threading.BoundedSemaphore(max_concurrent)

    def submit(
        self,
        job_id: str,
        verb: str,
        work: Callable[[], Any],
        on_finish: Callable[[str, dict[str, Any] | None, str | None], None],
        now: str,
    ) -> bool:
        """Accept a job: reserve a concurrency slot (False if full → back-pressure, NO record
        written), write the ``pending`` record, and start a daemon worker. ``work`` is the slow
        handler call (returns a dict result, may raise); ``on_finish(status, result, error)`` is a
        best-effort completion hook the caller uses to write the audit receipt. Re-raises a
        :class:`JobStoreCorruptError` from :meth:`JobStore.create` AFTER releasing the slot (the
        caller maps it to a 500 + releases the idempotency key — nothing fired)."""
        if not self._slots.acquire(blocking=False):
            return False
        try:
            self.store.create(job_id, verb, now)
            # Thread.start() is INSIDE the guard (L3 codex+nemotron converged HIGH): if it raises
            # (OOM / a thread-creation limit) AFTER create succeeds, the slot must be released or it
            # leaks permanently — two such failures would wedge the runtime at 503 until restart.
            # The pending record it leaves is reaped by the lease / sweep / prune (it never ran).
            t = threading.Thread(
                target=self._run, args=(job_id, work, on_finish),
                name=f"levain-job-{job_id[:8]}", daemon=True)
            t.start()
        except BaseException:
            self._slots.release()  # nothing is running — give the slot back, then propagate
            raise
        return True

    def _run(
        self,
        job_id: str,
        work: Callable[[], Any],
        on_finish: Callable[[str, dict[str, Any] | None, str | None], None],
    ) -> None:
        """The worker body (a daemon thread). Claim ``pending``→``running`` (don't run if the claim
        fails — already terminal / corrupt), run the handler, write the terminal record + fire the
        audit hook, and ALWAYS release the concurrency slot."""
        try:
            try:
                claimed = self.store.claim_running(job_id, _utcnow_iso())
            except Exception as e:  # noqa: BLE001 — a corrupt store etc.; do not run on an unclaimable job
                _log.error("job %s: claim failed (%s) — not running", job_id, type(e).__name__)
                return
            if not claimed:
                _log.warning("job %s: not claimable (already terminal/missing) — not running", job_id)
                return
            status: str = "done"
            result: dict[str, Any] | None = None
            error: str | None = None
            try:
                out = work()
                if isinstance(out, dict):
                    result = out
                else:
                    status, error = "failed", "bad_handler_result"
            except Exception as e:  # noqa: BLE001 — any handler fault → the job FAILS (surfaced via poll)
                # Carry a BOUNDED message (not just the type) so the operator's poll shows WHY it
                # failed (a timeout / a bad-input EditError message / a subprocess exit), not a bare
                # "RuntimeError". Truncated — the error renders in the cockpit + the audit detail.
                status, error = "failed", f"{type(e).__name__}: {e}"[:300]
                _log.warning("job %s: handler raised %s", job_id, type(e).__name__)
            try:
                self.store.finish(job_id, status, result, error, _utcnow_iso())
            except Exception as e:  # noqa: BLE001 — best-effort; the lease backstops a lost finish
                _log.error("job %s: finish write failed (%s); poll will lease-expire it",
                           job_id, type(e).__name__)
            try:
                on_finish(status, result, error)
            except Exception as e:  # noqa: BLE001 — the audit hook is best-effort, never fails the job
                _log.warning("job %s: on_finish hook raised %s", job_id, type(e).__name__)
        finally:
            self._slots.release()
