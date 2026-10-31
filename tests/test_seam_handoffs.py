"""C15 S1: ``seam_handoffs`` and its one fenced writer ``close``. Every expectation below was first read from a real run
on a scratch store (an active coordinator launch holding the lease, as ``boot`` in test_coordinator_lease makes it)."""
from __future__ import annotations

import sqlite3

import pytest

from levain.coordinator import seam_handoffs as sh
from levain.coordinator.lease import LeaseRefused
from levain.coordinator.store import CoordError, CoordStore


def _body(**over):
    body = {"state_line": "main is green", "in_flight": [{"item": "S1", "owner": "seat", "why": "review"}],
            "next": "open the queue", "contingencies": [{"if": "ci red", "then": "revert"}], "notes": "",
            "attested_operator_present": False, "attested_walked": False}
    body.update(over)
    return body


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    s = CoordStore(d)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.accept_drop("first words", surface="cli")
    ln = s.coord_status()["launches"][0]
    got, why = s.launch_activate(ln["launch_id"], harness_session_id="probe-A")
    assert why is None and got.state == "active"      # activation claims the lease for the coordinator launch
    s.session_key = got.session_key
    return s


def _token(store):
    return store.coord_status()["lease"]["token"]


def _close(store, request_id="r1", prev=None, kind="rotation", body=None, token=None):
    return sh.close(store, kind=kind, body=_body() if body is None else body, request_id=request_id,
                    prev_handoff_id=prev, token=_token(store) if token is None else token)


def _count(store):
    c = sqlite3.connect(store.path)
    try:
        return c.execute("SELECT COUNT(*) FROM seam_handoffs").fetchone()[0]
    finally:
        c.close()


def _max_drop_gseq(store):
    """A drop and the reply that acknowledges it are both counted rows; the reply is the later number."""
    c = sqlite3.connect(store.path)
    try:
        return c.execute("SELECT MAX(gseq) FROM (SELECT gseq FROM drops UNION ALL SELECT gseq FROM replies)").fetchone()[0]
    finally:
        c.close()


def test_a_close_appends_one_handoff_written_by_the_active_coordinator_launch(store):
    h = _close(store)
    assert (h.handoff_id, h.seam_kind, h.close_key, h.prev_handoff_id) == (1, "rotation", "r1", None)
    assert h.writer == store.session_key                     # derived from the lease holder, never from the request
    assert h.body == ('{"attested_operator_present":false,"attested_walked":false,"contingencies":[{"if":"ci red",'
                      '"then":"revert"}],"in_flight":[{"item":"S1","owner":"seat","why":"review"}],'
                      '"next":"open the queue","notes":"","state_line":"main is green"}')
    assert sh.latest(store) == h and _count(store) == 1


def test_covers_gseq_is_the_highest_counted_gseq_at_the_start(store):
    store.accept_drop("second words", surface="cli")
    drop_gseq = _max_drop_gseq(store)
    h = _close(store)
    # the close's own fenced-write beat and its own handoff row take later numbers and count for nothing
    assert h.covers_gseq == drop_gseq and h.gseq > drop_gseq


def test_a_retry_returns_the_same_handoff_even_after_another_counted_row_landed(store):
    first = _close(store)
    store.accept_drop("landed after the close", surface="cli")
    assert _max_drop_gseq(store) > first.covers_gseq
    again = _close(store)                                     # same request id, same words
    assert again == first and again.covers_gseq == first.covers_gseq
    assert _count(store) == 1


def test_the_same_request_id_with_other_words_is_refused(store):
    _close(store)
    with pytest.raises(CoordError, match="already closed handoff 1 by another writer or with other words"):
        _close(store, body=_body(notes="changed"))
    with pytest.raises(CoordError, match="already closed handoff 1 by another writer or with other words"):
        _close(store, kind="dream")
    assert _count(store) == 1


def test_a_late_close_behind_a_newer_handoff_is_refused(store):
    first = _close(store, "r1")
    second = _close(store, "r2", prev=first.handoff_id)
    assert (second.handoff_id, second.prev_handoff_id) == (2, 1)
    with pytest.raises(CoordError, match="a newer handoff exists"):
        _close(store, "r3", prev=first.handoff_id)            # the stale coordinator's late close
    with pytest.raises(CoordError, match="a newer handoff exists"):
        _close(store, "r4", prev=None)
    assert _close(store, "r2", prev=first.handoff_id) == second    # the retry of the one that landed still answers
    assert _count(store) == 2


@pytest.mark.parametrize("kind", ["light", "weekly", "", None])
def test_an_unknown_kind_is_refused(store, kind):
    with pytest.raises(CoordError, match="kind rotation, operator, dream"):
        _close(store, kind=kind)
    assert _count(store) == 0


@pytest.mark.parametrize("key", sh.BODY_KEYS)
def test_a_missing_body_key_is_refused(store, key):
    body = _body()
    del body[key]
    with pytest.raises(CoordError, match=f"missing: {key}"):
        _close(store, body=body)
    assert _count(store) == 0


def test_an_unknown_body_key_and_a_bad_shape_are_refused(store):
    with pytest.raises(CoordError, match="unknown: extra"):
        _close(store, body=_body(extra=1))
    with pytest.raises(CoordError, match="in_flight is a list"):
        _close(store, body=_body(in_flight="none"))
    with pytest.raises(CoordError, match="in_flight entry has exactly the keys item, owner, why"):
        _close(store, body=_body(in_flight=[{"item": "x"}]))
    with pytest.raises(CoordError, match="attested_walked is true or false"):
        _close(store, body=_body(attested_walked="yes"))
    assert _count(store) == 0


@pytest.mark.parametrize("line", ["", "   \n", " 　", None, 7])   # NBSP, U+3000: the writer's, not the trigger's
def test_an_empty_state_line_is_refused(store, line):
    with pytest.raises(CoordError, match="state_line is non-empty text"):
        _close(store, body=_body(state_line=line))
    assert _count(store) == 0


def test_a_lone_surrogate_or_a_non_text_key_is_the_same_refusal(store):
    import json

    lone = json.loads('"\\ud800"')                 # what a JSON reader hands over for an unpaired \ud800
    with pytest.raises(CoordError, match="encodes as UTF-8"):
        _close(store, body=_body(notes=lone))
    with pytest.raises(CoordError, match="encodes as UTF-8"):
        _close(store, body=_body(in_flight=[{"item": lone, "owner": "o", "why": "w"}]))
    with pytest.raises(CoordError, match="every key is text"):
        _close(store, body={**_body(), 1: "x"})
    assert _count(store) == 0


def test_a_retry_after_the_lease_lapsed_is_refused_by_the_fence(tmp_path):
    from datetime import datetime, timedelta, timezone

    class Clock:
        now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.now

    clock = Clock()
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    s = CoordStore(d, clock=clock)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.accept_drop("first", surface="cli")
    a = s.coord_status()["launches"][0]
    assert s.launch_activate(a["launch_id"], harness_session_id="probe-A")[1] is None
    tok_a = _token(s)
    first = sh.close(s, kind="rotation", body=_body(), request_id="r1", prev_handoff_id=None, token=tok_a)
    clock.now += timedelta(minutes=31)                          # past LEASE_TTL: the lease lapses
    with pytest.raises(LeaseRefused, match="past its until"):   # the fence runs before the stored row is looked up
        sh.close(s, kind="rotation", body=_body(), request_id="r1", prev_handoff_id=None, token=tok_a)
    # a second coordinator: A ends, a drop launches B, B holds the lease and reuses A's request id with A's words
    s.launch_end(a["launch_id"], "ended", "rotated")
    s.accept_drop("second", surface="cli")
    b = [ln for ln in s.coord_status()["launches"] if ln["state"] == "pending"][0]
    got, why = s.launch_activate(b["launch_id"], harness_session_id="probe-B")
    assert why is None and got.session_key != first.writer
    tok_b = _token(s)
    with pytest.raises(CoordError, match="already closed handoff 1 by another writer or with other words"):
        sh.close(s, kind="rotation", body=_body(), request_id="r1", prev_handoff_id=None, token=tok_b)
    assert _count(s) == 1 and sh.latest(s) == first


def test_latest_is_a_read_that_makes_nothing(tmp_path, store):
    missing = CoordStore(tmp_path / "nowhere")
    assert sh.latest(missing) is None and not (tmp_path / "nowhere").exists()
    bare = tmp_path / "bare"
    bare.mkdir(mode=0o700)
    assert sh.latest(CoordStore(bare)) is None and not CoordStore(bare).path.exists()
    assert sh.latest(store) is None                             # a store with the table and no handoff
    h = _close(store)
    assert sh.latest(store) == h
    c = sqlite3.connect(store.path)
    try:
        c.execute("DROP TABLE seam_handoffs")                   # the store as S0 left it
        c.commit()
    finally:
        c.close()
    assert sh.latest(CoordStore(store.directory)) is None
    c = sqlite3.connect(store.path)
    try:                                                        # the read made no table back
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'seam_handoffs'").fetchone() is None
    finally:
        c.close()


def test_an_empty_body_is_every_key_with_empty_lists(store):
    h = _close(store, body=_body(in_flight=[], contingencies=[], next="", notes=""))
    assert '"in_flight":[]' in h.body and '"contingencies":[]' in h.body


def test_an_oversize_body_or_list_is_refused(store):
    with pytest.raises(CoordError, match=f"at most {sh.MAX_BODY_BYTES} bytes"):
        _close(store, body=_body(notes="x" * sh.MAX_BODY_BYTES))
    too_many = [{"item": "i", "owner": "o", "why": "w"}] * (sh.LIST_MAX + 1)
    with pytest.raises(CoordError, match=f"in_flight holds at most {sh.LIST_MAX} entries"):
        _close(store, body=_body(in_flight=too_many))
    assert _count(store) == 0
    full = [{"item": "i", "owner": "o", "why": "w"}] * sh.LIST_MAX
    assert _close(store, body=_body(in_flight=full)).handoff_id == 1      # the cap itself is allowed


@pytest.mark.parametrize("request_id", [None, "", "  ", 5, "two\nlines"])
def test_a_missing_request_id_is_refused(store, request_id):
    with pytest.raises(CoordError, match="needs a request id"):
        _close(store, request_id=request_id)
    assert _count(store) == 0


def test_no_token_a_stale_token_or_the_operators_lease_is_refused(store):
    with pytest.raises(CoordError, match="needs the coordinator's identity"):
        sh.close(store, kind="rotation", body=_body(), request_id="r1", prev_handoff_id=None, token=None)
    with pytest.raises(LeaseRefused, match="is not the live lease's"):
        _close(store, token=_token(store) + 1000)
    assert _count(store) == 0
    op = CoordStore(store.directory.parent / "op")             # a store where only the operator holds the lease
    op.directory.mkdir(mode=0o700, exist_ok=True)
    tok = op.coord_take()
    with pytest.raises(LeaseRefused, match="written by an active coordinator launch; the lease holder is the operator"):
        sh.close(op, kind="operator", body=_body(), request_id="r1", prev_handoff_id=None, token=tok)
    assert _count(op) == 0


def test_no_live_coordinator_is_refused(store):
    token = _token(store)
    store.coord_release(token, by="coordinator")
    with pytest.raises(LeaseRefused, match="is not the live lease's"):
        _close(store, token=token)
    assert _count(store) == 0


def _raw_insert(conn, *, key="k", kind="rotation", gseq=900, prev=1, body='{"state_line":"a"}', rowid="NULL"):
    conn.execute("INSERT OR REPLACE INTO seam_handoffs (handoff_id, close_key, seam_kind, written_at, writer, gseq, "
                 "covers_gseq, prev_handoff_id, request_digest, body) VALUES (?, ?, ?, 't', 'w', ?, 0, ?, 'd', ?)",
                 (None if rowid == "NULL" else rowid, key, kind, gseq, prev, body))


def test_the_table_refuses_what_the_writer_refuses_and_is_append_only(store):
    _close(store)
    c = sqlite3.connect(store.path)
    try:
        for sql, msg in (("UPDATE seam_handoffs SET writer = 'x'", "a seam handoff is append-only"),
                         ("DELETE FROM seam_handoffs", "a seam handoff is append-only")):
            with pytest.raises(sqlite3.DatabaseError, match=msg):
                c.execute(sql)
        big = "a" * sh.MAX_BODY_BYTES
        crowd = ",".join(["1"] * (sh.LIST_MAX + 1))
        for kwargs, msg in (({"kind": "light"}, "kind rotation, operator or dream"),
                            ({"body": '{"state_line":" "}'}, "needs a state_line"),
                            ({"body": '{"state_line":"\\n"}'}, "needs a state_line"),
                            ({"body": '{"state_line":"\\t "}'}, "needs a state_line"),
                            ({"body": '{"state_line":"\\u000b\\f\\r"}'}, "needs a state_line"),
                            ({"body": "{}"}, "needs a state_line"),
                            ({"body": "not json"}, "needs a state_line"),
                            ({"body": '{"state_line":"%s"}' % big}, "over its size cap"),
                            ({"body": '{"state_line":"a","in_flight":[%s]}' % crowd}, "over its size cap"),
                            ({"body": '{"state_line":"a","contingencies":[%s]}' % crowd}, "over its size cap"),
                            ({"prev": None}, "a newer handoff exists"),
                            ({"prev": 7}, "a newer handoff exists"),
                            ({"key": "r1"}, "never replaced"),
                            ({"rowid": 1}, "never replaced")):
            with pytest.raises(sqlite3.DatabaseError, match=msg):
                _raw_insert(c, **kwargs)
        assert c.execute("SELECT COUNT(*) FROM seam_handoffs").fetchone()[0] == 1
    finally:
        c.close()


def test_a_store_from_before_this_slice_opens_and_takes_a_close(store):
    c = sqlite3.connect(store.path)
    try:
        c.execute("DROP TABLE seam_handoffs")                   # the store as S0 left it: no handoff table, no triggers
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        assert "seam_handoffs" not in names and not [n for n in names if n.startswith("seam_handoffs_")]
        c.commit()
    finally:
        c.close()
    old = CoordStore(store.directory)                           # a fresh object: its opener brings the schema current
    h = sh.close(old, kind="dream", body=_body(), request_id="r1", prev_handoff_id=None, token=_token(old))
    assert h.handoff_id == 1 and h.seam_kind == "dream"
    c = sqlite3.connect(store.path)
    try:
        assert c.execute("SELECT COUNT(*) FROM seam_handoffs").fetchone()[0] == 1
        assert len([r for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'seam_handoffs_%'")]) == 7
    finally:
        c.close()


def test_seam_handoffs_is_exempt_with_a_reason():
    from levain.coordinator import seam_classes as sc

    assert "seam_handoffs" in sc.EXEMPT and sc.EXEMPT["seam_handoffs"].strip()
    assert "seam_handoffs" not in sc.COUNTED and "seam_handoffs" not in sc.INFRASTRUCTURE


def test_a_retry_naming_another_previous_handoff_is_refused(store):
    first = _close(store, "r1")
    second = _close(store, "r2", prev=first.handoff_id)
    with pytest.raises(CoordError, match="naming previous handoff 1, not 2"):
        _close(store, "r2", prev=second.handoff_id)
    with pytest.raises(CoordError, match="naming previous handoff 1, not None"):
        _close(store, "r2", prev=None)
    assert _close(store, "r2", prev=first.handoff_id) == second


def test_a_prev_that_names_no_handoff_does_not_read_as_a_newer_one(store):
    with pytest.raises(CoordError, match="the close named previous handoff 5; the latest is None"):
        _close(store, "r1", prev=5)
    first = _close(store, "r1")
    with pytest.raises(CoordError, match="the close named previous handoff 9; the latest is 1"):
        _close(store, "r2", prev=9)
    for bad in (0, -3):
        with pytest.raises(CoordError, match=f"the close named previous handoff {bad}; the latest is 1"):
            _close(store, "r2", prev=bad)
    assert first.handoff_id == 1


def test_a_nul_in_a_text_field_is_a_refusal_not_an_integrity_error(store):
    with pytest.raises(CoordError, match="state_line holds a NUL"):
        _close(store, body=_body(state_line="\x00visible"))
