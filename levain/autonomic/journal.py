"""levain.autonomic.journal — the durable run journal: an unattended run's effects, at most once.

An unattended run is a sequence of EFFECTS (a send, a write, a payment). This journal is what lets
such a run stop for a human between two effects and resume later without doing anything twice. It
is the substrate for the per-effect gate in the autonomic-fold design (section 7): the gate decides
whether the next effect may run, and the journal makes that decision hold.

The model is durable execution's, reduced to the part Levain needs: every side effect is a
journaled unit with a stable id, and resuming a run replays the recorded results of the effects
that already ran instead of running them again. There is deliberately no mid-effect pause: a run
suspends BEFORE an effect, never inside one, because an in-flight Python call cannot be resumed.

Four properties, each exercised by a run in ``tests/test_autonomic_journal.py``:

  - **hold-until-decided** — while a hold is open on a binding, NO effect of that binding proceeds,
    in this run or a sibling run (the "sibling leak": a gate that pauses one branch while another
    branch's effect runs during the pause);
  - **reject-cancels** — rejecting a hold cancels the run; none of its later effects run;
  - **dedup-on-replay** — an effect with a recorded result is never executed again; the record is
    returned instead;
  - **fence-on-cancel** — a fence on a binding (a demotion, a status change, a cancel) stops every
    run admitted under an older governance generation at its next effect.

And one rule that is not a property but follows from "at most once": an effect whose intent was
recorded and whose result was not (the process died mid-call, or the call raised) has an UNKNOWN
outcome. It is POISONED: never retried, surfaced for a human. A send that may have gone out must
not be sent again on a guess.

The residual, stated: the lock is not held across the effect call itself (it cannot span the I/O),
so a fence or hold written while an effect is already inside its call stops the NEXT effect, not
that one. At most one effect per run is in flight, and it is journaled.

Storage: one append-only JSONL file, every read-check-write under one ``flock`` on a sidecar lock
file, every append flushed and fsynced. State is derived by replaying the file. Stdlib only.
"""
from __future__ import annotations

import enum
import fcntl
import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["EffectStatus", "EffectOutcome", "HoldResult", "RunJournal", "JournalCorruptError"]


class JournalCorruptError(RuntimeError):
    """The journal file cannot be read. Fail closed: an unreadable journal cannot prove an effect
    has not already run, so nothing proceeds until a human looks at it."""


class EffectStatus(str, enum.Enum):
    DONE = "done"            # executed now; ``result`` is what ``fn`` returned
    REPLAYED = "replayed"    # executed earlier; ``result`` is the recorded value; ``fn`` NOT called
    HELD = "held"            # a hold is open on this binding; ``fn`` NOT called
    POISONED = "poisoned"    # intent recorded, outcome unknown; ``fn`` NOT called, needs a human
    IN_FLIGHT = "in_flight"  # another live process is inside this effect right now; ``fn`` NOT called
    FENCED = "fenced"        # the binding was fenced past this run's generation; ``fn`` NOT called
    CANCELLED = "cancelled"  # the run was cancelled (a rejected hold); ``fn`` NOT called


@dataclass(frozen=True)
class EffectOutcome:
    status: EffectStatus
    result: Any = None
    hold_id: str | None = None

    @property
    def ran_now(self) -> bool:
        return self.status is EffectStatus.DONE


@dataclass(frozen=True)
class HoldResult:
    ok: bool
    reason: str


@dataclass
class _State:
    runs: dict[str, dict[str, Any]]
    intents: dict[tuple[str, str], dict[str, Any]]
    results: dict[tuple[str, str], dict[str, Any]]
    unknown: set[tuple[str, str]]
    holds: dict[str, dict[str, Any]]
    fences: dict[str, int]
    cancelled: set[str]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunJournal:
    """The durable journal for unattended runs. One file per journal; many runs and bindings."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(self._path.name + ".lock")

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
            os.fsync(f.fileno())
        if not existed:
            dfd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
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
                os.fsync(f.fileno())

    def _state(self) -> _State:
        st = _State({}, {}, {}, set(), {}, {}, set())
        if not self._path.exists():
            return st
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
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
            if t == "run":
                st.runs[r["run_id"]] = r
            elif t == "intent":
                st.intents[(r["run_id"], r["effect_id"])] = r
            elif t == "result":
                st.results[(r["run_id"], r["effect_id"])] = r
            elif t == "unknown":
                st.unknown.add((r["run_id"], r["effect_id"]))
            elif t == "hold":
                st.holds[r["hold_id"]] = dict(r, decided=None)
            elif t == "decide":
                if r["hold_id"] in st.holds:
                    st.holds[r["hold_id"]]["decided"] = r["approve"]
            elif t == "fence":
                st.fences[r["binding_id"]] = max(st.fences.get(r["binding_id"], 0), r["generation"])
            elif t == "cancel":
                st.cancelled.add(r["run_id"])
            else:
                raise JournalCorruptError(f"run journal {self._path} line {n}: unknown record {t!r}")
        return st

    # --- runs ------------------------------------------------------------------------------
    def start(self, run_id: str, *, binding_id: str, generation: int) -> None:
        """Admit a run under the binding's CURRENT governance generation. Starting an existing run
        id again is a no-op (a resumed run keeps its original admission)."""
        with self._locked():
            if run_id in self._state().runs:
                return
            self._append({"t": "run", "run_id": run_id, "binding_id": binding_id,
                          "generation": int(generation)})

    def fence(self, binding_id: str, *, generation: int) -> None:
        """Every run of ``binding_id`` admitted under a generation BELOW ``generation`` stops at its
        next effect. Called by a demotion, a status change or a cancel, with the new generation."""
        with self._locked():
            self._append({"t": "fence", "binding_id": binding_id, "generation": int(generation)})

    # --- effects ---------------------------------------------------------------------------
    def effect(self, run_id: str, effect_id: str, *, digest: str,
               fn: Callable[[], Any], needs_decision: bool = False) -> EffectOutcome:
        """Run one effect at most once.

        ``digest`` identifies exactly what the effect will do (the bytes a human approves). It is
        recorded with the intent and with any hold, and a decision must echo it.
        ``needs_decision`` is the gate's verdict for this effect: True opens a hold (the run
        suspends BEFORE the effect) unless an approved hold for this very effect already exists.
        """
        key = (run_id, effect_id)
        with self._locked():
            st = self._state()
            run = st.runs.get(run_id)
            if run is None:
                raise KeyError(f"run {run_id!r} was never started")
            binding_id = run["binding_id"]
            if run_id in st.cancelled:
                return EffectOutcome(EffectStatus.CANCELLED)
            if st.fences.get(binding_id, 0) > run["generation"]:
                return EffectOutcome(EffectStatus.FENCED)
            if key in st.results:
                return EffectOutcome(EffectStatus.REPLAYED, st.results[key]["result"])
            if key in st.unknown:
                return EffectOutcome(EffectStatus.POISONED)
            if key in st.intents:
                pid = st.intents[key].get("pid")
                if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
                    return EffectOutcome(EffectStatus.IN_FLIGHT)
                return EffectOutcome(EffectStatus.POISONED)
            own_hold = None
            for h in st.holds.values():
                if h["binding_id"] != binding_id:
                    continue
                if h["decided"] is None:
                    # hold-until-decided: an open hold anywhere on the binding stops this effect,
                    # whether it belongs to this run or a sibling.
                    return EffectOutcome(EffectStatus.HELD, hold_id=h["hold_id"])
                if h["run_id"] == run_id and h["effect_id"] == effect_id and h["decided"]:
                    own_hold = h
            if needs_decision and own_hold is None:
                hold_id = uuid.uuid4().hex
                self._append({"t": "hold", "hold_id": hold_id, "binding_id": binding_id,
                              "run_id": run_id, "effect_id": effect_id, "digest": digest})
                return EffectOutcome(EffectStatus.HELD, hold_id=hold_id)
            if own_hold is not None and own_hold["digest"] != digest:
                # Approved bytes and the bytes about to run differ: what was approved is not this.
                self._append({"t": "cancel", "run_id": run_id, "reason": "digest_changed"})
                return EffectOutcome(EffectStatus.CANCELLED)
            self._append({"t": "intent", "run_id": run_id, "effect_id": effect_id,
                          "digest": digest, "pid": os.getpid()})
        try:
            result = fn()
            json.dumps(result)
        except BaseException:
            with self._locked():
                self._append({"t": "unknown", "run_id": run_id, "effect_id": effect_id})
            raise
        with self._locked():
            self._append({"t": "result", "run_id": run_id, "effect_id": effect_id, "result": result})
        return EffectOutcome(EffectStatus.DONE, result)

    # --- decisions -------------------------------------------------------------------------
    def decide(self, hold_id: str, *, approve: bool, digest: str) -> HoldResult:
        """Resolve a hold. ``digest`` must equal the one recorded with the hold (the decision is
        bound to what was shown). A rejection cancels the hold's run. A hold decides once."""
        with self._locked():
            st = self._state()
            h = st.holds.get(hold_id)
            if h is None:
                return HoldResult(False, "unknown_hold")
            if h["decided"] is not None:
                return HoldResult(False, "already_decided")
            if h["digest"] != digest:
                return HoldResult(False, "digest_mismatch")
            self._append({"t": "decide", "hold_id": hold_id, "approve": bool(approve)})
            if not approve:
                self._append({"t": "cancel", "run_id": h["run_id"], "reason": "rejected"})
            return HoldResult(True, "approved" if approve else "rejected")

    def poisoned(self) -> list[tuple[str, str]]:
        """Every effect whose outcome is unknown and whose recording process is gone: the list a
        human must look at. (An intent whose process is still alive is in flight, not poisoned.)"""
        with self._locked():
            st = self._state()
        out = sorted(st.unknown - set(st.results))
        for key, intent in st.intents.items():
            if key in st.results or key in st.unknown:
                continue
            pid = intent.get("pid")
            if not (isinstance(pid, int) and _pid_alive(pid)):
                out.append(key)
        return sorted(set(out))
