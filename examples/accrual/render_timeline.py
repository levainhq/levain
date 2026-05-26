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


def _first_commit_date(repo: Path, file_path: str) -> date:
    """Date of the commit that first added `file_path` to the repo.

    Uses `--follow` to chase renames, so a file moved from path A to path B
    reports A's original add date. For a file that was deleted then later
    re-added at the same path, this returns the OLDEST add date (full
    lineage), not the most recent re-add — appropriate for an accrual
    demo whose point is showing the long-lived practice trajectory. If
    "current incarnation only" semantics are needed instead, use out[0].
    """
    out = _git(
        [
            "log",
            "--diff-filter=A",
            "--follow",
            "--format=%ad",
            "--date=short",
            "--",
            file_path,
        ],
        cwd=repo,
    ).strip().splitlines()
    if not out:
        raise RuntimeError(
            f"git log found no add-commit for {file_path} — does it exist in this repo?"
        )
    # `--follow` may list multiple add events across renames; take the OLDEST,
    # which is the bottom-most line in default reverse-chronological order.
    return date.fromisoformat(out[-1])


def _auto_snapshots(repo: Path, file_path: str) -> list[str]:
    """Derive sensible default snapshot anchors from the file's git history.

    Returns four `LABEL=YYYY-MM-DD` strings: birth, ~25% in, ~75% in, today.
    For repos with only a few days of history, the intermediate anchors may
    collapse to the same date — _commit_at handles that fine (returns the
    same commit at each).
    """
    first = _first_commit_date(repo, file_path)
    now = date.today()
    span = (now - first).days
    if span <= 0:
        return [
            f"Birth={first.isoformat()}",
            f"Now={now.isoformat()}",
        ]
    early = first + (now - first) / 4
    late = first + ((now - first) * 3) / 4
    return [
        f"Birth={first.isoformat()}",
        f"Early={early.isoformat()}",
        f"Late={late.isoformat()}",
        f"Now={now.isoformat()}",
    ]


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# Renames over the 5-month history. Maps display-heading prefix → canonical
# architectural name. Lets the new-marker fire only on genuine architectural
# additions (Proven, Foundation, Cross-Domain), not on renames. Lookups are
# case-insensitive — operators have used both `## Partnership Context` and
# the rare lowercase `## partnership context` in git history; both must
# canonicalize the same way.
_RENAME_KEY = {
    "partnership context": "Partnership",
    "partnership": "Partnership",
    "current state": "State",
    "state": "State",
    "recent context": "Recent",
    "recent": "Recent",
    "top of mind": "Top of Mind",
    "action items": "Actions",
    "actions": "Actions",
    "emerging patterns": "Developing",
    "developing knowledge": "Developing",
    "developing": "Developing",
    "graduated recently": "Proven",
    "proven knowledge": "Proven",
    "proven": "Proven",
    "foundation": "Foundation",
    "cross-domain discoveries": "Cross-Domain",
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
    """Canonical name for architectural-identity comparisons across renames.

    Lookup is case-insensitive (keys are lowercased); unrecognized headings
    pass through preserving their original case for display use.
    """
    display = _section_display(raw)
    return _RENAME_KEY.get(display.lower(), display)


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
    try:
        content = _content_at(repo, sha, file_path)
    except RuntimeError as e:
        # The anchor date can land on a commit where the file was DELETED
        # (later re-added) — `git show <sha>:<path>` then fatals. Don't let
        # that crash the whole demo; report a friendlier error explaining
        # what to do.
        raise RuntimeError(
            f"Anchor {anchor_date} for {label!r} resolves to commit {sha[:8]} "
            f"where {file_path} does not exist (likely a deleted-and-re-added "
            f"gap in the file's history). Pass --snapshots LABEL=YYYY-MM-DD "
            f"explicitly to skip past the gap."
        ) from e
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

    # Build a data-driven opener describing the actual shape of the trajectory
    # this run measured — flow's history was one-cliff-bracketed-by-flats, but
    # other operators' histories will have different shapes (steady accrual,
    # multiple cliffs, gentle ramp). Generate the prose from the numbers.
    trajectory_prose = _describe_trajectory(snapshots, deltas)

    out.append("# Continuity Accrual — A Growth Timeline")
    out.append("")
    out.append(
        f"This is one continuity file ({file_path}) at "
        f"{len(snapshots)} points in its life. "
        f"It started at {first.lines} lines and grew to {last.lines}. "
        f"{trajectory_prose}"
    )
    out.append("")
    out.append(
        "**This is the proof under Levain's pitch — ship the seed that grows a "
        "practice, not the practice.** A new operator's first snapshot looks "
        f"like {first.label}, not {last.label}. The last snapshot isn't a "
        "target. It's evidence the engine works."
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
    closing_prose = _describe_shape(deltas, len(new_sections))
    out.append("## What this shows")
    out.append("")
    out.append(
        f"From {first.label} to {last.label}: "
        f"**{growth:+d} lines, +{len(new_sections)} sections**. "
        f"{closing_prose}"
    )
    out.append("")
    out.append(
        "The seed's job is to get you to your first snapshot cleanly. "
        "Everything after is what your partnership grows."
    )
    out.append("")
    return "\n".join(out)


def _describe_trajectory(snapshots: list[Snapshot], deltas: list[tuple[int, int]]) -> str:
    """Generate the opener prose from the actual per-snapshot deltas.

    Different histories produce different narratives — flow's was
    flat → cliff → flat. A 5-day-old repo will be near-flat throughout.
    A startup-phase project will show steady linear ramp. Pick the
    description that matches the data.
    """
    if len(snapshots) < 2:
        return "Too few snapshots to describe a trajectory."
    line_deltas = [d for d, _ in deltas[1:]]
    total = sum(abs(d) for d in line_deltas) or 1
    biggest = max(line_deltas, key=abs)
    biggest_idx = line_deltas.index(biggest)
    biggest_share = abs(biggest) / total
    if biggest_share >= 0.55 and len(line_deltas) >= 3:
        # One delta dominates → cliff-shaped trajectory
        cliff_from = snapshots[biggest_idx].label
        cliff_to = snapshots[biggest_idx + 1].label
        return (
            f"The growth wasn't steady — most of it happened between "
            f"{cliff_from} and {cliff_to} ({biggest:+d} lines in that one "
            "interval). The rest of the timeline is flatter."
        )
    if biggest_share >= 0.4:
        return (
            f"The biggest jump was between {snapshots[biggest_idx].label} and "
            f"{snapshots[biggest_idx + 1].label} ({biggest:+d} lines), with "
            "smaller changes across the other intervals."
        )
    return (
        "Growth was distributed roughly evenly across the timeline — no "
        "single cliff, no long flats."
    )


def _describe_shape(deltas: list[tuple[int, int]], total_new_sections: int) -> str:
    """Closing prose: what the trajectory shape means for the seed claim."""
    section_jumps = [s for _, s in deltas[1:] if s > 0]
    line_deltas = [d for d, _ in deltas[1:]]
    if total_new_sections == 0:
        return (
            "The shape held across the window. Density grew inside the same "
            "architecture, which is what most of partnership-cognition's "
            "compounding actually looks like."
        )
    if len(section_jumps) == 1:
        return (
            "Sections appeared at one architectural moment, then stabilized. "
            "That's the seed-grows-a-practice claim in one shape: the engine "
            "produces a structure when the practice needs it."
        )
    if any(d < 0 for d in line_deltas):
        return (
            "Note the negative deltas — the file actually shrank at points. "
            "That's wrap-discipline compressing density without losing meaning. "
            "Growth here isn't volume; it's how much you can keep in scope."
        )
    return (
        "Architecture grew with use — sections appeared when the practice "
        "needed them. The seed didn't predict the shape; running it produced it."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", type=Path, default=Path.home() / "Documents" / "flow")
    p.add_argument("--file", default="global/continuity.md")
    p.add_argument("--out", type=Path, default=Path("growth_timeline.md"))
    p.add_argument(
        "--snapshots",
        nargs="+",
        default=None,
        help=(
            "LABEL=YYYY-MM-DD pairs. When omitted, defaults derive from the "
            "file's own git history: Birth (first-add date) / Early (~25%% in) / "
            "Late (~75%% in) / Now. Override with explicit dates if you want "
            "specific anchors."
        ),
    )
    args = p.parse_args(argv)

    if not (args.repo / ".git").exists():
        print(f"FAIL: {args.repo} is not a git repo.")
        return 1

    snapshot_specs = args.snapshots
    if snapshot_specs is None:
        try:
            snapshot_specs = _auto_snapshots(args.repo, args.file)
        except RuntimeError as e:
            print(f"FAIL: could not derive default snapshots: {e}")
            print(f"      Pass --snapshots LABEL=YYYY-MM-DD ... explicitly.")
            return 1
        print(f"  Auto-detected snapshots: {' | '.join(snapshot_specs)}\n")

    snapshots: list[Snapshot] = []
    for spec in snapshot_specs:
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
        except (OSError, UnicodeDecodeError) as e:
            # Broader filesystem / decode failures: bad --repo path, missing
            # --file, non-UTF-8 content at the checked-out blob. Surface a
            # clean error instead of a raw traceback.
            print(f"FAIL: could not read snapshot for {label.strip()!r}: {e}")
            return 1
        snapshots.append(snap)
        print(
            f"  {snap.label:8s}  {snap.anchor_date}  commit={snap.commit}  "
            f"lines={snap.lines:4d}  sections={len(snap.sections)}"
        )

    rendered = render(snapshots, args.file)
    try:
        args.out.write_text(rendered, encoding="utf-8")
    except OSError as e:
        print(f"FAIL: could not write output to {args.out}: {e}")
        return 1
    print(f"\nWrote {args.out} ({rendered.count(chr(10))+1} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
