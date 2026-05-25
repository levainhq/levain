"""Render an accrual timeline from a continuity.md's git history.

Reads four snapshots (week 1 / week 4 / week 12 / now) by date and renders a
single markdown file showing how the practice grew — line counts, section
list with first-appearance markers, per-snapshot deltas.

This is Levain's README proof artifact for "ship the seed that grows a
practice, not the practice." A new operator can read it as: this is what
your week 1 will look like, and this is what month 5 looks like — not as a
finished foreign personality to inherit, but as a trajectory.

Usage:
    python render_timeline.py [--repo PATH] [--file PATH-IN-REPO] [--out PATH]

Defaults to:
    --repo ~/Documents/flow
    --file global/continuity.md
    --out  growth_timeline.md
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class Snapshot:
    label: str                          # "Week 1", "Week 4", ...
    anchor_date: str                    # "2026-01-12"
    commit: str                         # short sha
    full_commit: str                    # full sha
    content: str
    lines: int
    sections: list[tuple[str, str]]     # [(display_name, canonical_key)]


def _git(args: list[str], cwd: Path) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def _commit_at(repo: Path, file_path: str, anchor_date: str) -> str:
    """Return the SHA of the latest commit touching `file_path` on or before
    `anchor_date` (YYYY-MM-DD)."""
    cutoff = f"{anchor_date}T23:59:59"
    out = _git(
        ["log", "-1", "--format=%H", f"--before={cutoff}", "--", file_path],
        cwd=repo,
    ).strip()
    if not out:
        raise RuntimeError(f"No commits touching {file_path} on or before {anchor_date}")
    return out


def _content_at(repo: Path, sha: str, file_path: str) -> str:
    """Return the contents of `file_path` at commit `sha`."""
    return _git(["show", f"{sha}:{file_path}"], cwd=repo)


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# Renames over the 5-month history. Maps display-heading prefix → canonical
# architectural name. Lets the new-marker fire only on genuine architectural
# additions (Proven, Foundation, Cross-Domain), not on renames.
_RENAME_KEY = {
    "Partnership Context": "Partnership",
    "Partnership": "Partnership",
    "Current State": "State",
    "State": "State",
    "Recent Context": "Recent",
    "Recent": "Recent",
    "Top of Mind": "Top of Mind",
    "Action Items": "Actions",
    "Actions": "Actions",
    "Emerging Patterns": "Developing",
    "Developing Knowledge": "Developing",
    "Developing": "Developing",
    "Graduated Recently": "Proven",
    "Proven Knowledge": "Proven",
    "Proven": "Proven",
    "Foundation": "Foundation",
    "Cross-Domain Discoveries": "Cross-Domain",
}


def _section_display(raw: str) -> str:
    """Trim decoration/dates from a section heading for clean display.

    Keeps the architectural name; drops everything after `|` / `—` /
    bold-marker decorations that turn State headers into 800+ char banners.
    Falls back to the raw stripped heading if the split would empty the name.
    """
    name = raw.strip()
    for sep in (" | ", " — ", " - ", "**"):
        if sep in name:
            candidate = name.split(sep, 1)[0].strip()
            if candidate:
                name = candidate
    return name


def _section_key(raw: str) -> str:
    """Canonical name for architectural-identity comparisons across renames."""
    display = _section_display(raw)
    return _RENAME_KEY.get(display, display)


def _strip_fenced_blocks(content: str) -> str:
    """Blank out lines inside triple-backtick fenced code blocks so headings
    inside code samples don't get picked up as architectural sections."""
    out_lines: list[str] = []
    in_fence = False
    for line in content.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out_lines.append("\n")
            continue
        out_lines.append("\n" if in_fence else line)
    return "".join(out_lines)


def _extract_sections(content: str) -> list[tuple[str, str]]:
    """Returns [(display_name, canonical_key), ...] in document order.

    Strips fenced code blocks first so `## ` inside examples doesn't count.
    """
    out: list[tuple[str, str]] = []
    cleaned = _strip_fenced_blocks(content)
    for m in _SECTION_RE.finditer(cleaned):
        raw = m.group(1)
        out.append((_section_display(raw), _section_key(raw)))
    return out


def collect_snapshot(repo: Path, file_path: str, label: str, anchor_date: str) -> Snapshot:
    sha = _commit_at(repo, file_path, anchor_date)
    content = _content_at(repo, sha, file_path)
    sections = _extract_sections(content)
    return Snapshot(
        label=label,
        anchor_date=anchor_date,
        commit=sha[:8],
        full_commit=sha,
        content=content,
        lines=content.count("\n") + (0 if content.endswith("\n") or not content else 1),
        sections=sections,
    )


def render(snapshots: list[Snapshot], file_path: str) -> str:
    """Render the timeline as markdown."""
    first = snapshots[0]
    last = snapshots[-1]
    seen: set[str] = set()
    out: list[str] = []

    # Per-snapshot deltas — the actual shape of the trajectory
    deltas: list[tuple[int, int]] = []
    for i, snap in enumerate(snapshots):
        if i == 0:
            deltas.append((0, 0))
            continue
        prev = snapshots[i - 1]
        prev_keys = {k for _, k in prev.sections}
        new_count = sum(1 for _, k in snap.sections if k not in prev_keys)
        deltas.append((snap.lines - prev.lines, new_count))

    out.append("# Continuity Accrual — A Growth Timeline")
    out.append("")
    out.append(
        f"This is one continuity file ({file_path}) at four points in its life. "
        f"It started at {first.lines} lines and grew to {last.lines} — but the "
        "growth wasn't steady. Between week 1 and week 4 the architecture barely "
        "moved. Between week 4 and week 12 it shifted, hard. Between week 12 and "
        "now it has been stable, with density growing inside the same shape."
    )
    out.append("")
    out.append(
        "**This is the proof under Levain's pitch — ship the seed that grows a "
        "practice, not the practice.** A new operator's week 1 looks like the first "
        "snapshot, not the last. The last snapshot isn't a target. It's evidence "
        "the engine works."
    )
    out.append("")
    out.append("---")
    out.append("")

    for i, snap in enumerate(snapshots):
        out.append(f"## {snap.label} — {snap.anchor_date}")
        out.append("")
        out.append(f"- **Commit:** `{snap.commit}`")
        out.append(f"- **Lines:** {snap.lines}")
        out.append(f"- **Sections:** {len(snap.sections)}")
        if i > 0:
            d_lines, d_sections = deltas[i]
            sign = "+" if d_lines >= 0 else ""
            out.append(
                f"- **Δ from previous:** {sign}{d_lines} lines, "
                f"{'+' if d_sections >= 0 else ''}{d_sections} sections"
            )
        out.append("")
        out.append("**Section list:**")
        out.append("")
        for display, key in snap.sections:
            marker = " *(new)*" if key not in seen else ""
            out.append(f"- `## {display}`{marker}")
            seen.add(key)
        out.append("")
        out.append("---")
        out.append("")

    # Closing — measured, not maximalist
    growth = last.lines - first.lines
    first_keys = {k for _, k in first.sections}
    new_sections = [d for d, k in last.sections if k not in first_keys]
    out.append("## What this shows")
    out.append("")
    out.append(
        f"From week 1 to now: **+{growth} lines, +{len(new_sections)} sections**. "
        "Not steady accrual — one architectural cliff between week 4 and week 12, "
        "bracketed by two long flat stretches. The growth isn't just volume; the "
        "architecture is what compounds. The shape locked in around week 12 and "
        "has held since; what grew inside it was density."
    )
    out.append("")
    out.append(
        "The seed's job is to get you to week 1 cleanly. Everything after is what "
        "your partnership grows."
    )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", type=Path, default=Path.home() / "Documents" / "flow")
    p.add_argument("--file", default="global/continuity.md")
    p.add_argument("--out", type=Path, default=Path("growth_timeline.md"))
    p.add_argument(
        "--snapshots",
        nargs="+",
        default=["Week 1=2026-01-12", "Week 4=2026-01-26", "Week 12=2026-03-23", "Now=2026-05-25"],
        help="LABEL=YYYY-MM-DD pairs",
    )
    args = p.parse_args(argv)

    if not (args.repo / ".git").exists():
        print(f"FAIL: {args.repo} is not a git repo.")
        return 1

    snapshots: list[Snapshot] = []
    for spec in args.snapshots:
        if "=" not in spec:
            print(f"FAIL: snapshot spec '{spec}' must be LABEL=YYYY-MM-DD")
            return 1
        label, anchor = spec.split("=", 1)
        anchor = anchor.strip()
        try:
            date.fromisoformat(anchor)
        except ValueError:
            print(f"FAIL: snapshot anchor '{anchor}' must be ISO date YYYY-MM-DD")
            return 1
        try:
            snap = collect_snapshot(args.repo, args.file, label.strip(), anchor)
        except RuntimeError as e:
            print(f"FAIL: {e}")
            return 1
        snapshots.append(snap)
        print(
            f"  {snap.label:8s}  {snap.anchor_date}  commit={snap.commit}  "
            f"lines={snap.lines:4d}  sections={len(snap.sections)}"
        )

    rendered = render(snapshots, args.file)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"\nWrote {args.out} ({rendered.count(chr(10))+1} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
