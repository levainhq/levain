"""The doctor's `seat profile` row."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from levain import doctor


def test_row_builds(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", os.path.realpath(tmp_path))
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("x")
    (row,) = doctor._check_seat_profile(claude_bin=str(claude), python=sys.executable, managed=[])
    assert row.name == "seat profile" and row.ok and not row.warn
    assert "Bash,Read,Edit,Write" in row.detail
    assert not list((Path(os.path.realpath(tmp_path)) / "levain-seat-canary").iterdir())  # cleaned up


def test_row_no_claude_is_neutral(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: None)
    (row,) = doctor._check_seat_profile()
    assert row.neutral and row.ok


def test_row_fails_on_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", os.path.realpath(tmp_path))
    (row,) = doctor._check_seat_profile(claude_bin="relative/claude", python=sys.executable, managed=[])
    assert not row.ok and "refused" in row.detail


def test_row_builds_dry_and_writes_no_read_budget_directory(tmp_path, monkeypatch):
    """L1 wire2 F3. Revert: ``_check_seat_profile`` building without ``dry_run=True`` (the read budget's install then
    writes ``<canary prefix>/levain-home/read_budget`` on every doctor run)."""
    monkeypatch.setenv("TMPDIR", os.path.realpath(tmp_path))
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("x")
    (row,) = doctor._check_seat_profile(claude_bin=str(claude), python=sys.executable, managed=[])
    assert row.ok and not row.warn, row.detail
    assert not [p for p in Path(os.path.realpath(tmp_path)).rglob("*") if p.name == "read_budget"]
    assert not (Path(os.path.realpath(tmp_path)) / "levain-seat-canary" / "levain-home").exists()
