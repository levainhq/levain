"""ND-10 live-seat half: the morning nightclose RECORDS every live worker seat (levain.desk.nightclose, levain#19). One
write-once ``nightclose_seat:<seat launch>:<owner>`` row per live worker, from the roster's own projection
(levain.desk.ready.live_seat_launches); the night desk's launch and the owner are not recorded; nothing is stopped. Real
store, real launch rows, the real CLI entry for the printed line."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from levain.cli import main
from levain.desk import launch as launch_mod
from levain.desk.legacy import ROLE_DESK
from levain.desk.nightclose import SEAT_PREFIX, nightclose
from levain.desk.store import Part
from tests.test_desk_events import (Clock, Pinger, activate, down, pinger, qdir, rows, sensor, states,  # noqa: F401
                                    store)
from tests.test_desk_nightclose import COUNTS, morning, night

SEATS = f"SELECT key, value FROM meta WHERE key GLOB '{SEAT_PREFIX}*' ORDER BY key"
LAUNCH_ROWS = "SELECT launch_id, seq, kind, cause FROM launch_events ORDER BY launch_id, seq"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))


def worker(store, token, what, harness="claude"):
    """A live worker launch on its own thread, through the store's own launch API (activated, as the roster reads it)."""
    d = store.accept_drop(what, surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", what)])[0]
    ln = store.launch_request(token, harness=harness, role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id=f"sess-{what.replace(' ', '-')}")
    assert why is None and got.state == "active"
    return got, thread.thread_id


def seat_keys(qdir):
    return [k for k, _ in rows(qdir, SEATS)]


def test_two_live_workers_are_recorded_and_the_night_desk_and_owner_are_not(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    w1, t1 = worker(store, token, "fix the build")
    w2, t2 = worker(store, token, "write the docs", harness="codex")
    other = store.launch_request(token, harness="claude", role=ROLE_DESK)       # a second desk launch, active
    activate(store, other.launch_id, "probe-other")
    got = nightclose(store, clock(), token=token)
    assert set(got.seats) == {w1.launch_id, w2.launch_id} and len(got.seats) == 2
    assert seat_keys(qdir) == sorted(f"{SEAT_PREFIX}{w.launch_id}:{b.launch_id}" for w in (w1, w2))
    by = {k.split(":")[1]: json.loads(v) for k, v in rows(qdir, SEATS)}
    one = by[w1.launch_id]
    assert (one["owner"], one["harness"], one["role"], one["threads"], one["session_key"]) == (
        b.launch_id, "claude", "worker", [t1], w1.session_key)
    assert by[w2.launch_id]["harness"] == "codex" and by[w2.launch_id]["threads"] == [t2]
    assert all(a.launch_id not in k and other.launch_id not in k.split(":")[1] and b.launch_id != k.split(":")[1]
               for k in seat_keys(qdir))


def test_a_worker_that_has_ended_is_not_recorded(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    live, _ = worker(store, token, "still going")
    gone, _ = worker(store, token, "already over")
    assert store.launch_end_unlisted(gone.launch_id) is not None
    got = nightclose(store, clock(), token=token)
    assert got.seats == (live.launch_id,)
    assert seat_keys(qdir) == [f"{SEAT_PREFIX}{live.launch_id}:{b.launch_id}"]


def test_a_rerun_records_nothing_more_and_does_not_fail(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    w, _ = worker(store, token, "fix the build")
    nightclose(store, clock(), token=token)
    snap = rows(qdir, COUNTS)
    again = nightclose(store, clock(), token=token)
    assert again.seats == () and again.handed == () and again.stopped is False
    assert rows(qdir, COUNTS) == snap and len(seat_keys(qdir)) == 1


def test_nothing_is_stopped_the_seats_launch_rows_are_unchanged(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    w1, _ = worker(store, token, "fix the build")
    w2, _ = worker(store, token, "write the docs")
    ids = (w1.launch_id, w2.launch_id)
    before = [r for r in rows(qdir, LAUNCH_ROWS) if r[0] in ids]
    nightclose(store, clock(), token=token)
    nightclose(store, clock(), token=token)
    assert [r for r in rows(qdir, LAUNCH_ROWS) if r[0] in ids] == before
    assert all(store.launch(i).state == "active" for i in ids)


def test_a_seat_created_after_the_first_close_is_recorded_by_the_second(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    first, _ = worker(store, token, "fix the build")
    nightclose(store, clock(), token=token)
    late, _ = worker(store, token, "started after the close")
    got = nightclose(store, clock(), token=token)
    assert got.seats == (late.launch_id,)
    assert seat_keys(qdir) == sorted(f"{SEAT_PREFIX}{w.launch_id}:{b.launch_id}" for w in (first, late))


@pytest.fixture
def clock():
    c = Clock()
    c.now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=22)
    return c                                    # the night's 21 minutes end at the real now the CLI reads


def test_the_verb_prints_the_count_and_the_names_on_one_line(store, tmp_path, qdir, clock, pinger, capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    w1, t1 = worker(store, token, "fix the build")
    w2, t2 = worker(store, token, "write the docs")
    assert main(["coord", "nightclose", "--token", str(token), "--queue-dir", str(qdir)]) == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    on = ", ".join(f"{w.launch_id} claude threads {t}" for w, t in sorted(((w1, t1), (w2, t2)), key=lambda p: p[0].launch_id))
    assert out.strip().endswith(f"; seats recorded 2 new; 2 on record for {b.launch_id} ({on})")
    assert main(["coord", "nightclose", "--token", str(token), "--queue-dir", str(qdir)]) == 0
    again = capsys.readouterr().out.strip()                    # a retry: 0 new, the same two still named
    assert again.endswith(f"; seats recorded 0 new; 2 on record for {b.launch_id} ({on})")


def test_a_lost_print_does_not_lose_the_seats(store, tmp_path, qdir, clock, pinger, capsys):
    """The close commits and its result never reaches the desk (simulated: the call is made directly): the retry the
    desk runs names the seats from the durable records, and the JSON carries them too."""
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    w1, t1 = worker(store, token, "fix the build")
    got = nightclose(store, clock(), token=token)
    assert got.seats == (w1.launch_id,) and [r["launch_id"] for r in got.on_record] == [w1.launch_id]
    assert main(["coord", "nightclose", "--token", str(token), "--queue-dir", str(qdir), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["seats"] == [] and [(r["launch_id"], r["threads"]) for r in out["on_record"]] == [(w1.launch_id, [t1])]
    w2, _ = worker(store, token, "started after")
    later = nightclose(store, clock(), token=token)
    assert later.seats == (w2.launch_id,) and {r["launch_id"] for r in later.on_record} == {w1.launch_id, w2.launch_id}


def test_the_day_desks_brief_tells_it_to_adopt_the_recorded_seats(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    text = launch_mod._brief_text(store, store.launch(b.launch_id))
    assert "nightclose_seat:" in text and "ADOPT those seats, never stop them" in text
    assert "confirm each is still active in the roster first" in text
