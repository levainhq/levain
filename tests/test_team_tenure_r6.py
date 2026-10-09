"""Two items the r5 fix lane stopped on (the 1009+6 seat's rulings, in r5's frame): each test is a run that failed on
eef283a.

The frame is r5's (git's lockfile + compare-and-swap): every move of the pin holds the ``net`` lock and then the worktree
lock, the order ``_sync`` takes them, and ``_sync`` reads the ledger it syncs only once it holds ``net``; a write of
``pending_ops`` changes only the ops its own call added or consumed, inside the state lock, never a snapshot of them.

G1 sync reads the pin after taking net · G2 join and G3 repin --root move the pin holding net · G4 a re-land never
erases a pending op another operation added meanwhile.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure)
import pytest

from levain.team.transport import BRANCH, WARNINGS, GitLedger, TeamBusy, TeamError
from tests.test_team_tenure import gl, keys, team, two  # noqa: F401


def _try_net(repo, where, seen):
    try:
        with gl(repo).lock(name="net", timeout=0.2):
            seen.append((where, "acquired"))
    except TeamBusy:
        seen.append((where, "busy"))


def test_g1_sync_reads_the_ledger_it_syncs_only_after_taking_net(two, monkeypatch):
    """seat item 1: `_sync` computed its remote ref and branch BEFORE the net lock, so a `join --root` landing while it
    waited synced the old ledger's remote into the new pin. Now everything it syncs is read under net."""
    tmp, ana, ben = two
    moved = BRANCH + "-moved"
    real_lock = GitLedger.lock
    fired, fetched = [], []

    def lock(self, *a, name="lock", **k):
        if name == "net" and not fired:
            fired.append(1)
            gl(ben).save_state(branch=moved)      # the pin moved while this sync waited for net
        return real_lock(self, *a, name=name, **k)
    real_fetch = GitLedger._fetch

    def fetch(self, remote, rref, *a, **k):
        fetched.append(rref)
        return real_fetch(self, remote, rref, *a, **k)
    monkeypatch.setattr(GitLedger, "lock", lock)
    monkeypatch.setattr(GitLedger, "_fetch", fetch)
    with pytest.raises(TeamError):
        gl(ben).sync(push=False)      # the moved branch is not on the remote: a pinned clone refuses, as it should
    assert fired and fetched, (fired, fetched)
    assert all(r.endswith("/" + moved) for r in fetched), fetched


def test_g2_join_moves_the_pin_holding_net(two, monkeypatch):
    """seat item 1: `join` held only the worktree lock, so a sync already past its own reads kept them. Now the net lock
    is held from the prospective validation through the worktree attach."""
    tmp, ana, ben = two
    seen = []
    from levain.team import tenure as T
    real_derive, real_attach = T.derive, GitLedger._attach_worktree
    fired = []

    def derive(*a, **k):
        out = real_derive(*a, **k)
        if not fired:
            fired.append(1)
            _try_net(ben, "validate", seen)
        return out

    def attach(self):
        _try_net(ben, "attach", seen)
        return real_attach(self)
    monkeypatch.setattr(T, "derive", derive)
    monkeypatch.setattr(GitLedger, "_attach_worktree", attach)
    assert team("join", "--no-install", repo=ben) == 0
    assert seen == [("validate", "busy"), ("attach", "busy")], seen


def test_g3_repin_root_moves_the_pin_holding_net(two, monkeypatch):
    """seat item 1: `repin --root` took no lock at all. Now it holds net (and the worktree lock) while it moves the pin."""
    tmp, ana, ben = two
    root = gl(ben).pinned_root
    seen = []
    real_save = GitLedger.save_state

    def save(self, _mutate=None, **changes):
        if "pinned_root" in changes or _mutate is not None:
            _try_net(ben, "repin", seen)
        return real_save(self, _mutate, **changes)
    monkeypatch.setattr(GitLedger, "save_state", save)
    assert team("repin", "--root", root, repo=ben) == 0
    assert seen == [("repin", "busy")], seen


def test_g4_a_reland_never_erases_a_pending_op_added_meanwhile(two, monkeypatch):
    """seat item 2: `_reland` saved `pending_ops` from its entry snapshot, so an op another operation queued while it
    ran was erased (C's stale-write shape, code L3 r5). Now it removes only the ops it consumed."""
    tmp, ana, ben = two
    g = gl(ben)
    root = g.pinned_root
    stale = {"root": root, "fields": [{"k": ["member", "zed"], "old": None, "new": {"x": 1}, "base": "never"}]}
    late = {"root": root, "fields": [], "late": True}
    g.save_state(pending_ops=[stale])
    real_derivation = GitLedger.derivation
    fired = []

    def derivation(self, *a, **k):
        out = real_derivation(self, *a, **k)
        if not fired:
            fired.append(1)
            gl(ben).save_state(_mutate=lambda st: st.__setitem__("pending_ops", list(st.get("pending_ops") or []) +
                                                                  [late]))
        return out
    monkeypatch.setattr(GitLedger, "derivation", derivation)
    WARNINGS.clear()
    gl(ben)._reland()
    assert fired
    ops = gl(ben).state().get("pending_ops") or []
    assert late in ops, ops
    assert stale not in ops, ops     # the consumed (refused) op is still removed
