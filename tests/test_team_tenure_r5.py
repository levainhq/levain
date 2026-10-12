"""Code L3 round 5 on the strict signed ledgers (f5f0751..fac2942): each test is a run that failed on fac2942.

The frame (the seat's research step): git's own answer to concurrent state transitions. ONE lock is held across the
whole read-validate-write, and each write is a compare-and-swap against the value it was validated on. Races are built
as deterministic interleavings: a hook between validate and persist performs the concurrent operation (no sleeps).

A1 join holds the ledger lock · A2 sync's held refs and pending ops carry the root captured at entry · B a join proves
the proposed signing key before it persists anything · C join's persist is a CAS · D regenesis --from refuses a commit
only the local branch head reaches · E a join whose key is no member's key exits 2.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure)
import inspect
import shutil

import pytest

from levain.team import signing as S
from levain.team import tenure as T
from levain.team.transport import WARNINGS, GitLedger, TeamBusy
from tests.test_team_tenure import clone, gl, keys, ruling, sh, team, two  # noqa: F401
from tests.test_team_tenure_rows_a import LB, ruling_np


def _heads(tmp):
    return sh("git", "ls-remote", "--heads", str(tmp / "origin.git"), cwd=tmp)


def _after_prospective_derive(monkeypatch, action):
    """Run ``action()`` once, right after join's prospective derivation returns (between validate and persist)."""
    real = T.derive
    fired = []

    def hooked(*a, **k):
        out = real(*a, **k)
        if not fired:
            fired.append(1)
            action()
        return out
    monkeypatch.setattr(T, "derive", hooked)
    return fired


def test_a1_join_holds_the_ledger_lock_from_validate_to_attach(two, monkeypatch):
    """codex r5 1 (HIGH): `join` took no worktree lock, so a sync or a writer could run against a half-moved pin. Now a
    concurrent writer finds the lock held from the prospective validation through the worktree attach."""
    tmp, ana, ben = two
    seen = []

    def try_lock(where):
        try:
            with gl(ben).lock(timeout=0.2):
                seen.append((where, "acquired"))
        except TeamBusy:
            seen.append((where, "busy"))
    _after_prospective_derive(monkeypatch, lambda: try_lock("validate"))
    real_attach = GitLedger._attach_worktree

    def attach(self):
        try_lock("attach")
        return real_attach(self)
    monkeypatch.setattr(GitLedger, "_attach_worktree", attach)
    assert team("join", "--no-install", repo=ben) == 0
    assert seen == [("validate", "busy"), ("attach", "busy")], seen


def _held_scenario(two, keys):
    """ben's offline entry is held: his signing key is rotated to one that is only PENDING (T42)."""
    tmp, ana, ben = two
    assert team("key", "add", "ben", str(keys["mal"]), repo=ben) == 0
    assert ruling_np(ben, "src/a.py", "ben: held entry") == 0
    assert ruling(ana, "src/c.py", "ana: meanwhile") == 0
    g = gl(ben)
    g.save_state(signing_key=str(keys["mal"]))
    return g.pinned_root


def test_a2_held_entries_are_filed_under_the_root_captured_at_entry(two, keys, monkeypatch):
    """codex r5 1: `_rebase_moves` re-read `self.pinned_root` when it filed a held entry, so a pin moved mid-sync
    (join --root, repin --root) filed genesis A's entry under B's held refs, for B's sync to replay. Now the root is
    captured once at entry."""
    tmp, ana, ben = two
    root = _held_scenario(two, keys)
    fake = "f" * 40
    real = GitLedger._pick_counts

    def pick_then_move_the_pin(self, d, c):
        out = real(self, d, c)
        self.save_state(pinned_root=fake)       # a concurrent pin move, after the pick was judged
        return out
    monkeypatch.setattr(GitLedger, "_pick_counts", pick_then_move_the_pin)
    try:
        gl(ben).sync(push=False)
    except Exception:       # the rest of the sync runs on a fake pin; what was filed is the question
        pass
    monkeypatch.setattr(GitLedger, "_pick_counts", real)
    refs = sh("git", "for-each-ref", "--format=%(refname)", "refs/levain/held/", cwd=ben).split()
    assert refs and all(r.startswith(f"refs/levain/held/{root}/") for r in refs), refs


def test_a2_sync_reads_the_pin_once_per_operation(two, keys, monkeypatch):
    """codex r5 1: `_rebase_moves`, `_reland` and `_replay_held` each read the pin at most ONCE in their own body
    (and `_held_ref` never reads it): every later use is of that captured, immutable value."""
    tmp, ana, ben = two
    _held_scenario(two, keys)
    watched = {"_rebase_moves", "_reland", "_replay_held", "_held_ref"}
    reads: dict[str, int] = {}
    prop = GitLedger.pinned_root

    def counted(self):
        caller = inspect.stack()[1].function
        if caller in watched:
            reads[caller] = reads.get(caller, 0) + 1
        return prop.fget(self)
    monkeypatch.setattr(GitLedger, "pinned_root", property(counted))
    for name in ("_rebase_moves", "_reland", "_replay_held"):
        real = getattr(GitLedger, name)

        def once(self, *a, _real=real, _name=name, **k):
            before = dict(reads)
            out = _real(self, *a, **k)
            own = reads.get(_name, 0) - before.get(_name, 0)
            assert own <= 1, f"{_name} read the pin {own} times"
            assert reads.get("_held_ref", 0) == before.get("_held_ref", 0), "_held_ref re-read the pin"
            return out
        monkeypatch.setattr(GitLedger, name, once)
    WARNINGS.clear()
    gl(ben).sync(push=False)
    assert sh("git", "for-each-ref", "refs/levain/held/", cwd=ben).strip(), "the scenario did not hold an entry"
    gl(ben).save_state(signing_key=str(keys["ben"]))
    gl(ben).sync(push=False)     # the replay path, with ben's in-force key


def test_b_join_proves_the_proposed_signing_key_before_persisting_anything(two, keys, tmp_path, capsys):
    """codex r5 3: `join --signing-key /missing/key.pub` persisted the key path (and the pin) and then failed reading
    it; a public key whose private half is not here persisted and exited 0. Now the key is read, fingerprinted and made
    to sign a nonce first: a failure persists nothing and names the key path."""
    tmp, ana, ben = two
    before = gl(ben).state()
    missing = tmp_path / "nowhere" / "key.pub"
    capsys.readouterr()
    assert team("join", "--signing-key", str(missing), "--no-install", repo=ben) == 2
    assert str(missing) in capsys.readouterr().err
    assert gl(ben).state() == before
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    shutil.copy(keys["ana2"], lonely / "ana2.pub")      # the public half only: nothing here can sign with it
    assert team("join", "--signing-key", str(lonely / "ana2.pub"), "--no-install", repo=ben) == 2
    assert str(lonely / "ana2.pub") in capsys.readouterr().err
    assert gl(ben).state() == before


@pytest.mark.parametrize("field", ["accepted", "anchor"])
def test_c_join_persists_only_if_the_pin_state_it_validated_is_still_there(two, monkeypatch, capsys, field):
    """codex r5 4 + complement r5 3: join wrote `accepted`/`anchor` from the snapshot it read before validating, so an
    `accept-merge` or `repin --anchor` that completed meanwhile was erased. Now persist is a compare-and-swap: on a
    changed pin state it writes nothing and says join must be re-run."""
    tmp, ana, ben = two
    g = gl(ben)
    other = sh("git", "rev-parse", f"{LB}~1", cwd=ben).strip()

    def concurrent():
        if field == "accepted":
            g.save_state(_mutate=lambda st: st.__setitem__("accepted", {**dict(st.get("accepted") or {}), other: 2}))
        else:
            g.save_state(anchor=other)      # what `repin --anchor` writes
    _after_prospective_derive(monkeypatch, concurrent)
    capsys.readouterr()
    rc = team("join", "--new-device", "--no-install", repo=ben)
    after = gl(ben).state()
    if field == "accepted":
        assert other in (after.get("accepted") or {}), "the concurrent accept-merge was erased"
    else:
        assert after.get("anchor") == other, "the concurrent repin was erased"
    assert rc == 2
    assert "changed the pin state" in capsys.readouterr().err


def test_d_regenesis_from_refuses_a_commit_only_the_local_head_reaches(two, keys, capsys):
    """complement r5 1: `--from` was accepted as an ancestor of `gl.head()`, the local branch, which a sync moves onto
    a host-rewritten tip while frozen; a forged descendant of the genesis became the signed read-only record. Now only
    an ancestor of the anchor or of the counted head is accepted."""
    tmp, ana, ben = two
    base = sh("git", "ls-remote", str(tmp / "origin.git"), f"refs/heads/{LB}", cwd=tmp).split()[0]
    assert ruling(ben, "src/b.py", "ben: published") == 0
    gl(ben).sync(push=False)
    forger = clone(tmp, "forger", "x@ex.com")
    sh("git", "fetch", "-q", "origin", f"{LB}:{LB}", cwd=forger)
    forged = sh("git", "-c", "commit.gpgsign=false", "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "forged",
                cwd=forger).strip()
    sh("git", "push", "-q", "-f", "origin", f"{forged}:refs/heads/{LB}", cwd=forger)
    try:
        gl(ben).sync(push=False)
    except Exception:
        pass
    g = gl(ben)
    assert g.head() == forged, "precondition: the local head carries the forged commit"
    anchor = g.state().get("anchor")
    assert anchor and anchor != forged
    heads = _heads(tmp)
    capsys.readouterr()
    assert team("regenesis", "--from", forged, "--owner", "ben", "--member", "ben=ben@ex.com", repo=ben) == 2
    assert _heads(tmp) == heads
    assert "not on the ledger" in capsys.readouterr().err


def test_e_a_join_whose_key_is_no_members_key_exits_2(two, keys, capsys):
    """complement r5 2: a join that pinned and persisted, with this machine's key in force for no member, said
    "joined" and exited 0, while every later append is refused. Now it is a JoinIncomplete naming the key."""
    tmp, ana, ben = two
    cy = clone(tmp, "cy", "cy@ex.com")
    capsys.readouterr()
    assert team("join", "--signing-key", str(keys["cy"]), "--no-install", repo=cy) == 2
    fp = S.fingerprint(keys["cy"].read_text())
    err = capsys.readouterr().err
    assert f"an owner must add this machine's key: {fp}" in err, err
    assert gl(cy).joined(), "the join's persisted half stays"


def test_f_a_setup_failure_after_an_incomplete_join_keeps_the_pending_message(two, keys, capsys, monkeypatch):
    """complement r5 7: `_install_all` (or the anneal_db save) raising after a JoinIncomplete replaced the pending-steps
    message with its own error. Now the JoinIncomplete is re-raised, chained to the setup failure."""
    from levain.team import cli as C
    tmp, ana, ben = two
    cy = clone(tmp, "cy", "cy@ex.com")

    def broken(repo, python=None):
        raise OSError("hook install failed")
    monkeypatch.setattr(C, "_install_all", broken)
    capsys.readouterr()
    assert team("join", "--signing-key", str(keys["cy"]), repo=cy) == 2
    assert "an owner must add this machine's key" in capsys.readouterr().err
