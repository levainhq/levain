"""Code L3 round 8 on the op lock (c8f0e9e..1f303ec): each test is a run that failed on 1f303ec.

J1 the hook fetch decides again under the op · J2 an overlapping refresh by another caller stays quiet.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure)
import threading
import time

from levain.team.transport import GitLedger
from tests.test_team_tenure import gl, keys, two  # noqa: F401


def test_j1_the_hook_fetch_decides_again_under_the_op(two, monkeypatch):
    """codex r8 5: `fetch_if_due` read the due time before the op, so a second hook that waited on a refresh by the
    first fetched again at once. Now it re-reads the stamp under the op and returns."""
    tmp, ana, ben = two
    gl(ben).save_state(last_fetch_attempt=0)
    got = threading.Event()

    def other_refresh():
        with gl(ben).op(timeout=5):
            got.set()
            time.sleep(0.1)     # well inside the 0.5 s op wait (code L3 r9 complement)
            gl(ben).save_state(last_fetch_attempt=time.time(), last_fetch_ok=time.time())
    t = threading.Thread(target=other_refresh)
    t.start()
    got.wait(5)
    calls = []
    real = GitLedger._sync_in_op
    monkeypatch.setattr(GitLedger, "_sync_in_op", lambda self, **k: calls.append(k) or real(self, **k))
    assert gl(ben).fetch_if_due(300, timeout=5) is None
    t.join()
    assert calls == [], "fetched again right after another caller's refresh"


def test_j2_an_overlapping_refresh_stays_quiet(two):
    """complement r8 2: a busy op while another caller refreshes put a "not refreshed" note on every concurrent edit.
    Now the note appears only when the copy shown is itself older than the interval."""
    tmp, ana, ben = two
    gl(ben).save_state(last_fetch_attempt=0, last_fetch_ok=time.time())
    got, done = threading.Event(), threading.Event()

    def holder():
        with gl(ben).op(timeout=5):
            got.set()
            done.wait(10)
    t = threading.Thread(target=holder)
    t.start()
    got.wait(5)
    try:
        assert gl(ben).fetch_if_due(300, timeout=5) is None
    finally:
        done.set()
        t.join()
