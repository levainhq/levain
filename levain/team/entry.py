"""Ledger entries: schema v1, validation, canonical hashing, the per-file hash chain, and the secret scrub.

An entry is one JSON object per line in an append-only file. Each file carries its own SHA-256 chain:
``hash = sha256(prev + canonical_json(entry without "hash"))``, and the first entry's ``prev`` is ``""``.
The chain proves the history of one file was not rewritten after the fact; it does not prove who wrote it
(git's author and the host's access control do that part).

``canonical()`` is a cross-package contract, meant to be recomputed byte for byte by any importer of the
ledger (anneal-memory's team import is the first). ``tests/test_team_golden.py`` pins the bytes; changing the
arguments here breaks every ledger already written.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone

SCHEMA_VERSION = 1
TYPES = ("decision", "constraint", "finding", "question", "tension", "ack", "retire")
KINDS = ("ruling", "practice")
MODES = ("surface", "ask-once", "block")
# decision/constraint must say whether they bind (ruling) or describe a habit (practice).
NEEDS_KIND = ("decision", "constraint")
ALLOWED = {
    "v", "id", "ts", "author", "agent", "session", "type", "kind", "mode", "paths", "owner", "words",
    "summary", "reason", "recheck", "supersedes", "refs", "pack", "rule_id", "prev", "hash",
}
_STR_FIELDS = ("id", "ts", "author", "agent", "session", "type", "kind", "mode", "owner", "words",
               "summary", "reason", "recheck", "pack", "rule_id", "prev", "hash")
HANDLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}-\d{14}-[0-9a-f]{8}")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                         # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),               # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),             # GitHub fine-grained tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),                    # OpenAI/Anthropic-style secret keys
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),     # Stripe keys
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),            # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),  # JWTs
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}"),        # Authorization: Bearer ...
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),    # credentials in a URL
    # KEY=value / "key": "value" for password/secret/api key/token names, including DB_PASSWORD= and
    # client_secret=; the value must contain a digit so prose like "token: authentication" passes.
    re.compile(r"(?i)(?<![A-Za-z])[A-Za-z0-9_]*(?:password|passwd|secret|api[_-]?key|token)[A-Za-z0-9_-]*[\"']?\s*[:=]\s*"
               r"[\"']?(?=[^\s\"']*\d)[^\s\"']{8,}"),
]
_SCANNED = ("words", "summary", "reason", "recheck")
# Limits shared with anneal-memory's team import, so a levain-written entry never fails that import.
_MAX_LEN = {"owner": 200, "words": 4000, "reason": 4000, "summary": 2000, "recheck": 1000}
_MAX_ITEMS = 100
_MAX_PATH = 300
PLAIN_RE = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
_FUTURE_SLACK_S = 86400


class EntryError(ValueError):
    """An entry was refused. Nothing is written when this is raised."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(author: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{safe_handle(author)}-{stamp}-{secrets.token_hex(4)}"


def safe_handle(author: str) -> str:
    """The author as a path segment and id prefix (``pack:name@1.0`` -> ``pack-name-1.0``)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", author).strip("-.") or "x"
    return safe[:64]


def canonical(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_hash(prev: str, entry: dict) -> str:
    return hashlib.sha256((prev + canonical(entry)).encode("utf-8")).hexdigest()


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


def validate(entry: dict, known: dict[str, dict] | None = None, *, scan: bool = True) -> None:
    """Raise EntryError if the entry may not be written.

    ``known`` (id -> entry, every entry already in the ledger) enables the cross-entry checks: a
    ``supersedes`` id must exist, and replacing or retiring a ruling takes a ruling's evidence (words).
    """
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
    if (when - datetime.now(timezone.utc)).total_seconds() > _FUTURE_SLACK_S:
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
        # an ack points at what it acknowledges through refs, NEVER supersedes (that would hide the ruling)
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


def build(author: str, type_: str, *, kind=None, mode=None, paths=None, owner=None, words=None, summary=None,
          reason=None, recheck=None, supersedes=None, refs=None, agent=None, session=None, pack=None,
          rule_id=None) -> dict:
    e = {"v": SCHEMA_VERSION, "id": new_id(author), "ts": now_iso(), "author": author, "type": type_}
    for k, v in (("kind", kind), ("mode", mode), ("owner", owner), ("words", words), ("summary", summary),
                 ("reason", reason), ("recheck", recheck), ("agent", agent), ("session", session),
                 ("pack", pack), ("rule_id", rule_id)):
        if v is not None and v != "":
            e[k] = v
    e["paths"] = list(paths or [])
    e["supersedes"] = list(supersedes or [])
    if refs:
        e["refs"] = list(refs)
    return e


def seal(entry: dict, prev: str) -> dict:
    sealed = dict(entry)
    sealed.pop("hash", None)
    sealed["prev"] = prev
    sealed["hash"] = chain_hash(prev, sealed)
    return sealed


MAX_JSON_DEPTH = 32
MAX_LINE_CHARS = 262_144


class LineError(ValueError):
    """A ledger line that is refused before ``json.loads`` sees it."""


def parse_line(line: str) -> object:
    """``json.loads`` for a ledger line, with the nesting bounded BEFORE the parser runs.

    A forged line nested hundreds of thousands of brackets deep makes ``json.loads`` raise RecursionError on
    CPython (not a JSONDecodeError, so an ordinary handler misses it and one line stops every read of the whole
    ledger) and has run for minutes on Windows. One linear, string-aware scan refuses it first; entries are flat
    objects, so the bound is far above anything levain writes. Raises LineError (a ValueError) for every
    refusal, JSONDecodeError included.
    """
    if len(line) > MAX_LINE_CHARS:
        raise LineError(f"longer than {MAX_LINE_CHARS} characters")
    depth = 0
    in_str = esc = False
    for ch in line:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise LineError(f"nested deeper than {MAX_JSON_DEPTH}")
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise LineError("closes more than it opens")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise LineError(exc.msg) from exc
    except (ValueError, RecursionError) as exc:  # ValueError: CPython's integer-digit limit
        raise LineError(f"{type(exc).__name__}") from exc


_HEX64 = re.compile(r"[0-9a-f]{64}")


def _printable(text: str) -> str:
    """Hostile text made safe to carry in a problem string: no lone surrogate (it cannot be encoded to UTF-8, so
    it would crash the canon write and the print of the very report that names the line), and bounded length. The
    middle is elided, never the tail: a hostile id leads the string and the reason for the problem ends it."""
    text = text.encode("utf-8", "replace").decode("utf-8")
    return text if len(text) <= 300 else text[:150] + "..." + text[-147:]


def verify_lines(lines: list[str]) -> tuple[list[dict], list[str], str]:
    """Walk one file's chain. Returns (entries, problems, last_hash).

    A broken line is reported, never silently skipped. An entry whose own hash does not match is
    reported AND kept out of the returned entries: an edited ruling must not be enforced with words
    nobody recorded. A chain break (``prev`` mismatch with an intact hash) is reported and kept.
    """
    entries, problems, prev = [], [], ""
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = parse_line(line)
        except LineError as exc:
            problems.append(f"line {n}: not JSON ({exc})")
            continue
        if not isinstance(e, dict):
            problems.append(f"line {n}: not a JSON object")
            continue
        if e.get("prev") != prev:
            problems.append(f"line {n} ({e.get('id')}): chain break, prev does not match the line before")
        try:
            actual = chain_hash(e.get("prev", "") if isinstance(e.get("prev"), str) else "", e)
        except Exception:  # noqa: BLE001 - a hostile line (a lone surrogate) must be one problem, never a failed read
            # A line that cannot even be hashed takes no part in the chain: it must not move the cursor its
            # neighbours are checked against.
            problems.append(f"line {n} ({e.get('id')}): cannot be hashed, the entry is not valid text")
            continue
        if e.get("hash") != actual:
            problems.append(f"line {n} ({e.get('id')}): hash mismatch, the entry was edited after it was written")
            claimed = e.get("hash")
            if isinstance(claimed, str) and _HEX64.fullmatch(claimed):
                prev = claimed  # a claimed hash that is not a digest never becomes the cursor the next write seals over
            continue
        try:
            # The secret scrub is a WRITE gate. Re-running it on read would silently stop enforcing an existing
            # ruling whenever a newer levain widened the patterns.
            validate(e, scan=False)
        except Exception as exc:  # noqa: BLE001 - EntryError is the expected one; a hostile line never fails the read
            why = str(exc) if isinstance(exc, EntryError) else f"{type(exc).__name__}: {exc}"
            problems.append(f"line {n} ({e.get('id')}): invalid entry ({why})")
            prev = e["hash"]
            continue
        prev = e["hash"]
        entries.append(e)
    return entries, [_printable(p) for p in problems], prev
