"""ND-10a: the events half of the morning nightclose (levain.desk.nightclose). After it, no event is left claimed,
delivered-without-ack or retried without a named owner (the day desk's launch, proven by its lease token); a launch
created before the stop claims nothing more; a second nightclose where nothing changed writes no stop and no event
state, and one after a launch was created since the stop stops that launch too."""
from __future__ import annotations

import sqlite3

import pytest

from levain.desk import events, launch as launch_mod
from levain.desk.nightclose import STOP_PREFIX, NightCloseOwnerRefused, NightCloseRefused, nightclose
from levain.desk.store import CoordError
from tests.test_desk_events import (Clock, Pinger, activate, clock, down, pinger, qdir, rows, sensor, states,  # noqa: F401
                                    store)

OPEN = ("claimed", "delivered", "retried")
COUNTS = "SELECT COUNT(*) FROM event_states UNION ALL SELECT COUNT(*) FROM meta"


def night(store, tmp_path, qdir, clock, pinger):
    """Launch A (the night desk) holds the lease: one event it let lapse (retried), one it holds delivered, and one
    nobody took (received)."""
    cred = sensor(store, tmp_path)
    retried = down(store, cred, 1, subject="h1").event.event_id
    ln = store.pending_desk()
    assert store.launch_claim_spawn(ln.launch_id, 1)[1] is None
    launch_mod._brief_text(store, ln)                                       # claims h1
    activate(store, ln.launch_id)
    token = store.coord_claim(ln.launch_id, ln.session_key)
    clock.advance(minutes=15)
    store.coord_beat(token)
    delivered = down(store, cred, 2, subject="h2").event.event_id
    events.claim_for_launch(store, ln.launch_id)                            # h2 claimed, until 35 minutes
    clock.advance(minutes=6)
    store.coord_beat(token)
    events.progress(store, pinger=pinger)                                   # h1 past its claim_until: retried; h2 delivered
    received = down(store, cred, 3, subject="h3").event.event_id
    assert [states(qdir, e)[-1] for e in (retried, delivered, received)] == ["retried", "delivered", "received"]
    return cred, ln, (retried, delivered, received)


def morning(store, ln):
    """Launch B (the day desk) takes the lease from the night desk's ended launch."""
    ended, fresh = store.launch_replace_done(ln.launch_id)
    assert store.launch_claim_spawn(fresh.launch_id, 1)[1] is None
    activate(store, fresh.launch_id, "probe-B")
    token = store.coord_claim(fresh.launch_id, fresh.session_key)
    return fresh, token


def latest(qdir, eid):
    return rows(qdir, f"SELECT kind, launch_id FROM event_states WHERE event_id = '{eid}' AND kind IN "
                      "('received','claimed','delivered','acked','suppressed','alerted','retried') "
                      "ORDER BY seq DESC LIMIT 1")[0]


def stops(qdir):
    return rows(qdir, f"SELECT key FROM meta WHERE key GLOB '{STOP_PREFIX}*' ORDER BY key")


def test_nothing_is_left_claimed_delivered_or_retried_without_the_day_desk_as_owner(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    got = nightclose(store, clock(), token=token)
    assert set(got.handed) == set(evs[:2]) and got.owner == b.launch_id
    for eid in evs[:2]:
        assert latest(qdir, eid) == ("claimed", b.launch_id)
    assert latest(qdir, evs[2])[0] == "received"                            # never taken: the day desk's brief takes it
    left = [e for e in events.listing(store) if e.state in OPEN and e.launch_id != b.launch_id]
    assert left == []


def test_a_launch_that_began_before_the_stop_claims_nothing_after_it(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    nightclose(store, clock(), token=token)
    down(store, cred, 9, subject="h9")
    assert events.claim_for_launch(store, a.launch_id) == []
    assert events.claim_for_launch(store, "ln-" + "f" * 32) == []          # no such launch: fails closed
    assert latest(qdir, evs[2])[0] == "received"
    assert evs[2] in [e.event_id for e in events.claim_for_launch(store, b.launch_id)]


def test_a_launch_that_begins_after_the_stop_claims_as_before(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    nightclose(store, clock(), token=token)
    ended, c = store.launch_replace_done(b.launch_id)
    assert store.launch_claim_spawn(c.launch_id, 1)[1] is None
    activate(store, c.launch_id, "probe-C")
    assert evs[2] in [e.event_id for e in events.claim_for_launch(store, c.launch_id)]


def test_a_second_nightclose_writes_nothing(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    _b, token = morning(store, a)
    nightclose(store, clock(), token=token)
    snap = rows(qdir, COUNTS)
    again = nightclose(store, clock(), token=token)
    assert again.handed == () and again.stopped is False
    assert rows(qdir, COUNTS) == snap


def test_a_launch_created_after_the_stop_is_stopped_by_the_next_close_before_its_events_are_handed(
        store, tmp_path, qdir, clock, pinger):
    """L3 r1 (three lineages): the same owner closing again once launch C, created after the first stop, holds an
    event. The second close writes a fresh cutoff, so C is stopped as its event is handed to B."""
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    nightclose(store, clock(), token=token)
    c = store.launch_request(token, harness="claude", role="coordinator")  # a rotation B asks for after the stop
    assert store.launch_claim_spawn(c.launch_id, 1)[1] is None
    assert evs[2] in [e.event_id for e in events.claim_for_launch(store, c.launch_id)]
    first = stops(qdir)
    got = nightclose(store, clock(), token=token)
    assert got.stopped is True and len(stops(qdir)) == len(first) + 1
    assert evs[2] in got.handed and latest(qdir, evs[2]) == ("claimed", b.launch_id)
    h9 = down(store, cred, 9, subject="h9").event.event_id
    assert events.claim_for_launch(store, c.launch_id) == []                # C is quiesced, not only robbed
    assert latest(qdir, h9)[0] == "received"


def test_a_launch_asked_for_before_the_stop_and_spawned_after_it_is_stopped(store, tmp_path, qdir, clock, pinger):
    """Complement L4, decided: a launch is placed by its creation, so a pending launch asked for before the stop is
    stopped even when it spawns after it (the conservative reading)."""
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    c = store.launch_request(token, harness="claude", role="coordinator")  # pending, not spawned
    nightclose(store, clock(), token=token)
    assert store.launch_claim_spawn(c.launch_id, 1)[1] is None              # spawned after the stop
    assert events.claim_for_launch(store, c.launch_id) == []
    assert latest(qdir, evs[2])[0] == "received"


@pytest.mark.parametrize("who", ["ended", "pending", "nonsense"])
def test_an_owner_that_is_not_the_lease_holders_launch_is_refused(store, tmp_path, qdir, clock, pinger, who):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    owner = {"ended": a.launch_id, "nonsense": "ln-" + "0" * 32}.get(who) or \
        store.launch_request(token, harness="claude", role="coordinator").launch_id
    before = rows(qdir, COUNTS)
    with pytest.raises(NightCloseOwnerRefused):
        nightclose(store, clock(), token=token, owner=owner)
    assert rows(qdir, COUNTS) == before
    assert nightclose(store, clock(), token=token, owner=b.launch_id).owner == b.launch_id


def test_a_token_that_is_not_the_live_leases_is_refused(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    before = rows(qdir, COUNTS)
    for bad in (token + 1000, None, "1"):
        with pytest.raises(NightCloseRefused) as got:
            nightclose(store, clock(), token=bad, owner=b.launch_id)
        assert not isinstance(got.value, NightCloseOwnerRefused)
    assert rows(qdir, COUNTS) == before


def test_an_untrusted_event_is_not_handed_to_a_desk(store, tmp_path, qdir, clock, pinger):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    low = sensor(store, tmp_path, "low", "untrusted")
    u = events.post(store, "low", 1, "site_status", {"subject": "x", "state": "down"}, credential=low).event.event_id
    _b, token = morning(store, a)
    got = nightclose(store, clock(), token=token)
    assert u not in got.handed
    assert latest(qdir, u)[0] in ("alerted", "suppressed")
    assert rows(qdir, f"SELECT COUNT(*) FROM event_states WHERE event_id = '{u}' AND kind = 'claimed'") == [(0,)]


def test_a_malformed_stop_row_fails_closed_and_never_crashes_a_claim(store, tmp_path, qdir, clock, pinger):
    """L3 r1: a stray ``meta`` row. One whose key only LIKE would match is ignored; one with a stop's key and an
    unreadable record stops every launch created before it, the owner too, until a fresh close writes a readable one."""
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    nightclose(store, clock(), token=token)
    cutoff = int(stops(qdir)[-1][0][len(STOP_PREFIX):])
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        c.execute("INSERT INTO meta (key, value) VALUES ('nightclose~stop:zz', '{}')")
        c.commit()
        assert events.claim_for_launch(store, a.launch_id) == []
        assert evs[2] in [e.event_id for e in events.claim_for_launch(store, b.launch_id)]
        c.execute("INSERT INTO meta (key, value) VALUES (?, 'not json')", (f"{STOP_PREFIX}{cutoff + 1:020d}",))
        c.commit()
    finally:
        c.close()
    assert events.claim_for_launch(store, a.launch_id) == []
    assert events.claim_for_launch(store, b.launch_id) == []                # fail closed: the owner is unreadable
    assert nightclose(store, clock(), token=token).stopped is True          # a fresh close recovers it
    assert evs[2] in [e.event_id for e in events.claim_for_launch(store, b.launch_id)]


def test_with_no_launch_holding_the_lease_it_refuses_and_writes_nothing(store, tmp_path, qdir, clock, pinger):
    night(store, tmp_path, qdir, clock, pinger)
    token = rows(qdir, "SELECT MAX(seq) FROM lease_events WHERE kind = 'claim'")[0][0]
    clock.advance(hours=6)                                                  # the night desk's lease is past its until
    before = rows(qdir, "SELECT COUNT(*) FROM event_states")
    with pytest.raises(NightCloseRefused):
        nightclose(store, clock(), token=token)
    assert rows(qdir, "SELECT COUNT(*) FROM event_states") == before
    assert issubclass(NightCloseRefused, CoordError)


def test_the_close_issues_the_same_statements_at_any_count_of_settled_events(store, tmp_path, qdir, clock, pinger):
    """The events half reads the open events in one statement: the STATEMENTS the close runs in the coord write
    transaction do not grow with the settled events ever stored."""
    from tests.test_desk_events import _settled_beats, _statements

    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    _b, token = morning(store, a)
    nightclose(store, clock(), token=token)

    def again():
        store.coord_beat(token)
        assert nightclose(store, clock(), token=token).handed == ()

    _settled_beats(store, clock, cred, 5, first=10)
    clock.advance(minutes=3)
    few = _statements(store, again)
    _settled_beats(store, clock, cred, 40, first=15)
    clock.advance(minutes=3)
    many = _statements(store, again)
    assert many == few, (few, many)


def test_the_set_based_reads_equal_the_old_per_event_loops_over_every_mix_of_states(store, tmp_path, qdir, clock, pinger):
    """G3: ``unseen_event`` and the close's handed set read only the events in the wanted states. The REFERENCE below is
    the old loop (every trusted event, one read each); over a store of many events in every lifecycle state (trusted and
    untrusted) the two give identical answers."""
    from tests.test_desk_events import _conn, launch

    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    low = sensor(store, tmp_path, "low", "untrusted")
    plan = [("received", None), ("claimed", a), ("claimed", b), ("delivered", a), ("delivered", b), ("acked", a),
            ("suppressed", None), ("alerted", None), ("retried", None), ("retried", None), ("claimed", a),
            ("delivered", a), ("received", None), ("acked", b)]
    made = []
    for i, (kind, who) in enumerate(plan * 3):
        made.append(down(store, cred, 100 + i, subject=f"host-{i}").event.event_id)
        with store._write() as (conn, now):
            if kind != "received":
                events._state(conn, now, made[-1], kind, launch_id=None if who is None else who.launch_id,
                              claim_until=events._plus(now, events.CLAIM_TTL) if kind == "claimed" else None)
    for i in range(5):          # untrusted ones are alerted at once (terminal): they must stay out of both sets
        events.post(store, "low", 200 + i, "site_status", {"subject": f"u{i}", "state": "down"}, credential=low)

    def old_unseen(conn, ln):
        start = events._launch_start(conn, ln.launch_id)
        for (eid,) in conn.execute("SELECT event_id FROM events WHERE trust != 'untrusted' ORDER BY gseq").fetchall():
            row = conn.execute(f"SELECT kind, gseq FROM event_states WHERE event_id = ? AND {events._in('kind', events.LIFECYCLE)} "
                               "ORDER BY seq DESC LIMIT 1", (eid,)).fetchone()
            if row is None or row[0] not in events._AVAILABLE:
                continue
            claimed = conn.execute("SELECT 1 FROM event_states WHERE event_id = ? AND kind = 'claimed' AND launch_id = ?",
                                   (eid, ln.launch_id)).fetchone()
            if row[1] > start or claimed is None:
                return eid
        return None

    def old_handed(conn, who):
        out = []
        for (eid,) in conn.execute("SELECT event_id FROM events WHERE trust != 'untrusted' ORDER BY gseq").fetchall():
            ev = events._event(conn, eid)
            if ev.state == "retried" or (ev.state in ("claimed", "delivered") and ev.launch_id != who):
                out.append(ev.event_id)
        return out

    conn = _conn(qdir)
    try:
        for ln_id in (a.launch_id, b.launch_id):
            ln = launch(store, ln_id)
            assert events.unseen_event(conn, ln) == old_unseen(conn, ln)
        want = old_handed(conn, b.launch_id)
    finally:
        conn.close()
    assert len(want) > 10
    got = nightclose(store, clock(), token=token)
    assert list(got.handed) == want
