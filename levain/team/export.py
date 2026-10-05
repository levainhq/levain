"""``levain team export``: the ledger as JSON lines, for anneal-memory's team import and for people.

Default (``--jsonl``): the FRAMED stream of anneal's team-import contract v2: a header line, then one envelope per
VERIFIED ledger line, grouped per file in file order, so the reader can rebuild each file's ``prev -> hash`` chain, resolve
every ``supersedes`` target and require exactly one root per file. The ledger line travels as a JSON string value inside an
envelope this module builds with ``json.dumps``, so ledger content cannot become structure. A line that fails
verification (tampered, torn, misfiled) is left out here exactly as it is left out of enforcement; `levain team verify`
names it. ``in_force=True`` is a convenience view (only what is in force, time order): it is not framed and cannot be
chain-verified.

Framing makes per-file structure checkable (one root, one author per file); it is not authentication: the stream is one
trust unit this exporter vouches for.
"""
from __future__ import annotations

import json
import re

from . import index as I

STREAM_HEADER = {"anneal_team_stream": 2}
# anneal's frame-label rule; a ledger path under ledger/ (<handle>/<device>.jsonl) always fits it
_FRAME_LABEL = re.compile(r"[A-Za-z0-9._@:+/=-]{1,200}")


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"


def _envelope(label: str, n: int, line: str) -> str:
    return json.dumps({"frame": label, "n": n, "line": line}, separators=(",", ":")) + "\n"


def export_stream(ledger: I.Ledger, *, in_force: bool = False) -> list[str]:
    """Output lines for ``levain team export --jsonl`` (each ends with a newline)."""
    if in_force:
        return [_line(e) for e in ledger.in_force]
    out = [json.dumps(STREAM_HEADER, separators=(",", ":")) + "\n"]
    for f in sorted(ledger.files, key=lambda f: f.rel):
        if not _FRAME_LABEL.fullmatch(f.rel):
            raise ValueError(f"ledger file {f.rel!r} cannot be framed (label must match {_FRAME_LABEL.pattern})")
        out.extend(_envelope(f.rel, n, _line(obj).rstrip("\n")) for n, obj in enumerate(f.entries, 1))
    return out
