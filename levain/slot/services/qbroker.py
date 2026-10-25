"""The queue broker service (CONTRACT §1.3, §23): ONE process as ``_levain_qbroker``, serving every egress slot's
``R/qb/slot<n>/q.sock`` with the broker :mod:`levain.qbroker.__main__` builds.

The unit passes no slot list (:func:`levain.slot.units.argv`), so the program reads the slots itself: the egress slots
in service (:func:`scan`: the slots :func:`levain.slot.install_helper.slots_in_service` names, read the same way), each
served on its §2.2 socket with the uid and group of the slot's primary account (:attr:`levain.slot.ids.SlotId.primary`).
A slot whose account, group or socket dir is not there yet is logged and tried again at the next scan; the scan repeats
every :data:`RESCAN_S`, so a slot ``replenish`` adds is served without restarting the unit. A slot that
:data:`MISSES_TO_STOP` scans in a row do not find in service (gone from ``L/slots/``, or retired) stops being served,
the connections it holds closed. A scan that cannot read the slot list, or one slot's state record, changes nothing it
could not read: what runs keeps running.

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
from typing import Callable, Dict, List, Optional, Tuple

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
#: PROVISIONAL (head rules, L3 r1 item 6): consecutive scans that must miss a served slot before it is stopped, so a
#: directory renamed for the moment one scan reads ``L/slots/`` does not drop its clients.
MISSES_TO_STOP = 2


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"levain qbroker[{os.getpid()}]: {msg}\n")
        sys.stderr.flush()
    except Exception:   # noqa: BLE001
        pass


def _retired(store: H.Store, n: int) -> bool:
    """Slot n's state record says ``retired``; no record is in service. Reads ``state`` only, never ``keep/`` (root's
    0700, which this account cannot open): :func:`levain.slot.runtime_helper.read_view`'s reading of that one file."""
    slot = SlotId("egress", n)
    with store.dir("slots", slot.tag) as sfd:
        raw = H.read_at(sfd, "state", f"slots/{slot.tag}/state")
    return raw is not None and H.parse_slot_state_of(raw, slot).state == "retired"


def scan(seams: H.Seams) -> Tuple[List[int], Dict[int, str]]:
    """``(in service, unreadable)``: the egress slots :func:`levain.slot.install_helper.slots_in_service` keeps, sorted,
    and each slot whose state record could not be read, with why. The store is opened as
    :class:`levain.slot.runtime_helper.Store` opens it (root's dirs only). Raises when the slot list cannot be read."""
    store = H.Store(seams)
    try:
        serve: List[int] = []
        unreadable: Dict[int, str] = {}
        for n in sorted(H.configured_slots(store)["egress"]):
            try:
                if not _retired(store, n):
                    serve.append(n)
            except Exception as e:   # noqa: BLE001 - one slot's record never decides another's
                unreadable[n] = f"{type(e).__name__}: {e}"
        return serve, unreadable
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
        self._missed: Dict[int, int] = {}      # served slots the last scans did not find in service, and how many

    def sync(self) -> None:
        try:
            want, unreadable = scan(self.seams)
        except Exception as e:   # noqa: BLE001 - the list unreadable: what runs keeps running, the next scan reads again
            self._note(-1, f"the slot list could not be read: {type(e).__name__}: {e}")
            return
        self._failed.pop(-1, None)
        for n, why in unreadable.items():
            self._note(n, f"its state record could not be read ({why}); left as it is")
        for n in list(self.running):
            if n in want or n in unreadable:
                self._missed.pop(n, None)
                if n in want:
                    self._failed.pop(n, None)
                continue
            self._missed[n] = self._missed.get(n, 0) + 1
            if self._missed[n] < MISSES_TO_STOP:
                continue
            del self._missed[n]
            self._failed.pop(n, None)
            self._stop(n, self.running.pop(n))
            self.log(f"slot {n}: not in service in L/slots; stopped serving it")
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
        except Exception as e:   # noqa: BLE001 - one slot that cannot be served yet never stops the others
            self._note(n, f"not served yet: {type(e).__name__}: {e}")
            return
        try:
            lst.start()
        except Exception as e:   # noqa: BLE001
            self._stop(n, lst)
            self._note(n, f"not served yet: {type(e).__name__}: {e}")
            return
        self.running[n] = lst
        self._failed.pop(n, None)
        self.log(f"slot {n}: serving {path} for {name} (uid {uid}, gid {gid})")

    def _note(self, n: int, msg: str) -> None:
        if self._failed.get(n) != msg:
            self._failed[n] = msg
            self.log(msg if n < 0 else f"slot {n}: {msg}")

    def _stop(self, n: int, lst: SlotListener) -> None:
        try:
            lst.stop()
        except Exception as e:   # noqa: BLE001 - one listener's stop never skips another's
            self.log(f"slot {n}: stopping its listener failed: {type(e).__name__}: {e}")

    def stop(self) -> None:
        for n in sorted(self.running):
            self._stop(n, self.running.pop(n))
        self._missed.clear()


def serve(seams: H.Seams, *, limits_owner: int = 0, rescan_s: float = RESCAN_S,
          stop: Optional[threading.Event] = None, log: Callable[[str], None] = _log,
          build: Callable[..., object] = QM.build_broker, listener: Callable[..., SlotListener] = SlotListener) -> int:
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
        slots = Slots(broker, seams, log=log, listener=listener)
        while not stop.is_set():
            slots.sync()
            stop.wait(rescan_s)
        log("stopping")
        return 0
    finally:
        try:
            if slots is not None:
                slots.stop()
        finally:
            try:
                for s, h in old.items():
                    signal.signal(s, h)
            finally:
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


__all__ = ["ACCOUNT", "EX_INSTANCE_HELD", "EX_WRONG_USER", "INSTANCE_LOCK", "INSTANCE_WAIT_S", "MISSES_TO_STOP",
           "RESCAN_S", "Slots", "main", "scan", "serve", "take_instance_lock"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
