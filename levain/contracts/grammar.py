"""§1.1: the grammar shared by F1-F5 (ASCII decimal, strict base64, the integer domain).

``int()`` is never called on unchecked text: it accepts ``+1``, `` 1``, ``1_0`` and fullwidth digits.
``base64.b64decode(..., validate=True)`` accepts non-zero pad bits, so a decode is followed by a re-encode
and an equality check (signed-note: "decoders MUST reject non-canonical encodings").
"""
from __future__ import annotations

import base64
import re

from . import ContractError

# Sizes, indices and timestamps in note text: levain's profile over RFC 9162's uint64 (Go's tlog uses int64).
NOTE_INT_MAX = 2**63 - 1
# Every integer inside a JSON leaf (F8): the largest integer an IEEE double holds exactly.
JSON_INT_MAX = 2**53 - 1

_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)", re.ASCII)
_B64 = re.compile(r"[A-Za-z0-9+/]*={0,2}", re.ASCII)


def parse_decimal(text: str, maximum: int = NOTE_INT_MAX) -> int:
    """An ASCII decimal with no leading zero except ``0`` itself, in ``0..maximum``."""
    if not isinstance(text, str) or not _DECIMAL.fullmatch(text):
        raise ContractError(f"not an ASCII decimal without leading zeros: {text!r}")
    # the length bound comes first: int() raises a plain ValueError past CPython's digit limit (4300)
    if len(text) > len(str(maximum)):
        raise ContractError(f"integer of {len(text)} digits is above {maximum}")
    try:
        n = int(text)
    except ValueError:
        raise ContractError(f"not a convertible decimal: {text[:32]!r}") from None
    if n > maximum:
        raise ContractError(f"integer {text} is above {maximum}")
    return n


def check_int(x: object, maximum: int) -> int:
    """An integer value in a frozen structure: ``type(x) is int`` (so ``True`` is refused), ``0 <= x <= maximum``."""
    if type(x) is not int:
        raise ContractError(f"not an integer: {x!r}")
    if x < 0 or x > maximum:
        raise ContractError(f"integer {x} is outside 0..{maximum}")
    return x


def b64decode(text: str) -> bytes:
    """Standard-alphabet base64 with ``=`` padding, canonical only."""
    if not isinstance(text, str) or len(text) % 4 or not _B64.fullmatch(text):
        raise ContractError("not standard padded base64")
    raw = base64.b64decode(text, validate=True)
    if base64.b64encode(raw).decode("ascii") != text:
        raise ContractError("non-canonical base64 (non-zero pad bits)")
    return raw


def b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")
