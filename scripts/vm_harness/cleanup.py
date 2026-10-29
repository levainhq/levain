"""3b-8a VM-harness cleanup (DESIGN r17 R16): a cleanup journal written BEFORE each spawn, a ``finally`` that
REVOKEs every journaled session, and a ``_levain_gw<n>`` uid scan that fails the run on any survivor.

Harness code, not product code: it lives outside the shipped ``levain/`` package. Every effect is injected
(``alloc`` / ``mint`` / ``render`` / ``revoke`` / process lister), so the logic is unit-tested without a VM or
root. The real REVOKE is :func:`control_revoke` (the repo's own ``ControlClient`` and the ``REVOKE`` CONTROL
verb, ``root`` caller class); the real lister runs ``ps`` with NO argv column (argv can carry secrets).
"""
from __future__ import annotations

import json
import os
import pwd
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

#: The listing form: columns only, never an argv-printing one.
PS_COMMAND = ("ps", "-Ao", "pid,etime,pcpu,ucomm,uid")


class CleanupFailed(Exception):
    """Cleanup left something behind (a REVOKE failed, or a process survived)."""


@dataclass(frozen=True)
class RevokeFailure:
    session: str
    error: str


@dataclass(frozen=True)
class Survivor:
    pid: int
    etime: str
    pcpu: str
    ucomm: str
    uid: int


# -- S-A: the journal --------------------------------------------------------------------------------------

class CleanupJournal:
    """JSONL, one fsynced line per entry."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: dict) -> None:
        line = (json.dumps(entry, sort_keys=True) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        dfd = os.open(self.path.parent, os.O_RDONLY)   # make the (possibly new) directory entry durable too
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def journaled_sessions(path: Path | str) -> list[str]:
    """Session ids in journal order, deduplicated. A torn or malformed line is skipped (a crash mid-write)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[str] = []
    for raw in p.read_text(errors="replace").splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        s = obj.get("session") if isinstance(obj, dict) else None
        if isinstance(s, str) and s and s not in out:
            out.append(s)
    return out


def walk_journal(path: Path | str, revoke: Callable[[str], object]) -> list[RevokeFailure]:
    """REVOKE every journaled session; a failure never stops the walk. Returns every failure."""
    failures: list[RevokeFailure] = []
    for s in journaled_sessions(path):
        try:
            revoke(s)
        except Exception as e:   # noqa: BLE001 - the walk's whole job is to continue past any failure
            failures.append(RevokeFailure(s, f"{type(e).__name__}: {e}"))
    return failures


def start_session(journal: CleanupJournal, slot: int, lease: str, *, alloc: Callable, mint: Callable,
                  render: Callable) -> str:
    """The seam: intent journaled BEFORE alloc, the session id journaled the moment MINT returns, both before
    render (which spawns the front)."""
    journal.append({"slot": slot, "lease": lease, "intent": "alloc"})
    alloc(slot, lease)
    session = mint(slot, lease)
    journal.append({"slot": slot, "lease": lease, "session": session})
    render(session)
    return session


def control_revoke(socket_path: Path | str) -> Callable[[str], dict]:
    """The real REVOKE: a fresh ``ControlClient`` per call (needs a live root CONTROL; VM only)."""
    def revoke(session: str) -> dict:
        from levain.slot.control_client import ControlClient
        with ControlClient(socket_path) as c:
            return c.call("REVOKE", session=session)
    return revoke


# -- S-B: the uid scan -------------------------------------------------------------------------------------

def default_lister() -> str:
    return subprocess.run(PS_COMMAND, capture_output=True, text=True, check=True, timeout=30).stdout


def parse_ps(text: str) -> list[Survivor]:
    rows: list[Survivor] = []
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 5 or not parts[0].isdigit() or not parts[-1].isdigit():
            continue   # header or noise
        rows.append(Survivor(int(parts[0]), parts[1], parts[2], " ".join(parts[3:-1]), int(parts[-1])))
    return rows


def _resolve_name(name: str) -> int | None:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def scan_survivors(uids: Iterable[int | str], lister: Callable[[], str] = default_lister, *,
                   resolve: Callable[[str], int | None] = _resolve_name) -> list[Survivor]:
    """Every process owned by one of ``uids`` (ints or ``_levain_gw<n>`` names). An unresolvable name fails closed."""
    want: set[int] = set()
    for u in uids:
        if isinstance(u, int):
            want.add(u)
            continue
        n = resolve(u)
        if n is None:
            raise CleanupFailed(f"cannot resolve uid for {u!r}: its processes cannot be scanned")
        want.add(n)
    if not want:
        return []
    return [r for r in parse_ps(lister()) if r.uid in want]


# -- the run -----------------------------------------------------------------------------------------------

def run_with_cleanup(journal: CleanupJournal, body: Callable[[CleanupJournal], object],
                     revoke: Callable[[str], object], *, uids: Iterable[int | str] = (),
                     lister: Callable[[], str] = default_lister,
                     resolve: Callable[[str], int | None] = _resolve_name) -> None:
    """Run ``body``; in a ``finally`` walk the journal, then scan the uids. Cleanup problems fail the run: if
    ``body`` raised they are attached to its exception as notes, otherwise :class:`CleanupFailed` is raised."""
    problems: list[str] = []
    try:
        body(journal)
    except BaseException as e:
        _cleanup(journal, revoke, uids, lister, resolve, problems)
        for p in problems:
            e.add_note(f"cleanup: {p}")
        raise
    _cleanup(journal, revoke, uids, lister, resolve, problems)
    if problems:
        raise CleanupFailed("; ".join(problems))


def _cleanup(journal, revoke, uids, lister, resolve, problems: list[str]) -> None:
    for f in walk_journal(journal.path, revoke):
        problems.append(f"REVOKE {f.session} failed: {f.error}")
    try:
        for s in scan_survivors(uids, lister, resolve=resolve):
            problems.append(f"surviving process pid={s.pid} uid={s.uid} {s.ucomm} etime={s.etime}")
    except Exception as e:   # noqa: BLE001 - an unscannable uid or failed ps is a failed run, not a silent pass
        problems.append(f"uid scan failed: {type(e).__name__}: {e}")
