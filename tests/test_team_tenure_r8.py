"""Code L3 round 7 on the op lock (1349c09..c8f0e9e): each test is a run that failed on c8f0e9e.

r7 found the op construct sound and its COVERAGE short: writers that did not take it, a fork that inherited it, a
hook fetch that skipped silently under it, and init's key saved before every refusal ran.

I1 append waits for the op · I2 a busy op is reported by the hook fetch, never stamped as an attempt · I3 init saves
no key when a member key is refused · I4 a forked child does not inherit the op.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure)
import os
import threading

import pytest

from levain.team import entry as E
from levain.team import roles as R
from levain.team.transport import WARNINGS, TeamBusy, TeamError
from tests.test_team_tenure import clone, gl, keys, ruling, sh, two  # noqa: F401


def _hold_op(repo, during):
    """Hold the clone's op in another thread while ``during()`` runs here."""
    got, done = threading.Event(), threading.Event()

    def holder():
        with gl(repo).op(timeout=5):
            got.set()
            done.wait(30)
    t = threading.Thread(target=holder)
    t.start()
    got.wait(10)
    try:
        return during()
    finally:
        done.set()
        t.join()


def test_i1_append_waits_for_the_op(two):
    """codex r7 1 (HIGH): `append` (the hook's ack, pack seeding, `record`) ran outside the op, so a `join --root`
    could move the pin between its validation and its commit. Now it takes the op, waiting its own lock_timeout."""
    tmp, ana, ben = two
    assert ruling(ana, "src/a.py", "ana: a ruling to acknowledge") == 0
    gl(ben).sync(push=False)
    rid = gl(ben).ledger().entries[-1]["id"]
    entry = E.build("ben", "ack", refs=[rid], session="s", agent="t", summary="x")

    def go():
        with pytest.raises(TeamBusy):
            gl(ben).append(entry, push=False, lock_timeout=0.2)
    _hold_op(ben, go)


def test_i2_a_busy_op_is_reported_by_the_hook_fetch_and_never_stamped(two):
    """codex r7 3 (HIGH) + complement 3: `fetch_if_due` stamped the attempt, met a busy op and returned None, so the hook
    showed the old copy with no note and did not retry for a whole interval."""
    tmp, ana, ben = two
    before = gl(ben).state().get("last_fetch_attempt")
    note = _hold_op(ben, lambda: gl(ben).fetch_if_due(0, timeout=5))
    assert note and "another levain team operation" in note, note
    assert gl(ben).state().get("last_fetch_attempt") == before


def test_i3_init_saves_no_key_when_a_member_key_is_refused(tmp_path, keys):
    """complement r7 1 + codex r7 4: init saved the proposed signing key, then refused a member key."""
    WARNINGS.clear()
    a = tmp_path / "a"
    a.mkdir()
    sh("git", "init", "-q", "--initial-branch=main", cwd=a)
    sh("git", "config", "user.email", "ana@ex.com", cwd=a)
    sh("git", "config", "user.name", "ana", cwd=a)
    (a / "f").write_text("x")
    sh("git", "add", "f", cwd=a)
    sh("git", "commit", "-qm", "i", cwd=a)
    team_ = R.Team(project="demo", owner="ana", members={"ana": "ana@ex.com"})
    with pytest.raises(TeamError):
        gl(a).init(team_, member_keys={"ghost": keys["ben"].read_text()}, signing_key=str(keys["ana"]))
    assert "signing_key" not in gl(a).state(), gl(a).state()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_i4_a_forked_child_does_not_inherit_the_op(two):
    """codex r7 2 (HIGH): a fork under the op copied the held depth (and the lock's open file description), so the child
    ran whole operations as if it held the lock. Now the child must take the op afresh and waits for the parent."""
    tmp, ana, ben = two
    with gl(ben).op():
        pid = os.fork()
        if pid == 0:     # the child: exit 0 only if it was kept out
            try:
                with gl(ben).op(timeout=0.3):
                    os._exit(1)
            except TeamBusy:
                os._exit(0)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    with gl(ben).op(timeout=2):     # and the parent's release really released it
        pass
