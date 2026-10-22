"""The reaper service (CONTRACT §1.3, §23; reaper DESIGN §4.1): ONE root service of three single-threaded processes.

The MAIN process runs §4.2 step 1 (``R/`` and every §2.2 run dir, then ``R/reaper/wake.sock`` bound and its path set
root : ``levain-wake`` 0660 before anything is read from it), then forks the PASS WORKER and the REVOKER and only
supervises them: it relays each wake datagram (content ignored) as one byte on each child's pipe, and restarts a child
that died. It never signals a slot uid. Each child exits when its pipe reaches EOF (the main process is gone), so a
restarted service never runs beside an orphaned worker.

The worker runs §4.2 steps 2-4 and then the pass (:meth:`levain.slot.reaper.Reaper.run_pass`) on every V11 tick and
on wakes, coalesced: a wake during a pass leaves bytes in the pipe, so one more pass follows, never sooner than V18
after the previous pass began (a flood is one extra pass). The revoker runs :meth:`levain.slot.reaper.Revoker.wake` on
every wake and every V11 tick, and every :data:`levain.slot.reaper.REVOKER_RETRY_S` while a front kill is pending.

Why processes and not threads (RH2): the macOS signalling child is a ``fork()``, and forking a process with a second
Python thread can leave the child blocked on a lock that thread held. Each process here runs one Python thread; the
signalling child's wait is bounded by its caller's deadline either way.
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
    reaper.boot_steps()
    mono = reaper.seams.monotonic
    last = float("-inf")
    n = 0
    while True:
        gap = last + RP.V18_S - mono()
        if gap > 0:
            time.sleep(gap)
        if not _drain(wake_fd):
            log("worker: the main process is gone; exiting")
            return 0
        last = mono()
        rep = reaper.run_pass()
        n += 1
        log(f"worker pass {rep['pass_seq']}: control {rep['control']} swept {rep['swept']} revoked {rep['revoked']} "
            f"ended {rep['ended']} scrubbed {rep['scrubbed']} reverted {rep['reverted']} held {rep['held']}")
        if passes is not None and n >= passes:
            return 0
        _wait(wake_fd, last + tick_s - mono())


def revoker_loop(revoker: RP.Revoker, wake_fd: int, *, wakes: Optional[int] = None, tick_s: float = RP.V11_S,
                 log: Callable[[str], None] = _log) -> int:
    log(f"revoker pid {os.getpid()} parent {os.getppid()}")
    n = 0
    while True:
        out = revoker.wake()
        n += 1
        log(f"revoker wake {n}: control {out['control']} outcomes {out['outcomes']}")
        if wakes is not None and n >= wakes:
            return 0
        slow = out["control"] == "slow"
        _wait(wake_fd, RP.REVOKER_RETRY_S if revoker.pending() or slow else tick_s)
        if not _drain(wake_fd):
            log("revoker: the main process is gone; exiting")
            return 0


class _Child:
    def __init__(self, role: str, run: Callable[[int], int]) -> None:
        self.role, self.run = role, run
        self.pid = -1
        self.wfd = -1
        self.started = float("-inf")
        self.finished = False


def serve(seams: H.Seams, *, tuning: RP.Tuning = RP.Tuning(), worker_passes: Optional[int] = None,
          revoker_wakes: Optional[int] = None, tick_s: float = RP.V11_S,
          chown_path: Callable[[str, int, int], None] = os.chown, log: Callable[[str], None] = _log,
          netns: Optional[Callable[[list], None]] = None) -> int:
    """The main process. ``worker_passes`` / ``revoker_wakes`` bound a child for a host smoke run (a child that ran
    its bound exits 0 and is not restarted; the main process returns once both have); production passes None."""
    log(f"main pid {os.getpid()}")
    RP.boot_dirs(seams, log=log)
    sock = bind_wake(seams, chown_path=chown_path)
    reaper = RP.Reaper(seams, tuning=tuning, log=log, netns=netns)
    revoker = RP.Revoker(seams, log=log)
    children = [
        _Child("worker", lambda fd: worker_loop(reaper, fd, passes=worker_passes, tick_s=tick_s, log=log)),
        _Child("revoker", lambda fd: revoker_loop(revoker, fd, wakes=revoker_wakes, tick_s=tick_s, log=log)),
    ]
    stop: List[int] = []

    def on_term(signum, _frame) -> None:
        stop.append(signum)
    old = {s: signal.signal(s, on_term) for s in (signal.SIGTERM, signal.SIGINT)}

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
        for c in children:
            start(c)
        while not stop:
            try:
                ready, _, _ = select.select([sock], [], [], SUPERVISE_TICK_S)
            except InterruptedError:
                ready = []
            if ready:
                n = 0
                while True:
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
                if code == 0 and (worker_passes is not None or revoker_wakes is not None):
                    c.finished = True
                    log(f"{c.role} pid {c.pid} finished its bound")
                    continue
                log(f"{c.role} pid {c.pid} died ({code}); restarting")
                c.pid = -1
                time.sleep(max(0.0, c.started + RESTART_S - time.monotonic()))
                start(c)
            if all(c.finished for c in children):
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
        sock.close()
        try:
            os.unlink(str(seams.R / "reaper" / WAKE_SOCK))
        except FileNotFoundError:
            pass
        for s, h in old.items():
            signal.signal(s, h)


def main(argv: Optional[List[str]] = None) -> int:
    if os.geteuid() != 0:
        print("levain reaper: must run as root (the service unit runs it)", file=sys.stderr)
        return 77
    return serve(H.Seams())


__all__ = ["RESTART_S", "WAKE_SOCK", "bind_wake", "main", "revoker_loop", "serve", "worker_loop"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
