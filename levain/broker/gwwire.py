"""`levain-gwwire/1`, the front <-> keys wire (CONTRACT §4.2): AF_UNIX stream, one exchange per connection plus the
front's one long-lived STATUS connection. A frame is a 1-byte type, a 4-byte big-endian length and the payload.

The JSON payloads (`H Q R U F A`) are decoded strictly: UTF-8 first (never `json.loads` on bytes, which guesses
UTF-16/32), `NaN` / `Infinity` refused, a duplicate key refused, and the object must hold EXACTLY its type's keys
with values of the right type. Anything else, an over-cap length, or a type the connection's state does not allow
next, is a `WireError`: the reader closes the connection with no frame.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Iterable, Optional, Union

from levain.broker.norm import is_name

#: V10's per-type caps. The empty frames carry nothing.
CAPS = {b"H": 4096, b"A": 4096, b"F": 4096, b"Q": 65536, b"R": 65536, b"U": 65536,
        b"B": 262144, b"b": 262144, b"D": 262144, b"K": 0, b"E": 0, b"X": 0, b"e": 0}
RAW = frozenset({b"B", b"b", b"D"})
EMPTY = frozenset({b"K", b"E", b"X", b"e"})
#: the largest body chunk a sender puts in one `B` / `b` / `D`.
CHUNK = 262144
_HDR = 5


class WireError(Exception):
    """A frame the wire does not allow here: the connection is closed with no frame."""


def encode(ftype: bytes, payload: Union[bytes, dict, None] = None) -> bytes:
    if ftype not in CAPS:
        raise WireError(f"type {ftype!r}")
    if ftype == b"A" and payload is None:
        payload = {}   # `A` carries `{}`; a bare `encode(b"A")` must not emit `null`, which decode refuses
    if ftype in EMPTY:
        data = b""
        if payload:
            raise WireError("empty frame with a payload")
    elif ftype in RAW:
        data = bytes(payload or b"")
    else:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(data) > CAPS[ftype]:
        raise WireError(f"{ftype!r} over its cap")
    return ftype + len(data).to_bytes(4, "big") + data


# --- payload schemas ----------------------------------------------------------------------------------------------

def _pairs_hook(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise WireError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name: str) -> None:
    raise WireError(f"constant {name}")


def _json(data: bytes) -> dict:
    try:
        text = data.decode("utf-8")
        doc = json.loads(text, object_pairs_hook=_pairs_hook, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:   # RecursionError: a deeply nested payload
        raise WireError(f"payload: {type(e).__name__}") from None
    if not isinstance(doc, dict):
        raise WireError("payload is not an object")
    return doc


_HNAME = re.compile(r"[a-z0-9!#$%&'*+.^_`|~-]+\Z")
_HVALUE = re.compile(r"[\t\x20-\x7e]*\Z")
_BODY = re.compile(r"(none|chunked|length:(0|[1-9][0-9]{0,15}))\Z")


def _str(v: object) -> bool:
    return isinstance(v, str)


def _uint(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _headers(v: object) -> bool:
    return isinstance(v, list) and all(
        isinstance(p, list) and len(p) == 2 and _str(p[0]) and _str(p[1]) and _HNAME.match(p[0])
        and _HVALUE.match(p[1]) for p in v)


_SCHEMAS = {
    b"H": {"v": lambda v: v == 1 and _uint(v), "session": lambda v: _str(v) and bool(v), "incarnation": _uint},
    b"A": {},
    b"F": {"refused": lambda v: _str(v) and is_name(v)},
    b"Q": {"v": lambda v: v == 1 and _uint(v), "token": _str, "route": _str, "method": _str, "path": _str,
           "query": _str, "headers": _headers, "body": lambda v: _str(v) and bool(_BODY.match(v)),
           "upgrade": lambda v: v in ("none", "websocket")},
    b"R": {"v": lambda v: v == 1 and _uint(v), "status": lambda v: _uint(v) and 200 <= v <= 599,
           "headers": _headers},
    b"U": {"v": lambda v: v == 1 and _uint(v), "headers": _headers},
}


def decode(ftype: bytes, data: bytes) -> Union[dict, bytes, None]:
    """The payload of a whole frame: a dict for the JSON types, bytes for `B b D`, None for the empty types."""
    if ftype not in CAPS:
        raise WireError(f"unknown type {ftype!r}")
    if len(data) > CAPS[ftype]:
        raise WireError(f"{ftype!r} over its cap")
    if ftype in EMPTY:
        if data:
            raise WireError(f"{ftype!r} carries a payload")
        return None
    if ftype in RAW:
        if not data:
            raise WireError(f"empty {ftype!r}")
        return data
    doc = _json(data)
    schema = _SCHEMAS[ftype]
    if set(doc) != set(schema):
        raise WireError(f"{ftype!r} keys")
    for k, ok in schema.items():
        if not ok(doc[k]):
            raise WireError(f"{ftype!r}.{k}")
    return doc


# --- reading ------------------------------------------------------------------------------------------------------

class HoldingReader(asyncio.StreamReader):
    """A `StreamReader` that never lets a transport error overtake bytes already received. A plain `StreamReader`
    raises its stored exception before it returns what it buffered, so a complete frame that arrived just before a
    reset is lost. Here the error is held as `terminal` and the stream is ended instead: `read()` returns every
    buffered byte, then EOF, and `FrameReader` raises `terminal` only once no complete frame is left.

    When `sock` is set (the connection's socket), the bytes the kernel still holds for it at the error are read into
    the buffer first, at most `SALVAGE_CAP`: an error on a WRITE can abort the transport before its reader saw what the
    peer sent last (keys' `F` followed by its close, then the front's next `B` write failing)."""

    SALVAGE_CAP = 2 * CHUNK

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.terminal: Optional[BaseException] = None
        self.sock: Optional[object] = None   # anything with fileno(); -1 once closed
        self._ended = False

    def feed_eof(self) -> None:
        self._ended = True
        super().feed_eof()

    def set_exception(self, exc: BaseException) -> None:
        if self.terminal is not None or self._ended:   # a clean EOF already seen stays a clean EOF
            return
        got = 0
        while self.sock is not None and not self._ended and got < self.SALVAGE_CAP:
            try:
                data = os.read(self.sock.fileno(), min(65536, self.SALVAGE_CAP - got))  # type: ignore[attr-defined]
            except OSError:   # nothing more (EAGAIN), the reset itself, or a closed socket (fileno -1)
                break
            if not data:
                break
            self.feed_data(data)
            got += len(data)
        self.terminal = exc
        if not self._ended:
            self.feed_eof()

    def exception(self) -> Optional[BaseException]:
        # what `StreamWriter.drain()` consults: a writer on this connection still sees the error at once
        return self.terminal


class FrameReader:
    """Frames off a `StreamReader`, decoded from this object's OWN buffer: every complete frame held is returned
    before a terminal error is raised. Over a `HoldingReader` that includes the frames that arrived just before the
    transport failed; over a plain `StreamReader`, only those already moved into this buffer."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._buf = bytearray()

    def _take(self) -> Optional[tuple[bytes, bytes]]:
        if len(self._buf) < _HDR:
            return None
        ftype = bytes(self._buf[:1])
        n = int.from_bytes(self._buf[1:_HDR], "big")
        if ftype not in CAPS:
            raise WireError(f"unknown type {ftype!r}")
        if n > CAPS[ftype]:
            raise WireError(f"{ftype!r} over its cap")
        if len(self._buf) < _HDR + n:
            return None
        data = bytes(self._buf[_HDR:_HDR + n])
        del self._buf[:_HDR + n]
        return ftype, data

    async def read(self, allowed: Iterable[bytes]) -> Optional[tuple[bytes, Union[dict, bytes, None]]]:
        """The next frame as (type, payload), or None at a clean EOF between frames. EOF mid-frame raises
        `asyncio.IncompleteReadError`; a frame not in `allowed` raises `WireError`."""
        while True:
            got = self._take()
            if got is not None:
                ftype, data = got
                if ftype not in allowed:
                    raise WireError(f"{ftype!r} not allowed here")
                return ftype, decode(ftype, data)
            chunk = await self._reader.read(65536)
            if not chunk:
                terminal = getattr(self._reader, "terminal", None)
                if terminal is not None:
                    raise terminal
                if self._buf:
                    raise asyncio.IncompleteReadError(bytes(self._buf), None)
                return None
            self._buf += chunk


async def read_frame(reader: FrameReader, allowed_types: Iterable[bytes]):
    return await reader.read(allowed_types)


# --- the per-connection state machines ----------------------------------------------------------------------------

class FrontSide:
    """What keys may send the front next. STATUS: `A` once, then `K` (or EOF). Exchange: `A` | `F`; after `A`:
    `R` | `F` | `U`; after `R`: `b`* `e` (only `e` for a response with no body); after `U`: `D`* `E`."""

    def __init__(self, status: bool) -> None:
        self.state = "hello" if status else "asked"

    _ALLOWED = {"hello": frozenset({b"A"}), "live": frozenset({b"K"}), "asked": frozenset({b"A", b"F"}),
                "accepted": frozenset({b"R", b"F", b"U"}), "body": frozenset({b"b", b"e"}),
                "nobody": frozenset({b"e"}), "upgraded": frozenset({b"D", b"E"}), "done": frozenset()}

    def allowed(self) -> frozenset:
        return self._ALLOWED[self.state]

    def got(self, ftype: bytes, *, nobody: bool = False) -> None:
        if ftype not in self.allowed():
            raise WireError(f"{ftype!r} in state {self.state}")
        s = self.state
        if s == "hello":
            self.state = "live"
        elif s == "live" or ftype in (b"F", b"e", b"E"):
            self.state = "done"
        elif s in ("asked", "accepted") and ftype == b"A":
            self.state = "accepted"
        elif ftype == b"R":
            self.state = "nobody" if nobody else "body"
        elif ftype == b"U":
            self.state = "upgraded"


class KeysSide:
    """What the front may send keys next: the first frame is `H` (STATUS, after which nothing is valid) or `Q`
    (exchange); then `B`* (valid before keys' `A` too) and `E`, or `X` at any point; after keys' `U`: `D`* `E`."""

    def __init__(self) -> None:
        self.state = "first"

    _ALLOWED = {"first": frozenset({b"H", b"Q"}), "status": frozenset(), "request": frozenset({b"B", b"E", b"X"}),
                "ended": frozenset({b"X"}), "upgraded": frozenset({b"D", b"E", b"X"}), "done": frozenset()}

    def allowed(self) -> frozenset:
        return self._ALLOWED[self.state]

    def got(self, ftype: bytes) -> None:
        if ftype not in self.allowed():
            raise WireError(f"{ftype!r} in state {self.state}")
        if ftype == b"H":
            self.state = "status"
        elif ftype == b"Q":
            self.state = "request"
        elif ftype == b"E":
            self.state = "ended" if self.state == "request" else "done"
        elif ftype == b"X":
            self.state = "done"

    def upgraded(self) -> None:
        self.state = "upgraded"
