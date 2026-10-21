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
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse

__all__ = ["FORMAT", "JOB_KEYS", "KEYS", "MODE", "TMP_STALE", "file_name", "record", "record_problem", "sync"]

FORMAT = "levain-dispatch-published-contract/1"
KEYS = frozenset({"format", "digest", "contract", "payload", "signature", "job"})
JOB_KEYS = frozenset({"store_id", "job_id", "filed_at", "unsigned_lease_end", "expires_at"})
MODE = 0o640
# a temp name of levain.qbroker.store.replace_file older than this is a crashed write's, removed by sync; a younger
# one may be another process's write in flight
TMP_STALE = 60.0
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
_NAME = re.compile(r"[0-9a-f]{64}\.json")
_TMP = re.compile(r"\.tmp-[0-9]+-[0-9a-f]{16}")


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


def sync(directory: Path, live: Iterable[Mapping[str, Any]], *, now: float | None = None) -> list[str]:
    """Make ``directory`` hold exactly one record per ``live`` record (by its digest), removals FIRST: unlink every
    other ``<64 hex>.json`` and every temp name of :func:`levain.qbroker.store.replace_file` older than
    :data:`TMP_STALE` seconds (relative to the directory's fd, then one directory fsync), then write each record
    missing or different. Each removal and each write runs in its own guard, so one failure stops nothing else; a
    record whose final bytes are over the broker's read bound (:data:`levain.qbroker.grants.PUBLISHED_MAX`, the
    contract text's JSON escaping included) or whose shape is wrong is never written (an old file at its name is
    removed). Returns one line per failure (empty when the directory is exact); other names are left alone. Raises
    OSError only when the directory cannot be opened (never following a link) or listed.

    Known edge (A, the L3 r1 triage): a removal that fails leaves a terminal job's record in place; the job's terminal
    state is committed all the same, because the operator's stop outranks this mirror, so a cancel is never refused
    for a file that could not be removed. The record is retried by the store's next operation and bounded by the
    contract's ``expires_at`` (at most a day), which the broker checks.
    Known edge (B): publishing is not serialised across processes, so a sync from a snapshot read before a cancel can
    rewrite the cancelled job's record after that cancel's sync removed it. Dispatch alone writes this directory and
    is the principal that runs the job, which could keep such a record by intent (CONTRACT §26), so no lock was added;
    the next store operation removes it, and ``expires_at`` bounds it."""
    from levain.qbroker.grants import PUBLISHED_MAX   # here: grants imports this module
    from levain.qbroker.store import replace_file

    failures: list[str] = []
    wanted: dict[str, bytes] = {}
    for rec in live:
        try:
            problem = record_problem(dict(rec))
            if problem is not None:
                failures.append(f"a live record is not publishable: {problem}")
                continue
            name, data = file_name(rec["digest"]), canonical_bytes(dict(rec))
        except (ValueError, TypeError, ContractError) as exc:
            failures.append(f"a live record is not publishable: {exc}")
            continue
        if len(data) > PUBLISHED_MAX:
            failures.append(f"{name} is {len(data)} bytes, over the broker's {PUBLISHED_MAX}: not published, "
                            f"so its calls queue")
            continue
        wanted[name] = data
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names = os.listdir(fd)
        stale = time.time() - TMP_STALE if now is None else now - TMP_STALE
        doomed = sorted(n for n in names if _NAME.fullmatch(n) and n not in wanted)
        for n in names:
            if _TMP.fullmatch(n):
                try:
                    if os.stat(n, dir_fd=fd, follow_symlinks=False).st_mtime < stale:
                        doomed.append(n)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    failures.append(f"{n} could not be read: {exc}")
        removed = False
        for n in doomed:
            try:
                os.unlink(n, dir_fd=fd)
                removed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                failures.append(f"{n} could not be removed: {exc}")
        if removed:
            try:
                os.fsync(fd)
            except OSError as exc:
                failures.append(f"the directory could not be fsynced after removals: {exc}")
        present = {n for n in names if n in wanted}
    finally:
        os.close(fd)
    for name, data in sorted(wanted.items()):
        path = directory / name
        try:
            if name in present and _current(path) == data:
                continue
            replace_file(path, data, MODE)
        except OSError as exc:
            failures.append(f"{name} could not be written: {exc}")
    return failures


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
