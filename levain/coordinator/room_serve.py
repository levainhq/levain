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

NOT HERE: the HTTP routes and the page's token (``levain/cockpit/room.py``, ``levain/web_server.py``, RM-F). A route
calls the three methods above and maps the exceptions; ``levain serve`` builds one :class:`RoomServe` and calls
:meth:`start` and :meth:`stop`.

Stdlib plus levain. The launcher is a seam (``launcher``) so a test can supply one of its own."""
from __future__ import annotations

import threading
from typing import Any, Callable, Mapping, Optional

from levain.coordinator import room
from levain.coordinator.chat import ChatService, ChatStore, SendReceipt, _open_room
from levain.coordinator.room_launch import (RoomHandle, RoomLauncher, RoomSetting, make_probe, make_tripwires)
from levain.coordinator.store import CoordError, Part

__all__ = ["PUMP_INTERVAL", "RoomServe", "RoomServeError"]

# [seat, UNMEASURED] how long the pump waits between steps when a step found nothing
PUMP_INTERVAL = 0.25
# [seat] how long :meth:`RoomServe.stop` waits for the pump thread
_JOIN_WAIT = 10.0


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
        self.chat_store = ChatStore(store)
        skw: dict[str, Any] = {"probe": make_probe(self.chat_store), "tripwires": make_tripwires(self.chat_store)}
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
        self._stopped = False
        self._handle: Optional[RoomHandle] = None
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.recovered: dict[str, int] = {}
        self.last_error: Optional[str] = None

    # --- lifecycle -------------------------------------------------------------------------------------------------
    def start(self) -> dict[str, int]:
        """``recover()``, then the pump. Idempotent: a second call changes nothing and returns the first's counts. A
        stopped RoomServe is not restarted (build a new one)."""
        with self._lock:
            if self._stopped:
                raise RoomServeError("the room was stopped; build a new one")
            if self._started:
                return self.recovered
            self.chat_store._connect().close()                 # the chat tables exist before recover reads them
            self.recovered = self.service.recover()
            self._started = True
            self._thread = threading.Thread(target=self._run, name="levain-room-pump", daemon=True)
            self._thread.start()
            return self.recovered

    def stop(self) -> None:
        """Stop the pump, end the room's child, close the service. Idempotent; never raises."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_evt.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_WAIT)
        for close in (self.launcher.close, self.service.close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - a stop that cannot finish one step still does the next
                self.last_error = f"stop: {type(exc).__name__}: {str(exc)[:200]}"
        self._handle = None

    def _ready(self) -> None:
        if self._stopped:
            raise RoomServeError("the room was stopped; nothing was done")
        if not self._started:
            raise RoomServeError("the room was not started (recover has not run); nothing was done")

    # --- the pump --------------------------------------------------------------------------------------------------
    def pump_once(self) -> int:
        """One pump step (the loop's body, callable by a test): recycle an idle child, then read the held launch. The
        count of new events. Raises what the step raised; the loop records it."""
        self._ready()
        handle = self._handle
        ended = self.launcher.recycle_idle()
        if handle is not None and handle.launch_id in ended:
            self._handle = handle = None
        if handle is None:
            return 0
        return self.service.read_next(handle.launch_id)

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            got = 0
            try:
                got = self.pump_once()
            except Exception as exc:  # noqa: BLE001 - the pump outlives one bad step; the cause is on last_error
                self.last_error = f"pump: {type(exc).__name__}: {str(exc)[:200]}"
            if not got:
                self._stop_evt.wait(self._interval)

    # --- the seam a route calls ------------------------------------------------------------------------------------
    def ensure_room(self) -> RoomHandle:
        """The open room's running launch: a room is opened on the first need (a new thread, the store's own way) and
        its child opened or reopened (resuming) by the launcher. Raises :class:`RoomServeError` before start; the
        launcher's :class:`~levain.coordinator.room_launch.RoomLaunchError` when the open check fails."""
        with self._lock:
            self._ready()
            if self.chat_store._read(_open_room) is None:
                self._open_new_room()
            self._handle = self.launcher.ensure()
            return self._handle

    def send(self, *, author_surface: str, client_id: str, outbox_id: str, body: str,
             room_thread: Optional[str] = None, attachments: tuple = ()) -> SendReceipt:
        """Your line to the room. ``room_thread`` is the thread the surface drew (None: the open room); a rotated one
        is refused ``RoomRotated`` by the service with nothing written. A stored ``outbox_id`` is answered from its
        record first (the service's replay), so a resend after a restart opens nothing."""
        with self._lock:
            self._ready()
            if room_thread is not None and self.chat_store._read(_open_room) != room_thread:
                # a surface that drew a room that is not the open one: the service refuses it, and a refusal opens
                # nothing (no child is spawned for a line that will not be sent)
                return self.service.send_room(room_thread, author_surface=author_surface, client_id=client_id,
                                              outbox_id=outbox_id, body=body, attachments=attachments)
            handle = self.ensure_room()
        return self.service.send_room(room_thread if room_thread is not None else handle.room_thread,
                                      author_surface=author_surface, client_id=client_id, outbox_id=outbox_id,
                                      body=body, attachments=attachments)

    def view(self) -> dict[str, Any]:
        """What a route hands the page, with no side effect (it opens nothing): the open room thread (None when no room
        is open), the launch the pump holds, and the last pump or stop error."""
        self._ready()
        thread = self.chat_store._read(_open_room)
        held = self._handle
        return {"room_thread": thread, "launch_id": None if held is None else held.launch_id,
                "started": True, "last_error": self.last_error}

    def rotate(self) -> str:
        """His "new conversation": a new room thread opened, the old one rotated. The next send opens the new room's
        child (the launcher ends the old one). Returns the new room thread."""
        with self._lock:
            self._ready()
            new = self._new_thread()
            with self._store._write() as (conn, _now):
                room.rotate(conn, new)
            self._handle = None
            return new

    # --- the thread -----------------------------------------------------------------------------------------------
    def _new_thread(self) -> str:
        d = self._store.accept_drop("the room", surface="cli")[0]
        return self._store.split(d.drop_id, [Part("note", "the room")])[0].thread_id

    def _open_new_room(self) -> str:
        thread = self._new_thread()
        with self._store._write() as (conn, _now):
            room.open_room_thread(conn, thread)
        return thread
