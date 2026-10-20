"""The client half of ``levain-control/1`` (CONTRACT §3.1, §3.3, §11 V23): the runtime helper, the
supervisor and the reaper speak to CONTROL through it.

One :class:`ControlClient` is one unix-socket connection, and one request is in flight on it at a time
(the wire has no pipelining). The framing is :mod:`levain.slot.control`'s own (:func:`encode_frame`,
:func:`read_frame`), so the two halves cannot disagree on it.

- An answer ``{"ok":true,…}`` returns its fields without ``id`` / ``ok``.
- ``{"ok":false,"refused":<name>}`` raises :class:`levain.slot.Refused` with that name, and the connection
  stays usable, except ``locked`` (§3.3: RETRYABLE by its caller), which is retried on the same
  connection with backoff until V23 has passed since the first attempt, and only then raised.
- A connection that cannot be made, is closed, times out mid-answer or answers off the wire's shape is
  ``Refused("control-down")``, and the client closes it (a stream that lost an answer cannot be
  resynchronised). On a lease connection that close IS the lease's end (§3.1): the session it bound is
  revoked by CONTROL.

The CONNECTION is the lease (§3.1): :meth:`ControlClient.open_lease` answers the nonce and the caller
keeps this object open for the session's life; :meth:`ControlClient.fileno` lets the supervisor assert
that the descriptor never reaches a child.
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Callable

from levain.slot import Refused
from levain.slot.control import FrameMalformed, FrameTooLarge, encode_frame, read_frame

#: CONTRACT §11 V23: the window a ``locked`` refusal is retried in.
V23_SECONDS = 10.0

#: DECISION: the ``locked`` retry's backoff (§3.3 fixes only "retryable"; V23 bounds the total).
LOCKED_RETRY_FIRST_S = 0.01
LOCKED_RETRY_MAX_S = 0.5

_RESERVED = frozenset({"v", "op", "id"})


class ControlClient:
    """One connection to CONTROL at ``path``. ``timeout`` bounds the connect and each answer.

    ``retry_window_s`` (V23), ``clock`` and ``sleep`` are attributes so a test can shorten the window
    without the signature growing."""

    retry_window_s: float = V23_SECONDS
    clock: Callable[[], float] = staticmethod(time.monotonic)
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    def __init__(self, path: Path | str, *, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.lease_nonce: str | None = None
        self._mu = threading.Lock()
        self._next_id = 0
        # SOCK_CLOEXEC sets close-on-exec atomically where the OS has it (Linux). macOS has no such flag:
        # Python marks every socket non-inheritable right after socket() (PEP 446), and the supervisor's
        # fileno() assertion is the check that holds on both.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0))
        try:
            sock.settimeout(timeout)
            sock.connect(str(self.path))
        except OSError as e:
            sock.close()
            raise Refused("control-down", f"{self.path}: {e.strerror or e}") from None
        self._sock: socket.socket | None = sock

    # -- the connection --------------------------------------------------------------------------------

    def fileno(self) -> int:
        sock = self._sock
        if sock is None:
            raise Refused("control-down", "closed")
        return sock.fileno()

    def close(self) -> None:
        """Close the connection (on a lease connection: end the lease). Idempotent."""
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def __enter__(self) -> "ControlClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _down(self, detail: str) -> Refused:
        self.close()
        return Refused("control-down", detail)

    # -- one exchange ----------------------------------------------------------------------------------

    def _exchange(self, op: str, fields: dict[str, object]) -> dict:
        """Send one request and read its answer (caller holds ``_mu``). Any break = ``control-down``."""
        sock = self._sock
        if sock is None:
            raise Refused("control-down", "closed")
        self._next_id += 1
        rid = self._next_id
        try:
            frame = encode_frame({**fields, "v": 1, "op": op, "id": rid})
        except FrameTooLarge:
            raise Refused("frame-too-large", op) from None   # never sent: the connection stays good
        try:
            sock.sendall(frame)
            ans = read_frame(sock)
        except (OSError, FrameMalformed, FrameTooLarge) as e:
            raise self._down(f"{op}: {e}") from None
        if ans is None:
            raise self._down(f"{op}: EOF")
        okv = ans.get("ok")
        if okv is True and ans.get("id") == rid:
            return ans
        if okv is False and type(ans.get("refused")) is str and ans.get("id") in (rid, None):
            # An over-cap frame is refused with id null (its id was never read), then the server closes.
            if ans.get("id") is None:
                self.close()
            return ans
        raise self._down(f"{op}: an answer off the wire's shape")

    def call(self, op: str, **fields: object) -> dict:
        """One verb. Returns the answer's fields; raises ``Refused(<name>)`` on a refusal (``locked``
        only after V23 of retries) and ``Refused("control-down")`` on a broken connection."""
        bad = _RESERVED & set(fields)
        if bad:
            raise Refused("bad-request", f"{sorted(bad)} are the frame's own keys")
        with self._mu:
            deadline = self.clock() + self.retry_window_s
            delay = LOCKED_RETRY_FIRST_S
            while True:
                ans = self._exchange(op, fields)
                if ans["ok"] is True:
                    return {k: v for k, v in ans.items() if k not in ("id", "ok")}
                name = ans["refused"]
                left = deadline - self.clock()
                if name != "locked" or left <= 0:
                    raise Refused(name, op)
                self.sleep(min(delay, left))
                delay = min(delay * 2, LOCKED_RETRY_MAX_S)

    def open_lease(self) -> str:
        """``LEASE-OPEN`` (§3.1 step 1): this connection becomes the lease; returns its nonce. Keep this
        object open for as long as the session lives: its close is the lease's EOF."""
        ans = self.call("LEASE-OPEN")
        nonce = ans.get("lease_nonce")
        if type(nonce) is not str or not nonce:
            raise self._down("LEASE-OPEN answered no lease_nonce")
        self.lease_nonce = nonce
        return nonce


__all__ = ["ControlClient", "LOCKED_RETRY_FIRST_S", "LOCKED_RETRY_MAX_S", "V23_SECONDS"]
