"""levain.qbroker.server — one listener per configured slot, the peer's uid checked before a byte is read.

``R/qb/slot<n>/q.sock`` (CONTRACT c3 §2.2): owner ``_levain_qbroker``, group ``_levain_s<n>``, directory 0750 and
socket 0660; the peer's uid must be slot n's. After ``bind`` the PATH is chmodded 0660, and :meth:`SlotListener.start`
refuses to serve (unlinking it) unless the socket's group is the slot's. A connection whose peer uid (Linux
``SO_PEERCRED``, macOS ``LOCAL_PEERCRED``) is not slot n's is closed unread and unanswered. Connections are bounded
per listener (:data:`MAX_CONNECTIONS`): one past the bound is closed at once, never queued behind a lock. Each
frame must arrive whole within :data:`IDLE_TIMEOUT` of the listener starting to read it. :meth:`SlotListener.stop`
closes the connections already accepted too, and no frame read after it began is handed to the broker. Stdlib only.
"""
from __future__ import annotations

import errno
import logging
import os
import socket
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

from levain.qbroker import wire
from levain.qbroker.broker import Broker
from levain.qbroker.peercred import peer_uid

__all__ = ["IDLE_TIMEOUT", "MAX_CONNECTIONS", "SlotListener", "peer_uid", "socket_path"]

log = logging.getLogger("levain.qbroker")

# one slot runs one session; its hook process files and polls on at most a few connections at once
MAX_CONNECTIONS = 16
# a frame not whole this long after the read began is dropped (an idle connection too); a hook reconnects to ask
IDLE_TIMEOUT = 60.0
# after an accept() error that is not the listener's own close (EMFILE, ECONNABORTED), the pause before the next
ACCEPT_BACKOFF = 0.1


def socket_path(run_dir: Path | str, slot: int) -> Path:
    return Path(run_dir) / "qb" / f"slot{slot}" / "q.sock"


class SlotListener:
    def __init__(self, broker: Broker, slot: int, path: Path | str, expected_uid: int, *, expected_gid: int,
                 peer_uid: Callable[[socket.socket], int] = peer_uid, max_connections: int = MAX_CONNECTIONS,
                 idle_timeout: float = IDLE_TIMEOUT) -> None:
        self.broker = broker
        self.slot = slot
        self.path = Path(path)
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self._peer_uid = peer_uid
        self._slots = threading.BoundedSemaphore(max_connections)
        self._idle = idle_timeout
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._conns: set[socket.socket] = set()       # accepted and not yet closed; stop() closes them
        self._conns_lock = threading.Lock()
        self._stopped = False
        self.refused_peers = 0

    def start(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(st.st_mode):
                raise FileExistsError(f"{self.path} exists and is not a socket")
            os.unlink(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            sock.bind(str(self.path))
            bound = True
            os.chmod(self.path, 0o660)
            st = os.lstat(self.path)
            if not stat.S_ISSOCK(st.st_mode) or st.st_gid != self.expected_gid:
                raise PermissionError(f"{self.path} is in group {st.st_gid}, not slot {self.slot}'s "
                                      f"{self.expected_gid}: refusing to serve")
            sock.listen(MAX_CONNECTIONS)
        except BaseException:
            sock.close()
            if bound:
                self._unlink()
            raise
        self._sock = sock
        thread = threading.Thread(target=self._accept_loop, name=f"qbroker-slot{self.slot}", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._sock = None
            sock.close()
            self._unlink()
            raise
        self._thread = thread

    def _unlink(self) -> None:
        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except FileNotFoundError:
            pass

    def stop(self) -> None:
        with self._conns_lock:
            self._stopped = True
            conns, self._conns = list(self._conns), set()
        for conn in conns:          # wakes its _serve thread, whose own close follows
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            self._unlink()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _accept_loop(self) -> None:
        while True:
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except OSError as exc:
                if self._sock is None or exc.errno == errno.EBADF:     # stopped, or the socket is gone
                    return
                log.warning("qbroker: slot %d accept failed: %s", self.slot, exc)
                time.sleep(ACCEPT_BACKOFF)
                continue
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            with self._conns_lock:
                if self._stopped:
                    self._slots.release()
                    conn.close()
                    return
                self._conns.add(conn)
            try:
                threading.Thread(target=self._serve, args=(conn,), name=f"qbroker-slot{self.slot}-conn",
                                 daemon=True).start()
            except BaseException:
                self._forget(conn)
                self._slots.release()
                conn.close()

    def _serve(self, conn: socket.socket) -> None:
        try:
            try:
                uid = self._peer_uid(conn)
            except OSError:
                uid = None
            if uid != self.expected_uid:
                self.refused_peers += 1
                log.warning("qbroker: slot %d refused a peer of uid %s", self.slot, uid)
                return
            while True:
                try:
                    message = wire.read_frame(conn, timeout=self._idle)
                except wire.FrameError as exc:
                    conn.settimeout(self._idle)
                    wire.write_frame(conn, wire.refusal(None, exc.name))
                    return
                if message is None:
                    return
                if self._stopped:
                    return
                conn.settimeout(self._idle)
                wire.write_frame(conn, self.broker.handle(self.slot, message))
        except OSError:
            return
        finally:
            self._forget(conn)
            conn.close()
            self._slots.release()

    def _forget(self, conn: socket.socket) -> None:
        with self._conns_lock:
            self._conns.discard(conn)
