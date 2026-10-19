"""levain.qbroker.server — one listener per configured slot, the peer's uid checked before a byte is read.

``R/qb/slot<n>/q.sock`` (CONTRACT c3 §2.2): owner ``_levain_qbroker``, group ``_levain_s<n>``, directory 0750 and
socket 0660; the peer's uid must be slot n's. After ``bind`` the PATH is chmodded 0660, and :meth:`SlotListener.start`
refuses to serve (unlinking it) unless the socket's group is the slot's. A connection whose peer uid (Linux
``SO_PEERCRED``, macOS ``LOCAL_PEERCRED``) is not slot n's is closed unread and unanswered. Connections are bounded
per listener (:data:`MAX_CONNECTIONS`): one past the bound is closed at once, never queued behind a lock. Each
frame must arrive whole within :data:`IDLE_TIMEOUT` of the listener starting to read it. Stdlib only.
"""
from __future__ import annotations

import errno
import logging
import os
import socket
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from levain.qbroker import wire
from levain.qbroker.broker import Broker

__all__ = ["IDLE_TIMEOUT", "MAX_CONNECTIONS", "SlotListener", "peer_uid", "socket_path"]

log = logging.getLogger("levain.qbroker")

# one slot runs one session; its hook process files and polls on at most a few connections at once
MAX_CONNECTIONS = 16
# a frame not whole this long after the read began is dropped (an idle connection too); a hook reconnects to ask
IDLE_TIMEOUT = 60.0
# after an accept() error that is not the listener's own close (EMFILE, ECONNABORTED), the pause before the next
ACCEPT_BACKOFF = 0.1
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
        self._thread = threading.Thread(target=self._accept_loop, name=f"qbroker-slot{self.slot}", daemon=True)
        self._thread.start()

    def _unlink(self) -> None:
        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except FileNotFoundError:
            pass

    def stop(self) -> None:
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
            while True:
                try:
                    message = wire.read_frame(conn, timeout=self._idle)
                except wire.FrameError as exc:
                    conn.settimeout(self._idle)
                    wire.write_frame(conn, wire.refusal(None, exc.name))
                    return
                if message is None:
                    return
                conn.settimeout(self._idle)
                wire.write_frame(conn, self.broker.handle(self.slot, message))
        except OSError:
            return
        finally:
            conn.close()
            self._slots.release()
