"""M2 options H and A: `levain ws-git`'s repository checks and its liveness gate, `ws-put`,
`ws-adopt` as an import, the doctor's ownership scan and git warnings, and the Linux mask repair
command. Real git on temporary repositories, and the hands-side scripts run as the current user (the
sudo prefix is the only thing replaced); no sudo, no sandbox. The cross-user behaviour runs in CI
(tests/ci/isolation_e2e.sh)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from levain.firing import hands, ws_git
from levain.firing.ws_git import WsGitError, check_repo, find_gitdir, foreign_entries

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
    argv = ws_git.ws_git_argv(h, tmp_path / "r" / ".git", ["log", "-1"])
    assert f"--git-dir={tmp_path / 'r' / '.git'}" in argv and f"--work-tree={tmp_path / 'r'}" in argv
    assert "safe.bareRepository=explicit" in argv and "gpg.program=false" in argv
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


def _read_only(tree: Path) -> None:
    for root, dirs, files in os.walk(tree, topdown=False):
        for f in files:
            if not (Path(root) / f).is_symlink():
                (Path(root) / f).chmod(0o444)
        for d in dirs:
            if not (Path(root) / d).is_symlink():
                (Path(root) / d).chmod(0o555)
    tree.chmod(0o555)


def _writable(tree: Path) -> None:
    tree.chmod(0o755)
    for root, dirs, _files in os.walk(tree):
        for d in dirs:
            if not (Path(root) / d).is_symlink():
                (Path(root) / d).chmod(0o755)


def _scan(tmp_path: Path, monkeypatch, uid: int) -> list[Path]:
    _no_sudo(monkeypatch)                      # the walk runs "as the hands user": here, as me
    return foreign_entries(_hands(tmp_path, uid=uid))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
def test_the_scan_reports_every_entry_the_hands_user_does_not_own_or_the_operator_can_write(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    deep = ws / "d1" / "d2" / "d3" / "d4" / "d5" / "d6"
    deep.mkdir(parents=True)                                   # no depth limit
    (deep / "f").write_text("x")
    (ws / "bare").mkdir()                                     # an empty folder: where a bare repo would go
    (ws / "link").symlink_to("/etc")                           # a link: judged by its owner, never followed
    dirs = [ws.joinpath(*["d1", "d2", "d3", "d4", "d5", "d6"][:i]) for i in range(1, 7)]
    everything = {ws, deep / "f", ws / "bare", ws / "link", *dirs}
    (deep / "foreign").mkdir()
    _read_only(ws)
    try:
        assert _scan(tmp_path, monkeypatch, ME) == []          # all the "hands user's", none writable to me
        assert _scan(tmp_path, monkeypatch, ME + 1) == [ws]   # the workspace itself not the hands user's: a finding, not entered
        deep.chmod(0o755)                                      # a folder deep down opened up to the operator
        assert _scan(tmp_path, monkeypatch, ME) == [deep]
        deep.chmod(0o555)
        (ws / "bare").chmod(0o777)
        assert _scan(tmp_path, monkeypatch, ME) == [ws / "bare"]
    finally:
        _writable(ws)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_a_scan_that_cannot_walk_the_tree_fails_closed(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    (ws / "locked" / "inner").mkdir(parents=True)
    (ws / "locked").chmod(0)                   # the owner itself shut out: the walk errors
    try:
        with pytest.raises(WsGitError, match="scan of the workspace"):
            _scan(tmp_path, monkeypatch, ME)
    finally:
        (ws / "locked").chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_a_foreign_folder_the_hands_user_cannot_enter_is_a_finding_not_a_failed_scan(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    (ws / "x" / "inner").mkdir(parents=True)
    (ws / "x").chmod(0)                         # "the operator's" (see uid below), closed to the walker
    try:
        assert _scan(tmp_path, monkeypatch, ME + 1) == [ws]   # pruned at the first foreign entry: no error
    finally:
        (ws / "x").chmod(0o755)


def test_the_scan_refuses_to_answer_for_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ws_git.os, "geteuid", lambda: 0)
    with pytest.raises(WsGitError, match="not as root"):
        _scan(tmp_path, monkeypatch, ME)


def test_a_file_only_its_owner_can_read_is_no_finding(tmp_path: Path, monkeypatch) -> None:
    # Linux: the operator's named entry is cut by the mask on a 0600 file; the hands-side walk
    # still sees it, so ordinary entity work is not a standing FAIL.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "private").write_text("x")
    (ws / "private").chmod(0o400)
    (ws / "closed").mkdir(mode=0o500)
    _read_only(ws)
    try:
        assert _scan(tmp_path, monkeypatch, ME) == []
    finally:
        _writable(ws)


def test_the_bare_repository_setting_is_read_from_the_operators_git(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    cfg.write_text("")
    assert ws_git.bare_repository_explicit() is False
    cfg.write_text("[safe]\n\tbareRepository = explicit\n")
    assert ws_git.bare_repository_explicit() is True
    real = subprocess.run
    monkeypatch.setattr(ws_git.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "git version 2.37.1\n", "") if argv[-1] == "--version" else real(argv, **kw))
    assert ws_git.bare_repository_explicit() is False      # before 2.38 the key protects nothing


# --- the liveness gate --------------------------------------------------------------------------


def test_a_user_with_a_process_running_is_live_and_one_without_is_not() -> None:
    assert ws_git.entity_session_live(ME) is True              # this test runs as ME
    assert ws_git.entity_session_live(4_000_017) is False      # no such user


def test_liveness_fails_closed_when_the_process_table_cannot_be_read(monkeypatch) -> None:
    monkeypatch.setattr(ws_git.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 3, "", "pgrep: cannot read"))
    with pytest.raises(WsGitError, match="cannot tell"):
        ws_git.entity_session_live(4_000_017)


def test_on_macos_only_launchds_own_per_user_agents_do_not_count_as_live(monkeypatch) -> None:
    monkeypatch.setattr(ws_git.platform, "system", lambda: "Darwin")
    state = {"pgrep": ["101\n102\n"], "ps": ""}

    def fake_run(argv, **kw):
        if argv[0].endswith("pgrep"):
            out = state["pgrep"][0] if len(state["pgrep"]) == 1 else state["pgrep"].pop(0)
            return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, state["ps"], "")

    monkeypatch.setattr(ws_git.subprocess, "run", fake_run)
    xpc = "/System/Library/Frameworks/NetFS.framework/Versions/A/XPCServices/PlugInLibraryService.xpc/Contents/MacOS/PlugInLibraryService"
    state["ps"] = f"  101     1 /usr/sbin/cfprefsd\n  102     1 {xpc}\n"
    assert ws_git.entity_session_live(4_000_017) is False
    for rows in ("  101     1 /usr/sbin/cfprefsd\n  102     1 /bin/sh\n",                 # an orphan of the entity's
                 "  101     1 /usr/sbin/cfprefsd\n  102     1 /usr/bin/python3\n",
                 "  101     1 /usr/sbin/cfprefsd\n  102     1 /System/Library/Frameworks/Ruby.framework/x/ruby\n",
                 "  101     1 /usr/sbin/cfprefsd\n  102   555 /usr/sbin/distnoted\n"):    # not launchd's child
        state["ps"] = rows
        assert ws_git.entity_session_live(4_000_017) is True, rows
    state["ps"] = "  101     1 /usr/sbin/cfprefsd\n"                                    # 102 gone in between
    with pytest.raises(WsGitError, match="changed while being read"):
        ws_git.entity_session_live(4_000_017)
    state["ps"] = f"  101     1 /usr/sbin/cfprefsd\n  102     1 {xpc}\n"
    state["pgrep"] = ["101\n102\n", "101\n102\n103\n"]                               # 103 forked in between
    with pytest.raises(WsGitError, match="changed while being read"):
        ws_git.entity_session_live(4_000_017)


def test_a_proc_mounted_hidepid_means_liveness_cannot_be_told() -> None:
    base = "22 1 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw"
    assert ws_git.proc_hides_processes(base + ",hidepid=invisible\n") is True
    assert ws_git.proc_hides_processes(base + ",hidepid=2\n") is True
    assert ws_git.proc_hides_processes(base + ",hidepid=0\n") is False
    assert ws_git.proc_hides_processes(base + "\n") is False


def _hands(tmp_path: Path, uid: int = ME) -> ws_git.Hands:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (tmp_path / ".levain").mkdir(exist_ok=True)   # tmp_path doubles as the entity: the hands lock lives here
    return ws_git.Hands("_levain_x_abcdef", uid, str(tmp_path), ws)


def _no_sudo(monkeypatch) -> None:
    """Run the hands side as the current user: only the sudo prefix changes."""
    monkeypatch.setattr(ws_git, "_as_hands", lambda h, *argv, env=(), group=None: [
        "/usr/bin/env", "-i", f"HOME={h.home}", f"PATH={ws_git.SECURE_PATH}", *env, *argv])


def test_ws_git_refuses_while_the_entitys_user_runs_anything(tmp_path: Path, monkeypatch, capsys) -> None:
    h = _hands(tmp_path)                       # uid = this test's own, so a process is running
    _repo(h.workspace / "r")
    ran = []
    monkeypatch.setattr(ws_git, "load_hands", lambda e: h)
    monkeypatch.setattr(ws_git, "_run_relayed", lambda argv: ran.append(argv) or 0)
    assert ws_git.cmd_ws_git(tmp_path, h.workspace / "r", ["status"]) == 1
    assert "processes running" in capsys.readouterr().out and ran == []
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: False)
    assert ws_git.cmd_ws_git(tmp_path, h.workspace / "r", ["status"]) == 0 and len(ran) == 1


# --- ws-put -------------------------------------------------------------------------------------


@pytest.mark.parametrize("dest", ["../x", "a/../../x", "/etc/passwd", "", "a//b", "./x", "a/."])
def test_ws_put_refuses_a_destination_that_could_leave_the_workspace(tmp_path: Path, dest) -> None:
    with pytest.raises(WsGitError):
        ws_git.put_parts(tmp_path / "ws", dest)


def test_ws_put_names_a_destination_relative_to_the_workspace_or_absolute_inside_it(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert ws_git.put_parts(ws, "a/b.txt") == ["a", "b.txt"]
    assert ws_git.put_parts(ws, str(ws / "a" / "b.txt")) == ["a", "b.txt"]
    with pytest.raises(WsGitError, match="not inside"):
        ws_git.put_parts(ws, str(tmp_path / "wsx" / "b.txt"))


def _put(h: ws_git.Hands, src: Path, dest: str) -> int:
    return ws_git.cmd_ws_put(h.home, src, dest)


@pytest.fixture
def put_env(tmp_path: Path, monkeypatch):
    h = _hands(tmp_path, uid=4_000_017)        # the source checks look for the hands user's folders
    _no_sudo(monkeypatch)
    monkeypatch.setattr(ws_git, "load_hands", lambda e: h)
    src = tmp_path / "src.sh"
    src.write_text("#!/bin/sh\necho hi\n")
    src.chmod(0o755)
    return h, src


def test_ws_put_writes_data_never_a_program_and_creates_missing_folders(put_env) -> None:
    h, src = put_env
    assert _put(h, src, "new/dir/run.sh") == 0
    out = h.workspace / "new" / "dir" / "run.sh"
    assert out.read_text() == src.read_text() and out.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in out.parent.iterdir()] == ["run.sh"]              # no temp file left


def test_ws_put_stops_at_a_symlinked_folder_and_writes_nothing_through_it(put_env, tmp_path: Path, capsys) -> None:
    h, src = put_env
    outside = tmp_path / "outside"
    outside.mkdir()
    (h.workspace / "a").mkdir()
    (h.workspace / "a" / "link").symlink_to(outside)
    assert _put(h, src, "a/link/x") == 1
    assert list(outside.iterdir()) == [] and "could not write" in capsys.readouterr().out
    h.workspace.joinpath("top").symlink_to(outside)
    assert _put(h, src, "top/x") == 1 and list(outside.iterdir()) == []


def test_ws_put_replaces_a_symlink_at_the_destination_instead_of_writing_through_it(put_env, tmp_path: Path) -> None:
    h, src = put_env
    target = tmp_path / "victim"
    target.write_text("keep")
    (h.workspace / "f").symlink_to(target)
    assert _put(h, src, "f") == 0
    assert target.read_text() == "keep"
    assert not (h.workspace / "f").is_symlink() and (h.workspace / "f").read_text() == src.read_text()


def test_ws_put_refuses_a_source_inside_a_workspace_and_a_non_file(put_env, tmp_path: Path, monkeypatch) -> None:
    h, src = put_env
    monkeypatch.setitem(ws_git.WORKSPACE_ROOT, "darwin", h.workspace)
    (h.workspace / "planted").write_text("x")       # a file in the workspace, not yours to hand in
    assert _put(h, h.workspace / "planted", "copy") == 1 and not (h.workspace / "copy").exists()
    assert _put(h, tmp_path, "dir-copy") == 1 and not (h.workspace / "dir-copy").exists()


def test_ws_put_refuses_a_source_reached_through_a_link_or_a_second_name(put_env, tmp_path: Path, capsys) -> None:
    h, src = put_env
    (tmp_path / "via-link").symlink_to(src)          # a link of my own is followed (someone else's is not:
    assert _put(h, tmp_path / "via-link", "a") == 0  # see the source test below)
    (h.workspace / "a").unlink()
    os.link(src, tmp_path / "hard")                  # a hard link: the same file under a second name
    assert _put(h, tmp_path / "hard", "b") == 1 and "no hard link" in capsys.readouterr().out
    os.unlink(tmp_path / "hard")
    assert _put(h, src, "c") == 0                    # control: one name again, accepted
    assert not (h.workspace / "a").exists() and not (h.workspace / "b").exists()


def test_ws_put_refuses_a_fifo_without_waiting_on_it(put_env, tmp_path: Path) -> None:
    h, _src = put_env
    os.mkfifo(tmp_path / "fifo")
    assert _put(h, tmp_path / "fifo", "f") == 1


def test_a_source_under_a_folder_of_the_entitys_or_a_link_someone_else_made_is_refused(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    (tmp_path / "l").symlink_to(tmp_path / "d")
    ws_git._check_operator_source(tmp_path / "d" / "f", _hands(tmp_path, uid=4_000_017))   # control
    with pytest.raises(WsGitError, match="belongs to the entity"):
        ws_git._check_operator_source(tmp_path / "d" / "f", _hands(tmp_path, uid=ME))
    monkeypatch.setattr(ws_git.os, "getuid", lambda: 4_000_018)  # the link is now someone else's
    with pytest.raises(WsGitError, match="link another user made"):
        ws_git._check_operator_source(tmp_path / "l" / "f", _hands(tmp_path, uid=4_000_017))


# --- ws-adopt: an import ------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def adopt_env(tmp_path: Path, monkeypatch):
    h = _hands(tmp_path)
    _no_sudo(monkeypatch)
    monkeypatch.setattr(ws_git, "load_hands", lambda e: h)
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: False)
    # here "the hands user" is this test's own uid, which owns the source too; the source check has
    # its own test above
    monkeypatch.setattr(ws_git, "_pin_operator_source",
                        lambda src, hands, want_dir: os.open(src, os.O_RDONLY | os.O_DIRECTORY))
    monkeypatch.setattr(ws_git, "_hands_can_write", lambda hands, tree: None)   # "the hands user" is me here
    src = tmp_path / "mine"
    _repo(src)
    _git(src, "checkout", "-q", "-b", "main")
    (src / "a").write_text("a")
    _git(src, "add", "a")
    _git(src, "commit", "-q", "-m", "one")
    for b in ("side", "feature/x"):
        _git(src, "checkout", "-q", "-b", b)
        (src / b.replace("/", "_")).write_text(b)
        _git(src, "add", ".")
        _git(src, "commit", "-q", "-m", b)
    _git(src, "checkout", "-q", "main")
    _git(src, "tag", "v1")
    _git(src, "remote", "add", "origin", "git@github.com:o/r.git")
    _git(src, "remote", "add", "local", "/some/path")
    _git(src, "remote", "add", "tok", "https://me:ghp_secret@github.com/o/r.git")
    (src / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\ntouch /tmp/never\n")
    return h, src


def test_ws_adopt_imports_every_branch_and_tag_and_leaves_the_original_alone(adopt_env, capsys) -> None:
    h, src = adopt_env
    before = _git(src, "for-each-ref")
    assert ws_git.cmd_ws_adopt(h.home, src) == 0, capsys.readouterr().out
    dest = h.workspace / "mine"
    assert _git(dest, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags") == \
        _git(src, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags")
    assert _git(dest, "symbolic-ref", "HEAD") == "refs/heads/main" and (dest / "a").read_text() == "a"
    assert _git(dest, "remote") == "origin"              # a local-path remote and one with a token are dropped
    assert not (dest / ".git" / "hooks").exists()                             # no hooks, not even samples
    assert _git(src, "for-each-ref") == before and (src / ".git").is_dir()    # the original is untouched


def test_ws_adopt_refuses_when_a_branch_did_not_come_across_and_keeps_nothing(adopt_env, monkeypatch, capsys) -> None:
    h, src = adopt_env
    monkeypatch.setattr(ws_git, "_IMPORT_SCRIPT", ws_git._IMPORT_SCRIPT.replace("'+refs/heads/*:refs/heads/*'",
                                                                              "'+refs/heads/main:refs/heads/main'"))
    assert ws_git.cmd_ws_adopt(h.home, src) == 1
    assert "did not all come across" in capsys.readouterr().out and not (h.workspace / "mine").exists()


def test_ws_adopt_checks_its_source_like_ws_put(adopt_env, monkeypatch, capsys) -> None:
    h, src = adopt_env
    monkeypatch.setattr(ws_git, "_pin_operator_source", lambda s, hh, want_dir: (_ for _ in ()).throw(WsGitError("chosen")))
    assert ws_git.cmd_ws_adopt(h.home, src) == 1 and "chosen" in capsys.readouterr().out


def test_ws_adopt_refuses_a_repository_inside_a_workspace_and_while_live(adopt_env, monkeypatch, capsys) -> None:
    h, src = adopt_env
    monkeypatch.setitem(ws_git.WORKSPACE_ROOT, "darwin", src.parent)
    assert ws_git.cmd_ws_adopt(h.home, src) == 1 and "inside an entity's workspace" in capsys.readouterr().out
    monkeypatch.setitem(ws_git.WORKSPACE_ROOT, "darwin", Path("/Users/Shared/levain"))
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: True)
    assert ws_git.cmd_ws_adopt(h.home, src) == 1 and "processes running" in capsys.readouterr().out
    assert not (h.workspace / "mine").exists()


@pytest.mark.parametrize("entry,hit", [
    ("*", True), ("/Users/Shared/*", True), ("/Users/Shared/levain/*", True),
    ("/Users/Shared/levain/_levain_x_abcdef/workspace/r", True), ("/some/path", False), ("/Users/Sh/*", False),
])
def test_a_safe_directory_entry_covering_the_workspace_is_found(tmp_path: Path, monkeypatch, entry, hit) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[safe]\n\tdirectory = /some/other\n\tdirectory = {entry}\n")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    found = ws_git.wildcard_safe_directory((Path("/Users/Shared/levain"),))
    assert (found == [f"file:{cfg}"]) is hit


def test_ws_git_output_is_stripped_of_terminal_control_bytes() -> None:
    out = ws_git._sanitise(b"ok\tline\n\x1b]0;title\x07\x1b[2Jcaf\xc3\xa9\r\x00")
    assert out == b"ok\tline\n]0;title[2Jcaf\xc3\xa9"


def test_the_mask_repair_runs_as_the_owner_on_its_own_files_only(tmp_path: Path) -> None:
    h = ws_git.Hands("_levain_x_abcdef", 950, "/var/lib/levain-hands/_levain_x_abcdef", tmp_path)
    argv = ws_git.mask_repair_argv(h)
    assert argv[:4] == ["/usr/bin/sudo", "-n", "-u", h.user]
    assert argv[argv.index("-user") + 1] == h.user and "m::rX" in argv
    # after a hands command: only what changed since it started (RUN 2026-10-09, chmod 600 hid a file)
    since = ws_git.mask_repair_argv(h, since=1760000000.7)
    assert since[since.index("-newerct") + 1] == "@1760000000"


def _doctor_env(tmp_path: Path, monkeypatch):
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
    monkeypatch.setattr(ws_git, "wildcard_safe_directory", lambda roots=(): [])
    monkeypatch.setattr(ws_git, "bare_repository_explicit", lambda: True)
    return doctor, ed


def test_doctor_fails_on_anything_in_the_workspace_the_entity_does_not_own(tmp_path: Path, monkeypatch) -> None:
    doctor, ed = _doctor_env(tmp_path, monkeypatch)
    monkeypatch.setattr(ws_git, "foreign_entries", lambda h: [h.workspace / "folder"])
    monkeypatch.setattr(ws_git, "wildcard_safe_directory", lambda roots=(): ["file:/etc/gitconfig"])
    results = doctor._check_hands_isolation(ed)
    assert not results[0].ok and "do not belong to the entity" in results[0].detail
    assert results[1].ok and results[1].warn and "EVERY repository" in results[1].detail


def test_doctor_warns_unless_the_operators_git_takes_bare_repositories_only_explicitly(tmp_path: Path, monkeypatch) -> None:
    doctor, ed = _doctor_env(tmp_path, monkeypatch)
    monkeypatch.setattr(ws_git, "foreign_entries", lambda h: [])
    assert [r.name for r in doctor._check_hands_isolation(ed)] == ["hands isolation"]
    monkeypatch.setattr(ws_git, "bare_repository_explicit", lambda: False)
    main, bare = doctor._check_hands_isolation(ed)
    assert main.ok and bare.ok and bare.warn and bare.hint == "git config --global safe.bareRepository explicit"


# --- the hands lock: no session starts mid-ws-git, no ws-git mid-session ---------------------------


def test_ws_git_and_a_session_exclude_each_other_through_the_hands_lock(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    fd = ws_git.hold_session_lock(tmp_path)
    fd2 = ws_git.hold_session_lock(tmp_path, wait=0)           # several sessions at once: shared
    try:
        with pytest.raises(WsGitError, match="session of this entity is open"):
            with ws_git._exclusive(tmp_path):
                pass
    finally:
        os.close(fd)
        os.close(fd2)
    with ws_git._exclusive(tmp_path):
        with pytest.raises(WsGitError, match="working in this entity's workspace"):
            ws_git.hold_session_lock(tmp_path, wait=0.3)
    os.close(ws_git.hold_session_lock(tmp_path, wait=0))       # free again once ws-git is done


def test_ws_git_refuses_while_a_session_holds_the_lock_and_runs_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    h = _hands(tmp_path, uid=4_000_017)
    _repo(h.workspace / "r")
    ran = []
    monkeypatch.setattr(ws_git, "load_hands", lambda e: h)
    monkeypatch.setattr(ws_git, "_run_relayed", lambda argv: ran.append(argv) or 0)
    fd = ws_git.hold_session_lock(tmp_path)
    try:
        assert ws_git.cmd_ws_git(tmp_path, h.workspace / "r", ["status"]) == 1
        assert "session of this entity is open" in capsys.readouterr().out and ran == []
    finally:
        os.close(fd)


def test_a_hands_entitys_session_holds_the_lock_from_open_to_close(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    import functools

    from levain import session as session_mod
    from levain.firing.hands import hands_user_name, hands_workspace, host_os
    from levain.session import EntitySession, SessionStartError

    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LEVAIN_ENTITY_DIR", raising=False)
    ent = tmp_path / "e"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    rec = {"hands_user": hands_user_name(ent), "hands_uid": 499,
           "hands_workspace": str(hands_workspace(host_os(), hands_user_name(ent)))}
    (ent / ".levain" / "confinement.json").write_text(json.dumps(rec))
    monkeypatch.setattr(session_mod, "hold_session_lock", functools.partial(ws_git.hold_session_lock, wait=0.3))
    # The REPL holds the lock too; a headless session would need the (here nonexistent) hands account.
    with ws_git._exclusive(ent):                                # ws-git running: the session refuses
        with pytest.raises(SessionStartError, match="working in this entity's workspace"):
            EntitySession.open(ent, model="m", base_url="http://127.0.0.1:9", with_tools=True, mode="interactive")
    s = EntitySession.open(ent, model="m", base_url="http://127.0.0.1:9", with_tools=True, mode="interactive")
    try:
        with pytest.raises(WsGitError, match="session of this entity is open"):
            with ws_git._exclusive(ent):
                pass
    finally:
        s.close()
    with ws_git._exclusive(ent):                                # closed: ws-git may run
        pass


def test_ws_put_takes_the_hands_lock_and_the_liveness_gate(put_env, tmp_path: Path, monkeypatch, capsys) -> None:
    h, src = put_env
    fd = ws_git.hold_session_lock(tmp_path)
    try:
        assert _put(h, src, "x") == 1 and "session of this entity is open" in capsys.readouterr().out
    finally:
        os.close(fd)
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: True)
    assert _put(h, src, "x") == 1 and "processes running" in capsys.readouterr().out
    assert not (h.workspace / "x").exists()


def _src_repo(tmp_path: Path) -> Path:
    src = tmp_path / "srcrepo"
    _repo(src)
    return src


@pytest.mark.parametrize("plant", ["other-writable", "group-writable", "gitfile", "alternates", "include", "worktrees"])
def test_ws_adopt_lets_your_git_read_only_a_repository_nobody_else_could_have_written(tmp_path: Path, monkeypatch, plant) -> None:
    src = _src_repo(tmp_path)
    h = _hands(tmp_path, uid=4_000_017)
    monkeypatch.setattr(ws_git, "_hands_can_write", lambda hands, tree: None)
    check = lambda: ws_git._check_source_repo(os.open(src, os.O_RDONLY | os.O_DIRECTORY), src, h)  # noqa: E731
    check()                                                              # control: clean
    g = src / ".git"
    if plant == "other-writable":
        (g / "config").chmod(0o646)
    elif plant == "group-writable":
        (g / "refs").chmod(0o775)
    elif plant == "gitfile":
        shutil.rmtree(g)
        g.write_text("gitdir: /elsewhere\n")
    elif plant == "alternates":
        (g / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (g / "objects" / "info" / "alternates").write_text("/x\n")
    elif plant == "include":
        _set(g, "include.path", "/tmp/x")
    else:
        (g / "worktrees").mkdir()
    with pytest.raises(WsGitError):
        check()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
def test_the_hands_user_is_asked_itself_whether_it_can_write_the_repository(tmp_path: Path, monkeypatch) -> None:
    src = _src_repo(tmp_path)
    _no_sudo(monkeypatch)                                   # "the hands user" is me: it owns, so it can write
    h = _hands(tmp_path)
    assert ws_git._hands_can_write(h, src / ".git") == str(src)          # the parent first: it could rename .git
    _read_only(src)
    try:
        assert ws_git._hands_can_write(h, src / ".git") is None
        (src / ".git" / "refs").chmod(0o755)
        assert ws_git._hands_can_write(h, src / ".git") == str(src / ".git" / "refs")
        (src / ".git" / "refs").chmod(0o555)
        (src / ".git" / "objects").chmod(0o311)                       # it can see in, but not walk: no answer
        with pytest.raises(WsGitError, match="could not ask"):
            ws_git._hands_can_write(h, src / ".git")
    finally:
        _writable(src)
    assert ws_git._hands_can_write(h, tmp_path / "nowhere" / ".git") is None   # cannot see it: cannot write it


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_ws_adopt_refuses_when_the_name_no_longer_leads_to_the_pinned_repository(tmp_path: Path, monkeypatch) -> None:
    src = _src_repo(tmp_path)
    rfd = os.open(src, os.O_RDONLY | os.O_DIRECTORY)

    def swap(hands, tree):                                   # during the probe, the repository is renamed away
        os.rename(src, tmp_path / "away")
        _repo(src)
        return None

    monkeypatch.setattr(ws_git, "_hands_can_write", swap)
    with pytest.raises(WsGitError, match="changed while it was being checked"):
        ws_git._check_source_repo(rfd, src, _hands(tmp_path, uid=4_000_017))


@pytest.mark.parametrize("name,url,ok", [
    ("origin", "git@github.com:o/r.git", True), ("origin", "ssh://git@github.com/o/r.git", True),
    ("origin", "https://github.com/o/r.git", True), ("origin", "https://me:tok@github.com/o/r.git", False),
    ("origin", "ssh://oauth-token@example.com/o/r.git", False), ("corp.prod", "git@github.com:o/r.git", False),
    ("origin", "/local/path", False),
])
def test_only_remotes_that_carry_no_login_and_that_ws_git_accepts_are_copied(name, url, ok) -> None:
    assert ws_git._remote_ok(name, url) is ok


def test_ws_adopt_checks_the_repository_before_your_git_reads_it(adopt_env, capsys) -> None:
    h, src = adopt_env
    (src / ".git" / "config").chmod(0o666)
    assert ws_git.cmd_ws_adopt(h.home, src) == 1 and "not yours alone" in capsys.readouterr().out
    assert not (h.workspace / "mine").exists()


def test_a_path_that_runs_through_a_link_is_a_finding_not_an_answer(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "d").mkdir(parents=True)
    (real / "d").chmod(0o555)
    (tmp_path / "via").symlink_to(real)
    try:
        assert ws_git._writable_or_moved(real / "d") is False      # control: same object, real path, read-only
        assert ws_git._writable_or_moved(tmp_path / "via" / "d") is True
    finally:
        (real / "d").chmod(0o755)


def test_a_source_under_a_folder_anyone_can_swap_entries_in_is_refused_unless_sticky(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    (drop / "job").mkdir(parents=True)
    (drop / "job" / "input").write_text("x")
    h = _hands(tmp_path, uid=4_000_017)
    drop.chmod(0o777)                                   # others could rename job and put another in its place
    try:
        with pytest.raises(WsGitError, match="anyone can swap"):
            ws_git._check_operator_source(drop / "job" / "input", h)
        drop.chmod(0o1777)                              # sticky: only an entry's owner may move it
        ws_git._check_operator_source(drop / "job" / "input", h)
    finally:
        drop.chmod(0o755)


def test_ws_adopt_refuses_a_repository_the_hands_user_says_it_can_write(tmp_path: Path, monkeypatch) -> None:
    src = _src_repo(tmp_path)
    monkeypatch.setattr(ws_git, "_hands_can_write", lambda hands, tree: str(tree / "config"))
    with pytest.raises(WsGitError, match="entity's user can write"):
        ws_git._check_source_repo(os.open(src, os.O_RDONLY | os.O_DIRECTORY), src, _hands(tmp_path, uid=4_000_017))


@pytest.mark.parametrize("teardown_ok", [True, False])
def test_a_session_keeps_the_hands_lock_until_its_teardown_has_succeeded(tmp_path: Path, teardown_ok) -> None:
    from levain.session import EntitySession

    class Conv:
        def close(self):
            if not teardown_ok:
                raise RuntimeError("the shell did not stop")

    (tmp_path / ".levain").mkdir()
    s = EntitySession(entity_dir=tmp_path, binding=None, conversation=Conv(), workspace=tmp_path,
                      model_label="m", with_tools=True, bash_ok=True,
                      hands_lock_fd=ws_git.hold_session_lock(tmp_path))
    s.close()
    s.close()                                                   # idempotent: never a second os.close
    if teardown_ok:
        with ws_git._exclusive(tmp_path):
            pass
    else:
        with pytest.raises(WsGitError, match="session of this entity is open"):
            with ws_git._exclusive(tmp_path):
                pass
        os.close(s.hands_lock_fd)


@pytest.mark.parametrize("left", [None, "processes of _levain_x_000000 are still running (pid 4242)"])
def test_a_hands_session_keeps_its_locks_while_anything_of_its_user_runs(tmp_path: Path, monkeypatch, left) -> None:
    """S2 L3 r2 (codex HIGH): close() of the shell dropped a group it could not empty, and the session
    then released the workspace lock. The session's end now stops the hands user's processes and
    empties the shells' groups; while anything is left, both locks stay held."""
    from levain.firing import confinement as C
    from levain.session import EntitySession

    calls: list[str] = []
    monkeypatch.setattr(C, "sweep_hands_user", lambda user, **kw: calls.append(user) or left)
    monkeypatch.setattr(C, "unemptied_shell_groups", lambda user: [])

    class Conv:
        def close(self):
            pass

    (tmp_path / ".levain").mkdir()
    s = EntitySession(entity_dir=tmp_path, binding=None, conversation=Conv(), workspace=tmp_path,
                      model_label="m", with_tools=True, bash_ok=True,
                      hands_lock_fd=ws_git.hold_session_lock(tmp_path),
                      hands_session_fd=ws_git.hold_hands_session(tmp_path), hands_user="_levain_x_000000")
    s.close()
    assert calls == ["_levain_x_000000"] and s.left_running == left
    if left is None:
        with ws_git._exclusive(tmp_path):
            pass
        os.close(ws_git.hold_hands_session(tmp_path))
    else:
        with pytest.raises(WsGitError, match="session of this entity is open"):
            with ws_git._exclusive(tmp_path):
                pass
        with pytest.raises(WsGitError, match="one runs at a time"):
            ws_git.hold_hands_session(tmp_path)
        os.close(s.hands_lock_fd)
        os.close(s.hands_session_fd)


def test_unemptied_shell_groups_keep_a_hands_session_locked(tmp_path: Path, monkeypatch) -> None:
    """A closed shell of this hands user still holding a group keeps the lock; another user's does not."""
    from levain.firing import confinement as C
    from levain.session import _stop_hands_user

    class Shell:
        def __init__(self, user, groups):
            self.hands_user, self.unemptied_groups = user, groups

        def close(self):
            pass

    monkeypatch.setattr(C, "sweep_hands_user", lambda user, **kw: None)
    mine, theirs = Shell("_levain_a_000000", (4242,)), Shell("_levain_b_000000", (4343,))
    monkeypatch.setattr(C, "_UNEMPTIED_SHELLS", {mine, theirs})
    assert "4242" in (_stop_hands_user("_levain_a_000000", 4_000_017) or "")
    assert _stop_hands_user("_levain_c_000000", 4_000_018) is None


def test_a_second_session_running_as_the_hands_user_is_refused(tmp_path: Path) -> None:
    """(d): the session's end stops every process of the hands user, which is safe only while one
    session owns it, so a second is refused rather than having its processes killed by the first."""
    (tmp_path / ".levain").mkdir()
    fd = ws_git.hold_hands_session(tmp_path)
    try:
        with pytest.raises(WsGitError, match="one runs at a time"):
            ws_git.hold_hands_session(tmp_path)
        # The REPL's shared hands lock is a different lock: it is not refused.
        os.close(ws_git.hold_session_lock(tmp_path))
    finally:
        os.close(fd)
    os.close(ws_git.hold_hands_session(tmp_path))


def test_an_interrupted_stop_keeps_the_locks_and_close_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """S2d codex HIGH: the wall-clock TurnTimeout (a BaseException) landing inside the end-of-session
    stop escaped close(), skipped the lock handling, and close() could not be retried."""
    from levain.firing import confinement as C
    from levain.firing.deadline import TurnTimeout
    from levain.session import EntitySession

    def interrupted(user, **kw):
        raise TurnTimeout(30)

    monkeypatch.setattr(C, "sweep_hands_user", interrupted)

    class Conv:
        def close(self):
            pass

    (tmp_path / ".levain").mkdir()
    s = EntitySession(entity_dir=tmp_path, binding=None, conversation=Conv(), workspace=tmp_path,
                      model_label="m", with_tools=True, bash_ok=True,
                      hands_lock_fd=ws_git.hold_session_lock(tmp_path),
                      hands_session_fd=ws_git.hold_hands_session(tmp_path), hands_user="_levain_x_000000")
    s.close()
    assert "interrupted (TurnTimeout)" in (s.left_running or "")
    with pytest.raises(WsGitError, match="one runs at a time"):
        ws_git.hold_hands_session(tmp_path)
    os.close(s.hands_lock_fd)
    os.close(s.hands_session_fd)


def test_a_failed_teardown_still_stops_the_hands_user_and_keeps_the_locks(tmp_path: Path, monkeypatch) -> None:
    """r3 codex HIGH: when conversation.close() raised, the stop was skipped, so a setsid child kept
    running past the session. The stop runs whatever the teardown did; the locks stay either way."""
    from levain.firing import confinement as C
    from levain.session import EntitySession

    swept: list[str] = []
    monkeypatch.setattr(C, "sweep_hands_user", lambda user, **kw: swept.append(user))
    monkeypatch.setattr(C, "unemptied_shell_groups", lambda user: [])

    class Conv:
        def close(self):
            raise RuntimeError("the executor did not close")

    (tmp_path / ".levain").mkdir()
    s = EntitySession(entity_dir=tmp_path, binding=None, conversation=Conv(), workspace=tmp_path,
                      model_label="m", with_tools=True, bash_ok=True,
                      hands_lock_fd=ws_git.hold_session_lock(tmp_path),
                      hands_session_fd=ws_git.hold_hands_session(tmp_path), hands_user="_levain_x_000000")
    s.close()
    assert swept == ["_levain_x_000000"]
    with pytest.raises(WsGitError, match="one runs at a time"):
        ws_git.hold_hands_session(tmp_path)
    os.close(s.hands_lock_fd)
    os.close(s.hands_session_fd)


def test_on_linux_only_network_verbs_get_the_net_group_and_ssh_reads_no_config(tmp_path: Path, monkeypatch) -> None:
    """D (Phill 2026-10-09): the egress boundary lets out only the net group's gid, so only the verbs
    that reach a remote get it, and ssh offers the deploy key with no ~/.ssh/config (the entity writes
    that file; a ProxyCommand there would run with the net gid)."""
    monkeypatch.setattr(ws_git, "_real_git", lambda: "/usr/bin/git")
    h = _hands(tmp_path)
    gitdir = h.workspace / "r" / ".git"
    for args, net in ((["push", "origin", "main"], True), (["ls-remote"], True), (["-c", "x.y=z", "fetch"], False),
                      (["status"], False), (["-C", "push", "log"], False), (["--super-prefix", "push", "status"], False)):
        argv = ws_git.ws_git_argv(h, gitdir, args, system="Linux")
        assert (("-g", f"{h.user}_net") == tuple(argv[4:6])) is net, args
    assert "-g" not in ws_git.ws_git_argv(h, gitdir, ["push"], system="Darwin")[:6]
    argv = ws_git.ws_git_argv(h, gitdir, ["push"], system="Linux")
    assert f"core.sshCommand=/usr/bin/ssh -F /dev/null -i {h.home}/.ssh/id_ed25519 -o IdentitiesOnly=yes" in argv
