"""The queue broker service (CONTRACT §1.3, §23): ONE process as ``_levain_qbroker``, serving every egress slot's
``R/qb/slot<n>/q.sock`` with the broker :mod:`levain.qbroker.__main__` builds.

The unit passes no slot list (:func:`levain.slot.units.argv`), so the program reads the slots itself: the egress slots
under ``L/slots/`` (:func:`levain.slot.runtime_helper.configured_slots`, the reaper's own reading), each served on its
§2.2 socket with the uid and group of the slot's primary account (:attr:`levain.slot.ids.SlotId.primary`). A slot whose
account, group or socket dir is not there yet is logged and tried again at the next scan; the scan repeats every
:data:`RESCAN_S`, so a slot ``replenish`` adds is served without restarting the unit, and a slot gone from ``L/slots/``
stops being served.

Before it binds anything it takes the service-instance lock (``L/queue/requests/service.lock``): a listener unlinks a
socket already at its path, so a second instance would otherwise take the first one's sockets from under it. Runs until
SIGTERM or SIGINT.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from levain.qbroker import __main__ as QM
from levain.qbroker.server import SlotListener, socket_path
from levain.slot import Refused
from levain.slot import runtime_helper as H
from levain.slot.ids import SlotId

ACCOUNT = "_levain_qbroker"
#: PROVISIONAL (head rules): how often the slot list is read again (a replenished slot waits at most this long).
RESCAN_S = 30.0
#: PROVISIONAL (head rules): the lock's place. ``L/queue/requests/`` is the one store dir the broker owns (§2.1); the
#: queue's readers there (``git grep -n -e iterdir -e glob -- levain/qbroker``) take only ``pend-*.json`` names.
INSTANCE_LOCK = "service.lock"
#: how long a starting instance waits for the instance lock before it exits :data:`EX_INSTANCE_HELD` and its unit
#: manager starts it again (the reaper's value, :data:`levain.slot.services.reaper.INSTANCE_WAIT_S`).
INSTANCE_WAIT_S = 10.0
EX_INSTANCE_HELD = 75                                     # EX_TEMPFAIL
EX_WRONG_USER = 77                                        # EX_NOPERM


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"levain qbroker[{os.getpid()}]: {msg}\n")
        sys.stderr.flush()
    except Exception:   # noqa: BLE001
        pass


def egress_slots(seams: H.Seams) -> List[int]:
    """The egress slot indices ``L/slots/`` holds, sorted (the store opened as :class:`levain.slot.runtime_helper.Store`
    opens it: root's dirs only)."""
    store = H.Store(seams)
    try:
        return sorted(H.configured_slots(store)["egress"])
    finally:
        store.close()


def take_instance_lock(seams: H.Seams, *, wait_s: float) -> Optional[int]:
    """The held lock fd, or None when another holder kept it for ``wait_s``. Close-on-exec."""
    fd = os.open(str(seams.L / "queue" / "requests" / INSTANCE_LOCK),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
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


class Slots:
    """The running listeners, one per served egress slot; :meth:`sync` brings them to the slot list read now."""

    def __init__(self, broker: object, seams: H.Seams, *, log: Callable[[str], None] = _log,
                 listener: Callable[..., SlotListener] = SlotListener) -> None:
        self.broker, self.seams, self.log, self._listener = broker, seams, log, listener
        self.running: Dict[int, SlotListener] = {}
        self._failed: Dict[int, str] = {}      # the last failure logged per slot (a repeat is not logged again)

    def sync(self) -> None:
        try:
            want = egress_slots(self.seams)
        except (OSError, Refused) as e:     # the store unreadable: what runs keeps running, the next scan reads again
            self._note(-1, f"the slot list could not be read: {type(e).__name__}: {e}")
            return
        self._failed.pop(-1, None)
        for n in [n for n in self.running if n not in want]:
            self.running.pop(n).stop()
            self._failed.pop(n, None)
            self.log(f"slot {n}: no longer in L/slots; stopped serving it")
        for n in want:
            if n not in self.running:
                self._start(n)

    def _start(self, n: int) -> None:
        name = SlotId("egress", n).primary
        path = socket_path(self.seams.R, n)
        try:
            uid, gid = self.seams.account_uid(name), self.seams.group_gid(name)
            if not os.path.isdir(os.path.dirname(path)):
                raise FileNotFoundError(f"{os.path.dirname(path)} is not there")
            lst = self._listener(self.broker, n, path, uid, expected_gid=gid)
            lst.start()
        except Exception as e:   # noqa: BLE001 - one slot that cannot be served yet never stops the others
            self._note(n, f"not served yet: {type(e).__name__}: {e}")
            return
        self.running[n] = lst
        self._failed.pop(n, None)
        self.log(f"slot {n}: serving {path} for {name} (uid {uid}, gid {gid})")

    def _note(self, n: int, msg: str) -> None:
        if self._failed.get(n) != msg:
            self._failed[n] = msg
            self.log(msg if n < 0 else f"slot {n}: {msg}")

    def stop(self) -> None:
        for n in sorted(self.running):
            self.running.pop(n).stop()


def serve(seams: H.Seams, *, limits_owner: int = 0, rescan_s: float = RESCAN_S,
          stop: Optional[threading.Event] = None, log: Callable[[str], None] = _log,
          build: Callable[..., object] = QM.build_broker) -> int:
    """Serve until ``stop`` is set (SIGTERM / SIGINT set it when the caller passes none). ``limits_owner``: the uid
    ``L/policy/limits`` must belong to (root in production; a test cannot be root). Returns :data:`EX_INSTANCE_HELD`
    when another instance holds the lock."""
    log(f"main pid {os.getpid()} uid {os.geteuid()}")
    lock = take_instance_lock(seams, wait_s=INSTANCE_WAIT_S)
    if lock is None:
        log("another queue broker instance holds the instance lock; exiting")
        return EX_INSTANCE_HELD
    old: dict = {}
    slots: Optional[Slots] = None
    try:
        if stop is None:
            stop = threading.Event()
            for s in (signal.SIGTERM, signal.SIGINT):
                old[s] = signal.signal(s, lambda *_: stop.set())   # type: ignore[union-attr]
        broker = build(argparse.Namespace(store=str(seams.L), run=str(seams.R)), limits_owner=limits_owner)
        slots = Slots(broker, seams, log=log)
        while not stop.is_set():
            slots.sync()
            stop.wait(rescan_s)
        log("stopping")
        return 0
    finally:
        if slots is not None:
            slots.stop()
        for s, h in old.items():
            signal.signal(s, h)
        os.close(lock)


def main(argv: Optional[List[str]] = None) -> int:
    seams = H.Seams()
    try:
        me = seams.account_uid(ACCOUNT)
    except Refused:
        me = None
    if me is None or os.geteuid() != me:
        print(f"levain qbroker: must run as {ACCOUNT} (the service unit runs it)", file=sys.stderr)
        return EX_WRONG_USER
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return serve(seams)


__all__ = ["ACCOUNT", "EX_INSTANCE_HELD", "EX_WRONG_USER", "INSTANCE_LOCK", "INSTANCE_WAIT_S", "RESCAN_S", "Slots",
           "egress_slots", "main", "serve", "take_instance_lock"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
