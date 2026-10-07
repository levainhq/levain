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
    for os_name, rel, _globs in BROWSER_PROFILE_DIRS:   # present, so Linux denies them too
        if os_name == mine:
            (home / rel).mkdir(parents=True, exist_ok=True)
    policy = build_policy(_entity(home))
    for os_name, rel, _globs in BROWSER_PROFILE_DIRS:
        if os_name != mine:
            continue
        root = (home / rel).resolve()
        assert root in policy.deny_read_write, rel
        assert crown_jewel_reason(policy, home / rel / "Default" / "Cookies") is not None, rel


def test_on_linux_an_absent_root_is_denied_and_created_only_where_its_parent_exists(home: Path, monkeypatch) -> None:
    """Head ruling 2026-10-07 (gemini L3): an absent root under a present parent is denied, so an entity cannot plant
    it as a link mid-session; its mountpoint is levain's, recorded for removal at close. One whose parent is absent
    (no ~/snap) is not denied, so nothing is created under a missing parent."""
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    (home / ".config").mkdir(exist_ok=True)
    entity = _entity(home)
    policy = build_policy(entity)
    for rel in (".mozilla", ".zen", ".config/google-chrome", ".config/opera"):
        root = (home / rel).resolve()
        assert root in policy.deny_read_write and root in policy.browser_mountpoints, rel
        assert not root.exists(), rel                          # building the policy creates nothing
    for rel in ("snap/firefox/common/.mozilla", ".var/app", ".cache/chrome-devtools-mcp"):
        assert (home / rel).resolve() not in policy.deny_read_write, rel
    (home / ".mozilla").mkdir()
    assert (home / ".mozilla").resolve() not in build_policy(entity).browser_mountpoints   # present: not ours


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


def test_on_linux_each_ruled_browser_is_denied_when_present(home: Path, monkeypatch):
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    rels = (".config/google-chrome", ".config/google-chrome-unstable", ".config/BraveSoftware", ".mozilla",
            "snap/firefox/common/.mozilla", "snap/chromium/common/chromium", ".config/opera", ".var/app", ".zen",
            ".librewolf", ".waterfox", ".config/opera-developer", ".config/vivaldi-snapshot",
            ".cache/chrome-devtools-mcp")
    for rel in rels:
        (home / rel).mkdir(parents=True)
    roots = cf.browser_profile_roots(home)
    for rel in rels:
        assert (home / rel).resolve() in roots, rel
    # $XDG_CONFIG_HOME adds a location; the default one stays denied too (levain's environment may not be the
    # browser's)
    xdg = home / "xdg"
    (xdg / "chromium").mkdir(parents=True)
    (home / ".config" / "chromium").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    roots = cf.browser_profile_roots(home)
    assert (xdg / "chromium").resolve() in roots and (home / ".config" / "chromium").resolve() in roots


def _linux(monkeypatch):
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    return cf


def test_a_profile_reached_through_a_symlink_elsewhere_is_denied_by_either_spelling(home: Path, monkeypatch, tmp_path):
    """Head ruling (L1 + L2, 2026-10-07): a dotfile manager's or a moved disk's ~/.mozilla is legitimate. It is denied
    at the link's spelling and the target's, and bash is not refused."""
    cf = _linux(monkeypatch)
    real = tmp_path / "data" / "mozilla"
    (real / "firefox").mkdir(parents=True)
    (home / ".mozilla").symlink_to(real)
    entity = _entity(home)
    policy = build_policy(entity)
    assert real.resolve() in policy.deny_read_write   # on Linux a mount through the link IS the target's mount
    assert crown_jewel_reason(policy, home / ".mozilla" / "firefox" / "x" / "cookies.sqlite") is not None
    assert crown_jewel_reason(policy, real / "firefox" / "x" / "cookies.sqlite") is not None
    cf._refuse_planted_browser_links(policy)   # does not raise


@pytest.mark.parametrize("plant", ["snap", ".mozilla", ".config"])
def test_a_profile_path_linked_into_the_entitys_workspace_refuses_bash_not_the_session(home: Path, monkeypatch,
                                                                                       plant) -> None:
    """L1 + L2: with the Linux base bound read-write an entity can plant an absent ancestor (~/snap) or the root
    itself as a link into its workspace; any component counts, not only the last. The session is still built; bash
    is refused at spawn."""
    cf = _linux(monkeypatch)
    entity = _entity(home)
    ws = entity / "workspace"
    target = ws / "planted"
    rel = {"snap": "snap/firefox/common/.mozilla", ".mozilla": ".mozilla", ".config": ".config/google-chrome"}[plant]
    (target / Path(rel).relative_to(plant)).mkdir(parents=True)
    (home / plant).symlink_to(target)
    policy = build_policy(entity)                       # the session opens
    with pytest.raises(cf.ConfinementError, match=f"symlink {home / plant}"):
        cf._refuse_planted_browser_links(policy)        # bash does not


def test_a_profile_that_appears_mid_session_is_denied_from_the_next_spawn(home: Path, monkeypatch) -> None:
    """L1: the roots are re-derived at every spawn, as a union."""
    cf = _linux(monkeypatch)
    policy = build_policy(_entity(home))
    cookie = home / "snap" / "firefox" / "common" / ".mozilla" / "firefox" / "x" / "cookies.sqlite"
    assert crown_jewel_reason(policy, cookie) is None          # no ~/snap/firefox yet: nothing to mount on
    (home / "snap" / "firefox" / "common").mkdir(parents=True)   # snapd installs Firefox mid-session
    refreshed = cf.refresh_socket_denies(policy)
    assert crown_jewel_reason(refreshed, cookie) is not None
    assert (home / "snap/firefox/common/.mozilla").resolve() in refreshed.browser_mountpoints


@pytest.mark.parametrize("osname,store", [
    ("Darwin", "Library/Application Support/com.openai.atlas/browser-data/host/Default/Local Storage"),
    ("Darwin", "Library/Application Support/com.openai.atlas/browser-data/host/user-abc/Session Storage"),
    ("Darwin", ".cache/chrome-devtools-mcp/chrome-profile/Default/Session Storage"),
    ("Darwin", "Library/WebKit/com.kagi.kagimacOS/WebsiteData/Default/abc123/LocalStorage"),
    ("Darwin", "Library/Application Support/zen/Profiles/x.default/storage/default"),
    ("Darwin", "Library/Application Support/com.operasoftware.OperaDeveloper/Local Storage"),
    ("Linux", ".zen/x.default/storage/default"),
    ("Linux", ".librewolf/x.default/webappsstore.sqlite"),
    ("Linux", ".var/app/io.gitlab.librewolf-community/.librewolf/x.default/storage/default"),
    ("Linux", ".var/app/com.opera.Opera/config/opera/Session Storage"),
    ("Linux", ".cache/chrome-devtools-mcp/chrome-profile/Default/Local Storage"),
])
def test_a_hardlink_to_each_layouts_page_storage_refuses_bash(home: Path, monkeypatch, osname, store) -> None:
    """L2: each browser's page storage at its own depth (Atlas two levels down, Orion's WebKit data, flatpak forks)."""
    import levain.firing.confinement as cf
    from levain.firing.confinement import ConfinementError, _refuse_multiply_linked_jewels

    monkeypatch.setattr(cf.platform, "system", lambda: osname)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    p = home / store
    if store.endswith(".sqlite"):
        p.parent.mkdir(parents=True)
        p.write_text("levain.token")
        f = p
    else:
        p.mkdir(parents=True)
        f = p / "000003.log"
        f.write_text("levain.token")
    entity = _entity(home)
    (entity / "workspace" / "alias").hardlink_to(f)
    with pytest.raises(ConfinementError, match="names on disk"):
        _refuse_multiply_linked_jewels(build_policy(entity))


def test_the_editors_check_reuses_the_spawns_storage_scan(home: Path, monkeypatch) -> None:
    """L1 cost: which storage folders exist is found at spawn and reused by the editor's checks for that policy."""
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Darwin")
    policy = build_policy(_entity(home))
    calls = []
    real = cf._browser_storage

    def counting(*a, **k):
        calls.append(k["fresh"])
        return real(*a, **k)

    monkeypatch.setattr(cf, "_browser_storage", counting)
    cf._refuse_multiply_linked_jewels(policy)
    store = home / "Library/Application Support/Chromium/Default/Session Storage"
    store.mkdir(parents=True)
    (store / "000003.log").write_text("levain.token")
    alias = home / "alias"
    alias.hardlink_to(store / "000003.log")
    assert cf.linked_jewel_reason(policy, alias) is None          # the spawn's scan, before the folder existed
    with pytest.raises(cf.ConfinementError, match="names on disk"):
        cf._refuse_multiply_linked_jewels(policy)                  # the next spawn looks again
    assert cf.linked_jewel_reason(policy, alias) is not None
    assert calls == [True, False, True, False]


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


def test_spawn_shell_refuses_a_planted_browser_link_before_the_platform_spawn(home: Path, monkeypatch) -> None:
    from levain.firing.confinement import ConfinementError, ConfinementProvider

    class _Recorder(ConfinementProvider):
        name = "recorder"
        spawned = False

        def available(self) -> bool:
            return True

        def render_profile(self, policy):
            return ""

        def _spawn_shell_impl(self, policy, *, env=None, default_timeout=120.0):
            type(self).spawned = True
            raise ConfinementError("recorder reached the platform spawn")

    _linux(monkeypatch)
    entity = _entity(home)
    (entity / "workspace" / "planted").mkdir()
    (home / ".mozilla").symlink_to(entity / "workspace" / "planted")
    with pytest.raises(ConfinementError, match="symlink"):
        _Recorder().spawn_shell(build_policy(entity))
    assert not _Recorder.spawned


@pytest.mark.parametrize("case", ["dangling_root", "ancestor_absent_child", "below_root"])
def test_a_link_into_the_workspace_refuses_bash_even_when_the_profile_is_absent_or_deeper(home: Path, monkeypatch,
                                                                                          case) -> None:
    """L3 (codex, gemini, complement): a dangling ~/.mozilla, a ~/.config linked into the workspace before Chrome
    exists, and a Default/ inside a real profile linked into the workspace all reach the entity's tree."""
    cf = _linux(monkeypatch)
    entity = _entity(home)
    ws = entity / "workspace"
    if case == "dangling_root":
        (home / ".mozilla").symlink_to(ws / "not-yet")          # the entity creates the target later
        link = home / ".mozilla"
    elif case == "ancestor_absent_child":
        (ws / "cfg").mkdir()
        (home / ".config").symlink_to(ws / "cfg")             # google-chrome/ does not exist yet
        link = home / ".config"
    else:
        (home / ".config" / "google-chrome").mkdir(parents=True)
        (ws / "profile").mkdir()
        (home / ".config" / "google-chrome" / "Default").symlink_to(ws / "profile")
        link = home / ".config" / "google-chrome" / "Default"
    policy = build_policy(entity)
    with pytest.raises(cf.ConfinementError, match=f"symlink {link}"):
        cf._refuse_planted_browser_links(policy)


def test_a_profile_folder_linked_elsewhere_is_denied_at_its_target(home: Path, monkeypatch, tmp_path) -> None:
    """codex L3: Default/ moved to another disk and linked back; the target holds the cookies and the token."""
    _linux(monkeypatch)
    elsewhere = tmp_path / "disk" / "chrome-default"
    elsewhere.mkdir(parents=True)
    (home / ".config" / "google-chrome").mkdir(parents=True)
    (home / ".config" / "google-chrome" / "Default").symlink_to(elsewhere)
    policy = build_policy(_entity(home))
    assert crown_jewel_reason(policy, elsewhere / "Cookies") is not None


def test_roots_are_resolved_when_home_itself_is_a_symlink(tmp_path, monkeypatch) -> None:
    """complement L3: the file editor compares resolved paths, so a root under a symlinked $HOME must be resolved."""
    import levain.firing.confinement as cf

    real = tmp_path / "data" / "me"
    (real / ".mozilla").mkdir(parents=True)
    (tmp_path / "home").symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _linux(monkeypatch)
    policy = build_policy(_entity(real))
    assert crown_jewel_reason(policy, tmp_path / "home" / ".mozilla" / "firefox" / "x" / "cookies.sqlite") is not None
    cf._refuse_planted_browser_links(policy)   # $HOME's own link is not a planted profile link


def test_an_absent_root_under_a_linked_config_is_created_at_its_target(home: Path, monkeypatch, tmp_path) -> None:
    """L1: GNU stow folds ~/.config into a link; an absent google-chrome under it is denied and created there too."""
    cf = _linux(monkeypatch)
    dotfiles = tmp_path / "dotfiles" / "config"
    dotfiles.mkdir(parents=True)
    (home / ".config").symlink_to(dotfiles)
    policy = build_policy(_entity(home))
    assert (dotfiles / "google-chrome").resolve() in policy.browser_mountpoints
    assert crown_jewel_reason(policy, home / ".config" / "google-chrome" / "Default" / "Cookies") is not None
    cf._refuse_planted_browser_links(policy)   # a dotfile manager's link is legitimate


@pytest.mark.parametrize("store", [".config/google-chrome/Default/Session Storage",
                                   ".mozilla/firefox/x.default/storage/default/http+++127.0.0.1+7420/ls"])
def test_a_link_inside_page_storage_that_leaves_the_profile_refuses_bash(home: Path, monkeypatch, store) -> None:
    """L1: LevelDB's create follows a link planted in the storage folder, so the token lands where it points."""
    cf = _linux(monkeypatch)
    entity = _entity(home)
    d = home / store
    d.mkdir(parents=True)
    (d / "000003.log").symlink_to(entity / "workspace" / "stolen.log")
    with pytest.raises(cf.ConfinementError, match="out of the profile"):
        cf._refuse_planted_browser_links(build_policy(entity))


@pytest.mark.parametrize("plant", ["snap", ".config/google-chrome/Default"])
def test_a_symlink_loop_refuses_bash_and_still_opens_the_session(home: Path, monkeypatch, plant) -> None:
    """L1: Path.resolve() raises RuntimeError on a loop (Python 3.12); the session must still open."""
    cf = _linux(monkeypatch)
    p = home / plant
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(p)
    entity = _entity(home)
    policy = build_policy(entity)
    with pytest.raises(cf.ConfinementError, match="loop"):
        cf._refuse_planted_browser_links(policy)


def test_a_non_browser_flatpak_apps_link_does_not_refuse_bash(home: Path, monkeypatch) -> None:
    """L1: only the browsers' flatpak folders are walked."""
    cf = _linux(monkeypatch)
    entity = _entity(home)
    d = home / ".var/app/org.example.Notes/config/notes"
    d.mkdir(parents=True)
    (d / "settings").symlink_to(entity / "workspace")
    cf._refuse_planted_browser_links(build_policy(entity))


def test_on_macos_a_linked_root_is_denied_at_both_spellings(home: Path, monkeypatch, tmp_path) -> None:
    import levain.firing.confinement as cf

    monkeypatch.setattr(cf.platform, "system", lambda: "Darwin")
    real = tmp_path / "ext" / "Firefox"
    real.mkdir(parents=True)
    (home / "Library/Application Support").mkdir(parents=True)
    (home / "Library/Application Support/Firefox").symlink_to(real)
    policy = build_policy(_entity(home))
    assert home / "Library/Application Support/Firefox" in policy.deny_read_write
    assert real.resolve() in policy.deny_read_write
