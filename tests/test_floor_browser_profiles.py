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


@pytest.mark.parametrize("rel", ["Library/Application Support/Google/Chrome",
                                 "Library/Application Support/BraveSoftware/Brave-Browser",
                                 "Library/Application Support/Arc/User Data"])
def test_a_hardlink_to_a_profiles_page_storage_refuses_bash_at_both_depths(home: Path, monkeypatch, rel) -> None:
    """L1d + L2d: Brave and Arc keep profiles one level deeper than Chrome; their storage is walked too."""
    import levain.firing.confinement as cf
    from levain.firing.confinement import ConfinementError, _refuse_multiply_linked_jewels

    monkeypatch.setattr(cf.platform, "system", lambda: "Darwin")
    prof = _profile(home, rel)
    entity = _entity(home)
    (entity / "workspace" / "alias.log").hardlink_to(prof / "Session Storage" / "000003.log")
    try:
        with pytest.raises(ConfinementError, match="names on disk"):
            _refuse_multiply_linked_jewels(build_policy(entity))
    finally:
        (prof / "Private").chmod(0o700)


@pytest.mark.parametrize("osname,rel", [("Darwin", "Library/Application Support/com.operasoftware.Opera"),
                                        ("Darwin", "Library/Application Support/com.operasoftware.OperaGX"),
                                        ("Linux", ".config/opera")])
def test_a_hardlink_to_operas_page_storage_at_the_profile_root_refuses_bash(home: Path, monkeypatch, osname,
                                                                            rel) -> None:
    """L1: Opera keeps its default profile at the root itself, with no Default/ folder."""
    import levain.firing.confinement as cf
    from levain.firing.confinement import ConfinementError, _refuse_multiply_linked_jewels

    monkeypatch.setattr(cf.platform, "system", lambda: osname)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    store = home / rel / "Session Storage"
    store.mkdir(parents=True)
    (store / "000003.log").write_text("levain.token")
    entity = _entity(home)
    (entity / "workspace" / "alias.log").hardlink_to(store / "000003.log")
    with pytest.raises(ConfinementError, match="names on disk"):
        _refuse_multiply_linked_jewels(build_policy(entity))


def test_a_hardlink_to_firefox_page_storage_refuses_bash(home: Path, monkeypatch) -> None:
    import levain.firing.confinement as cf
    from levain.firing.confinement import ConfinementError, _refuse_multiply_linked_jewels

    monkeypatch.setattr(cf.platform, "system", lambda: "Darwin")
    store = home / "Library/Application Support/Firefox/Profiles/x.default/storage/default/http+++127.0.0.1+7420/ls"
    store.mkdir(parents=True)
    (store / "data.sqlite").write_text("levain.token")
    entity = _entity(home)
    (entity / "workspace" / "alias").hardlink_to(store / "data.sqlite")
    with pytest.raises(ConfinementError, match="names on disk"):
        _refuse_multiply_linked_jewels(build_policy(entity))


def test_a_hardlink_inside_a_profiles_cache_alone_does_not_refuse_bash(home: Path, monkeypatch) -> None:
    """The carve-out Phill ruled: a profile is not walked whole, so WebKit-style cache hardlinks do not stop bash."""
    import levain.firing.confinement as cf
    from levain.firing.confinement import _refuse_multiply_linked_jewels

    monkeypatch.setattr(cf.platform, "system", lambda: "Darwin")
    prof = _profile(home, "Library/Application Support/Google/Chrome")
    try:
        _refuse_multiply_linked_jewels(build_policy(_entity(home)))   # does not raise
    finally:
        (prof / "Private").chmod(0o700)


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


def test_each_ruled_browser_profile_file_is_denied_on_macos(home: Path, monkeypatch) -> None:
    """L1d + L2d: Chrome's other channels sit beside Google/Chrome, Brave and Arc one level deeper. A real profile file
    for each browser and channel is denied to the file editor (a path, not an entry name, so this fails if the list
    stops covering one)."""
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Darwin")
    policy = build_policy(_entity(home))
    AS = "Library/Application Support"
    for f in (f"{AS}/Google/Chrome/Default/Cookies", f"{AS}/Google/Chrome Canary/Default/Cookies",
              f"{AS}/Google/Chrome Beta/Default/Cookies", f"{AS}/Google/Chrome Dev/Default/Cookies",
              f"{AS}/Google/Chrome for Testing/Default/Cookies", f"{AS}/Chromium/Default/Cookies",
              f"{AS}/BraveSoftware/Brave-Browser/Default/Cookies", f"{AS}/Microsoft Edge Beta/Default/Cookies",
              f"{AS}/Microsoft Edge/Default/Cookies", f"{AS}/Arc/User Data/Default/Cookies",
              f"{AS}/Vivaldi/Default/Cookies", f"{AS}/com.operasoftware.Opera/Cookies",
              f"{AS}/Firefox/Profiles/x.default/cookies.sqlite", "Library/Safari/History.db",
              "Library/Containers/com.apple.Safari/Data/x", "Library/Cookies/Cookies.binarycookies"):
        assert crown_jewel_reason(policy, home / f) is not None, f


def test_on_linux_each_ruled_browser_is_denied_when_present_and_a_symlinked_root_refuses(home: Path, monkeypatch):
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Linux")
    rels = (".config/google-chrome", ".config/google-chrome-unstable", ".config/BraveSoftware", ".mozilla",
            "snap/firefox/common/.mozilla", "snap/chromium/common/chromium", ".config/opera", ".var/app")
    for rel in rels:
        (home / rel).mkdir(parents=True)
    roots = cf.browser_profile_roots(home)
    for rel in rels:
        assert (home / rel).resolve() in roots, rel
    # $XDG_CONFIG_HOME relocates .config
    xdg = home / "xdg"
    (xdg / "chromium").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert (xdg / "chromium").resolve() in cf.browser_profile_roots(home)
    # L2d: an entity could plant the absent path as a link into its workspace
    (home / ".config" / "vivaldi").symlink_to(home)
    with pytest.raises(cf.ConfinementError, match="symlink"):
        cf.browser_profile_roots(home)


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


@pytest.mark.parametrize("store", [
    ".var/app/com.google.Chrome/config/google-chrome/Default/Session Storage",          # flatpak Chrome
    ".var/app/org.chromium.Chromium/config/chromium/Default/Local Storage",            # flatpak Chromium
    ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Session Storage",   # flatpak Brave
    ".var/app/org.mozilla.firefox/.mozilla/firefox/x.default/storage/default",         # flatpak Firefox
    "snap/brave/current/.config/BraveSoftware/Brave-Browser/Default/Session Storage",  # snap Brave
])
def test_on_linux_a_hardlink_to_a_containerised_browsers_page_storage_refuses_bash(home: Path, monkeypatch,
                                                                                   store) -> None:
    """L1d: flatpak and snap keep a browser's profile several levels under the denied root, deeper than the plain
    storage globs reach, so their page storage is walked by its own patterns."""
    import levain.firing.confinement as cf
    from levain.firing.confinement import ConfinementError, _refuse_multiply_linked_jewels

    monkeypatch.setattr(cf.platform, "system", lambda: "Linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    d = home / store
    d.mkdir(parents=True)
    (d / "000003.log").write_text("levain.token")
    entity = _entity(home)
    (entity / "workspace" / "alias.log").hardlink_to(d / "000003.log")
    with pytest.raises(ConfinementError, match="names on disk"):
        _refuse_multiply_linked_jewels(build_policy(entity))


def test_on_linux_a_relative_xdg_config_home_is_ignored(home: Path, monkeypatch) -> None:
    """L1: the XDG Base Directory spec says a relative $XDG_CONFIG_HOME is invalid and is ignored."""
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Linux")
    monkeypatch.chdir(home)
    (home / "relxdg" / "chromium").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relxdg")
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    assert (home / "relxdg" / "chromium").resolve() not in cf.browser_profile_roots(home)
