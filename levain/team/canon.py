"""PROJECT.md: the generated canon. One owner writes it, on a clock; the ledger stays the source of truth.

Every structured line is generated from in-force entries and carries the entry id, owner, author and date,
so the canon can always be traced back to the record. The header names the ledger tree it was built from
and how many entries that tree held; ``staleness()`` compares that with the ledger now.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import index as I
from . import roles as R

_HEADER_RE = re.compile(r"generated (\S+) by (\S+) from ledger (\S+) \((\d+) entries\)")


def _count(ledger: I.Ledger) -> int:
    """Entries that can change the canon. Acknowledgements are excluded: every agent's retry writes one."""
    return sum(1 for e in ledger.entries if e.get("type") != "ack")


def _entry_block(e: dict, team: R.Team) -> list[str]:
    meta = [e["id"]]
    if e.get("owner"):
        meta.append(f"owner {I.oneline(e['owner'])}")
    if e.get("kind") == "ruling":
        meta.append(f"mode {e.get('mode') or team.mode}")
    src = f"pack {e['pack']}" if e.get("pack") else e.get("author", "?")
    meta.append(f"recorded by {src} {e.get('ts', '')[:10]}")
    out = [f"- {' · '.join(meta)}"]
    if e.get("words"):
        out.append(f'  > "{I.oneline(e["words"])}"')
    if e.get("summary"):
        out.append(f"  summary by {e.get('author')}: {I.oneline(e['summary'])}")
    if e.get("reason"):
        out.append(f"  why: {I.oneline(e['reason'])}")
    if e.get("recheck"):
        out.append(f"  re-check: `{I.oneline(e['recheck'])}`")
    return out


def render(team: R.Team, ledger: I.Ledger, *, tree: str, by: str, ts: str) -> str:
    live = ledger.in_force
    rulings = [e for e in live if e.get("kind") == "ruling"]
    by_path: dict[str, list[dict]] = defaultdict(list)
    wide: list[dict] = []
    for e in rulings:
        if e.get("paths"):
            for g in e["paths"]:
                by_path[g].append(e)
        else:
            wide.append(e)
    lines = [
        f"# {team.project}: project canon",
        "",
        f"generated {ts} by {by} from ledger {tree} ({_count(ledger)} entries); do not hand-edit.",
        f"The ledger (branch `levain-ledger`, `ledger/`) is the source of truth; this file is derived from it.",
        f"Owner: {team.owner} · members: {', '.join(team.members)} · default mode: {team.mode}",
        "",
    ]

    def section(title: str, items: list[dict], note: str = "") -> None:
        if not items:
            return
        lines.append(f"## {title}")
        if note:
            lines.append(note)
        lines.append("")
        for e in items:
            lines.extend(_entry_block(e, team))
        lines.append("")

    if by_path:
        lines.append("## Rulings in force, by path")
        lines.append("An agent editing a matching path is shown the ruling (and, by mode, stopped until it asks).")
        lines.append("")
        for g in sorted(by_path):
            lines.append(f"### `{I.oneline(g)}`")
            for e in by_path[g]:
                lines.extend(_entry_block(e, team))
            lines.append("")
    section("Project-wide rulings", wide, "No path: these reach agents through this file, not at the edit.")
    section("Tensions (an edit on these paths always stops: ask the owners)",
            [e for e in live if e.get("type") == "tension"])
    section("Open questions", [e for e in live if e.get("type") == "question"])
    section("Practices (how we usually do it; may change freely)",
            [e for e in live if e.get("kind") == "practice"])
    section("Findings", [e for e in live if e.get("type") == "finding"])
    if ledger.problems:
        lines.append("## Ledger integrity problems at generation")
        lines.extend(f"- {I.oneline(p)}" for p in ledger.problems)
        lines.append("")
    if not any(l.startswith("## ") for l in lines):
        lines.append("Nothing is in force yet.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def header(text: str | None) -> dict | None:
    if not text:
        return None
    m = _HEADER_RE.search(text)
    if not m:
        return None
    return {"ts": m.group(1), "by": m.group(2), "tree": m.group(3), "entries": int(m.group(4))}


def staleness(text: str | None, state: str) -> str:
    """One line: was the canon generated from the ledger and team.toml as they are now?"""
    h = header(text)
    if h is None:
        return "no canon yet (the owner runs `levain team consolidate`)"
    if h["tree"] == state:
        return f"canon current (generated {h['ts']} by {h['by']})"
    return f"canon is behind the ledger (generated {h['ts']} by {h['by']}; the owner re-runs `levain team consolidate`)"
