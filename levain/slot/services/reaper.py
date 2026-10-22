"""The reaper service (CONTRACT §1.3, §23; reaper DESIGN §4.1): ONE root service of three single-threaded processes.

The MAIN process runs §4.2 step 1 (``R/`` and every §2.2 run dir, then ``R/reaper/wake.sock`` bound and its path set
root : ``levain-wake`` 0660 before anything is read from it), then forks the PASS WORKER and the REVOKER and only
supervises them: it relays each wake datagram (content ignored) as one byte on each child's pipe, and restarts a child
that died. It never signals a slot uid. Before it touches ``wake.sock`` it takes the service-instance lock
(``R/reaper/service.lock``), which both children inherit, so a second instance, or a restart while an orphaned child
still runs, waits for that lock and then exits instead of running beside them. Each child exits when its pipe reaches
EOF (the main process is gone); on Linux the kernel also sends it SIGTERM then, so it does not finish a long pass first.

The worker runs §4.2 steps 2-4 and then the pass (:meth:`levain.slot.reaper.Reaper.run_pass`) on every V11 tick and
on wakes, coalesced: a wake during a pass leaves bytes in the pipe, so one more pass follows, never sooner than V18
after the previous pass began (a flood is one extra pass). The revoker runs :meth:`levain.slot.reaper.Revoker.wake` on
every wake and every V11 tick, and every :data:`levain.slot.reaper.REVOKER_RETRY_S` while a front kill is pending.

Why processes and not threads (RH2): the macOS signalling child is a ``fork()``, and forking a process with a second
Python thread can leave the child blocked on a lock that thread held. Each process here runs one Python thread; the
signalling child's wait is bounded by its caller's deadline either way (RH2).
"""
from __future__ import annotations

import errno
import os
import select
import signal
import socket
import stat
import sys
import time
from typing import Callable, List, Optional

from levain.slot import reaper as RP
from levain.slot import runtime_helper as H

WAKE_SOCK = "wake.sock"
#: DECISION: a child that died is restarted no sooner than this after its last start (a crash loop costs one fork a
#: second, never a spin).
RESTART_S = 1.0
#: DECISION: the main process's select tick (how soon it sees a dead child).
SUPERVISE_TICK_S = 0.5
INSTANCE_LOCK = "service.lock"
#: DECISION: how long a starting instance waits for the instance lock (an orphaned child finishing its pass) before it
#: exits :data:`EX_INSTANCE_HELD` and its unit manager starts it again.
INSTANCE_WAIT_S = 10.0
EX_INSTANCE_HELD = 75                                     # EX_TEMPFAIL
#: DECISION: the most wake datagrams the main process reads in one tick (a flood never starves child supervision; the
#: rest wait for the next tick, and one relayed byte already means "run again").
WAKE_DRAIN_MAX = 64


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"levain reaper[{os.getpid()}]: {msg}\n")
        sys.stderr.flush()
    except Exception:   # noqa: BLE001
        pass


def bind_wake(seams: H.Seams, *, chown_path: Callable[[str, int, int], None] = os.chown) -> socket.socket:
    """``R/reaper/wake.sock``: a stale socket left by a dead reaper is removed (anything else at the path refuses);
    bound, then the path set 0660 and root : ``levain-wake`` (§2.2), before the first read."""
    path = str(seams.R / "reaper" / WAKE_SOCK)
    try:
        st = os.lstat(path)
        if not stat.S_ISSOCK(st.st_mode):
            raise OSError(errno.EEXIST, f"{path} exists and is not a socket")
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(path)
        os.chmod(path, 0o660)
        chown_path(path, 0, seams.group_gid("levain-wake"))
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def take_instance_lock(seams: H.Seams, *, wait_s: float) -> Optional[int]:
    """The held service-instance lock fd, or None when another holder kept it for ``wait_s``. The fd is close-on-exec,
    so only this process and its forks hold it."""
    fd = os.open(str(seams.R / "reaper" / INSTANCE_LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        delay = H.LOCK_SLICE_S
        while not H.try_flock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _end_with_parent(parent: int, log: Callable[[str], None]) -> None:
    """Linux only: SIGTERM to this child when the main process dies (``PR_SET_PDEATHSIG``). macOS has no such call;
    the pipe's EOF and the instance lock cover it there."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:   # 1 = PR_SET_PDEATHSIG
            log(f"prctl(PR_SET_PDEATHSIG) failed: errno {ctypes.get_errno()}")
    except Exception as e:   # noqa: BLE001 - the pipe's EOF still ends the child
        log(f"prctl(PR_SET_PDEATHSIG) unavailable: {type(e).__name__}: {e}")
    if os.getppid() != parent:                             # the main process died before the call took
        os._exit(0)


def _drain(fd: int) -> bool:
    """Read every pending wake byte; False when the pipe reached EOF (the main process is gone)."""
    while True:
        try:
            got = os.read(fd, 4096)
        except BlockingIOError:
            return True
        except InterruptedError:
            continue
        if not got:
            return False
        if len(got) < 4096:
            return True


def _wait(fd: int, timeout: float) -> None:
    try:
        select.select([fd], [], [], max(0.0, timeout))
    except InterruptedError:
        pass


def worker_loop(reaper: RP.Reaper, wake_fd: int, *, passes: Optional[int] = None, tick_s: float = RP.V11_S,
                log: Callable[[str], None] = _log) -> int:
    log(f"worker pid {os.getpid()} parent {os.getppid()}")
    try:
        reaper.boot_steps()
    except Exception as e:   # noqa: BLE001 - a bad boot step is logged; the passes run their own steps
        log(f"worker boot failed: {type(e).__name__}: {e}")
    mono = reaper.seams.monotonic
    last = float("-inf")
    n = 0
    while True:
        gap = last + RP.V18_S - mono()
        if gap > 0:
            reaper.seams.sleep(gap)
        if not _drain(wake_fd):
            log("worker: the main process is gone; exiting")
            return 0
        last = mono()
        n += 1
        try:
            rep = reaper.run_pass()
        except Exception as e:   # noqa: BLE001 - one bad pass is logged; the next tick or wake runs another
            log(f"worker pass failed: {type(e).__name__}: {e}")
        else:
            log(f"worker pass {rep['pass_seq']}: control {rep['control']} swept {rep['swept']} "
                f"revoked {rep['revoked']} ended {rep['ended']} scrubbed {rep['scrubbed']} "
                f"reverted {rep['reverted']} held {rep['held']}")
        if passes is not None and n >= passes:
            return 0
        _wait(wake_fd, last + tick_s - mono())


def revoker_loop(revoker: RP.Revoker, wake_fd: int, *, wakes: Optional[int] = None, tick_s: float = RP.V11_S,
                 log: Callable[[str], None] = _log) -> int:
    log(f"revoker pid {os.getpid()} parent {os.getppid()}")
    n = 0
    while True:
        n += 1
        slow = False
        try:
            out = revoker.wake()
        except Exception as e:   # noqa: BLE001 - one bad wake is logged and retried like a slow one
            log(f"revoker wake {n} failed: {type(e).__name__}: {e}")
            slow = True
        else:
            log(f"revoker wake {n}: control {out['control']} outcomes {out['outcomes']}")
            slow = out["control"] == "slow"
        if wakes is not None and n >= wakes:
            return 0
        _wait(wake_fd, RP.REVOKER_RETRY_S if revoker.pending() or slow else tick_s)
        if not _drain(wake_fd):
            log("revoker: the main process is gone; exiting")
            return 0


class _Child:
    def __init__(self, role: str, run: Callable[[int], int], bound: Optional[int]) -> None:
        self.role, self.run, self.bound = role, run, bound
        self.pid = -1
        self.wfd = -1
        self.started = float("-inf")
        self.finished = False


def serve(seams: H.Seams, *, tuning: RP.Tuning = RP.Tuning(), worker_passes: Optional[int] = None,
          revoker_wakes: Optional[int] = None, tick_s: float = RP.V11_S,
          chown_path: Callable[[str, int, int], None] = os.chown, log: Callable[[str], None] = _log,
          netns: Optional[Callable[[list], None]] = None) -> int:
    """The main process. ``worker_passes`` / ``revoker_wakes`` bound a child for a host smoke run (a child that ran
    its bound exits 0 and is not restarted; the main process returns once every bounded child has, and stops an
    unbounded one); production passes None. Returns :data:`EX_INSTANCE_HELD` when another instance holds the lock."""
    log(f"main pid {os.getpid()}")
    RP.boot_dirs(seams, log=log)
    lock = take_instance_lock(seams, wait_s=INSTANCE_WAIT_S)
    if lock is None:
        log("another reaper instance, or a child it left, holds the instance lock; exiting")
        return EX_INSTANCE_HELD
    main_pid = os.getpid()
    sock: Optional[socket.socket] = None
    children: List[_Child] = []
    stop: List[int] = []
    old: dict = {}

    def on_term(signum, _frame) -> None:
        stop.append(signum)

    def start(c: _Child) -> None:
        r, w = os.pipe()
        os.set_blocking(w, False)
        os.set_blocking(r, False)
        c.started = time.monotonic()
        pid = os.fork()
        if pid == 0:                                       # the child: one Python thread, its own read end only
            code = 70
            try:
                for s in old:
                    signal.signal(s, signal.SIG_DFL)
                _end_with_parent(main_pid, log)
                sock.close()
                os.close(w)
                for other in children:
                    if other.wfd >= 0:
                        os.close(other.wfd)
                code = c.run(r)
            except BaseException as e:   # noqa: BLE001 - the main process restarts a child that died
                log(f"{c.role}: {type(e).__name__}: {e}")
            finally:
                os._exit(code)
        os.close(r)
        if c.wfd >= 0:
            os.close(c.wfd)
        c.pid, c.wfd = pid, w
        log(f"started {c.role} pid {pid} (main {os.getpid()})")

    try:
        sock = bind_wake(seams, chown_path=chown_path)
        reaper = RP.Reaper(seams, tuning=tuning, log=log, netns=netns)
        revoker = RP.Revoker(seams, log=log)
        children[:] = [
            _Child("worker", lambda fd: worker_loop(reaper, fd, passes=worker_passes, tick_s=tick_s, log=log),
                   worker_passes),
            _Child("revoker", lambda fd: revoker_loop(revoker, fd, wakes=revoker_wakes, tick_s=tick_s, log=log),
                   revoker_wakes),
        ]
        for s in (signal.SIGTERM, signal.SIGINT):
            old[s] = signal.signal(s, on_term)
        for c in children:
            if stop:
                break
            start(c)
        bounded = [c for c in children if c.bound is not None]
        while not stop:
            try:
                ready, _, _ = select.select([sock], [], [], SUPERVISE_TICK_S)
            except InterruptedError:
                ready = []
            if ready:
                n = 0
                while n < WAKE_DRAIN_MAX:
                    try:
                        sock.recv(64)
                        n += 1
                    except BlockingIOError:
                        break
                for c in children:
                    if c.wfd >= 0 and not c.finished:
                        try:
                            os.write(c.wfd, b"w")
                        except (BlockingIOError, BrokenPipeError):
                            pass                           # a full pipe already holds a wake: coalesced
                log(f"wake: {n} datagram(s) relayed")
            for c in children:
                if c.finished or c.pid < 0:
                    continue
                got, status = os.waitpid(c.pid, os.WNOHANG)
                if got == 0:
                    continue
                code = os.waitstatus_to_exitcode(status)
                if code == 0 and c.bound is not None:
                    c.finished = True
                    if c.wfd >= 0:
                        os.close(c.wfd)
                        c.wfd = -1
                    log(f"{c.role} pid {c.pid} finished its bound")
                    continue
                log(f"{c.role} pid {c.pid} died ({code}); restarting")
                c.pid = -1
                time.sleep(max(0.0, c.started + RESTART_S - time.monotonic()))
                if stop:
                    break
                start(c)
            if bounded and all(c.finished for c in bounded):
                return 0
        return 0
    finally:
        for c in children:
            if c.pid > 0 and not c.finished:
                try:
                    os.kill(c.pid, signal.SIGTERM)
                    os.waitpid(c.pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
            if c.wfd >= 0:
                os.close(c.wfd)
        if sock is not None:                               # only the socket this instance bound is unlinked
            sock.close()
            try:
                os.unlink(str(seams.R / "reaper" / WAKE_SOCK))
            except FileNotFoundError:
                pass
        for s, h in old.items():
            signal.signal(s, h)
        os.close(lock)


def main(argv: Optional[List[str]] = None) -> int:
    if os.geteuid() != 0:
        print("levain reaper: must run as root (the service unit runs it)", file=sys.stderr)
        return 77
    return serve(H.Seams())


__all__ = ["EX_INSTANCE_HELD", "INSTANCE_LOCK", "INSTANCE_WAIT_S", "RESTART_S", "WAKE_DRAIN_MAX", "WAKE_SOCK",
           "bind_wake", "main", "revoker_loop", "serve", "take_instance_lock", "worker_loop"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
