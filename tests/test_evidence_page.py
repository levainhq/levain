"""The deferral-probe evidence page quotes its analysis script and that script's output; the copies must not drift from
the shipped files, and the label file must match the page's table."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent.parent / "docs" / "evidence"
PAGE = EVIDENCE / "deferral-probe.md"
FOLDER = EVIDENCE / "deferral-probe"


def _blocks(text: str, fence: str) -> list[str]:
    parts = text.split("```" + fence + "\n")[1:]
    return [p.split("```", 1)[0] for p in parts]


def test_page_copies_the_shipped_script() -> None:
    assert _blocks(PAGE.read_text(), "python")[0] == (FOLDER / "analysis.py").read_text()


def test_page_output_is_what_the_script_prints() -> None:
    out = subprocess.run([sys.executable, "-I", "analysis.py"], cwd=FOLDER, capture_output=True, text=True, check=True).stdout
    assert _blocks(PAGE.read_text(), "text")[0] == out


def test_label_file_matches_the_page_table() -> None:
    rows = json.loads((FOLDER / "labels_z.json").read_text())
    table = [ln for ln in PAGE.read_text().splitlines() if ln.startswith("| claude-opus-5") and ln.count("|") == 8]
    assert len(rows) == 60 and len(table) == 60
    for row, line in zip(rows, table):
        assert line == "| " + " | ".join(str(c) for c in row) + " |"
