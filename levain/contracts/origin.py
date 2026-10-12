"""F6: log origins. The grammar and the team production are frozen; fleet and receipt productions are not.

Grammar: ASCII segments ``[a-z0-9][a-z0-9._-]*`` joined by ``/``, at most 255 bytes, a subset of C2SP's
cosigner-name rule, so every levain origin is a legal note key name. A levain origin carries one of the
frozen prefixes; a new prefix is a version, never an edit. The one frozen production is the team log's,
``levain-team/<32 lowercase hex>`` (128 random bits chosen at genesis). ``levain-fleet/…`` (P13) and
``levain-receipt/…`` (P12) are checked by prefix and grammar only.
"""
from __future__ import annotations

import hashlib
import re

from . import ContractError

MAX_ORIGIN_BYTES = 255
TEAM_PREFIX = "levain-team/"
FLEET_PREFIX = "levain-fleet/"
RECEIPT_PREFIX = "levain-receipt/"
PREFIXES = {TEAM_PREFIX: "team", FLEET_PREFIX: "fleet", RECEIPT_PREFIX: "receipt"}
TEAM_ID_HEX = 32

_SEGMENT = r"[a-z0-9][a-z0-9._-]*"
_GRAMMAR = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*", re.ASCII)
_TEAM = re.compile(rf"levain-team/[0-9a-f]{{{TEAM_ID_HEX}}}", re.ASCII)


def check_grammar(origin: object) -> str:
    if not isinstance(origin, str) or not _GRAMMAR.fullmatch(origin):
        raise ContractError(f"origin {origin!r} is not in the levain origin grammar")
    if len(origin.encode("ascii")) > MAX_ORIGIN_BYTES:
        raise ContractError(f"origin is over {MAX_ORIGIN_BYTES} bytes")
    return origin


def log_kind(origin: object) -> str:
    """'team', 'fleet' or 'receipt' for a levain origin; refuses any other origin."""
    check_grammar(origin)
    for prefix, kind in PREFIXES.items():
        if origin.startswith(prefix) and len(origin) > len(prefix):
            return kind
    raise ContractError(f"origin {origin!r} carries no levain prefix")


def check_team_origin(origin: object) -> str:
    """The frozen team production, exactly: ``levain-team/`` + 32 lowercase hex, nothing after."""
    check_grammar(origin)
    if not _TEAM.fullmatch(origin):
        raise ContractError(f"origin {origin!r} is not levain-team/<32 lowercase hex>")
    return origin


def team_origin(team_id: str) -> str:
    return check_team_origin(TEAM_PREFIX + team_id)


def fp_hex(blob: bytes) -> str:
    """Lowercase hex of SHA-256 of an SSH public-key wire blob (the digest F7's fingerprint base64-encodes)."""
    if not isinstance(blob, bytes):
        raise ContractError("a key blob is bytes")
    return hashlib.sha256(blob).hexdigest()
