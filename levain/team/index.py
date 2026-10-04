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


def matches(glob: str, relpath: str) -> bool:
    return bool(glob_regex(glob).match(relpath))


@dataclass
class LedgerFile:
    rel: str                       # path under ledger/, e.g. "ana/3f2a9c1d.jsonl"
    raw: list[dict]                # every line that parsed as a JSON object, file order (export)
    entries: list[dict]            # the lines that verified
    last_hash: str


@dataclass
class Ledger:
    entries: list[dict]            # every verified entry, sorted by (ts, id)
    problems: list[str]            # chain breaks, bad lines: reported, never hidden
    files: list[LedgerFile] = field(default_factory=list)

    @functools.cached_property
    def by_id(self) -> dict[str, dict]:
        return {e["id"]: e for e in self.entries}

    @functools.cached_property
    def superseded(self) -> set[str]:
        out: set[str] = set()
        for e in self.entries:
            if e.get("type") != "ack":
                out.update(e.get("supersedes", []))
        return out

    @functools.cached_property
    def in_force(self) -> list[dict]:
        return [e for e in self.entries if e.get("type") in ANCHORABLE and e["id"] not in self.superseded]

    def acked(self, session: str) -> set[str]:
        out: set[str] = set()
        for e in self.entries:
            if e.get("type") == "ack" and e.get("session") == session:
                out.update(e.get("refs", []))
        return out

    def applying_to(self, relpath: str) -> list[dict]:
        return [e for e in self.in_force if any(matches(g, relpath) for g in e.get("paths", []))]

    def pack_rules(self, pack: str) -> dict[str, dict]:
        """rule_id -> the in-force entry a pack seeded for it."""
        return {e["rule_id"]: e for e in self.in_force
                if str(e.get("pack", "")).split("@")[0] == pack and e.get("rule_id")}


def load_dir(ledger_dir: Path) -> Ledger:
    entries, problems, files = [], [], []
    if not ledger_dir.is_dir():
        return Ledger([], [], [])
    for f in sorted(ledger_dir.rglob("*.jsonl")):
        rel = f.relative_to(ledger_dir).as_posix()
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: unreadable ({exc})")
            continue
        got, probs, last = E.verify_lines(lines)
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
    return Ledger(uniq, problems, files)


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
