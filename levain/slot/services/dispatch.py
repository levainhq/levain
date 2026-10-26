"""The dispatch service (CONTRACT §1.3): ONE process as ``_levain_dispatch``, the dispatch supervisor.

It takes the SUPERVISOR LOCK first and does nothing without it (W2-d DESIGN §2 R0, U5): ``L/dispatch/supervisor.lock``,
a 0600 regular file owned by this uid in dispatch's own 0700 store dir (:func:`levain.slot.layout.dispatch_store_dir`),
opened ``O_NOFOLLOW|O_CLOEXEC``, checked by ``fstat`` before the ``flock``, never unlinked or replaced, held for the
process's life. A start that finds it held retries ``LOCK_NB`` for :data:`LOCK_WAIT_S`, so a restart racing the old
holder's shutdown hands over; past that a live holder exists, and it logs one line and exits 0. The unit's restart
policy is ``on-failure`` (:mod:`levain.slot.units`), so that exit 0 is the one exit not relaunched.

With the lock, its body is dispatch's SESSION-EVENTS follower (:func:`levain.dispatch.follow_run.main`) over the job
store in the same dir; the run supervisor's launch step (W-B2b) is not built. The follower stops cleanly on SIGTERM or
SIGINT and returns 0; this program then dies by SIGTERM with its default action, so each unit manager treats the stop
as its own policy says (systemd's on-failure does not restart a SIGTERM death; launchd relaunches one unless the job
was booted out), and an exit 0 still means only the lock loser.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from levain.dispatch import follow_run as FR
from levain.slot.layout import RUN, STORE, dispatch_store_dir

NAME = "dispatch"
LOCK_NAME = "supervisor.lock"
#: [judged by 1021+25 head, 10-21] how long a start waits for a held supervisor lock before it exits 0
LOCK_WAIT_S = 30.0
_LOCK_SLICE_S = 0.05
EX_USAGE = 64
#: sysexits.h: the store dir or the lockfile is not what install made. The Linux unit stays down on it
#: (``RestartPreventExitStatus``); launchd relaunches any non-zero exit.
EX_CONFIG = 78


class LockRefused(Exception):
    """The store dir or the lockfile is not the one this uid owns with the rows' shape."""


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"levain dispatch[{os.getpid()}]: {msg}\n")
        sys.stderr.flush()
    except Exception:   # noqa: BLE001
        pass


def _host() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def _check_store_dir(directory: Path) -> None:
    try:
        st = os.lstat(directory)
    except OSError as e:
        raise LockRefused(f"{directory}: {e.strerror or e}") from None
    if not stat.S_ISDIR(st.st_mode):
        raise LockRefused(f"{directory} is not a directory")
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise LockRefused(f"{directory} is uid {st.st_uid} mode {stat.S_IMODE(st.st_mode):04o}, "
                          f"not this uid's ({os.geteuid()}) 0700 directory")


def take_supervisor_lock(directory: Path, *, wait_s: float = LOCK_WAIT_S,
                         monotonic: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], None] = time.sleep) -> Optional[int]:
    """The held lock fd, or None when another holder kept it for ``wait_s``. Raises :class:`LockRefused` for a
    store dir or lockfile of the wrong shape (a link, another owner, a group or other bit)."""
    _check_store_dir(directory)
    path = directory / LOCK_NAME
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise LockRefused(f"{path}: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise LockRefused(f"{path} is not this uid's 0600 regular file "
                              f"(uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):04o})")
        deadline = monotonic() + wait_s
        delay = _LOCK_SLICE_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                pass
            if monotonic() >= deadline:
                os.close(fd)
                return None
            sleep(delay)
            delay = min(delay * 2, 0.5)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _die_by_sigterm() -> int:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
    return 128 + signal.SIGTERM          # reached only if the signal is blocked


def _parser(host: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="levain dispatch service",
                                 description="the dispatch supervisor; its unit runs it as _levain_dispatch.")
    ap.add_argument("--store-dir", default=str(dispatch_store_dir(host)), help="dispatch's store (job state, lock)")
    ap.add_argument("--socket", default=str(RUN[host] / "control" / "control.sock"), help="the socket CONTROL serves")
    ap.add_argument("--export-dir", default=str(STORE[host] / "export" / "dispatch"),
                    help="root's export/dispatch directory")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, wait_s: float = LOCK_WAIT_S,
         follow: Optional[Callable[[Sequence[str]], int]] = None, die: Callable[[], int] = _die_by_sigterm) -> int:
    if os.geteuid() == 0:
        print("levain dispatch: refusing to run as root (it runs as _levain_dispatch)", file=sys.stderr)
        return EX_USAGE
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [NAME]:                  # ``service-entry dispatch`` leaves the program's own name
        args = args[1:]
    ap = _parser(_host())
    a = ap.parse_args(args)
    for flag, v in (("--store-dir", a.store_dir), ("--socket", a.socket), ("--export-dir", a.export_dir)):
        if not os.path.isabs(v):
            ap.error(f"{flag} must be an absolute path")
    store = Path(a.store_dir)
    try:
        fd = take_supervisor_lock(store, wait_s=wait_s)
    except LockRefused as e:
        _log(f"refused: {e}")
        return EX_CONFIG
    if fd is None:
        _log(f"another dispatch supervisor holds {store / LOCK_NAME}; exiting 0, not restarted")
        return 0
    try:
        _log(f"holding {store / LOCK_NAME}")
        rc = (follow or FR.main)(["--store-dir", str(store), "--socket", a.socket, "--export-dir", a.export_dir])
    finally:
        os.close(fd)
    # the follower returns 0 only when SIGTERM or SIGINT stopped it [judged by dsup, 10-26, against
    # levain/dispatch/follow_run.py at 4b540ebb: ``run()`` returns only once ``stop()`` ran, and only its signal
    # handler calls ``stop()``]
    return die() if rc == 0 else rc


__all__ = ["EX_CONFIG", "LOCK_NAME", "LOCK_WAIT_S", "LockRefused", "main", "take_supervisor_lock"]


if __name__ == "__main__":
    sys.exit(main())
