"""ND-10b: the started-effect half of the morning nightclose (levain.desk.nightclose; levain#19). DONE: no effect is
left claimed and unowned after the stop. A started effect is a journal row still ``unknown``; the stop records it for
the day desk's launch and never settles, retries or re-sends it. Real store, real journal file, the journal's own API."""
from __future__ import annotations

import pytest

from levain.desk import events
from levain.desk.effect_attempts import Journal, idem_key
from levain.desk.nightclose import EFFECT_PREFIX, NightCloseRefused, nightclose
from tests.test_desk_events import (Clock, Pinger, activate, clock, down, pinger, qdir, rows, sensor, states,  # noqa: F401
                                    store)
from tests.test_desk_nightclose import COUNTS, latest, morning, night

NOW = "2026-11-16T04:00:00Z"


def reserve(journal, event_id, *, verb="restart_service", scope="inc-1", night_scope="2026-11-15", digest="d"):
    row, mine = journal.reserve(key=idem_key(event_id, verb, digest), order_id="so-1", order_sha256="a" * 64,
                                incarnation=1, verb=verb, event_id=event_id, args_digest=digest, recipient=None,
                                incident_scope=scope, night_scope=night_scope, per_incident=5, per_night=5, now=NOW)
    assert mine
    return row


def recorded(qdir):
    return rows(qdir, f"SELECT key, value FROM meta WHERE key GLOB '{EFFECT_PREFIX}*' ORDER BY key")


def test_every_unknown_effect_is_recorded_for_the_day_desk_and_its_event_is_handed(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    journal = Journal(tmp_path / "attempts.db")
    started = [reserve(journal, e, digest=str(i)) for i, e in enumerate(evs[:2])]
    b, token = morning(store, a)
    got = nightclose(store, clock(), token=token, journal=journal)
    assert set(got.effects) == {r.attempt_id for r in started}
    keys = [k for k, _ in recorded(qdir)]
    assert keys == [f"{EFFECT_PREFIX}{r.attempt_id:020d}:{b.launch_id}" for r in started]
    assert all(b.launch_id in v for _, v in recorded(qdir))                 # owned: the record names the lease holder
    for eid in evs[:2]:                                                     # and its open event is the owner's too
        assert latest(qdir, eid) == ("claimed", b.launch_id)


def test_the_stop_never_touches_the_journal(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    journal = Journal(tmp_path / "attempts.db")
    unknown, settled = reserve(journal, evs[0]), reserve(journal, evs[1], digest="2")
    assert journal.settle(settled.attempt_id, "ok", NOW)
    before = journal.rows()
    b, token = morning(store, a)
    got = nightclose(store, clock(), token=token, journal=journal)
    assert got.effects == (unknown.attempt_id,)                             # a settled effect needs no owner
    assert journal.rows() == before and [r.status for r in before] == ["unknown", "ok"]
    assert journal.lookup(unknown.idem_key).status == "unknown"             # never retried, never re-sent: left unknown


def test_an_effect_that_settles_after_the_stop_reads_its_own_status_not_the_stops(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    journal = Journal(tmp_path / "attempts.db")
    row = reserve(journal, evs[0])
    b, token = morning(store, a)
    nightclose(store, clock(), token=token, journal=journal)
    assert journal.settle(row.attempt_id, "ok", NOW)                        # the started effect finishes by itself
    assert journal.lookup(row.idem_key).status == "ok"
    assert all("ok" not in v and "status" not in v for _, v in recorded(qdir))


def test_a_second_close_records_nothing_more(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    journal = Journal(tmp_path / "attempts.db")
    reserve(journal, evs[0])
    b, token = morning(store, a)
    nightclose(store, clock(), token=token, journal=journal)
    snap = rows(qdir, COUNTS)
    again = nightclose(store, clock(), token=token, journal=journal)
    assert again.effects == () and again.handed == () and again.stopped is False
    assert rows(qdir, COUNTS) == snap


def test_an_effect_reserved_after_the_first_close_is_recorded_by_the_next(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    journal = Journal(tmp_path / "attempts.db")
    b, token = morning(store, a)
    nightclose(store, clock(), token=token, journal=journal)
    late = reserve(journal, evs[2])
    got = nightclose(store, clock(), token=token, journal=journal)
    assert got.effects == (late.attempt_id,) and got.stopped is False


def test_a_journal_that_cannot_be_read_refuses_and_writes_nothing(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)

    class Broken:
        def rows(self):
            raise OSError("disk")

    before = rows(qdir, COUNTS)
    with pytest.raises(NightCloseRefused):
        nightclose(store, clock(), token=token, journal=Broken())
    assert rows(qdir, COUNTS) == before


def test_a_new_owner_records_the_effects_the_old_owner_held(store, tmp_path, qdir, clock, pinger):
    """The record is the owner's: a later lease holder is not owned by an earlier launch's record."""
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    journal = Journal(tmp_path / "attempts.db")
    row = reserve(journal, evs[0])
    b, token = morning(store, a)
    nightclose(store, clock(), token=token, journal=journal)
    ended, c = store.launch_replace_done(b.launch_id)
    assert store.launch_claim_spawn(c.launch_id, 1)[1] is None
    activate(store, c.launch_id, "probe-C")
    token_c = store.coord_claim(c.launch_id, c.session_key)
    got = nightclose(store, clock(), token=token_c, journal=journal)
    assert got.owner == c.launch_id and got.effects == (row.attempt_id,)
    assert len(recorded(qdir)) == 2


def test_without_a_journal_the_events_half_is_unchanged(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    got = nightclose(store, clock(), token=token)
    assert got.effects == () and recorded(qdir) == []


def test_the_journal_is_read_outside_the_stores_write_transaction(store, tmp_path, qdir, clock, pinger, monkeypatch):
    """The journal read is a file read of every historical attempt: it must not run inside the store's write lock."""
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    journal = Journal(tmp_path / "attempts.db")
    row = reserve(journal, evs[0])
    settled = reserve(journal, evs[1], digest="2")
    assert journal.settle(settled.attempt_id, "ok", NOW)
    b, token = morning(store, a)
    inside = []
    real_write = store._write

    class Spy:
        def __init__(self, *args, **kw):
            self.cm = real_write(*args, **kw)

        def __enter__(self):
            inside.append(True)
            return self.cm.__enter__()

        def __exit__(self, *exc):
            try:
                return self.cm.__exit__(*exc)
            finally:
                inside.pop()

    class Watching:
        held = []

        def rows(self):
            self.held.append(bool(inside))
            return journal.rows()

    monkeypatch.setattr(store, "_write", lambda *args, **kw: Spy(*args, **kw))
    watched = Watching()
    got = nightclose(store, clock(), token=token, journal=watched)
    assert watched.held == [False]                                          # read once, with no write transaction open
    assert got.effects == (row.attempt_id,)                                 # and only the still-unknown row is kept
