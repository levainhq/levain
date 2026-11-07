"""3b-8a S-C: the VM-harness driver. It wires the cleanup journal (S-A) and the ``_levain_gw<n>`` uid scan (S-B)
of :mod:`scripts.vm_harness.cleanup` around a lifecycle, writes the RESULT header, and makes the post-run
slot-state check that the ambiguous alloc/mint edge (see ``cleanup.py``) is left to.

Harness code, not product code. Every effect is injected (``alloc`` / ``mint`` / ``render`` / ``revoke`` /
``slot_check`` / the process lister), so it is unit-tested with no VM, no root and no socket.

The seam signatures here are THIS HARNESS'S OWN CONTRACT, not the runtime's API: ``alloc(slot, lease)``,
``mint(slot, lease) -> session id``, ``render(session)``, ``revoke(session)``, ``slot_check(slot) -> bool``.
The real adapters are :class:`VmAdapters` (below), driven from the host against a clone VM. The seam order is
reshaped to the runtime's: ``mint`` is ``runtime-helper prepare`` (the id exists only once it returns; the
``intent`` line is already journaled before it), ``render`` is journal-ack, then ``render`` + ``start`` (the vendor
runs only after the id is fsynced into the journal). ``guest_dispatch.py`` holds the product's own lease across
that split. The existing ``levain session`` pty route stays for end-to-end runs.

What it does not do: it never reads ACTIVE (or any single success) as health, and it does not test FRONT-LOST
(3c). The RESULT header says both, on disk, before the first alloc, so a crashed run still carries it. A run is
scoped to a FRESH evidence dir: one that already holds a journal or a RESULT is refused, never reused.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from scripts.vm_harness.cleanup import (
    CleanupFailed, CleanupJournal, default_lister, run_with_cleanup, start_session,
)
from scripts.vm_harness.host_profile import DARWIN, HostProfile, check_guest_os

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
    """Write ``text``, fsync it and its directory. ``exclusive`` creates the file and refuses (FileExistsError) one that exists."""
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
    dfd = os.open(path.parent, os.O_RDONLY)   # make the (possibly new) directory entry durable too, as CleanupJournal.append does
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


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
        def checked_mint(slot: int, lease: str) -> str:
            session = mint(slot, lease)
            if not isinstance(session, str) or not session:   # journalled and rendered as the id: a non-id would walk no REVOKE
                raise TypeError(f"mint({slot}, {lease!r}) returned {session!r}, not a non-empty session id string")
            return session

        def body(j: CleanupJournal) -> None:
            for p in plan:
                start_session(j, p.slot, p.lease, alloc=alloc, mint=checked_mint, render=render)

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


# -- the real adapters (host side, against a clone VM) -------------------------------------------------------------

class AdapterError(Exception):
    """A real adapter could not do its job (a refusal, a timeout, a slot mismatch)."""


class GuestError(RuntimeError):
    """A guest command exited non-zero. ``rc`` is its exit status (255 / 1 / ... from ssh or sudo itself)."""

    def __init__(self, rc: int, msg: str = "") -> None:
        super().__init__(f"rc={rc}: {msg}")
        self.rc = rc


SESSION_RE = re.compile(r"s-[0-9a-f]{16}")        # levain.slot.grammar.SESSION_RE
SLOT_TAG_RE = re.compile(r"e[0-9]{1,2}")
ARG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")   # the only shape a guest argv token may take
ABSENT_RC = 3            # guest_ops get: the file is not there
WAIT_PROBE_S = 20.0


class VmAdapters:
    """The driver's seams against a running clone VM. ``guest(argv, timeout) -> stdout`` runs one command in the
    guest (raising :class:`GuestError` on a non-zero exit); ``spawn(argv)`` starts a long-lived guest process and
    returns an object with ``poll()``, ``terminate()`` (and ``wait()`` / ``kill()`` when real). Both are injected, so
    the ordering logic is tested with no VM.

    The journal-before-vendor guarantee: ``mint`` returns the id the moment ``prepare`` has answered, and the vendor
    cannot start until ``render`` writes the ack, which :func:`start_session` only reaches after journaling the id.
    Every rendezvous tag is unique per run and the exchange dir is emptied of old tags first, so a previous run's
    ack or final can never satisfy this one."""

    OPS = DARWIN.ops_argv()            # the default profile's prefixes; an instance with another profile carries its own
    DISPATCH = DARWIN.dispatch_argv()

    def __init__(self, guest: Callable[..., str], spawn: Callable[[list[str]], object], *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], object] = time.sleep,
                 prepare_s: float = 90.0, run_s: float = 180.0, tag_prefix: str = "drv",
                 nonce: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
                 profile: HostProfile = DARWIN) -> None:
        self.profile = profile
        self.OPS, self.DISPATCH = profile.ops_argv(), profile.dispatch_argv()
        self._guest, self._spawn, self._clock, self._sleep = guest, spawn, clock, sleep
        self.prepare_s, self.run_s, self.tag_prefix, self._nonce = prepare_s, run_s, tag_prefix, nonce
        self._fresh = False
        self._tags: dict[str, str] = {}     # session -> tag
        self._planned: dict[str, int] = {}  # session -> the slot the plan said
        self._actual: dict[str, str] = {}   # session -> the slot prepare reported (e<n>)
        self._children: list[object] = []
        self._all_tags: list[str] = []      # every tag spawned, minted or not: close() cancels each
        self._child_of: dict[str, object] = {}   # session -> its guest dispatch process

    def ops(self, verb: str, arg: str, timeout: float = 60) -> str:
        arg = str(arg)
        if not ARG_RE.fullmatch(arg) and arg != "-":   # nothing guest-controlled reaches a remote argv unchecked
            raise AdapterError(f"refusing to pass {arg[:40]!r} to the guest ({verb})")
        return self._guest([*self.OPS, verb, arg], timeout)

    def _get(self, name: str, timeout: float = 60) -> dict | None:
        """The named rendezvous record, None ONLY when the guest says the file is absent (rc 3). Any other failure
        (ssh down, sudo refused, a refused symlink, a probe timeout) is an AdapterError, never "not there yet"."""
        try:
            text = self.ops("get", name, timeout)
        except AdapterError:
            raise
        except GuestError as e:
            if e.rc == ABSENT_RC:
                return None
            raise AdapterError(f"guest get {name} failed: {e}") from e
        except Exception as e:   # noqa: BLE001 - a timeout or a spawn failure
            raise AdapterError(f"guest get {name} failed: {type(e).__name__}: {e}") from e
        try:
            obj = json.loads(text)
        except ValueError:
            return None    # a record being replaced; the writer renames atomically, so this is not a torn read
        return obj if isinstance(obj, dict) else None

    def _wait(self, tag: str, names: tuple[str, ...], deadline_s: float, child: object, what: str) -> tuple[str, dict]:
        end = self._clock() + deadline_s
        while True:
            for n in names:
                got = self._get(f"{tag}.{n}.json", self._probe(end))
                if got is not None:
                    return n, got
            err = self._get(f"{tag}.error.json", self._probe(end))
            if err is not None:
                raise AdapterError(f"guest dispatch failed before {what}: {err.get('exc')}")
            if child.poll() is not None:
                raise AdapterError(f"guest dispatch exited (rc={child.poll()}) before {what}")
            if self._clock() >= end:
                raise AdapterError(f"timed out after {deadline_s}s waiting for {what}")
            self._sleep(1.0)

    def _probe(self, end: float) -> float:
        """One probe may not outlive the deadline it serves (a floor of 1 s so a probe can still answer)."""
        return max(1.0, min(WAIT_PROBE_S, end - self._clock()))

    # the seams
    def alloc(self, slot: int, lease: str) -> None:
        if not self._fresh:   # stale tags of an earlier run are removed (and proven gone) before anything is minted
            left = self.ops("fhclean", "-").strip()
            if left != "0":
                raise AdapterError(f"the guest exchange dir still holds {left} old rendezvous entries")
            self._fresh = True
        state = self.ops("slotstate", slot).strip()
        if state != "reusable":
            raise AdapterError(f"slot {slot} is {state!r}, not reusable")

    def mint(self, slot: int, lease: str) -> str:
        tag = f"{self.tag_prefix}{slot}x{self._nonce()}"
        if not ARG_RE.fullmatch(tag):
            raise AdapterError(f"bad rendezvous tag {tag!r}")
        self._all_tags.append(tag)
        child = self._spawn([*self.DISPATCH, tag])
        self._children.append(child)
        _, prepared = self._wait(tag, ("prepared",), self.prepare_s, child, "prepare")
        session, actual = prepared.get("session"), prepared.get("slot")
        if not isinstance(session, str) or not session:
            raise AdapterError(f"prepared record carries no session id: {prepared!r}")
        if not SESSION_RE.fullmatch(session):   # the record is guest-written: nothing else goes to a journal or an argv
            raise AdapterError(f"prepared record carries a malformed session id: {session[:40]!r}")
        if not isinstance(actual, str) or not SLOT_TAG_RE.fullmatch(actual):
            raise AdapterError(f"prepared record carries no slot tag: {actual!r}")
        self._tags[session] = tag
        self._child_of[session] = child
        self._planned[session] = slot
        self._actual[session] = actual
        return session

    def render(self, session: str) -> None:
        tag = self._tags[session]
        got = self.ops("slotof", session).strip()
        want, actual = f"e{self._planned[session]}", self._actual[session]
        if got != actual:
            raise AdapterError(f"session {session} is leased in slot {got!r}, prepare said {actual!r}: not rendering")
        if got != want:   # the id is journaled by now, so the REVOKE walk covers it; the vendor must not run
            raise AdapterError(f"session {session} landed in slot {got!r}, the plan said {want!r}: not rendering")
        self.ops("put", f"{tag}.journaled")
        child = self._child_of[session]
        _, final = self._wait(tag, ("final",), self.run_s, child, "the vendor to finish")
        if final.get("poll") != 0 or final.get("session") != session:   # exit 2, GATE_UNPROVEN, None (still running)
            raise AdapterError(f"the vendor did not finish cleanly: final={final!r}")

    def revoke(self, session: str) -> str:
        return self.ops("revoke", session)

    def slot_check(self, slot: int) -> bool:
        slots = {slot} | {int(a[1:]) for s, a in self._actual.items() if self._planned.get(s) == slot}
        return all(self.ops("slotstate", n).strip() == "reusable" for n in sorted(slots))

    def lister(self) -> str:
        return self._guest(self.profile.ps_argv(), 60)

    def verify_host(self) -> None:
        """Refuse a guest that is not the profile's OS (``uname -s``), before any lifecycle runs. Opt-in: a caller with a
        real guest runs it once; the injected-guest unit tests do not answer ``uname``."""
        try:
            out = self._guest(["/usr/bin/uname", "-s"], 30)
        except Exception as e:   # noqa: BLE001 - an unanswerable guest is a refusal, not a pass
            raise AdapterError(f"cannot ask the guest its OS: {type(e).__name__}: {e}") from e
        check_guest_os(self.profile, out)

    def resolve(self, name: str) -> int | None:
        v = self.ops("resolve", name).strip()
        return None if v == "none" else int(v)

    def close(self) -> None:
        """Cancel every remote dispatcher (killing the local ssh client does not), then terminate -> wait -> kill ->
        reap the local children. Best effort; the REVOKE walk and the uid scan are the proof."""
        for tag in self._all_tags:
            try:
                self.ops("cancel", tag, 20)
            except Exception:   # noqa: BLE001 - absent pid file (finished) or a dead guest
                pass
        for c in self._children:
            try:
                if c.poll() is None:
                    c.terminate()
                    try:
                        c.wait(5)
                    except Exception:   # noqa: BLE001
                        c.kill()
                        c.wait(5)
                else:
                    c.wait(0)
            except Exception:   # noqa: BLE001
                pass


def run_on_vm(evidence_dir: Path | str, plan: Iterable[SlotPlan], adapters: VmAdapters, *,
              deadline_s: float = 90.0) -> RunResult:
    """:func:`run_vm_lifecycle` wired to ``adapters``: the guest's ``ps`` and ``getpwnam`` are injected, so the uid
    scan never silently looks at the host."""
    try:
        return run_vm_lifecycle(evidence_dir, plan, alloc=adapters.alloc, mint=adapters.mint, render=adapters.render,
                                revoke=adapters.revoke, slot_check=adapters.slot_check, lister=adapters.lister,
                                resolve=adapters.resolve, deadline_s=deadline_s)
    finally:
        adapters.close()
