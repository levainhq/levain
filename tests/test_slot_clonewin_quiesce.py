"""1105-32 lane 1d (Finding 4) and 1105-7 window 2 (DESIGN_window2): the macOS V4 clone window quiesces the slot uid's
launchd domain before its first proof and again after ``ls-files`` (the last ``asuser`` child, which recreates the
uid's ``distnoted``), root hashes the worktree itself with no child, and no process is ever tolerated.

Fake seams only: a process table the fake ``kill_domain`` empties (or does not), no real launchd, uid or root.
The real bootout, the real ``distnoted`` and a second macOS are daytime VM rows (RETURN_clonewin.md).
"""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from levain.slot import proc as PR
from levain.slot import start as SS
from levain.slot import wall


class Table:
    """A fake slot-uid process table: ``alive`` names, ``agents`` the ones a bootout removes."""

    def __init__(self, alive, agents=("distnoted",), bootout_ok=True, empties=True, fail_on_kill=None):
        self.alive, self.agents, self.bootout_ok, self.empties = list(alive), set(agents), bootout_ok, empties
        self.kills = 0
        self.fail_on_kill = fail_on_kill      # the Nth kill (1-based) answers an unproven domain

    def no_process(self) -> bool:
        return not self.alive

    def kill_domain(self, ctx, a, children=()):
        self.kills += 1
        if self.fail_on_kill == self.kills:
            return PR.Domain(False, "respawn-after-bootout"), frozenset()
        if not self.bootout_ok:
            return PR.Domain(False, "kill-unproven"), frozenset()
        if self.empties:
            self.alive = [p for p in self.alive if p not in self.agents]
        return PR.PROVEN, frozenset()


@pytest.fixture
def root(tmp_path):
    """The session dir ``sfd`` is open on."""
    return str(tmp_path)


@pytest.fixture
def sfd(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(SS, "check_attributes", lambda fd: None)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    yield fd
    os.close(fd)


def _check(sfd, t, quiesce):
    return SS.clone_check(sfd, "dispatch", lambda argv, bound: b"", t.no_process, spec_base=lambda: 0,
                          quiesce=quiesce)


def _quiesce(monkeypatch, t, host="darwin"):
    monkeypatch.setattr(SS, "kill_domain", t.kill_domain)
    ctx = SimpleNamespace(seams=SimpleNamespace(host=host))
    return SS._window_quiesce(ctx, SimpleNamespace())


def test_red_without_quiesce_an_agent_after_the_clone_refuses(sfd):
    """The 23c79b16 behaviour: distnoted alive as the slot uid, no quiesce, refuses."""
    t = Table(["distnoted"])
    with pytest.raises(SS.CheckFailed, match="before the check"):
        _check(sfd, t, None)


def test_agent_after_the_clone_passes_after_the_quiesce(sfd, monkeypatch):
    t = Table(["distnoted"])
    stats = _check(sfd, t, _quiesce(monkeypatch, t))
    assert stats is not None and t.kills == 1 and t.alive == []


def test_a_bootout_that_fails_refuses(sfd, monkeypatch):
    t = Table(["distnoted"], bootout_ok=False)
    with pytest.raises(SS.CheckFailed, match="not proven ended: kill-unproven"):
        _check(sfd, t, _quiesce(monkeypatch, t))


def test_a_bootout_that_raises_is_not_a_pass(sfd, monkeypatch):
    t = Table(["distnoted"])

    def boom(ctx, a, children=()):
        raise SS.Refused("respawn-after-bootout", "x")
    monkeypatch.setattr(SS, "kill_domain", boom)
    q = SS._window_quiesce(SimpleNamespace(seams=SimpleNamespace(host="darwin")), SimpleNamespace())
    with pytest.raises(SS.Refused):
        _check(sfd, t, q)


def test_a_uid_that_never_empties_refuses(sfd, monkeypatch):
    t = Table(["distnoted"], empties=False)
    with pytest.raises(SS.CheckFailed, match="before the check"):
        _check(sfd, t, _quiesce(monkeypatch, t))
    assert t.kills == 1


def test_a_non_agent_process_still_refuses_after_the_quiesce(sfd, monkeypatch):
    """A detached git gc (or an asuser process that outlives the bootout) is not removed: the predicate is unchanged."""
    t = Table(["distnoted", "git-gc"])
    with pytest.raises(SS.CheckFailed, match="before the check"):
        _check(sfd, t, _quiesce(monkeypatch, t))
    assert t.alive == ["git-gc"]


def test_the_last_window_stays_a_detection(root, sfd, monkeypatch):
    """Without a root hash (the Linux shape) an agent that appears while the check children run is refused, not
    quieted: no second quiesce. The child is a real ``hash-object`` batch answering the real id, and the process table
    is the one the production predicate reads (``t.no_process``)."""
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])
    run = _fake_run(root, files, t, on_hash=lambda: t.alive.append("distnoted"))
    q = _quiesce(monkeypatch, t)
    with pytest.raises(SS.CheckFailed, match="after the last check child"):
        SS.clone_check(sfd, "dispatch", run, t.no_process, spec_base=lambda: 0, quiesce=q)
    assert t.kills == 1


# --- window 2 on macOS (DESIGN_window2) ----------------------------------------------------------------------------

def _git_id(data: bytes) -> str:
    return subprocess.run(["git", "hash-object", "--no-filters", "--stdin"], input=data, capture_output=True,
                          check=True, cwd="/").stdout.decode().strip()


def _write(root, files, mode=0o644):
    """Real files under ``<sfd>/repo``; answers ``{path: (sha, mode string)}`` with real git's ids."""
    out = {}
    for path, data in files.items():
        full = os.path.join(root, "repo", path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        os.chmod(full, mode)
        out[path] = (_git_id(data), "100755" if mode & 0o100 else "100644")
    return out


def _record(files) -> bytes:
    return b"".join(f"{mode} {sha} 0\t{p}".encode() + b"\0" for p, (sha, mode) in files.items())


def _fake_run(root, files, t, on_ls=None, on_hash=None, calls=None):
    """The check children as fakes: ``ls-files`` answers the real record; ``hash-object`` real git over the worktree
    as it is NOW. ``on_*`` run when that child runs (a recreated agent, a write); no child may run after window 2's
    kill when ``t.kills >= 2``."""
    repo = os.path.join(root, "repo")

    def run(argv, bound):
        if calls is not None:
            calls.append(tuple(argv))
        assert t.kills < 2, "a check child ran after window 2's quiesce"
        if tuple(argv) == wall.ls_files_argv():
            rec = _record(files)
            if on_ls:
                on_ls()
            return rec
        n = len(wall.HASH_OBJECT_PREFIX)
        assert tuple(argv[:n]) == wall.HASH_OBJECT_PREFIX
        out = subprocess.run(["git", "hash-object", "--no-filters", "--", *argv[n:]], capture_output=True, check=True,
                             cwd=repo).stdout
        if on_hash:
            on_hash()
        return out
    return run


def _mac(sfd, t, run, monkeypatch, root_hash=None):
    q = _quiesce(monkeypatch, t)
    rh = root_hash or (lambda fd, ents: SS.hash_worktree_at(fd, ents, os.getuid()))
    return SS.clone_check(sfd, "dispatch", run, t.no_process, spec_base=lambda: 0, quiesce=q, root_hash=rh)


def test_macos_ls_files_recreates_the_agent_and_the_start_passes(root, sfd, monkeypatch):
    """r1-r10 (the vmsample): the last ``asuser`` child recreates ``distnoted``. Window 2 ends the domain again, so
    the start passes: two bootouts, one child."""
    files = _write(root, {"a.txt": b"one\n", "d/b.txt": b"two\n"})
    t = Table([])
    calls = []
    run = _fake_run(root, files, t, on_ls=lambda: t.alive.append("distnoted"), calls=calls)
    stats = _mac(sfd, t, run, monkeypatch)
    assert stats is not None and (stats.entries, stats.batches) == (2, 0) and stats.bytes_hashed == 8
    assert t.kills == 2 and t.alive == [] and calls == [wall.ls_files_argv()]


def test_macos_a_process_before_the_quiesce_is_noted_not_refused(root, sfd, monkeypatch, capsys):
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])
    _mac(sfd, t, _fake_run(root, files, t, on_ls=lambda: t.alive.append("distnoted")), monkeypatch)
    assert "window 2: slot-uid process(es) present before the quiesce" in capsys.readouterr().err


def test_macos_no_check_child_runs_after_window_2(root, sfd, monkeypatch):
    """No ``hash-object`` child on macOS at all: ``ls-files`` is the only argv the fake launcher ever sees."""
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])
    calls = []
    _mac(sfd, t, _fake_run(root, files, t, calls=calls), monkeypatch)
    assert calls and not any(c[:len(wall.HASH_OBJECT_PREFIX)] == wall.HASH_OBJECT_PREFIX for c in calls)


def test_macos_a_write_during_ls_files_is_caught_by_root_hash(root, sfd, monkeypatch):
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])
    path = os.path.join(root, "repo", "a.txt")

    def rewrite():
        t.alive.append("distnoted")
        with open(path, "wb") as f:
            f.write(b"two\n")
    with pytest.raises(SS.CheckFailed, match="blob"):
        _mac(sfd, t, _fake_run(root, files, t, on_ls=rewrite), monkeypatch)


def test_macos_window_2_unproven_refuses(root, sfd, monkeypatch):
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([], fail_on_kill=2)
    with pytest.raises(SS.CheckFailed, match="not proven ended: respawn-after-bootout"):
        _mac(sfd, t, _fake_run(root, files, t), monkeypatch)
    assert t.kills == 2


def test_macos_a_process_after_the_root_hash_refuses(root, sfd, monkeypatch):
    """Nothing is launched after the proof, so a process the final detection sees is refused, never quieted again."""
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])

    def hasher(fd, ents):
        n = SS.hash_worktree_at(fd, ents, os.getuid())
        t.alive.append("git-gc")
        return n
    with pytest.raises(SS.CheckFailed, match="after the last check child"):
        _mac(sfd, t, _fake_run(root, files, t), monkeypatch, root_hash=hasher)
    assert t.kills == 2


def test_root_hash_without_a_quiesce_is_a_programming_error(root, sfd):
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])
    with pytest.raises(ValueError):
        SS.clone_check(sfd, "dispatch", _fake_run(root, files, t), t.no_process, spec_base=lambda: 0,
                       root_hash=lambda fd, ents: 0)


def test_linux_has_no_quiesce(monkeypatch):
    t = Table(["distnoted"])
    assert _quiesce(monkeypatch, t, host="linux") is None
    assert t.kills == 0


def test_other_kinds_never_quiesce(sfd, monkeypatch):
    t = Table(["distnoted"])
    q = _quiesce(monkeypatch, t)
    SS.clone_check(sfd, "planner", lambda a, b: b"", t.no_process, spec_base=lambda: 0, quiesce=q)
    assert t.kills == 0
