"""Process control for one uid: freeze, kill, the "no process left" proof and SCRUBBED (CONTRACT §5.6,
§7.1, §7.3, §11 V12; plan §2e 4).

END rests on these answers: a slot uid is reused only after :func:`scrub` proved that no process of it,
of its gateway uid and (verifier) of its child uid remains. A wrong ``True`` here hands a live process
to the next session, so every proof fails CLOSED: a pid whose facts cannot be read counts as a process
of every uid.

Two kill domains:

- :class:`UidProcs` sweeps by uid (macOS for everything; Linux for SCRUBBED's gateway sweep and the uid
  half of every proof). Freeze = ``SIGSTOP`` every running process of the uid, repeated until scans
  find none running; kill = ``SIGKILL`` until scans find none at all. A fork between two scans is
  caught by the next one, and a sweep is DONE only when two scans in a row agree (one scan can miss a
  child whose parent forked it and exited between the pid list and the parent's read).
- :class:`CgroupProcs` (Linux) drives the session cgroup (§7.3): ``cgroup.freeze`` + ``frozen 1``,
  ``cgroup.kill`` + ``populated 0``; its proof is ``populated 0`` AND the uid sweep's.

Linux: every signal goes through :func:`signal_checked`: pin the pid (a pidfd, so a pid that dies and
is reused after the check cannot receive it), re-read it, and send only if it is still the same
process (start time) of the target uid (§7.1's "live check (uid + start time) of any process before
a signal"). macOS has no pidfd, so it never signals a pid at all (codex r1 3, plan §2e 4): a forked child
drops to the target uid (:func:`levain.slot.wall.drop_and_assert`) and calls ``kill(-1, sig)``
(:func:`signal_as_uid`); a process that is not root can signal only its own uid's processes, so no pid
reuse can hand the signal to anyone else. The per-pid listing stays: it is what the proofs read.

Every OS act is a seam (:class:`ProcSeams`): the process listing, one pid's read, the pin / signal,
the cgroup file read / write, the clock and the sleep. The unit suite drives a fake table.

Why libproc on macOS (``proc_listallpids`` + ``proc_pidinfo(PROC_PIDTBSDINFO)``) rather than parsing
``ps``: one fixed 136-byte struct carries the real, effective AND saved uid, the stop state and a
microsecond start time, which is exactly the live check; ``ps``'s ``lstart`` is whole seconds and the
parse would be a second, looser definition of "a process of the uid".
"""
from __future__ import annotations

import ctypes
import errno
import os
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from levain.firing.hands import HostOS
from levain.launch import child_env
from levain.slot import Refused, write_all
from levain.slot.wall import CGROUP_ROOT, drop_and_assert

#: CONTRACT §11 V12: the freeze / kill bound. END's own copy is ``end.V12_SECONDS`` (a test keeps them one).
V12_SECONDS = 10.0

#: DECISION: the sweep's poll backoff (the contract fixes only the V12 bound).
POLL_FIRST_S = 0.005
POLL_MAX_S = 0.1

RUN, STOP, ZOMBIE, UNKNOWN = "run", "stop", "zombie", "unknown"


@dataclass(frozen=True)
class Proc:
    """One process as a scan saw it. ``uids`` = (real, effective, saved); ``start`` is an opaque
    start-time token (equal tokens = the same process); ``state`` is :data:`RUN` (anything that can
    still execute: running, sleeping, being forked), :data:`STOP`, :data:`ZOMBIE` or :data:`UNKNOWN`
    (its facts could not be read: counted as every uid's, never signalled)."""
    pid: int
    uids: tuple[int, ...]
    start: int
    state: str

    def of(self, uid: int) -> bool:
        """A process "of the uid" (§7.3): DECISION any of real, effective, saved (§7.3 names no id; a
        saved uid can be made effective again, so it is the uid's process too)."""
        return uid in self.uids


def _unpinned(_handle: object) -> None:
    return None


def _read_text(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                return b"".join(chunks).decode("utf-8", "replace")
            chunks.append(b)
    finally:
        os.close(fd)


def _write_text(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        write_all(fd, text.encode("ascii"))
    finally:
        os.close(fd)


def _self_uids() -> tuple[int, ...]:
    return (os.getuid(), os.geteuid())


@dataclass
class ProcSeams:
    """The OS acts. ``listing()`` = every process on the host; ``probe(pid)`` = one pid now (``None`` =
    gone); ``pin(pid)`` -> a handle the signal goes to (raises ``ProcessLookupError`` when gone);
    ``send(handle, sig)`` (raises ``ProcessLookupError`` when gone); ``unpin(handle)``; ``read(path)`` /
    ``write(path, text)`` for cgroup files (``FileNotFoundError`` when absent). ``signal_uid(uid, sig)``:
    when set (macOS), a sweep signals every process of the uid at once AS the uid, never a pid, and
    returns whether the signal was sent; ``pin`` / ``send`` are then never used."""
    listing: Callable[[], Iterable[Proc]]
    probe: Callable[[int], Proc | None]
    pin: Callable[[int], object]
    send: Callable[[object, int], None]
    unpin: Callable[[object], None] = _unpinned
    signal_uid: Callable[[int, int], bool] | None = None
    read: Callable[[str], str] = _read_text
    write: Callable[[str, str], None] = _write_text
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    self_uids: Callable[[], tuple[int, ...]] = _self_uids
    self_pid: Callable[[], int] = os.getpid
    #: macOS: end the uid's per-user launchd domain within a timeout, answering whether launchctl said it did
    #: (:func:`darwin_bootout`); ``None`` elsewhere.
    domain_out: Callable[[int, float], bool] | None = None


def check_target(uid: object, seams: ProcSeams) -> int:
    """A uid this module may sweep: never root, never the caller's own (a sweep of it would kill the
    caller and every session of its user)."""
    if type(uid) is not int or uid <= 0 or uid in seams.self_uids():
        raise Refused("bad-target-uid", repr(uid))
    return uid


def signal_checked(p: Proc, uid: int, sig: int, seams: ProcSeams) -> bool:
    """Send ``sig`` to ``p`` only if, AFTER pinning the pid, a fresh read still shows the same process
    (start time) of ``uid``. Returns whether it was sent. The pin comes first so that a pid reused after
    the read cannot receive the signal (Linux pidfd; on macOS the pin is the pid itself, see the residue
    in :func:`darwin_seams`)."""
    if p.pid <= 1 or p.pid == seams.self_pid() or p.state == UNKNOWN:
        return False
    try:
        handle = seams.pin(p.pid)
    except OSError:          # gone, or not pinnable: unsignalled, so the sweep's proof stays unmet
        return False
    try:
        now = seams.probe(p.pid)
        if now is None or now.state == UNKNOWN or now.start != p.start or not now.of(uid):
            return False
        try:
            seams.send(handle, sig)
        except OSError:
            return False
        return True
    finally:
        seams.unpin(handle)


def _scan(uid: int, seams: ProcSeams) -> list[Proc]:
    """The processes that bear on ``uid``: its own, and every unreadable one (fail closed). A listing
    that fails is one unreadable process: every proof answers ``False``, never raises."""
    try:
        return [p for p in seams.listing() if p.state == UNKNOWN or p.of(uid)]
    except OSError:
        return [Proc(0, (), 0, UNKNOWN)]


def _frozen(procs: list[Proc]) -> bool:
    return all(p.state in (STOP, ZOMBIE) for p in procs)


def _gone(procs: list[Proc]) -> bool:
    """DECISION: a zombie counts as gone. It runs nothing and holds no descriptor; only its parent's
    reap removes it, which the uid cannot be made to do."""
    return all(p.state == ZOMBIE for p in procs)


def _key(procs: list[Proc]) -> frozenset[tuple[int, int, str]]:
    return frozenset((p.pid, p.start, p.state) for p in procs)


def _sweep(uid: int, sig: int, done: Callable[[list[Proc]], bool], target: Callable[[Proc], bool],
           bound_s: float, seams: ProcSeams) -> bool:
    """Signal every ``target`` process of ``uid`` until two scans in a row are ``done`` and identical,
    with nothing signalled between them; ``False`` once ``bound_s`` has passed without that."""
    deadline = seams.clock() + bound_s
    delay = POLL_FIRST_S
    confirm: frozenset | None = None
    while True:
        procs = _scan(uid, seams)
        if done(procs):
            if confirm is not None and confirm == _key(procs):
                return True
            confirm = _key(procs)
        else:
            confirm = None
        if seams.clock() >= deadline:
            return False
        targets = [p for p in procs if target(p)]
        if targets and seams.signal_uid is not None:
            if seams.signal_uid(uid, sig):
                confirm = None
        else:
            for p in targets:
                if signal_checked(p, uid, sig, seams):
                    confirm = None
        if confirm is None:
            seams.sleep(delay)
            delay = min(delay * 2, POLL_MAX_S)


class UidProcs:
    """Freeze / kill / prove by uid sweep. Each method matches :class:`levain.slot.end.EndSeams`."""

    def __init__(self, seams: ProcSeams | None = None) -> None:
        self.seams = seams or default_seams()

    def freeze(self, uid: int, bound_s: float) -> bool:
        """``SIGSTOP`` every running process of ``uid`` until two scans find all of them stopped (or
        zombies) within ``bound_s``."""
        uid = check_target(uid, self.seams)
        return _sweep(uid, signal.SIGSTOP, _frozen, lambda p: p.state == RUN, bound_s, self.seams)

    def kill(self, uid: int, bound_s: float) -> bool:
        """END's kill: on macOS the uid's launchd domain is booted out first (``domain_out``, at most half of
        ``bound_s``), then :meth:`sweep_kill` runs whatever the bootout answered, with the rest of the bound (never
        less than half); ``True`` only if launchctl reported the domain done AND the sweep proved the uid empty.
        Without the bootout launchd started an agent again between the sweep's last scan and END's proof: a
        300k-file END went HELD ``kill-unproven``, distnoted 2913 killed and 2942 there at the proof [measured:
        levain-memory 1019-s1design/runs/s3-macos-2f-final/raw/Z5b_300k.txt, 2026-10-21]. END freezes every
        process of the uid before this, so none of them can ask launchd for an agent while the domain goes; a
        later request (END's own acts after the proof) is what SCRUB's bootout is for. The order is undo's
        (install_helper ``_end_processes``): domain, kill, proof."""
        uid = check_target(uid, self.seams)
        if self.seams.domain_out is None:
            return self.sweep_kill(uid, bound_s)
        deadline = self.seams.clock() + bound_s
        booted = self.seams.domain_out(uid, bound_s / 2)
        swept = self.sweep_kill(uid, max(deadline - self.seams.clock(), bound_s / 2))
        return booted and swept

    def sweep_kill(self, uid: int, bound_s: float) -> bool:
        """``SIGKILL`` every process of ``uid`` until two scans find none (zombies aside) within
        ``bound_s``. A stopped process dies of ``SIGKILL`` without being continued. No bootout: :func:`scrub`'s
        gateway sweep, since scrub boots out each uid itself right after it."""
        uid = check_target(uid, self.seams)
        return _sweep(uid, signal.SIGKILL, _gone, lambda p: p.state in (RUN, STOP), bound_s, self.seams)

    def no_process(self, uid: int) -> bool:
        """The proof: two scans in a row find no process of ``uid`` and no unreadable pid."""
        uid = check_target(uid, self.seams)
        return _gone(_scan(uid, self.seams)) and _gone(_scan(uid, self.seams))


class CgroupProcs:
    """Linux: the session cgroup ``cgroup`` (``/sys/fs/cgroup/levain.slice/{e,v}<n>.scope/<session>``,
    :func:`levain.slot.records.cgroup_path`, or a verifier step's child cgroup, §5.6) drives the freeze
    and kill; the proof adds the uid sweep, because §7.3 states the test as "no process of the slot
    uid", and a process leaves its cgroup only by a root act.

    A cgroup that does not exist held nothing (no vendor ever ran, §7.3's vacuous FROZEN/DEAD): each
    answer is then the uid sweep's alone (DECISION: a process of the uid outside any session cgroup
    cannot be frozen or proven by it, so it fails the proof rather than being ignored)."""

    def __init__(self, cgroup: str, seams: ProcSeams | None = None) -> None:
        parts = cgroup[len(CGROUP_ROOT) + 1:].split("/")
        if not cgroup.startswith(CGROUP_ROOT + "/") or any(p in ("", ".", "..") for p in parts):
            raise Refused("bad-value", f"cgroup {cgroup!r}")
        self.cgroup = cgroup
        self.seams = seams or default_seams("linux")
        self.uid = UidProcs(self.seams)

    def events(self) -> dict[str, str] | None:
        """``cgroup.events`` as a dict, ``None`` when the cgroup does not exist."""
        try:
            text = self.seams.read(self.cgroup + "/cgroup.events")
        except FileNotFoundError:
            return None
        out: dict[str, str] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2:
                out[parts[0]] = parts[1]
        return out

    def _poll(self, key: str, want: str, bound_s: float) -> bool | None:
        """Poll ``cgroup.events`` until ``key`` reads ``want``; ``None`` = the cgroup is gone."""
        deadline = self.seams.clock() + bound_s
        delay = POLL_FIRST_S
        while True:
            ev = self.events()
            if ev is None:
                return None
            if ev.get(key) == want:
                return True
            if self.seams.clock() >= deadline:
                return False
            self.seams.sleep(delay)
            delay = min(delay * 2, POLL_MAX_S)

    def _act(self, name: str, key: str, want: str, uid: int, bound_s: float) -> bool:
        uid = check_target(uid, self.seams)
        try:
            self.seams.write(f"{self.cgroup}/{name}", "1")
        except FileNotFoundError:
            return self.uid.no_process(uid)
        except OSError:
            return False   # DECISION: a kernel without the file (cgroup.kill is 5.14+) proves nothing
        got = self._poll(key, want, bound_s)
        if got is None:
            return self.uid.no_process(uid)
        return got

    def freeze(self, uid: int, bound_s: float) -> bool:
        # The freeze proof is cgroup-scoped (comp r1 5); kill + no_process are what cover every process of the uid.
        return self._act("cgroup.freeze", "frozen", "1", uid, bound_s)

    def kill(self, uid: int, bound_s: float) -> bool:
        return self._act("cgroup.kill", "populated", "0", uid, bound_s)

    def no_process(self, uid: int) -> bool:
        uid = check_target(uid, self.seams)
        ev = self.events()
        if ev is not None and ev.get("populated") != "0":
            return False
        return self.uid.no_process(uid)


def end_seams(host: HostOS, cgroup: str | None = None, seams: ProcSeams | None = None
              ) -> tuple[Callable[[int, float], bool], Callable[[int, float], bool], Callable[[int], bool]]:
    """``(freeze, kill, no_process)`` for :class:`levain.slot.end.EndSeams`: Linux the session cgroup
    (alloc's ``cgroup``, required), macOS the uid sweep."""
    if host == "linux":
        if cgroup is None:
            raise Refused("bad-value", "a Linux session names its cgroup")
        c = CgroupProcs(cgroup, seams)
        return c.freeze, c.kill, c.no_process
    if cgroup is not None:
        raise Refused("bad-value", "macOS has no cgroup")
    u = UidProcs(seams or default_seams(host))
    return u.freeze, u.kill, u.no_process


def scrub(slot_uid: int, gw_uid: int, child_uid: int | None = None, *, seams: ProcSeams | None = None,
          bound_s: float = V12_SECONDS) -> None:
    """§7.3 SCRUBBED: ``SIGKILL`` every process of ``gw_uid`` (one front per slot, so the uid alone names
    it), then (macOS) end each uid's launchd domain, then require that none of ``slot_uid``, ``gw_uid`` and
    ``child_uid`` owns a process; else ``Refused("scrub-processes-remain:<uid>")`` naming the first that does,
    and the slot stays leased. A bootout launchctl did not report done (an error or past the shared deadline) is
    ``Refused("scrub-domain-remains:<uid>")``: the process scan cannot see agents launchd would start again. The
    bootouts share one deadline, ``bound_s``, since the caller holds the pool lock. END's kill boots the slot uid
    out too (:meth:`UidProcs.kill`); this second one, after END, takes the per-user agents launchd started for the
    uid while END ran: on a long END they appeared after END's own proof had passed and only SCRUB refused
    (levain-memory runs/s2-macos/RESULT_D1D2.md X1, X6). Both bootouts are needed; neither replaces the other."""
    u = UidProcs(seams)
    uids = [check_target(x, u.seams) for x in (slot_uid, gw_uid) + ((child_uid,) if child_uid is not None else ())]
    u.sweep_kill(gw_uid, bound_s)  # its answer is the proof below, not this sweep's bool
    if u.seams.domain_out is not None:
        deadline = u.seams.clock() + bound_s
        for x in uids:
            left = deadline - u.seams.clock()
            if left <= 0 or not u.seams.domain_out(x, left):
                raise Refused(f"scrub-domain-remains:{x}", "launchctl did not report the uid's domain booted out")
    for x in uids:
        if not u.no_process(x):
            raise Refused(f"scrub-processes-remain:{x}", "a process of the uid survived")


# --- real seams --------------------------------------------------------------------------------------

# Linux: /proc + pidfd.

def parse_linux_proc(pid: int, stat_text: str, status_text: str) -> Proc:
    """``/proc/<pid>/stat`` (state = field 3, starttime = field 22, counted after the ``)`` that ends
    ``comm``, which may itself hold spaces and parentheses) + ``/proc/<pid>/status``'s ``Uid:`` line
    (real, effective, saved, fs)."""
    rest = stat_text[stat_text.rindex(")") + 1:].split()
    st, start = rest[0], int(rest[19])
    uids: tuple[int, ...] = ()
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            uids = tuple(int(x) for x in line.split()[1:4])
    if len(uids) != 3:
        return Proc(pid, (), 0, UNKNOWN)
    state = ZOMBIE if st in ("Z", "X", "x") else STOP if st in ("T", "t") else RUN
    return Proc(pid, uids, start, state)


def linux_probe(pid: int, *, read: Callable[[str], str] = _read_text) -> Proc | None:
    """One pid now. ``stat`` is read before and after ``status``, so a pid that died and was reused
    between the two reads (two different processes' facts) is caught by the start times; a mismatch
    three times running is :data:`UNKNOWN`."""
    base = f"/proc/{pid}"
    try:
        for _ in range(3):
            a = read(base + "/stat")
            s = read(base + "/status")
            b = read(base + "/stat")
            pa, pb = parse_linux_proc(pid, a, s), parse_linux_proc(pid, b, s)
            if pa.start == pb.start:
                return pb
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        return Proc(pid, (), 0, UNKNOWN)
    return Proc(pid, (), 0, UNKNOWN)


def linux_listing(*, listdir: Callable[[str], list[str]] = os.listdir,
                  probe: Callable[[int], Proc | None] = linux_probe) -> list[Proc]:
    out = []
    for name in listdir("/proc"):
        if name.isdigit():
            p = probe(int(name))
            if p is not None:
                out.append(p)
    return out


def linux_seams() -> ProcSeams:
    return ProcSeams(listing=linux_listing, probe=linux_probe, pin=os.pidfd_open,   # type: ignore[attr-defined]
                     send=lambda fd, sig: signal.pidfd_send_signal(fd, sig),       # type: ignore[attr-defined]
                     unpin=os.close)


# macOS: libproc.

PROC_PIDTBSDINFO = 3
_BSDINFO = struct.Struct("=12I16s32s6I2Q")      # struct proc_bsdinfo, <sys/proc_info.h>
SIDL, SRUN, SSLEEP, SSTOP, SZOMB = 1, 2, 3, 4, 5
assert _BSDINFO.size == 136


def parse_bsdinfo(pid: int, raw: bytes) -> Proc:
    f = _BSDINFO.unpack(raw)
    status, uid, ruid, svuid = f[1], f[5], f[7], f[9]
    start = f[20] * 1_000_000 + f[21]
    state = ZOMBIE if status == SZOMB else STOP if status == SSTOP else RUN
    return Proc(pid, (ruid, uid, svuid), start, state)


class _LibProc:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p,
                                          ctypes.c_int]
        self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]

    def probe(self, pid: int) -> Proc | None:
        """``ESRCH`` = gone (a zombie reads as gone too: its bsdinfo is no longer served); any other
        failure is :data:`UNKNOWN`."""
        buf = ctypes.create_string_buffer(_BSDINFO.size)
        n = self.lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buf, _BSDINFO.size)
        if n == _BSDINFO.size:
            p = parse_bsdinfo(pid, buf.raw)
            return p if p.pid == pid else Proc(pid, (), 0, UNKNOWN)
        if ctypes.get_errno() == errno.ESRCH:
            return None
        return Proc(pid, (), 0, UNKNOWN)

    def pids(self) -> list[int]:
        n = self.lib.proc_listallpids(None, 0)
        while True:
            cap = max(n, 0) + 256
            arr = (ctypes.c_int * cap)()
            got = self.lib.proc_listallpids(arr, ctypes.sizeof(arr))
            if got < 0:
                raise OSError(ctypes.get_errno(), "proc_listallpids")
            if got < cap:
                return [arr[i] for i in range(got) if arr[i] > 0]
            n = got * 2

    def listing(self) -> list[Proc]:
        out = []
        for pid in self.pids():
            p = self.probe(pid)
            if p is not None:
                out.append(p)
        return out


#: The child's exit status when it signalled; anything else means nothing was sent (a refused drop: 70).
SIGNAL_SENT = 0


def signal_child(uid: int, sig: int, *, drop: Callable[[int], None] | None = None,
                 kill: Callable[[int, int], None] = os.kill) -> None:
    """The signalling child's whole job, in this order (plan §2e 4): DROP to ``uid`` with the one drop
    (:func:`levain.slot.wall.drop_and_assert`: groups exactly ``{uid}``, gid ``uid`` (§1.1: gid == uid), real
    and effective ids, saved where the host reads them, all ``uid`` != 0, ``seteuid(0)`` proven to fail),
    THEN ``kill(-1, sig)``. A drop that is not proven raises before the kill, so nothing is signalled as root.
    ``kill(-1)`` from a process that is not root reaches only processes of its own uid; on macOS that includes
    the caller (:func:`signal_as_uid` reaps a child its own stop-class signal stopped)."""
    (drop or _drop_to)(uid)
    kill(-1, sig)


def _drop_to(uid: int) -> None:
    drop_and_assert(uid, uid, (uid,), nproc=None)


#: The signals whose default action stops the receiver. XNU's ``kill(-1)`` from a uid that is not root reaches
#: the CALLER too (runs/s2-macos raw/F_M3_exp: the child read ``T`` and its write after the kill never ran).
_STOP_SIGNALS = frozenset({signal.SIGSTOP, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU})


def signal_as_uid(uid: int, sig: int, *, fork: Callable[[], int] = os.fork,
                  waitpid: Callable[[int, int], tuple[int, int]] = os.waitpid,
                  child: Callable[[int, int], None] = signal_child,
                  exit: Callable[[int], None] = os._exit,
                  kill: Callable[[int, int], None] = os.kill) -> bool:
    """macOS's signal (codex r1 3): fork; the child runs :func:`signal_child` and ``_exit``s
    :data:`SIGNAL_SENT`, or 70 if the drop or the kill raised (ESRCH included: nothing of the uid to signal);
    the parent reaps it. Returns whether the child signalled. Only root can run it (the drop; a target of 0
    is refused by the drop itself, ``drop-to-root``, and the sweeps that call this ran :func:`check_target`).

    A stop-class ``sig`` stops the child too (its ``kill(-1)`` reaches its own uid, itself included), so the
    reap waits with ``WUNTRACED``: a child seen STOPPED by ``sig`` counts as sent (``kill(-1)`` delivers to
    every target in one call); a child stopped by any other signal does not. Either way the stopped child is
    then SIGKILLed and reaped, so the call never blocks on it. The pid is this call's own unreaped child, so
    the root kill cannot reach a reused pid. DECISION: a slot process stopping the child with ``sig`` before
    its kill also reads as sent; the sweeps decide on their scans, the return only paces them."""
    pid = fork()
    if pid == 0:   # the child: never returns into the parent's stack
        code = SIGNAL_SENT
        try:
            child(uid, sig)
        except BaseException:   # noqa: BLE001
            code = 70
        finally:
            exit(code)
    _, status = waitpid(pid, os.WUNTRACED)
    if os.WIFSTOPPED(status):
        sent = sig in _STOP_SIGNALS and os.WSTOPSIG(status) == sig
        try:
            kill(pid, signal.SIGKILL)
        except ProcessLookupError:   # already dead: still ours to reap
            pass
        waitpid(pid, 0)
        return sent
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == SIGNAL_SENT


def _never_pin(pid: int) -> object:
    raise OSError(errno.ENOTSUP, "macOS signals by uid (signal_as_uid), never a pid")


def _never_send(handle: object, sig: int) -> None:
    raise OSError(errno.ENOTSUP, "macOS signals by uid (signal_as_uid), never a pid")


def darwin_seams() -> ProcSeams:
    """libproc for the listing and the probe; :func:`signal_as_uid` for every signal. ``pin`` / ``send``
    refuse, so no code path can send a per-pid signal as root on macOS (the pid-reuse window codex r1 3
    named is not narrowed, it is gone)."""
    lp = _LibProc()
    return ProcSeams(listing=lp.listing, probe=lp.probe, pin=_never_pin, send=_never_send,
                     signal_uid=signal_as_uid, domain_out=darwin_bootout)


#: ``launchctl bootout``'s exit while the domain's bootout is already under way (stderr ``Domain is tearing down``).
BOOTOUT_TEARING_DOWN = 124


def darwin_bootout(uid: int, timeout: float = V12_SECONDS) -> bool:
    """``launchctl bootout user/<uid>``, as root: ends the uid's per-user launchd domain and every agent in it
    (lsd, trustd, distnoted, secd), which launchd otherwise keeps for the uid after END's kill
    [measured: levain-memory runs/s2-macos/RESULT_D1D2.md X2 + X6, 2026-10-20]. ``True`` on exit 0, which it
    gives on an already-empty and on a never-created domain too (same file, X3; runs/s3-macos-2f-final Z2b), and on
    124 ``Domain is tearing down``: a bootout of the domain is already under way (END's kill boots out the slot uid
    and SCRUB boots it out again right after a short END; runs/s3-macos-2f-final/raw/Z1_Z2.txt measured 124 on a
    bootout right after one). Any other exit, an error or ``timeout`` is ``False``. The process scan after it is
    still the proof of no process. Never use ``launchctl print user/<uid>`` as a check: it re-creates
    an empty domain (X3)."""
    if type(uid) is not int or uid <= 0:
        raise Refused("bad-target-uid", repr(uid))
    try:
        r = subprocess.run(["/bin/launchctl", "bootout", f"user/{uid}"], capture_output=True, cwd="/",
                           env=child_env(), timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 or (r.returncode == BOOTOUT_TEARING_DOWN and b"tearing down" in (r.stderr or b""))


def default_seams(host: HostOS | None = None) -> ProcSeams:
    host = host or ("darwin" if sys.platform == "darwin" else "linux")
    return darwin_seams() if host == "darwin" else linux_seams()


__all__ = ["CgroupProcs", "POLL_FIRST_S", "POLL_MAX_S", "Proc", "ProcSeams", "RUN", "SIGNAL_SENT", "STOP", "UNKNOWN",
           "UidProcs", "V12_SECONDS", "ZOMBIE", "check_target", "darwin_seams", "default_seams", "end_seams",
           "linux_listing", "linux_probe", "linux_seams", "parse_bsdinfo", "parse_linux_proc", "scrub",
           "signal_as_uid", "signal_checked", "signal_child", "darwin_bootout"]
