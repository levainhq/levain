"""levain.qbroker.wire — the frame both the hook side and the CONTROL side of the broker speak.

CONTRACT c3 §3.3's framing, the same rules: a 4-byte big-endian length, at most :data:`MAX_FRAME` bytes, then the
canonical JSON of ``{"v": 1, "op": ..., "id": <int>, ...}``; one answer per request, ``{"id", "ok": true, ...}`` or
``{"id", "ok": false, "refused": "<name>"}``. A body that is not exactly its own canonical encoding is refused, so
two encodings of one message never both pass. Stdlib only.
"""
from __future__ import annotations

import socket
import struct
from typing import Any

from levain.contracts.canon import canonical_bytes, parse
from levain.contracts import ContractError

__all__ = ["MAX_FRAME", "VERSION", "FrameError", "answer", "encode", "read_frame", "refusal", "write_frame"]

MAX_FRAME = 64 * 1024
VERSION = 1
_LEN = struct.Struct(">I")


class FrameError(Exception):
    """A frame that cannot be read as one; ``name`` is the refusal name the peer is answered with."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def encode(message: dict[str, Any]) -> bytes:
    body = canonical_bytes(message)
    if len(body) > MAX_FRAME:
        raise FrameError("frame-too-large")
    return _LEN.pack(len(body)) + body


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """``n`` bytes, or ``None`` at a clean EOF before the first byte; a short read after it is a FrameError."""
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            if got == 0:
                return None
            raise FrameError("frame-truncated")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket) -> dict[str, Any] | None:
    """The next message, or ``None`` at EOF. The length is checked before a byte of the body is read."""
    head = _recv_exact(sock, _LEN.size)
    if head is None:
        return None
    (n,) = _LEN.unpack(head)
    if n > MAX_FRAME:
        raise FrameError("frame-too-large")
    body = _recv_exact(sock, n) if n else b""
    if body is None:
        raise FrameError("frame-truncated")
    try:
        message = parse(body)
    except ContractError:
        raise FrameError("frame-malformed") from None
    if not isinstance(message, dict) or canonical_bytes(message) != body:
        raise FrameError("frame-malformed")
    return message


def write_frame(sock: socket.socket, message: dict[str, Any]) -> None:
    sock.sendall(encode(message))


def answer(frame_id: Any, **fields: Any) -> dict[str, Any]:
    return {"id": frame_id, "ok": True, **fields}


def refusal(frame_id: Any, name: str) -> dict[str, Any]:
    return {"id": frame_id, "ok": False, "refused": name}
