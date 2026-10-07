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
    for var in ("AWS_LOGIN_CACHE_DIRECTORY", "CLOUDSDK_CONFIG", "AZURE_CONFIG_DIR", "XDG_RUNTIME_DIR"):
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


# --- Linux: tool directories are mounted read-only, $HOME-level links refuse ---------------------


def test_linux_plan_masks_a_link_in_a_tool_dir_at_its_target_without_refusing(home: Path) -> None:
    link, target = _stow_kube(home)
    (home / ".kube" / "cache").mkdir()
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    kube = str((home / ".kube").resolve())
    assert [kube, kube] in _ops(argv, "--ro-bind")
    assert [str(home.resolve() / ".kube" / "cache")] * 2 in _ops(argv, "--bind-try")
    masked = [d for s, d in _ops(argv, "--ro-bind") if s == "/dev/null"]
    assert str(target.resolve()) in masked
    assert str(link) not in masked                     # a mount cannot land on the link
    assert len(masked) == len(set(masked))             # one mask per destination
    # the read-only bind comes before every mask, so a mask lands on top of it
    assert argv.index("--ro-bind") < argv.index("/dev/null")


def test_linux_plan_refuses_a_home_level_cred_link(home: Path) -> None:
    target = home / "dotfiles" / "netrc"
    target.parent.mkdir()
    target.write_text("machine x login y password z\n")
    (home / ".netrc").symlink_to(target)
    with pytest.raises(ConfinementError, match="symlink in a directory this user can write"):
        C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))


def test_linux_plan_refuses_a_symlinked_tool_dir(home: Path) -> None:
    (home / "dotfiles" / "kube").mkdir(parents=True)
    (home / ".kube").symlink_to(home / "dotfiles" / "kube")
    with pytest.raises(ConfinementError, match="symlink"):
        C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))


def test_linux_plan_refuses_a_symlinked_cred_subtree_in_a_writable_dir(home: Path) -> None:
    (home / "dotfiles" / "gh").mkdir(parents=True)
    (home / ".config").mkdir()
    (home / ".config" / "gh").symlink_to(home / "dotfiles" / "gh")
    with pytest.raises(ConfinementError, match="symlink in a directory this user can write"):
        C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))


def test_linux_plan_creates_an_absent_tool_dir_and_puts_no_file_in_it(home: Path) -> None:
    argv, create_first = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    for tool in (".kube", ".docker", ".aws", ".config/git"):
        d = str(home.resolve() / tool)
        assert d in create_first, tool
        assert [d, d] in _ops(argv, "--ro-bind"), tool
    masked = [d for s, d in _ops(argv, "--ro-bind") if s == "/dev/null"]
    for f in (".kube/config", ".docker/config.json", ".aws/credentials", ".config/git/credentials"):
        assert str(home.resolve() / f) not in masked, f"{f}: a 0444 stub inside a tool dir"
    # a $HOME-level file has no tool dir to hide in, so it is still masked (with a session placeholder)
    assert str(home.resolve() / ".netrc") in masked


def test_mount_plan_records_home_level_cred_files_as_placeholders(home: Path) -> None:
    policy = build_policy(_entity(home), deny_standard_creds=True)
    argv, _ = C._bwrap_plan(policy)
    mounted, _ = C._mount_plan_paths(argv, policy)
    assert mounted[str(home.resolve() / ".netrc")] == "file"


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


def test_the_banner_names_the_home_level_files_that_will_be_placeholders(home: Path) -> None:
    (home / ".npmrc").write_text("registry=x\n")
    note = C.session_placeholder_note(True, system="Linux")
    assert note is not None
    assert "~/.netrc" in note and "~/.pypirc" in note and "~/.git-credentials" in note
    assert "~/.npmrc" not in note                     # present: masked over the real file, no placeholder
    assert "~/.kube/config" not in note               # in a read-only tool dir: no placeholder at all
    assert C.session_placeholder_note(True, system="Darwin") is None
    assert C.session_placeholder_note(False, system="Linux") is None


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


def test_a_shell_releases_its_claim_only_once_its_process_group_is_gone(home: Path, monkeypatch) -> None:
    f = home / ".netrc"
    C._prepare_mountpoints({str(f): "file"})
    C._ledger_enter([(str(f), "file")], {str(f)}, f"{os.getpid()}:-:-:k")
    shell = C._BwrapShell(policy=None, manifest={}, argv=["/bin/true"], cwd=home, env={})  # type: ignore[arg-type]
    shell._ledger_claim = f"{os.getpid()}:-:-:k"

    class _P:
        pid = 4242

        def poll(self):
            return 0
    shell._proc = _P()
    monkeypatch.setattr(C, "_group_gone", lambda pgid, timeout: False)
    shell.close()
    assert f.exists(), "the namespace may still hold the mount: keep the claim"
    shell2 = C._BwrapShell(policy=None, manifest={}, argv=["/bin/true"], cwd=home, env={})  # type: ignore[arg-type]
    shell2._ledger_claim = f"{os.getpid()}:-:-:k"
    shell2._proc = _P()
    monkeypatch.setattr(C, "_group_gone", lambda pgid, timeout: True)
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


