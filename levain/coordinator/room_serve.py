"""levain.coordinator.room_serve: what ``levain serve`` holds for the room (RM-G, DESIGN_ROOM §8 "RM-G wiring").

One :class:`RoomServe` per serve process. It builds the :class:`~levain.coordinator.chat.ChatService` (with the room's
probe and tripwire context), the :class:`~levain.coordinator.room_launch.RoomLauncher` over it, and the pump, and it
hands a route one narrow seam: :meth:`RoomServe.send`, :meth:`RoomServe.view`, :meth:`RoomServe.rotate`. It decides
nothing about what a line may do (the service's) or what the child may do (the launcher's); it only orders them.

**The order it keeps.** :meth:`start` runs ``ChatService.recover()`` and only then starts the pump, and every entry
that can send or open refuses with :class:`RoomServeError` before :meth:`start` has returned. Nothing is opened at
start: the room's child is spawned on the first need (:meth:`ensure_room`, which a send makes), so a serve nobody
types into holds no child. A restarted serve finds the dead process's room launch and the launcher ends it and resumes
the room thread's newest conversation (the launcher's own rule).

**The pump** reads the open room launch's vendor stream (``ChatService.read_next``) and recycles an idle child
(``RoomLauncher.recycle_idle``). A step that raises is recorded in :attr:`RoomServe.last_error` and the loop goes on;
the pump never opens a room (a quiet serve stays quiet).

**Stale surfaces** (§3.6): a surface names the room THREAD it drew. :meth:`send` passes it to the service, which
refuses a rotated one (:class:`~levain.coordinator.chat.RoomRotated`); a surface that names none sends to the open room.
Each send is its own outbox row, so two tabs never hold anything between requests.

**The room's thread** is made with :meth:`~levain.coordinator.store.CoordStore.bare_thread`, never by a drop: a drop
would wake the coordinator in the same transaction and would sit in the drop list. A new room is opened, and a rotation
done, in one transaction with its thread.

**One lock orders the seam.** :meth:`send` holds it across the service's send, so a stop or a rotate waits for a line
in flight and a line naming no room lands on the room that was open
when it began. The pump takes it to take its handle and to recycle (so an idle recycle never lands between a send's
open and its recorded line), and never across a vendor read. The lock order is RoomServe, then the launcher, then the
service, on every path. A rotation ends the old launch under the lock and the held handle is None until the next send,
by design: the old child's late output is not read. :meth:`stop` waits behind a send in flight, by design, and says
so to its caller (``on_wait``). A page poll reads :meth:`status`, which takes no lock.

NOT HERE: the HTTP routes and the page's token (the cockpit's room routes, RM-F, are the callers). A route calls the
three methods above and maps the exceptions; ``levain serve`` builds one :class:`RoomServe` and calls :meth:`start`
and :meth:`stop`.

Stdlib plus levain. The launcher is a seam (``launcher``) so a test can supply one of its own."""
from __future__ import annotations

import threading
from typing import Any, Callable, Mapping, Optional

from levain.coordinator import room
from levain.coordinator.chat import ChatService, ChatStore, RoomRotated, SendReceipt, _message, _open_room
from levain.coordinator.room_launch import (RoomHandle, RoomLauncher, RoomSetting, make_probe, make_tripwires)
from levain.coordinator.store import CoordError

__all__ = ["PUMP_INTERVAL", "RoomServe", "RoomServeError"]

# [seat, UNMEASURED] how long the pump waits between steps when a step found nothing
PUMP_INTERVAL = 0.25
# [seat] how long :meth:`RoomServe.stop` waits for the pump thread
_JOIN_WAIT = 10.0
# [seat] how long :meth:`RoomServe.stop` waits for a send in flight before it says it is waiting
_STOP_NOTE_AFTER = 1.0
_TITLE = "the room"


class RoomServeError(CoordError):
    """The room was asked for out of order (before start, or after stop); nothing was done."""


class RoomServe:
    """See the module docstring. ``store`` is the coordinator store (an ``EnvelopeStore``); ``setting`` is what the room
    runs as; ``run_dir`` is where each launch's private directory is made. ``launcher(store, service)`` builds the
    launcher (default: a :class:`RoomLauncher` over ``setting``); ``launcher_kw`` is passed to that default
    (``spawn``, ``environ``, ``home``, ``idle_seconds``...)."""

    def __init__(self, store: Any, setting: RoomSetting, *, run_dir: str, pump_interval: float = PUMP_INTERVAL,
                 launcher: Optional[Callable[[ChatStore, ChatService], Any]] = None,
                 launcher_kw: Optional[Mapping[str, Any]] = None,
                 service_kw: Optional[Mapping[str, Any]] = None) -> None:
        self._store = store
        self._setting = setting
        self.chat_store = ChatStore(store)
        skw: dict[str, Any] = {"probe": make_probe(self.chat_store, door_names=setting.door_names),
               "tripwires": make_tripwires(self.chat_store, door_names=setting.door_names)}
        skw.update(service_kw or {})                 # a test's own probe replaces the room's
        self.service = ChatService(self.chat_store, **skw)
        if launcher is None:
            kw = dict(launcher_kw or {})
            self.launcher = RoomLauncher(self.chat_store, self.service, setting, run_dir=run_dir, **kw)
        else:
            self.launcher = launcher(self.chat_store, self.service)
        self._interval = pump_interval
        self._lock = threading.RLock()
        self._started = False
        self._recovered = False
        self._stopped = False
        self._handle: Optional[RoomHandle] = None
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.recovered: dict[str, int] = {}
        self.last_error: Optional[str] = None

    @property
    def store(self) -> Any:
        """The coordinator store the room's tables live in (the cockpit's drop and proposal routes write through it)."""
        return self._store

    @property
    def directory(self) -> Any:
        """The directory of that store's ``dispatch.db``: the one the cockpit's room reads must be pointed at."""
        return self._store.path.parent

    # --- lifecycle -------------------------------------------------------------------------------------------------
    def start(self) -> dict[str, int]:
        """``recover()``, then the pump. Idempotent: a second call changes nothing and returns the first's counts. A
        stopped RoomServe is not restarted (build a new one)."""
        with self._lock:
            if self._stopped:
                raise RoomServeError("the room was stopped; build a new one")
            if self._started:
                return self.recovered
            if not self._recovered:
                self.chat_store._connect().close()             # the chat tables exist before recover reads them
                self.recovered = self.service.recover()
                self._recovered = True
            thread = threading.Thread(target=self._run, name="levain-room-pump", daemon=True)
            thread.start()                                     # a start that raises leaves the serve not started
            self._thread = thread
            self._started = True
            return self.recovered

    def stop(self, on_wait: Optional[Callable[[], None]] = None) -> None:
        """Stop the pump, end the room's child, close the service. Idempotent; never raises (``on_wait`` aside).

        The pump is told to stop first, with no lock taken. A send in flight holds the serve's lock (across the
        launcher's open, bounded by the launcher's open deadline), and this waits for it: by design, a line is never
        cut off mid-send. When the lock is not free within :data:`_STOP_NOTE_AFTER` seconds, ``on_wait()`` is called
        once (``levain serve`` prints that it is waiting) and the wait goes on."""
        self._stop_evt.set()                     # before _stopped: a step in between is a clean stop, not an error
        if not self._lock.acquire(timeout=_STOP_NOTE_AFTER):
            if on_wait is not None:
                on_wait()
            self._lock.acquire()
        try:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
            self._handle = None
        finally:
            self._lock.release()
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_WAIT)
            if thread.is_alive():
                self.last_error = (f"stop: the pump did not stop within {_JOIN_WAIT:g}s; it may still run against a "
                                   "closed launcher")
        for close in (self.launcher.close, self.service.close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - a stop that cannot finish one step still does the next
                self.last_error = f"stop: {type(exc).__name__}: {str(exc)[:200]}"

    def _ready(self) -> None:
        if self._stopped:
            raise RoomServeError("the room was stopped; nothing was done")
        if not self._started:
            raise RoomServeError("the room was not started (recover has not run); nothing was done")

    # --- the pump --------------------------------------------------------------------------------------------------
    def pump_once(self) -> int:
        """One pump step (the loop's body, callable by a test): recycle an idle child, then read the held launch. The
        count of new events. Raises what the step raised; the loop records it. A held launch whose child exited is
        ended and dropped, and :attr:`last_error` says so; the next send resumes it."""
        with self._lock:
            # the recycle is under the lock a send holds across its open and its recorded line: an idle child is not
            # ended between a send's ensure() and the line landing
            self._ready()
            handle = self._handle
            ended = self.launcher.recycle_idle()
            if handle is not None and handle.launch_id in ended:
                self._handle = None
                return 0
            if handle is None:
                return 0
        if not self.launcher.child_alive(handle.launch_id):
            with self._lock:
                if self._handle is handle:
                    try:
                        self.launcher.end(handle.launch_id, "room child exited")
                    finally:
                        self._handle = None
                    self.last_error = (f"the room's child exited (launch {handle.launch_id}); the next send "
                                       "resumes the conversation")
            return 0
        try:
            got = self.service.read_next(handle.launch_id)
        except Exception:
            if self._handle is not handle:       # a send or rotate replaced the handle mid-step: not this step's fault
                return 0
            raise
        if got:
            self.last_error = None
        return got

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            got = 0
            try:
                got = self.pump_once()
            except RoomServeError:
                if not self._stop_evt.is_set():
                    self.last_error = "pump: the room was asked for out of order"
            except Exception as exc:  # noqa: BLE001 - the pump outlives one bad step; the cause is on last_error
                self.last_error = f"pump: {type(exc).__name__}: {str(exc)[:200]}"
            if not got:
                self._stop_evt.wait(self._interval)

    # --- the seam a route calls ------------------------------------------------------------------------------------
    def ensure_room(self) -> RoomHandle:
        """The open room's running launch: a room is opened on the first need (a new thread, the store's own way) and
        its child opened or reopened (resuming) by the launcher. Raises :class:`RoomServeError` before start; the
        launcher's :class:`~levain.coordinator.room_launch.RoomLaunchError` when the open check fails. A good open
        clears :attr:`last_error`."""
        with self._lock:
            self._ready()
            if self.chat_store._read(_open_room) is None:
                self._open_new_room()
            self._handle = self.launcher.ensure()
            self.last_error = None
            return self._handle

    def send(self, *, author_surface: str, client_id: str, outbox_id: str, body: str,
             room_thread: Optional[str] = None, attachments: tuple = ()) -> SendReceipt:
        """Your line to the room. ``room_thread`` is the thread the surface drew (None: the open room); a rotated one
        is refused ``RoomRotated`` by the service with nothing written. A stored ``outbox_id`` is answered from its
        record before any room is resolved (it names the thread it was sent to unless the surface names one), so a
        resend after a restart or a rotation opens nothing. The lock is held through the service's send."""
        with self._lock:
            self._ready()
            send = dict(author_surface=author_surface, client_id=client_id, outbox_id=outbox_id, body=body,
                        attachments=attachments)
            stored = self.chat_store._read(lambda conn: _message(conn, "outbox_id", outbox_id)) \
                if isinstance(outbox_id, str) else None
            if stored is not None:
                return self.service.send_room(room_thread if room_thread is not None else stored.thread_id, **send)
            if room_thread is not None and self.chat_store._read(_open_room) != room_thread:
                # a surface that drew a room that is not the open one: the service refuses it, and a refusal opens
                # nothing (no child is spawned for a line that will not be sent)
                return self.service.send_room(room_thread, **send)
            handle = self.ensure_room()
            return self.service.send_room(room_thread if room_thread is not None else handle.room_thread, **send)

    def view(self) -> dict[str, Any]:
        """What a route hands the page, with no side effect (it opens nothing): the open room thread (None when no room
        is open), the launch the pump holds, and the last pump or stop error."""
        with self._lock:                         # one snapshot: never a new room paired with the old launch
            self._ready()
            thread = self.chat_store._read(_open_room)
            held = self._handle
            return {"room_thread": thread, "launch_id": None if held is None else held.launch_id,
                    "last_error": self.last_error}

    def status(self) -> dict[str, Any]:
        """What a page POLL may read: the last pump, open or stop error, and whether the serve runs. It takes NO lock
        and reads no store (roomfix1 R1: the poll called :meth:`view`, which waits for a send holding the lock across
        the launcher's open, so every poll stalled and held a request slot until it ended). One attribute read each,
        so it never blocks; it never raises."""
        host = getattr(self._setting, "host", None)
        return {"last_error": self.last_error, "running": self._started and not self._stopped,
                "drops_via": "desk" if host is not None and host.relays else "card"}

    def interrupt(self, launch_id: str) -> Any:
        """His Stop: the service's unfenced interrupt of ``launch_id`` (the room's newest launch, which the route reads
        from the record). It does NOT take this serve's lock, so a stop is never queued behind a send in flight; the
        service orders it against its own reads."""
        self._ready()
        return self.service.interrupt(launch_id, None)

    def rotate(self, expected_room: Optional[str] = None) -> str:
        """His "new conversation": a new room thread opened, the old one rotated, in one transaction. With no room
        open it raises (:class:`RoomServeError`) before writing anything; the first send opens a room. ``expected_room``
        is the room thread the surface drew: checked INSIDE that transaction, under the lock, so two taps on one stale
        page rotate once (the second is :class:`~levain.coordinator.chat.RoomRotated`, nothing written). The next send
        opens the new room's child (the launcher ends the old one). Returns the new room thread."""
        with self._lock:
            self._ready()
            with self._store._write() as (conn, now):
                current = room.current_room(conn)
                if current is None:
                    raise RoomServeError("no room is open to rotate; nothing was done")
                if expected_room is not None and current != expected_room:
                    raise RoomRotated("the room changed since this page drew it; nothing was rotated")
                new = self._store.bare_thread(conn, now, _TITLE)
                room.rotate(conn, new, now=now)
            old = self._handle
            try:
                if old is not None:              # ended under the lock: the pump reads nothing from a rotated launch
                    self.launcher.end(old.launch_id, "room rotated")
            except Exception as exc:  # noqa: BLE001 - the rotation is committed; the pump's recycle ends a leftover
                self.last_error = f"rotate: the old launch did not end: {type(exc).__name__}: {str(exc)[:200]}"
            finally:
                self._handle = None
            return new

    # --- the thread -----------------------------------------------------------------------------------------------
    def _open_new_room(self) -> str:
        with self._store._write() as (conn, now):
            thread = self._store.bare_thread(conn, now, _TITLE)
            room.open_room_thread(conn, thread, now=now)
        return thread
