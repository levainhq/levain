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
from levain.slot import render as RD
from levain.slot import start as SS
from levain.slot import wall

#: The real (a) check, kept before the ``sfd`` fixture replaces it with a no-op.
_CHECK_ATTRIBUTES = SS.check_attributes


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


# --- the Linux closing window over the real cgroup proof (L4B_RUN, row 136) ---------------------------------------

_CG = "/sys/fs/cgroup/levain.slice/e0.scope/s-0000000000000001"
_SLOT = 899


class _Cgroup:
    """A fake kernel for :class:`proc.CgroupProcs`: after the last check child, ``cgroup.events`` reads ``populated
    1`` for ``lag`` reads, then 0; no process of the slot uid anywhere unless ``survivor``."""

    def __init__(self, lag, survivor_after=False, helper=False):
        self.lag, self.survivor_after, self.survivor = lag, survivor_after, False
        self.t, self.after, self.lists = 0.0, None, 0
        self.helper_after, self.helper = helper, False   # a live slot-uid member left in the cgroup (codex cgrace 1)
        self.lists_at_finish = None

    def finish_children(self):
        self.after = 0
        self.survivor = self.survivor_after
        self.helper = self.helper_after
        self.lists_at_finish = self.lists

    def read(self, path):
        if self.helper and self.t > 0:
            self.helper = False                           # it exits during the first sleep of the wait
        if path == _CG + "/cgroup.threads":
            return "4441\n" if self.helper else ""
        assert path == _CG + "/cgroup.events"
        if self.after is None:
            return "populated 0\nfrozen 0\n"
        self.after += 1
        if self.helper:
            return "populated 1\nfrozen 0\n"
        return "populated 1\nfrozen 0\n" if self.after <= self.lag else "populated 0\nfrozen 0\n"

    def probe(self, pid):
        return PR.Proc(pid, (_SLOT,) * 3, 1, PR.RUN) if self.helper and pid == 4441 else None

    def listing(self):
        self.lists += 1
        return [PR.Proc(4440, (_SLOT,) * 3, 1, PR.RUN)] if self.survivor else []

    def sleep(self, s):
        self.t += s

    def no_process(self):
        seams = PR.ProcSeams(listing=self.listing, probe=self.probe, pin=lambda pid: pid,
                             send=lambda h, s: None, read=self.read, clock=lambda: self.t, sleep=self.sleep,
                             self_uids=lambda: (0, 0), self_pid=lambda: 1)
        return PR.CgroupProcs(_CG, seams).no_process(_SLOT)


def _linux_check(root, sfd, k):
    files = _write(root, {"a.txt": b"one\n"})
    run = _fake_run(root, files, SimpleNamespace(kills=0), on_hash=k.finish_children)
    return SS.clone_check(sfd, "dispatch", run, k.no_process, spec_base=lambda: 0)


@pytest.mark.parametrize("lag", [1, 2])
def test_linux_the_populated_lag_after_the_last_child_passes(root, sfd, lag):
    """The soak refusal: ``populated 1`` for a read or two after the last check child's reap, nothing alive."""
    k = _Cgroup(lag)
    assert _linux_check(root, sfd, k) is not None
    assert k.after == lag + 1 and k.t < PR.NO_PROCESS_SETTLE_S and k.lists == 4   # both windows' uid sweeps ran


def test_linux_a_survivor_past_the_settle_bound_still_refuses_with_the_same_text(root, sfd):
    k = _Cgroup(10 ** 6)
    with pytest.raises(SS.CheckFailed) as e:
        _linux_check(root, sfd, k)
    assert str(e.value) == "window: a slot-uid process is alive after the last check child"
    assert k.t >= PR.NO_PROCESS_SETTLE_S


def test_linux_a_uid_survivor_after_the_lag_still_refuses(root, sfd):
    """``populated`` settles to 0 but a slot-uid process outside the cgroup is alive: the uid sweep still runs."""
    k = _Cgroup(1, survivor_after=True)
    with pytest.raises(SS.CheckFailed) as e:
        _linux_check(root, sfd, k)
    assert str(e.value) == "window: a slot-uid process is alive after the last check child"
    assert k.lists_at_finish == 2               # window 1 ran (one clean uid sweep) before the last check child
    assert k.after == 2 and k.lists >= 3


def test_linux_a_live_helper_that_exits_during_the_wait_still_refuses(root, sfd):
    """codex cgrace 1 (HIGH): a slot-uid helper outlives the last check child (``populated 1``), could change the
    hashed tree, and exits before the old 250 ms wait ended. The wait absorbs only reaping lag: it refuses at once."""
    k = _Cgroup(1, helper=True)
    with pytest.raises(SS.CheckFailed) as e:
        _linux_check(root, sfd, k)
    assert str(e.value) == "window: a slot-uid process is alive after the last check child"
    assert k.lists_at_finish == 2 and k.lists == 2 and k.t == 0   # no wait, no uid sweep after the hash


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
    """An argument check: it fails before any check child runs (L3_win2_r1 complement LOW 1)."""
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])
    calls = []
    with pytest.raises(ValueError):
        SS.clone_check(sfd, "dispatch", _fake_run(root, files, t, calls=calls), t.no_process, spec_base=lambda: 0,
                       root_hash=lambda fd, ents: 0)
    assert calls == []


def _real_attributes(root, monkeypatch):
    """The real (a) check over a real ``repo/.git/info/attributes`` holding render's line."""
    info = os.path.join(root, "repo", ".git", "info")
    os.makedirs(info)
    path = os.path.join(info, "attributes")
    with open(path, "wb") as f:
        f.write(RD.READSET_ATTRIBUTES)
    monkeypatch.setattr(SS, "check_attributes", _CHECK_ATTRIBUTES)
    return path


def test_macos_attributes_proven_twice_pass(root, sfd, monkeypatch):
    _real_attributes(root, monkeypatch)
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])
    assert _mac(sfd, t, _fake_run(root, files, t, on_ls=lambda: t.alive.append("distnoted")), monkeypatch)


def test_macos_attributes_rewritten_during_ls_files_refuses(root, sfd, monkeypatch):
    """L3_win2_r1 codex #1 / gemini MED: a process alive during ``ls-files`` and killed by window 2's quiesce rewrote
    ``info/attributes`` after its first proof; (a) runs again after the proof and refuses."""
    path = _real_attributes(root, monkeypatch)
    files = _write(root, {"a.txt": b"one\n"})
    t = Table([])

    def rewrite():
        t.alive.append("distnoted")
        with open(path, "wb") as f:
            f.write(b"* filter=evil\n")
    with pytest.raises(SS.CheckFailed, match=r"\(a\) info/attributes"):
        _mac(sfd, t, _fake_run(root, files, t, on_ls=rewrite), monkeypatch)
    assert t.kills == 2


def test_linux_has_no_quiesce(monkeypatch):
    t = Table(["distnoted"])
    assert _quiesce(monkeypatch, t, host="linux") is None
    assert t.kills == 0


def test_other_kinds_never_quiesce(sfd, monkeypatch):
    t = Table(["distnoted"])
    q = _quiesce(monkeypatch, t)
    SS.clone_check(sfd, "planner", lambda a, b: b"", t.no_process, spec_base=lambda: 0, quiesce=q)
    assert t.kills == 0
