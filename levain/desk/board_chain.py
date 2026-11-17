"""levain.desk.board_chain — the one chain checker for ``items.jsonl`` and ``repos.jsonl`` (design 7.4, P1).

Stdlib only and no levain import, so both pure folds (:mod:`levain.desk.board_state`, :mod:`levain.desk.repos_fold`) and the
writer (:mod:`levain.desk.board_store`) call the SAME function over the SAME lines. It reads nothing and writes nothing.

The rules, per file (the two files are two chains; nothing chains across them):

* the LEGACY PREFIX is every line before the first one carrying ``prev``;
* the CUTOVER is that first line: its ``prev`` must be the sha256 of the WHOLE prefix's bytes (each line as written, plus the
  newline that ends it), or of empty bytes for a file that never had a legacy line;
* every later record must carry ``prev``, equal to the sha256 of the previous line's bytes as written (newline excluded).

A line that is not a JSON object (a torn line, a blank line) is not a record: it is never judged for ``prev`` (the fold
reports it torn), but its bytes are still the previous line of the next record, so a deleted, inserted or edited line breaks
the chain at the record that follows it.

Lines are split on ``\\n`` ONLY by the caller (never ``splitlines()``) and passed as given, ``bytes`` or ``str`` (a ``str`` is
hashed as its UTF-8), without the trailing empty piece. The breaks come back as NAMED problems; what a break MEANS (a torn
file, a refused write) is the caller's: a chain break is read exactly as a torn line.

An ``ack-torn`` re-anchors nothing by itself: it is an ordinary chained record whose ``prev`` is the line before it, which at
the tail (where a torn line is acknowledged in practice, since the writer refuses after one) is the torn line, so its ``prev``
equals the ``sha256`` it names. A break in the middle of the file is acknowledged by naming that line; the lines after it
that chained onto it stay valid because the hash they carry is of the line's bytes, whatever the fold makes of it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = ["ACK_OP", "Chain", "ChainBreak", "UNCHAINED", "acked_lines", "check_chain", "line_hash"]

ACK_OP = "ack-torn"
UNCHAINED = "unchained record after the cutover (an old writer?)"
CUTOVER_BAD = "cutover prev does not commit the legacy prefix"


@dataclass(frozen=True)
class ChainBreak:
    """One break: the 1-based ``line`` and the named ``problem`` (no ``line N:`` prefix; the caller adds it)."""

    line: int
    problem: str


@dataclass(frozen=True)
class Chain:
    breaks: tuple[ChainBreak, ...] = ()
    cutover: int | None = None          # the line number of the first record carrying prev, None while the file is all legacy
    legacy: int = 0                     # how many lines precede the cutover (all of them, when there is none)

    def by_line(self) -> dict[int, str]:
        return {b.line: b.problem for b in self.breaks}


def _bytes(line: Any) -> bytes:
    if isinstance(line, bytes):
        return line
    if isinstance(line, str):
        return line.encode("utf-8", "surrogatepass")
    return b""                              # not text at all: the fold reports it torn; it has no bytes to hash


def line_hash(line: Any) -> str:
    """The lowercase hex sha256 of a line's bytes as written (newline excluded)."""
    return hashlib.sha256(_bytes(line)).hexdigest()


def _record(line: bytes) -> dict | None:
    """The line's JSON object, or None when it is not one (blank, torn, nested too deep, not an object)."""
    try:
        rec = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def check_chain(lines: Sequence[Any]) -> Chain:
    """Walk ``lines`` (split on ``\\n`` only, no trailing empty piece) and report every break."""
    blines = [_bytes(x) for x in lines]
    prefix = bytearray()
    cutover: int | None = None
    breaks: list[ChainBreak] = []
    for n, b in enumerate(blines, 1):
        rec = _record(b)
        if cutover is None:
            if rec is None or "prev" not in rec:
                prefix += b + b"\n"
                continue
            cutover = n
            if rec["prev"] != hashlib.sha256(bytes(prefix)).hexdigest():
                breaks.append(ChainBreak(n, CUTOVER_BAD))
            continue
        if rec is None:
            continue
        if "prev" not in rec:
            breaks.append(ChainBreak(n, UNCHAINED))
        elif rec["prev"] != hashlib.sha256(blines[n - 2]).hexdigest():
            breaks.append(ChainBreak(n, f"prev does not match line {n - 1}"))
    return Chain(tuple(breaks), cutover, len(blines) if cutover is None else cutover - 1)


def acked_lines(lines: Sequence[Any]) -> set[int]:
    """The line numbers some ``ack-torn`` record in ``lines`` acknowledges: a well-shaped ack (version 1, a positive integer
    ``line``, a 64-hex ``sha256``, a non-empty ``why``) whose ``sha256`` is the hash of that line's bytes. The writer's view; each
    fold applies this rule plus its own record checks."""
    out: set[int] = set()
    for b in (_bytes(x) for x in lines):
        rec = _record(b)
        if rec is None or rec.get("op") != ACK_OP or rec.get("v") != 1:
            continue
        n, sha, why = rec.get("line"), rec.get("sha256"), rec.get("why")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(lines):
            continue
        if isinstance(sha, str) and isinstance(why, str) and why.strip() and sha == line_hash(lines[n - 1]):
            out.add(n)
    return out
