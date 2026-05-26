"""Tests for the accrual demo script (examples/accrual/render_timeline.py).

The accrual demo is a standalone script in examples/, not part of the
installed package. We import it via sys.path manipulation so the unit
tests still ship in `tests/` and run under `pytest`. Worth covering
because Phase B's L1 review caught a real-history bug (`_RENAME_KEY`
missing `Action Items` / `Developing Knowledge` / `Proven Knowledge`)
and the section-display/key/fence-strip logic is load-bearing for the
proof artifact's correctness.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# Import the accrual script directly from examples/. The repo root is
# the parent of tests/ (this file's directory).
ACCRUAL_DIR = Path(__file__).resolve().parent.parent / "examples" / "accrual"
sys.path.insert(0, str(ACCRUAL_DIR))

import render_timeline as accrual  # noqa: E402


# ---------- _section_display ----------

def test_section_display_strips_after_pipe():
    assert accrual._section_display("State | banner text") == "State"


def test_section_display_strips_after_em_dash():
    assert accrual._section_display("State — banner text") == "State"


def test_section_display_strips_bold_decoration():
    # `**Important Stuff**` should not collapse to empty — fallback to raw.
    assert accrual._section_display("**Important Section**") == "**Important Section**"


def test_section_display_preserves_plain_heading():
    assert accrual._section_display("Top of Mind") == "Top of Mind"


def test_section_display_handles_long_state_banner():
    """State headers in flow's continuity sometimes span 800+ chars; should
    trim cleanly at the first delimiter."""
    raw = "State | Mon May 25 — **SEVEN-MODE DAY** + lots of content..."
    assert accrual._section_display(raw) == "State"


# ---------- _section_key (RENAME_KEY canonicalization) ----------

def test_section_key_canonicalizes_renames():
    """The L1 H1 catch: intermediate names existed in mid-history that the
    default RENAME_KEY had to grow to cover."""
    # Original week-1 names
    assert accrual._section_key("Partnership Context") == "Partnership"
    assert accrual._section_key("Current State") == "State"
    assert accrual._section_key("Recent Context") == "Recent"
    assert accrual._section_key("Emerging Patterns") == "Developing"
    assert accrual._section_key("Graduated Recently") == "Proven"
    # Intermediate names (the L1-caught gap)
    assert accrual._section_key("Action Items") == "Actions"
    assert accrual._section_key("Developing Knowledge") == "Developing"
    assert accrual._section_key("Proven Knowledge") == "Proven"
    # Final names
    assert accrual._section_key("Partnership") == "Partnership"
    assert accrual._section_key("State") == "State"
    assert accrual._section_key("Cross-Domain Discoveries") == "Cross-Domain"


def test_section_key_unknown_section_passes_through():
    """Unrecognized section names should round-trip through the display
    normalizer but not be coerced to anything else."""
    assert accrual._section_key("Some Future Section") == "Some Future Section"


# ---------- _auto_snapshots ----------

def test_auto_snapshots_returns_four_label_date_pairs(tmp_path: Path):
    """Init a tiny git repo with a file and verify _auto_snapshots returns
    well-formed LABEL=YYYY-MM-DD strings."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    target = tmp_path / "log.md"
    target.write_text("initial", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "log.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "add"], check=True)

    specs = accrual._auto_snapshots(tmp_path, "log.md")
    # 2-anchor minimum (Birth + Now) when span is small; 4 otherwise.
    assert len(specs) in (2, 4)
    for spec in specs:
        assert "=" in spec
        label, date_str = spec.split("=", 1)
        # Both halves should be non-empty and the date should parse.
        from datetime import date
        assert label and date.fromisoformat(date_str)


def test_first_commit_date_raises_for_missing_file(tmp_path: Path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    # Empty repo, no commits — _first_commit_date should raise.
    with pytest.raises((RuntimeError, Exception)):
        accrual._first_commit_date(tmp_path, "nonexistent.md")


def test_section_key_canonicalizes_case_insensitively():
    """Lowercase / mixed-case headings still canonicalize to the same key
    as their canonical-case counterpart."""
    assert accrual._section_key("partnership context") == "Partnership"
    assert accrual._section_key("PARTNERSHIP CONTEXT") == "Partnership"
    assert accrual._section_key("Partnership Context") == "Partnership"
    assert accrual._section_key("DEVELOPING knowledge") == "Developing"
    assert accrual._section_key("cross-domain discoveries") == "Cross-Domain"
    # Unknown sections preserve their original case.
    assert accrual._section_key("some Custom Section") == "some Custom Section"


# ---------- _strip_fenced_blocks ----------

def test_strip_fenced_blocks_removes_inner_headings():
    content = (
        "## Real Section\n"
        "some text\n"
        "```markdown\n"
        "## Fake Inside Fence\n"
        "```\n"
        "## Another Real\n"
    )
    cleaned = accrual._strip_fenced_blocks(content)
    # The fake heading should be gone after fence-stripping
    assert "Fake Inside Fence" not in cleaned
    # The real headings should remain
    assert "Real Section" in cleaned
    assert "Another Real" in cleaned


def test_strip_fenced_blocks_handles_unbalanced_fences():
    """An unclosed fence should not crash — just consume everything past
    the opening fence."""
    content = "## Before\n```\n## Eaten\nmore text\n"
    cleaned = accrual._strip_fenced_blocks(content)
    assert "Before" in cleaned
    assert "Eaten" not in cleaned


def test_strip_fenced_blocks_preserves_non_fenced_content():
    content = "## Heading\nbody text\nmore body\n"
    cleaned = accrual._strip_fenced_blocks(content)
    # All non-fenced lines preserved
    assert "Heading" in cleaned
    assert "body text" in cleaned
    assert "more body" in cleaned


# ---------- _extract_sections ----------

def test_extract_sections_returns_display_and_canonical_pairs():
    content = (
        "## Partnership Context\nintro\n"
        "## Current State\nbody\n"
        "## Emerging Patterns\nbody\n"
    )
    sections = accrual._extract_sections(content)
    # display names preserved as-given; canonical keys map renames
    assert ("Partnership Context", "Partnership") in sections
    assert ("Current State", "State") in sections
    assert ("Emerging Patterns", "Developing") in sections


def test_extract_sections_skips_fenced_headings():
    """Integration of strip_fenced_blocks + _SECTION_RE."""
    content = (
        "## Real One\n"
        "```\n## Fenced\n```\n"
        "## Real Two\n"
    )
    sections = accrual._extract_sections(content)
    section_keys = [s[1] for s in sections]
    assert "Fenced" not in section_keys
    assert any("Real One" in s[0] for s in sections)
    assert any("Real Two" in s[0] for s in sections)
