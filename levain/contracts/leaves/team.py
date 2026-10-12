"""F9: the ``line`` leaf, its device message, the sidecar receipt (a hint), and the team-log scope matrix.

Leaf ``(line, 1)``, team log only::

    {"leaf":"line","v":1,"file":<str>,"entry":<the SEALED entry, "prev" and "hash" included>,
     "device_fp":<F7 fingerprint>,"device_sig":<canonical base64 of the raw SSHSIG blob, no armor>}

No other key, and no ``n``: a line's position is derived from the log, never carried beside the signature. Ingest and
verify both: run the FROZEN entry schema (``entry_v1.validate_entry_v1``, with ``now`` = the ingest time at ingest
and ``None`` at verify, and ``known`` = the snapshot's entries by unique id, rev 11); recompute ``entry.hash = chain_hash(entry.prev, entry)``; verify ``device_sig`` (F12,
namespace ``levain-team-entry``) over ``string(origin) || string(file) || string(32-byte chain hash)``; refuse a
repeated ``(file, entry.hash)`` (an operator fault at verify) and an ``entry.id`` already held by an accepted leaf;
and apply ``may_write_team_log`` against the snapshot, the team log's verified prefix before this leaf. A leaf this
module refuses is invalid forever; any rule change is a new ``(line, v)``.

Scope is read from the key directory (P1), never from the leaf: callers pass the writing device's scope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .. import ContractError
from .. import canon
from .. import origin as O
from .. import sshsig
from ..grammar import JSON_INT_MAX, b64decode, b64encode, check_int
from ..keys import ssh_string
from ..namespaces import TEAM_ENTRY_NAMESPACE
from .entry_v1 import TS_RE, validate_entry_v1

LEAF, VERSION = "line", 1
LOG_KIND = "team"
LEAF_KEYS = frozenset({"leaf", "v", "file", "entry", "device_fp", "device_sig"})
SCOPES = ("human", "member", "receipt")
HUMAN_TYPES = frozenset({"finding", "question", "tension", "ack", "retire"})
MEMBER_TYPES = frozenset({"finding", "question", "tension"})
ALLOW, REFUSE = "allow", "refuse"
_FP = re.compile(r"SHA256:[A-Za-z0-9+/]{43}", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)


class UnknownScopeError(ContractError):
    """A scope this version does not know: REFUSE at ingest, UNVERIFIABLE at verify."""


class OperatorFault(ContractError):
    """A second leaf with an accepted leaf's ``(file, entry.hash)``: ingest refuses it, so at verify the operator
    appended it."""


@dataclass(frozen=True)
class LineLeaf:
    file: str
    entry: dict
    device_fp: str
    device_sig: bytes

    @property
    def hash(self) -> str:
        return self.entry["hash"]


def device_message(origin: str, file: str, chain_hash: str) -> bytes:
    """``string(origin ASCII) || string(file UTF-8) || string(32 raw bytes of the chain hash)``, SSH strings.

    ``file`` is the exact JSON string value (no Unicode normalisation); ``chain_hash`` is 64 lowercase hex.
    """
    O.check_team_origin(origin)
    if not isinstance(file, str):
        raise ContractError("a line's file is a string")
    if not isinstance(chain_hash, str) or not _HEX64.fullmatch(chain_hash):
        raise ContractError("a chain hash is 64 lowercase hex")
    try:
        f = file.encode("utf-8")
    except UnicodeEncodeError:
        raise ContractError("a line's file is not UTF-8 encodable") from None
    return ssh_string(origin.encode("ascii")) + ssh_string(f) + ssh_string(bytes.fromhex(chain_hash))


def check_line_fields(obj: dict) -> LineLeaf:
    """The context-free half of a ``(line, 1)`` leaf: every field's shape, the frozen entry schema (``now=None``),
    the recomputed chain hash and the SSHSIG blob's structure and profile. The registry's validator."""
    if not isinstance(obj, dict) or set(obj) != LEAF_KEYS:
        raise ContractError(f"a line leaf has exactly the keys {sorted(LEAF_KEYS)}")
    if obj["leaf"] != LEAF or type(obj["v"]) is not int or obj["v"] != VERSION:
        raise ContractError("not a (line, 1) leaf")
    file, entry, device_fp, device_sig = obj["file"], obj["entry"], obj["device_fp"], obj["device_sig"]
    if not isinstance(file, str):
        raise ContractError("a line's file is a string")
    if not isinstance(entry, dict):
        raise ContractError("a line's entry is an object")
    validate_entry_v1(entry, None, scan=False, now=None)
    if not isinstance(entry.get("prev"), str) or not isinstance(entry.get("hash"), str):
        raise ContractError("a line carries the SEALED entry, with its prev and hash")
    if entry["hash"] != canon.chain_hash(entry["prev"], entry):
        raise ContractError("entry.hash is not chain_hash(entry.prev, entry)")
    if not isinstance(device_fp, str) or not _FP.fullmatch(device_fp):
        raise ContractError("device_fp is an F7 fingerprint, SHA256:<43 base64>")
    if not isinstance(device_sig, str):
        raise ContractError("device_sig is canonical base64 text")
    blob = b64decode(device_sig)
    sshsig.check_profile(sshsig.parse(blob), namespace=TEAM_ENTRY_NAMESPACE, device_fp=device_fp)
    return LineLeaf(file, entry, device_fp, blob)


class Snapshot:
    """The team log's verified prefix before a leaf: the ACCEPTED line leaves, in log order."""

    def __init__(self, leaves: Iterable[LineLeaf] = ()):
        self._by_id: dict[str, list[dict]] = {}
        self._file_hash: set[tuple[str, str]] = set()
        for leaf in leaves:
            self.add(leaf)

    def add(self, leaf: LineLeaf) -> None:
        self._by_id.setdefault(leaf.entry["id"], []).append(leaf.entry)
        self._file_hash.add((leaf.file, leaf.hash))

    def holds_id(self, entry_id: str) -> bool:
        return entry_id in self._by_id

    def entries_with_id(self, entry_id: str) -> list[dict]:
        return list(self._by_id.get(entry_id, ()))

    def holds_file_hash(self, file: str, chain_hash: str) -> bool:
        return (file, chain_hash) in self._file_hash

    def known(self) -> dict[str, dict]:
        """The frozen schema's ``known`` (S1_DESIGN rev 11): unique entry id -> entry. An id held twice (unreachable
        after X01) is left out, so a link to it is refused as unknown: default-deny, as the matrix reads it."""
        return {i: es[0] for i, es in self._by_id.items() if len(es) == 1}


def may_write_team_log(scope: str, entry: dict, snapshot: Snapshot) -> str:
    """``ALLOW`` or ``REFUSE`` for a validated entry from a device of ``scope``; total over every entry type.

    TS l.410-412: ``human`` writes finding, question, tension, ack, retire; ``member`` writes finding, question,
    tension and carries no non-empty ``supersedes``; ``receipt`` writes no line at all. For every scope until P2:
    ``decision`` / ``constraint`` and any ``kind: "ruling"`` are refused. ``supersedes`` is default-deny: every
    target must be in the snapshot exactly once and not a ruling. Raises ``UnknownScopeError`` for any other scope.
    """
    if scope not in SCOPES:
        raise UnknownScopeError(f"unknown scope {scope!r}")
    t = entry.get("type")
    if scope == "receipt":
        return REFUSE
    if t in ("decision", "constraint") or entry.get("kind") == "ruling":
        return REFUSE
    if t not in (HUMAN_TYPES if scope == "human" else MEMBER_TYPES):
        return REFUSE
    targets = entry.get("supersedes", [])
    if not isinstance(targets, list):
        return REFUSE
    if scope == "member" and len(targets) > 0:
        return REFUSE
    if t == "retire" and not targets:
        return REFUSE
    for target in targets:
        held = snapshot.entries_with_id(target) if isinstance(target, str) else []
        if len(held) != 1 or held[0].get("kind") == "ruling":
            return REFUSE
    return ALLOW


def check_line(d: bytes, *, origin: str, scope: str, snapshot: Snapshot, now: datetime | None) -> LineLeaf:
    """The full check of one ``(line, 1)`` leaf at ingest (``now`` = the ingest time) or verify (``now=None``).

    Raises ``canon.UnknownLeafError``, ``UnknownScopeError``, ``OperatorFault`` or ``ContractError``.
    """
    obj = canon.check_leaf(d, LOG_KIND)
    if obj["leaf"] != LEAF:
        raise ContractError(f"a {obj['leaf']!r} leaf is not a line")
    leaf = check_line_fields(obj)
    if scope not in SCOPES:
        raise UnknownScopeError(f"unknown scope {scope!r}")
    if snapshot.holds_file_hash(leaf.file, leaf.hash):
        raise OperatorFault(f"a second leaf for ({leaf.file!r}, {leaf.hash})")
    if snapshot.holds_id(leaf.entry["id"]):
        raise ContractError(f"entry id {leaf.entry['id']!r} is already held by an accepted leaf")
    # rev 11: the frozen schema's snapshot rules (refs and supersedes held; an ack or a retire never superseded)
    validate_entry_v1(leaf.entry, snapshot.known(), scan=False, now=now)
    sshsig.verify(leaf.device_sig, namespace=TEAM_ENTRY_NAMESPACE, device_fp=leaf.device_fp,
                  message=device_message(origin, leaf.file, leaf.hash))
    if may_write_team_log(scope, leaf.entry, snapshot) != ALLOW:
        raise ContractError(f"scope {scope!r} may not write this entry to the team log")
    return leaf


def judge_line(d: bytes, *, origin: str, scope: str, snapshot: Snapshot, at: str,
               now: datetime | None = None) -> tuple[str, LineLeaf | None, str]:
    """``(verdict, leaf or None, reason)``; verdict is ``canon.ACCEPT``, ``REFUSE`` or ``UNVERIFIABLE``.

    ``at="ingest"`` needs ``now``; ``at="verify"`` ignores it (a verifier cannot know the ingest time). An unknown
    ``(leaf, v)`` or scope is REFUSE at ingest and UNVERIFIABLE at verify. Any exception is a refusal, never a crash.
    """
    if at == "ingest":
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("ingest needs the ingest time as an aware datetime")
    elif at == "verify":
        now = None
    else:
        raise ValueError("at is 'ingest' or 'verify'")
    try:
        leaf = check_line(d, origin=origin, scope=scope, snapshot=snapshot, now=now)
    except (canon.UnknownLeafError, UnknownScopeError) as e:
        return (canon.REFUSE if at == "ingest" else canon.UNVERIFIABLE), None, str(e)
    except OperatorFault as e:
        return canon.REFUSE, None, f"operator fault: {e}"
    except ContractError as e:
        return canon.REFUSE, None, str(e)
    except Exception as e:   # noqa: BLE001 - F8: any exception in leaf validation is a refusal
        return canon.REFUSE, None, f"leaf validation failed: {type(e).__name__}"
    return canon.ACCEPT, leaf, ""


def verify_team_lines(leaves: Iterable[tuple[bytes, str]], origin: str) -> list[tuple[str, str]]:
    """Walk a team log's line leaves in log order at verify; ``leaves`` = ``(leaf bytes, writer's scope)``.

    The snapshot grows by ACCEPTED leaves only, so the first accepted holder of an id wins and a refused leaf holds
    no id. Returns ``(verdict, reason)`` per leaf.
    """
    snap = Snapshot()
    out = []
    for d, scope in leaves:
        verdict, leaf, reason = judge_line(d, origin=origin, scope=scope, snapshot=snap, at="verify")
        if leaf is not None:
            snap.add(leaf)
        out.append((verdict, reason))
    return out


# ---- the sidecar receipt: a HINT format ------------------------------------------------------------------------

RECEIPT_KEYS = frozenset({"v", "type", "file", "n", "hash", "handle", "device", "scope", "device_sig", "leaf",
                          "team_version", "accepted"})
_DERIVED_INTS = ("n", "leaf", "team_version")


def device_hint(leaf: LineLeaf) -> str:
    """The sidecar's ``device``: ``fp_hex`` of the signing key's blob, first 16 hex characters (a display hint)."""
    return O.fp_hex(sshsig.parse(leaf.device_sig).public_key)[:16]


def derive_receipt(leaf: LineLeaf, *, n: int, leaf_index: int, handle: str, scope: str, team_version: int) -> dict:
    """Every sidecar field a client derives from the verified log and the key directory (all but ``accepted``)."""
    for x in (n, leaf_index, team_version):
        check_int(x, JSON_INT_MAX)
    if n < 1:
        raise ContractError("n is a 1-based position")
    return {"v": 1, "type": "device", "file": leaf.file, "n": n, "hash": leaf.hash, "handle": handle,
            "device": device_hint(leaf), "scope": scope, "device_sig": b64encode(leaf.device_sig), "leaf": leaf_index,
            "team_version": team_version}


def check_receipt(receipt: object, derived: dict) -> None:
    """Refuse a sidecar receipt whose fields disagree with what the client derives. Nothing reads authority from it.

    ``accepted`` is the server's clock and cannot be derived: it is checked for shape only.
    """
    if set(derived) != RECEIPT_KEYS - {"accepted"}:
        raise ValueError("derived holds every receipt field but accepted (derive_receipt)")
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise ContractError(f"a device receipt has exactly the keys {sorted(RECEIPT_KEYS)}")
    for k, want in derived.items():
        got = receipt[k]
        if k in _DERIVED_INTS or k == "v":
            if type(got) is not int:
                raise ContractError(f"receipt {k} is an integer")
        if got != want:
            raise ContractError(f"receipt {k} {got!r} disagrees with the derived {want!r}")
    if not isinstance(receipt["accepted"], str) or not TS_RE.fullmatch(receipt["accepted"]):
        raise ContractError("receipt accepted is YYYY-MM-DDTHH:MM:SSZ")
