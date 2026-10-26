"""The dispatch service (CONTRACT §1.3): ONE process as ``_levain_dispatch``, the dispatch supervisor.

It takes the SUPERVISOR LOCK first and does nothing without it (W2-d DESIGN §2 R0, U5): ``L/dispatch/supervisor.lock``,
a 0600 regular file owned by this uid in dispatch's own 0700 store dir (:func:`levain.slot.layout.dispatch_store_dir`),
opened ``O_NOFOLLOW|O_CLOEXEC``, checked by ``fstat`` before the ``flock``, never unlinked or replaced, held for the
process's life. A start that finds it held retries ``LOCK_NB`` for :data:`LOCK_WAIT_S`, so a restart racing the old
holder's shutdown hands over; past that it logs one line and exits :data:`EX_TEMPFAIL`. The unit's restart policy is
``on-failure`` (:mod:`levain.slot.units`), so the unit manager starts it again, paced by its own restart delay: a holder
can release between the last failed try and the deadline, and an exit the manager does not relaunch would then leave
nobody holding the lock.

With the lock, its body is dispatch's SESSION-EVENTS follower (:func:`levain.dispatch.follow_run.main`) over the job
store in the same dir; the run supervisor's launch step (W-B2b) is not built. The follower stops cleanly on SIGTERM or
SIGINT and returns 0. This program records each SIGTERM / SIGINT it receives (a wakeup fd, so one delivered while the
follower's own handler is installed counts too); when the follower returns 0 after one, it dies by SIGTERM with its
default action, so each unit manager treats the stop as its own policy says (systemd's on-failure does not restart a
SIGTERM death; launchd relaunches one unless the job was booted out). A 0 with no signal recorded exits
:data:`EX_SOFTWARE`, which both relaunch. A run as any user but ``_levain_dispatch`` (root included) exits
:data:`EX_WRONG_USER` before it reads argv, the queue broker service's rule. A usage error and a store dir or lockfile of
the wrong shape exit :data:`EX_CONFIG`.
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
from levain.slot import Refused
from levain.slot import runtime_helper as H
from levain.slot.layout import RUN, STORE, dispatch_store_dir

NAME = "dispatch"
ACCOUNT = "_levain_dispatch"
LOCK_NAME = "supervisor.lock"
#: [judged by 1021+25 head, 10-21] how long a start waits for a held supervisor lock before it exits EX_TEMPFAIL
LOCK_WAIT_S = 30.0
_LOCK_SLICE_S = 0.05
#: sysexits.h: the follower returned 0 though no stop signal reached this program
EX_SOFTWARE = 70
#: sysexits.h: the supervisor lock stayed held for :data:`LOCK_WAIT_S`; both unit managers relaunch it
EX_TEMPFAIL = 75
#: sysexits.h EX_NOPERM: the euid is not :data:`ACCOUNT`'s uid, or that account does not exist
EX_WRONG_USER = 77
#: sysexits.h: a usage error, or a store dir / lockfile not what install made. The Linux unit stays down on it
#: (``RestartPreventExitStatus``); launchd relaunches any non-zero exit.
EX_CONFIG = 78
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


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
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise LockRefused(f"{directory} is uid {st.st_uid} mode {stat.S_IMODE(st.st_mode):04o}, "
                          f"not this uid's ({os.geteuid()}) 0700 directory")


def take_supervisor_lock(directory: Path, *, wait_s: float = LOCK_WAIT_S,
                         monotonic: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], None] = time.sleep) -> Optional[int]:
    """The held lock fd, or None when another holder kept it for ``wait_s``. Raises :class:`LockRefused` for a
    store dir or lockfile of the wrong shape (a link, another owner, a group, other or special bit) and for any error
    of the lock calls but contention."""
    _check_store_dir(directory)
    path = directory / LOCK_NAME
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise LockRefused(f"{path}: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
        mode = stat.S_IMODE(st.st_mode)
        # a umask only clears bits, so a mode inside 0600 is this program's own creation under a narrow umask
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or mode & ~0o600:
            raise LockRefused(f"{path} is not this uid's 0600 regular file (uid {st.st_uid}, mode {mode:04o})")
        if mode != 0o600:
            os.fchmod(fd, 0o600)
        deadline = monotonic() + wait_s
        delay = _LOCK_SLICE_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                pass
            if monotonic() >= deadline:
                break
            sleep(delay)
            delay = min(delay * 2, 0.5)
    except OSError as e:
        _close_quietly(fd)
        raise LockRefused(f"{path}: {e.strerror or e}") from None
    except BaseException:
        _close_quietly(fd)
        raise
    try:
        os.close(fd)
    except OSError as e:
        raise LockRefused(f"{path}: {e.strerror or e}") from None
    return None


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _die_by_sigterm() -> int:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    os.kill(os.getpid(), signal.SIGTERM)
    return 128 + signal.SIGTERM          # only if the process outlives its own default-action SIGTERM


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise _UsageError(message)


class _StopSignals:
    """Records SIGTERM / SIGINT from :meth:`__enter__` to :meth:`__exit__` by the wakeup fd, which receives the
    number of every signal that reaches a Python handler: the follower's while it is installed. It installs no handler,
    so a signal outside the follower's keeps its default action (SIGTERM kills, SIGINT raises KeyboardInterrupt).
    Main thread only (``set_wakeup_fd`` is)."""

    def __init__(self) -> None:
        self.seen: set[int] = set()

    def __enter__(self) -> "_StopSignals":
        self._r, self._w = os.pipe()
        try:
            os.set_blocking(self._r, False)
            os.set_blocking(self._w, False)
            self._old_wakeup = signal.set_wakeup_fd(self._w, warn_on_full_buffer=False)
        except BaseException:
            os.close(self._r)
            os.close(self._w)
            raise
        return self

    def __exit__(self, *_exc) -> None:
        signal.set_wakeup_fd(self._old_wakeup)
        try:
            while True:
                chunk = os.read(self._r, 512)
                if not chunk:
                    break
                self.seen.update(b for b in chunk if b in _STOP_SIGNALS)
        except BlockingIOError:
            pass
        os.close(self._r)
        os.close(self._w)

    @property
    def stopped(self) -> bool:
        return bool(self.seen)


def _parser(host: str) -> argparse.ArgumentParser:
    ap = _Parser(prog="levain dispatch service",
                 description="the dispatch supervisor; its unit runs it as _levain_dispatch.")
    ap.add_argument("--store-dir", default=str(dispatch_store_dir(host)), help="dispatch's store (job state, lock)")
    ap.add_argument("--socket", default=str(RUN[host] / "control" / "control.sock"), help="the socket CONTROL serves")
    ap.add_argument("--export-dir", default=str(STORE[host] / "export" / "dispatch"),
                    help="root's export/dispatch directory")
    return ap


def main(argv: Optional[Sequence[str]] = None, *, wait_s: float = LOCK_WAIT_S,
         follow: Optional[Callable[[Sequence[str]], int]] = None, die: Callable[[], int] = _die_by_sigterm) -> int:
    seams = H.Seams()
    try:
        me = seams.account_uid(ACCOUNT)
    except Refused:
        me = None
    if me is None or os.geteuid() != me:
        print(f"levain dispatch: must run as {ACCOUNT} (the service unit runs it)", file=sys.stderr)
        return EX_WRONG_USER
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [NAME]:                  # ``service-entry dispatch`` leaves the program's own name
        args = args[1:]
    ap = _parser(_host())
    try:
        a = ap.parse_args(args)
        for flag, v in (("--store-dir", a.store_dir), ("--socket", a.socket), ("--export-dir", a.export_dir)):
            if not os.path.isabs(v):
                ap.error(f"{flag} must be an absolute path")
    except _UsageError as e:
        ap.print_usage(sys.stderr)
        print(f"{ap.prog}: error: {e}", file=sys.stderr)
        return EX_CONFIG
    store = Path(a.store_dir)
    try:
        fd = take_supervisor_lock(store, wait_s=wait_s)
    except LockRefused as e:
        _log(f"refused: {e}")
        return EX_CONFIG
    if fd is None:
        _log(f"another dispatch supervisor held {store / LOCK_NAME} for {wait_s:g} s; exiting {EX_TEMPFAIL} to be "
             f"started again")
        return EX_TEMPFAIL
    try:
        _log(f"holding {store / LOCK_NAME}")
        with _StopSignals() as stops:
            rc = (follow or FR.main)(["--store-dir", str(store), "--socket", a.socket, "--export-dir", a.export_dir])
    finally:
        os.close(fd)
    if rc != 0:
        return rc
    if stops.stopped:
        return die()
    _log(f"the follower returned 0 with no stop signal; exiting {EX_SOFTWARE}")
    return EX_SOFTWARE


__all__ = ["ACCOUNT", "EX_CONFIG", "EX_SOFTWARE", "EX_TEMPFAIL", "EX_WRONG_USER", "LOCK_NAME", "LOCK_WAIT_S",
           "LockRefused", "main", "take_supervisor_lock"]


if __name__ == "__main__":
    sys.exit(main())
