"""The remove-step mechanism (row 33 R1b; DESIGN 1022-8/upgrade §1.1 the remove-step table, rules e and g).

Every recorded resource a run removes goes through ONE per-identity step builder of its kind (in the kind's own
module: ``units``, ``sudoers``, ``accounts``, ``pf``, ``bwrap``, ``netns``, ``layout``), which takes the RECORDED
identity and touches nothing else. Each act of a builder is a ``call`` step that first reads the live object:

* **ABSENT**: done, silent, nothing run;
* **OURS** (present and Levain's): the act runs;
* **NOT_OURS** (present, not Levain's): nothing run, the record lands in :attr:`Live.skips` (``install-skips``);
* a read whose tool FAILS raises :class:`ReadFailed`: the step fails, the run stops, the re-run reads again. A failed
  read is never taken as ABSENT.

:class:`Live` carries every host touch a remove step makes (argv runs, each bounded by :attr:`Live.timeout`; the root
its file paths sit under, which the removal argv name too, through :meth:`Live.at`; the fsync; the fault hook), so a
test drives the whole table against a fake runner and a tmp root. Every remove step is a :class:`RemoveStep` that
names the programs its read and its acts run (``tools``), so the plan's tool preflight refuses a missing one before
step 1 (a ``call`` step's argv is otherwise invisible to it). :data:`REMOVE_POINTS` are the
crash table's ``<kind>:<point>`` names; ``Live.fault`` is called at each and is a no-op outside the staged-tree
launcher (R2 wires ``Seams.fault`` to it).

Root-side: imports and runs on the 3.9 floor.
"""
from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from levain.firing.hands import Step
from levain.launch import child_env

ABSENT, OURS, NOT_OURS = "absent", "ours", "not-ours"

#: The crash table's points, one per act boundary of each remove step (§5 R1b/R3 "The remove-step crash table").
REMOVE_POINTS = frozenset({
    "unit/1:after-stop", "unit/1:after-rm", "sudoers/1:after-rm", "member/1:after-remove",
    "group/1:after-retire", "group/1:after-delete", "account/1:after-retire", "account/1:after-first-act",
    "pf-alias/1:after-remove", "pf-anchor/1:after-flush", "pf-enable/1:after-release", "apparmor/1:after-unload",
    "bwrap-copy/1:after-copy-rm", "netns/1:after-delete", "entry/1:after-rm",
})


class ReadFailed(Exception):
    """A live read whose tool failed: none of the three outcomes."""


def _run(argv: Sequence[str], **kw: object) -> "subprocess.CompletedProcess[str]":
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("cwd", "/")
    env = kw.pop("env", None)          # env= written at the call, so the spawn scan sees it
    return subprocess.run(list(argv), env=child_env() if env is None else env, **kw)  # type: ignore[call-overload]


def _no_fault(point: str) -> None:
    return None


def _fsync(fd: int) -> None:
    from levain.slot.generation import _full_fsync

    _full_fsync(fd)


@dataclass(frozen=True)
class RemoveStep(Step):
    """A remove step: ``tools`` are the host programs its live read and its acts run, which no ``argv`` names."""

    tools: Tuple[str, ...] = ()


#: The bound on one argv a remove step runs (a hung ``launchctl print`` or ``systemctl show`` must not hang the run).
RUN_TIMEOUT_S = 60.0


@dataclass
class Live:
    #: Every argv a remove step reads with or acts with, ``subprocess.run``-shaped (called with ``timeout=``).
    run: Callable[..., object] = _run
    #: The root every file path a remove step reads or removes sits under (``/`` on a host).
    root: Path = Path("/")
    #: Called with ``<kind>:<point>`` at each act boundary (a no-op outside the launcher).
    fault: Callable[[str], None] = _no_fault
    #: The durable writes' fsync (macOS ``F_FULLFSYNC``).
    fsync: Callable[[int], None] = _fsync
    #: The NOT_OURS records this run met: ``(kind, identity)``.
    skips: set = field(default_factory=set)
    #: Seconds each argv may run before it counts as failed.
    timeout: float = RUN_TIMEOUT_S

    def at(self, path: Path) -> Path:
        p = Path(path)
        return self.root / p.relative_to("/") if p.is_absolute() else self.root / p

    def timed(self, argv: Sequence[str], **kw: object) -> object:
        """:attr:`run` with :attr:`timeout` unless the caller bounds it itself (the runner a callee gets)."""
        kw.setdefault("timeout", self.timeout)
        return self.run(list(argv), **kw)

    def out(self, argv: Sequence[str]) -> Tuple[int, str, str]:
        """``(exit, stdout, stderr)``; a run past :attr:`timeout` answers exit 124, a failure like any other."""
        try:
            r = self.timed(argv)
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {self.timeout:g}s"
        return (getattr(r, "returncode", 1), getattr(r, "stdout", "") or "", getattr(r, "stderr", "") or "")

    def read(self, argv: Sequence[str]) -> str:
        """``argv``'s stdout; a non-zero exit raises :class:`ReadFailed`."""
        rc, out, err = self.out(argv)
        if rc != 0:
            raise ReadFailed(f"{' '.join(argv)} exited {rc}: {(err or out).strip()[:200]}")
        return out

    def act(self, argv: Sequence[str]) -> Tuple[bool, str]:
        rc, out, err = self.out(argv)
        return rc == 0, "" if rc == 0 else f"{' '.join(argv)} exited {rc}: {(err or out).strip()[:200]}"


def root_file(live: Live, path: Path, *, check: Optional[Callable[[bytes], bool]] = None) -> str:
    """A recorded path's outcome: no entry = ABSENT; a regular file owned by root (whose bytes pass ``check``, when
    given) = OURS; anything else at that name = NOT_OURS. An lstat or read that fails otherwise raises."""
    p = live.at(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return ABSENT
    except OSError as e:
        raise ReadFailed(f"cannot read {path}: {e.strerror or e}") from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != _root_uid(live):
        return NOT_OURS
    if check is not None:
        try:
            with open(p, "rb") as fh:
                data = fh.read(1 << 16)    # ``check`` sees only the first 64 KiB: a header test, never a whole-file digest
        except OSError as e:
            raise ReadFailed(f"cannot read {path}: {e.strerror or e}") from None
        if not check(data):
            return NOT_OURS
    return OURS


def _root_uid(live: Live) -> int:
    """0 on a host; under a tmp root (a test) the owner of that root stands in for root."""
    return 0 if live.root == Path("/") else os.lstat(live.root).st_uid


Classify = Callable[[], str]
Act = Callable[[], Tuple[bool, str]]


def remove_step(why: str, record: tuple, live: Live, classify: Classify, act: Act, *,
                tools: Sequence[str]) -> RemoveStep:
    """One act of a remove step: the live read first (§1.1 rule e), then the act only for OURS. ``tools``: every
    program ``classify`` and ``act`` run (required, so no builder leaves the preflight blind)."""
    def go() -> Tuple[bool, str]:
        try:
            state = classify()
        except ReadFailed as e:
            return False, f"{record[0]}: the live read failed, record kept: {e}"
        if state == NOT_OURS:
            live.skips.add(record)
            return True, f"{record[0]} {record[1]!r} is present and not Levain's: left in place (install-skips)"
        if state == ABSENT:
            return True, ""
        return act()
    return RemoveStep(why, call=go, tools=tuple(tools))


def chain(*acts: Act) -> Act:
    """Acts in order, stopping at the first that fails."""
    def go() -> Tuple[bool, str]:
        for a in acts:
            ok, why = a()
            if not ok:
                return False, why
        return True, ""
    return go


def point(live: Live, name: str) -> Act:
    assert name in REMOVE_POINTS, name
    def go() -> Tuple[bool, str]:
        live.fault(name)
        return True, ""
    return go


def at(live: Live, path: Path) -> str:
    """A recorded path as a removal argv names it: under :attr:`Live.root`, the same object the live read classified
    (``path`` itself on a host)."""
    return str(live.at(Path(path)))


__all__ = ["ABSENT", "Live", "NOT_OURS", "OURS", "REMOVE_POINTS", "RUN_TIMEOUT_S", "ReadFailed", "RemoveStep", "at",
           "chain", "point", "remove_step", "root_file"]
