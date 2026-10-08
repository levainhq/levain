"""The standard credential stores at every spelling, and Linux absent paths without lasting host files.

Lane P2's research note, items 5 and 6 (2026-10-07): ``build_policy`` added each standard cred path
RESOLVED only, so a stow-style ``~/.kube/config -> ~/dotfiles/kube/config`` left the link itself out
of the floor, and on Linux every absent cred file became a permanent 0444 file on the host. These
tests read the policy, the rendered Seatbelt text and the bwrap argv as data. Nothing here runs a
sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.firing import confinement as C
from levain.firing.confinement import (
    ConfinementError,
    SeatbeltProvider,
    build_policy,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL", *{v for v, _, _ in C._CRED_OVERRIDES}):
        monkeypatch.delenv(var, raising=False)
    return h


def _entity(home: Path) -> Path:
    d = home / "ent"
    (d / ".levain").mkdir(parents=True)
    return d


def _stow_kube(home: Path) -> tuple[Path, Path]:
    """``~/.kube/config`` as a link into a dotfiles tree, the way stow and chezmoi lay it out."""
    target = home / "dotfiles" / "kube" / "config"
    target.parent.mkdir(parents=True)
    target.write_text("apiVersion: v1\n")
    (home / ".kube").mkdir()
    link = home / ".kube" / "config"
    link.symlink_to(target)
    return link, target


def _ops(argv: list[str], op: str) -> list[list[str]]:
    width = {"--tmpfs": 1, "--remount-ro": 1}.get(op, 2)
    return [argv[i + 1:i + 1 + width] for i, a in enumerate(argv) if a == op]


# --- the three spellings ------------------------------------------------------------------------


def test_a_symlinked_cred_file_is_denied_at_its_link_and_its_target(home: Path) -> None:
    link, target = _stow_kube(home)
    policy = build_policy(_entity(home), deny_standard_creds=True)
    assert link in policy.deny_files, "the link itself must be in the floor (unlink/rename match it)"
    assert target.resolve() in policy.deny_files
    profile = SeatbeltProvider().render_profile(policy)
    assert f'(literal "{link}")' in profile
    assert f'(literal "{target.resolve()}")' in profile
    # the link's directory is write-pinned, so the link cannot be swapped by renaming it either
    assert home / ".kube" in policy.deny_write_dirs


def test_a_symlinked_home_puts_every_cred_store_in_at_the_raw_spelling(tmp_path: Path, monkeypatch) -> None:
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "link-home"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))
    policy = build_policy(_entity(link), deny_standard_creds=True)
    for rel in (".netrc", ".npmrc", ".aws/credentials"):
        assert link / rel in policy.deny_files
        assert real.resolve() / rel in policy.deny_files
    assert link / ".config" / "gh" in policy.deny_read_write
    assert real.resolve() / ".config" / "gh" in policy.deny_read_write


def test_the_ssh_vectors_still_come_from_the_same_three_spellings(home: Path) -> None:
    policy = build_policy(_entity(home), ssh_mode="raw")
    for n in ("authorized_keys", "authorized_keys2", "config", "rc"):
        assert C._spellings(f"~/.ssh/{n}") == [home / ".ssh" / n]
        assert home / ".ssh" / n in policy.deny_write_files


def test_spellings_keeps_all_three_when_they_differ(tmp_path: Path, monkeypatch) -> None:
    real = tmp_path / "r"
    (real / "dot").mkdir(parents=True)
    (real / "dot" / "netrc").write_text("x")
    lh = tmp_path / "lh"
    lh.symlink_to(real)
    (real / ".netrc").symlink_to(real / "dot" / "netrc")
    monkeypatch.setenv("HOME", str(lh))
    assert C._spellings("~/.netrc") == [lh / ".netrc", real.resolve() / ".netrc",
                                        (real / "dot" / "netrc").resolve()]


# --- Linux: tool directories get a read-only view; deeper links refuse ---------------------------


def test_linux_plan_masks_a_link_in_a_tool_dir_at_its_target_without_refusing(home: Path) -> None:
    link, target = _stow_kube(home)
    (home / ".kube" / "cache").mkdir()
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    kube = str((home / ".kube").resolve())
    assert [kube] in _ops(argv, "--tmpfs")
    assert [str(home.resolve() / ".kube" / "cache")] * 2 in _ops(argv, "--bind-try")
    masked = [d for s, d in _ops(argv, "--ro-bind") if s == "/dev/null"]
    assert str(target.resolve()) in masked
    assert str(link) not in masked                     # a mount cannot land on the link
    assert len(masked) == len(set(masked))             # one mask per destination
    # the read-only bind comes before every mask, so a mask lands on top of it
    assert argv.index("--ro-bind") < argv.index("/dev/null")


def test_linux_plan_follows_a_home_level_symlinked_tool_dir(home: Path) -> None:
    """Ruling 2026-10-07: a link directly in $HOME cannot be replaced inside bash, so the tool
    directory it names gets its view at the target instead of a refusal."""
    (home / "dotfiles" / "kube").mkdir(parents=True)
    (home / ".kube").symlink_to(home / "dotfiles" / "kube")
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    assert [str((home / "dotfiles" / "kube").resolve())] in _ops(argv, "--tmpfs")


def test_linux_plan_refuses_a_symlinked_cred_subtree_in_a_writable_dir(home: Path) -> None:
    (home / "dotfiles" / "gh").mkdir(parents=True)
    (home / ".config").mkdir()
    (home / ".config" / "gh").symlink_to(home / "dotfiles" / "gh")
    with pytest.raises(ConfinementError, match="symlink in a directory this user can write"):
        C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))


def test_linux_plan_creates_an_absent_tool_dir_and_puts_no_file_in_it(home: Path) -> None:
    (home / ".config").mkdir()   # bound back writable: ~/.config/git is creatable, so it gets a view
    argv, create_first = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    git = str(home.resolve() / ".config" / "git")
    assert git in create_first and [git] in _ops(argv, "--tmpfs")
    for tool in (".kube", ".docker", ".aws"):   # directly in $HOME: absent from its view, uncreatable
        d = str(home.resolve() / tool)
        assert d not in create_first and d not in argv, tool
    masked = [d for s, d in _ops(argv, "--ro-bind") if s == "/dev/null"]
    for f in (".kube/config", ".docker/config.json", ".aws/credentials", ".config/git/credentials",
              ".netrc"):
        assert str(home.resolve() / f) not in masked, f"{f}: a 0444 stub"


def test_home_level_cred_files_need_no_placeholder_in_the_home_view(home: Path) -> None:
    """Ruling 2026-10-07 (b): an absent ~/.netrc is absent from step (0)'s view, so it needs no
    placeholder on the host, and one the host creates later is never seen inside bash."""
    policy = build_policy(_entity(home), deny_standard_creds=True)
    argv, _ = C._bwrap_plan(policy)
    mounted, unmounted = C._mount_plan_paths(argv, policy)
    netrc = str(home.resolve() / ".netrc")
    assert netrc not in mounted and netrc not in unmounted and netrc not in argv


# --- the session-scoped placeholder ledger -----------------------------------------------------------


def test_prepare_mountpoints_reports_what_it_created(home: Path) -> None:
    f = home / ".netrc"
    created = C._prepare_mountpoints({str(f): "file", str(home / "x" / "y"): "dir"})
    assert (str(f), "file") in created
    assert f.stat().st_size == 0 and (f.stat().st_mode & 0o777) == 0o444


def test_a_placeholder_is_removed_when_its_only_session_releases_it(home: Path) -> None:
    f = home / ".netrc"
    C._prepare_mountpoints({str(f): "file"})
    C._ledger_enter([(str(f), "file")], {str(f)}, "1:a")
    assert [str(p) for p in C.live_floor_placeholders()] == [str(f)]
    C._ledger_release("1:a")
    assert not f.exists()
    assert C.live_floor_placeholders() == []


def test_a_placeholder_another_session_claims_survives_the_first_close(home: Path) -> None:
    f = home / ".netrc"
    C._prepare_mountpoints({str(f): "file"})
    C._ledger_enter([(str(f), "file")], {str(f)}, f"{os.getpid()}:a")
    C._ledger_enter([], {str(f)}, f"{os.getpid()}:b")    # a second shell masks the same file
    C._ledger_release(f"{os.getpid()}:a")
    assert f.exists(), "removing it would detach the other session's mask"
    C._ledger_release(f"{os.getpid()}:b")
    assert not f.exists()


def test_a_placeholder_the_operator_replaced_is_left_alone(home: Path) -> None:
    f = home / ".netrc"
    C._prepare_mountpoints({str(f): "file"})
    C._ledger_enter([(str(f), "file")], {str(f)}, "1:a")
    f.chmod(0o600)
    f.unlink()
    f.write_text("machine real\n")                    # e.g. `npm login` writing by rename
    C._ledger_release("1:a")
    assert f.read_text() == "machine real\n"
    assert C.live_floor_placeholders() == []


def test_the_sweep_removes_a_placeholder_whose_session_died(home: Path) -> None:
    f = home / ".npmrc"
    C._prepare_mountpoints({str(f): "file"})
    dead = 2 ** 22 + 12345                               # above every default pid_max
    C._ledger_enter([(str(f), "file")], {str(f)}, f"{dead}:x")
    live = home / ".pypirc"
    C._prepare_mountpoints({str(live): "file"})
    C._ledger_enter([(str(live), "file")], {str(live)}, f"{os.getpid()}:y")
    removed = C.sweep_floor_placeholders()
    assert removed == [str(f)]
    assert not f.exists() and live.exists()
    C._ledger_release(f"{os.getpid()}:y")


def test_an_empty_created_tool_dir_is_removed_and_a_used_one_kept(home: Path) -> None:
    kube, docker = home / ".kube", home / ".docker"
    kube.mkdir(mode=0o700)
    docker.mkdir(mode=0o700)
    C._ledger_enter([(str(kube), "dir"), (str(docker), "dir")], {str(kube), str(docker)}, "1:a")
    (docker / "config.json").write_text("{}")         # the operator logged in meanwhile
    C._ledger_release("1:a")
    assert not kube.exists()
    assert docker.exists()


def test_the_ledger_is_a_crown_jewel(home: Path) -> None:
    policy = build_policy(_entity(home))
    assert C._ledger_dir().resolve() in policy.deny_read_write
    assert C.crown_jewel_reason(policy, C._ledger_dir() / "placeholders.json") is not None


def test_doctor_sweeps_dead_placeholders_and_names_live_ones(home: Path) -> None:
    from levain.doctor import _check_floor_placeholders

    assert _check_floor_placeholders() == []
    dead_f, live_f = home / ".netrc", home / ".pypirc"
    C._prepare_mountpoints({str(dead_f): "file", str(live_f): "file"})
    C._ledger_enter([(str(dead_f), "file")], {str(dead_f)}, f"{2 ** 22 + 7}:x")
    C._ledger_enter([(str(live_f), "file")], {str(live_f)}, f"{os.getpid()}:y")
    [row] = _check_floor_placeholders()
    assert row.ok and str(dead_f) in row.detail and str(live_f) in row.detail
    assert not dead_f.exists() and live_f.exists()
    C._ledger_release(f"{os.getpid()}:y")
    assert not live_f.exists()


def test_a_placeholder_written_in_place_is_left_alone(home: Path) -> None:
    f = home / ".npmrc"
    C._prepare_mountpoints({str(f): "file"})
    C._ledger_enter([(str(f), "file")], {str(f)}, "1:a")
    f.chmod(0o600)
    f.write_text("//registry.npmjs.org/:_authToken=real\n")   # same inode, the operator's content now
    C._ledger_release("1:a")
    assert f.read_text().endswith("=real\n")


def test_a_placeholder_made_under_a_strict_umask_is_still_removable(home: Path) -> None:
    f = home / ".netrc"
    old = os.umask(0o077)
    try:
        C._prepare_mountpoints({str(f): "file"})
    finally:
        os.umask(old)
    assert (f.stat().st_mode & 0o777) == 0o444
    C._ledger_enter([(str(f), "file")], {str(f)}, "1:a")
    C._ledger_release("1:a")
    assert not f.exists()


def test_prepare_mountpoints_owns_only_what_its_own_mkdir_made(home: Path) -> None:
    (home / ".config").mkdir()                       # the operator's, before levain looked
    made = C._prepare_mountpoints({str(home / ".config" / "gh" / "x" / "hosts.yml"): "file",
                                   str(home / ".aws" / "sso" / "cache"): "dir"})
    paths = {p for p, _ in made}
    assert str(home / ".config") not in paths
    assert {str(home / ".config" / "gh"), str(home / ".config" / "gh" / "x"), str(home / ".aws"),
            str(home / ".aws" / "sso"), str(home / ".aws" / "sso" / "cache")} <= paths
    assert (str(home / ".config" / "gh" / "x" / "hosts.yml"), "file") in made


def test_a_claim_from_another_pid_namespace_is_kept_and_a_reused_pid_is_not(monkeypatch) -> None:
    assert C._claim_alive(f"{2 ** 22 + 9}:-:some-other-ns:x")       # not ours to judge
    monkeypatch.setattr(C, "_pidns", lambda: "ours")
    monkeypatch.setattr(C, "_proc_start_time", lambda pid: "200")
    assert C._claim_alive(f"{os.getpid()}:200:ours:x")
    assert not C._claim_alive(f"{os.getpid()}:100:ours:x"), "the pid now names a later process"


def test_a_ledger_reached_through_a_link_is_not_trusted(home: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "forged"
    (elsewhere / "floor").mkdir(parents=True, mode=0o700)
    (home / ".levain-runtime").symlink_to(elsewhere)
    f = home / ".netrc"
    C._prepare_mountpoints({str(f): "file"})
    st = f.stat()
    (elsewhere / "floor" / "placeholders.json").write_text(
        '{"entries": [{"path": "%s", "kind": "file", "dev": %d, "ino": %d, "claims": []}]}'
        % (f, st.st_dev, st.st_ino))
    assert C.sweep_floor_placeholders() == []
    assert f.exists(), "nothing is removed on the word of a ledger levain cannot trust"
    assert "not a directory" in (C.ledger_problem() or "")


def test_a_corrupt_ledger_is_reported(home: Path) -> None:
    from levain.doctor import _check_floor_placeholders

    C._ledger_enter([], set(), "1:a")
    (C._ledger_dir() / "placeholders.json").write_text("{not json")
    [row] = _check_floor_placeholders()
    assert row.ok is False and "cannot be read" in row.detail


def test_a_shell_releases_its_claim_only_once_its_pid_namespace_is_gone(home: Path, monkeypatch) -> None:
    f = home / ".netrc"
    C._prepare_mountpoints({str(f): "file"})
    C._ledger_enter([(str(f), "file")], {str(f)}, f"{os.getpid()}:-:-:k")
    shell = C._BwrapShell(policy=None, manifest={}, argv=["/bin/true"], cwd=home, env={})  # type: ignore[arg-type]
    shell._ledger_claim = f"{os.getpid()}:-:-:k"

    class _P:   # a command's leader (`_Leader`), already exited
        pid = 4242
        exited = True
        reaped = False

        def wait(self, timeout=None):
            return True

        def reap(self, timeout=None):
            return True

        def release_watch(self):
            pass

        def hold_for_signal(self):
            return False   # 4242 is not a group of this test's: send it nothing
    lead = _P()
    shell._groups = {4242: lead}
    shell._bashes = {4242: (lead, (4243, "1"))}   # the command's bash, pid 1 of its namespace
    monkeypatch.setattr(C, "_bash_gone", lambda pid, start, timeout: False)
    shell.close()
    assert f.exists(), "the namespace may still hold the mount: keep the claim"
    shell2 = C._BwrapShell(policy=None, manifest={}, argv=["/bin/true"], cwd=home, env={})  # type: ignore[arg-type]
    shell2._ledger_claim = f"{os.getpid()}:-:-:k"
    lead2 = _P()
    shell2._groups = {4242: lead2}
    shell2._bashes = {4242: (lead2, (4243, "1"))}
    monkeypatch.setattr(C, "_bash_gone", lambda pid, start, timeout: True)
    shell2.close()
    assert not f.exists()


def test_group_gone_sees_a_live_and_a_finished_group() -> None:
    import subprocess as sp

    p = sp.Popen(["sleep", "5"], start_new_session=True, env={"PATH": "/bin:/usr/bin"})
    try:
        assert C._group_gone(p.pid, timeout=0.2) is False
    finally:
        p.kill()
        p.wait()
    assert C._group_gone(p.pid, timeout=2.0) is True


def test_levain_exit_closes_every_live_shell(monkeypatch) -> None:
    closed = []

    class _S:
        def close(self):
            closed.append(self)
    s = _S()
    C._LIVE_BWRAP_SHELLS.add(s)
    try:
        C._close_live_shells()
    finally:
        C._LIVE_BWRAP_SHELLS.discard(s)
    assert closed == [s]




# --- $HOME's own entries are read-only inside bash (desk ruling, option (c)) --------------------------


def test_linux_plan_binds_home_read_only_with_subdirectories_back(home: Path) -> None:
    (home / "proj").mkdir()
    (home / "dotfiles").mkdir()
    (home / ".config").symlink_to(home / "dotfiles")
    (home / ".zshrc").write_text("")
    argv, _ = C._bwrap_plan(build_policy(_entity(home)))
    h = str(home.resolve())
    i_home = next(i for i in range(len(argv) - 1) if argv[i:i + 2] == ["--tmpfs", h])
    for sub in ("proj", "dotfiles", "ent"):
        d = str(home.resolve() / sub)
        i_sub = next(i for i in range(len(argv) - 2) if argv[i:i + 3] == ["--bind", d, d])
        assert i_sub > i_home, sub
    rc = str(home.resolve() / ".zshrc")
    assert ["--ro-bind-try", rc, rc] in [argv[i:i + 3] for i in range(len(argv) - 2)]
    cfg = str(home.resolve() / ".config")
    assert ["--symlink", str(home / "dotfiles"), cfg] in [argv[i:i + 3] for i in range(len(argv) - 2)]
    assert argv.count(cfg) == 1, "a link stays a (frozen) link, never a mount"
    # every deeper mount comes after the view, or the view would hide it; the view goes read-only last
    first_body = min(i for i, a in enumerate(argv) if a == "--tmpfs" and argv[i + 1] != h)
    assert first_body > i_home
    assert argv[-2:] == ["--remount-ro", h]
    assert ["--bind", h, h] not in [argv[i:i + 3] for i in range(len(argv) - 2)]


_live_bwrap = pytest.mark.skipif(
    not (__import__("platform").system() == "Linux" and C.bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


@_live_bwrap
def test_linux_live_home_entries_cannot_be_created_removed_or_swapped(home: Path) -> None:
    (home / "sub").mkdir()
    (home / "target").mkdir()
    (home / "lnk").symlink_to(home / "target")
    ent = _entity(home)
    shell = C.BwrapProvider().spawn_shell(build_policy(ent, workspace=ent / "workspace"))
    try:
        def rc(cmd: str) -> int:
            return shell.run(cmd + " >/dev/null 2>&1; echo RC=$?", timeout=20).output.strip().split("RC=")[-1]
        assert rc(f"ln -sfn / {home}/lnk") != "0"
        assert rc(f"rm -f {home}/lnk") != "0"
        assert rc(f"touch {home}/top") != "0"
        assert rc(f"mkdir {home}/newdir") != "0"
        assert rc(f"touch {home}/sub/ok") == "0"
        assert rc(f"touch {ent}/workspace/w") == "0"
    finally:
        shell.close()
    assert os.readlink(home / "lnk") == str(home / "target")
    assert not (home / "top").exists() and not (home / "newdir").exists()
    assert (home / "sub" / "ok").exists() and (ent / "workspace" / "w").exists()


def test_a_dir_levain_made_that_is_not_empty_yet_is_kept_not_forgotten(home: Path) -> None:
    parent, child = home / ".config", home / ".config" / "gh"
    made = C._prepare_mountpoints({str(child): "dir"})
    C._ledger_enter(made, {str(child)}, "1:a")                 # only the mountpoint is claimed
    C._ledger_release("1:b")                                     # an unrelated release runs the exit
    assert str(parent) in [e["path"] for e in __import__("json").loads(
        (C._ledger_dir() / "placeholders.json").read_text())["entries"]]
    C._ledger_release("1:a")
    assert not child.exists() and not parent.exists()


def test_a_dead_levain_whose_sandbox_group_lives_keeps_its_claim(monkeypatch) -> None:
    import subprocess as sp

    p = sp.Popen(["sleep", "5"], start_new_session=True, env={"PATH": "/bin:/usr/bin"})
    try:
        monkeypatch.setattr(C, "_pidns", lambda: "ns")
        assert C._claim_alive(f"{2 ** 22 + 3}:-:ns:x:g{p.pid}")
    finally:
        p.kill()
        p.wait()
    assert not C._claim_alive(f"{2 ** 22 + 3}:-:ns:x:g{p.pid}")


def test_a_git_store_path_with_spaces_is_read_whole(tmp_path: Path) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text('[credential]\n\thelper = store --file "/home/u/my creds"\n'
                   "\thelper = \"store --file='/opt/x y'\"\n\thelper = osxkeychain\n")
    assert C._git_store_files(cfg) == [Path("/home/u/my creds"), Path("/opt/x y")]


def test_a_secret_file_registry_that_cannot_be_read_refuses_the_floor(home: Path, monkeypatch) -> None:
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "levain.launch", None)
    with pytest.raises(ConfinementError):
        build_policy(_entity(home))


# --- L3 r2: a failure part-way through the mountpoints ----------------------------------------------


def test_mountpoints_made_before_a_failure_reach_the_callers_list(home: Path) -> None:
    """complement r2: _prepare_mountpoints raising part-way lost what it had made, so the ledger never
    removed it and a 0444 ~/.netrc stayed on the host for good."""
    (home / "afile").write_text("")
    made: list[tuple[str, str]] = []
    with pytest.raises(OSError):
        C._prepare_mountpoints({str(home / ".netrc"): "file", str(home / "afile" / "x"): "file"}, made)
    assert (str(home / ".netrc"), "file") in made


def test_a_placeholder_whose_chmod_fails_is_removed_not_left(home: Path, monkeypatch) -> None:
    """L1 then codex r3: a file whose fchmod failed is not the 0444 file the ledger knows how to
    remove (under umask 077 it is 0400), so it is removed at once rather than recorded."""
    made: list[tuple[str, str]] = []

    def boom(fd, mode):
        raise OSError("fchmod failed")

    monkeypatch.setattr(C.os, "fchmod", boom)
    with pytest.raises(OSError):
        C._prepare_mountpoints({str(home / ".netrc"): "file"}, made)
    assert not (home / ".netrc").exists() and made == []


# --- ruling 2026-10-07: a link directly in $HOME is masked at its target -----------------------------


def _config_link(home: Path) -> Path:
    """``~/.config`` as a link to a writable directory outside $HOME, holding a gh token."""
    cfg = home.parent / "elsewhere" / "cfg"
    (cfg / "gh").mkdir(parents=True)
    (cfg / "gh" / "hosts.yml").write_text("oauth_token: SECRET-TOKEN\n")
    (home / ".config").symlink_to(cfg)
    return cfg.resolve()


def test_linux_plan_accepts_a_home_level_dir_link_and_mounts_at_the_resolved_target(home: Path) -> None:
    cfg = _config_link(home)
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    link = str(home.resolve() / ".config")
    through_link = [a for a in argv if a.startswith(link + "/")]
    assert argv.count(link) == 1 and argv[argv.index(link) - 2] == "--symlink"
    assert through_link == [], "a mount spelled through the link cannot land inside bwrap's new root"
    assert any(a == str(cfg / "gh") or a.startswith(str(cfg / "gh")) for a in argv), "covered at its target"
    assert [str(cfg), str(cfg)] in _ops(argv, "--bind"), "the target is pinned against a rename"
    assert [str(cfg.parent)] * 2 in _ops(argv, "--bind"), "and so is its parent, outside $HOME"


def test_linux_plan_masks_a_home_level_cred_link_at_its_target(home: Path) -> None:
    target = home / "dotfiles" / "netrc"
    target.parent.mkdir()
    target.write_text("machine x login y password z\n")
    (home / ".netrc").symlink_to(target)
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    masked = [d for s, d in _ops(argv, "--ro-bind") if s == "/dev/null"]
    assert str(target.resolve()) in masked
    assert str(home.resolve() / ".netrc") not in masked


def test_the_home_level_exemption_needs_the_read_only_home(home: Path, monkeypatch) -> None:
    """Mutation check of the ruling's premise: without step (0)'s read-only $HOME the link could be
    swapped from inside, so it is refused again."""
    _config_link(home)
    monkeypatch.setattr(C, "_home_read_only_root", lambda: None)
    with pytest.raises(ConfinementError, match="symlink"):
        C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))


_live = pytest.mark.skipif(
    not (__import__("platform").system() == "Linux" and C.bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


def _live_rc(shell, cmd: str) -> str:
    return shell.run(cmd + " >/dev/null 2>&1; echo RC=$?", timeout=20).output.strip().split("RC=")[-1]


@_live
def test_linux_live_a_home_level_config_link_starts_and_its_cred_is_denied(home: Path) -> None:
    cfg = _config_link(home)
    ent = _entity(home)
    shell = C.BwrapProvider().spawn_shell(build_policy(ent, workspace=ent / "workspace",
                                                       deny_standard_creds=True))
    try:
        for spelling in (home / ".config" / "gh" / "hosts.yml", cfg / "gh" / "hosts.yml"):
            out = shell.run(f"cat {spelling} 2>&1; echo END", timeout=20).output
            assert "SECRET-TOKEN" not in out, spelling
            assert "END" in out
    finally:
        shell.close()


@_live
def test_linux_live_a_home_level_config_link_cannot_be_swapped_or_removed(home: Path) -> None:
    cfg = _config_link(home)
    ent = _entity(home)
    shell = C.BwrapProvider().spawn_shell(build_policy(ent, workspace=ent / "workspace",
                                                       deny_standard_creds=True))
    try:
        assert _live_rc(shell, f"ln -sfn / {home}/.config") != "0"
        assert _live_rc(shell, f"rm -f {home}/.config") != "0"
        assert _live_rc(shell, f"mv {cfg} {cfg}.moved") != "0"
    finally:
        shell.close()
    assert os.readlink(home / ".config") == str(home.parent / "elsewhere" / "cfg")


@_live
def test_linux_live_a_deeper_link_still_refuses(home: Path) -> None:
    (home / "dotfiles" / "gh").mkdir(parents=True)
    (home / ".config").mkdir()
    (home / ".config" / "gh").symlink_to(home / "dotfiles" / "gh")
    ent = _entity(home)
    with pytest.raises(ConfinementError, match="symlink in a directory this user can write"):
        C.BwrapProvider().spawn_shell(build_policy(ent, workspace=ent / "workspace",
                                                   deny_standard_creds=True))


# --- ruling 2026-10-07: a tool directory is a view without its cred names ----------------------------


def test_linux_plan_gives_a_tool_dir_a_view_without_its_cred_names(home: Path) -> None:
    kube = home / ".kube"
    (kube / "cache").mkdir(parents=True)
    (kube / "notes.txt").write_text("")
    (kube / "current").symlink_to("cache")
    (kube / "config").write_text("token: SECRET\n")
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    k = str(kube.resolve())
    assert [k] in _ops(argv, "--tmpfs") and [k] in _ops(argv, "--remount-ro")
    assert [k, k] not in _ops(argv, "--ro-bind"), "the host directory itself is never shown"
    assert [k + "/cache"] * 2 in _ops(argv, "--bind-try")
    assert [k + "/notes.txt"] * 2 in _ops(argv, "--ro-bind-try")
    assert ["cache", k + "/current"] in _ops(argv, "--symlink")
    assert k + "/config" not in argv, "a cred name is absent in the view, not masked by an empty file"


def test_an_absent_cred_in_a_tool_dir_is_neither_mounted_nor_watched_on_the_host(home: Path) -> None:
    (home / ".kube").mkdir()
    policy = build_policy(_entity(home), deny_standard_creds=True)
    argv, _ = C._bwrap_plan(policy)
    mounted, unmounted = C._mount_plan_paths(argv, policy)
    cfg = str(home.resolve() / ".kube" / "config")
    assert cfg not in mounted and cfg not in unmounted and cfg not in argv


@_live
def test_linux_live_a_cred_the_host_creates_in_a_tool_dir_after_spawn_is_not_readable(home: Path) -> None:
    kube = home / ".kube"
    (kube / "cache").mkdir(parents=True)
    (kube / "cache" / "seen").write_text("CARRIED\n")
    ent = _entity(home)
    shell = C.BwrapProvider().spawn_shell(build_policy(ent, workspace=ent / "workspace",
                                                       deny_standard_creds=True))
    try:
        (kube / "config").write_text("token: SECRET-KUBE\n")      # the operator's kubectl, mid-session
        out = shell.run(f"cat {kube}/config 2>&1; cat {kube}/cache/seen; echo END", timeout=20).output
        assert "SECRET-KUBE" not in out
        assert "CARRIED" in out and "END" in out
        assert _live_rc(shell, f"touch {kube}/config") != "0"
        assert _live_rc(shell, f"touch {kube}/cache/w") == "0"
    finally:
        shell.close()
    assert (kube / "cache" / "w").exists()


def test_nothing_inside_a_tool_dir_view_is_created_on_the_host(home: Path) -> None:
    """The ruling: no pre-creation on the host. A jewel subtree inside a tool directory (the aws
    caches) is mounted inside the view; its absent parents are not pinned, so not created, on the host."""
    (home / ".aws").mkdir()
    policy = build_policy(_entity(home), deny_standard_creds=True)
    argv, create_first = C._bwrap_plan(policy)
    aws = str(home.resolve() / ".aws")
    assert not [c for c in create_first if c.startswith(aws + "/")]
    mounted, _ = C._mount_plan_paths(argv, policy)
    assert not [q for q in mounted if q.startswith(aws + "/")]
    assert [aws + "/sso/cache"] in _ops(argv, "--tmpfs")


# --- L3 r3 ------------------------------------------------------------------------------------------


def test_an_entity_inside_a_tool_dir_keeps_its_read_only_store(home: Path) -> None:
    """codex r3: the tool view bound ~/.kube/project back read-write AFTER the entity store's
    read-only bind, covering it, so bash could create a confinement.json there."""
    ent = home / ".kube" / "project"
    (ent / ".levain").mkdir(parents=True)
    argv, _ = C._bwrap_plan(build_policy(ent, deny_standard_creds=True))
    lv, proj = str((ent / ".levain").resolve()), str(ent.resolve())
    i_store = next(i for i in range(len(argv) - 2) if argv[i:i + 3] == ["--ro-bind", lv, lv])
    i_child = next(i for i in range(len(argv) - 2) if argv[i:i + 3] == ["--bind-try", proj, proj])
    assert i_child < i_store


def test_a_jewel_under_a_bound_back_child_of_a_view_stays_watched(home: Path) -> None:
    """codex r3: everything inside the ~/.aws view counted as hidden, but ~/.aws/sso is the host
    directory bound back, so ~/.aws/sso/cache is host-backed: it must be prepared, ledgered and
    watched like any other mountpoint."""
    (home / ".aws" / "sso").mkdir(parents=True)
    policy = build_policy(_entity(home), deny_standard_creds=True)
    argv, _ = C._bwrap_plan(policy)
    mounted, _ = C._mount_plan_paths(argv, policy)
    assert mounted.get(str(home.resolve() / ".aws" / "sso" / "cache")) == "dir"
    assert str(home.resolve() / ".aws" / "credentials") not in mounted


def test_a_dangling_home_level_tool_dir_link_is_refused_with_the_reason(home: Path) -> None:
    (home / ".kube").symlink_to(home / "nowhere")
    with pytest.raises(ConfinementError, match="leads nowhere"):
        C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))


def test_a_workspace_that_is_home_itself_is_refused(home: Path) -> None:
    """complement r3: $HOME's top level is read-only inside bash, so a workspace there could not be
    written at its root."""
    with pytest.raises(ConfinementError, match="workspace"):
        C._bwrap_plan(build_policy(_entity(home), workspace=home))


def test_a_mask_nested_under_a_window_stays_hidden() -> None:
    """L1 r3: a tmpfs inside a window hides what is under it again; only the nearest mount counts."""
    class _P:
        own_memory_files = deny_files = deny_read_write = deny_write_files = sqlite_sidecars = ()
        socket_spellings = deny_sockets = ()
        config_file = None

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        aws = Path(d).resolve() / "aws"
        (aws / "sso").mkdir(parents=True)
        argv = ["--tmpfs", str(aws), "--bind-try", str(aws / "sso"), str(aws / "sso"),
                "--tmpfs", str(aws / "sso" / "cache"),
                "--ro-bind", "/dev/null", str(aws / "sso" / "cache" / "tok.json")]
        mounted, _ = C._mount_plan_paths(argv, _P())
    assert str(aws / "sso" / "cache") in mounted
    assert str(aws / "sso" / "cache" / "tok.json") not in mounted


@_live
def test_linux_live_a_netrc_the_host_creates_during_a_running_command_is_unreadable(home: Path) -> None:
    """Ruling 2026-10-07 (b): no window in which a $HOME-level cred file the host creates mid-session
    is readable, not even by a command already running when it appears."""
    import threading
    import time

    ent = _entity(home)
    shell = C.BwrapProvider().spawn_shell(build_policy(ent, workspace=ent / "workspace",
                                                       deny_standard_creds=True))

    def operator_logs_in() -> None:
        time.sleep(1.5)
        (home / ".netrc").write_text("machine example.com login u password SECRET-NETRC\n")

    t = threading.Thread(target=operator_logs_in)
    t.start()
    try:
        out = shell.run(f"for i in $(seq 1 40); do cat {home}/.netrc 2>/dev/null && break; "
                        "sleep 0.1; done; echo END", timeout=30).output
    finally:
        t.join()
        shell.close()
    assert (home / ".netrc").exists(), "the host side did create it"
    assert "SECRET-NETRC" not in out and "END" in out


@_live
def test_linux_live_home_view_cred_absent_workspace_and_store_as_before(home: Path) -> None:
    """Head checks on the $HOME view: an existing $HOME-level cred file is ABSENT inside (not an
    empty file); the workspace and the entity's own store under $HOME keep their modes; and any entry
    the host adds at the top of $HOME mid-session is simply not seen."""
    (home / ".netrc").write_text("machine x password SECRET-EXISTING\n")
    ent = _entity(home)
    shell = C.BwrapProvider().spawn_shell(build_policy(ent, workspace=ent / "workspace",
                                                       deny_standard_creds=True))
    try:
        (home / "added-later").write_text("")
        out = shell.run(f"ls -a {home}; test -e {home}/.netrc && echo NETRC-PRESENT; echo END",
                        timeout=20).output
        assert "NETRC-PRESENT" not in out and ".netrc" not in out and "added-later" not in out
        assert _live_rc(shell, f"touch {ent}/workspace/w") == "0"
        assert _live_rc(shell, f"touch {ent}/.levain/new-top-level") != "0"
        assert _live_rc(shell, f"mkdir -p {ent}/.levain/sub && touch {ent}/.levain/sub/x") in ("0", "1")
    finally:
        shell.close()
    assert (ent / "workspace" / "w").exists() and not (ent / ".levain" / "new-top-level").exists()


def test_an_absent_workspace_directly_in_home_is_in_the_view(home: Path, monkeypatch) -> None:
    """codex closing pass: the workspace was created after the plan read $HOME, so one directly in
    $HOME was missing from the view and bash's cwd sat on a covered directory."""
    ent = _entity(home)
    ws = home / "newws"
    policy = build_policy(ent, workspace=ws)
    made: list = []
    monkeypatch.setattr(C, "_prepare_mountpoints", lambda mounted, made=None: None)
    argv, *_ = C.BwrapProvider()._prepare(policy, made)
    w = str(ws.resolve())
    assert ["--bind", w, w] in [argv[i:i + 3] for i in range(len(argv) - 2)]
