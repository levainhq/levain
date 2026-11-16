"""How full the live desk's context is, read from its session transcript (levain#79). Stdlib only.

The reading is flow's ``scripts/seat_context.py`` reading, kept in step with it: a session's transcript is a JSONL file in which every
assistant record carries a ``message.usage`` block, and the context in use at that turn is
``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``. The newest such record with a non-zero sum is the reading
(``seat_context.scan``), so the number a desk shows here and the one the fan-in's sweep prints for the same session agree to within the
turn that landed between the two reads.

BOUNDED: a transcript grows without limit, so the file is never read whole. The tail is read in growing windows (:data:`WINDOWS`, the last
of them :data:`MAX_TAIL`, the same 8 MB ``seat_context.scan`` reads) and the newest record with a usage block that window holds is taken;
a window that holds none says so and the next, larger one is tried. A transcript with none in :data:`MAX_TAIL` is unreadable here, never
zero.

The lines are the desk kind's rotation lines in ``seat_context.LIMITS``: SOFT (rotate at the next natural seam, advisory) and HARD
(rotate now). A desk Phill has told to go deep (``seat_context.limits_for``) has its HARD line raised to ``DEEP_HARD`` and its SOFT advisory
dropped: the host's context command says so (``deep``), and this module only draws what it was told. The numbers are in :data:`SOFT` / :data:`HARD` / :data:`DEEP_HARD`; a test holds
them equal to flow's table where flow's checkout is present.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = ["ContextView", "DEEP_HARD", "HARD", "MAX_TAIL", "SOFT", "WINDOWS", "lines_for", "parse_command", "read_context"]

SOFT = 350_000          # seat_context.LIMITS["desk"][0]
HARD = 500_000          # seat_context.LIMITS["desk"][1]
DEEP_HARD = 800_000     # seat_context.DEEP_HARD
MAX_TAIL = 8_000_000    # the most of a transcript's end that is read (seat_context.scan)
WINDOWS = (262_144, 1_048_576, 4_194_304, MAX_TAIL)


@dataclass(frozen=True)
class ContextView:
    """The desk's context as the Desk view draws it: tokens in use, the two rotation lines, the model the record names."""
    tokens: int
    soft: Optional[int]
    hard: int
    model: Optional[str] = None


def lines_for(deep: bool) -> tuple[Optional[int], int]:
    """``(soft, hard)`` for a desk: a go-deep grant raises HARD and drops SOFT (flow's ``limits_for``, kind desk)."""
    return (None, DEEP_HARD) if deep else (SOFT, HARD)


def _tail(f: Any, size: int, window: int) -> list[bytes]:
    """The whole lines in the last ``window`` bytes of an open file of ``size`` bytes. A seek that lands inside a record leaves its
    tail as a partial first line, which is dropped (the byte before the seek says whether it is); a seek on a record's first byte
    keeps it."""
    start = max(0, size - window)
    partial = False
    if start:
        f.seek(start - 1)
        partial = f.read(1) != b"\n"
    f.seek(start)
    lines = f.read(size - start).splitlines()
    return lines[1:] if partial else lines


def _usage_of(raw: bytes) -> Optional[tuple[int, Optional[str]]]:
    """``(tokens, model)`` of one transcript line that carries a non-zero usage sum, else None (seat_context.scan's filter and sum)."""
    if b'"usage"' not in raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    m = d.get("message") if isinstance(d, dict) else None
    u = m.get("usage") if isinstance(m, dict) else None
    if not u or not isinstance(u, dict):
        return None
    try:
        ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    except TypeError:
        return None
    if isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0:
        return None
    model = m.get("model")
    return ctx, (model if isinstance(model, str) and model else None)


def read_context(path: str | os.PathLike[str], deep: bool = False) -> Optional[ContextView]:
    """The newest context reading in the transcript at ``path``, or None when the file cannot be read or holds no usage record in
    its last :data:`MAX_TAIL` bytes. Reads only the tail, in growing windows; never the whole file."""
    soft, hard = lines_for(deep)
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            for window in WINDOWS:
                for raw in reversed(_tail(f, size, window)):
                    got = _usage_of(raw)
                    if got is not None:
                        return ContextView(got[0], soft, hard, got[1])
                if window >= size:
                    return None            # the window already held the whole file
    except OSError:
        return None
    return None


def parse_command(text: str) -> Optional[tuple[Path, bool]]:
    """``(transcript path, deep)`` from the host context command's stdout: one JSON object ``{"transcript": <absolute path to a
    .jsonl file>, "deep": bool?}``. None for anything else (not JSON, no path, a relative path, a path that is not a ``.jsonl``):
    unreadable is unknown, never a guess. Only those two fields are read."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    p = doc.get("transcript")
    if not isinstance(p, str) or not p or len(p) > 4096 or "\x00" in p or not os.path.isabs(p) or not p.endswith(".jsonl"):
        return None
    deep = doc.get("deep")
    return Path(p), deep is True
