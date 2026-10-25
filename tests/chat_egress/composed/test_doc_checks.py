"""CH-D2 S9: R15-17-12, a doc check and not code: *"no rev-15.4 OPEN pointer survives in either file"*. The row names the
places the ruled OPEN markers were deleted from: the design's R-3, item 6 and C-4, and the conditions file's rows
R15-12-B6 and R15-15-Q1. Each section is cut by its own heading, must be found and non-empty, and holds no `OPEN`
outside a `⟦...⟧` change note (history prose is allowed to say a thing WAS open).

The files live in the levain project-memory repo (a separate checkout): `CHD2_DOCS_DIR` names the folder holding
DESIGN_REV15.md and CHD2_BUILD_CONDITIONS_REV15.md; the default is the one on this Mac, and the test skips where it is
absent (CI).
"""

from __future__ import annotations

import os
import pwd
import re
from pathlib import Path

import pytest

# the account home, not $HOME: the suite points HOME at a scratch directory
DOCS = Path(os.environ.get("CHD2_DOCS_DIR", Path(pwd.getpwuid(os.getuid()).pw_dir) / "Briefcase/levain/project_memory/1024-6/chd1"))

# (file, a regex for the section's first line, a regex for the line that ends it)
SECTIONS = [
    ("DESIGN_REV15.md", r"^- \*\*R-3 ", r"^- \*\*R-4 "),
    ("DESIGN_REV15.md", r"^- \*\*C-4 ", r"^- \*\*C-5|^## |^### "),
    ("DESIGN_REV15.md", r"^\*\*6\. The read-shaped list", r"^\*\*7\. "),
    ("CHD2_BUILD_CONDITIONS_REV15.md", r"^- \*\*R15-12-B6\*\*", r"^- \*\*R15-12-B7\*\*"),
    ("CHD2_BUILD_CONDITIONS_REV15.md", r"^- \*\*R15-15-Q1\*\*", r"^- \*\*R15-15-Q3\*\*|^- \*\*R15-15-Q2\*\*"),
]


def cut(text: str, start: str, end: str) -> str:
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.search(start, ln):
            for j in range(i + 1, len(lines)):
                if re.search(end, lines[j]):
                    return "\n".join(lines[i:j])
            return "\n".join(lines[i:])
    return ""


def open_markers(section: str) -> list[str]:
    bare = re.sub(r"⟦[^⟧]*⟧", "", section)
    return [m.group(0) for m in re.finditer(r"[^\n]{0,60}\bOPEN\b[^\n]{0,60}", bare)]


def test_the_checker_finds_a_marker_and_ignores_a_change_note() -> None:
    """The control: the check can fail."""
    assert open_markers("a thing\nQ-R15.4-1 is OPEN for the head\n")
    assert open_markers("it was ⟦rev15.5, R15-16: replaces the OPEN pointer⟧ ruled") == []
    assert cut("x\n- **R-3 Push**\nbody\n- **R-4 y**\nz", r"^- \*\*R-3 ", r"^- \*\*R-4 ") == "- **R-3 Push**\nbody"
    assert cut("nothing here", r"^- \*\*R-3 ", r"^- \*\*R-4 ") == ""


@pytest.mark.skipif(not (DOCS / "DESIGN_REV15.md").is_file(), reason=f"{DOCS} is not on this machine (CHD2_DOCS_DIR)")
@pytest.mark.parametrize("name,start,end", SECTIONS, ids=[f"{s[0][:6]}:{s[1][-12:]}" for s in SECTIONS])
def test_no_open_pointer_survives_in_the_named_sections(name, start, end) -> None:
    section = cut((DOCS / name).read_text(), start, end)
    assert len(section) > 200, f"{name}: the section {start!r} was not found"
    assert open_markers(section) == [], open_markers(section)
