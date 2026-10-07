"""Browser profiles are crown jewels in the universal floor (head ruling 2026-10-07).

L2 measured the cockpit's launch token on disk in Chrome's Session Storage, unencrypted; a profile also holds every
site's session cookies and saved logins. Every entry of ``BROWSER_PROFILE_DIRS`` is denied to both hands, read and
write, and a profile full of SQLite files does not stop bash from starting (they are directories, not file jewels).
"""
from __future__ import annotations

import platform
from pathlib import Path

import pytest

from levain.firing.confinement import (
    BROWSER_PROFILE_DIRS,
    browser_profile_roots,
    build_policy,
    bwrap_available,
    crown_jewel_reason,
    sandbox_exec_available,
    select_provider,
)

_MAC = platform.system() == "Darwin" and sandbox_exec_available()
_LINUX = platform.system() == "Linux" and bwrap_available()
live = pytest.mark.skipif(not (_MAC or _LINUX), reason="needs macOS sandbox-exec or a working bwrap")
_REFUSALS = ("Operation not permitted", "Read-only file system", "Permission denied",
             "Device or resource busy") + (("No such file or directory",) if _LINUX else ())


def _entity(root: Path) -> Path:
    d = root / "entities" / "coyote"
    (d / ".levain").mkdir(parents=True)
    (d / "workspace").mkdir()
    return d


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _profile(home: Path, rel: str) -> Path:
    """A profile as a browser leaves it: a directory of SQLite and leveldb files."""
    p = home / rel / "Default"
    (p / "Session Storage").mkdir(parents=True)
    (p / "Session Storage" / "000003.log").write_text("levain.token secret-session-storage")
    (p / "Cookies").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    # What made bash refuse to start on this Mac (2026-10-07): Safari's WebKit cache hardlinks its own records, and
    # its container refuses a directory listing. A profile is denied by path, never walked for other names.
    (p / "Cache").mkdir()
    (p / "Cache" / "record").write_text("cached")
    (p / "Cache" / "record-blob").hardlink_to(p / "Cache" / "record")
    (p / "Private").mkdir(mode=0o000)
    return p


def test_a_hardlink_to_a_profiles_page_storage_refuses_bash(home: Path) -> None:
    """codex L3: profiles are not walked whole, but their Session/Local Storage is, so another name for the file the
    cockpit's token lives in (say, in the entity's workspace) is caught at shell start."""
    from levain.firing.confinement import ConfinementError, _refuse_multiply_linked_jewels

    rel = "Library/Application Support/Google/Chrome" if platform.system() == "Darwin" else ".config/google-chrome"
    prof = _profile(home, rel)
    entity = _entity(home)
    (entity / "workspace" / "alias.log").hardlink_to(prof / "Session Storage" / "000003.log")
    try:
        with pytest.raises(ConfinementError, match="names on disk"):
            _refuse_multiply_linked_jewels(build_policy(entity))
    finally:
        (prof / "Private").chmod(0o700)


def test_every_browser_profile_root_is_denied_to_the_file_editor(home: Path) -> None:
    mine = "darwin" if platform.system() == "Darwin" else "linux"
    for os_name, rel in BROWSER_PROFILE_DIRS:   # present, so Linux denies them too
        if os_name == mine:
            (home / rel).mkdir(parents=True, exist_ok=True)
    policy = build_policy(_entity(home))
    for os_name, rel in BROWSER_PROFILE_DIRS:
        if os_name != mine:
            continue
        root = (home / rel).resolve()
        assert root in policy.deny_read_write, rel
        assert crown_jewel_reason(policy, home / rel / "Default" / "Cookies") is not None, rel


@pytest.mark.skipif(platform.system() == "Darwin", reason="Linux-only rule")
def test_on_linux_an_absent_browser_is_not_denied_so_bwrap_does_not_create_it(home: Path) -> None:
    """codex L3: bwrap creates an absent denied path on the host to mount over it."""
    assert browser_profile_roots(home) == []
    (home / ".mozilla").mkdir()
    assert browser_profile_roots(home) == [(home / ".mozilla").resolve()]


def test_the_list_covers_the_ruled_browsers_on_both_platforms() -> None:
    joined = "\n".join(rel for _os, rel in BROWSER_PROFILE_DIRS)
    for name in ("Google/Chrome", "Chromium", "BraveSoftware", "Microsoft Edge", "Arc", "Vivaldi", "Firefox",
                 "Library/Safari", "Library/Containers/com.apple.Safari", "Library/Cookies",
                 ".config/google-chrome", ".config/chromium", ".config/microsoft-edge", ".config/vivaldi",
                 ".mozilla", ".var/app"):
        assert name in joined, name


@live
def test_a_confined_shell_starts_with_profiles_present_and_cannot_read_them(home: Path) -> None:
    rel = "Library/Application Support/Google/Chrome" if _MAC else ".config/google-chrome"
    prof = _profile(home, rel)
    entity = _entity(home)
    with select_provider().spawn_shell(build_policy(entity)) as sh:
        r = sh.run(f"touch '{entity}/workspace/control' 2>&1", timeout=20)
        assert r.exit_code == 0, f"bash was refused with a profile present: {r.output!r}"
        for cmd in (f"cat '{prof}/Session Storage/000003.log'", f"head -c 16 '{prof}/Cookies'",
                    f"touch '{prof}/planted'"):
            out = sh.run(cmd + " 2>&1", timeout=20)
            assert out.exit_code != 0 and any(m in out.output for m in _REFUSALS), (cmd, out.output)
    assert not (prof / "planted").exists()
    (prof / "Private").chmod(0o700)   # let pytest clean up
