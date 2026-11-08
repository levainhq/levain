"""Row 156 step 2C: the importer, a status markdown file's numbered LANE MENU rows -> the board store.

Each numbered row of the lane-menu section becomes one ready item, written through the ordinary ready
:func:`~levain.coordinator.ready.add` / :func:`~levain.coordinator.ready.correct` API (the coordinator role, under the
live lease token), never raw SQL, so the fence and the journal hold.

Field mapping (the worker-attach design, section 5):

* ``title``: the row's bold span, cut at its first sentence, at most 120 characters.
* ``done_line``: the text after the row's ``DONE:`` marker, cut at its first sentence, at most 1000 characters.
* ``tier``: the first ``(tier: <name>`` after the title, when it names a known tier.
* ``hold``: a prose hold. A row whose title or lead (the text before its first bracketed note) says ``HELD``
  (uppercase: the board's hold mark; a lowercase "held" is ordinary prose) or ``on hold`` (any case), as whole words, gets the sentence that says it. The item stays in its category; the reader derives "held" from the
  field.
* ``residue``: the band row ids (``np-xxxxxxxx``) the lead names, in order.
* ``ref``: ``STATUS_1_0.md#<n>@<blob sha>``, the sha being git's blob hash of the row's own line, so it changes
  exactly when the row's text does and the full text stays reachable by it.

Idempotent: a row whose imported fields already read so writes nothing; a changed row writes ONE correction. The
importer owns the text fields only. It never moves an existing item between categories and never reorders it (a
decision of the coordinator is not undone by a re-import); a new item enters ``ready``, or ``done`` when its title
leads with a landed word. A row gone from the file is left in the store and named in the result. The importer's id
map is a parameter (``id_for``); the default is ``levain:row-<n>``.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levain.coordinator import ready
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["ImportResult", "Row", "blob_sha", "default_id", "import_status", "parse_rows"]

REF_NAME = "STATUS_1_0.md"
TITLE_MAX = 120
DONE_MAX = 1000
HOLD_MAX = 1000
REF_MAX = 300
_ROW = re.compile(r"(\d+)\. \*\*(.+?)\*\*(.*)")
_SECTION = re.compile(r"## LANE MENU\b")
_TIER = re.compile(r"\(tier: (\w+)")
_TIER_NOTE = re.compile(r"^\s*\(tier:[^)]*\)\s*")
_BAND = re.compile(r"\bnp-[0-9a-f]{8}\b")
_HOLD = re.compile(r"(?:\bHELD\b|\b(?i:on hold)\b)(?!\s+(?i:back|branches)\b)")
_DONE_WORDS = re.compile(r"(?:ON MAIN|LANDED|RELEASED|DONE)\b")
_DONE_MARK = re.compile(r"\bDONE: ")
_BOUNDARY = re.compile(r"[.!?](?=\s+[A-Z`*(\[])|[.!?]$")


@dataclass(frozen=True)
class Row:
    n: int
    id: str
    title: str
    done_line: str | None
    tier: str | None
    hold: str | None
    residue: str | None
    ref: str
    category: str


@dataclass
class ImportResult:
    added: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)      # in the store from an earlier import, not in this file
    unparsed: list[int] = field(default_factory=list)    # numbered lines in the section the row shape did not fit


def blob_sha(data: bytes) -> str:
    """Git's blob hash of ``data`` (``git hash-object``), so a ref can be checked with git."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def default_id(n: int) -> str:
    return f"levain:row-{n}"


def _mask(text: str) -> str:
    """``text`` with every backticked span blanked to the same length, so a path or a version in code never ends a sentence."""
    return re.sub(r"`[^`]*`", lambda m: "x" * len(m.group(0)), text)


def _sentence(text: str, high: int) -> str:
    """The first sentence of ``text``, at most ``high`` characters (a longer one is cut at a word)."""
    text = " ".join(text.split())
    hit = _BOUNDARY.search(_mask(text))
    cut = text[: hit.end()] if hit else text
    if len(cut) > high:
        whole = text[high:high + 1].isspace()
        cut = cut[:high]
        if not whole and " " in cut:
            cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:")


def _lead(rest: str) -> str:
    """The row's text before its first bracketed note (the re-derive history lives in the brackets)."""
    at = rest.find(" [")
    return rest if at < 0 else rest[:at]


def _hold(title: str, lead: str) -> str | None:
    for text in (title, lead):
        hit = _HOLD.search(_mask(text))
        if hit:
            start = max((m.end() for m in _BOUNDARY.finditer(_mask(text), 0, hit.start())), default=0)
            return _sentence(text[start:], HOLD_MAX)
    return None


def parse_rows(text: str, *, id_for: Callable[[int], str] = default_id) -> tuple[list[Row], list[int]]:
    """The numbered rows of the lane-menu section of ``text``, and the numbers of lines that looked like rows but
    did not fit the shape. A duplicate row number keeps its first row."""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if _SECTION.match(ln)), None)
    if start is None:
        raise CoordError("no lane-menu section found in the status file")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    rows: list[Row] = []
    seen: set[int] = set()
    unparsed: list[int] = []
    for line in lines[start + 1:end]:
        head = re.match(r"(\d+)\. ", line)
        if head is None:
            continue
        m = _ROW.fullmatch(line)
        if m is None:
            unparsed.append(int(head.group(1)))
            continue
        n = int(m.group(1))
        if n in seen:
            continue
        seen.add(n)
        raw_title, rest = m.group(2), m.group(3)
        title = _sentence(raw_title, TITLE_MAX)
        lead = _lead(rest)
        mark = _DONE_MARK.search(rest)
        done = _sentence(rest[mark.end():], DONE_MAX) if mark else None
        tier = _TIER.search(rest)
        ids = list(dict.fromkeys(_BAND.findall(lead)))
        sha = blob_sha(line.encode())
        rows.append(Row(
            n=n, id=id_for(n), title=title, done_line=done or None,
            tier=tier.group(1) if tier and tier.group(1) in ready.TIERS else None,
            hold=_hold(raw_title, _TIER_NOTE.sub("", lead)), residue=", ".join(ids)[:HOLD_MAX] if ids else None,
            ref=f"{REF_NAME}#{n}@{sha}"[:REF_MAX],
            category="done" if _DONE_WORDS.match(raw_title) else "ready"))
    return rows, unparsed


def _stored(store: CoordStore, repo: str) -> dict[str, dict]:
    return {i["id"]: i for i in ready.list_ready(store, all=True, repo=repo)["items"] if i["repo"] == repo}


def import_status(store: CoordStore, token: int, path: str | Path, *, who: str = "status-import",
                  id_for: Callable[[int], str] = default_id, repo: str = "levain") -> ImportResult:
    """Import the numbered lane-menu rows of the status file at ``path`` into ``store`` under the coordinator
    lease ``token``. Returns what was added, corrected, left unchanged and left behind. Idempotent (see the module
    docstring)."""
    data = Path(path).read_bytes()
    rows, unparsed = parse_rows(data.decode("utf-8"), id_for=id_for)
    out = ImportResult(unparsed=unparsed)
    stored = _stored(store, repo)
    for row in rows:
        why = f"import of {row.ref}"
        if row.id not in stored:
            ready.add(store, token, row.id, repo=repo, title=row.title, who=who, why=why, to=row.category, tier=row.tier,
                      done_line=row.done_line, rank=row.n, hold=row.hold, residue=row.residue, ref=row.ref)
            out.added.append(row.id)
            continue
        have = stored[row.id]
        facts = ready.item_facts(store, row.id)
        want = {"title": row.title, "done_line": row.done_line, "tier": row.tier, "hold": row.hold,
                "residue": row.residue, "ref": row.ref}
        now = {"title": have["title"], "done_line": have["done_line"], "tier": have["tier"], "hold": facts["hold"],
               "residue": facts["residue"], "ref": facts["ref"]}
        if want == now:
            out.unchanged.append(row.id)
            continue
        ready.correct(store, token, row.id, to=have["declared_category"], who=who, why=why, **want)
        out.corrected.append(row.id)
    mine = {row.id for row in rows}
    out.absent = sorted(i for i in stored if i not in mine
                        and str(ready.item_facts(store, i)["ref"] or "").startswith(f"{REF_NAME}#"))
    return out
