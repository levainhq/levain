"""``levain-walk/1``: the frame codec root's walker writes and the importer reads (CONTRACT §10.1).

One module both sides import, so the encoder and the decoder cannot disagree on a byte. A frame is a ``>I``
header length, the header as canonical JSON (``levain.contracts.canon``), then exactly ``size`` payload bytes.
Every entry header carries every key ``{"v":1,"path_b64","type","mode","size","digest"}``; the terminal header
is ``{"v":1,"end":true,"count":<frames>,"bytes":<payload total>}``. A header-only type carries ``size 0`` and
``digest ""``. Anything else is a refusal by name, ``frame-bad:<key>``.

The header cap is DERIVED from :data:`MAX_DEPTH` (:func:`header_cap`), so no tree a depth-capped END can accept
is stranded by it. :func:`end_level_offset` is where git's root tree sits in END levels, derived from the prefix
adopt's prune keeps, never a literal of its own.
"""
from __future__ import annotations

import base64
import binascii
import struct
from typing import BinaryIO, Iterator

from levain.contracts import ContractError, canon
from levain.slot import Refused

FORMAT = "levain-walk/1"

#: The Levain depth constant in END levels (a root child of the retained ``tree/`` is level 1): the format
#: ceiling, force-retire's ``--max-depth`` parse ceiling, the frame-header cap's input and step 5's comparator.
#: DECISION (HEAD_r5 Q-B): chosen equal to git's ``core.maxTreeDepth`` default from v2.43.0; it is not a
#: measured value and does not move with the host git.
MAX_DEPTH = 2048
#: The longest name one path component can have (NAME_MAX on both OSes).
NAME_MAX = 255
#: The bytes the header's fixed keys and their values can take beside ``path_b64``.
FIXED_HEADER_BYTES = 1024

#: The one subtree of the retained tree that becomes git's worktree: adopt's prune keeps it, and git's root
#: tree is its content.
PRUNE_SCOPE_PREFIX: tuple[bytes, ...] = (b"repo",)

ENTRY_KEYS = frozenset({"v", "path_b64", "type", "mode", "size", "digest"})
TERMINAL_KEYS = frozenset({"v", "end", "count", "bytes"})
#: Types whose frame carries payload bytes.
PAYLOAD_TYPES = frozenset({"file", "link"})
#: Types whose frame is a header only (``size 0``, ``digest ""``).
HEADER_ONLY_TYPES = frozenset({"dir", "fifo", "socket", "device", "skipped-git", "unread"})
TYPES = PAYLOAD_TYPES | HEADER_ONLY_TYPES

_LEN = struct.Struct(">I")
_HEX = frozenset("0123456789abcdef")
_MODE_MAX = 0o7777
_INT_MAX = (1 << 53) - 1


def header_cap(max_depth: int = MAX_DEPTH) -> int:
    """The largest header a frame may carry: the fixed keys plus base64 of the longest path a depth-capped END
    can record, ``max_depth`` components of :data:`NAME_MAX` bytes and ``max_depth - 1`` separators."""
    longest = max_depth * NAME_MAX + (max_depth - 1)
    return FIXED_HEADER_BYTES + 4 * -(-longest // 3)


def end_level_offset() -> int:
    """The END level of git's root tree: a git tree of height ``h`` puts its deepest entry at END level
    ``h + end_level_offset()``."""
    return len(PRUNE_SCOPE_PREFIX)


def b64(path: bytes) -> str:
    return base64.b64encode(path).decode("ascii")


def _bad(key: str, detail: str = "") -> Refused:
    return Refused(f"frame-bad:{key}", detail)


def _header_bytes(header: dict) -> bytes:
    data = canon.canonical_bytes(header)
    if len(data) > header_cap():
        raise _bad("length", f"{len(data)} bytes > {header_cap()}")
    return _LEN.pack(len(data)) + data


def encode_header(path: bytes, type_: str, mode: int, size: int, digest: str) -> bytes:
    """One entry header, length-prefixed, validated by the same rules the decoder applies."""
    header = {"v": 1, "path_b64": b64(path), "type": type_, "mode": mode, "size": size, "digest": digest}
    check_entry(header)
    return _header_bytes(header)


def encode_terminal(count: int, total: int) -> bytes:
    header = {"v": 1, "end": True, "count": count, "bytes": total}
    check_terminal(header)
    return _header_bytes(header)


def _int(header: dict, key: str, hi: int) -> int:
    v = header[key]
    if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > hi:
        raise _bad(key, repr(v)[:40])
    return v


def check_entry(header: dict) -> bytes:
    """Validate an entry header; return its decoded path."""
    if set(header) != ENTRY_KEYS:
        extra = sorted(set(header) ^ ENTRY_KEYS)
        raise _bad("keys", ",".join(extra)[:200])
    if header["v"] != 1 or isinstance(header["v"], bool):
        raise _bad("v")
    t = header["type"]
    if not isinstance(t, str) or t not in TYPES:
        raise _bad("type", repr(t)[:40])
    _int(header, "mode", _MODE_MAX)
    size = _int(header, "size", _INT_MAX)
    digest = header["digest"]
    if not isinstance(digest, str):
        raise _bad("digest")
    if t in HEADER_ONLY_TYPES:
        if size != 0:
            raise _bad("size", f"{t} carries no payload")
        if digest != "":
            raise _bad("digest", f"{t} carries no digest")
    elif not (digest.startswith("sha256:") and len(digest) == 71 and set(digest[7:]) <= _HEX):
        raise _bad("digest", digest[:80])
    raw = header["path_b64"]
    if not isinstance(raw, str):
        raise _bad("path_b64")
    try:
        path = base64.b64decode(raw.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise _bad("path_b64", "not base64") from None
    # One encoding per path: a header whose base64 is not the canonical form of its bytes is refused.
    if not path or b64(path) != raw:
        raise _bad("path_b64", "not canonical")
    if path.startswith(b"/") or b"\0" in path or any(c in (b"", b".", b"..") for c in path.split(b"/")):
        raise _bad("path_b64", "not a relative tree path")
    return path


def check_terminal(header: dict) -> None:
    if set(header) != TERMINAL_KEYS:
        raise _bad("keys", ",".join(sorted(set(header) ^ TERMINAL_KEYS))[:200])
    if header["v"] != 1 or isinstance(header["v"], bool):
        raise _bad("v")
    if header["end"] is not True:
        raise _bad("end")
    _int(header, "count", _INT_MAX)
    _int(header, "bytes", _INT_MAX)


def _read_exact(fh: BinaryIO, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = fh.read(n - len(out))
        if not chunk:
            break
        out += chunk
    return bytes(out)


def read_header(fh: BinaryIO) -> dict | None:
    """The next header from ``fh``, validated: an entry header (its payload follows, ``size`` bytes, for the
    caller to read) or the terminal one. ``None`` at a clean end of stream before any length byte; a stream
    that ends anywhere else is ``frame-bad:truncated``."""
    raw = _read_exact(fh, _LEN.size)
    if not raw:
        return None
    if len(raw) != _LEN.size:
        raise _bad("truncated", "inside a length")
    (n,) = _LEN.unpack(raw)
    if n == 0 or n > header_cap():
        raise _bad("length", f"{n}")
    data = _read_exact(fh, n)
    if len(data) != n:
        raise _bad("truncated", "inside a header")
    try:
        header = canon.parse(data)
    except ContractError as e:
        raise _bad("json", str(e)) from None
    if not isinstance(header, dict):
        raise _bad("json", "not an object")
    if canon.canonical_bytes(header) != data:
        raise _bad("json", "not canonical")
    if "end" in header:
        check_terminal(header)
    else:
        check_entry(header)
    return header


def iter_frames(fh: BinaryIO, *, payload_cap: int = _INT_MAX) -> Iterator[tuple[dict, bytes]]:
    """The reference decoder: every ``(header, payload)`` up to and including the terminal frame (payload
    ``b""``), checked against the terminal's ``count`` and ``bytes``. A stream with no terminal frame is
    ``frame-bad:terminal``; bytes after it are ``frame-bad:trailing``. Payloads are read whole, so this is for
    bounded streams (tests, the DONE); the importer streams payloads itself through :func:`read_header`."""
    count = total = 0
    while True:
        header = read_header(fh)
        if header is None:
            raise _bad("terminal", "the stream ended without one")
        if "end" in header:
            if header["count"] != count or header["bytes"] != total:
                raise _bad("terminal", f"says {header['count']}/{header['bytes']}, saw {count}/{total}")
            if fh.read(1):
                raise _bad("trailing")
            yield header, b""
            return
        size = header["size"]
        if total + size > payload_cap:
            raise _bad("size", f"payload past {payload_cap}")
        payload = _read_exact(fh, size)
        if len(payload) != size:
            raise _bad("truncated", "inside a payload")
        count += 1
        total += size
        yield header, payload
