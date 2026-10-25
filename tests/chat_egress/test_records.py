"""CH-D2 S5a-2: the egress records (`levain.chat_egress.records`), the `chat_egress_events` rows C0 asks for.

Rows (`project_memory/1024-20/chd2_s5a/ROWS.md`, RECORDS): R15-12-B5, R15-14-4, R15-17-9, R15-26-3 (UNIQUE and the
count), R15-28-5 / R15-29-3 (`duplicate_rows`), R15-27-3 / R15-29-1 (the row counts through a scripted wire) and
R15-10-A8 (AMBIGUOUS from a real `kill -9`). Every row here is written through `lifecycle.RequestLifecycle.fire`,
the way S5b's decider writes them, except the storage-refusal test, which goes at the table directly.
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
from levain.chat_egress.lifecycle import Effect, Event, RequestLifecycle, Settled, Verdict
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
                records.write_outcome(launch_id, obs)
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
    columns = {r[1] for r in sqlite3.connect(store.path).execute("PRAGMA table_info(chat_egress_events)")}
    assert not {c for c in columns if "byte" in c or "transfer" in c or "content" in c}, columns
    # no raw response-header value is persisted (R15-21-3's rule, met by what write_outcome takes)
    raw = b"".join(p.read_bytes() for p in store.path.parent.iterdir() if p.name.startswith("dispatch.db"))
    assert b"SENTINELTYPE" not in raw and b"7, 7" not in raw
    # in 1.0 nothing but `unknown` can be stored as `ended` (an allowed attempt first, so only the CHECK can refuse)
    late = _rid()
    records.write_attempt(lid, late, Verdict.ALLOW, GET)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _raw_insert(store, lid, late, "egress_outcome", status=200, ended="complete")


# --- UNIQUE and the count (R15-26-3, R15-28-5, R15-29-3) --------------------------------------------------------

def test_a_second_row_of_a_kind_is_refused_counted_and_logged_once_per_launch(records, store, caplog):
    lid, quiet = make_launch(store), make_launch(store)
    rid = _rid()
    caplog.set_level(logging.WARNING, logger=rec.__name__)
    assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is True
    assert records.write_attempt(lid, rid, Verdict.ALLOW, GET) is False
    assert _kinds(records, lid, rid) == ["egress_attempt"]
    assert records.duplicate_rows(lid) == 1
    assert records.write_attempt(lid, rid, Verdict.DENY, GET) is False
    assert records.duplicate_rows(lid) == 2
    obs = wire.ObsResponse(rid, 200, None, "1")
    assert records.write_outcome(lid, obs) is True
    assert records.write_outcome(lid, obs) is False           # a second outcome: the same rule
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
    # the allow frame could not be sent: the attempt landed, the mark is written, a later report is absorbed
    ([Event.ALLOW, "RESP"], {Effect.SEND_ALLOW}, ["egress_attempt", "egress_failed"], Settled.FAILED),
    # the client left while deciding and the release raised: attempt + ended landed, the mark beside them
    (["ENDED"], {Effect.RELEASE}, ["egress_attempt", "egress_ended_before_outcome", "egress_failed"], Settled.FAILED),
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
