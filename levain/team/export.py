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

import hashlib
import json
import re

from . import index as I

STREAM_HEADER = {"anneal_team_stream": 2}
# anneal's frame-label rule; the paths levain writes (<handle>/<device>.jsonl) fit it, others get a digest label
_FRAME_LABEL = re.compile(r"[A-Za-z0-9._@:+/=-]{1,200}")


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"


_OPAQUE = "unnamed-"


def _label(index: int, rel: str) -> str:
    """The frame label for the ``index``-th ledger file: its path when that fits anneal's rule, else
    ``unnamed-<index>-<sha256 of the path>``. Labels are unique BY CONSTRUCTION, with nothing to detect: a path that
    itself starts with ``unnamed-`` is treated as unframeable, so a literal label can never sit in the opaque namespace,
    and the file's index makes every opaque label differ even if two digests were ever equal. A path can be anything a
    member pushed under ``ledger/``, the label is only for grouping and report text, so a file that cannot be named is
    still exported, never a reason to withhold the rest."""
    if _FRAME_LABEL.fullmatch(rel) and not rel.startswith(_OPAQUE):
        return rel
    return f"{_OPAQUE}{index}-" + hashlib.sha256(rel.encode("utf-8", "surrogatepass")).hexdigest()


def _envelope(label: str, n: int, line: str) -> str:
    return json.dumps({"frame": label, "n": n, "line": line}, separators=(",", ":")) + "\n"


def export_stream(ledger: I.Ledger, *, in_force: bool = False) -> list[str]:
    """Output lines for ``levain team export --jsonl`` (each ends with a newline)."""
    if in_force:
        return [_line(e) for e in ledger.in_force]
    out = [json.dumps(STREAM_HEADER, separators=(",", ":")) + "\n"]
    for index, f in enumerate(sorted(ledger.files, key=lambda f: f.rel)):
        label = _label(index, f.rel)
        out.extend(_envelope(label, n, _line(obj).rstrip("\n")) for n, obj in enumerate(f.entries, 1))
    return out
