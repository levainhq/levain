"""levain.dispatch.verify_stage: the dispatch runner's VERIFY stage (S14 R3) and the follower's production ``resolve``.

**The stage** (:class:`VerifyStage`, driven by :meth:`levain.dispatch.run_jobs.JobRunner.tick` for each ``verifying``
job). For each run ``i`` of the job whose executor artifact the follower took: mint a ``kind: verifier`` ticket through
CONTROL (``RUN-TICKET-MINT``; the fields are :func:`levain.dispatch.registry.verifier_ticket`'s, derived from the ACTIVE
verifier item and nowhere else; ``reserved_microdollars`` 0, ``auth`` brokered, ``egress_routes`` none; the
``input_bundle_digest`` is the taken bundle's) under a deterministic attempt id (:func:`verifier_attempt`), so a restart
replays the same mint and never makes a second ticket, then start its slot through the ``VerifierRuntime`` the caller
gives. A ticket CONTROL already holds ``bound``/``released`` is not started again; a ``cancelled``/``expired`` one is
named and left. Nothing here writes a verifier OUTCOME: that is the follower's ``record_verification``, from the
terminal event CONTROL emits.

**The start seam** (:class:`VerifierRuntime`, S14 A4). ``HelperSlotRuntime.start`` is the dispatch/egress shape only, so a
verifier session starts through :class:`levain.slot.dispatch_runtime.HelperVerifierRuntime`, which the production runner
builds. ``start(ticket, inputs)`` is handed the ticket and the :class:`VerifierInputs` (the taken bundle the ticket was
minted against, the entry's driver input, its steps and driver template): the steps and template from CONTROL's record
of the minted ticket (``RUN-TICKET-VERIFIER``), the driver input only when its digest is the ticket's.
A stage built with no runtime gets :class:`UnbuiltVerifierRuntime`, which refuses by name and starts nothing. A ticket
that closes (``cancelled`` or ``expired``) before binding is reported ``closed``; the runner then fails the job (D7).

**The resolve** (:class:`ControlResolve`, the follower's production ``resolve``). A verifier's terminal ``state`` event
names a ticket; the run it is, the entry and the steps it was minted with come from CONTROL's OWN record of that ticket
(``RUN-TICKET-VERIFIER``, one read-only verb added for this) and never from a file the runner writes. The ticket must
also be the one :func:`verifier_attempt` names for its job and run (``RUN-TICKET-STATUS``), so a verifier ticket another
party minted resolves nothing. The three things CONTROL does not hold, the artifact's TREE, the verifier's ENVELOPE bytes
and the executor's ANSWERED model, come from readers the caller injects (:class:`Readers`). An envelope reader that
answers nothing is passed on as ``envelope`` None, and the job store fails that job ``report-missing``; a tree reader that
answers nothing is passed on as ``tree`` None, and the job store fails that job ``verifier-tree-unavailable``. Either way
the event is acked and the follower moves on: a verifier infrastructure failure fails the job, never wedges the stream.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from levain.contracts.canon import canonical_bytes
from levain.dispatch import DispatchError, registry
from levain.dispatch.follow import VerifierCall
from levain.dispatch.jobs import Job, JobStore
from levain.dispatch.verifier_report import VerifierRun, contract_entry
from levain.slot import Refused

log = logging.getLogger("levain.dispatch.verify_stage")

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class VerifierInputs:
    """What the stage hands a runtime besides the ticket (S14 A4, design A D1 a): the taken bundle the ticket was
    minted against (``bundle_path`` and the ``bundle_digest`` its mint named), the entry's driver input as canonical
    bytes, and the two things the render blob names that the ticket also holds, the ``steps`` and the entry's
    ``driver_template``. The steps and the template are CONTROL's record of the minted ticket, and the driver input is
    handed on only when its digest is the ticket's (:meth:`VerifyStage._inputs`). No trust is added: root re-checks
    both digests (``input-bundle-mismatch``, ``driver-input-mismatch``)."""
    bundle_path: Path
    bundle_digest: str
    driver_input: bytes
    steps: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    driver_template: str = ""


class VerifierRuntime(Protocol):
    """Starts the slot of an already-minted verifier ticket (class ``none``) and lets it run. Failure-atomic like the
    executor's: a ``start`` that raises left no session live. It is handed the ticket (a bearer capability) and the
    :class:`VerifierInputs`, and keeps the ticket nowhere. Raises :class:`levain.slot.Refused` (or any ``name``-bearing
    refusal). A refusal named in :data:`PERMANENT_START_REFUSALS` is one no retry can change."""

    def start(self, ticket: str, inputs: VerifierInputs) -> None: ...


#: start refusals that name the INPUT, not the infrastructure: the same bundle would be refused again, so the stage
#: cancels the ticket and the next pass fails the job (D7), instead of retrying for ever
PERMANENT_START_REFUSALS = frozenset({"verifier-bundle-mismatch", "verifier-bundle-unreadable"})


class UnbuiltVerifierRuntime:
    """The runtime a stage built with no runtime gets: every start is refused ``verifier-start-unbuilt``. Nothing is
    started, nothing is faked. (The production runner no longer uses it: ``run_jobs.build_runner`` defaults to
    :class:`levain.slot.dispatch_runtime.HelperVerifierRuntime`.)"""

    def start(self, ticket: str, inputs: VerifierInputs) -> None:
        raise Refused("verifier-start-unbuilt", "no verifier runtime was given to the stage")


def verifier_attempt(job_id: str, run_index: int) -> str:
    """The deterministic attempt id of run ``run_index``'s verifier ticket of ``job_id``: ``dv-`` and 32 hex of the sha256
    of ``"<job id>:<index>:verifier"``. The same job and run always name the same attempt, so a replayed mint is CONTROL's
    idempotent answer and a restart never makes a second verifier ticket."""
    return "dv-" + hashlib.sha256(f"{job_id}:{run_index}:verifier".encode("utf-8")).hexdigest()[:32]


def mint_args(job: Job, run_index: int, active: Mapping[str, Any], input_bundle_digest: str) -> dict[str, Any]:
    """RUN-TICKET-MINT's fields for run ``run_index``'s verifier ticket: the ticket's kind and its fixed shape, the
    registry's derived verifier fields (the ACTIVE item's), the job's digest and the taken bundle's. Raises
    :class:`DispatchError` when the verifier does not resolve, and ``ValueError`` for a bundle digest that is no digest."""
    if not isinstance(input_bundle_digest, str) or _DIGEST.fullmatch(input_bundle_digest) is None:
        raise ValueError("the taken bundle's digest is not sha256:<64 hex>")
    v = registry.verifier_ticket(active, verifier_id=job.contract.template.verifier_id)
    return {"attempt_id": verifier_attempt(job.id, run_index), "kind": "verifier", "job_id": job.id,
            "run_index": run_index, "contract_digest": job.digest, "egress_routes": [], "reserved_microdollars": 0,
            "auth": "brokered", "verifier_generation": v["verifier_generation"], "verifier_id": v["verifier_id"],
            "verifier_steps": v["verifier_steps"], "driver_input_digest": v["driver_input_digest"],
            "input_bundle_digest": input_bundle_digest}


@dataclass(frozen=True)
class VerifyAct:
    """What one pass did for one run: ``action`` is ``minted``, ``started``, ``waiting`` (the ticket is bound or
    released: a session exists), ``closed`` (cancelled or expired: left, named), or ``refused`` (``name`` says why)."""
    run: int
    action: str
    name: str = ""


class VerifyStage:
    """``control`` is the dispatch-peer CONTROL port (``call(op, **fields)``, a refusal raises ``Refused``); ``active``
    the active config items; ``runtime`` a :class:`VerifierRuntime`."""

    def __init__(self, control: Any, active: Callable[[], Mapping[str, Any]], runtime: VerifierRuntime | None = None,
                 store_dir: "Path | str | None" = None) -> None:
        self.control, self.active, self.store_dir = control, active, store_dir
        self.runtime: VerifierRuntime = runtime if runtime is not None else UnbuiltVerifierRuntime()
        self._started: set[str] = set()

    def _bundle_path(self, taken: Any) -> Path:
        rel = taken.path
        if not isinstance(rel, str) or not rel:
            raise Refused("no-bundle", "the taken artifact names no bundle")
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise Refused("bundle-path-unsafe", "the taken bundle's path leaves the store")
        return Path(rel) if self.store_dir is None else Path(self.store_dir) / rel

    def _inputs(self, job: Job, taken: Any, ticket: str, entry_driver_input: Optional[bytes]) -> VerifierInputs:
        """The inputs of one start, from the MINTED TICKET's own record (S14 fix 2, design A): its steps and its driver
        template are CONTROL's record of the ticket (``RUN-TICKET-VERIFIER``: the facts and the entry it resolved at the
        mint), never the active item read again at start, so a configuration change between the mint and the start
        cannot hand root steps or a template the ticket does not name. CONTROL keeps only the driver input's DIGEST, so
        the bytes are ``entry_driver_input`` when the caller derived them, else the active entry's canonical bytes, and
        either is handed on only when its sha256 is the ticket's ``driver_input_digest``; any other is refused
        ``verifier-driver-input-moved`` before the runtime is asked (root's own ``driver-input-mismatch`` stays the
        check that counts)."""
        ans = self.control.call("RUN-TICKET-VERIFIER", ticket=ticket)
        facts = ans.get("facts") if isinstance(ans, Mapping) else None
        resolved = ans.get("verifier") if isinstance(ans, Mapping) else None
        if not isinstance(facts, Mapping) or not isinstance(resolved, Mapping) or facts.get("kind") != "verifier":
            raise Refused("verifier-ticket-unreadable", "CONTROL's record of the ticket is not a verifier ticket's")
        steps, want, template = facts.get("verifier_steps"), facts.get("driver_input_digest"), \
            resolved.get("driver_template")
        if not isinstance(steps, list) or not isinstance(want, str) or _DIGEST.fullmatch(want) is None \
                or not isinstance(template, str) or not template:
            raise Refused("verifier-ticket-unreadable", "the ticket's steps, driver input digest or template is missing")
        if entry_driver_input is not None:
            driver_input = entry_driver_input
        else:
            v = registry.verifier_ticket(self.active(), verifier_id=job.contract.template.verifier_id)
            driver_input = canonical_bytes(v["entry"]["driver_input"])
        if "sha256:" + hashlib.sha256(driver_input).hexdigest() != want:
            raise Refused("verifier-driver-input-moved", "the driver input at hand is not the one the ticket was minted "
                                                         "with")
        return VerifierInputs(self._bundle_path(taken), taken.bundle_digest, driver_input, list(steps), template)

    def run(self, job: Job, run_index: int, taken: Any, entry_driver_input: Optional[bytes] = None) -> VerifyAct:
        """Mint (once) and start (once) the verifier ticket of one run. ``taken`` is the run's taken artifact (its
        bundle is what is verified; None or one with no bundle mints nothing). Never raises for a refusal: it is
        named."""
        bundle_digest = getattr(taken, "bundle_digest", None)
        attempt = verifier_attempt(job.id, run_index)
        try:
            status = self.control.call("RUN-TICKET-STATUS", attempt_id=attempt)
        except Refused as exc:
            if exc.name != "attempt-unknown":
                return VerifyAct(run_index, "refused", exc.name)
            status = None
        if status is None:
            if bundle_digest is None:
                return VerifyAct(run_index, "refused", "no-bundle")      # an artifact with no bundle has nothing to verify
            try:
                args = mint_args(job, run_index, self.active(), bundle_digest)
                ans = self.control.call("RUN-TICKET-MINT", **args)
            except (DispatchError, ValueError) as exc:
                return VerifyAct(run_index, "refused", f"verifier-unresolved:{type(exc).__name__}")
            except Refused as exc:
                return VerifyAct(run_index, "refused", exc.name)
            ticket, state = ans.get("ticket"), "unbound"
            if not isinstance(ticket, str) or not ticket:
                return VerifyAct(run_index, "refused", "mint-no-ticket")   # CONTROL's mint answered no handle: named
            minted = True
        else:
            ticket, state = status.get("ticket"), status.get("state")
            minted = False
        if state != "unbound" and isinstance(ticket, str):
            # no longer startable here (bound, released, cancelled, expired): the set holds only the window between
            # this process's start and the lease's bind, so it never grows past the tickets still unbound
            self._started.discard(ticket)
        if state in ("bound", "released"):
            return VerifyAct(run_index, "waiting")
        if state != "unbound" or not isinstance(ticket, str) or not ticket:
            return VerifyAct(run_index, "closed", str(state)[:32])
        if ticket in self._started:
            # started by THIS process and not yet bound by the lease (CONTROL binds at ATTACH): never started twice here.
            # A restarted process holds no such set and asks the runtime again; the lease refuses a held ticket.
            return VerifyAct(run_index, "waiting")
        if bundle_digest is None:
            return VerifyAct(run_index, "refused", "no-bundle")
        try:
            inputs = self._inputs(job, taken, ticket, entry_driver_input)
            self.runtime.start(ticket, inputs)
            self._started.add(ticket)
        except (DispatchError, ValueError) as exc:
            return VerifyAct(run_index, "refused", f"verifier-unresolved:{type(exc).__name__}")
        except Refused as exc:
            if exc.name in PERMANENT_START_REFUSALS:
                self._close_ticket(ticket)
            return VerifyAct(run_index, "refused", exc.name)
        except Exception as exc:  # noqa: BLE001 - a start that raised left no session; name it and retry next pass
            log.warning("job %s run %d: the verifier start raised %s", job.id, run_index, type(exc).__name__)
            return VerifyAct(run_index, "refused", f"start-failed:{type(exc).__name__}")
        return VerifyAct(run_index, "started" if not minted else "minted")


    def _close_ticket(self, ticket: str) -> None:
        """Cancel a ticket whose start is refused for good: the next pass reads it closed, and the runner fails the
        job (D7). A refused cancel is left for the next pass (the start is refused again, and so is the cancel)."""
        try:
            self.control.call("RUN-TICKET-CANCEL", ticket=ticket)
        except Exception as exc:  # noqa: BLE001 - named in the log, never raised from a stage pass
            log.warning("cancelling a verifier ticket refused for good failed: %s", getattr(exc, "name", type(exc).__name__))


@dataclass(frozen=True)
class Readers:
    """What CONTROL does not hold, injected: ``tree(job, run)`` the run's artifact tree (a full object id) or None;
    ``envelope(session, ticket)`` the verifier's report envelope bytes or None; ``answered(job, run)`` the model the
    executor's provider answered with, or None. Each may raise; a raise resolves nothing. ``envelope``'s None fails the
    job ``report-missing`` (passed on, never a wedge); ``answered``'s is optional (D5: the record then holds no answering
    model); ``tree``'s fails the job ``verifier-tree-unavailable`` (passed on, never a wedge)."""
    tree: Callable[[Job, int], Optional[str]]
    envelope: Callable[[str, str], Optional[bytes]]
    answered: Callable[[Job, int], Optional[str]]


class ControlResolve:
    """The follower's production ``resolve``: a terminal verifier event -> its :class:`VerifierCall`, the run's identity,
    entry and steps from CONTROL's own record of the event's ticket. Returns None (the follower stops
    ``ticket-unresolved``, unacked) for a ticket CONTROL does not hold as a verifier ticket, one that is not the attempt
    :func:`verifier_attempt` names for its job and run, a job the store does not hold, or any reader that raises or
    answers a value of the wrong type (an envelope other than None or bytes-like, a tree or answered other than None or
    str; a bytearray or memoryview envelope is normalised to bytes). An envelope reader's None and a tree reader's None are passed on (the store fails
    the job ``report-missing`` / ``verifier-tree-unavailable``, and the follower acks). Never raises."""

    def __init__(self, port: Any, jobs: JobStore, readers: Readers) -> None:
        self.port, self.jobs, self.readers = port, jobs, readers

    def __call__(self, event: Mapping[str, Any]) -> Optional[VerifierCall]:
        try:
            return self._resolve(event)
        except Exception as exc:  # noqa: BLE001 - unreachable CONTROL or a broken reader: unresolved, never a guess
            log.warning("a verifier event resolves nothing: %s", getattr(exc, "name", type(exc).__name__))
            return None

    def _resolve(self, event: Mapping[str, Any]) -> Optional[VerifierCall]:
        ticket, session = event.get("ticket"), event.get("session")
        if not isinstance(ticket, str) or not ticket or not isinstance(session, str) or not session:
            return None
        ans = self.port.call("RUN-TICKET-VERIFIER", ticket=ticket)
        facts, entry = ans.get("facts"), ans.get("verifier")
        if not isinstance(facts, Mapping) or not isinstance(entry, Mapping) or facts.get("kind") != "verifier":
            return None
        # CONTROL's resolved entry also names the generation (RESOLVED_KEYS); the run carries the contract's seven only
        # (`verifier_report.contract_entry`), which the envelope's `verifier` must equal byte for byte
        job_id, run = facts.get("job_id"), facts.get("run_index")
        if not isinstance(job_id, str) or type(run) is not int:
            return None
        # the ticket must be the one dispatch's runner minted for this job and run: CONTROL's own attempt index says so
        mine = self.port.call("RUN-TICKET-STATUS", attempt_id=verifier_attempt(job_id, run))
        if mine.get("ticket") != ticket:
            return None
        job = self.jobs.get(job_id)
        if job is None or facts.get("contract_digest") != job.digest:
            return None
        vrun = VerifierRun(job_id, run, session, ticket, contract_entry(entry), list(facts["verifier_steps"]),
                           str(facts["driver_input_digest"]), str(facts["input_bundle_digest"]))
        if "verifier_report_refused" in event:
            # a refused event is judged on its name alone: no envelope, no tree, no model is read for it
            return VerifierCall(vrun, None, job.repo, "0" * 40, {})
        envelope = self.readers.envelope(session, ticket)
        tree = self.readers.tree(job, run)
        answered = self.readers.answered(job, run)
        # a bytes-like envelope (bytearray, memoryview) is the same bytes: normalised, never a wedge; any other type, and a
        # tree or answered reader's non-str, resolves nothing (the docstring's "a value of the wrong type")
        if isinstance(envelope, (bytearray, memoryview)):
            envelope = bytes(envelope)
        if envelope is not None and not isinstance(envelope, bytes):
            return None
        if tree is not None and not isinstance(tree, str):
            return None
        if answered is not None and not isinstance(answered, str):
            return None
        # an envelope reader that answers None (absent, retention-expired, a link, a fifo, over the cap) is passed on as
        # None: the store's envelope check fails the job `report-missing` and the follower acks, so one bad envelope
        # never wedges the events behind it (S14 L3 F1). A tree reader's None (the production one until A6) is passed on
        # the same way: the store fails the job `verifier-tree-unavailable` and the follower acks (S14 fix 1)
        # D5: the answering model is optional; a run whose lineage names none records "requested; answering model unknown"
        return VerifierCall(vrun, envelope, job.repo, tree, {"answered": answered} if answered else {})


__all__ = ["ControlResolve", "Readers", "PERMANENT_START_REFUSALS", "UnbuiltVerifierRuntime", "VerifierInputs",
           "VerifierRuntime", "VerifyAct", "VerifyStage",
           "mint_args", "verifier_attempt"]
