"""levain.dispatch.published — the signed contract dispatch publishes for the queue broker (CONTRACT §26 S1-b).

When the dispatch control store ACCEPTS the operator's signature over a contract (:meth:`levain.dispatch.jobs.JobStore.sign`),
the store writes ``<directory>/<digest hex>.json``: the canonical :data:`FORMAT` record ``{format, digest, contract,
payload, signature, job: {store_id, job_id, filed_at, unsigned_lease_end, expires_at}}`` (temp + fsync + rename +
directory fsync, mode 0640; on the real layout the directory is ``L/contracts/``, ``_levain_dispatch : levain-queue``
2750, so the file takes the group). It is removed once the job is terminal. :func:`sync` makes the directory hold
exactly the live signed jobs' records, so a crash between the signature's commit and the write, or between a job
ending and the unlink, is repaired by the store's next operation.

The broker trusts none of it: the file is written by the suspect principal, and the broker re-checks every field and
the signature itself (:mod:`levain.qbroker.grants`). :func:`record_problem` is the shape check both sides share, so
what dispatch writes and what the broker reads are one format. Stdlib only.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse

__all__ = ["FORMAT", "JOB_KEYS", "KEYS", "MODE", "file_name", "record", "record_problem", "sync"]

FORMAT = "levain-dispatch-published-contract/1"
KEYS = frozenset({"format", "digest", "contract", "payload", "signature", "job"})
JOB_KEYS = frozenset({"store_id", "job_id", "filed_at", "unsigned_lease_end", "expires_at"})
MODE = 0o640
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
_NAME = re.compile(r"[0-9a-f]{64}\.json")


def file_name(digest: str) -> str:
    """``<digest hex>.json`` for a ``sha256:<hex>`` digest; ValueError for anything else."""
    m = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if m is None:
        raise ValueError("a contract digest is sha256: + 64 lowercase hex")
    return f"{m.group(1)}.json"


def record(*, digest: str, contract: str, payload: Mapping[str, Any], signature: str, store_id: str, job_id: str,
           filed_at: str, unsigned_lease_end: str, expires_at: str) -> dict[str, Any]:
    return {"format": FORMAT, "digest": digest, "contract": contract, "payload": dict(payload),
            "signature": signature,
            "job": {"store_id": store_id, "job_id": job_id, "filed_at": filed_at,
                    "unsigned_lease_end": unsigned_lease_end, "expires_at": expires_at}}


def record_problem(obj: Any) -> str | None:
    """Why ``obj`` is not a :data:`FORMAT` record's shape (exact keys, text fields, a payload object); ``None`` when
    it is. Shape only: what the fields mean is the reader's to check."""
    if not isinstance(obj, dict) or set(obj) != KEYS:
        return "not a published-contract record"
    if obj["format"] != FORMAT:
        return f"format {str(obj['format'])[:60]!r} is not {FORMAT!r}"
    if not all(isinstance(obj[k], str) for k in ("digest", "contract", "signature")):
        return "digest, contract and signature are text"
    if not isinstance(obj["payload"], dict):
        return "the payload is an object"
    job = obj["job"]
    if not isinstance(job, dict) or set(job) != JOB_KEYS or not all(isinstance(v, str) for v in job.values()):
        return "job is a {store_id, job_id, filed_at, unsigned_lease_end, expires_at} record of text"
    return None


def sync(directory: Path, live: Iterable[Mapping[str, Any]]) -> None:
    """Make ``directory`` hold exactly one record per ``live`` record (by its digest): write each one missing or
    different (:func:`levain.qbroker.store.replace_file`), unlink every other ``<64 hex>.json``. Other names are left
    alone. Raises OSError; the directory is opened without following a link."""
    from levain.qbroker.store import replace_file

    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        present = {n for n in os.listdir(fd) if _NAME.fullmatch(n)}
    finally:
        os.close(fd)
    wanted: dict[str, bytes] = {}
    for rec in live:
        problem = record_problem(dict(rec))
        if problem is not None:
            raise ValueError(f"a live record is not publishable: {problem}")
        wanted[file_name(rec["digest"])] = canonical_bytes(dict(rec))
    for name, data in sorted(wanted.items()):
        path = directory / name
        if name in present and _current(path) == data:
            continue
        replace_file(path, data, MODE)
    for name in sorted(present - set(wanted)):
        try:
            os.unlink(directory / name)
        except FileNotFoundError:
            pass


def _current(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read()
    finally:
        os.close(fd)
    try:
        parse(data)
    except ContractError:
        return None
    return data
