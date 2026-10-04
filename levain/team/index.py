"""The in-force view of a ledger, derived from its files every time (never stored as truth).

In force = every anchorable entry that no later entry supersedes. An ``ack`` never supersedes; a
``retire`` only supersedes. Globs are repo-relative: ``billing.py`` names the file at the repo root,
``**/billing.py`` names it anywhere.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import entry as E

ANCHORABLE = ("decision", "constraint", "finding", "question", "tension")


@functools.lru_cache(maxsize=512)
def glob_regex(glob: str) -> re.Pattern:
    """``**`` crosses directories, ``*`` and ``?`` do not; a trailing ``/`` means the whole tree."""
    g = glob.strip()
    while g.startswith("./"):
        g = g[2:]
    if g.endswith("/"):
        g += "**"
    out, i = [], 0
    while i < len(g):
        c = g[i]
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


@functools.lru_cache(maxsize=512)
def _folded(glob: str) -> re.Pattern:
    return re.compile(glob_regex(glob).pattern, re.IGNORECASE)


def matches(glob: str, relpath: str, *, fold: bool = False) -> bool:
    """``fold``: match case-insensitively, for case-insensitive filesystems (macOS default), where
    ``src/BILLING.PY`` and ``src/billing.py`` are one file."""
    return bool((_folded(glob) if fold else glob_regex(glob)).match(relpath))


@dataclass
class LedgerFile:
    rel: str                       # path under ledger/, e.g. "ana/3f2a9c1d.jsonl"
    raw: list[dict]                # every line that parsed as a JSON object, file order (export)
    entries: list[dict]            # the lines that verified
    last_hash: str


def may_link(linker: str, target_author: str, owner: str | None) -> bool:
    """May ``linker`` supersede or retire an entry written by ``target_author``?

    The same author always may (a pack re-seed is author ``pack:<name>`` superseding itself). Across authors,
    only the team's canon owner may; otherwise any member could silence a client ruling with one line.
    """
    return linker == target_author or (owner is not None and linker == owner)


@dataclass
class Ledger:
    entries: list[dict]            # every verified entry, sorted by (ts, id)
    file_problems: list[str]       # chain breaks, bad lines, misfiled entries: reported, never hidden
    files: list[LedgerFile] = field(default_factory=list)
    owner: str | None = None       # team.toml owner: the one author whose links cross authors

    @functools.cached_property
    def by_id(self) -> dict[str, dict]:
        return {e["id"]: e for e in self.entries}

    @functools.cached_property
    def _links(self) -> tuple[set[str], list[str]]:
        honoured: set[str] = set()
        refused: list[str] = []
        for e in self.entries:
            if e.get("type") == "ack":
                continue
            for s in e.get("supersedes", []):
                target = self.by_id.get(s)
                if target is None:
                    refused.append(f"{e['id']} supersedes {s}, which is not in the ledger (ignored)")
                elif may_link(e.get("author", ""), target.get("author", ""), self.owner):
                    honoured.add(s)
                else:
                    refused.append(f"{e['id']} by {e.get('author')} supersedes {s} by {target.get('author')}: "
                                   f"only the same author or the owner ({self.owner}) may; ignored, {s} stays in force")
        return honoured, refused

    @property
    def superseded(self) -> set[str]:
        return self._links[0]

    @property
    def problems(self) -> list[str]:
        return self.file_problems + self._links[1]

    @functools.cached_property
    def in_force(self) -> list[dict]:
        return [e for e in self.entries if e.get("type") in ANCHORABLE and e["id"] not in self.superseded]

    def acked(self, session: str) -> set[str]:
        out: set[str] = set()
        for e in self.entries:
            if e.get("type") == "ack" and e.get("session") == session:
                out.update(e.get("refs", []))
        return out

    def applying_to(self, relpath: str, *, fold: bool = False) -> list[dict]:
        return [e for e in self.in_force if any(matches(g, relpath, fold=fold) for g in e.get("paths", []))]

    def retired_by_others(self, pack: str) -> dict[str, dict]:
        """rule_id -> the pack's last entry for it, where someone other than the pack retired or replaced it."""
        author = f"pack:{pack}"
        last: dict[str, dict] = {}
        for e in self.entries:
            if e.get("author") == author and e.get("rule_id") and e.get("type") != "retire":
                last[e["rule_id"]] = e
        out = {}
        for rid, e in last.items():
            if e["id"] in self.superseded and any(
                    e["id"] in o.get("supersedes", []) and o.get("author") != author for o in self.entries):
                out[rid] = e
        return out

    def pack_rules(self, pack: str) -> dict[str, dict]:
        """rule_id -> the in-force entry a pack seeded for it."""
        return {e["rule_id"]: e for e in self.in_force
                if str(e.get("pack", "")).split("@")[0] == pack and e.get("rule_id")}


def load_dir(ledger_dir: Path, owner: str | None = None) -> Ledger:
    entries, problems, files = [], [], []
    if not ledger_dir.is_dir():
        return Ledger([], [], [], owner)
    for f in sorted(ledger_dir.rglob("*.jsonl")):
        rel = f.relative_to(ledger_dir).as_posix()
        try:
            # split on "\n" only: str.splitlines() also splits on U+2028/U+2029/U+0085, which JSON written with
            # ensure_ascii=False carries unescaped inside a string
            lines = f.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: unreadable ({exc})")
            continue
        got, probs, last = E.verify_lines(lines)
        owner_dir = rel.split("/", 1)[0]
        misfiled = [e for e in got if E.safe_handle(e.get("author", "")) != owner_dir]
        probs += [f"{e['id']}: author {e.get('author')!r} is filed under {owner_dir}/ (not enforced)" for e in misfiled]
        got = [e for e in got if e not in misfiled]
        raw = []
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                raw.append(obj)
        files.append(LedgerFile(rel, raw, got, last))
        problems += [f"{rel}: {p}" for p in probs]
        entries += got
    seen, uniq = set(), []
    for e in entries:
        if e["id"] in seen:
            problems.append(f"duplicate id {e['id']} (kept the first)")
            continue
        seen.add(e["id"])
        uniq.append(e)
    uniq.sort(key=lambda e: (e.get("ts", ""), e.get("id", "")))
    return Ledger(uniq, problems, files, owner)


def age(ts: str, now: datetime | None = None) -> str:
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "age unknown"
    d = (now or datetime.now(timezone.utc)) - then
    if d.days >= 1:
        return f"{d.days}d ago"
    if d.total_seconds() < 60:
        return "just now"
    h = d.seconds // 3600
    return f"{h}h ago" if h else f"{max(1, d.seconds // 60)}m ago"


def render(e: dict) -> str:
    """One entry as an agent or a person reads it: the decider's words first, then who owns the call."""
    label = f"{e.get('type')} · {e['kind']}" if e.get("kind") else str(e.get("type"))
    bits = [f"[{label}] {e['id']}"]
    if e.get("words"):
        bits.append(f'  words: "{e["words"]}"')
    if e.get("owner"):
        bits.append(f"  owner of the call: {e['owner']}")
    src = f"pack {e['pack']}" if e.get("pack") else (e.get("agent") or "human")
    bits.append(f"  recorded by {e.get('author')} ({src}), {age(e.get('ts', ''))}")
    if e.get("summary"):
        bits.append(f"  summary by {e.get('author')}: {e['summary']}")
    if e.get("reason"):
        bits.append(f"  why: {e['reason']}")
    if e.get("paths"):
        bits.append(f"  governs: {', '.join(e['paths'])}")
    if e.get("recheck"):
        bits.append(f"  re-check: {e['recheck']}")
    return "\n".join(bits)
