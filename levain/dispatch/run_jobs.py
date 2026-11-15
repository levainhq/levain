"""levain.dispatch.run_jobs: the dispatch runner (S14 R1 + R2): a signed job to k running, priced runs, and out again.

``JobRunner.run_job(job_id)`` takes one ``signed``/``queued`` job and does, in this order and nothing else:

1. **Preflight** (:meth:`JobRunner.preflight`), before any state moves: the job's STORED contract is parsed again
   (the bytes the operator's signature covers, re-checked against the job's digest), the SHARE is computed, every
   model's brokered route is resolved by CONTROL (read-only), the template's verifier entry resolves, and the job's
   handoff bundle is present. Every refusal is a :class:`RunnerRefused` with a stable ``name`` and a ``reason_class``
   (``standing`` or ``transient``); NOTHING is taken, minted or started on one.
2. **Take** (:meth:`levain.dispatch.jobs.JobStore.take_slot`, the first production caller): ``signed``/``queued ->
   running``. An :class:`~levain.dispatch.jobs.EnvelopeRefused` / :class:`~levain.dispatch.jobs.TransitionRefused`
   leaves the job as it is and propagates by its own name.
3. **Mint and start k runs**, each GATED, through a brokered :class:`levain.agents.session_launcher.RealLauncher`: run
   ``i`` is model ``models[i]``, priced at the share, keyed by :func:`run_key` (deterministic, so a restart claims the
   same key and the index refuses a second mint), and carries a :class:`~levain.agents.session.DispatchBinding` so its
   ticket's facts are the JOB's id, the run's index and the job's digest (never an agent occurrence's). Only when all k
   are started are they released (record, then release, the controller's order). A refusal at run ``i`` aborts the
   gated runs already started (nothing of theirs ran) and, once every run's END is proven, fails the job ``run_failed``
   (:meth:`JobRunner._refused`); the one exception is a TRANSIENT refusal of a run no other run has started beside: the
   job stays ``running`` and the next tick mints again (the lease bounds the retries, :meth:`JobStore.overdue_running`).

THE SHARE: ``dollars_cents * 10_000 // k`` micro-dollars per run, read from the signed contract and from nowhere else
(exact integer multiply, floor division; the remainder, under k micro-dollars, is reserved by no run). ``share == 0``
refuses ``share-zero``. CONTROL then holds each run's share to its ceiling (``max_reserved_microdollars_per_run``, a
standing ``ticket-ceiling:...``) at the mint, and keys books every call of a run under its own scope against it.

**R2: watch, exit and stop** (:meth:`JobRunner.tick`, :meth:`JobRunner.settle_job`, :meth:`JobRunner.stop_job`). Every
tick first RECONCILES each ``running`` job from the durable facts alone (the session index by each run's deterministic
key, and CONTROL's ``RUN-TICKET-STATUS`` by that key), so a fresh runner after a crash needs nothing the dead one held;
only then does it pick up ``signed``/``queued`` jobs (a standing preflight refusal fails the job ``run_failed`` untaken,
a transient one moves ``signed -> queued`` and retries). For a running job, in this order of precedence:

* a stop is wanted: the supervisor's cancel (``cancel_for``: ``planner_cancel``/``human_cancel``; in production
  :func:`store_cancel_for`, the request the store kept when it refused the cancel of a running job), an ended envelope
  (:meth:`JobRunner.stop_envelope`, from ``end_envelope``'s ``to_stop``), a lapsed lease (``overdue_running``), or a run
  that OVERRAN its share (a lineage row with ``overrun`` for any run: ``budget_cap``). The runner stops every run (an
  END attempt for each, never a guess), and records the stop ONLY when every run is proven over;
* a run that never started or that a crash left between its mint and its start (a ``pending``/``minted`` record), or that
  ended with no taken artifact past :data:`ARTIFACT_GRACE_S`: the other runs are stopped the same way and the job fails
  ``run_failed`` naming the run and the reason;
* a run that ended with an ``artifact_failed`` note and no taken artifact (the name root's artifact step gave), or whose
  taken artifact names no bundle (the executor changed nothing): the job fails ``run_failed`` at once, named
  ``artifact_failed:<name>`` or ``no-change``, the other runs stopped first;
* every run ``ended`` and every artifact taken by the follower: ``running -> verifying`` by
  :meth:`JobStore.runs_exited`, which itself refuses unless the store holds a taken artifact for every run, and
  records a cancel kept since this pass read the stop request (the runs are over) instead of verifying.

A started run is over when its index record says ``ended`` (S1 END proved the slot uid's group gone, written by the
launcher). A child this process holds is polled for it; a run a dead runner left is ended by :meth:`RealLauncher.settle_run`
once CONTROL has RELEASED its ticket, or when a stop is wanted. Known edge: a restart cannot tell a live session from a
finished one while its ticket is still ``bound``, so it waits for the slot's release (or the lease) rather than ending it.

**The workspace (S14 design B2).** An executor reads exactly the contract's signed read list. The runner verifies the job's
handoff BEFORE the take, in two steps: a cheap prefix check in :meth:`JobRunner.preflight` (``handoff-mismatch``) and
the full verify (:class:`levain.dispatch.run_workspace.ReadSetStager`, a ``readset-*`` refusal) between preflight and
:meth:`JobStore.take_slot`, so a bad handoff mints and takes nothing. The verified set is held under each run's key for
the length of the start loop (the slot runtime's ``workspace`` seam is ``stager.stage``) and discarded when it ends. The
launch's own ``workspace`` member stays None: the W1 upload is the operator-uid agent launch's, and no upload reaches this
process. A runner built WITHOUT a stager verifies nothing and stages nothing (a test seam; :func:`build_runner` always
builds one).

What this half still does NOT do (S14 R3, ``1114-42_c42/S14_RUNNER_DESIGN.md`` sections 1 and 8): mint a verifier ticket,
run as a process with its lock, and carry the sign-view wording. Known edge, unchanged from the design page: a brokered ticket carries no
``tool_calls``/``tokens`` caps, so the dollar share is the hard bound.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from levain.agents.session import DispatchBinding, SessionLaunch
from levain.agents.session_launcher import ControlPort, HeldChild, LauncherRefused, RealLauncher, RunPhase
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import registry
from levain.dispatch.contract import ResolvedContract, parse_contract
from levain.dispatch.handoff import HandoffRefused
from levain.dispatch.jobs import EnvelopeEnd, Job, JobStore
from levain.dispatch import readset as RS
from levain.slot import Refused

if TYPE_CHECKING:
    from levain.dispatch.adopt import Envelope
    from levain.dispatch.run_workspace import ReadSetStager
    from levain.dispatch.verify_stage import VerifyStage

log = logging.getLogger("levain.dispatch.run_jobs")

#: one cent is this many micro-dollars
MICROS_PER_CENT = 10_000
#: the executor harness: an executor ticket is on ``claude`` only (``slot/start.py``, ``executor-vendor-unsupported``);
#: each template model runs as a Claude Code session on a BROKERED route that serves it
HARNESS = "claude"
#: the launcher's name for a key that already holds a record: the run was minted before (a restart), never again
_HELD_KEY = "session-key-already-used"
#: how long after a run's END the follower may still take its artifact before the run counts as having left none: it
#: covers "no event at all", the one case no `artifact_failed` note names. An import runs up to V7 (1800 s) after the
#: reaper's next tick (V11, 60 s) and the importer's result grace (60 s), plus 120 s for the follower's own cadence.
#: dispatch cannot import ``levain.slot``, so the sum is written out and pinned in
#: ``tests/test_dispatch_s14d3_runner.py`` against ``runtime_helper.V7_SECONDS``, ``reaper.V11_S`` and
#: ``runtime_helper.RESULT_GRACE_S``; [judged]
ARTIFACT_GRACE_S = 60.0 + 1800.0 + 60.0 + 120.0
#: the causes a supervisor's ``cancel_for`` hook may name for a running job
_CANCEL_CAUSES = ("planner_cancel", "human_cancel")
#: the read-set refusals that are the host's fault and not the contract's: a retry may pass (the handoff copy past its
#: deadline or size on a slow disk among them); pinned equal to ``levain.slot.workspace``'s table by a test
_TRANSIENT_READSET = frozenset({"readset-git-unavailable", "readset-workdir", "readset-handoff-raced"})
_NAME_OK = re.compile(r"[^A-Za-z0-9._:/@-]")


class RunnerRefused(DispatchError):
    """The runner refused a job before taking it, by a stable ``name``; ``reason_class`` is ``standing`` (it will not
    pass until something changes) or ``transient`` (a retry may). The detail names the problem, never a secret."""

    def __init__(self, name: str, detail: str = "", *, reason_class: str = "standing") -> None:
        self.name, self.detail, self.reason_class = name, detail, reason_class
        super().__init__(f"{name}: {detail}" if detail else name)


def _readset_refused(exc: "RS.ReadSetRefused") -> RunnerRefused:
    """A read-set refusal as the runner's: its own name and detail; transient only for the host's faults."""
    cls = "transient" if exc.name in _TRANSIENT_READSET else "standing"
    return RunnerRefused(exc.name, (getattr(exc, "detail", "") or str(exc))[:200], reason_class=cls)


def share_micros(dollars_cents: int, k: int) -> int:
    """One run's dollar share in micro-dollars: ``dollars_cents * 10_000 // k``, exact integers, floor."""
    if type(dollars_cents) is not int or dollars_cents < 0 or type(k) is not int or k < 1:
        raise RunnerRefused("share-inputs", "the contract's dollars_cents and k are not a non-negative and a positive integer")
    return dollars_cents * MICROS_PER_CENT // k


def run_key(job_id: str, run_index: int) -> str:
    """The deterministic session key (and attempt id) of run ``run_index`` of ``job_id``: ``dj-`` and 32 hex of the
    sha256 of ``"<job id>:<index>"``. The same job and index always name the same key, so a runner that restarts claims
    the key the first one did and the session index refuses a second mint for it."""
    digest = hashlib.sha256(f"{job_id}:{run_index}".encode("utf-8")).hexdigest()
    return "dj-" + digest[:32]


def render_brief(job_id: str, contract: ResolvedContract, run_index: int, k: int) -> str:
    """The run's brief, from the signed contract alone: the goal and the exact files it may read and write."""
    lines = [f"# Dispatch task {job_id} (run {run_index + 1} of {k})", "", "## Goal", "", contract.goal, "",
             "## Files you may read (exactly these)", ""]
    lines += [f"- {e.path}" for e in contract.read] or ["- (none)"]
    lines += ["", "## Files you may write (exactly these)", ""]
    lines += [f"- {e.path}" for e in contract.write] or ["- (none)"]
    lines += ["", "Change nothing else. When the work is done, stop.", ""]
    return "\n".join(lines)


def lineage_overrun_reader(path: object) -> Callable[[str], bool | None]:
    """``overran(run_id)``: True when CONTROL's lineage sink at ``path`` holds a row of that run with ``overrun`` set
    (keys books the part of a call's cost past its hold there, and stops the run); False when it holds none; None when
    the sink cannot be read (DECISION, S14 L3 F5: an absent sink too, since a run CONTROL minted is expected to have one
    and a missing one is no evidence of "no overrun"), or a line naming the run that is not the sink's torn last line
    does not parse: UNKNOWN, which the runner never reads as "no overrun" (a job whose runs' overrun is unknown does not
    move to ``verifying``; it waits, bounded by its lease). Reads the sink whole each call, tolerating a torn last line,
    and keeps no state."""
    def overran(run_id: str) -> bool | None:
        if type(run_id) is not str or not run_id or not isinstance(path, (str, os.PathLike)):
            return None
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        lines = data.split(b"\n")
        for n, raw in enumerate(lines):
            if not raw or run_id.encode("utf-8") not in raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                if n == len(lines) - 1:
                    continue                    # the torn last line (no newline yet): a write in progress
                return None                     # a whole line naming the run that does not read: unknown, never "none"
            if isinstance(row, dict) and row.get("run_id") == run_id and type(row.get("overrun")) is int \
                    and row["overrun"] > 0:
                return True
        return False
    return overran


@dataclass(frozen=True)
class Plan:
    """What a preflight that passed derived: the parsed contract, the k models, the per-run ``share`` and each model's
    route as CONTROL resolved it (``route``, ``routemap_generation``, ``route_policy_digest``)."""
    job_id: str
    digest: str
    contract: ResolvedContract
    models: tuple[str, ...]
    k: int
    share: int
    routes: tuple[Mapping[str, object], ...]
    verifier_id: str


@dataclass(frozen=True)
class RunStart:
    """One run's outcome: its ``index`` and ``key``, its ``model``; ``child`` the gated child this call spawned (None for
    a run whose key already held a record, which this call neither re-minted nor released); ``refused`` the launcher's
    refusal name for a run that did not start (``reason_class`` with it)."""
    index: int
    key: str
    model: str
    child: Any = None
    refused: str | None = None
    reason_class: str | None = None


@dataclass(frozen=True)
class Started:
    """:meth:`JobRunner.run_job`'s answer: the job as it is after the take (and after the stop, when a refusal failed
    it), each run's outcome in index order, and ``refused`` (the first refusal's name) when a run did not start.
    ``released`` is True when this call released its children (all k runs started, none refused). ``unproven`` names the
    index of an aborted run whose END was not proven. ``stopped`` is the cause the job was recorded stopped with (None
    when it is still running: all started, a transient retry, or a stop whose END proof is outstanding); ``pending``
    is the cause of a stop that was wanted and NOT recorded because some run's END is unproven (never ``stopped``)."""
    job: Job
    runs: tuple[RunStart, ...]
    refused: str | None
    released: bool
    unproven: tuple[int, ...] = ()
    stopped: str | None = None
    pending: str | None = None


@dataclass(frozen=True)
class Settled:
    """What one :meth:`JobRunner.settle_job` / :meth:`JobRunner.stop_job` pass did to a running job: ``action`` is one of
    ``waiting``, ``verifying`` (``runs_exited`` written), ``stopped`` (``cause`` recorded), ``unproven`` (a stop was
    wanted and some run's END is not proven: nothing recorded, the next pass asks again), ``retrying`` (a transient
    mint refusal: the runs were minted again), ``skipped`` (not a running job, or unreadable), ``error`` (the pass over
    this job raised; :meth:`JobRunner.tick` moved on to the others). ``detail`` names why."""
    job: Job | None
    action: str
    cause: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class _View:
    """One run as the durable facts say it is: ``kind`` is ``none`` (no record), ``retry`` (a TRANSIENT mint refusal,
    retakable), ``zero`` (refused for good, or undone: it never ran), ``inflight`` (pending/minted: a start never
    completed), ``live`` (started, not proven over), ``ended`` (END proven) or ``unreadable``."""
    index: int
    key: str
    kind: str
    phase: RunPhase
    taken: bool = False
    overran: bool | None = False
    #: the name root's artifact step failed this run's export with (an ``artifact_failed`` event the follower noted)
    failed: str | None = None
    #: the taken artifact names no bundle: the executor changed nothing (``adopt-nothing-new``)
    nochange: bool = False


class JobRunner:
    """One runner over one job store. ``launcher`` is a BROKERED :class:`RealLauncher`; ``control`` the dispatch-peer
    CONTROL port the preflight reads ``ROUTE-RESOLVE`` through (the launcher's own port in production); ``active`` returns
    the active config items (``verifier`` among them) the verifier entry resolves from; ``envelope_for(job)`` is the
    object an envelope job's take runs under (None for a job filed outside one).

    R2: ``overran(run_id)`` answers whether the run overran its share (:func:`lineage_overrun_reader`; None, or a raise,
    is UNKNOWN: an ended job whose overrun is unknown never moves to ``verifying``);
    ``cancel_for(job)`` names ``planner_cancel`` / ``human_cancel`` for a running job (None: no cancel); the store
    records a cancel only from that party's kept request (:meth:`JobStore.stop_recorded`); ``clock`` is seconds since the epoch, the launcher's own by default;
    ``grace_s`` is :data:`ARTIFACT_GRACE_S`."""

    def __init__(self, store: JobStore, launcher: RealLauncher, control: ControlPort,
                 active: Callable[[], Mapping[str, Any]], *,
                 envelope_for: Callable[[Job], "Envelope | None"] | None = None,
                 overran: Callable[[str], bool | None] | None = None,
                 cancel_for: Callable[[Job], str | None] | None = None,
                 clock: Callable[[], float] | None = None, grace_s: float = ARTIFACT_GRACE_S,
                 verify: "VerifyStage | None" = None, stager: "ReadSetStager | None" = None) -> None:
        if getattr(launcher, "auth", None) != "brokered":
            raise ValueError("the dispatch runner mints priced, BROKERED tickets only")
        self.verify = verify
        self.stager = stager
        self.store, self.launcher, self.control, self.active = store, launcher, control, active
        self.envelope_for = envelope_for
        self.overran, self.cancel_for = overran, cancel_for
        self.clock = clock if clock is not None else launcher.clock
        self.grace_s = grace_s
        #: the gated children THIS process spawned and has not seen over, by run key (a restart holds none)
        self._children: dict[str, Any] = {}

    # -- preflight ---------------------------------------------------------------------------------------------------

    def preflight(self, job: Job, *, running: bool = False) -> Plan:
        """Everything that can be known before the job leaves ``signed``/``queued``; raises :class:`RunnerRefused` and
        changes nothing. The contract is the STORED one (re-parsed, and its digest re-checked against the job's).
        ``running``: the plan of a job already taken (a transient retry), whose state is ``running``."""
        if job.state not in (("running",) if running else ("signed", "queued")):
            raise RunnerRefused("job-not-takable", f"job {job.id} is {job.state}; only a signed or queued job runs")
        try:
            contract = parse_contract(job.contract_text)
        except ContractRefused as exc:
            raise RunnerRefused("contract-unreadable", str(exc)[:200]) from None
        if contract.digest != job.digest:
            raise RunnerRefused("contract-digest-drift", "the stored contract is not the one its digest names")
        tpl = contract.template
        k = tpl.k
        if type(k) is not int or k < 1 or len(tpl.models) < k:
            raise RunnerRefused("template-models", "the template names fewer models than k")
        models = tuple(tpl.models[:k])
        try:
            cents = contract.budget["dollars_cents"]
        except (KeyError, TypeError):
            raise RunnerRefused("share-inputs", "the contract's budget names no dollars_cents") from None
        share = share_micros(cents, k)
        if share == 0:
            raise RunnerRefused("share-zero", f"{cents} cents over k={k} is no whole micro-dollar per run")
        routes = tuple(self._route(m) for m in models)
        try:
            registry.verifier_ticket(self.active(), verifier_id=tpl.verifier_id)
        except DispatchError as exc:
            raise RunnerRefused("verifier-unresolved", str(exc)[:200]) from None
        try:
            handoff = self.store.handoff_row(job.id)
        except HandoffRefused as exc:
            raise RunnerRefused("handoff-unreadable", str(exc)[:200]) from None
        if handoff is None:
            raise RunnerRefused("no-handoff", f"job {job.id} has no handoff bundle")
        if self.stager is not None:
            try:
                problem = self.stager.prefix_problem(job)
            except RS.ReadSetRefused as exc:
                raise _readset_refused(exc) from None
            if problem is not None:
                raise RunnerRefused("handoff-mismatch", f"the handoff's {problem} is not the one the contract derives")
        return Plan(job.id, job.digest, contract, models, k, share, routes, tpl.verifier_id)

    def _route(self, model: str) -> Mapping[str, object]:
        """``ROUTE-RESOLVE {model}`` through the launcher's own resolve (one reading of CONTROL's answer): read-only, so
        a refusal minted nothing. CONTROL's ``no-brokered-route`` / ``route-ambiguous`` come back standing."""
        try:
            return self.launcher.resolve_route(model)
        except LauncherRefused as exc:
            raise RunnerRefused(exc.name, exc.detail, reason_class=exc.reason_class) from None

    # -- take, mint, start -------------------------------------------------------------------------------------------

    def run_job(self, job_id: str) -> Started:
        """Preflight, take the slot, mint and start the job's k runs (see the module docstring). Raises
        :class:`RunnerRefused` before anything moves; a refusal AFTER the take is returned in :class:`Started`, with
        the job failed ``run_failed`` once every run's END is proven (or left running for a transient retry)."""
        job = self.store.get(job_id)
        if job is None:
            raise RunnerRefused("job-unknown", job_id[:64])
        plan = self.preflight(job)
        v = self._verified(job)
        try:
            envelope = self.envelope_for(job) if self.envelope_for is not None else None
            taken = self.store.take_slot(job.id, envelope=envelope)
        except BaseException:
            self._discard(v)
            raise
        return self._launch(taken, plan, v)

    def _verified(self, job: Job) -> "RS.VerifiedReadSet | None":
        """The full verify of ``job``'s handoff (None with no stager). A refusal is a :class:`RunnerRefused` by the
        readset's own name; the host faults (:data:`_TRANSIENT_READSET`) are transient, every other one standing. A refusal leaves nothing on disk."""
        if self.stager is None:
            return None
        try:
            return self.stager.verify(job)
        except RS.ReadSetRefused as exc:
            raise _readset_refused(exc) from None

    def _discard(self, v: "RS.VerifiedReadSet | None") -> None:
        if v is not None and self.stager is not None:
            try:
                self.stager.discard(v)
            except Exception as exc:  # noqa: BLE001 - a leftover workdir is swept at the next start; never mask the launch
                log.warning("a verified read set was not discarded (%s); the next start's sweep takes it", type(exc).__name__)

    def _launch(self, taken: Job, plan: Plan, v: "RS.VerifiedReadSet | None" = None) -> Started:
        """Start the job's k runs. ``v`` is the verified read set (:meth:`_verified`), held under each run's key across its
        spawn and discarded when the loop ends (staging is cut inside ``spawn``, so afterwards the workdir has done its work)."""
        starts: list[RunStart] = []
        try:
            for i, model in enumerate(plan.models):
                if v is not None and self.stager is not None:
                    self.stager.register(run_key(plan.job_id, i), v)
                starts.append(self._start(plan, i, model))
                if starts[-1].refused is not None:
                    break
        finally:
            if v is not None and self.stager is not None:
                for i in range(plan.k):
                    self.stager.unregister(run_key(plan.job_id, i))
            self._discard(v)
        refused = next((s for s in starts if s.refused is not None), None)
        if refused is not None:
            return self._refused(taken, plan, starts, refused)
        for s in starts:
            if s.child is None:
                continue
            try:
                s.child.release()
            except Exception as exc:  # noqa: BLE001 - failure-atomic: a release that raised stops EVERY run
                # runs before this one may be live (released) and this one and those after are gated: none is left
                # half-started. Every run is stopped and proven over, then the job fails run_failed
                log.warning("job %s: run %d's release raised %s; stopping every run", plan.job_id, s.index,
                            type(exc).__name__)
                done = self._stop(taken, plan.k, "run_failed", run=s.index, name="release-failed",
                                  why=f"run {s.index}'s release raised {type(exc).__name__}")
                return _after_stop(done, taken, tuple(starts), "release-failed")
        return Started(taken, tuple(starts), None, True)

    def _start(self, plan: Plan, i: int, model: str) -> RunStart:
        key = run_key(plan.job_id, i)
        launch = SessionLaunch(
            occurrence_id=plan.job_id, session_key=key, harness=HARNESS, model=model,
            brief=render_brief(plan.job_id, plan.contract, i, plan.k), auth="brokered", exposure=None,
            workspace=None, usd_micros=plan.share,
            binding=DispatchBinding(plan.job_id, i, plan.digest))
        try:
            child = self.launcher.spawn(launch)
        except LauncherRefused as exc:
            if exc.name == _HELD_KEY:
                return RunStart(i, key, model)          # minted before: never again, and not this call's to release
            return RunStart(i, key, model, refused=exc.name, reason_class=exc.reason_class)
        except (Refused, ValueError) as exc:
            return RunStart(i, key, model, refused=getattr(exc, "name", type(exc).__name__), reason_class="standing")
        if isinstance(child, HeldChild):
            # a session is bound to the ticket and its END is unproven: not a started run, and not released
            return RunStart(i, key, model, child=child, refused="start-held", reason_class="standing")
        self._children[key] = child
        return RunStart(i, key, model, child=child)

    def _refused(self, taken: Job, plan: Plan, starts: list[RunStart], bad: RunStart) -> Started:
        """A run did not start after the take. Abort the gated runs already started (they ran nothing), prove every
        run's END, and only then record the stop: ``run_failed`` naming the run and the refusal. A TRANSIENT refusal of
        run ``i`` with no run started beside it and a retakable record is the one case left running: the next tick
        mints it again (``CONTROL`` refused it, so nothing was minted)."""
        unproven = []
        for s in starts:
            if s.child is not None:
                self._children.pop(s.key, None)
                if not _aborted(s.child):
                    unproven.append(s.index)
        started = sum(1 for s in starts if s.child is not None and s.refused is None)
        log.warning("job %s: run %d did not start (%s); %d gated run(s) aborted", plan.job_id, bad.index, bad.refused,
                    started)
        base = Started(taken, tuple(starts), bad.refused, False, tuple(unproven))
        if bad.reason_class == "transient" and started == 0 and bad.child is None and self._retakable(bad.key):
            return base
        done = self._stop(taken, plan.k, "run_failed", run=bad.index, name=_clean(bad.refused or "run-refused"),
                          why=f"run {bad.index} did not start: {bad.refused}")
        return _after_stop(done, taken, tuple(starts), bad.refused, tuple(unproven))

    def _retakable(self, key: str) -> bool:
        """The run's record is a TRANSIENT refusal of this job's mint (the launcher retakes exactly that record)."""
        ph = self.launcher.run_phase(key)
        return ph.state == "refused" and ph.reason_class == "transient"

    # -- the supervisor: watch, exit, stop ---------------------------------------------------------------------------

    def tick(self) -> list[Settled]:
        """One pass: RECONCILE every running job first (a fresh runner needs nothing the dead one held), then pick up
        every ``signed``/``queued`` job oldest filing first. Never raises for one job: a job's refusal is logged and the
        pass moves on: a job whose pass RAISES is answered ``error`` (logged by type, the job left as it is for the next
        tick) and every other job is still settled. Returns what was done to each job it touched."""
        out = [self._isolated(j.id, self.settle_job, j.id) for j in self.store.list() if j.state == "running"]
        if self.verify is not None:
            out += [self._isolated(j.id, self.verify_job, j.id) for j in self.store.list() if j.state == "verifying"]
        for j in self.store.list():
            if j.state in ("signed", "queued"):
                out.append(self._isolated(j.id, self._pick, j))
        return out

    def _isolated(self, job_id: str, act: Callable[[Any], Settled], arg: Any) -> Settled:
        """One job's pass, isolated: a raise is that job's ``error`` (logged by its type), never the whole tick's."""
        try:
            return act(arg)
        except Exception as exc:  # noqa: BLE001 - one job's fault never stops the others' supervision
            log.warning("job %s: this pass raised %s; left as it is, the pass moves on", job_id, type(exc).__name__)
            try:
                job = self.store.get(job_id)
            except Exception:  # noqa: BLE001
                job = None
            return Settled(job, "error", detail=type(exc).__name__)

    def verify_job(self, job_id: str) -> Settled:
        """One pass of the VERIFY stage over a ``verifying`` job (R3): for each run whose executor artifact the follower
        took, mint (once) and start (once) its ``kind: verifier`` ticket (:class:`levain.dispatch.verify_stage.VerifyStage`).
        Writes no outcome: ``record_verification``, driven by the follower from CONTROL's terminal event, moves the job
        ``verifying -> verified | failed``. ``detail`` names each run's act (``0:minted,1:refused:no-bundle``)."""
        job = self.store.get(job_id)
        if job is None or job.state != "verifying" or self.verify is None:
            return Settled(job, "skipped", detail="not-verifying")
        try:
            k = job.contract.template.k
        except ContractRefused:
            return Settled(job, "skipped", detail="contract-unreadable")
        acts = []
        for i in range(k):
            ph = self.launcher.run_phase(run_key(job.id, i))
            taken = (self.store.taken_artifact(ph.session, ph.ticket)
                     if ph.session is not None and ph.ticket is not None else None)
            if taken is None:
                acts.append(f"{i}:refused:no-taken-artifact")        # runs_exited needed it; a missing one is named, never minted
                continue
            act = self.verify.run(job, i, taken)
            acts.append(f"{i}:{act.action}" + (f":{act.name}" if act.name else ""))
            if act.action == "closed":
                # D7: a verifier ticket cancelled or expired before it bound can never be reminted (its attempt id is
                # deterministic), so the job fails here, naming the state, rather than staying `verifying` for ever
                failed = self.store.fail_verifier_closed(job.id, i, act.name)
                return Settled(failed, "failed", detail=",".join(acts))
        return Settled(self.store.get(job.id), "verifying", detail=",".join(acts))

    def _pick(self, job: Job) -> Settled:
        try:
            started = self.run_job(job.id)
        except RunnerRefused as exc:
            return self._unrun(job, exc)
        except DispatchError as exc:              # EnvelopeRefused / TransitionRefused: the job is as it was
            log.warning("job %s not taken: %s", job.id, type(exc).__name__)
            return Settled(self.store.get(job.id), "skipped", detail=type(exc).__name__)
        if started.stopped is not None:
            return Settled(started.job, "stopped", started.stopped, started.refused or "")
        if started.pending is not None:
            return Settled(started.job, "unproven", started.pending, started.refused or "")
        return Settled(started.job, "waiting" if started.refused is None else "retrying", detail=started.refused or "")

    def _unrun(self, job: Job, exc: RunnerRefused) -> Settled:
        """A preflight refusal: nothing was taken. A transient one waits (``signed -> queued``, the lease bounds it); a
        standing one fails the job ``run_failed`` from where it stands, the refusal's name as evidence."""
        if exc.name in ("job-not-takable", "job-unknown"):
            return Settled(self.store.get(job.id), "skipped", detail=exc.name)
        try:
            if exc.reason_class == "transient":
                return Settled(self.store.defer_untaken(job.id, name=_clean(exc.name)), "waiting", detail=exc.name)
            failed = self.store.fail_untaken(job.id, name=_clean(exc.name), why=exc.detail)
            return Settled(failed, "stopped", "run_failed", exc.name)
        except DispatchError as e:
            log.warning("job %s: %s not recorded: %s", job.id, exc.name, type(e).__name__)
            return Settled(self.store.get(job.id), "skipped", detail=exc.name)

    def stop_envelope(self, ended: EnvelopeEnd) -> list[Settled]:
        """Stop every job ``end_envelope`` left running (its ``to_stop``): each run is stopped and proven over, and only
        then is the job recorded ``envelope_ended``."""
        out = []
        for job_id in ended.to_stop:
            job = self.store.get(job_id)
            out.append(Settled(job, "skipped", detail="not-running") if job is None or job.state != "running"
                       else self._stop(job, job.contract.template.k, "envelope_ended", why="the envelope ended"))
        return out

    def stop_job(self, job_id: str, cause: str, *, why: str = "") -> Settled:
        """Stop running job ``job_id``'s runs, prove each over, and record ``cause`` (any ``stop_recorded`` cause) ONLY
        when every run's END is proven; else nothing is recorded and the answer is ``unproven``."""
        job = self.store.get(job_id)
        if job is None or job.state != "running":
            return Settled(job, "skipped", detail="not-running")
        return self._stop(job, job.contract.template.k, cause, why=why)

    def settle_job(self, job_id: str) -> Settled:
        """One supervisor pass over running job ``job_id`` (see the module docstring for the order of precedence)."""
        job = self.store.get(job_id)
        if job is None or job.state != "running":
            return Settled(job, "skipped", detail="not-running")
        try:
            k = job.contract.template.k
        except ContractRefused:
            return Settled(job, "skipped", detail="contract-unreadable")
        views = [self._view(job, i) for i in range(k)]
        wanted = self._stop_wanted(job, views)
        if wanted is not None:
            return self._stop(job, k, wanted[0], why=wanted[1])
        # a run that did not start, or a start a crash cut short, fails the job: the others are stopped first
        for v in views:
            if v.kind in ("zero", "inflight"):
                name = _clean(v.phase.reason or ("crashed-between-mint-and-start" if v.kind == "inflight"
                                                 else "run-never-ran"))
                return self._stop(job, k, "run_failed", run=v.index, name=name, why=f"run {v.index}: {name}")
        kinds = {v.kind for v in views}
        if kinds & {"none", "retry"}:
            if kinds <= {"none", "retry"}:
                return self._retry(job)
            bad = next(v for v in views if v.kind in ("none", "retry"))
            return self._stop(job, k, "run_failed", run=bad.index, name="partial-start",
                              why=f"run {bad.index} has no minted run beside runs that did")
        if "unreadable" in kinds:
            return Settled(job, "skipped", detail="index-unreadable")
        # an ended run whose export root could not make fails the job at once, by root's name (never `no-artifact`),
        # and one the executor left unchanged fails it at once too: neither waits for the other runs
        for v in views:
            if v.failed is not None:
                name = _clean("artifact_failed:" + v.failed)
                return self._stop(job, k, "run_failed", run=v.index, name=name,
                                  why=f"run {v.index}: root's artifact step failed: {v.failed[:80]}")
        for v in views:
            if v.nochange:
                return self._stop(job, k, "run_failed", run=v.index, name="no-change",
                                  why=f"run {v.index}: the executor made no change")
        if kinds == {"ended"}:
            missing = [v for v in views if not v.taken]
            if not missing:
                unknown = [v.index for v in views if v.overran is None]
                if unknown:
                    # the budget evidence did not read: never verified on it (the lease bounds the wait)
                    log.warning("job %s: overrun unknown for run(s) %s; not moved to verifying", job.id, unknown)
                    return Settled(job, "waiting", detail="overrun-unknown")
                return self._exited(job, views)
            late = [v for v in missing if self._after_grace(v)]
            if late:
                return self._stop(job, k, "run_failed", run=late[0].index, name="no-artifact",
                                  why=f"run {late[0].index} ended and left no taken artifact in {self.grace_s:g}s")
            return Settled(job, "waiting", detail="artifacts-not-taken")
        # some run ended with no artifact past the grace while another still runs: that run already failed the job
        for v in views:
            if v.kind == "ended" and not v.taken and self._after_grace(v):
                return self._stop(job, k, "run_failed", run=v.index, name="no-artifact",
                                  why=f"run {v.index} ended and left no taken artifact in {self.grace_s:g}s")
        return Settled(job, "waiting", detail="runs-live")

    def _stop_wanted(self, job: Job, views: list[_View]) -> tuple[str, str] | None:
        if self.cancel_for is not None:
            try:
                cause = self.cancel_for(job)
            except Exception:  # noqa: BLE001 - a hook that raised named no cancel
                log.warning("job %s: the cancel hook raised; no cancel assumed", job.id)
                cause = None
            if cause in _CANCEL_CAUSES:
                return cause, "cancelled while running"
            if cause is not None:
                log.warning("job %s: the cancel hook named %r, which is no cancel cause", job.id, str(cause)[:40])
        if job.id in self.store.overdue_running():
            return "lease_lapsed", "the lease lapsed while the runs were live"
        over = next((v for v in views if v.overran is True), None)
        if over is not None:
            return "budget_cap", f"run {over.index} overran its share"
        return None

    def _after_grace(self, v: _View) -> bool:
        # a run whose ticket CONTROL did not answer (down, refusing) is not read as having left no artifact: the take is
        # looked up by that ticket, so the absence proves nothing
        ended = v.phase.ended_s
        return v.phase.ticket is not None and type(ended) in (int, float) and self.clock() - ended >= self.grace_s

    def _view(self, job: Job, i: int) -> _View:
        key = run_key(job.id, i)
        child = self._children.get(key)
        if child is not None and _polled_over(child):
            self._children.pop(key, None)
        ph = self.launcher.run_phase(key)
        if ph.state == "started" and ph.ticket_state == "released" and key not in self._children:
            # CONTROL released the ticket: the slot was scrubbed, so the session is over; prove it and record `ended`
            if self.launcher.settle_run(key) == "ended":
                ph = self.launcher.run_phase(key)
        kind = {"none": "none", "pending": "inflight", "minted": "inflight", "started": "live", "ended": "ended",
                "undone": "zero", "unreadable": "unreadable"}.get(ph.state)
        if ph.state == "refused":
            kind = "retry" if ph.reason_class == "transient" else "zero"
        taken, failed, nochange = False, None, False
        if kind == "ended" and ph.session is not None and ph.ticket is not None:
            got = self.store.taken_artifact(ph.session, ph.ticket)
            taken = got is not None
            nochange = got is not None and got.bundle_digest is None
            if got is None:
                failed = self.store.artifact_failure(ph.session, ph.ticket)
        overran: bool | None = False
        if self.overran is not None:
            # fail CLOSED: None, a raise or anything but a bool is UNKNOWN, never "no overrun"
            if ph.run_id is None:
                overran = None if kind == "ended" else False
            else:
                try:
                    ans = self.overran(ph.run_id)
                except Exception:  # noqa: BLE001 - a reader that broke proved nothing
                    ans = None
                overran = ans if type(ans) is bool else None
        return _View(i, key, kind or "unreadable", ph, taken, overran, failed, nochange)

    def _retry(self, job: Job) -> Settled:
        """Every run of the running job is unminted or a transient refusal: nothing is minted, so mint it again."""
        try:
            plan = self.preflight(job, running=True)
        except RunnerRefused as exc:
            if exc.reason_class == "transient":
                return Settled(job, "retrying", detail=exc.name)
            return self._stop(job, plan_k(job), "run_failed", name=_clean(exc.name), why=exc.detail)
        try:
            v = self._verified(job)
        except RunnerRefused as exc:
            if exc.reason_class == "transient":
                return Settled(job, "retrying", detail=exc.name)
            return self._stop(job, plan_k(job), "run_failed", name=_clean(exc.name), why=exc.detail)
        started = self._launch(job, plan, v)
        if started.stopped is not None:
            return Settled(started.job, "stopped", started.stopped, started.refused or "")
        if started.pending is not None:
            return Settled(started.job, "unproven", started.pending, started.refused or "")
        return Settled(started.job, "retrying" if started.refused else "waiting", detail=started.refused or "retried")

    def _exited(self, job: Job, views: list[_View]) -> Settled:
        """Every run ended and every artifact taken: ``running -> verifying``."""
        for v in views:
            # every run is ended (END proven): no child of this job is held past here, whatever the poll saw
            self._children.pop(v.key, None)
        runs = {v.index: (v.phase.session, v.phase.ticket) for v in views}
        try:
            out = self.store.runs_exited(job.id, runs)  # type: ignore[arg-type]
        except DispatchError as exc:
            log.warning("job %s: runs_exited refused: %s", job.id, exc)
            return Settled(job, "waiting", detail="runs-exited-refused")
        if out.state == "cancelled":
            # the store read a cancel kept after this pass read none (JobStore.runs_exited): recorded, not verified
            return Settled(out, "stopped", out.history[-1].cause, "cancel requested; the runs had exited")
        return Settled(out, "verifying", "runs_exited")

    def _stop(self, job: Job, k: int, cause: str, *, run: int | None = None, name: str | None = None,
              why: str = "") -> Settled:
        """Stop every run of ``job`` and record ``cause`` ONLY when each is proven over (kill, then record). A held child
        is aborted first; then the launcher settles every key: ``none``/``zero`` (nothing ran) or ``ended`` (S1 END
        proved the group gone) count as proven, ``unproven`` does not."""
        unproven: list[int] = []
        for i in range(k):
            key = run_key(job.id, i)
            child = self._children.pop(key, None)
            if child is not None:
                _aborted(child)
            if self.launcher.settle_run(key) not in ("none", "zero", "ended"):
                unproven.append(i)
        if unproven:
            log.warning("job %s: %s stop not recorded; END unproven for run(s) %s", job.id, cause, unproven)
            return Settled(job, "unproven", cause, f"END unproven for run(s) {unproven}")
        try:
            out = self.store.stop_recorded(job.id, cause, why=why, run=run, name=name)
        except DispatchError as exc:
            log.warning("job %s: stop %s not recorded: %s", job.id, cause, exc)
            return Settled(self.store.get(job.id), "skipped", cause, f"stop-refused: {type(exc).__name__}")
        # the store decides the recorded cause in its own transaction (a kept human cancel outranks this pass's cause)
        return Settled(out, "stopped", out.history[-1].cause, name or why[:80])


def _after_stop(done: Settled, taken: Job, starts: tuple[RunStart, ...], refused: str | None,
                unproven: tuple[int, ...] = ()) -> Started:
    """A launch's answer after :meth:`JobRunner._stop`: ``stopped`` ONLY when the store recorded the stop; a stop whose
    END is unproven (or whose record the store refused) is ``pending``, never reported as stopped."""
    job = done.job if done.job is not None else taken
    if done.action == "stopped":
        return Started(job, starts, refused, False, unproven, stopped=done.cause)
    return Started(job, starts, refused, False, unproven, pending=done.cause)


def plan_k(job: Job) -> int:
    return job.contract.template.k


def _clean(name: str) -> str:
    """A refusal's name as the store's evidence label takes it: the allowed characters only, 128 at most."""
    return (_NAME_OK.sub("_", str(name)) or "refused")[:128]


def _polled_over(child: Any) -> bool:
    """One non-blocking look at a child this process holds: True once it exited AND its S1 END is proven (the launcher's
    child answers an exit only after END, and records ``ended`` as it does). Never raises."""
    try:
        return child.process.poll() is not None and bool(child.proof.proven)
    except Exception:  # noqa: BLE001
        return False


def _aborted(child: Any) -> bool:
    """Abort a gated child (it ran nothing); True when the abort proved it gone. Never raises."""
    try:
        return child.abort() == 0
    except Exception:  # noqa: BLE001 - an abort that fails is reported unproven, never raised past the other aborts
        return False


# -- the process: ``python -I -m levain.dispatch.run_jobs`` ------------------------------------------------------------------

#: the runner's lock beside the follower's ``follow.lock``: ONE runner per store
LOCK_NAME = "run.lock"
EX_USAGE = 64
EX_INSTANCE_HELD = 75      # EX_TEMPFAIL: another runner holds the store; the unit manager starts this one again
#: DECISION [judged]: the fixed interval between ticks. The runner reads no event as authority: it ticks on time alone.
TICK_S = 5.0


def take_instance_lock(store_dir: "str | os.PathLike[str]") -> int | None:
    """The held ``<store_dir>/run.lock`` fd (close-on-exec, no link followed), or None when another runner holds it."""
    import fcntl
    fd = os.open(os.path.join(os.fspath(store_dir), LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def store_cancel_for(store: JobStore) -> Callable[[Job], str | None]:
    """The runner's ``cancel_for``: the store's durable stop request (:meth:`JobStore.stop_requested`), which the two
    cancel verbs write for a RUNNING job only after the planner's capability or the human's signature checked, so what it
    names is that party's own cancel."""
    def cancel_for(job: Job) -> str | None:
        return store.stop_requested(job.id)
    return cancel_for


def build_runner(store: JobStore, socket_path: str, host: str, *, envelope_for: Any = None,
                 cancel_for: Any = None, verify_runtime: Any = None, control_dir: Any = None,
                 authority_root: Any = None) -> JobRunner:
    """The PRODUCTION runner: a BROKERED launcher over CONTROL's socket as the dispatch peer, the lineage sink as the
    overrun and spend source, the active configuration read afresh each tick, and a verify stage whose start is the
    caller's ``verify_runtime`` (default: :class:`levain.slot.dispatch_runtime.HelperVerifierRuntime` over the executor's own
    ``HelperSlotRuntime``). ``envelope_for`` (an envelope job's take object) is the caller's: None, so a job filed inside an envelope is
    refused its take (``EnvelopeRefused``, fail closed). ``cancel_for`` defaults to the store's durable stop request
    (:meth:`levain.dispatch.jobs.JobStore.stop_requested`). The configuration is read from ``control_dir`` (default the
    store's directory: ``control.db``) and verified against the authority replica at ``authority_root`` (default
    ``<store directory>/authority``), both explicit: nothing is resolved from the process environment."""
    from levain.agents.session_launcher import SessionIndex
    from pathlib import Path
    from levain.authority import anchor_path
    from levain.consent.queue import authority_source
    from levain.dispatch import cli as dcli
    from levain.dispatch import control as dctl
    from levain.dispatch.run_workspace import ReadSetStager
    from levain.dispatch.verify_stage import VerifyStage
    from levain.slot.dispatch_runtime import PORT_OPS, DispatchControlPort, HelperSlotRuntime, HelperVerifierRuntime
    from levain.slot.dispatch_usage import lineage_spend_reader
    from levain.slot.layout import STORE
    from levain.slot.lineage import SINK
    from levain.slot.services.dispatch_run import bound_usage_sources
    L = STORE[host]
    index_dir = L / "dispatch" / "sessions"
    sink = L / "lineage" / SINK
    # RUN-TICKET-VERIFIER (read-only, dispatch's own verb): the verify stage starts a ticket from CONTROL's record of it
    control = DispatchControlPort(socket_path, ops=PORT_OPS | {"RUN-TICKET-VERIFIER"})
    parent = L / "dispatch" / "readset-work"
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    stager = ReadSetStager(str(parent), store.handoff_row)
    slot_runtime = HelperSlotRuntime(host=host, control_socket=socket_path, store=L, workspace=stager.stage,
                                     repo_settings=stager.repo_settings)
    if verify_runtime is None:
        verify_runtime = HelperVerifierRuntime(slot_runtime)         # shares the executor's lease table: one close() covers both
    launcher = RealLauncher(control, slot_runtime,
                            bound_usage_sources(queue_root=L / "queue", store=L, index_dir=index_dir),
                            SessionIndex(index_dir), auth="brokered", spend=lineage_spend_reader(sink))

    # the control store and the authority it is verified against are the STORE DIRECTORY's own, never resolved from this
    # process's environment (LEVAIN_HOME, HOME, the consent hooks of a directory): design C D2
    control_at = Path(control_dir) if control_dir is not None else Path(store.directory)
    replica_at = Path(authority_root) if authority_root is not None else Path(store.directory) / "authority"
    authority = authority_source(replica_at / "cockpit.db", anchor=anchor_path(replica_at))

    def active() -> Mapping[str, Any]:
        cfg = dcli._control_read(control_at, None, nothing="run", authority=authority)
        if cfg is None:
            raise DispatchError(f"there is no control store: {control_at / dctl.DB_NAME} does not exist")
        return cfg.active()

    if cancel_for is None:
        cancel_for = store_cancel_for(store)

    return JobRunner(store, launcher, control, active, envelope_for=envelope_for, cancel_for=cancel_for,
                     overran=lineage_overrun_reader(sink),
                     verify=VerifyStage(control, active, verify_runtime, store_dir=store.directory), stager=stager)


def _parser(host: str) -> Any:
    import argparse
    from levain.slot.layout import RUN, dispatch_store_dir
    ap = argparse.ArgumentParser(prog="python -I -m levain.dispatch.run_jobs",
                                 description="the dispatch runner: signed jobs to running, verified runs; runs as "
                                             "_levain_dispatch.")
    ap.add_argument("--store-dir", default=str(dispatch_store_dir(host)), help="the dispatch job store's directory")
    ap.add_argument("--socket", default=str(RUN[host] / "control" / "control.sock"), help="the socket CONTROL serves")
    ap.add_argument("--interval", type=float, default=TICK_S, help="seconds between ticks")
    return ap


def main(argv: "list[str] | None" = None, *, build: Callable[..., JobRunner] | None = None,
         wait: Callable[[float], bool] | None = None, max_ticks: int | None = None) -> int:
    """Tick until SIGTERM/SIGINT. One runner per store: it holds ``<store-dir>/run.lock`` and exits
    :data:`EX_INSTANCE_HELD` when another does. Never as root (CONTROL answers the mint verbs to the ``_levain_dispatch``
    peer only). ``build`` (default :func:`build_runner`), ``wait`` (True when stopped during it) and ``max_ticks`` are the
    seams a test drives it through. A tick that raises is logged by its type and the loop goes on: no event or error is
    authority, and the next tick reconciles from the durable facts."""
    import signal
    import sys
    import threading
    if os.geteuid() == 0:
        print("levain dispatch runner: refusing to run as root (it runs as _levain_dispatch)", file=sys.stderr)
        return EX_USAGE
    host = "darwin" if sys.platform == "darwin" else "linux"
    ap = _parser(host)
    a = ap.parse_args(sys.argv[1:] if argv is None else list(argv))
    for flag, v in (("--store-dir", a.store_dir), ("--socket", a.socket)):
        if not os.path.isabs(v):
            ap.error(f"{flag} must be an absolute path")
    if not a.interval > 0:
        ap.error("--interval must be positive")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format=f"levain dispatch runner[{os.getpid()}]: %(name)s: %(message)s")
    store = JobStore(a.store_dir)
    store._check_directory()
    fd = take_instance_lock(a.store_dir)
    if fd is None:
        print(f"levain dispatch runner: another runner holds {os.path.join(a.store_dir, LOCK_NAME)}", file=sys.stderr)
        return EX_INSTANCE_HELD
    stopped = threading.Event()
    old: dict[int, Any] = {}
    try:
        runner = (build or build_runner)(store, a.socket, host)
        stager = getattr(runner, "stager", None)
        if stager is not None:
            stager.sweep()          # under run.lock (the sweep's precondition): nothing survives a restart
        for s in (signal.SIGTERM, signal.SIGINT):
            old[s] = signal.signal(s, lambda _n, _f: stopped.set())     # records the request only; the loop acts on it
        sleep = wait if wait is not None else stopped.wait
        ticks = 0
        while not stopped.is_set():
            try:
                for s in runner.tick():
                    if s.action not in ("waiting", "skipped"):
                        log.info("job %s: %s %s %s", getattr(s.job, "id", "?"), s.action, s.cause or "", s.detail)
            except Exception as exc:  # noqa: BLE001 - the next tick reconciles from the durable facts
                log.warning("a tick failed: %s", type(exc).__name__)
            ticks += 1
            if (max_ticks is not None and ticks >= max_ticks) or sleep(a.interval):
                break
        return 0
    finally:
        for s, h in old.items():
            signal.signal(s, signal.SIG_DFL if h is None else h)
        os.close(fd)


if __name__ == "__main__":
    import sys
    from levain.home_guard import mark_live
    mark_live()
    sys.exit(main())
