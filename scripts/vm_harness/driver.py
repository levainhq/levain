"""3b-8a S-C: the VM-harness driver. It wires the cleanup journal (S-A) and the ``_levain_gw<n>`` uid scan (S-B)
of :mod:`scripts.vm_harness.cleanup` around a lifecycle, writes the RESULT header, and makes the post-run
slot-state check that the ambiguous alloc/mint edge (see ``cleanup.py``) is left to.

Harness code, not product code. Every effect is injected (``alloc`` / ``mint`` / ``render`` / ``revoke`` /
``slot_check`` / the process lister), so it is unit-tested with no VM, no root and no socket. The real seams are
the VM sitting's to supply: ``cleanup.control_revoke`` is the real REVOKE; the real alloc / MINT / render and the
real slot-state check ride CONTROL on the VM and are exercised there, never here. The existing ``levain session``
pty route stays for end-to-end runs; this driver is the alloc / MINT / render route with the journal before the
spawn.

What it does not do: it never reads ACTIVE (or any single success) as health, and it does not test FRONT-LOST
(3c). The RESULT header says both, on disk, before the first alloc, so a crashed run still carries it.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from scripts.vm_harness.cleanup import (
    CleanupFailed, CleanupJournal, default_lister, run_with_cleanup, start_session,
)

#: DESIGN r17 W12: the harness states what it tests. One line, first in RESULT.txt.
RESULT_HEADER = (
    "RESULT: this run tests an incomplete lifecycle (alloc, MINT, render, REVOKE; no FRONT-LOST until 3c); "
    "ACTIVE is not health and is never read as health."
)

RESULT_FILE = "RESULT.txt"
JOURNAL_FILE = "cleanup.jsonl"

#: ``keys.GW_ACCOUNT``: the slot's gateway account.
GW_ACCOUNT = "_levain_gw{}"


@dataclass(frozen=True)
class SlotPlan:
    slot: int
    lease: str


@dataclass(frozen=True)
class RunResult:
    ok: bool
    problems: list[str]
    result_path: Path
    journal_path: Path


def journaled_slots(path: Path | str) -> list[int]:
    """Slots in journal order, deduplicated, from intent and session lines alike. A torn line is skipped."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[int] = []
    for raw in p.read_text(errors="replace").splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        s = obj.get("slot") if isinstance(obj, dict) else None
        if isinstance(s, int) and not isinstance(s, bool) and s not in out:
            out.append(s)
    return out


def _write_durable(path: Path, text: str, *, append: bool) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC), 0o600)
    try:
        data = memoryview(text.encode())
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


def run_vm_lifecycle(evidence_dir: Path | str, plan: Iterable[SlotPlan], *, alloc: Callable, mint: Callable,
                     render: Callable, revoke: Callable[[str], object], slot_check: Callable[[int], bool],
                     lister: Callable[[], str] = default_lister,
                     resolve: Callable[[str], int | None] | None = None,
                     clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], object] = time.sleep,
                     deadline_s: float = 60.0) -> RunResult:
    """Run ``plan`` (one session per entry) with the journal before every spawn; clean up in a ``finally``;
    then check every journaled slot is reusable. Returns a :class:`RunResult` (never raises on a cleanup or
    body failure: ``RESULT.txt`` ends in ``RESULT: PASS`` or ``RESULT: FAIL`` and ``problems`` says why). A
    ``BaseException`` (Ctrl-C) still gets its REVOKE walk and a FAIL line, then propagates.

    ``slot_check(slot)`` must be true once cleanup is done; it is what catches an alloc that committed but whose
    session id never reached the journal (nothing to REVOKE; the uid scan sees only a spawned process)."""
    plan = tuple(plan)
    if not plan:
        raise ValueError("empty plan: nothing to run, and a run that tests nothing must not report PASS")
    evidence = Path(evidence_dir)
    result_path = evidence / RESULT_FILE
    journal = CleanupJournal(evidence / JOURNAL_FILE)
    _write_durable(result_path, RESULT_HEADER + "\n", append=False)   # before the first alloc
    resolve_kw = {} if resolve is None else {"resolve": resolve}
    uids = [GW_ACCOUNT.format(p.slot) for p in plan]
    problems: list[str] = []
    verdict = "RESULT: FAIL (interrupted before the verdict was computed)"
    try:
        def body(j: CleanupJournal) -> None:
            for p in plan:
                start_session(j, p.slot, p.lease, alloc=alloc, mint=mint, render=render)

        try:
            run_with_cleanup(journal, body, revoke, uids=uids, lister=lister, clock=clock, sleep=sleep,
                             deadline_s=deadline_s, **resolve_kw)
        except CleanupFailed as e:
            problems.append(f"cleanup: {e}")
        except Exception as e:   # noqa: BLE001 - the body's failure is the run's result, not a crash of the driver
            problems.append(f"body: {type(e).__name__}: {e}")
            problems.extend(getattr(e, "__notes__", []))
        for slot in journaled_slots(journal.path):
            try:
                reusable = slot_check(slot)
            except Exception as e:   # noqa: BLE001 - an unanswerable check is a failure, never a pass
                problems.append(f"slot {slot} check failed: {type(e).__name__}: {e}")
                continue
            if not reusable:
                problems.append(f"slot {slot} is not reusable after cleanup")
        verdict = "RESULT: PASS" if not problems else f"RESULT: FAIL ({len(problems)} problem(s))"
    finally:
        _write_durable(result_path, verdict + "".join(f"\n  - {p}" for p in problems) + "\n", append=True)
    return RunResult(not problems, problems, result_path, journal.path)
