"""levain.dispatch.run_workspace: the dispatch runner's workspace, built from the job's signed read set (S14 design B).

A dispatch executor reads exactly the contract's signed read list and nothing else: not the operator's repo, not its
history, not its agent-context files unless the filer listed them. The operator-side filing door cut that read set
into one parentless commit ``E`` and put it in the job's handoff bundle (``readset.export_readset``). This module is
the run side of it: the runner verifies the handoff BEFORE it takes the job (:meth:`ReadSetStager.verify`), holds
the verified set under the run's key (:meth:`register`), and the slot runtime's ``workspace`` seam
(:meth:`ReadSetStager.stage`) re-cuts the slot's ``repo.bundle`` from it. A start whose key holds no verified set
stages nothing and is refused (``workspace-unregistered``), so the runtime ENDs the session.

Runner-side only: it never imports ``levain.dispatch.stager`` (the operator's sender) and reads no operator path.
Nothing here survives a restart: :meth:`sweep`, under the runner's own lock, removes every workdir a crash left.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from typing import Callable

from levain.agents.session_launcher import LauncherRefused
from levain.dispatch import readset as RS
from levain.slot.workspace import _TRANSIENT_READSET, refused_name_carried

__all__ = ["ReadSetStager", "attempt_name"]

_log = logging.getLogger(__name__)


def attempt_name() -> str:
    """A fresh attempt id (``dv-`` + 64 random bits) for one verification, so a retry never meets an old workdir."""
    return "dv-" + secrets.token_hex(8)


class ReadSetStager:
    """Verify a job's handoff into a workdir under ``parent`` and stage slots from the verified set.

    ``handoff_row`` is the job store's ``handoff_row(job_id) -> (bundle_id, path, size) | None``."""

    def __init__(self, parent: str, handoff_row: Callable[[str], "tuple[str, str, int] | None"]) -> None:
        self.parent = parent
        self._row = handoff_row
        self._mu = threading.Lock()
        self._held: dict[str, RS.VerifiedReadSet] = {}
        self._contracts: dict[str, bytes] = {}      # attempt id -> the contract bytes it was verified against

    # -- before the take -------------------------------------------------------------------------------------------------

    def verify(self, job) -> RS.VerifiedReadSet:
        """The full verify of ``job``'s handoff against its stored contract, or :class:`levain.dispatch.readset.ReadSetRefused`
        (nothing is left on disk by a refusal). The caller owns the result and must :meth:`discard` it."""
        row = self._row(job.id)
        if row is None:
            raise RS.ReadSetRefused("readset-handoff", f"job {job.id} has no handoff bundle")
        _bid, path, size = row
        contract_bytes = job.contract_text.encode("utf-8")
        fd = RS.open_handoff(path, size=size)
        try:
            v = RS.verify_readset(contract_bytes, job.digest, fd, self.parent, attempt_name())
        finally:
            os.close(fd)
        try:
            self._refused_names(v)
        except BaseException:
            RS.discard(v)
            raise
        with self._mu:
            self._contracts[v.attempt] = contract_bytes
        return v

    @staticmethod
    def _refused_names(v: RS.VerifiedReadSet) -> None:
        """``readset-refused-name`` when the verified commit's root tree carries a name the W1 upload refuses
        (``DISPATCH_REFUSED_NAMES``): the one constant and the one derivation (``refused_name_carried``), so the two
        doors cannot disagree. Refused, never stripped."""
        out = RS._git(os.path.join(v.workdir, RS._P), "ls-tree", "-z", "--name-only", v.commit, env=RS._p_env())
        names = [n.decode("utf-8", "replace") for n in out.split(b"\0") if n]
        carried = refused_name_carried([(n,) for n in names])
        if carried is not None:
            raise RS.ReadSetRefused("readset-refused-name", carried[1])

    def prefix_problem(self, job) -> "str | None":
        """The cheap pre-take check (``header`` / ``pack`` / ``None``); a handoff that cannot be opened is refused."""
        row = self._row(job.id)
        if row is None:
            raise RS.ReadSetRefused("readset-handoff", f"job {job.id} has no handoff bundle")
        fd = RS.open_handoff(row[1], size=row[2])
        try:
            return RS.handoff_prefix_problem(fd, job.contract_text.encode("utf-8"), job.digest)
        finally:
            os.close(fd)

    # -- the registry ----------------------------------------------------------------------------------------------------

    def register(self, key: str, v: RS.VerifiedReadSet) -> None:
        with self._mu:
            self._held[key] = v

    def unregister(self, key: str) -> None:
        with self._mu:
            self._held.pop(key, None)

    def _lookup(self, req) -> RS.VerifiedReadSet:
        with self._mu:
            v = self._held.get(getattr(req, "session_key", None))
        if v is None:
            raise LauncherRefused("workspace-unregistered", "no verified read set is held for this start")
        return v

    # -- the runtime's seams ---------------------------------------------------------------------------------------------

    def stage(self, req, staging) -> None:
        """``HelperSlotRuntime``'s ``workspace`` callable: cut ``staging/repo.bundle`` from the verified set held under
        ``req.session_key``. Any refusal is a :class:`LauncherRefused`; host faults are transient, everything else
        standing."""
        v = self._lookup(req)
        with self._mu:
            contract_bytes = self._contracts.get(v.attempt)
        if contract_bytes is None:
            raise LauncherRefused("workspace-unregistered", "the verified read set's contract is not held")
        try:
            RS.bundle_verified(v, contract_bytes, v.digest, str(staging))
        except RS.ReadSetRefused as exc:
            cls = "transient" if exc.name in _TRANSIENT_READSET else "standing"
            raise LauncherRefused(exc.name, getattr(exc, "detail", "") or str(exc), reason_class=cls) from None

    def repo_settings(self, req):
        """``HelperSlotRuntime``'s ``repo_settings`` callable: the settings layers frozen at verify."""
        return self._lookup(req).repo_settings

    # -- cleanup ---------------------------------------------------------------------------------------------------------

    def discard(self, v: RS.VerifiedReadSet) -> None:
        with self._mu:
            self._contracts.pop(v.attempt, None)
        RS.discard(v)

    def sweep(self) -> RS.SweepReport:
        """Remove every workdir under ``parent``. Only under the runner's exclusive run lock (the sweep's precondition),
        where no launch is mid-flight and nothing survives a restart."""
        return RS.sweep_workdirs(self.parent, keep=frozenset())
