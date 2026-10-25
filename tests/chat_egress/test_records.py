"""CH-D2 S5a-2: the egress records (`levain.chat_egress.records`), the `chat_egress_events` rows C0 asks for.

Rows (`project_memory/1024-20/chd2_s5a/ROWS.md`, RECORDS): R15-12-B5, R15-14-4, R15-17-9, R15-26-3 (UNIQUE and the
count), R15-28-5 / R15-29-3 (`duplicate_rows`), R15-27-3 / R15-29-1 (the row counts through a scripted wire) and
R15-10-A8 (AMBIGUOUS from a real `kill -9`), plus the S5a-2 L1/L2 fix round (the head's rulings in
`project_memory/1024-20/chd2_s5a/HEAD_RULINGS.md`). The scripted-wire, race and FAILED tests write through
`lifecycle.RequestLifecycle.fire`, the way S5b's decider writes them. The duplicate, outcome, settle-precedence and
input tests also call `EgressRecords`' writes directly, and the storage-refusal and schema-check tests go at the
table and `sqlite_master` with raw SQL.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from levain.chat_egress import records as rec
from levain.chat_egress import wire
from levain.chat_egress.lifecycle import Effect, Event, Failure, RequestLifecycle, Settled, State, Step, Verdict
from levain.coordinator.store import CoordStore, next_gseq

_CHILD = Path(__file__).with_name("records_kill_child.py")
_WORKTREE = Path(__file__).resolve().parents[2]

GET = rec.Attempt(host="example.com", method="GET", path="/index.html", names=(), setup_record_id=None,
                  attach=rec.Attach.NONE, deciding_step="read", open_network=False)


def _rid() -> str:
    return str(uuid.uuid4())


def make_launch(store: CoordStore) -> str:
    """A launch row for the rows to reference (`launches` is the dispatch store's; foreign keys are on)."""
    lid = "ln-" + uuid.uuid4().hex
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = store._stamp()
        conn.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, model, cwd, token, "
                     "placement, at) VALUES (?, ?, ?, 'claude', 'worker', '[]', NULL, NULL, 0, 'operator-uid', ?)",
                     (lid, next_gseq(conn, now), "s-" + uuid.uuid4().hex[:16], now))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return lid


@pytest.fixture
def store(tmp_path):
    return CoordStore(tmp_path / "dispatch")


@pytest.fixture
def records(store):
    return rec.EgressRecords(store)


def _ct_class(obs: wire.ObsResponse) -> str:
    """The decider's R15-21-3 enum for these tests' responses (none is an advertisement; S3b owns the classifier)."""
    return rec.CT_ABSENT if obs.content_type is None else rec.CT_OTHER


def drive(records: rec.EgressRecords, launch_id: str, request_id: str, script, attempt: rec.Attempt = GET,
          raising: frozenset = frozenset()):
    """A scripted wire: each item is a lifecycle Event the decider makes, or an observation-channel LINE the glue
    wrote (parsed by `wire.parse_obs_line`, as the decider's drain reads it). `perform(effect, step)` runs the write
    effects; the send, card and release effects are the composer's and are only collected here. An effect in
    `raising` raises, and the handler hands `lc.failure` to `records.record_failure`."""
    lc = RequestLifecycle()
    sent: list[Effect] = []
    for item in script:
        obs = None
        if isinstance(item, bytes):
            obs = wire.parse_obs_line(item)
            event = Event.RESPONSE if isinstance(obs, wire.ObsResponse) else Event.ENDED
        else:
            event = item

        def perform(eff, step, obs=obs):
            if eff in raising:
                raise OSError(f"scripted failure of {eff}")
            if eff is Effect.WRITE_ATTEMPT:
                records.write_attempt(launch_id, request_id, step.verdict, attempt)
            elif eff is Effect.WRITE_OUTCOME:
                records.write_outcome(launch_id, obs, _ct_class(obs))
            elif eff is Effect.WRITE_ENDED:
                records.write_ended(launch_id, request_id)
            else:
                sent.append(eff)

        try:
            lc.fire(event, perform)
        except OSError:
            # the composer's handler: the head's FAILED ruling, through records
            records.record_failure(launch_id, request_id, lc.failure)
    return lc, sent


def _kinds(records, launch_id, request_id):
    return [r.kind for r in records.rows(launch_id, request_id)]


# --- the row counts through a scripted wire (R15-27-3, R15-29-1 + R15-30-4/5, R15-10-A8's "both rows") ----------

@pytest.mark.parametrize("name, script, kinds, verdict, settled", [
    ("denied GET -> attempt only", [Event.DENY, "ENDED"], ["egress_attempt"], Verdict.DENY, Settled.DENIED),
    ("normal 200 -> attempt + outcome, srt's normal-close abort writes no row",
     [Event.ALLOW, "RESP", "ENDED"], ["egress_attempt", "egress_outcome"], Verdict.ALLOW, Settled.COMPLETED),
    ("held card, client leaves -> attempt + ended", [Event.CARD, "ENDED", Event.ALLOW],
     ["egress_attempt", "egress_ended_before_outcome"], Verdict.UNDECIDED, Settled.ENDED),
    ("srt's 502 after an allow -> attempt + ended", [Event.ALLOW, "ENDED", "RESP"],
     ["egress_attempt", "egress_ended_before_outcome"], Verdict.ALLOW, Settled.ENDED),
    ("allowed, launch closed with no report -> attempt only, AMBIGUOUS", [Event.ALLOW, Event.CLOSE],
     ["egress_attempt"], Verdict.ALLOW, Settled.AMBIGUOUS),
])
def test_rows_per_request_through_a_scripted_wire(records, store, name, script, kinds, verdict, settled):
    lid, rid = make_launch(store), _rid()
    lines = {"RESP": wire.obs_response_line(rid, 200, "text/html", "42"), "ENDED": wire.obs_ended_line(rid)}
    drive(records, lid, rid, [lines.get(s, s) if isinstance(s, str) else s for s in script])
    assert _kinds(records, lid, rid) == kinds
    assert records.rows(lid, rid)[0].verdict == verdict
    assert records.settled(lid, rid) is settled
    assert records.duplicate_rows(lid) == 0


# --- the outcome row (R15-12-B5, R15-14-4, R15-17-9) ------------------------------------------------------------

def test_outcome_has_status_declared_length_and_ended_unknown_and_no_byte_field(records, store):
    lid = make_launch(store)
    done, cut, odd = _rid(), _rid(), _rid()
    drive(records, lid, done, [Event.ALLOW, wire.obs_response_line(done, 200, "text/plain", "42")])
    # cut mid-body: the outcome came at the head, then the abort; the row is the same shape
    drive(records, lid, cut, [Event.ALLOW, wire.obs_response_line(cut, 206, None, None), wire.obs_ended_line(cut)])
    drive(records, lid, odd, [Event.ALLOW, wire.obs_response_line(odd, 404, "x/SENTINELTYPE", "7, 7")])
    got = {r.request_id: r for r in records.rows(lid) if r.kind == "egress_outcome"}
    assert (got[done].status, got[done].declared_length, got[done].ended) == (200, 42, "unknown")
    assert (got[cut].status, got[cut].declared_length, got[cut].ended) == (206, None, "unknown")
    assert (got[odd].status, got[odd].declared_length, got[odd].ended) == (404, None, "unknown")
    # R15-21-3: the enum is what is stored about the Content-Type (O-5)
    assert [got[k].content_type_class for k in (done, cut, odd)] == ["other", "absent", "other"]
    columns = {r[1] for r in sqlite3.connect(store.path).execute("PRAGMA table_info(chat_egress_events)")}
    raw_columns = columns - {"content_type_class"}
    assert not {c for c in raw_columns if "byte" in c or "transfer" in c or "content" in c}, columns
    # no raw response-header value is persisted (R15-21-3: only the enum and the parsed integer)
    raw = b"".join(p.read_bytes() for p in store.path.parent.iterdir() if p.name.startswith("dispatch.db"))
    assert b"SENTINELTYPE" not in raw and b"7, 7" not in raw
    # in 1.0 nothing but `unknown` can be stored as `ended` (an allowed attempt first, so only the CHECK can refuse)
    late = _rid()
    records.write_attempt(lid, late, Verdict.ALLOW, GET)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _raw_insert(store, lid, late, "egress_outcome", status=200, ended="complete", content_type_class="other")


def test_o5_the_content_type_enum_is_required_bounded_and_agrees_with_the_observation(records, store):
    """O-5 (R15-21-3): the outcome row stores the decider's enum, never a raw Content-Type."""
    lid = make_launch(store)
    adv, absent = _rid(), _rid()
    for rid in (adv, absent):
        records.write_attempt(lid, rid, Verdict.ALLOW, GET)
    ok = wire.ObsResponse(adv, 200, "application/x-git-upload-pack-advertisement; charset=x", None)
    with pytest.raises(rec.EgressRecordError):
        records.write_outcome(lid, ok, "application/x-git-upload-pack-advertisement")   # a raw value, not the enum
    with pytest.raises(rec.EgressRecordError):
        records.write_outcome(lid, ok, rec.CT_ABSENT)       # it had a Content-Type
    with pytest.raises(rec.EgressRecordError):
        records.write_outcome(lid, wire.ObsResponse(absent, 200, None, None), rec.CT_OTHER)   # it had none
    assert records.write_outcome(lid, ok, rec.CT_ADVERTISEMENT) is True
    assert records.write_outcome(lid, wire.ObsResponse(absent, 200, None, None), rec.CT_ABSENT) is True
    assert [r.content_type_class for r in records.rows(lid) if r.kind == "egress_outcome"] == [
        "git-upload-pack-advertisement", "absent"]
    late = _rid()
    records.write_attempt(lid, late, Verdict.ALLOW, GET)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):       # the table: the enum only
        _raw_insert(store, lid, late, "egress_outcome", status=200, ended="unknown", content_type_class="text/html")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):       # and an outcome always carries it
        _raw_insert(store, lid, late, "egress_outcome", status=200, ended="unknown")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):       # and nothing else does
        _raw_insert(store, lid, late, "egress_ended_before_outcome", content_type_class="other")


# --- UNIQUE and the count (R15-26-3, R15-28-5, R15-29-3) --------------------------------------------------------

def test_a_second_row_of_a_kind_is_refused_counted_and_logged_once_per_launch(records, store, caplog):
    lid, quiet = make_launch(store), make_launch(store)
    rid = _rid()
    caplog.set_level(logging.WARNING, logger=rec.__name__)
    assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is True
    assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is False
    assert _kinds(records, lid, rid) == ["egress_attempt"]
    assert records.duplicate_rows(lid) == 1
    assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is False
    assert records.duplicate_rows(lid) == 2
    obs = wire.ObsResponse(rid, 200, None, "1")
    assert records.write_outcome(lid, obs, rec.CT_ABSENT) is True
    assert records.write_outcome(lid, obs, rec.CT_ABSENT) is False   # a second outcome: the same rule
    assert records.write_ended(lid, rid) is False             # the end slot is taken: at most one of the two
    assert _kinds(records, lid, rid) == ["egress_attempt", "egress_outcome"]
    assert records.duplicate_rows(lid) == 4
    assert len([r for r in caplog.records if r.name == rec.__name__]) == 1
    assert records.duplicate_rows(quiet) == 0                  # a normal launch
    # two audit-missing rows (request_id null) in one launch: both stored, nothing counted
    missing = rec.Attempt(host="example.com", method="GET", path="/", names=(), setup_record_id=None,
                          attach=rec.Attach.NOT_EVALUATED, deciding_step="audit-missing", open_network=False)
    assert records.write_attempt(quiet, None, Verdict.DENY, missing) is True
    assert records.write_attempt(quiet, None, Verdict.DENY, missing) is True
    assert [r.request_id for r in records.rows(quiet)] == [None, None]
    assert records.duplicate_rows(quiet) == 0
    with pytest.raises(rec.EgressRecordError):               # a null id is the audit-missing deny only
        records.write_attempt(quiet, None, Verdict.ALLOW, GET)


@pytest.mark.parametrize("verdict, attempt", [
    (Verdict.DENY, GET),                                                         # the verdict differs
    (Verdict.ALLOW, rec.Attempt(host="example.com", method="GET", path="/other", names=(), setup_record_id=None,
                                attach=rec.Attach.NONE, deciding_step="read", open_network=False)),   # the path
    (Verdict.ALLOW, rec.Attempt(host="example.com", method="GET", path="/index.html", names=(),
                                setup_record_id=None, attach=rec.Attach.NONE, deciding_step="read",
                                open_network=True)),                             # open_network
], ids=["verdict", "path", "open_network"])
def test_l1_1_a_second_attempt_with_a_different_decision_raises_and_is_not_counted(records, store, verdict,
                                                                                    attempt):
    """L1-1: a reused request id whose decision differs from the stored one fails the effect (lifecycle's FAILED
    path), so the composer can never send a verdict the record does not hold. Only an identical replay is False."""
    lid, rid = make_launch(store), _rid()
    assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is True
    with pytest.raises(rec.EgressRecordError, match="different decision"):
        records.write_attempt(lid, rid, verdict, attempt)
    assert [(r.kind, r.verdict) for r in records.rows(lid, rid)] == [("egress_attempt", Verdict.ALLOW)]
    assert records.duplicate_rows(lid) == 0
    assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is False          # the identical replay
    assert records.duplicate_rows(lid) == 1
    # through lifecycle: the raise is an effect failure, and the deny frame that disagrees is never sent
    lc, sent = RequestLifecycle(), []

    def perform(eff, step):
        if eff is Effect.WRITE_ATTEMPT:
            records.write_attempt(lid, rid, step.verdict, GET)
        else:
            sent.append(eff)

    with pytest.raises(rec.EgressRecordError):
        lc.fire(Event.DENY, perform)
    assert lc.state.value == "failed" and lc.failure.effect is Effect.WRITE_ATTEMPT and sent == []


def test_l1_3_open_network_must_be_a_bool(records, store):
    lid = make_launch(store)
    for bad in ("False", 1, 0, None):
        a = rec.Attempt(host="example.com", method="GET", path="/", names=(), setup_record_id=None,
                        attach=rec.Attach.NONE, deciding_step="read", open_network=bad)  # type: ignore[arg-type]
        with pytest.raises(rec.EgressRecordError, match="open_network"):
            records.write_attempt(lid, _rid(), Verdict.ALLOW, a)
    assert records.rows(lid) == []


def test_l1_5_the_same_failed_mark_again_is_a_silent_replay(records, store, caplog):
    lid, rid, other = make_launch(store), _rid(), _rid()
    caplog.set_level(logging.WARNING, logger=rec.__name__)
    assert records.write_failed(lid, rid, Effect.SEND_ALLOW) is True
    assert records.write_failed(lid, rid, Effect.SEND_ALLOW) is False      # a retry of the same mark
    assert records.duplicate_rows(lid) == 0
    assert [r for r in caplog.records if r.name == rec.__name__] == []
    # the once-per-launch log slot is still there for a real duplicate
    assert records.write_failed(lid, rid, Effect.RELEASE) is False         # a different effect: a real duplicate
    assert records.write_attempt(lid, other, Verdict.ALLOW, GET) is True
    assert records.write_attempt(lid, other, Verdict.ALLOW, GET) is False
    assert records.duplicate_rows(lid) == 2
    assert len([r for r in caplog.records if r.name == rec.__name__]) == 1


def test_an_abort_racing_the_outcome_through_c0_stores_one_row_and_counts_nothing(records, store):
    lid = make_launch(store)
    for first, second in (("RESP", "ENDED"), ("ENDED", "RESP")):
        rid = _rid()
        lines = {"RESP": wire.obs_response_line(rid, 200, None, None), "ENDED": wire.obs_ended_line(rid)}
        drive(records, lid, rid, [Event.ALLOW, lines[first], lines[second]])
        assert len(records.rows(lid, rid)) == 2
    assert records.duplicate_rows(lid) == 0


# --- the table holds its own rules, whoever writes ----------------------------------------------------------------

def _raw_insert(store, lid, rid, kind, **cols):
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = store._stamp()
        names = ["gseq", "launch_id", "request_id", "kind", "at", *cols]
        conn.execute(f"INSERT INTO chat_egress_events ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                     (next_gseq(conn, now), lid, rid, kind, now, *cols.values()))
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_the_table_refuses_what_c0_never_asks_for(records, store):
    lid, rid, denied = make_launch(store), _rid(), _rid()
    records.write_attempt(lid, rid, Verdict.ALLOW, GET)
    records.write_attempt(lid, denied, Verdict.DENY, GET)
    attempt_cols = dict(verdict="allow", deciding_step="read", host="h", method="GET", path="/", names="[]",
                        attach="none", open_network=0)
    with pytest.raises(sqlite3.IntegrityError):                          # UNIQUE (launch_id, request_id, kind)
        _raw_insert(store, lid, rid, "egress_attempt", **attempt_cols)
    conn = sqlite3.connect(store.path)
    unique = [[c[2] for c in conn.execute(f"PRAGMA index_info('{ix[1]}')")]
              for ix in conn.execute("PRAGMA index_list(chat_egress_events)") if ix[2]]
    assert ["launch_id", "request_id", "kind"] in unique, unique     # the constraint itself, not only the trigger
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):                          # an outcome joins an ALLOW attempt
        _raw_insert(store, lid, denied, "egress_outcome", status=200, ended="unknown")
    with pytest.raises(sqlite3.IntegrityError):                          # never after a deny either
        _raw_insert(store, lid, denied, "egress_ended_before_outcome")
    with pytest.raises(sqlite3.IntegrityError):                          # no attempt at all
        _raw_insert(store, lid, _rid(), "egress_ended_before_outcome")
    _raw_insert(store, lid, rid, "egress_ended_before_outcome")
    with pytest.raises(sqlite3.IntegrityError):                          # outcome XOR ended-before-outcome
        _raw_insert(store, lid, rid, "egress_outcome", status=200, ended="unknown")
    with pytest.raises(sqlite3.IntegrityError):                          # never a raw query in the path
        _raw_insert(store, lid, _rid(), "egress_attempt", **{**attempt_cols, "path": "/x?token=SECRET"})
    conn = sqlite3.connect(store.path)
    for sql in ("UPDATE chat_egress_events SET verdict = 'deny'", "DELETE FROM chat_egress_events"):
        with pytest.raises(sqlite3.IntegrityError):                      # append-only
            conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):                          # INSERT OR REPLACE cannot take a key
        conn.execute("INSERT OR REPLACE INTO chat_egress_events (gseq, launch_id, request_id, kind, verdict, "
                     "deciding_step, host, method, path, names, attach, open_network, at) SELECT gseq + 1000, "
                     "launch_id, request_id, kind, 'deny', deciding_step, host, method, path, names, attach, "
                     "open_network, at FROM chat_egress_events WHERE request_id = ? AND kind = 'egress_attempt'",
                     (rid,))
    assert _kinds(records, lid, rid) == ["egress_attempt", "egress_ended_before_outcome"]


# --- FAILED (the head's rulings on S5a-1's L3 F1): a durable mark for a non-database failure, nothing for a DB one --

@pytest.mark.parametrize("script, raising, kinds, settled", [
    # the allow frame raised: the attempt landed, the mark is written, a later report is absorbed; whether the frame
    # left is unknown, so it reads AMBIGUOUS (the S5a-1 ruling, L2-7)
    ([Event.ALLOW, "RESP"], {Effect.SEND_ALLOW}, ["egress_attempt", "egress_failed"], Settled.AMBIGUOUS),
    # the deny frame raised: attempt + mark, FAILED (srt is owed an answer)
    ([Event.DENY], {Effect.SEND_DENY}, ["egress_attempt", "egress_failed"], Settled.FAILED),
    # the client left while deciding and the release raised: attempt + ended landed, the mark beside them; the
    # stored end row beats the mark (L2-7)
    (["ENDED"], {Effect.RELEASE}, ["egress_attempt", "egress_ended_before_outcome", "egress_failed"], Settled.ENDED),
    # the card's end raised before any row: the mark alone
    ([Event.CARD, "ENDED"], {Effect.CARD_ENDED}, ["egress_failed"], Settled.FAILED),
    # the outcome WRITE itself failed: nothing more is written, and the allowed attempt reads AMBIGUOUS
    ([Event.ALLOW, "RESP"], {Effect.WRITE_OUTCOME}, ["egress_attempt"], Settled.AMBIGUOUS),
    # the attempt write failed: no row at all
    ([Event.ALLOW], {Effect.WRITE_ATTEMPT}, [], None),
])
def test_failed_is_marked_for_a_send_or_card_failure_and_never_after_a_failed_row_write(
        records, store, script, raising, kinds, settled):
    lid, rid = make_launch(store), _rid()
    lines = {"RESP": wire.obs_response_line(rid, 200, None, None), "ENDED": wire.obs_ended_line(rid)}
    lc, _ = drive(records, lid, rid, [lines.get(s, s) for s in script], raising=frozenset(raising))
    assert lc.state.value == "failed"
    after = rec.EgressRecords(CoordStore(store.directory))     # a restarted reader
    assert [r.kind for r in after.rows(lid, rid)] == kinds
    assert after.settled(lid, rid) is settled
    with pytest.raises(rec.EgressRecordError):
        records.write_failed(lid, rid, Effect.WRITE_OUTCOME)   # a row write's failure is never marked
    marks = [r.failed_effect for r in after.rows(lid, rid) if r.kind == "egress_failed"]
    assert marks == [e.value for e in raising if e not in (Effect.WRITE_ATTEMPT, Effect.WRITE_OUTCOME)]


@pytest.mark.parametrize("order, mark, settled", [
    # the L2 probe's case: the mark first, then the allow attempt and its outcome: the outcome proves the send
    (["mark", "attempt", "outcome"], Effect.SEND_ALLOW, Settled.COMPLETED),
    (["attempt", "outcome", "mark"], Effect.SEND_ALLOW, Settled.COMPLETED),
    (["attempt", "outcome", "mark"], Effect.RELEASE, Settled.COMPLETED),
    (["attempt", "ended", "mark"], Effect.SEND_ALLOW, Settled.ENDED),
    (["attempt", "ended", "mark"], Effect.RELEASE, Settled.ENDED),
    # an allow with no end row: SEND_ALLOW raised -> unknown whether the frame left; any other effect -> FAILED
    (["attempt", "mark"], Effect.SEND_ALLOW, Settled.AMBIGUOUS),
    (["attempt", "mark"], Effect.RELEASE, Settled.FAILED),
    (["attempt", "mark"], Effect.CARD_ENDED, Settled.FAILED),
    (["mark"], Effect.CARD_ENDED, Settled.FAILED),
], ids=lambda v: v if isinstance(v, str) else None)
def test_l2_7_stored_end_rows_beat_the_failed_mark(records, store, order, mark, settled):
    """L2-7: the rows prove what was sent, so a stored end row reads as itself over the FAILED mark; the mark stays
    readable on its row for the card."""
    lid, rid = make_launch(store), _rid()
    for what in order:
        if what == "mark":
            assert records.write_failed(lid, rid, mark) is True
        elif what == "attempt":
            assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is True
        elif what == "outcome":
            assert records.write_outcome(lid, wire.ObsResponse(rid, 200, None, "0"), rec.CT_ABSENT) is True
        else:
            assert records.write_ended(lid, rid) is True
    after = rec.EgressRecords(CoordStore(store.directory))
    assert after.settled(lid, rid) is settled
    assert [r.failed_effect for r in after.rows(lid, rid) if r.kind == "egress_failed"] == [mark.value]


def test_l2_7_an_undecided_attempt_with_an_ended_row_reads_ended_over_the_mark(records, store):
    lid, rid = make_launch(store), _rid()
    drive(records, lid, rid, [Event.CARD, wire.obs_ended_line(rid)], raising=frozenset({Effect.RELEASE}))
    assert _kinds(records, lid, rid) == ["egress_attempt", "egress_ended_before_outcome", "egress_failed"]
    assert records.rows(lid, rid)[0].verdict is Verdict.UNDECIDED
    assert records.settled(lid, rid) is Settled.ENDED


# --- L2-3: a store holding another revision's table or trigger is refused at open --------------------------------

def _tamper(store, *sql):
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        for s in sql:
            conn.execute(s)
    finally:
        conn.close()


@pytest.mark.parametrize("change", [
    # a trigger body that enforces nothing, under the same name
    ("DROP TRIGGER chat_egress_events_one_end",
     "CREATE TRIGGER chat_egress_events_one_end BEFORE INSERT ON chat_egress_events BEGIN SELECT 1; END"),
    # an extra object on the table (a trigger that is merely gone is re-created by the open, then compares equal)
    ("CREATE INDEX chat_egress_events_extra ON chat_egress_events (host)",),
], ids=["trigger-body", "extra-index"])
def test_l2_3_a_store_whose_egress_objects_differ_is_refused_at_open(records, store, change):
    lid = make_launch(store)
    records.write_attempt(lid, _rid(), Verdict.ALLOW, GET)       # the store exists with this module's schema
    assert rec.EgressRecords(CoordStore(store.directory)).rows(lid)   # a fresh open accepts its own schema
    _tamper(store, *change)
    fresh = rec.EgressRecords(CoordStore(store.directory))
    with pytest.raises(rec.EgressRecordError, match="differ"):
        fresh.rows(lid)
    with pytest.raises(rec.EgressRecordError, match="differ"):    # every later call too: never half-open
        fresh.write_attempt(lid, _rid(), Verdict.ALLOW, GET)


def test_l2_3_a_stale_check_is_refused_at_open_never_inside_the_failure_handler(tmp_path):
    """L1-2: a store whose `failed_effect` CHECK predates an effect (here: one without 'release') is refused when it
    is opened, so `record_failure` never meets the stale CHECK as an IntegrityError."""
    store = CoordStore(tmp_path / "dispatch")
    lid = make_launch(store)
    stale = tuple(s.replace(", 'release')", ")") if s.startswith("CREATE TABLE") else s for s in rec.EGRESS_SCHEMA)
    assert stale != rec.EGRESS_SCHEMA
    _tamper(store, *stale)
    with pytest.raises(rec.EgressRecordError, match="differ"):
        rec.EgressRecords(CoordStore(store.directory)).record_failure(
            lid, _rid(), Failure(Step(State.ENDED), Effect.RELEASE, ()))


# --- R15-10-A8: AMBIGUOUS from a real kill -9 ---------------------------------------------------------------------

def test_r15_10_a8_a_killed_service_leaves_an_attempt_that_reads_ambiguous(tmp_path):
    directory = tmp_path / "dispatch"
    store = CoordStore(directory)
    lid, done, cut = make_launch(store), _rid(), _rid()
    env = {**os.environ, "PYTHONPATH": str(_WORKTREE)}
    child = subprocess.Popen([sys.executable, str(_CHILD), str(directory), lid, done, cut],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        line = child.stdout.readline()          # the child prints this only after it SENT the second allow
        assert line == b"ALLOW-SENT " + cut.encode() + b"\n", (line, child.stderr.read() if child.poll() else b"")
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    after = rec.EgressRecords(CoordStore(directory))           # the restarted reader: a fresh store object
    assert [r.kind for r in after.rows(lid, done)] == ["egress_attempt", "egress_outcome"]
    assert after.settled(lid, done) is Settled.COMPLETED
    assert [(r.kind, r.verdict) for r in after.rows(lid, cut)] == [("egress_attempt", Verdict.ALLOW)]
    assert after.settled(lid, cut) is Settled.AMBIGUOUS
