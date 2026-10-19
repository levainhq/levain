"""levain.qbroker.server — one listener per configured slot, the peer's uid checked before a byte is read.

``R/qb/slot<n>/q.sock`` (CONTRACT c3 §2.2): the directory is root-made 2750 in group ``_levain_s<n>``, so the socket
this process binds takes that group; the PATH is then chmodded 0660 before ``listen``. A connection whose peer uid
(Linux ``SO_PEERCRED``, macOS ``LOCAL_PEERCRED``) is not slot n's is closed unread and unanswered. Connections are
bounded per listener (:data:`MAX_CONNECTIONS`): one past the bound is closed at once, never queued behind a lock.
Stdlib only.
"""
from __future__ import annotations

import logging
import os
import socket
import stat
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from levain.qbroker import wire
from levain.qbroker.broker import Broker

__all__ = ["IDLE_TIMEOUT", "MAX_CONNECTIONS", "SlotListener", "peer_uid", "socket_path"]

log = logging.getLogger("levain.qbroker")

# one slot runs one session; its hook process files and polls on at most a few connections at once
MAX_CONNECTIONS = 16
# a connection silent this long is dropped; a hook reconnects to ask again
IDLE_TIMEOUT = 60.0
_SOL_LOCAL = 0            # macOS <sys/un.h>
_LOCAL_PEERCRED = 0x001   # macOS <sys/un.h>: struct xucred
_XUCRED = struct.Struct("@IIh16I")   # cr_version, cr_uid, cr_ngroups, cr_groups[NGROUPS]
_UCRED = struct.Struct("3i")         # Linux struct ucred: pid, uid, gid


def peer_uid(sock: socket.socket) -> int:
    """The connected peer's effective uid, from the kernel."""
    if sys.platform == "darwin":
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED.size)
        version, uid = _XUCRED.unpack(raw[:_XUCRED.size])[:2]
        if version != 0:
            raise OSError(f"xucred version {version}")
        return uid
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    return _UCRED.unpack(raw)[1]


def socket_path(run_dir: Path | str, slot: int) -> Path:
    return Path(run_dir) / "qb" / f"slot{slot}" / "q.sock"


class SlotListener:
    def __init__(self, broker: Broker, slot: int, path: Path | str, expected_uid: int, *,
                 peer_uid: Callable[[socket.socket], int] = peer_uid, max_connections: int = MAX_CONNECTIONS,
                 idle_timeout: float = IDLE_TIMEOUT) -> None:
        self.broker = broker
        self.slot = slot
        self.path = Path(path)
        self.expected_uid = expected_uid
        self._peer_uid = peer_uid
        self._slots = threading.BoundedSemaphore(max_connections)
        self._idle = idle_timeout
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
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
        try:
            sock.bind(str(self.path))
            os.chmod(self.path, 0o660)
            sock.listen(MAX_CONNECTIONS)
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name=f"qbroker-slot{self.slot}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _accept_loop(self) -> None:
        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            try:
                threading.Thread(target=self._serve, args=(conn,), name=f"qbroker-slot{self.slot}-conn",
                                 daemon=True).start()
            except BaseException:
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
            conn.settimeout(self._idle)
            while True:
                try:
                    message = wire.read_frame(conn)
                except wire.FrameError as exc:
                    wire.write_frame(conn, wire.refusal(None, exc.name))
                    return
                if message is None:
                    return
                wire.write_frame(conn, self.broker.handle(self.slot, message))
        except OSError:
            return
        finally:
            conn.close()
            self._slots.release()
