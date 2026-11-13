"""The room's ``--room DIR`` refusals, spelled in another letter case (roomfix2 K1, FINDINGS_room_r3 LOW).

These tests need no browser. They lived in ``test_cockpit_room_join.py``, whose file names the browser mark, so the CI browser
job (which runs every file naming it, and fails on ANY skip) ran them on Linux, where they skip: a case-sensitive filesystem
has no case alias of a directory, so the property under test (a case alias is refused as the directory it names) has no
instance there. They run, non-vacuously, where an alias exists (macOS APFS by default, a case-insensitive Windows or Linux
volume); on a case-sensitive filesystem they skip with that reason, in the ordinary pytest job, where a skip is allowed."""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A HOME and a LEVAIN_HOME under tmp_path (his real home is never read or written)."""
    home = tmp_path / "home"
    (home / ".levain" / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LEVAIN_HOME", str(home / ".levain"))
    return home


def _case_alias(p):
    """``p`` spelled with its letters' case swapped, when this filesystem takes that spelling as the same directory (a
    case-insensitive one: macOS APFS by default); else the test is skipped."""
    import os
    from pathlib import Path
    alias = Path(str(p.resolve()).swapcase())
    if str(alias) == str(p.resolve()) or not alias.exists() or not os.path.samefile(alias, p):
        pytest.skip("this filesystem is case-sensitive: no case alias of a directory exists")
    return alias


@pytest.mark.parametrize("which, why", [
    ("home", "your home directory"),
    ("above_home", "holds your home directory"),
    ("in_levain", "inside levain's home"),
    ("levain", "holds levain's home"),
])
def test_a_case_alias_of_a_refused_room_dir_is_refused(fake_home, which, why):
    """roomfix2 K1 (L3 r2, all three seats): `Path.resolve()` keeps the spelling it was given, so `--room /USERS/<me>`
    compared unequal to the home it names and the room opened over his whole home. Every side is compared by identity."""
    from levain.web_server import _build_room_serve
    d = {"home": fake_home, "above_home": fake_home.parent, "in_levain": fake_home / ".levain" / "x",
         "levain": fake_home / ".levain"}[which]
    d.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match=why):
        _build_room_serve(_case_alias(d))


def test_a_case_alias_of_a_project_dir_is_served_under_its_own_spelling(fake_home):
    """roomfix2 K1: the deny rules are written from home's real spelling, so the room's cwd is passed on canonical."""
    import os
    from levain.web_server import _build_room_serve
    proj = fake_home / "proj"
    proj.mkdir()
    rs = _build_room_serve(_case_alias(proj))
    try:
        cwd = rs.launcher._setting.cwd
        assert os.path.samefile(cwd, proj) and cwd == str(proj.resolve()), (cwd, str(proj.resolve()))
    finally:
        rs.stop()


def test_a_case_alias_in_home_still_refuses_home_and_writes_the_deny_rules_in_the_real_spelling(fake_home, monkeypatch):
    """roomfix2 K1: HOME itself spelled in another case is the same home: refused as DIR, and the deny rules of a
    project DIR name the real spelling."""
    from levain.web_server import _build_room_serve
    alias = _case_alias(fake_home)
    monkeypatch.setenv("HOME", str(alias))
    with pytest.raises(ValueError, match="your home directory"):
        _build_room_serve(fake_home)
    proj = fake_home / "proj"
    proj.mkdir()
    from levain.desk.room_default import CONFINED
    rs = _build_room_serve(proj, profile=CONFINED)      # the deny list is the confined room's (the opt-in since ROOMFLIP)
    try:
        deny = json.loads(rs.launcher._settings_json)["permissions"]["deny"]
        real = str(fake_home.resolve())
        assert f"Read(/{real}/.ssh)" in deny, deny[:4]
        assert not any(str(alias) in d for d in deny)
    finally:
        rs.stop()


def test_home_matched_by_identity_alone_is_named_as_home(fake_home, monkeypatch):
    """FINDINGS_room_r3 LOW: a DIR that IS home but matched only by (st_dev, st_ino), not by spelling, was told it
    "holds your home directory". The canonical layer is switched off here to reach the identity-only match."""
    from pathlib import Path
    import levain.web_server as ws
    alias = _case_alias(fake_home)
    monkeypatch.setattr(ws, "_canonical_path", lambda p: Path(p).resolve())
    with pytest.raises(ValueError) as exc:
        ws._room_dir_refusal(alias, ws._canonical_path(fake_home / ".levain" / "consent"))
    assert "is your home directory" in str(exc.value) and "holds your home" not in str(exc.value), str(exc.value)
