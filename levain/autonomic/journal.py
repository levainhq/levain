"""levain.autonomic.journal — the durable run journal: an unattended run's effects, at most once.

An unattended run is a sequence of EFFECTS (a send, a write, a payment). This journal is what lets
such a run stop for a human between two effects and resume later without doing anything twice. It
is the substrate for the per-effect gate in the autonomic-fold design (section 7): the gate decides
whether the next effect may run, and the journal makes that decision hold.

The model is durable execution's, reduced to the part Levain needs: every side effect is a
journaled unit with a stable id, and resuming a run replays the recorded results of the effects
that already ran instead of running them again. There is deliberately no mid-effect pause: a run
suspends BEFORE an effect, never inside one, because an in-flight Python call cannot be resumed.

Identity is derived, never assigned: a run's id is a content address over the binding and the
exact triggering event (:func:`run_id_for`), so delivering the same event again resumes the same
run; an effect's id is its position in the run (``link-<i>``); a hold's id is derived from the
run and effect it guards (:func:`hold_id_for`), so proposing the same effect again finds the same
hold instead of opening a second one.

Four properties, each one a run that fails without it:

  - **hold-until-decided** — while a hold is open on a binding, no effect of that binding runs
    WITHOUT a decision, in this run or a sibling run (the "sibling leak": a gate that pauses one
    branch while another branch's undecided effect runs during the pause). An effect whose own hold
    was approved runs: a person approved exactly those bytes;
  - **reject-cancels** — rejecting a hold cancels the run; none of its later effects run;
  - **dedup-on-replay** — an effect with a recorded result is never executed again; the record is
    returned instead;
  - **fence-on-cancel** — a fence on a binding (a pause, a revoke, a demotion) stops every run
    admitted under an older governance generation at its next effect.

And one rule that is not a property but follows from "at most once": an effect whose intent was
recorded and whose result was not (the process died mid-call, or the call raised) has an UNKNOWN
outcome. It is POISONED: never retried, surfaced for a human. A send that may have gone out must
not be sent again on a guess.

The lock is not held across the effect call itself (it cannot span the I/O), so a fence or hold
written while an effect is already inside its call stops the NEXT effect, not that one. At most one
effect per run is in flight, and it is journaled.

Storage: one append-only JSONL file, every read-check-write under one ``flock`` on a sidecar lock
file, every append flushed and fsynced. State is derived by replaying the file. Stdlib only.
"""
from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "EffectStatus", "EffectOutcome", "HoldResult", "RunJournal", "RunRef", "JournalCorruptError",
    "run_id_for", "hold_id_for", "effect_digest",
]


class JournalCorruptError(RuntimeError):
    """The journal file cannot be read. Fail closed: an unreadable journal cannot prove an effect
    has not already run, so nothing proceeds until a human looks at it."""


class EffectStatus(str, enum.Enum):
    DONE = "done"            # executed now; ``result`` is what ``fn`` returned
    REPLAYED = "replayed"    # executed earlier; ``result`` is the recorded value; ``fn`` NOT called
    HELD = "held"            # a decision is open on this binding; ``fn`` NOT called
    APPROVED = "approved"    # (``hold`` only) this effect's hold is already approved; run it
    POISONED = "poisoned"    # intent recorded, outcome unknown; ``fn`` NOT called, needs a human
    IN_FLIGHT = "in_flight"  # another live process is inside this effect right now; ``fn`` NOT called
    FENCED = "fenced"        # the binding was fenced past this run's generation; ``fn`` NOT called
    CANCELLED = "cancelled"  # the run was cancelled (a rejected hold); ``fn`` NOT called


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

    @property
    def ran_now(self) -> bool:
        return self.status is EffectStatus.DONE


@dataclass(frozen=True)
class HoldResult:
    ok: bool
    reason: str


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


def hold_id_for(run_id: str, effect_id: str) -> str:
    """The one hold that can guard ``effect_id`` of ``run_id``."""
    return f"hold:{run_id}:{effect_id}"


def effect_digest(*, action_name: str, payload: str, context_id: str) -> str:
    """The digest of exactly what an effect will do: the bytes a decision approves."""
    text = _canonical({"action_name": action_name, "payload": payload, "context_id": context_id})
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _State:
    runs: dict[str, dict[str, Any]]
    intents: dict[tuple[str, str], dict[str, Any]]
    results: dict[tuple[str, str], dict[str, Any]]
    unknown: set[tuple[str, str]]
    holds: dict[str, dict[str, Any]]
    fences: dict[str, int]
    cancelled: set[str]
    receipts: dict[tuple[str, str], str]


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
    """The durable journal for unattended runs. One file per journal; many runs and bindings."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._lease_dir = self._path.with_name(self._path.name + ".leases")

    # --- leases: who is inside an effect right now -------------------------------------------
    # An effect's owner holds an exclusive flock on its lease file from just BEFORE its intent is
    # appended until just AFTER its result (or unknown) is appended. The OS drops the lock when the
    # owner dies, so "intent, no result, lease not held" means the owner is gone: POISONED. A process
    # id would be wrong twice: a recycled pid reads a dead owner as alive, and a second thread of the
    # same process reads a live owner as dead.
    def _lease_path(self, run_id: str, effect_id: str) -> Path:
        name = hashlib.sha256(f"{run_id}\0{effect_id}".encode("utf-8")).hexdigest()[:32]
        return self._lease_dir / name

    def _take_lease(self, run_id: str, effect_id: str) -> int:
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lease_path(run_id, effect_id), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _drop_lease(self, run_id: str, effect_id: str, fd: int) -> None:
        try:
            self._lease_path(run_id, effect_id).unlink()
        except FileNotFoundError:
            pass
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

    @property
    def path(self) -> Path:
        return self._path

    # --- storage ---------------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        existed = self._path.exists()
        if existed:
            self._drop_torn_tail()
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            durable_fsync(f.fileno())
        if not existed:
            dfd = os.open(self._path.parent, os.O_RDONLY)
            try:
                durable_fsync(dfd)
            finally:
                os.close(dfd)

    def _drop_torn_tail(self) -> None:
        """Truncate a final line with no newline: an append the process died inside, which never
        committed. Without this the next append would glue onto it and corrupt a committed line."""
        with open(self._path, "rb+") as f:
            data = f.read()
            if data and not data.endswith(b"\n"):
                f.truncate(data.rfind(b"\n") + 1)
                f.flush()
                durable_fsync(f.fileno())

    def _state(self) -> _State:
        st = _State({}, {}, {}, set(), {}, {}, set(), {})
        if not self._path.exists():
            return st
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalCorruptError(f"cannot read run journal {self._path}: {exc}") from exc
        lines = text.split("\n")
        for n, line in enumerate(lines, start=1):
            if not line:
                continue
            try:
                r = json.loads(line)
                t = r["t"]
            except (ValueError, KeyError, TypeError) as exc:
                if n == len(lines) and not text.endswith("\n"):
                    break  # a torn final append (crash mid-write) never committed; ignore it
                raise JournalCorruptError(f"run journal {self._path} line {n} is not a record") from exc
            try:
                self._apply(st, t, r)
            except (KeyError, TypeError) as exc:
                raise JournalCorruptError(f"run journal {self._path} line {n}: malformed {t!r} record") from exc
        return st

    def _apply(self, st: _State, t: Any, r: dict[str, Any]) -> None:
        if t == "run":
            st.runs[r["run_id"]] = r
        elif t == "intent":
            st.intents[(r["run_id"], r["effect_id"])] = r
        elif t == "result":
            st.results[(r["run_id"], r["effect_id"])] = r
        elif t == "unknown":
            st.unknown.add((r["run_id"], r["effect_id"]))
        elif t == "hold":
            st.holds.setdefault(r["hold_id"], dict(r, decided=None, by=None))
        elif t == "decide":
            if r["hold_id"] in st.holds and st.holds[r["hold_id"]]["decided"] is None:
                st.holds[r["hold_id"]]["decided"] = r["approve"]
                st.holds[r["hold_id"]]["by"] = r.get("by")
        elif t == "withdraw":
            h = st.holds.get(r["hold_id"])
            if h is not None and h["decided"] is None:
                del st.holds[r["hold_id"]]   # closed undecided; a later hold() opens it afresh
        elif t == "fence":
            st.fences[r["binding_id"]] = max(st.fences.get(r["binding_id"], 0), int(r["generation"]))
        elif t == "cancel":
            st.cancelled.add(r["run_id"])
        elif t == "receipt":
            st.receipts[(r["run_id"], r["effect_id"])] = r["receipt_id"]
        else:
            raise KeyError(f"unknown record {t!r}")

    # --- runs ------------------------------------------------------------------------------
    def next_generation(self, binding_id: str) -> int:
        """A generation above every fence AND every run admission this journal has for the binding: a
        record created at it (a removed grant added again) fences every run admitted before it."""
        with self._locked():
            st = self._state()
            seen = [st.fences.get(binding_id, 0)]
            seen += [r["generation"] for r in st.runs.values() if r["binding_id"] == binding_id]
            return max(seen) + 1

    def generation(self, binding_id: str) -> int:
        """The binding's current governance generation: the highest fence written for it (0 if none)."""
        with self._locked():
            return self._state().fences.get(binding_id, 0)

    def start(self, run_id: str, *, binding_id: str, generation: int) -> None:
        """Admit a run under the binding's governance ``generation``, which the caller reads from the
        authority that fences the binding (:meth:`BindingStore.admit` reads it under the store lock).
        Starting an existing run id again is a no-op (a resumed run keeps its original admission, so a
        run fenced once stays fenced)."""
        with self._locked():
            if run_id in self._state().runs:
                return
            self._append({"t": "run", "run_id": run_id, "binding_id": binding_id,
                          "generation": int(generation)})

    def fence(self, binding_id: str, *, generation: int | None = None) -> int:
        """Every run of ``binding_id`` admitted under a generation BELOW the fence stops at its next
        effect. ``generation`` defaults to the current generation plus one. Fence a binding on every
        governance change that must stop the runs already admitted under it (a store given this
        journal fences on its own verbs). Returns the fence's generation."""
        with self._locked():
            current = self._state().fences.get(binding_id, 0)
            gen = current + 1 if generation is None else int(generation)
            self._append({"t": "fence", "binding_id": binding_id, "generation": gen})
            return gen

    def cancel(self, run_id: str, *, reason: str) -> None:
        """End a run: none of its later effects run. The gate cancels a run when it makes a terminal
        decision not to fire one of its effects (a kill, a refusal), so delivering the event again
        cannot reach a different decision for the same effect."""
        with self._locked():
            if run_id not in self._state().cancelled:
                self._append({"t": "cancel", "run_id": run_id, "reason": reason})

    # --- effects ---------------------------------------------------------------------------
    def _barrier(self, st: _State, run_id: str, effect_id: str,
                 current_generation: int | None, digest: str | None) -> EffectOutcome | None:
        """The outcome that stops ``effect_id`` before any decision logic, or ``None``.
        ``current_generation`` is the binding's generation from the authority that fences it (the
        registry); the run is fenced if it or any fence in this journal is past the run's admission.
        ``digest`` is what the caller is about to do; a recorded result for DIFFERENT bytes is not a
        replay of this effect (the run is not deterministic), so the run is cancelled."""
        key = (run_id, effect_id)
        run = st.runs.get(run_id)
        if run is None:
            raise KeyError(f"run {run_id!r} was never started")
        # a recorded result first: a replay runs nothing, so a cancel or fence after the effect does not
        # hide what already happened (and a receipt that never landed can still be written)
        if key in st.results:
            done = st.intents.get(key, {}).get("digest")
            if digest is not None and done is not None and done != digest:
                if run_id not in st.cancelled:
                    self._append({"t": "cancel", "run_id": run_id, "reason": "replay_digest_changed"})
                return EffectOutcome(EffectStatus.CANCELLED)
            return EffectOutcome(EffectStatus.REPLAYED, st.results[key]["result"],
                                 receipt_id=st.receipts.get(key))
        if run_id in st.cancelled:
            return EffectOutcome(EffectStatus.CANCELLED)
        if max(st.fences.get(run["binding_id"], 0), current_generation or 0) > run["generation"]:
            return EffectOutcome(EffectStatus.FENCED)
        if key in st.unknown:
            return EffectOutcome(EffectStatus.POISONED)
        if key in st.intents:
            if self._lease_held(run_id, effect_id):
                return EffectOutcome(EffectStatus.IN_FLIGHT)
            return EffectOutcome(EffectStatus.POISONED)
        return None

    def peek(self, run_id: str, effect_id: str, *, current_generation: int | None = None,
             digest: str | None = None) -> EffectOutcome | None:
        """The barrier that would stop ``effect_id`` right now (cancelled, fenced, already done,
        poisoned, in flight), or ``None`` if nothing but a decision could. Runs nothing. A caller
        uses it to short-circuit a replay before deciding anything; :meth:`effect` re-checks under
        its own lock."""
        with self._locked():
            return self._barrier(self._state(), run_id, effect_id, current_generation, digest)

    def hold(self, run_id: str, effect_id: str, *, digest: str, at: str | None = None,
             current_generation: int | None = None) -> EffectOutcome:
        """Open (or find) the decision that guards ``effect_id``: the run suspends BEFORE the effect.

        Returns HELD with the hold id (new, or the existing open one: proposing the same effect again
        never opens a second decision), APPROVED if that hold was already approved (the decision was
        made and the effect has not run: run it with :meth:`effect`), or the barrier that stops the
        effect. A different ``digest`` from the one the open or approved hold carries means the
        bytes changed under the decision: the run is cancelled. ``at`` (an ISO time) is recorded with a
        new hold, so a hold whose pending never landed can be found and rejected later."""
        hold_id = hold_id_for(run_id, effect_id)
        with self._locked():
            st = self._state()
            barrier = self._barrier(st, run_id, effect_id, current_generation, digest)
            if barrier is not None:
                return barrier
            h = st.holds.get(hold_id)
            if h is not None:
                if h["digest"] != digest:
                    self._append({"t": "cancel", "run_id": run_id, "reason": "digest_changed"})
                    return EffectOutcome(EffectStatus.CANCELLED)
                if h["decided"] is None:
                    return EffectOutcome(EffectStatus.HELD, hold_id=hold_id)
                if h["decided"]:
                    return EffectOutcome(EffectStatus.APPROVED, hold_id=hold_id, decided_by=h["by"])
                return EffectOutcome(EffectStatus.CANCELLED)   # rejected (the run is cancelled too)
            self._append({"t": "hold", "hold_id": hold_id, "binding_id": st.runs[run_id]["binding_id"],
                          "run_id": run_id, "effect_id": effect_id, "digest": digest, "at": at})
            return EffectOutcome(EffectStatus.HELD, hold_id=hold_id, new_hold=True)

    def effect(self, run_id: str, effect_id: str, *, digest: str,
               fn: Callable[[], Any], needs_decision: bool = False,
               current_generation: int | None = None) -> EffectOutcome:
        """Run one effect at most once.

        ``digest`` identifies exactly what the effect will do (the bytes a person approves). It is
        recorded with the intent and with any hold, and a decision must echo it.
        ``needs_decision`` is the gate's verdict for this effect: True means the effect runs only
        under an APPROVED hold of its own (one is opened if there is none). An effect with no
        approved hold of its own is HELD while any hold on its binding is open.
        ``current_generation``: the binding's generation from its fencing authority (see ``_barrier``)."""
        hold_id = hold_id_for(run_id, effect_id)
        with self._locked():
            st = self._state()
            barrier = self._barrier(st, run_id, effect_id, current_generation, digest)
            if barrier is not None:
                return barrier
            binding_id = st.runs[run_id]["binding_id"]
            own = st.holds.get(hold_id)
            approved = own is not None and own["decided"] is True
            if own is not None and own["decided"] is None:
                return EffectOutcome(EffectStatus.HELD, hold_id=hold_id)
            if own is not None and own["decided"] is False:
                return EffectOutcome(EffectStatus.CANCELLED)
            if needs_decision and not approved:
                self._append({"t": "hold", "hold_id": hold_id, "binding_id": binding_id,
                              "run_id": run_id, "effect_id": effect_id, "digest": digest})
                return EffectOutcome(EffectStatus.HELD, hold_id=hold_id)
            if not approved:
                # hold-until-decided: an open hold anywhere on the binding stops an undecided effect,
                # whether the hold belongs to this run or a sibling.
                for h in st.holds.values():
                    if h["binding_id"] == binding_id and h["decided"] is None:
                        return EffectOutcome(EffectStatus.HELD, hold_id=h["hold_id"])
            if approved and own is not None and own["digest"] != digest:
                # Approved bytes and the bytes about to run differ: what was approved is not this.
                self._append({"t": "cancel", "run_id": run_id, "reason": "digest_changed"})
                return EffectOutcome(EffectStatus.CANCELLED)
            lease = self._take_lease(run_id, effect_id)   # before the intent: see "leases" above
            try:
                self._append({"t": "intent", "run_id": run_id, "effect_id": effect_id,
                              "digest": digest, "pid": os.getpid()})
            except BaseException:
                self._drop_lease(run_id, effect_id, lease)
                raise
        try:
            try:
                result = fn()
                json.dumps(result)
            except BaseException:
                with self._locked():
                    self._append({"t": "unknown", "run_id": run_id, "effect_id": effect_id})
                raise
            with self._locked():
                self._append({"t": "result", "run_id": run_id, "effect_id": effect_id, "result": result})
        finally:
            self._drop_lease(run_id, effect_id, lease)
        return EffectOutcome(EffectStatus.DONE, result)

    def note_receipt(self, run_id: str, effect_id: str, receipt_id: str) -> None:
        """Record that the receipt for this effect was persisted, so a replay does not write another."""
        with self._locked():
            self._append({"t": "receipt", "run_id": run_id, "effect_id": effect_id,
                          "receipt_id": receipt_id})

    # --- decisions -------------------------------------------------------------------------
    def decide(self, hold_id: str, *, approve: bool, digest: str, by: str | None = None) -> HoldResult:
        """Resolve a hold. ``digest`` must equal the one recorded with the hold (the decision is
        bound to what was shown). A rejection cancels the hold's run. A hold decides once. ``by``
        names the decider for the record."""
        with self._locked():
            st = self._state()
            h = st.holds.get(hold_id)
            if h is None:
                return HoldResult(False, "unknown_hold")
            if h["decided"] is not None:
                return HoldResult(False, "already_decided")
            if h["digest"] != digest:
                return HoldResult(False, "digest_mismatch")
            self._append({"t": "decide", "hold_id": hold_id, "approve": bool(approve), "by": by})
            if not approve:
                self._append({"t": "cancel", "run_id": h["run_id"], "reason": "rejected"})
            return HoldResult(True, "approved" if approve else "rejected")

    def withdraw(self, hold_id: str) -> bool:
        """Close an UNDECIDED hold without deciding it: the infrastructure failed (its pending could not
        be persisted, a chain's state could not be written), which is not a "no" from anyone, so the run
        is NOT cancelled and re-delivering the event proposes the effect again. Returns True iff an
        open hold was closed."""
        with self._locked():
            h = self._state().holds.get(hold_id)
            if h is None or h["decided"] is not None:
                return False
            self._append({"t": "withdraw", "hold_id": hold_id})
            return True

    def open_holds(self) -> list[dict[str, Any]]:
        """Every undecided hold (``hold_id``, ``binding_id``, ``run_id``, ``effect_id``, ``digest``,
        ``at``):
        each one is stopping its binding's undecided effects until someone decides it."""
        with self._locked():
            st = self._state()
        return [{k: h.get(k) for k in ("hold_id", "binding_id", "run_id", "effect_id", "digest", "at")}
                for h in st.holds.values() if h["decided"] is None]

    def poisoned(self) -> list[tuple[str, str]]:
        """Every effect whose outcome is unknown and whose owner is gone: the list a human must look
        at. (An intent whose owner still holds its lease is in flight, not poisoned.)"""
        with self._locked():
            st = self._state()
            out = set(st.unknown - set(st.results))
            for key in st.intents:
                if key not in st.results and key not in st.unknown and not self._lease_held(*key):
                    out.add(key)
        return sorted(out)
