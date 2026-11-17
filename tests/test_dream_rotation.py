"""levain#82 slice 1: the dream rotation's write path (``levain.desk.dream_rotation.rotate``), with no CLI. Each test
is a reproduced sequence on a real store: a real card filed through the dream's own builder, a real active desk launch,
a real ``rotate`` call. Nothing here drives ``coord open``."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

from levain.cockpit.consent import parse_stamp
from levain.desk import dream_rotation, dream_status, events, lease, review, seam_handoffs
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordError
from tests.test_dream_seam import (_file, _home_env, _requested, _saved_after, _snapshot, desk)  # noqa: F401

BODY = {"state_line": "rotating onto the dream", "in_flight": [], "next": "read the new memory", "contingencies": [],
        "notes": "", "attested_operator_present": False, "attested_walked": False}


def _rows(desk, sql, *args):
    return sqlite3.connect(desk.path).execute(sql, args).fetchall()


def _handoffs(desk):
    return _rows(desk, "SELECT handoff_id, close_key, seam_kind, writer, prev_handoff_id FROM seam_handoffs "
                       "ORDER BY handoff_id")


def _launches(desk):
    return _rows(desk, "SELECT launch_id FROM launches ORDER BY gseq")


def _rotate(desk, body=None):
    return dream_rotation.rotate(desk, token=desk.token, body=BODY if body is None else body)


def _claim_an_event(desk, tmp_path):
    path = tmp_path / "mon.cred"
    events.register(desk, "mon", "first_party", path)
    ev = events.post(desk, "mon", 1, "site_status", {"subject": "taosrv", "state": "down"},
                     credential=events.read_credential(path)).event
    assert [e.event_id for e in events.claim_for_launch(desk, desk.launch.launch_id)] == [ev.event_id]
    return ev


def test_a_due_desk_rotation_writes_one_dream_handoff_and_one_pending_desk(desk):
    _saved_after(desk)
    before = _launches(desk)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.Rotation) and got.occurrence == "occ-1"
    assert got.handoff.seam_kind == "dream" and got.handoff.close_key == "dream-rotation:occ-1"
    assert got.handoff.writer == desk.launch.session_key and got.handoff.prev_handoff_id is None
    assert [h[1:3] for h in _handoffs(desk)] == [("dream-rotation:occ-1", "dream")]
    # the desk is ended with the rotation's cause and no longer holds the lease
    assert got.ended.launch_id == desk.launch.launch_id and got.ended.state == "ended"
    assert got.ended.cause == lease.DREAM_ROTATED
    assert desk.coord_status()["lease"] is None or desk.coord_status()["lease"]["holder"] != desk.launch.session_key
    # the next desk is asked for: one new pending desk launch, on the night model
    assert got.successor.state == "pending" and got.successor.role == ROLE_DESK
    assert [r[0] for r in _launches(desk)] == [*(r[0] for r in before), got.successor.launch_id]
    assert got.successor.launch_id != desk.launch.launch_id


def test_the_successor_runs_the_night_model_when_the_setting_has_one(desk, tmp_path):
    desk.coord_set_agent("claude", model="day-model", night_model="night-model", cwd=str(tmp_path))
    _saved_after(desk)
    assert _rotate(desk).successor.model == "night-model"


def test_a_second_rotation_for_the_same_occurrence_writes_nothing(desk):
    _saved_after(desk)
    first = _rotate(desk)
    before = _snapshot(desk.directory)
    with pytest.raises(lease.LeaseRefused):          # the retry by the rotated desk: its lease was released
        _rotate(desk)
    assert _snapshot(desk.directory) == before and len(_handoffs(desk)) == 1
    assert first.successor is not None


def test_a_handoff_already_stored_under_the_occurrences_key_is_not_a_rotation_and_nothing_is_written(desk):
    _saved_after(desk)
    stored = seam_handoffs.close(desk, kind="dream", body=BODY, request_id="dream-rotation:occ-1",
                                 prev_handoff_id=None, token=desk.token)
    before = _snapshot(desk.directory)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.AlreadyStored) and not isinstance(got, dream_rotation.Rotation)
    assert got.handoff == stored and got.occurrence == "occ-1"
    assert _snapshot(desk.directory) == before        # not even a lease beat, and the desk is still live
    assert desk.coord_status()["lease"]["holder"] == desk.launch.session_key


def test_the_handoff_and_the_successor_commit_together_or_not_at_all(desk, monkeypatch):
    _saved_after(desk)
    before = _snapshot(desk.directory)

    def boom(*a, **k):
        raise RuntimeError("wake failed")

    monkeypatch.setattr(lease, "wake", boom)
    with pytest.raises(RuntimeError):
        _rotate(desk)
    assert _snapshot(desk.directory) == before        # no handoff, no ended desk, no new launch, no beat
    assert _handoffs(desk) == [] and desk.coord_status()["lease"]["holder"] == desk.launch.session_key


def test_no_desk_agent_set_rolls_the_handoff_back(desk):
    _saved_after(desk)
    desk.coord_set_agent("none")
    before = _snapshot(desk.directory)
    with pytest.raises(CoordError, match="no next desk could be asked for"):
        _rotate(desk)
    assert _snapshot(desk.directory) == before and _handoffs(desk) == []
    assert desk.coord_status()["lease"]["holder"] == desk.launch.session_key     # the desk keeps its lease


def test_an_expired_successor_leaves_the_dream_handoff_for_the_next_desk(desk):
    _saved_after(desk)
    got = _rotate(desk)
    later = parse_stamp(got.successor.at) + lease.LAUNCH_TTL + timedelta(minutes=1)
    desk._clock = lambda: later
    with desk._write() as (conn, now):
        lease._expire_pending(conn, now)
        assert lease.launch(conn, got.successor.launch_id).state == "expired"
    assert seam_handoffs.latest(desk).handoff_id == got.handoff.handoff_id       # the handoff is the durable half
    with desk._write() as (conn, now):                                           # the next wake asks for a desk again
        assert lease.wake(conn, now, night=True) is not None


def test_the_rotated_desk_is_not_counted_as_a_death(desk):
    _saved_after(desk)
    got = _rotate(desk)
    with desk._write() as (conn, now):
        assert lease.launch_death(conn, got.ended) is False
        assert lease.wake_state(conn, now)[0] != "backoff"
        assert lease._deaths(conn)[0] == 0


def test_end_for_dream_refuses_a_desk_that_is_not_the_live_one(desk):
    with desk._write() as (conn, now):
        lease.end_for_dream(conn, now, desk.launch.launch_id)       # live: allowed
    with desk._write() as (conn, now):
        with pytest.raises(lease.LeaseRefused):
            lease.end_for_dream(conn, now, desk.launch.launch_id)   # already ended
        with pytest.raises(lease.LeaseRefused):
            lease.end_for_dream(conn, now, "ln-" + "0" * 32)


# --- never fires when rotation_due is false ---------------------------------------------------------------------
def test_no_dream_save_writes_nothing(desk):
    before = _snapshot(desk.directory)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.NotDue) and "no dream has saved memory" in got.why
    assert _snapshot(desk.directory) == before


def test_a_save_before_the_desk_started_writes_nothing(desk):
    _file(desk.directory, "consolidated", parse_stamp(_requested(desk)) - timedelta(hours=1))
    before = _snapshot(desk.directory)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.NotDue) and "is not after this desk started" in got.why
    assert _snapshot(desk.directory) == before


def test_a_rolled_back_save_writes_nothing(desk):
    _saved_after(desk)
    _file(desk.directory, "rolled-back", parse_stamp(_requested(desk)) + timedelta(hours=2), occurrence="occ-1",
          reason="rolled back by operator")
    before = _snapshot(desk.directory)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.NotDue) and "rolled back" in got.why
    assert _snapshot(desk.directory) == before


def test_a_shadow_nights_save_writes_nothing(desk, tmp_path):
    from levain.dream import shadow
    from tests.test_dream_seam import _dir

    day = SimpleNamespace(day="2026-11-14", run_id="r", run_dir=tmp_path)
    c = shadow.shadow_card(day, "shadow\n", occurrence="occ-s", occ_dir=_dir(tmp_path, "occ"), flow_text="flow\n")
    review.file_item(desk, "agent", c.body, dedupe_key=c.dedupe_key)
    before = _snapshot(desk.directory)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.NotDue)
    assert _snapshot(desk.directory) == before


def test_a_claimed_event_stops_the_rotation_and_writes_nothing(desk, tmp_path):
    _saved_after(desk)
    ev = _claim_an_event(desk, tmp_path)
    before = _snapshot(desk.directory)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.NotDue) and got.why.startswith("mid-work:") and ev.event_id in got.why
    assert _snapshot(desk.directory) == before


def test_an_unacknowledged_open_stops_the_rotation_and_writes_nothing(desk):
    from levain.desk import seam_build, seam_open

    _saved_after(desk)
    b = seam_build.build(desk, kind="light", signal_kind="silence", signal_key="k-82", queue_dir=desk.directory,
                         token=desk.token)
    assert seam_open.record_delivery(desk, b.open_id, "light_verb", recipient=desk.launch.launch_id)
    before = _snapshot(desk.directory)
    got = _rotate(desk)
    assert isinstance(got, dream_rotation.NotDue) and "has not acknowledged it" in got.why
    assert _snapshot(desk.directory) == before


def test_a_body_that_attests_the_operator_is_present_writes_nothing(desk):
    _saved_after(desk)
    before = _snapshot(desk.directory)
    got = _rotate(desk, {**BODY, "attested_operator_present": True})
    assert isinstance(got, dream_rotation.NotDue) and "operator is present" in got.why
    assert _snapshot(desk.directory) == before


def test_a_token_that_is_not_the_live_lease_is_refused_and_writes_nothing(desk):
    _saved_after(desk)
    before = _snapshot(desk.directory)
    with pytest.raises(lease.LeaseRefused):
        dream_rotation.rotate(desk, token=desk.token + 1000, body=BODY)
    assert _snapshot(desk.directory) == before


@pytest.mark.parametrize("token", [None, True, "7", 1.5])
def test_a_token_that_is_not_an_int_is_refused_before_anything_opens(desk, token):
    _saved_after(desk)
    before = _snapshot(desk.directory)
    with pytest.raises(CoordError):
        dream_rotation.rotate(desk, token=token, body=BODY)
    assert _snapshot(desk.directory) == before


def test_a_malformed_body_is_refused_before_anything_opens(desk):
    _saved_after(desk)
    before = _snapshot(desk.directory)
    with pytest.raises(CoordError):
        _rotate(desk, {"state_line": "x"})
    assert _snapshot(desk.directory) == before


def test_a_claim_between_the_advisory_read_and_the_write_stops_the_rotation(desk, tmp_path, monkeypatch):
    # the advisory read says due; an event is claimed before the write; the in-transaction seam_check sees it
    _saved_after(desk)
    real = dream_status.rotation_due
    calls = []

    def read_then_claim(store, launch, *, at_turn_boundary):
        answer = real(store, launch, at_turn_boundary=at_turn_boundary)
        calls.append(answer)
        _claim_an_event(desk, tmp_path)
        return answer

    monkeypatch.setattr(dream_status, "rotation_due", read_then_claim)
    before_handoffs = _handoffs(desk)
    got = _rotate(desk)
    assert calls and calls[0][0] is True
    assert isinstance(got, dream_rotation.NotDue) and got.why.startswith("mid-work:")
    assert _handoffs(desk) == before_handoffs
    assert desk.coord_status()["lease"]["holder"] == desk.launch.session_key


def test_a_manual_close_for_an_older_dream_is_not_adopted_for_the_newer_one(desk):
    _saved_after(desk, hours=0.001, occurrence="occ-A")
    _saved_after(desk, hours=0.002, occurrence="occ-B")
    desk._clock = lambda: parse_stamp(_requested(desk)) + timedelta(minutes=1)      # the desk closed by hand for A
    by_hand = seam_handoffs.close(desk, kind="dream", body=BODY, request_id="by-hand-A", prev_handoff_id=None,
                                  token=desk.token)
    got = _rotate(desk, {**BODY, "state_line": "B's own line"})
    assert isinstance(got, dream_rotation.Rotation) and got.occurrence == "occ-B"
    assert got.handoff.close_key == "dream-rotation:occ-B" and got.handoff.prev_handoff_id == by_hand.handoff_id
    assert json.loads(got.handoff.body)["state_line"] == "B's own line"
    assert [h[1] for h in _handoffs(desk)] == ["by-hand-A", "dream-rotation:occ-B"]


@pytest.mark.parametrize("key", ["x" * 500, "two\nlines:"])
def test_a_rotation_has_the_same_key_checks_a_close_has(desk, key, monkeypatch):
    _saved_after(desk)
    monkeypatch.setattr(dream_rotation, "KEY_PREFIX", key)
    before = _snapshot(desk.directory)
    with pytest.raises(CoordError):
        _rotate(desk)
    assert _snapshot(desk.directory) == before


def test_a_manual_dream_close_is_never_taken_for_the_rotation_handoff(desk):
    _saved_after(desk, hours=0.001)
    desk._clock = lambda: parse_stamp(_requested(desk)) + timedelta(minutes=1)      # by hand, after the save
    manual = seam_handoffs.close(desk, kind="dream", body=BODY, request_id="by-hand-1", prev_handoff_id=None,
                                 token=desk.token)
    got = _rotate(desk)
    assert got.handoff.close_key == "dream-rotation:occ-1" and got.handoff.prev_handoff_id == manual.handoff_id
    assert [h[1] for h in _handoffs(desk)] == ["by-hand-1", "dream-rotation:occ-1"]
