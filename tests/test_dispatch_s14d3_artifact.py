"""S14 D3 (the `_levain_dispatch` side of the artifact): the event's digest binds the file, an `artifact_failed` event
names the job's failure, a nothing-new run fails at once, and the grace covers a slow import.

CONTROL, the job store and the follower are the real ones of test_dispatch_follow's rig; the runner rows ride
test_dispatch_run_jobs_exit's c6 rig. Everything root-side (the uid that reads `L/export/dispatch`) is VM residue."""
from __future__ import annotations

import hashlib
import logging

import pytest

from levain.dispatch import export as X
from levain.dispatch import follow as F
from levain.slot import control_client as CC
from levain.slot import records as R
from tests.test_dispatch_follow import (Dispatch, artifact_follower, exported, held, published_for,  # noqa: F401
                                        served)
from tests.test_dispatch_jobs import at_state, base_tree, clock, model, repo, store  # noqa: F401 - fixtures
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_slot_control_events import cas, to_retained
from tests.test_tickets import SESSION, bound_session


def acked_of(served, session) -> bool:  # noqa: F811
    return served.control.store.get(session).fields.get("artifact_acked") is True


# -- read_artifact(expect_digest=) --------------------------------------------------------------------------------------

def test_read_artifact_accepts_the_bytes_of_the_digest_and_refuses_any_other(tmp_path):
    session = "s-" + "1" * 16
    exported(tmp_path, session, "tkt")
    good = "sha256:" + hashlib.sha256((tmp_path / session / X.FILE).read_bytes()).hexdigest()
    assert X.read_artifact(tmp_path, session, expect_digest=good).ticket == "tkt"
    assert X.read_artifact(tmp_path, session).ticket == "tkt"                      # no expectation: as before
    with pytest.raises(X.ArtifactUnreadable) as e:
        X.read_artifact(tmp_path, session, expect_digest="sha256:" + "0" * 64)
    assert e.value.name == "artifact-digest-mismatch"


# -- the follower: the event's digest names exactly the file ------------------------------------------------------------

def test_a_file_whose_bytes_are_the_events_digest_is_taken_and_acked(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq, export = published_for(served, lambda s, t: exported(tmp_path, s, t))
    f = artifact_follower(d, export)
    assert f.step().outcome == CC.DEADLINE
    assert held(store, session, ticket) is not None and acked_of(served, session)
    assert store.event_cursor()[1] == seq
    f.close()


def test_a_file_whose_bytes_are_not_the_events_digest_is_passed_unacked_and_nothing_is_taken(
        store, repo, served, tmp_path, caplog):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq, export = published_for(served, lambda s, t: exported(tmp_path, s, t))
    exported(tmp_path, session, ticket, files=3)                                   # the file changed after the event
    f = artifact_follower(d, export)
    with caplog.at_level(logging.WARNING, logger="levain.dispatch.follow"):
        assert f.step().outcome == CC.DEADLINE
    assert "artifact-digest-mismatch" in caplog.text
    assert held(store, session, ticket) is None and not acked_of(served, session)
    assert store.event_cursor()[1] == seq                                          # the stream moved on
    f.close()


def test_t3_a_re_published_export_is_not_taken_under_the_old_event(store, repo, served, tmp_path, caplog):  # noqa: F811
    """D6 re-runs the sealed run and replaces `<s>`: a replay of the OLD event meets the NEW bytes and takes nothing."""
    d = Dispatch(store, repo, served)
    session, ticket, seq, export = published_for(served, lambda s, t: exported(tmp_path, s, t))
    exported(tmp_path, session, ticket, content=b"a re-published bundle")             # root published again
    store.drop_event_cursor()
    f = artifact_follower(d, export)
    with caplog.at_level(logging.WARNING, logger="levain.dispatch.follow"):
        f.step()
    assert "artifact-digest-mismatch" in caplog.text
    assert held(store, session, ticket) is None and not acked_of(served, session)
    f.close()


def test_t11_the_a6_insertion_point_runs_after_the_take_and_before_the_ack(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _seq, export = published_for(served, lambda s, t: exported(tmp_path, s, t))
    seen = []
    f = artifact_follower(d, export)
    f._before_ack = lambda ev, art: seen.append((held(store, session, ticket) is not None,
                                                 acked_of(served, session), art.ticket))
    f.step()
    assert seen == [(True, False, ticket)]                                         # taken, not yet acked, still RETAINED
    assert acked_of(served, session)
    f.close()


def test_a_refusal_at_the_insertion_point_leaves_the_event_unacked(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _seq, export = published_for(served, lambda s, t: exported(tmp_path, s, t))
    f = artifact_follower(d, export)

    def refuse(ev, art):
        raise F.FollowStopped("adopt-verify-refused", "a stand-in for A6")

    f._before_ack = refuse
    with pytest.raises(F.FollowStopped) as e:
        f.step()
    assert e.value.name == "adopt-verify-refused" and not acked_of(served, session)
    f.close()


# -- the follower: an artifact_failed event is the job's failure --------------------------------------------------------

def failed_event(served, name="adopt-cap:memory"):  # noqa: F811
    from tests.test_slot_control_events import cas as cas_
    ticket, _run, _att = bound_session(served)
    to_retained(served)
    cas_(served, R.RETAINED, R.RETAINED, SESSION, artifact_failed=name)
    ev = served.kept()[-1]
    assert ev["kind"] == "artifact" and ev["ticket"] == ticket
    return SESSION, ticket, ev["seq"]


def test_an_artifact_failed_event_is_recorded_by_name_and_passed_unacked(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq = failed_event(served)
    f = artifact_follower(d, tmp_path)
    f.step()
    assert store.artifact_failure(session, ticket) == "adopt-cap:memory"
    assert not acked_of(served, session) and store.event_cursor()[1] == seq
    f.close()


def test_a_replayed_artifact_failed_event_converges(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq = failed_event(served)
    f = artifact_follower(d, tmp_path)
    f.step()
    store.drop_event_cursor()
    f.step()                                                                       # the restart replay
    assert store.artifact_failure(session, ticket) == "adopt-cap:memory"
    assert store.event_cursor()[1] == seq
    f.close()


def test_an_artifact_failed_event_is_recorded_even_with_no_export_dir(store, repo, served):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = failed_event(served, "walker-cap:bytes")
    f = artifact_follower(d, None)
    f.step()
    assert store.artifact_failure(session, ticket) == "walker-cap:bytes"
    f.close()


