"""The FROZEN entry schema of ``(line, 1)``: a copy of ``levain/team/entry.py`` ``validate`` at levain ``31dea1f``.

``contracts`` may not import ``team`` (S1_DESIGN §4), and a ``(line, 1)`` verdict must never move when T's schema
moves, so F9 runs this copy, never ``team.entry``. It is the 31dea1f function with one change (S1_DESIGN rev 10):
the future-``ts`` branch reads ``now`` from its caller instead of the wall clock. Ingest passes the ingest time;
verify passes ``None``, which skips that one branch (a C2SP checkpoint carries no time, so a verifier cannot know
when a leaf was ingested; a far-future ``ts`` admitted by a log operator is a known edge, reachable only by the
trusted operator).

Pinned (tests/test_contracts_entry.py): this file's sha256, so an edit is a refusal, not a sentence; its verdicts
(accept, or refuse plus the refusing ``raise`` site, counted in source order) against the verdicts RECORDED in
frozen/entry/ at the freeze, which ``entry.validate`` at 31dea1f produced; and 100% branch coverage of
``validate_entry_v1`` by those vectors. A later T schema change is a new ``(line, v)``, never an edit here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import ContractError

SCHEMA_VERSION = 1
TYPES = ("decision", "constraint", "finding", "question", "tension", "ack", "retire")
KINDS = ("ruling", "practice")
MODES = ("surface", "ask-once", "block")
NEEDS_KIND = ("decision", "constraint")
ALLOWED = {
    "v", "id", "ts", "author", "agent", "session", "type", "kind", "mode", "paths", "owner", "words",
    "summary", "reason", "recheck", "supersedes", "refs", "pack", "rule_id", "prev", "hash",
}
_STR_FIELDS = ("id", "ts", "author", "agent", "session", "type", "kind", "mode", "owner", "words",
               "summary", "reason", "recheck", "pack", "rule_id", "prev", "hash")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}-\d{14}-[0-9a-f]{8}")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
    re.compile(r"(?i)(?<![A-Za-z])[A-Za-z0-9_]*(?:password|passwd|secret|api[_-]?key|token)[A-Za-z0-9_-]*[\"']?\s*[:=]\s*"
               r"[\"']?(?=[^\s\"']*\d)[^\s\"']{8,}"),
]
_SCANNED = ("words", "summary", "reason", "recheck")
_MAX_LEN = {"owner": 200, "words": 4000, "reason": 4000, "summary": 2000, "recheck": 1000}
_MAX_ITEMS = 100
_MAX_PATH = 300
PLAIN_RE = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
_FUTURE_SLACK_S = 86400


class EntryError(ContractError):
    """An entry was refused by the frozen v1 schema."""


def safe_handle(author: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", author).strip("-.") or "x"
    return safe[:64]


def scan_secrets(entry: dict) -> list[str]:
    hits = []
    for key in _SCANNED:
        text = entry.get(key) or ""
        if not isinstance(text, str):
            continue
        for pat in _SECRET_PATTERNS:
            if pat.search(text):
                hits.append(key)
                break
    return hits


def validate_entry_v1(entry: dict, known: dict[str, dict] | None = None, *, scan: bool = True,
                      now: datetime | None) -> None:
    extra = set(entry) - ALLOWED
    if extra:
        raise EntryError(f"unknown field(s): {', '.join(sorted(extra))}")
    if entry.get("v") != SCHEMA_VERSION:
        raise EntryError(f"schema version must be {SCHEMA_VERSION}")
    for f in _STR_FIELDS:
        if f in entry and not isinstance(entry[f], str):
            raise EntryError(f"{f} must be a string")
    for f in ("id", "ts", "author", "type"):
        if not entry.get(f):
            raise EntryError(f"missing required field: {f}")
    if not ID_RE.fullmatch(entry["id"]):
        raise EntryError(f"id {entry['id']!r} is not <author>-<yyyymmddHHMMSS>-<8 hex>")
    if not re.fullmatch(re.escape(safe_handle(entry["author"])) + r"-\d{14}-[0-9a-f]{8}", entry["id"]):
        raise EntryError(f"id {entry['id']!r} does not begin with its author's handle ({safe_handle(entry['author'])}-)")
    if not TS_RE.fullmatch(entry["ts"]):
        raise EntryError("ts must be ISO-8601 UTC, YYYY-MM-DDTHH:MM:SSZ")
    try:
        when = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise EntryError("ts is not a real date") from None
    if now is not None and (when - now).total_seconds() > _FUTURE_SLACK_S:
        raise EntryError("ts is more than a day in the future")
    for f in ("agent", "session"):
        if f in entry and not PLAIN_RE.fullmatch(entry[f]):
            raise EntryError(f"{f} must be a plain handle ([A-Za-z0-9._:@-], at most 128 chars)")
    for f, n in _MAX_LEN.items():
        if len(entry.get(f) or "") > n:
            raise EntryError(f"{f} is longer than {n} characters")
    if entry.get("owner") and not entry["owner"].isprintable():
        raise EntryError("owner must be printable text")
    t = entry["type"]
    if t not in TYPES:
        raise EntryError(f"type must be one of {', '.join(TYPES)}")
    if t in NEEDS_KIND and entry.get("kind") not in KINDS:
        raise EntryError(f"a {t} needs kind: ruling or practice")
    if entry.get("kind") is not None and entry["kind"] not in KINDS:
        raise EntryError("kind must be ruling or practice")
    if entry.get("mode") is not None:
        if entry["mode"] not in MODES:
            raise EntryError(f"mode must be one of {', '.join(MODES)}")
        if entry.get("kind") != "ruling":
            raise EntryError("only a ruling carries a mode (a practice is always surfaced, never denied)")
    if entry.get("kind") == "ruling":
        if not (entry.get("owner") or "").strip():
            raise EntryError("a ruling needs an owner (client:<name>, lead, or a member handle)")
        if not (entry.get("words") or "").strip():
            raise EntryError("a ruling needs the decider's own words in 'words'")
    for lf in ("paths", "supersedes", "refs"):
        val = entry.get(lf, [])
        if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
            raise EntryError(f"{lf} must be a list of non-empty strings")
    for lf in ("paths", "supersedes", "refs"):
        if len(entry.get(lf, [])) > _MAX_ITEMS:
            raise EntryError(f"{lf} has more than {_MAX_ITEMS} items")
    if any(len(p) > _MAX_PATH for p in entry.get("paths", [])):
        raise EntryError(f"a path glob is longer than {_MAX_PATH} characters")
    if any(sum(1 for s in p.split("/") if s != "**" and "**" in s) > 6 for p in entry.get("paths", [])):
        raise EntryError("a path glob has more than 6 segments with ** inside them")
    if any(s.count("**") > 1 for p in entry.get("paths", []) for s in p.split("/")):
        raise EntryError("a path segment may contain ** at most once (write `a/**/b/**/c` instead)")
    if t == "retire" and not entry.get("supersedes"):
        raise EntryError("a retire entry must name what it retires in 'supersedes'")
    if t == "ack":
        if not entry.get("refs"):
            raise EntryError("an ack must name the entries it acknowledges in 'refs'")
        if entry.get("supersedes"):
            raise EntryError("an ack may not supersede anything")
    for p in entry.get("paths", []):
        if p.startswith("/") or "\\" in p or ".." in p.split("/"):
            raise EntryError(f"paths are repo-relative globs with '/' separators, got {p!r}")
    if entry["id"] in entry.get("supersedes", []):
        raise EntryError("an entry cannot supersede itself")
    if known is not None:
        missing = [s for s in entry.get("supersedes", []) if s not in known]
        missing += [r for r in entry.get("refs", []) if r not in known]
        if missing:
            raise EntryError(f"supersedes/refs names unknown entr(ies): {', '.join(missing)}")
        for s in entry.get("supersedes", []):
            old = known[s]
            if old.get("type") == "ack":
                raise EntryError(f"{s} is an ack; acks are not superseded")
            if old.get("type") == "retire":
                raise EntryError(f"{s} is a retire; to bring back what it retired, record that entry again")
            if old.get("kind") == "ruling" and not (entry.get("words") or "").strip():
                raise EntryError(
                    f"{s} is a ruling: replacing or retiring it needs the decider's own words in 'words'")
            if old.get("kind") == "ruling" and t != "retire" and entry.get("kind") != "ruling":
                raise EntryError(f"{s} is a ruling: only another ruling (or a retire) may supersede it")
    hits = scan_secrets(entry) if scan else []
    if hits:
        raise EntryError(f"refused: text in {', '.join(hits)} looks like a secret (key/token/password)")
