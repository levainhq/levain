"""The reaper's artifact step, CONTRACT §7.7 3b (S14 design D, piece D2): a RETAINED executor or planner session with no
artifact field gets its sealed import run (:func:`levain.slot.runtime_helper.adopt_sealed_run`, class ``dispatch``), whose
publish carries root's ``dispatch-artifact.json`` in the same rename as the bundle, and then ONE field-only CAS names
the result on CONTROL's record: ``artifact_digest`` (the sha256 of the record file root wrote, D2) or, for a refusal
that will not clear, ``artifact_failed`` (D5).

It runs in its own reaper child (design D1 b), beside the pass worker and the revoker, so a long import never holds a
pass (BRAKE, END, SCRUB). The locks already serialize it against END, discard, adopt-open and adopt-verify: the alloc's
session lock and the machine-wide adopt lock are each TRIED, and either held means a skip until the next tick, with no
wait and no count. At most one sealed run per tick.

ROOT-side trust boundary: every byte published is root's copy and every field of the record is a root fact (the alloc,
``object.json``, the digests root computed while copying); the importer's ``report.json`` is checked and never read
here. Fails closed: a refusal that is not a skip ends in ``artifact_failed`` after at most :data:`ARTIFACT_ATTEMPTS`
tries, never in a publish nobody named. Stdlib, ``levain.slot.*`` and ``levain.contracts.canon`` only; ``levain.dispatch``
is never imported, and the module is valid Python 3.9.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Set

from levain.slot import Refused
from levain.slot import fsck_ids as FI
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot.grammar import SESSION_RE

#: DECISION (design D1): the artifact worker's tick. [judged]
ARTIFACT_TICK_S = 10.0
#: DECISION (design D5): how many tries a MACHINE refusal gets before it becomes ``artifact_failed``. [judged]
ARTIFACT_ATTEMPTS = 3
#: The alloc kinds the step imports: an executor's (``dispatch``) and a planner's. Never a verifier's (§7.7).
STEP_KINDS = frozenset({"dispatch", "planner"})

#: Refusal names that are the executor's content or root's record: they will not clear, so they are final at once.
PERMANENT_EXACT = frozenset({
    "adopt-path-too-long", "adopt-work-cap", "bundle-header-mismatch", "export-object-collision",
    "loose-name-mismatch", "refs-cap", "ref-head-loop", "export-result-mismatch", "base-unrecorded",
    "not-retained:absent"})
PERMANENT_PREFIX = ("walker-cap:", "adopt-cap:", "export-output-shape:", "artifact-record:")
#: ``importer-failed:*`` is permanent EXCEPT the publish (a machine fault in root's own copy).
MACHINE_IMPORTER = frozenset({"importer-failed:publish"})
#: Not failures: the next tick tries again, and nothing counts (a discard intent in flight is a transient state).
SKIPS = frozenset({"session-busy", "adopt-busy", "intent-in-flight"})
#: CAS refusals that mean only "the record moved": the next tick re-reads it.
STALE_CAS = frozenset({"stale-generation", "from-mismatch"})


def classify(name: str) -> str:
    """``permanent`` | ``machine`` | ``skip`` for one refusal name (design D5). A name this table does not know is a
    MACHINE one: bounded by the attempt count, so an unlisted refusal still ends in ``artifact_failed``."""
    if name in SKIPS:
        return "skip"
    if name in PERMANENT_EXACT or name.startswith(PERMANENT_PREFIX):
        return "permanent"
    if name.startswith("importer-failed:") and name not in MACHINE_IMPORTER:
        return "permanent"
    return "machine"


def _name(e: BaseException) -> str:
    return e.name if isinstance(e, Refused) else type(e).__name__


def new_report() -> Dict[str, List[str]]:
    return {"exported": [], "artifact_failed": [], "skipped": [], "held": []}


class ArtifactWorker:
    """The artifact child's state: the in-memory counts per session (a restart resets them, design D5), ``tries`` of
    the import's refusals and ``cas_refusals`` of CONTROL's non-stale CAS refusals (two counts, so one tick whose
    refusal is then refused its ``artifact_failed`` CAS is never counted twice), and the set of sessions CONTROL refused
    to name, parked until the next restart."""

    def __init__(self, seams: H.Seams, *, log: Callable[[str], None],
                 attempts: int = ARTIFACT_ATTEMPTS) -> None:
        self.seams = seams
        self.log = log
        self.attempts = attempts
        self.tries: Dict[str, int] = {}
        self.cas_refusals: Dict[str, int] = {}
        self.parked: Set[str] = set()

    def ctx(self) -> H.Ctx:
        return H.Ctx(seams=self.seams, store=H.Store(self.seams), sudo_uid=0, caller="reaper", operator_uid=-1, env={})

    # -- reads ---------------------------------------------------------------------------------------------------------

    @staticmethod
    def _record(ctx: H.Ctx, s: str) -> Optional[R.SessionState]:
        """CONTROL's record, a HINT (§7.1): the dir is CONTROL's, so it is read untrusted."""
        raw = ctx.store.read(("sessions",), f"{s}.json", trusted=False)
        if raw is None:
            return None
        rec = R.parse_state(raw)
        return rec if rec.session == s else None

    @staticmethod
    def _wanted(rec: Optional[R.SessionState]) -> bool:
        return (rec is not None and rec.state == R.RETAINED
                and not (R.ARTIFACT_FIELDS & set(rec.fields)))

    @staticmethod
    def _alloc(ctx: H.Ctx, s: str) -> Optional[R.Alloc]:
        raw = ctx.store.read(("alloc",), f"{s}.json")
        if raw is None:
            return None
        a = R.parse_alloc(raw)
        return a if a.session == s and a.kind in STEP_KINDS and isinstance(a.ticket, str) and a.ticket else None

    def candidates(self, ctx: H.Ctx) -> List[str]:
        """The sessions whose record hint says RETAINED with no artifact field, sorted; the alloc is re-read under the
        session lock, so this is only the cheap first cut. A counted session this pass sees absent or no longer wanted
        loses its counts (S14 L3 round 2: a session another worker settled or removed never holds an entry for ever);
        a parked session and one whose record is unreadable keep theirs."""
        try:
            with ctx.store.dir("sessions", trusted=False) as fd:
                names = sorted(os.listdir(fd))
        except Refused as e:
            if e.name != "store-missing":
                raise
            names = []
        out: List[str] = []
        keep: Set[str] = set(self.parked)
        for n in names:
            if not n.endswith(".json") or not SESSION_RE.fullmatch(n[:-5]) or n[:-5] in self.parked:
                continue
            try:
                if self._wanted(self._record(ctx, n[:-5])):
                    out.append(n[:-5])
            except Exception as e:   # noqa: BLE001 - a malformed record is named, never acted on
                keep.add(n[:-5])     # unreadable is not "no longer wanted": its counts stand
                self.log(f"artifact: record {n}: unreadable: {type(e).__name__}: {e}")
        keep.update(out)
        for s in (set(self.tries) | set(self.cas_refusals)) - keep:
            self._forget(s)
        return out

    def _forget(self, s: str) -> None:
        """Drop both counts of ``s``: a session seen absent or no longer wanted, or one whose CAS CONTROL took."""
        self.tries.pop(s, None)
        self.cas_refusals.pop(s, None)

    # -- one tick ------------------------------------------------------------------------------------------------------

    def tick(self) -> Dict[str, List[str]]:
        """One pass over the candidates, stopping after the first sealed run. Returns the report (``exported``,
        ``artifact_failed`` as ``<s>:<name>``, ``skipped`` as ``<s>:<name>``, ``held`` for a retry that counted)."""
        rep = new_report()
        ctx = self.ctx()
        try:
            try:
                ctx.client
            except (Refused, OSError) as e:
                rep["skipped"].append(f"control-down:{_name(e)}")
                return rep
            for s in self.candidates(ctx):
                try:
                    if self._one(ctx, s, rep):
                        break
                except Exception as e:   # noqa: BLE001 - one session's fault is logged and never ends the tick
                    self.log(f"artifact {s}: {type(e).__name__}: {e}")
                    self._refused(ctx, s, _name(e), rep)
        finally:
            try:
                ctx.close()
            finally:
                ctx.store.close()
        return rep

    def _one(self, ctx: H.Ctx, s: str, rep: Dict[str, List[str]]) -> bool:
        """The artifact step for ``s``. True when a sealed run was entered (the tick's one import is spent)."""
        seams = ctx.seams
        adopt = seams.adopt if seams.adopt is not None else H.AdoptSeams()
        run_seams = seams.adopt_run if seams.adopt_run is not None else H.AdoptRunSeams()
        try:
            with H.session_lock(ctx, s):
                a = self._alloc(ctx, s)
                rec = self._record(ctx, s)
                if a is None or not self._wanted(rec):        # re-checked under the lock: it moved, or was never ours
                    self._forget(s)
                    return False
                front = H.adopt_step3_root_facts(ctx, a, adopt, deadline=seams.monotonic() + adopt.lock_wait_s)
                outcome: Optional[Dict[str, object]] = None
                refusal: Optional[BaseException] = None
                ran = False
                try:
                    with H.adopt_lock_try(ctx):
                        H.adopt_step5_precaps(ctx, front, adopt)
                        FI.check_floor(adopt.git_version())
                        ran = True
                        outcome = H.adopt_sealed_run(ctx, front, adopt, run_seams, export_class="dispatch",
                                                     artifact=H.artifact_record_bytes)
                except Refused as e:
                    if e.name == "adopt-busy":
                        raise
                    refusal = e
                if refusal is not None:
                    self._refused(ctx, s, refusal.name, rep, rec=rec)   # type: ignore[attr-defined]
                    return ran
                assert outcome is not None
                self._set(ctx, s, rec, {"artifact_digest": outcome["artifact_digest"]}, rep, exported=True)
                return True
        except Refused as e:
            if classify(e.name) == "skip":
                rep["skipped"].append(f"{s}:{e.name}")           # a held lock: no wait, no count
                return False
            raise

    # -- the CAS and the count -----------------------------------------------------------------------------------------

    def _refused(self, ctx: H.Ctx, s: str, name: str, rep: Dict[str, List[str]],
                 rec: Optional[R.SessionState] = None) -> None:
        kind = classify(name)
        if kind == "skip":
            rep["skipped"].append(f"{s}:{name}")
            return
        n = self.tries[s] = self.tries.get(s, 0) + 1
        if kind == "machine" and n < self.attempts:
            rep["held"].append(f"{s}:{name}")
            self.log(f"artifact {s}: {name} (try {n} of {self.attempts}); retried on a later tick")
            return
        self._fail(ctx, s, name, rep, rec)

    def _fail(self, ctx: H.Ctx, s: str, name: str, rep: Dict[str, List[str]],
              rec: Optional[R.SessionState] = None) -> None:
        """CAS ``artifact_failed`` at ``rec``'s generation (the caller holds the session lock and re-read it), or, with
        no ``rec``, under the session lock taken here at the generation re-read there."""
        if rec is not None:
            self._set(ctx, s, rec, {"artifact_failed": name}, rep, failed=name)
            return
        try:
            with H.session_lock(ctx, s):
                rec = self._record(ctx, s)
                if not self._wanted(rec):
                    self._forget(s)
                    return
                self._set(ctx, s, rec, {"artifact_failed": name}, rep, failed=name)
        except Refused as e:
            rep["skipped"].append(f"{s}:{e.name}")   # a busy lock: the next tick counts and tries again

    def _set(self, ctx: H.Ctx, s: str, rec: Optional[R.SessionState], fields: Dict[str, object],
             rep: Dict[str, List[str]], *, exported: bool = False, failed: Optional[str] = None) -> None:
        """The field-only RETAINED -> RETAINED CAS at ``rec``'s generation. A stale answer is logged and the next
        tick re-reads; any other refusal counts in ``cas_refusals`` (never in ``tries``, which the import's refusal that
        led here may already have counted) and, at the attempts, parks the session (CONTROL will not name it: nothing
        here can fix that, and a re-run would publish again for nothing)."""
        assert rec is not None
        try:
            ctx.call("CAS", session=s, generation=rec.generation, **{"from": R.RETAINED, "to": R.RETAINED},
                     fields=dict(fields))
        except Refused as e:
            if e.name in STALE_CAS:
                self.log(f"artifact {s}: CAS {e.name}; the next tick re-reads")
                rep["skipped"].append(f"{s}:{e.name}")
                return
            n = self.cas_refusals[s] = self.cas_refusals.get(s, 0) + 1
            rep["skipped"].append(f"{s}:cas:{e.name}")
            self.log(f"artifact {s}: CAS refused {e.name} (refusal {n} of {self.attempts})")
            if n >= self.attempts:
                self.parked.add(s)
                self.log(f"artifact {s}: parked until the reaper restarts")
            return
        self._forget(s)
        if exported:
            rep["exported"].append(s)
        if failed is not None:
            rep["artifact_failed"].append(f"{s}:{failed}")
