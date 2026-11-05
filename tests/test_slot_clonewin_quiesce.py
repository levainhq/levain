"""1105-32 lane 1d (Finding 4): the macOS V4 clone window quiesces the slot uid's launchd domain before its first
proof and never tolerates a process.

Fake seams only: a process table the fake ``kill_domain`` empties (or does not), no real launchd, uid or root.
The real bootout, the real ``distnoted`` and a second macOS are daytime VM rows (RETURN_clonewin.md).
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from levain.slot import proc as PR
from levain.slot import start as SS


class Table:
    """A fake slot-uid process table: ``alive`` names, ``agents`` the ones a bootout removes."""

    def __init__(self, alive, agents=("distnoted",), bootout_ok=True, empties=True):
        self.alive, self.agents, self.bootout_ok, self.empties = list(alive), set(agents), bootout_ok, empties
        self.kills = 0

    def no_process(self) -> bool:
        return not self.alive

    def kill_domain(self, ctx, a, children=()):
        self.kills += 1
        if not self.bootout_ok:
            return PR.Domain(False, "kill-unproven"), frozenset()
        if self.empties:
            self.alive = [p for p in self.alive if p not in self.agents]
        return PR.PROVEN, frozenset()


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


def test_the_last_window_stays_a_detection(sfd, monkeypatch):
    """An agent that appears while the check children run is refused, not quieted: no second quiesce."""
    t = Table([])

    def run(argv, bound):
        t.alive.append("distnoted")
        return b""
    q = _quiesce(monkeypatch, t)
    # the empty read set runs no children, so drive the post-children probe with a scripted predicate instead
    calls = iter([True, False])
    with pytest.raises(SS.CheckFailed, match="after the last check child"):
        SS.clone_check(sfd, "dispatch", run, lambda: next(calls), spec_base=lambda: 0, quiesce=q)
    assert t.kills == 1


def test_linux_has_no_quiesce(monkeypatch):
    t = Table(["distnoted"])
    assert _quiesce(monkeypatch, t, host="linux") is None
    assert t.kills == 0


def test_other_kinds_never_quiesce(sfd, monkeypatch):
    t = Table(["distnoted"])
    q = _quiesce(monkeypatch, t)
    SS.clone_check(sfd, "planner", lambda a, b: b"", t.no_process, spec_base=lambda: 0, quiesce=q)
    assert t.kills == 0
