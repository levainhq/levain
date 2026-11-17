"""The doctor's `seat profile` row."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from levain import doctor


@pytest.fixture(autouse=True)
def _home(levain_home_outside_temp):
    """The row builds dry against the real levain home (L3 wire2 r2 codex H1): one outside every implicit temp root,
    on Linux CI too, where pytest's per-test home is under /tmp."""
    return levain_home_outside_temp


def test_row_refuses_a_levain_home_under_tmpdir_as_a_launch_would(tmp_path, monkeypatch):
    """L3 wire2 r2 codex H1. Revert: the row building without ``budget_home=`` (its budget is then the canary
    prefix's own home under $TMPDIR, which refuses on every machine) or the unconditional implicit-root check."""
    monkeypatch.setenv("TMPDIR", os.path.realpath(tmp_path))
    monkeypatch.setenv("LEVAIN_HOME", str(Path(os.path.realpath(tmp_path)) / "home"))
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("x")
    (row,) = doctor._check_seat_profile(claude_bin=str(claude), python=sys.executable, managed=[])
    assert not row.ok and "read budget directory" in row.detail and "temp directory" in row.detail   # detail: 400 chars


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
    assert not [p for p in Path(os.environ["LEVAIN_HOME"]).rglob("*") if p.name == "read_budget"]
    assert not (Path(os.path.realpath(tmp_path)) / "levain-seat-canary" / "levain-home").exists()
