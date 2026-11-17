"""levain.desk.board_chain — the one chain checker for ``items.jsonl`` and ``repos.jsonl`` (design 7.4, P1).

Stdlib only and no levain import, so both pure folds (:mod:`levain.desk.board_state`, :mod:`levain.desk.repos_fold`) and the
writer (:mod:`levain.desk.board_store`) call the SAME functions over the SAME lines. It reads nothing and writes nothing.

The rules, per file (the two files are two chains; nothing chains across them):

* the LEGACY PREFIX is every line before the first one carrying ``prev``;
* the CUTOVER is that first line: its ``prev`` must be the sha256 of the WHOLE prefix's bytes (each line as written, plus the
  newline that ends it), or of empty bytes for a file that never had a legacy line;
* every later record must carry ``prev``, equal to the sha256 of the previous line's bytes as written (newline excluded).

A line that is not a JSON object (a torn line, a blank line) is not a record: it is never judged for ``prev`` (the fold
reports it torn), but its bytes are still the previous line of the next record, so a deleted, inserted or edited line breaks
the chain at the record that follows it. A line is parsed as TEXT decoded as UTF-8 (:func:`record_of`), the same everywhere, so
a BOM line is judged the same by this checker, the folds and the writer.

Lines are split on ``\\n`` ONLY by the caller (never ``splitlines()``) and passed as given, ``bytes`` or ``str`` (a ``str`` is
hashed as its UTF-8), without the trailing empty piece. The breaks come back as NAMED problems; what a break MEANS (a torn
file, a refused write) is the caller's: a chain break is read exactly as a torn line.

An ``ack-torn`` binds the EXACT break. For a TORN line (it does not parse) the ack records the line and the sha256 of its bytes,
and the fold drops the line. For a CHAIN break (the line parses; its ``prev`` does not match) the ack also records ``want`` (the
``prev`` the line carries; null for an unchained record) and ``have`` (what the checker computed it should be: the sha256 of line
N-1's bytes, or of the legacy prefix at the cutover). The folds honour it ONLY while the line is unchanged and ``want`` and
``have`` still hold, so a later edit to line N-1 (or N) breaks again; line N itself is KEPT in the fold, never dropped. An ack is
collected only from a line that itself parses and chains, and is judged by one function (:func:`parse_ack`).

Known edges, by design:

* stripping ``prev`` from every record reads as an all-legacy file (no break): only a head kept outside the file (P6
  ``export`` / ``verify --head``) catches it;
* deleting records at the tail is invisible without a kept head (design 7.4);
* an ``ack-torn`` is an asserted record unless signed (P2), so the trail reports who acknowledged.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = ["ACK_OP", "Ack", "AckError", "Chain", "ChainBreak", "UNCHAINED", "acked_lines", "check_chain", "honours", "line_hash",
           "parse_ack", "record_of"]

ACK_OP = "ack-torn"
UNCHAINED = "unchained record after the cutover (an old writer?)"
CUTOVER_BAD = "cutover prev does not commit the legacy prefix"
_HEX = re.compile(r"[0-9a-f]{64}")
_SUFFIX = "an edit, a deletion, an insertion or a reorder before line {n}"


@dataclass(frozen=True)
class ChainBreak:
    """One break: the 1-based ``line``, the named ``problem`` (no ``line N:`` prefix; the caller adds it), ``want`` (the ``prev`` the
    line carries, None when it has none) and ``have`` (the hash the checker computed for it: line N-1's, or the prefix's)."""

    line: int
    problem: str
    want: str | None = None
    have: str | None = None


@dataclass(frozen=True)
class Chain:
    breaks: tuple[ChainBreak, ...] = ()
    cutover: int | None = None          # the line number of the first record carrying prev, None while the file is all legacy
    legacy: int = 0                     # how many lines precede the cutover (all of them, when there is none)

    def by_line(self) -> dict[int, ChainBreak]:
        return {b.line: b for b in self.breaks}


def _bytes(line: Any) -> bytes:
    if isinstance(line, bytes):
        return line
    if isinstance(line, str):
        return line.encode("utf-8", "surrogatepass")
    return b""                              # not text at all: the fold reports it torn; it has no bytes to hash


def line_hash(line: Any) -> str:
    """The lowercase hex sha256 of a line's bytes as written (newline excluded)."""
    return hashlib.sha256(_bytes(line)).hexdigest()


def record_of(line: Any) -> dict | None:
    """The line's JSON object, or None when it is not one (blank, torn, not UTF-8, nested too deep, not an object). A ``bytes``
    line is decoded as UTF-8 and parsed as text (``json.loads`` on bytes would strip a BOM, as no fold does); the text is parsed as
    it stands, not stripped."""
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not isinstance(line, str):
            return None
        rec = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def _why_class(n: int, p: str, hashes: list[str]) -> str:
    """Name the break at line ``n`` (1-based) whose ``prev`` is ``p``, from the hash of every line."""
    earlier = [j for j in range(1, n - 1) if hashes[j - 1] == p]
    if earlier:
        j = max(earlier)
        between = f"line {n - 1} was" if j == n - 2 else f"lines {j + 1}..{n - 1} were"
        return f"{between} inserted or moved between line {j} and line {n} (line {n} chained onto line {j})"
    later = [j for j in range(n + 1, len(hashes) + 1) if hashes[j - 1] == p]
    if later:
        return f"lines were reordered (it chains onto line {min(later)}, which now follows it)"
    return f"line {n - 1} was edited or deleted, or a line was inserted before line {n}"


def check_chain(lines: Sequence[Any]) -> Chain:
    """Walk ``lines`` (split on ``\\n`` only, no trailing empty piece) and report every break."""
    blines = [_bytes(x) for x in lines]
    hashes = [hashlib.sha256(b).hexdigest() for b in blines]
    prefix = bytearray()
    cutover: int | None = None
    breaks: list[ChainBreak] = []
    for n, b in enumerate(blines, 1):
        rec = record_of(lines[n - 1])
        if cutover is None:
            if rec is None or "prev" not in rec:
                prefix += b + b"\n"
                continue
            cutover = n
            have = hashlib.sha256(bytes(prefix)).hexdigest()
            if rec["prev"] != have:
                breaks.append(ChainBreak(n, f"{CUTOVER_BAD} (line {n} chained onto {rec['prev']}; the prefix is now {have}: "
                                            + _SUFFIX.format(n=n) + ")", rec["prev"], have))
            continue
        if rec is None:
            continue
        have = hashes[n - 2]
        if "prev" not in rec:
            breaks.append(ChainBreak(n, f"{UNCHAINED} (line {n} carries no prev; line {n - 1} is {have})", None, have))
        elif rec["prev"] != have:
            want = rec["prev"]
            klass = _why_class(n, want, hashes) if isinstance(want, str) else f"line {n - 1} was edited, or a line was inserted before line {n}"
            breaks.append(ChainBreak(n, f"{klass}: prev does not match line {n - 1} (line {n} chained onto {want}; line {n - 1} is now "
                                        f"{have}: {_SUFFIX.format(n=n)})", want if isinstance(want, str) else None, have))
    return Chain(tuple(breaks), cutover, len(blines) if cutover is None else cutover - 1)


class AckError(ValueError):
    """An ``ack-torn`` record that is ill-shaped; the message names how."""


@dataclass(frozen=True)
class Ack:
    line: int
    sha256: str
    why: str
    chain: bool                         # True when it carries ``want`` and ``have``: an ack of a CHAIN break
    want: str | None = None
    have: str | None = None


def parse_ack(rec: Any) -> Ack | None:
    """The ONE judge of an ``ack-torn`` record's shape: None when ``rec`` is not one (any other op), an :class:`Ack`, or
    :class:`AckError` naming what is wrong (version 1, non-empty ``at``/``by``/``why``, a positive integer ``line``, a 64-hex
    ``sha256``; ``have`` (64-hex) and ``want`` (64-hex or null) together or not at all)."""
    if not isinstance(rec, dict) or rec.get("op") != ACK_OP:
        return None
    if type(rec.get("v")) is not int or rec["v"] != 1:
        raise AckError(f"unknown record version {rec.get('v')!r}")
    for key in ("at", "by", "why"):
        v = rec.get(key)
        if not isinstance(v, str) or not v.strip():
            raise AckError(f"{key} must be a non-empty string")
    n, sha = rec.get("line"), rec.get("sha256")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise AckError("ack-torn line must be a positive integer")
    if not isinstance(sha, str) or not _HEX.fullmatch(sha):
        raise AckError("ack-torn sha256 must be 64 lowercase hex")
    if "have" not in rec and "want" not in rec:
        return Ack(n, sha, rec["why"], False)
    want, have = rec.get("want"), rec.get("have")
    if not isinstance(have, str) or not _HEX.fullmatch(have) or "want" not in rec or not (
            want is None or (isinstance(want, str) and _HEX.fullmatch(want))):
        raise AckError("ack-torn want must be 64 lowercase hex or null, and have 64 lowercase hex, together")
    return Ack(n, sha, rec["why"], True, want, have)


def honours(ack: Ack, line_sha: str, brk: ChainBreak | None) -> bool:
    """Does ``ack`` acknowledge the line whose bytes hash to ``line_sha``, given that line's chain break (None for a torn line)?
    A torn line: the hash matches. A chain break: the hash matches AND the ack names this very break (``want`` and ``have``), so a
    later edit to the line before it, or to the line, no longer matches."""
    if ack.sha256 != line_sha:
        return False
    if brk is None:
        return True
    return ack.chain and ack.want == brk.want and ack.have == brk.have


def acked_lines(lines: Sequence[Any], chain: Chain | None = None) -> set[int]:
    """The line numbers some ``ack-torn`` record in ``lines`` acknowledges (the writer's view; each fold applies this same rule
    plus its own record checks). Acks are collected only from lines that themselves parse and chain."""
    chain = chain or check_chain(lines)
    breaks = chain.by_line()
    out: set[int] = set()
    for m, line in enumerate(lines, 1):
        if m in breaks:
            continue
        try:
            ack = parse_ack(record_of(line))
        except AckError:
            continue
        if ack is not None and ack.line <= len(lines) and honours(ack, line_hash(lines[ack.line - 1]), breaks.get(ack.line)):
            out.add(ack.line)
    return out
