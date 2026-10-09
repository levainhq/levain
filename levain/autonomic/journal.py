"""levain.autonomic.journal — the durable run journal: an unattended run's effects, at most once.

An unattended run is a sequence of EFFECTS (a send, a write, a payment). This journal is what lets
such a run stop for a human between two effects and resume later without doing anything twice. It
is the substrate for the per-effect gate in the autonomic-fold design (section 7): the gate decides
whether the next effect may run, and the journal makes that decision hold.

The model is durable execution's, reduced to the part Levain needs: every side effect is a
journaled unit with a stable id, and resuming a run replays the recorded results of the effects
that already ran instead of running them again. There is deliberately no mid-effect pause: a run
suspends BEFORE an effect, never inside one, because an in-flight Python call cannot be resumed.

The journal is the ONLY durable home of a decision about a run's effect. A hold is the pending
decision itself: one row carries the sealed record a person is asked about and, for a link of a
chain, the state that resumes the chain after the decision. Open pendings and paused chains are read
from the open holds, never written anywhere else, and a decision is one write-once update (a
rejection's cancel of the run is in the same transaction). The one way back: a person's approval that
does not verify where it is used reopens its hold (signed-at-the-point-of-use, below).

Identity is derived, never assigned: a run's id is a content address over the binding and the
exact triggering event (:func:`run_id_for`), so delivering the same event again resumes the same
run; an effect's id is its position in the run (``link-<i>``); a hold's id is derived from the
run and effect it guards (:func:`hold_id_for`), so proposing the same effect again finds the same
hold instead of opening a second one.

Six properties, each one a run that fails without it:

  - **hold-until-decided** — while a hold is open on a binding (undecided, and its run neither
    cancelled nor fenced: a dead run's hold can never fire, so it stops nothing), no effect of that binding runs
    WITHOUT a decision, in this run or a sibling run (the "sibling leak": a gate that pauses one
    branch while another branch's undecided effect runs during the pause). An effect whose own hold
    was approved runs: a person approved exactly those bytes;
  - **reject-cancels** — rejecting a hold cancels the run; none of its later effects run;
  - **dedup-on-replay** — an effect with a recorded result is never executed again; the record is
    returned instead;
  - **fence-on-cancel** — a fence on a binding (a pause, a revoke, a demotion) stops every run
    admitted under an older governance generation at its next effect;
  - **fence-on-reclassify** — an effect is admitted only if its risk FENCE (a digest of the risk inputs
    its rung was decided from: the caller computes it, :meth:`RunJournal.effect` takes it) equals the fence
    read again from those inputs inside the admission's transaction. A classification changed in any way
    after the rung was decided stops the effect (STALE: the caller decides it again); inputs that cannot be
    read stop it too (UNCLASSIFIED), and nothing is recorded;
  - **authorized-at-the-point-of-use** — an effect that runs under an approval (a person's, or the
    silence default's ``on-loop``) is admitted only if the caller's ``authorize`` finds that decision
    authority under the CURRENT fence. An approval refused there does not run: the hold REOPENS (undecided, its
    signature cleared) for a new decision. One whose check cannot run is HELD, the approval intact. The
    check runs outside the write transaction and counts only for the decision and fence it read.

And one rule that is not a property but follows from "at most once": an effect whose intent was
recorded and whose result was not (the process died mid-call, or the call raised) has an UNKNOWN
outcome. It is POISONED: never retried, surfaced for a human. A send that may have gone out must
not be sent again on a guess.

The lock is not held across the effect call itself (it cannot span the I/O), so a fence or hold
written while an effect is already inside its call stops the NEXT effect, not that one. At most one
effect per run is in flight, and it is journaled.

Storage: the store directory's SQLite database (:mod:`levain.autonomic.db`), shared with the binding
registry, so a binding's fence, a run's admission and a one-shot's claim commit in the same
transaction as the registry change that requires them. Every verb is one transaction. The effect
leases are files in the same directory. Stdlib only.
"""
from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.autonomic.db import AutonomicDB

__all__ = [
    "EffectStatus", "EffectOutcome", "HoldResult", "RunJournal", "RunRef", "JournalCorruptError",
    "JournalConflictError", "UNSIGNED_DECIDER",
    "run_id_for", "hold_id_for", "effect_digest", "needs_signature", "AUTHORIZED", "decision_key",
]

_log = logging.getLogger(__name__)


class JournalCorruptError(RuntimeError):
    """The store cannot be read or written (damaged, or locked past the busy timeout). Fail closed: a
    journal that cannot be read cannot prove an effect has not already run, so nothing proceeds."""


class JournalConflictError(JournalCorruptError):
    """A write the store's constraints refuse (a second hold with a pending id another hold has). A
    :class:`JournalCorruptError`, so every caller that fails closed on one fails closed on this; the
    name says what happened."""


class EffectStatus(str, enum.Enum):
    DONE = "done"            # executed now; ``result`` is what ``fn`` returned
    REPLAYED = "replayed"    # executed earlier; ``result`` is the recorded value; ``fn`` NOT called
    HELD = "held"            # a decision is open on this binding; ``fn`` NOT called
    APPROVED = "approved"    # (``hold`` only) this effect's hold is already approved; run it
    POISONED = "poisoned"    # intent recorded, outcome unknown; ``fn`` NOT called, needs a human
    IN_FLIGHT = "in_flight"  # another live process is inside this effect right now; ``fn`` NOT called
    FENCED = "fenced"        # the binding was fenced past this run's generation; ``fn`` NOT called
    BARRED = "barred"        # the registry no longer grants this run (corrupt, absent, not fireable)
    CANCELLED = "cancelled"  # the run was cancelled (a rejected hold); ``fn`` NOT called
    STALE = "stale"          # the risk inputs changed since the rung was decided; ``fn`` NOT called
    UNCLASSIFIED = "unclassified"   # the risk inputs could not be read at admission; ``fn`` NOT called
    REOPENED = "reopened"    # the approval did not verify at admission: the hold is open again; ``fn`` NOT called


@dataclass(frozen=True)
class EffectOutcome:
    status: EffectStatus
    result: Any = None
    hold_id: str | None = None
    # REPLAYED only: the receipt id noted for this effect, or None if no receipt was noted.
    receipt_id: str | None = None
    # APPROVED only: who decided the hold ("human" / "on-loop"), as recorded with the decision.
    decided_by: str | None = None
    # HELD from ``hold`` only: True iff this call opened the hold (False: it was already open).
    new_hold: bool = False
    # REOPENED / UNCLASSIFIED, and HELD by an admission's check: why.
    why: str | None = None

    @property
    def ran_now(self) -> bool:
        return self.status is EffectStatus.DONE


@dataclass(frozen=True)
class HoldResult:
    ok: bool
    reason: str
    # an approval written by this call: its :func:`decision_key`, which the caller hands to ``effect`` as
    # ``expect`` so the effect runs only under the decision it wrote
    decision: tuple | None = None


@dataclass(frozen=True)
class RunRef:
    """Which journaled run and effect an action belongs to. Carried on a request so the gate runs
    the effect through the journal; ``None`` on a request means a manual (human-present) action."""

    run_id: str
    effect_id: str
    # True for a link of a multi-link chain: its pending may be resolved only through the chain.
    chained: bool = False

    def __post_init__(self) -> None:
        for name in ("run_id", "effect_id"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v:
                raise ValueError(f"RunRef.{name} must be a non-empty string")
        if not isinstance(self.chained, bool):
            raise ValueError("RunRef.chained must be a bool")


def _canonical(obj: Any) -> str:
    """Canonical JSON for a content address, or ``ValueError``. Refuses anything that does not
    round-trip exactly (a non-string key, a tuple, a non-finite float): two different values must
    never share an address because the encoder coerced one into the other."""
    try:
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"not canonical JSON: {e}") from e
    if json.loads(text) != obj:
        raise ValueError("not canonical JSON: the value does not round-trip")
    return text


def run_id_for(binding_id: str, event: Any) -> str:
    """The run id for ``binding_id`` firing on ``event``: a content address, so the same event
    delivered again is the same run (resumed, its done effects replayed), and two different events
    are two runs. Raises ``ValueError`` on an event that is not canonical JSON."""
    text = _canonical({"binding_id": binding_id, "event": event})
    return "run-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# SQL over ``holds``: the hold's run is neither cancelled nor fenced. A hold of a dead run can never fire
# (its effect's barrier stops it), so it is no decision anyone owes and it stops nothing.
_RUN_LIVE = (
    "NOT EXISTS (SELECT 1 FROM cancels c WHERE c.run_id = holds.run_id) AND NOT EXISTS (SELECT 1 FROM "
    "runs r JOIN fences f ON f.binding_id = r.binding_id WHERE r.run_id = holds.run_id AND "
    "f.generation > r.generation)")
# An OPEN hold: undecided, of a live run. The one definition used by the open-decision list and by
# hold-until-decided.
_OPEN_HOLD = f"decided IS NULL AND {_RUN_LIVE}"


# The columns of a hold record, in :meth:`RunJournal._hold_dict`'s order.
_HOLD_COLUMNS = ("hold_id, binding_id, run_id, effect_id, digest, at, pending, chain, chained, decided, "
                 "decided_by, decided_posture, signer, signature, challenge, fence")
# A decider whose approval needs no signature: the silence default, decided by the gate itself.
UNSIGNED_DECIDER = "on-loop"


def needs_signature(by: str | None) -> bool:
    """Whether an approval by ``by`` is a person's, and so is authority only with a verified signature.
    Every decider but :data:`UNSIGNED_DECIDER` is (``"human"``, and also an absent or unknown label: a
    decision that does not say it was the silence default is not treated as one)."""
    return by != UNSIGNED_DECIDER


# What ``authorize`` returns for an approval that is authority at this admission. Anything else is
# ``"unavailable:<why>"`` (the check could not run: the approval stands, the effect is held) or a refusal
# (``"bad:<why>"``, or any other value: the hold reopens for a new decision).
AUTHORIZED = "ok"
# How many times an admission re-runs its check when the decision or the risk inputs changed between the
# check (made outside the write transaction) and the admission; after that the effect is held.
_ADMIT_TRIES = 3
_RETRY = object()


def decision_key(hold: dict[str, Any]) -> tuple:
    """The decision recorded on ``hold``: two decisions with one key are the same decision (a person's
    carries its own signature)."""
    return tuple(hold.get(k) for k in ("by", "decided_posture", "signer", "signature", "challenge", "fence"))


def hold_id_for(run_id: str, effect_id: str) -> str:
    """The one hold that can guard ``effect_id`` of ``run_id``."""
    return f"hold:{run_id}:{effect_id}"


def effect_digest(*, action_name: str, payload: str, context_id: str) -> str:
    """The digest of exactly what an effect will do: the bytes a decision approves."""
    text = _canonical({"action_name": action_name, "payload": payload, "context_id": context_id})
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def durable_fsync(fd: int) -> None:
    """Flush ``fd`` to stable storage. On macOS ``os.fsync`` only reaches the drive's cache, so a power
    loss can drop a write the caller already acted on; ``F_FULLFSYNC`` flushes the cache too."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass   # a filesystem without it (some network mounts): fall back to fsync
    os.fsync(fd)


def durable_replace(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` so that, once this returns, a power loss cannot bring
    back the old contents: the temp file is flushed to disk before the rename and the directory after
    it. The stores' at-most-once claims (a claimed pending, a claimed chain) rely on it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        durable_fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        durable_fsync(dfd)
    finally:
        os.close(dfd)


class RunJournal:
    """The durable journal for unattended runs, in the store directory's SQLite database (shared with
    the binding registry, see :mod:`levain.autonomic.db`). Every verb is one transaction."""

    def __init__(self, directory: Path | str) -> None:
        self.db = AutonomicDB(directory)
        self.directory = self.db.directory
        self._lease_dir = self.directory / "leases"

    @property
    def path(self) -> Path:
        """The database file."""
        return self.db.path

    # --- leases: who is inside an effect right now -------------------------------------------
    # An effect's owner holds an exclusive flock on its lease file from just BEFORE its intent is
    # committed until just AFTER its result (or unknown) is committed. The OS drops the lock when the
    # owner dies, so "intent, no result, lease not held" means the owner is gone: POISONED. A process
    # id would be wrong twice: a recycled pid reads a dead owner as alive, and a second thread of the
    # same process reads a live owner as dead.
    def _lease_path(self, run_id: str, effect_id: str) -> Path:
        name = hashlib.sha256(f"{run_id}\0{effect_id}".encode("utf-8")).hexdigest()[:32]
        return self._lease_dir / name

    def _take_lease(self, run_id: str, effect_id: str) -> int:
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lease_path(run_id, effect_id), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _drop_lease(self, run_id: str, effect_id: str, fd: int) -> None:
        """Release a lease. Never raises: it runs after the effect's outcome is committed, and a lease
        file left behind is only debris (an unlocked lease marks nothing in flight)."""
        try:
            self._lease_path(run_id, effect_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            _log.error("run journal: could not remove lease %s/%s (%s): %s", run_id, effect_id,
                       type(e).__name__, e)
        finally:
            os.close(fd)   # releases the flock

    def _lease_held(self, run_id: str, effect_id: str) -> bool:
        try:
            fd = os.open(self._lease_path(run_id, effect_id), os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    # --- transactions --------------------------------------------------------------------------
    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.db.write() as conn:
                yield conn
        except sqlite3.IntegrityError as exc:
            # a write the store's constraints refuse (two holds with one pending id): fail closed, named
            raise JournalConflictError(f"run journal {self.db.path}: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            # an unreadable or damaged store cannot prove an effect has not run: fail closed
            raise JournalCorruptError(f"run journal {self.db.path}: {exc}") from exc

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.db.read() as conn:
                yield conn
        except sqlite3.DatabaseError as exc:
            raise JournalCorruptError(f"run journal {self.db.path}: {exc}") from exc

    # --- generations (the fence) -------------------------------------------------------------
    @staticmethod
    def _generation_in(conn: sqlite3.Connection, binding_id: str) -> int:
        row = conn.execute("SELECT generation FROM fences WHERE binding_id = ?", (binding_id,)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _fence_in(conn: sqlite3.Connection, binding_id: str, generation: int) -> None:
        conn.execute("INSERT INTO fences (binding_id, generation) VALUES (?, ?) ON CONFLICT(binding_id) "
                     "DO UPDATE SET generation = MAX(generation, excluded.generation)", (binding_id, generation))

    @staticmethod
    def _start_in(conn: sqlite3.Connection, run_id: str, binding_id: str, generation: int) -> None:
        conn.execute("INSERT OR IGNORE INTO runs (run_id, binding_id, generation) VALUES (?, ?, ?)",
                     (run_id, binding_id, int(generation)))

    def generation(self, binding_id: str) -> int:
        """The binding's current governance generation: its fence (0 if never fenced)."""
        with self._read() as conn:
            return self._generation_in(conn, binding_id)

    def start(self, run_id: str, *, binding_id: str) -> None:
        """Admit a run under the binding's CURRENT governance generation, read in the same transaction.
        (The fire path admits through :meth:`BindingStore.admit`, which does this in the same
        transaction as its fireability check and a one-shot's claim.) Starting an existing run id again
        is a no-op: a resumed run keeps its original admission, so a run fenced once stays fenced."""
        with self._write() as conn:
            self._start_in(conn, run_id, binding_id, self._generation_in(conn, binding_id))

    def fence(self, binding_id: str, *, generation: int | None = None) -> int:
        """Every run of ``binding_id`` admitted under a generation BELOW the fence stops at its next
        effect. ``generation`` defaults to the current one plus one. (The binding store fences in the
        same transaction as the change that requires it.) Returns the fence's generation."""
        with self._write() as conn:
            gen = self._generation_in(conn, binding_id) + 1 if generation is None else int(generation)
            self._fence_in(conn, binding_id, gen)
            return max(gen, self._generation_in(conn, binding_id))

    def cancel(self, run_id: str, *, reason: str) -> None:
        """End a run: none of its later effects run. The gate cancels a run when it makes a terminal
        decision not to fire one of its effects (a kill, a refusal)."""
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO cancels (run_id, reason) VALUES (?, ?)", (run_id, reason))

    def cancel_unstarted(self, run_id: str, effect_id: str, *, reason: str) -> EffectOutcome | None:
        """End a run unless ``effect_id`` has started, in one transaction: the check and the cancel cannot
        be split by an effect that starts between them. Returns ``None`` when the run was cancelled, or
        the barrier that reports the started effect (done, in flight, unknown) and cancels nothing.
        ``KeyError`` if the run was never started."""
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise KeyError(f"run {run_id!r} was never started")
            if conn.execute("SELECT 1 FROM effects WHERE run_id = ? AND effect_id = ?",
                            (run_id, effect_id)).fetchone() is not None:
                return self._barrier(conn, run_id, effect_id, None)
            conn.execute("INSERT OR IGNORE INTO cancels (run_id, reason) VALUES (?, ?)", (run_id, reason))
            return None

    @property
    def store_id(self) -> str:
        """This store's identity (random, made with the store): every confirm challenge names it, so a
        signature given in one store is not authority in another."""
        value = self.db.meta("store_id")
        if not value:
            raise JournalCorruptError(f"run journal {self.db.path}: the store has no identity")
        return value

    # --- effects ---------------------------------------------------------------------------
    def _barrier(self, conn: sqlite3.Connection, run_id: str, effect_id: str,
                 digest: str | None) -> EffectOutcome | None:
        """The outcome that stops ``effect_id`` before any decision logic, or ``None``, read in the
        caller's transaction. The run is fenced if the binding's fence (committed in the same database as
        the registry change that required it) is past its admission."""
        run = conn.execute("SELECT binding_id, generation FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(f"run {run_id!r} was never started")
        eff = conn.execute("SELECT digest, state, result, receipt_id FROM effects WHERE run_id = ? "
                           "AND effect_id = ?", (run_id, effect_id)).fetchone()
        cancelled = conn.execute("SELECT 1 FROM cancels WHERE run_id = ?", (run_id,)).fetchone() is not None
        # a recorded result first: a replay runs nothing, so a cancel or fence after the effect does not
        # hide what already happened (and a receipt that never landed can still be written). A recorded
        # result for DIFFERENT bytes is not a replay of this effect: the run is cancelled.
        if eff is not None and eff[1] == "done":
            if digest is not None and eff[0] != digest:
                conn.execute("INSERT OR IGNORE INTO cancels (run_id, reason) VALUES (?, ?)",
                             (run_id, "replay_digest_changed"))
                return EffectOutcome(EffectStatus.CANCELLED)
            return EffectOutcome(EffectStatus.REPLAYED, json.loads(eff[2]), receipt_id=eff[3])
        if cancelled:
            return EffectOutcome(EffectStatus.CANCELLED)
        if self._generation_in(conn, run[0]) > int(run[1]):
            return EffectOutcome(EffectStatus.FENCED)
        if conn.execute("SELECT 1 FROM meta WHERE key = 'registry'").fetchone() is not None:
            # this store holds a binding registry: the run's grant must still stand, read in this same
            # transaction (a corrupt registry, an absent or no-longer-fireable binding stops it)
            from levain.autonomic.binding import registry_bars
            barred = registry_bars(conn, run[0], run_id)
            if barred is not None:
                return EffectOutcome(EffectStatus.BARRED, barred)
        if eff is not None and eff[1] == "unknown":
            return EffectOutcome(EffectStatus.POISONED)
        if eff is not None and eff[1] == "intent":
            if self._lease_held(run_id, effect_id):
                return EffectOutcome(EffectStatus.IN_FLIGHT)
            return EffectOutcome(EffectStatus.POISONED)
        return None

    def peek(self, run_id: str, effect_id: str, *, digest: str | None = None) -> EffectOutcome | None:
        """The barrier that would stop ``effect_id`` right now (cancelled, fenced, already done,
        poisoned, in flight), or ``None`` if nothing but a decision could. Runs nothing. A caller
        uses it to short-circuit a replay before deciding anything; :meth:`effect` re-checks in its
        own transaction."""
        with self._write() as conn:   # may record the cancel of a changed replay
            return self._barrier(conn, run_id, effect_id, digest)

    @staticmethod
    def _hold_row(conn: sqlite3.Connection, hold_id: str) -> tuple | None:
        return conn.execute(f"SELECT {_HOLD_COLUMNS} FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()

    @staticmethod
    def _hold_dict(row: tuple) -> dict[str, Any]:
        (hold_id, binding_id, run_id, effect_id, digest, at, pending, chain, chained, decided, by, rung,
         signer, signature, challenge, fence) = row
        return {"hold_id": hold_id, "binding_id": binding_id, "run_id": run_id, "effect_id": effect_id,
                "digest": digest, "at": at, "pending": json.loads(pending) if pending else None,
                "chain": json.loads(chain) if chain else None, "chained": bool(chained),
                "decided": None if decided is None else bool(decided), "by": by, "decided_posture": rung,
                "signer": signer, "signature": signature, "challenge": challenge, "fence": fence}

    def hold(self, run_id: str, effect_id: str, *, digest: str, pending: dict[str, Any],
             at: str | None = None, chain: dict[str, Any] | None = None,
             chained: bool = False) -> EffectOutcome:
        """Open (or find) the decision that guards ``effect_id``: the run suspends BEFORE the effect.

        The hold IS the pending decision: it carries the sealed ``pending`` record a person is asked
        about and, for a link of a chain (``chained``), the ``chain`` continuation that resumes the
        walk after the decision, in the same row, written in the same transaction.

        Returns HELD with the hold id (``new_hold`` True iff this call opened it; proposing the same
        effect again finds the open one), APPROVED if that hold was already approved (the decision was
        made and the effect has not run: run it with :meth:`effect`), or the barrier that stops the
        effect. A different ``digest`` from the one the open or approved hold carries means the bytes
        changed under the decision: the run is cancelled."""
        hold_id = hold_id_for(run_id, effect_id)
        with self._write() as conn:
            barrier = self._barrier(conn, run_id, effect_id, digest)
            if barrier is not None:
                return barrier
            row = self._hold_row(conn, hold_id)
            if row is not None:
                h = self._hold_dict(row)
                if h["digest"] != digest:
                    conn.execute("INSERT OR IGNORE INTO cancels (run_id, reason) VALUES (?, ?)",
                                 (run_id, "digest_changed"))
                    return EffectOutcome(EffectStatus.CANCELLED)
                if h["decided"] is None:
                    return EffectOutcome(EffectStatus.HELD, hold_id=hold_id)
                if h["decided"]:
                    return EffectOutcome(EffectStatus.APPROVED, hold_id=hold_id, decided_by=h["by"])
                return EffectOutcome(EffectStatus.CANCELLED)   # rejected (the run is cancelled too)
            binding_id = conn.execute("SELECT binding_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
            pending_id = pending.get("pending_id") if isinstance(pending, dict) else None
            conn.execute(
                "INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, at, pending, pending_id, "
                "chain, chained, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "(SELECT COALESCE(MAX(seq), 0) + 1 FROM holds))",
                (hold_id, run_id, effect_id, binding_id, digest, at,
                 json.dumps(pending, sort_keys=True) if pending else None,
                 pending_id if isinstance(pending_id, str) else None,
                 json.dumps(chain, sort_keys=True) if chain is not None else None, 1 if chained else 0))
            return EffectOutcome(EffectStatus.HELD, hold_id=hold_id, new_hold=True)

    def effect(self, run_id: str, effect_id: str, *, digest: str, fence: str,
               fence_now: Callable[[sqlite3.Connection], str], fn: Callable[[], Any],
               needs_decision: bool = False,
               authorize: Callable[[dict[str, Any], str], str] | None = None,
               expect: tuple | None = None) -> EffectOutcome:
        """Run one effect at most once.

        ``digest`` identifies exactly what the effect will do (the bytes a person approves). It is
        recorded with the intent and with any hold, and a decision must echo it.
        ``fence`` is the digest of the risk inputs the rung this effect runs at was decided from;
        ``fence_now`` reads those inputs again and digests them, inside the admission's transaction (the
        connection is passed so a registry read is made in it). A different fence is STALE, one that
        cannot be read (``fence_now`` raises) is UNCLASSIFIED: nothing runs or is recorded either way.
        ``needs_decision`` is the gate's verdict for this effect: True means the effect runs only
        under an APPROVED hold of its own (one is opened if there is none). An effect with no
        approved hold of its own is HELD while any hold on its binding is open.
        ``authorize(hold, fence_now)`` decides whether the approval recorded on this effect's own hold is
        authority NOW (whoever decided it, a person or the silence default): :data:`AUTHORIZED`, or
        ``"unavailable:<why>"`` when the check cannot run (the approval stands and the effect is HELD), or
        a refusal (the hold reopens: REOPENED). With no ``authorize`` every approval is refused. The check
        runs OUTSIDE the write transaction (it may be slow: a signature verifier), and the admission
        uses its answer only if the decision and the fence it read are unchanged in the transaction;
        otherwise it checks again (:data:`_ADMIT_TRIES` times, then HELD).
        ``expect`` (a :func:`decision_key`) is the decision the caller acted on: an approved hold now
        carrying another decision is HELD, nothing run."""
        if not isinstance(fence, str) or not fence:
            raise TypeError("fence must be a non-empty string")
        hold_id = hold_id_for(run_id, effect_id)
        for _ in range(_ADMIT_TRIES):
            checked = self._authorized(hold_id, fence_now, authorize)
            taken: list[int] = []   # the lease, once taken: released here if the admission does not commit
            try:
                with self._write() as conn:
                    admitted = self._admit_in(conn, run_id, effect_id, hold_id, digest, needs_decision, taken,
                                              fence, fence_now, checked, expect)
            except BaseException:
                for fd in taken:
                    self._drop_lease(run_id, effect_id, fd)
                raise
            if admitted is _RETRY:
                continue
            if isinstance(admitted, EffectOutcome):
                return admitted
            return self._run_effect(run_id, effect_id, fn, admitted)
        return EffectOutcome(EffectStatus.HELD, hold_id=hold_id, why="admission_raced")

    def _authorized(self, hold_id: str, fence_now: Callable[[sqlite3.Connection], str],
                    authorize: Callable[[dict[str, Any], str], str] | None) -> tuple | None:
        """``(decision_key, fence, verdict)`` for this effect's approved hold, checked outside any write
        transaction, or ``None`` when there is no approved hold or its fence cannot be read now."""
        with self._read() as conn:
            row = self._hold_row(conn, hold_id)
            own = self._hold_dict(row) if row is not None else None
            if own is None or own["decided"] is not True:
                return None
            try:
                live = fence_now(conn)
            except Exception:  # noqa: BLE001 — the admission reads it again and reports it
                return None
        if authorize is None:
            verdict = "bad:approval_not_verifiable_here"
        else:
            try:
                verdict = authorize(own, live)
            except Exception as e:  # noqa: BLE001 — a check that could not run grants nothing
                verdict = f"unavailable:authorize_raised:{type(e).__name__}"
            if not isinstance(verdict, str):
                verdict = "bad:authorize_returned_no_verdict"
        return decision_key(own), live, verdict

    def _admit_in(self, conn: sqlite3.Connection, run_id: str, effect_id: str, hold_id: str, digest: str,
                  needs_decision: bool, taken: list[int], fence: str,
                  fence_now: Callable[[sqlite3.Connection], str],
                  checked: tuple | None, expect: tuple | None) -> EffectOutcome | int | object:
        """The admission of an effect, in the caller's transaction: the barrier, the hold rules, and
        the intent (the lease is taken just before it and appended to ``taken``). Returns the outcome
        that stops the effect, the lease fd, or :data:`_RETRY` when ``checked`` (:meth:`_authorized`) was
        made for another decision or fence than the ones read here."""
        barrier = self._barrier(conn, run_id, effect_id, digest)
        if barrier is not None:
            return barrier
        binding_id = conn.execute("SELECT binding_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        row = self._hold_row(conn, hold_id)
        own = self._hold_dict(row) if row is not None else None
        approved = own is not None and own["decided"] is True
        if own is not None and own["decided"] is None:
            return EffectOutcome(EffectStatus.HELD, hold_id=hold_id)
        if own is not None and own["decided"] is False:
            return EffectOutcome(EffectStatus.CANCELLED)
        if needs_decision and not approved:
            conn.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
                         "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM holds))",
                         (hold_id, run_id, effect_id, binding_id, digest))
            return EffectOutcome(EffectStatus.HELD, hold_id=hold_id)
        if not approved:
            # hold-until-decided: an open hold anywhere on the binding stops an undecided effect,
            # whether the hold belongs to this run or a sibling.
            other = conn.execute(f"SELECT hold_id FROM holds WHERE binding_id = ? AND {_OPEN_HOLD} "
                                 "ORDER BY seq LIMIT 1", (binding_id,)).fetchone()
            if other is not None:
                return EffectOutcome(EffectStatus.HELD, hold_id=other[0])
        if approved and own is not None and own["digest"] != digest:
            # Approved bytes and the bytes about to run differ: what was approved is not this.
            conn.execute("INSERT OR IGNORE INTO cancels (run_id, reason) VALUES (?, ?)",
                         (run_id, "digest_changed"))
            return EffectOutcome(EffectStatus.CANCELLED)
        try:
            live = fence_now(conn)   # the risk inputs, read again at the point of use
            if not isinstance(live, str) or not live:
                raise TypeError(f"fence_now returned {type(live).__name__}, not a fence")
        except Exception as e:  # noqa: BLE001 — inputs that cannot be read admit nothing (and crash nothing)
            _log.warning("run journal: risk fence of %s/%s unreadable (%s): %s", run_id, effect_id,
                         type(e).__name__, e)
            return EffectOutcome(EffectStatus.UNCLASSIFIED, why=f"fence_unreadable:{type(e).__name__}")
        if approved and own is not None:
            # the approval is authority only as checked for THIS decision under THIS fence (the check ran
            # outside the transaction, see ``effect``); a change since then checks again
            key = decision_key(own)
            if expect is not None and key != expect:
                return EffectOutcome(EffectStatus.HELD, hold_id=hold_id, why="decision_changed")
            if checked is None or checked[0] != key or checked[1] != live:
                return _RETRY
            verdict = checked[2]
            if verdict.startswith("unavailable:"):
                return EffectOutcome(EffectStatus.HELD, hold_id=hold_id, why=verdict)
            if verdict != AUTHORIZED:
                self._reopen_in(conn, hold_id)
                return EffectOutcome(EffectStatus.REOPENED, hold_id=hold_id, why=verdict.removeprefix("bad:"))
        if live != fence:
            # the risk inputs changed after this effect's rung was decided: that rung is not the one they
            # give now, so nothing is admitted or recorded until it is decided again
            return EffectOutcome(EffectStatus.STALE)
        lease = self._take_lease(run_id, effect_id)   # before the intent commits: see "leases"
        taken.append(lease)
        conn.execute("INSERT INTO effects (run_id, effect_id, digest, pid, state) "
                     "VALUES (?, ?, ?, ?, 'intent')", (run_id, effect_id, digest, os.getpid()))
        return lease

    def _run_effect(self, run_id: str, effect_id: str, fn: Callable[[], Any], lease: int) -> EffectOutcome:
        """Run ``fn`` outside any transaction (the lease marks it in flight), then commit its result,
        or ``unknown`` if it raised. The lease is released last."""
        try:
            try:
                result = fn()
                encoded = json.dumps(result)
            except BaseException:
                with self._write() as conn:
                    conn.execute("UPDATE effects SET state = 'unknown' WHERE run_id = ? AND effect_id = ?",
                                 (run_id, effect_id))
                raise
            with self._write() as conn:
                conn.execute("UPDATE effects SET state = 'done', result = ? WHERE run_id = ? AND effect_id = ?",
                             (encoded, run_id, effect_id))
        finally:
            self._drop_lease(run_id, effect_id, lease)
        return EffectOutcome(EffectStatus.DONE, result)

    def note_receipt(self, run_id: str, effect_id: str, receipt_id: str) -> None:
        """Record that the receipt for this effect was persisted, so a replay does not write another."""
        with self._write() as conn:
            conn.execute("UPDATE effects SET receipt_id = ? WHERE run_id = ? AND effect_id = ?",
                         (receipt_id, run_id, effect_id))

    # --- decisions -------------------------------------------------------------------------
    @staticmethod
    def _reopen_in(conn: sqlite3.Connection, hold_id: str) -> None:
        """Make an approved hold undecided again, its signature cleared: it is an open decision once more
        (and stops its binding's undecided effects until someone decides it)."""
        conn.execute("UPDATE holds SET decided = NULL, decided_by = NULL, decided_posture = NULL, signer = NULL, "
                     "signature = NULL, challenge = NULL, fence = NULL WHERE hold_id = ? AND decided = 1",
                     (hold_id,))

    def decide(self, hold_id: str, *, approve: bool, digest: str, by: str | None = None,
               posture: str | None = None, signer: str | None = None, signature: str | None = None,
               challenge: str | None = None, fence: str | None = None) -> HoldResult:
        """Resolve a hold. ``digest`` must equal the one recorded with the hold (the decision is
        bound to what was shown). A hold decides once: this write-once update IS the claim, so of any
        number of resolvers exactly one decision counts (a person's approval that does not verify at its
        effect's admission reopens the hold, see :meth:`effect`). A rejection cancels the hold's run in
        the same transaction. ``by`` names the decider; ``posture`` names the rung the decision met, which
        can be above the rung the hold's pending was proposed at.

        A person's approval (:func:`needs_signature`) is stored with what makes it authority: the
        ``signer``, the SSHSIG ``signature``, the ``challenge`` it signed and the risk ``fence`` the
        challenge named. Without them it is refused here (``unsigned_approval``), and nothing is written;
        the signature is verified again where it is used, at the effect's admission."""
        if approve and needs_signature(by) and not (signer and signature and challenge and fence):
            return HoldResult(False, "unsigned_approval")
        with self._write() as conn:
            row = self._hold_row(conn, hold_id)
            if row is None:
                return HoldResult(False, "unknown_hold")
            h = self._hold_dict(row)
            if h["decided"] is not None:
                return HoldResult(False, "already_decided")
            if h["digest"] != digest:
                return HoldResult(False, "digest_mismatch")
            signed = approve and needs_signature(by)
            conn.execute("UPDATE holds SET decided = ?, decided_by = ?, decided_posture = ?, signer = ?, "
                         "signature = ?, challenge = ?, fence = ? WHERE hold_id = ? AND decided IS NULL",
                         (1 if approve else 0, by, posture, signer if signed else None,
                          signature if signed else None, challenge if signed else None,
                          fence if signed else None, hold_id))
            if not approve:
                conn.execute("INSERT OR IGNORE INTO cancels (run_id, reason) VALUES (?, ?)",
                             (h["run_id"], "rejected"))
                return HoldResult(True, "rejected")
            return HoldResult(True, "approved", decision=decision_key(self._hold_dict(self._hold_row(conn, hold_id))))

    def _holds(self, where: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(f"SELECT {_HOLD_COLUMNS} FROM holds {where} ORDER BY seq", args).fetchall()
        return [self._hold_dict(r) for r in rows]

    def open_holds(self) -> list[dict[str, Any]]:
        """Every OPEN hold (:data:`_OPEN_HOLD`), with its pending record and chain continuation: the
        OPEN DECISIONS. This is the only list of pending decisions for journaled runs; each one is also
        stopping its binding's undecided effects until someone decides it."""
        return self._holds(f"WHERE {_OPEN_HOLD}")

    def approved_unrun(self) -> list[dict[str, Any]]:
        """Every APPROVED hold whose effect has not started and whose run is neither cancelled nor
        fenced: a decision that stands and will run on the next delivery or resolve of its run."""
        return self._holds(
            "WHERE decided = 1 AND NOT EXISTS (SELECT 1 FROM effects e WHERE e.run_id = holds.run_id "
            f"AND e.effect_id = holds.effect_id) AND {_RUN_LIVE}")

    def get_hold(self, hold_id: str) -> dict[str, Any] | None:
        """The hold record (open or decided), or ``None``."""
        found = self._holds("WHERE hold_id = ?", (hold_id,))
        return found[0] if found else None

    def find_pending(self, pending_id: str) -> dict[str, Any] | None:
        """The hold whose pending record has ``pending_id`` (open or decided), or ``None``."""
        found = self._holds("WHERE pending_id = ?", (pending_id,))
        return found[0] if found else None

    def poisoned(self) -> list[tuple[str, str]]:
        """Every effect whose outcome is unknown and whose owner is gone: the list a human must look
        at. (An intent whose owner still holds its lease is in flight, not poisoned.)"""
        with self._write() as conn:   # the write lock: an owner cannot commit 'done' while this checks
            rows = conn.execute("SELECT run_id, effect_id, state FROM effects WHERE state != 'done'").fetchall()
            return sorted((r, e) for r, e, state in rows if state == "unknown" or not self._lease_held(r, e))
