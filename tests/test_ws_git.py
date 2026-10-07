"""M2 option H: `levain ws-git`'s repository checks, the doctor's workspace scan, the wildcard
safe.directory warning and the Linux mask repair command. Real git on temporary repositories; no
sudo, no sandbox."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.firing import hands, ws_git
from levain.firing.ws_git import WsGitError, check_repo, find_gitdir, operator_owned_gitdirs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
ME = os.getuid()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path / ".git"


def _set(gitdir: Path, key: str, value: str) -> None:
    subprocess.run(["git", "config", "--file", str(gitdir / "config"), key, value], check=True)


def test_find_gitdir_walks_up_inside_the_workspace_only(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    g = _repo(ws / "repo")
    (ws / "repo" / "a" / "b").mkdir(parents=True)
    assert find_gitdir(ws / "repo" / "a" / "b", ws) == g
    with pytest.raises(WsGitError, match="not inside"):
        find_gitdir(tmp_path, ws)
    (ws / "plain").mkdir()
    with pytest.raises(WsGitError, match="no repository"):
        find_gitdir(ws / "plain", ws)


def test_find_gitdir_refuses_a_gitfile_or_a_linked_gitdir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "r1").mkdir(parents=True)
    (ws / "r1" / ".git").write_text("gitdir: /somewhere/else\n")
    with pytest.raises(WsGitError, match="not a plain directory"):
        find_gitdir(ws / "r1", ws)
    real = _repo(tmp_path / "elsewhere")
    (ws / "r2").mkdir()
    (ws / "r2" / ".git").symlink_to(real)
    with pytest.raises(WsGitError, match="not a plain directory"):
        find_gitdir(ws / "r2", ws)


def test_a_clean_repo_owned_by_the_hands_user_passes(tmp_path: Path) -> None:
    g = _repo(tmp_path / "r")
    _set(g, "remote.origin.url", "git@github.com:o/r.git")
    _set(g, "branch.main.remote", "origin")
    check_repo(g, ME)


def test_a_repo_the_hands_user_does_not_own_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WsGitError, match="does not belong"):
        check_repo(_repo(tmp_path / "r"), ME + 1)


@pytest.mark.parametrize("key,value", [
    ("core.fsmonitor", "touch /tmp/x"), ("core.hooksPath", "/tmp/h"), ("core.pager", "sh"),
    ("core.sshCommand", "sh"), ("alias.st", "!sh"), ("include.path", "/tmp/c"),
    ("includeIf.gitdir:/.path", "/tmp/c"), ("filter.x.clean", "sh"), ("diff.x.textconv", "sh"),
    ("credential.helper", "sh"), ("core.attributesFile", "/tmp/a"), ("core.worktree", "/"),
])
def test_any_config_key_outside_the_allowlist_is_refused(tmp_path: Path, key, value) -> None:
    g = _repo(tmp_path / "r")
    _set(g, key, value)
    with pytest.raises(WsGitError, match="can name a program"):
        check_repo(g, ME)


@pytest.mark.parametrize("url", ["ext::sh -c touch% /tmp/x", "/local/path", "file:///x", "fd::17"])
def test_a_remote_url_outside_the_accepted_schemes_is_refused(tmp_path: Path, url) -> None:
    g = _repo(tmp_path / "r")
    _set(g, "remote.origin.url", url)
    with pytest.raises(WsGitError, match="remote URL"):
        check_repo(g, ME)


@pytest.mark.parametrize("name", ["commondir", "worktrees", "modules"])
def test_linked_worktrees_and_submodules_are_refused(tmp_path: Path, name) -> None:
    g = _repo(tmp_path / "r")
    (g / name).mkdir()
    with pytest.raises(WsGitError, match=name):
        check_repo(g, ME)


def test_borrowed_objects_are_refused(tmp_path: Path) -> None:
    g = _repo(tmp_path / "r")
    (g / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (g / "objects" / "info" / "alternates").write_text("/elsewhere/objects\n")
    with pytest.raises(WsGitError, match="alternates"):
        check_repo(g, ME)


def test_ws_git_runs_as_the_hands_user_with_no_global_config_and_hooks_off(tmp_path: Path) -> None:
    h = ws_git.Hands("_levain_x_abcdef", 499, "/Users/_levain_x_abcdef", tmp_path)
    argv = ws_git.ws_git_argv(h, tmp_path / "r", ["log", "-1"])
    assert argv[:5] == ["/usr/bin/sudo", "-n", "-u", "_levain_x_abcdef", "/usr/bin/env"] and argv[5] == "-i"
    assert "GIT_CONFIG_NOSYSTEM=1" in argv and "GIT_CONFIG_GLOBAL=/dev/null" in argv
    for setting in ("core.hooksPath=/dev/null", "core.fsmonitor=", "core.pager=cat", "credential.helper="):
        assert setting in argv
    assert argv[-2:] == ["log", "-1"]


def test_the_neutralised_settings_really_switch_hooks_off(tmp_path: Path) -> None:
    # The argv's -c settings, run by real git as the current user: a pre-commit hook does not fire.
    g = _repo(tmp_path / "r")
    canary = tmp_path / "canary"
    hook = g / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\n")
    hook.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run(["git", *ws_git._NEUTRALISE, "-C", str(g.parent), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "x"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not canary.exists()
    r = subprocess.run(["git", "-C", str(g.parent), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "y"], env=env, capture_output=True, text=True)
    assert canary.exists()  # control: without the settings the hook fires


def test_the_scan_finds_repositories_the_hands_user_does_not_own(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    g1, g2 = _repo(ws / "a"), _repo(ws / "deep" / "b")
    assert sorted(operator_owned_gitdirs(ws, ME + 1)) == sorted([g1, g2])
    assert operator_owned_gitdirs(ws, ME) == []


def test_a_wildcard_safe_directory_is_found_with_its_origin(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[safe]\n\tdirectory = /some/path\n\tdirectory = *\n")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    found = ws_git.wildcard_safe_directory()
    assert found == [f"file:{cfg}"]
    cfg.write_text("[safe]\n\tdirectory = /some/path\n")
    assert ws_git.wildcard_safe_directory() == []


def test_the_mask_repair_runs_as_the_owner_on_its_own_files_only(tmp_path: Path) -> None:
    h = ws_git.Hands("_levain_x_abcdef", 950, "/var/lib/levain-hands/_levain_x_abcdef", tmp_path)
    argv = ws_git.mask_repair_argv(h)
    assert argv[:4] == ["/usr/bin/sudo", "-n", "-u", h.user]
    assert argv[argv.index("-user") + 1] == h.user and "m::rwX" in argv


def test_doctor_fails_on_an_operator_owned_repo_in_the_workspace(tmp_path: Path, monkeypatch) -> None:
    from levain import doctor

    ed = tmp_path / "e"
    (ed / ".levain").mkdir(parents=True)
    ws = Path("/Users/Shared/levain") / hands.hands_user_name(ed) / "workspace"
    me = hands.pwd.getpwuid(ME)
    rec = {"hands_user": hands.hands_user_name(ed), "hands_uid": ME, "hands_workspace": str(ws)}
    (ed / ".levain" / "confinement.json").write_text(json.dumps(rec))
    monkeypatch.setattr(hands, "host_os", lambda: "darwin")
    monkeypatch.setattr(doctor, "_probe", lambda cmd: (True, ""))
    import pwd as _pwd
    monkeypatch.setattr(_pwd, "getpwnam", lambda n: me)
    monkeypatch.setattr(ws_git, "operator_owned_gitdirs", lambda w, uid: [w / "repo" / ".git"])
    monkeypatch.setattr(ws_git, "wildcard_safe_directory", lambda: ["file:/etc/gitconfig"])
    results = doctor._check_hands_isolation(ed)
    assert not results[0].ok and "belong to you" in results[0].detail and "ws-adopt" in results[0].hint
    assert results[1].ok and results[1].warn and "EVERY repository" in results[1].detail
