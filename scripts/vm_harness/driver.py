"""3b-8a S-C: the VM-harness driver. It wires the cleanup journal (S-A) and the ``_levain_gw<n>`` uid scan (S-B)
of :mod:`scripts.vm_harness.cleanup` around a lifecycle, writes the RESULT header, and makes the post-run
slot-state check that the ambiguous alloc/mint edge (see ``cleanup.py``) is left to.

Harness code, not product code. Every effect is injected (``alloc`` / ``mint`` / ``render`` / ``revoke`` /
``slot_check`` / the process lister), so it is unit-tested with no VM, no root and no socket.

The seam signatures here are THIS HARNESS'S OWN CONTRACT, not the runtime's API: ``alloc(slot, lease)``,
``mint(slot, lease) -> session id``, ``render(session)``, ``revoke(session)``, ``slot_check(slot) -> bool``.
The real adapters are not written. They are written at the V2 sitting, against a live root CONTROL: the session
id is expected to come from ``runtime-helper prepare`` and the CONTROL MINT to happen inside ``render``, after
the front is spawned, so the seam order may be reshaped there to journal the id the moment it exists (L3 r1
found the current order does not match). Until then only fakes drive this driver; ``cleanup.control_revoke`` is
the one real seam, and it too needs that live CONTROL. The existing ``levain session`` pty route stays for
end-to-end runs.

What it does not do: it never reads ACTIVE (or any single success) as health, and it does not test FRONT-LOST
(3c). The RESULT header says both, on disk, before the first alloc, so a crashed run still carries it. A run is
scoped to a FRESH evidence dir: one that already holds a journal or a RESULT is refused, never reused.
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


def _write_durable(path: Path, text: str, *, append: bool, exclusive: bool = False) -> None:
    """Write ``text`` and fsync it. ``exclusive`` creates the file and refuses (FileExistsError) one that exists."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | (os.O_EXCL if exclusive else 0)
    fd = os.open(path, flags, 0o600)
    try:
        data = memoryview(text.encode())
        while data:
            n = os.write(fd, data)
            if n <= 0:
                raise OSError(f"zero-byte write to {path}: refusing to loop")
            data = data[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_slots(slots: list[int], slot_check: Callable[[int], object], clock: Callable[[], float],
                 sleep: Callable[[float], object], deadline_s: float, interval_s: float = 1.0) -> list[str]:
    """Problems for the slots that are not reusable. Only the answer ``True`` passes: ``False`` is "not yet" and
    is polled to the deadline (REVOKE only wakes an asynchronous reaper); a raise or any other answer (a truthy
    dict, a state string) is a failed check, never a pass."""
    problems: dict[int, str] = {}
    pending = list(slots)
    end = clock() + deadline_s
    while pending:
        still: list[int] = []
        for slot in pending:
            try:
                answer = slot_check(slot)
            except Exception as e:   # noqa: BLE001 - an unanswerable check is a failure, never a pass
                problems[slot] = f"slot {slot} check failed: {type(e).__name__}: {e}"
                continue
            if answer is True:
                continue
            if answer is not False:
                problems[slot] = f"slot {slot} check returned a malformed answer {answer!r} (only True or False)"
                continue
            still.append(slot)
        pending = still
        if not pending:
            break
        if clock() >= end:
            for slot in pending:
                problems[slot] = f"slot {slot} is not reusable after cleanup"
            break
        sleep(interval_s)
    return [problems[s] for s in slots if s in problems]


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

    ``slot_check(slot)`` must return exactly ``True`` once cleanup is done (``False`` is polled to ``deadline_s``;
    anything else fails as malformed); it is what catches an alloc that committed but whose session id never
    reached the journal (nothing to REVOKE; the uid scan sees only a spawned process). ``evidence_dir`` must be
    fresh (``FileExistsError`` otherwise, before anything runs). If RESULT.txt itself cannot be written, that
    ``OSError`` propagates, except that it never replaces an in-flight ``BaseException`` (it becomes a note on it)."""
    plan = tuple(plan)
    if not plan:
        raise ValueError("empty plan: nothing to run, and a run that tests nothing must not report PASS")
    evidence = Path(evidence_dir)
    result_path = evidence / RESULT_FILE
    journal = CleanupJournal(evidence / JOURNAL_FILE)
    if os.path.lexists(journal.path):
        raise FileExistsError(f"{journal.path} exists: a run is scoped to a fresh evidence dir")
    _write_durable(result_path, RESULT_HEADER + "\n", append=False, exclusive=True)   # before the first alloc
    resolve_kw = {} if resolve is None else {"resolve": resolve}
    uids = [GW_ACCOUNT.format(p.slot) for p in plan]
    problems: list[str] = []
    pending: BaseException | None = None   # a Ctrl-C / SystemExit: recorded, then re-raised after RESULT is written

    def interrupted(e: BaseException) -> None:
        problems.append(f"interrupted: {type(e).__name__}: {e}".rstrip(": "))
        problems.extend(getattr(e, "__notes__", []))

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
        except BaseException as e:   # noqa: BLE001 - the REVOKE walk already ran; the slot checks still must
            pending = e
            interrupted(e)
        try:
            slots = journaled_slots(journal.path)
        except Exception as e:   # noqa: BLE001 - unreadable journal: fail closed, check every planned slot
            problems.append(f"journal unreadable for the slot check: {type(e).__name__}: {e}")
            slots = list(dict.fromkeys(p.slot for p in plan))
        problems.extend(_check_slots(slots, slot_check, clock, sleep, deadline_s))
    except BaseException as e:   # noqa: BLE001 - interrupted during the slot phase itself
        if pending is None:
            pending = e
        interrupted(e)
    verdict = "RESULT: PASS" if not problems else f"RESULT: FAIL ({len(problems)} problem(s))"
    try:
        _write_durable(result_path, verdict + "".join(f"\n  - {p}" for p in problems) + "\n", append=True)
    except OSError as werr:
        if pending is None:
            raise
        pending.add_note(f"RESULT.txt final write failed: {type(werr).__name__}: {werr}")
    if pending is not None:
        raise pending
    return RunResult(not problems, problems, result_path, journal.path)
