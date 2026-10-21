"""levain.qbroker.grants — whether a broker-filed request is allowed by its run's signed contract (F4, W2-c2).

A dispatch executor's hook may send, beside a REQUEST's ``request``, a ``grant`` member ``{paths, input}``: the paths
the call touches and the sha256 of its input. :func:`grant_member_refusal` recomputes the ``pb1:`` digest from them
(:func:`levain.dispatch.grant.input_bound_digest`, one rule) and refuses a member that is not the sealed request's
``tool_input_digest``, so paths a request did not bind are never read. Then :func:`decide`, in this order, any
failure being a :class:`~levain.dispatch.grant.Queue` (the request is filed as any other, never an error):

1. the session is ``dispatch`` (CONTROL's word, never the hook's);
2. the signed contract dispatch published (:mod:`levain.dispatch.published`) at ``<contracts>/<digest hex>.json`` is
   read as a file the suspect principal wrote (:func:`read_published`: no symlink through a directory fd, a regular
   file owned by dispatch's uid, at most :data:`PUBLISHED_MAX` bytes, canonical JSON of that format);
3. its name and ``digest`` are the run's ``contract_digest`` (CONTROL's), the contract text parses as a contract
   (:func:`levain.dispatch.contract.parse_contract`, the function ``jobs.py`` stores and reads contracts with) whose
   recomputed digest is that one, ``payload.job_id`` is the run's ``job_id``, the job's and the contract's expiry are
   one and in the future;
4. :func:`levain.dispatch.sign.refusal` passes with an authority built from the answer-signer snapshot's entries that
   list :data:`CONTRACT_NAMESPACE` ONLY (:func:`contract_authority`; each fingerprint computed from its ``key_b64``);
5. a Codex call queues unless its ``cwd`` is the run's ``repo_root`` (Codex joins patch headers to ITS cwd);
6. :func:`levain.dispatch.grant.paths_decision` with root = the run's ``repo_root`` (from CONTROL).

Known edge (CONTRACT §26 S1-b): the job's ``store_id``, ``filed_at`` and ``unsigned_lease_end`` are dispatch's
unsigned word, so dispatch can replay a contract the operator signed for that ``job_id`` until its ``expires_at``;
dispatch is already the principal that runs that job. Stdlib only.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from levain.authority import AuthorityKey, AuthoritySnapshot, StandingPolicy, VerifierUnavailable
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.record import ExternalGateRequest
from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse
from levain.dispatch import DispatchError
from levain.dispatch import published
from levain.dispatch import sign as dsign
from levain.dispatch.cli import _CONTRACT_MAX
from levain.dispatch.contract import ResolvedContract, parse_contract
from levain.dispatch.grant import DIGEST_PREFIX, Allow, Queue, input_bound_digest, paths_decision
from levain.qbroker.signers import SignersSnapshot

__all__ = ["CONTRACT_NAMESPACE", "GRANT_KEYS", "PUBLISHED_MAX", "Granted", "contract_authority", "decide",
           "grant_member_refusal", "read_published", "verified_contract"]

CONTRACT_NAMESPACE = dsign.DISPATCH_SIGN_NAMESPACE
# CONTRACT §26 S1-b: the contract text is at most what the dispatch door accepts; 64 KiB more holds the payload, the
# signature (a passkey's included) and the job fields
PUBLISHED_MAX = _CONTRACT_MAX + 64 * 1024
GRANT_KEYS = frozenset({"paths", "input"})
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CODEX = "codex"


@dataclass(frozen=True)
class Granted:
    """The grant holds: :class:`~levain.dispatch.grant.Allow`'s paths and the contract signer it rests on."""
    paths: tuple[str, ...]
    contract_digest: str
    signer_fp: str


@dataclass(frozen=True)
class _PublishedJob:
    """The published record's job, as :func:`levain.dispatch.sign.refusal` reads a job."""
    store_id: str
    id: str
    digest: str
    expires_at: str
    filed_at: str
    unsigned_lease_end: str


class _Unreadable(Exception):
    pass


def grant_member_refusal(request: ExternalGateRequest, member: Any) -> str | None:
    """``None`` when ``member`` is ``{paths, input}`` whose ``pb1:`` digest under the request's tool is the request's
    ``tool_input_digest``; else the refusal line (the broker answers ``grant-digest-mismatch``)."""
    if not isinstance(member, Mapping) or set(member) != GRANT_KEYS:
        return "the grant member is not a {paths, input} record"
    paths, input_sha = member["paths"], member["input"]
    if not isinstance(paths, list) or not isinstance(input_sha, str) or _HEX64.fullmatch(input_sha) is None:
        return "the grant member's paths are a list and its input 64 lowercase hex"
    if not request.tool_input_digest.startswith(DIGEST_PREFIX):
        return "the request's digest is not a path-bound digest"
    try:
        digest = input_bound_digest(request.tool_name, paths, input_sha)
    except ValueError as exc:
        return f"the grant member does not bind: {exc}"
    if digest != request.tool_input_digest:
        return "the grant member's digest is not the request's"
    return None


def contract_authority(snapshot: SignersSnapshot) -> AuthoritySnapshot:
    """The snapshot's signers that list :data:`CONTRACT_NAMESPACE`, as the authority
    :func:`levain.dispatch.sign.refusal` checks a contract signature against (an answer-only key is absent). Each
    fingerprint is the snapshot's own, computed from the entry's ``key_type`` and ``key_b64``; every key is active
    (a removed signer is absent from the snapshot)."""
    keys = {fp: AuthorityKey(fingerprint=fp, public_key=s.public_key, key_class=s.key_class, enrolled_seq=1,
                             custody=s.custody)
            for fp, s in snapshot.signers.items() if CONTRACT_NAMESPACE in s.namespaces}
    return AuthoritySnapshot(store_id=f"qbroker-signers-{snapshot.generation}", seq=snapshot.generation,
                             head_hash="", keys=MappingProxyType(keys), policy=StandingPolicy())


def read_published(directory: Path, digest: str, *, owner_uid: int) -> dict[str, Any]:
    """The published record for ``digest``, read as untrusted bytes: ``directory`` and the file opened without
    following a link (the file relative to the directory's fd), a regular file owned by ``owner_uid``, at most
    :data:`PUBLISHED_MAX` bytes, canonical JSON of :data:`levain.dispatch.published.FORMAT`'s shape. Raises
    ``_Unreadable`` saying why."""
    try:
        name = published.file_name(digest)
    except ValueError as exc:
        raise _Unreadable(str(exc)) from None
    try:
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _Unreadable(f"the contracts directory cannot be opened ({errno.errorcode.get(exc.errno, exc)})") from None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        except FileNotFoundError:
            raise _Unreadable("no signed contract is published for the run's digest") from None
        except OSError as exc:
            raise _Unreadable(f"the published contract cannot be opened without following a link "
                              f"({errno.errorcode.get(exc.errno, exc)})") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _Unreadable("the published contract is not a regular file")
            if st.st_uid != owner_uid:
                raise _Unreadable(f"the published contract is owned by uid {st.st_uid}, not dispatch's {owner_uid}")
            if st.st_size > PUBLISHED_MAX:
                raise _Unreadable(f"the published contract is over {PUBLISHED_MAX} bytes")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(PUBLISHED_MAX + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    if len(data) > PUBLISHED_MAX:
        raise _Unreadable(f"the published contract grew past {PUBLISHED_MAX} bytes")
    try:
        obj = parse(data)
    except ContractError as exc:
        raise _Unreadable(f"the published contract does not parse: {exc}") from None
    if canonical_bytes(obj) != data:
        raise _Unreadable("the published contract is not canonical JSON")
    problem = published.record_problem(obj)
    if problem is not None:
        raise _Unreadable(f"the published contract: {problem}")
    return obj


def verified_contract(directory: Path, run: Mapping[str, Any], snapshot: SignersSnapshot | None, now: datetime, *,
                      owner_uid: int, ssh_keygen: Path | str | None = None) -> tuple[ResolvedContract, str] | str:
    """``(contract, signer_fp)`` when the published contract for ``run`` verifies (steps 2-4 of the module's list),
    else the reason it does not."""
    digest = run.get("contract_digest")
    try:
        rec = read_published(directory, digest, owner_uid=owner_uid)
    except _Unreadable as exc:
        return str(exc)
    if rec["digest"] != digest:
        return "the published contract names another digest than the run's"
    try:
        contract = parse_contract(rec["contract"])
    except DispatchError as exc:
        return f"the published contract text: {exc}"
    if contract.digest != digest:
        return "the published contract text's digest is not the run's"
    job, payload = rec["job"], rec["payload"]
    if payload.get("job_id") != run.get("job_id") or job["job_id"] != run.get("job_id"):
        return "the published contract is signed for another job than the run's"
    if job["expires_at"] != contract.expires_at:
        return "the published job's expiry is not the contract's"
    try:
        if parse_stamp(contract.expires_at) <= now:
            return f"the contract expired {contract.expires_at}"
    except ConsentError:
        return "the contract's expiry is not a timestamp"
    if snapshot is None:
        return "the signer snapshot is missing or unverifiable"
    signable = _PublishedJob(store_id=job["store_id"], id=job["job_id"], digest=digest, expires_at=job["expires_at"],
                             filed_at=job["filed_at"], unsigned_lease_end=job["unsigned_lease_end"])
    try:
        why = dsign.refusal(payload, rec["signature"], contract_authority(snapshot), signable, ssh_keygen=ssh_keygen)
    except VerifierUnavailable as exc:
        return f"the contract signature cannot be checked on this machine: {exc}"
    if why is not None:
        return why
    return contract, payload["signer_fp"]


def decide(*, kind: str, run: Mapping[str, Any] | None, request: ExternalGateRequest, paths: list[str],
           directory: Path | None, owner_uid: int | None, snapshot: SignersSnapshot | None, now: datetime,
           ssh_keygen: Path | str | None = None) -> Granted | Queue:
    """The module's steps 1-6 for a request whose ``grant`` member :func:`grant_member_refusal` accepted. Never
    raises: anything that cannot be read queues."""
    try:
        if kind != "dispatch" or not isinstance(run, Mapping):
            return Queue("the session is not a dispatch session")
        if directory is None or owner_uid is None:
            return Queue("this broker reads no published contracts")
        got = verified_contract(directory, run, snapshot, now, owner_uid=owner_uid, ssh_keygen=ssh_keygen)
        if isinstance(got, str):
            return Queue(got)
        contract, signer_fp = got
        root = run.get("repo_root")
        if request.agent == _CODEX and request.cwd != root:
            return Queue("Codex reads patch paths against its cwd, and the cwd is not the run's worktree root")
        verdict = paths_decision(contract, request.agent, request.tool_name, paths, root=root)
        if isinstance(verdict, Allow):
            return Granted(paths=verdict.paths, contract_digest=run["contract_digest"], signer_fp=signer_fp)
        return verdict
    except Exception as exc:     # noqa: BLE001 - the fallback is the queue
        return Queue(f"the grant could not be decided: {type(exc).__name__}")


def grant_row(request_id: str, granted: Granted, now: datetime) -> dict[str, Any]:
    from levain.qbroker.store import GRANT_ROW_FORMAT

    return {"format": GRANT_ROW_FORMAT, "request_id": request_id, "contract_digest": granted.contract_digest,
            "paths": list(granted.paths), "signer_fp": granted.signer_fp, "at": utc_stamp(now)}
