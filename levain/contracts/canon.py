"""F8: canonical JSON, the parse refusals, log-leaf framing, the global leaf registry, and ``chain_hash``.

Canonical JSON is the byte form ``team/entry.py`` ``canonical()`` already writes: ``json.dumps(obj,
sort_keys=True, separators=(",", ":"), ensure_ascii=False)``, UTF-8. Keys sort by Unicode code point; ``"``
and ``\\`` are escaped, ``\\b \\f \\n \\r \\t`` use the short forms, other U+0000-U+001F are ``\\u00xx``, and
everything else (DEL, U+2028, U+2029, ``<``, ``>``, ``&``) is written raw.

A log leaf's data ``d`` is accepted only when ``canonical(parse(d)) == d`` byte for byte, so one leaf object
has one leaf hash. ``parse`` refuses what ``json.loads`` lets through: duplicate keys, lone surrogates,
floats, NaN/Infinity, integers outside ±(2^53-1), nesting deeper than ``MAX_DEPTH`` (measured on the text,
iteratively, before the parser runs), a BOM, and anything that is not UTF-8. Any exception at any step of leaf
validation is a refusal, never a crash.

This module imports nothing from ``team``; ``chain_hash`` is a copy of ``team/entry.py``'s formula at 31dea1f,
pinned by test against every entry ``tests/test_team_golden.py`` ships.
"""
from __future__ import annotations

import hashlib
import json

from . import ContractError
from .grammar import JSON_INT_MAX, check_int

MAX_DEPTH = 32        # levain's bound (no spec governs it)
LOG_KINDS = ("team", "fleet", "receipt")

# The one global registry: (leaf, v) -> the log kinds that accept it. A leaf name has one meaning across every
# log; a new v is a new entry and an old (leaf, v) stays verifiable forever. Provisional shapes are never here.
REGISTRY: dict[tuple[str, int], frozenset[str]] = {
    ("line", 1): frozenset({"team"}),                  # F9
    ("receipt_checkpoint", 1): frozenset({"team"}),    # F10
}

ACCEPT, REFUSE, UNVERIFIABLE = "accept", "refuse", "unverifiable"


class UnknownLeafError(ContractError):
    """An unregistered ``(leaf, v)``: refused at ingest, UNVERIFIABLE at verify ("needs a newer levain")."""


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_bytes(obj: object) -> bytes:
    return canonical(obj).encode("utf-8")


def chain_hash(prev: str, entry: dict) -> str:
    """Lowercase hex SHA-256 of UTF-8(prev || canonical(entry without "hash"))."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256((prev + canonical(body)).encode("utf-8")).hexdigest()


def text_depth(text: str) -> int:
    """The deepest nesting of ``[``/``{`` outside strings, by one pass over the text (never by recursion)."""
    depth = deepest = 0
    in_str = escaped = False
    for c in text:
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif c in "]}":
            depth -= 1
    return deepest


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ContractError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_float(s: str) -> object:
    raise ContractError(f"float {s} in a leaf")


def _no_constant(s: str) -> object:
    raise ContractError(f"{s} in a leaf")


_JSON_INT_DIGITS = len(str(JSON_INT_MAX))


def _int(s: str) -> int:
    if len(s.lstrip("-")) > _JSON_INT_DIGITS:   # before int(), which raises a plain ValueError past 4300 digits
        raise ContractError(f"integer of {len(s)} characters is outside ±{JSON_INT_MAX}")
    try:
        n = int(s)
    except ValueError:
        raise ContractError(f"not a convertible integer: {s[:32]!r}") from None
    if abs(n) > JSON_INT_MAX:
        raise ContractError(f"integer {s} is outside ±{JSON_INT_MAX}")
    return n


def _no_lone_surrogate(obj: object) -> None:
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            x.encode("utf-8")    # raises UnicodeEncodeError on a lone surrogate
        elif isinstance(x, dict):
            for k, v in x.items():
                k.encode("utf-8")
                stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)


def parse(d: bytes) -> object:
    """Parse JSON leaf bytes under F8's refusals. Raises ``ContractError``."""
    try:
        if not isinstance(d, bytes):
            raise ContractError("leaf data is bytes")
        text = d.decode("utf-8")
        if text.startswith("﻿"):
            raise ContractError("a byte-order mark in leaf data")
        if text_depth(text) > MAX_DEPTH:
            raise ContractError(f"nesting deeper than {MAX_DEPTH}")
        obj = json.loads(text, object_pairs_hook=_pairs, parse_float=_no_float, parse_constant=_no_constant,
                         parse_int=_int)
        _no_lone_surrogate(obj)
        return obj
    except ContractError:
        raise
    except Exception as e:   # noqa: BLE001 - every failure to parse is a refusal (F8)
        raise ContractError(f"leaf data does not parse: {type(e).__name__}") from None


def check_leaf(d: bytes, log_kind: str) -> dict:
    """Ingest-time framing check: canonical bytes, a registered ``(leaf, v)``, accepted on ``log_kind``.

    Returns the parsed leaf object. Raises ``UnknownLeafError`` for an unregistered ``(leaf, v)`` and
    ``ContractError`` for every other refusal. The leaf type's own field rules are its module's (F9, F10).
    """
    try:
        if log_kind not in LOG_KINDS:
            raise ContractError(f"unknown log kind {log_kind!r}")
        obj = parse(d)
        if not isinstance(obj, dict):
            raise ContractError("a leaf is a JSON object")
        if canonical_bytes(obj) != d:
            raise ContractError("leaf bytes are not canonical")
        leaf, v = obj.get("leaf"), obj.get("v")
        if not isinstance(leaf, str) or not leaf:
            raise ContractError('a leaf carries a non-empty string "leaf"')
        check_int(v, JSON_INT_MAX)
        kinds = REGISTRY.get((leaf, v))
        if kinds is None:
            raise UnknownLeafError(f"unknown leaf type ({leaf!r}, {v})")
        if log_kind not in kinds:
            raise ContractError(f"leaf {leaf!r} v{v} is not accepted on a {log_kind} log")
        return obj
    except ContractError:
        raise
    except Exception as e:   # noqa: BLE001
        raise ContractError(f"leaf validation failed: {type(e).__name__}") from None


def classify_leaf(d: bytes, log_kind: str, *, at: str) -> str:
    """``ACCEPT``, ``REFUSE`` or ``UNVERIFIABLE`` for leaf framing; ``at`` is ``"ingest"`` or ``"verify"``.

    An unknown ``(leaf, v)`` is REFUSE at ingest and UNVERIFIABLE at verify, never tampering.
    """
    if at not in ("ingest", "verify"):
        raise ValueError("at is 'ingest' or 'verify'")
    try:
        check_leaf(d, log_kind)
    except UnknownLeafError:
        return REFUSE if at == "ingest" else UNVERIFIABLE
    except ContractError:
        return REFUSE
    return ACCEPT
