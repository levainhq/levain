"""P8b-i: ``levain.agents.gauges`` — read-only gauges that report a value and their own staleness."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from levain.agents import gauges as g
from levain.agents.store import open_agent_store, open_agent_store_view
from levain.autonomic.db import DB_NAME

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
# 181 bytes: the shape of an upstream error page written where an input should be
STUB = (b'{"error": {"type": "overloaded_error", "message": "Overloaded. The upstream service returned HTTP 529 '
        b'and no body; retry later or contact support with the request id req_0000000000000000"}}')


def seed_run(store, state: str, ended: str, name: str = "scratch") -> str:
    agent_id = "ag-" + name
    with store.write() as c:
        c.execute("INSERT INTO agents (agent_id, name, current_version, registration, operation, failure_streak, "
                  "created_at) VALUES (?,?,?,?,?,?,?)", (agent_id, name, 1, "registered", "active", 0, ended))
        c.execute("INSERT INTO agent_specs (agent_id, version, spec_hash, role_hash, spec, created_at) "
                  "VALUES (?,?,?,?,?,?)", (agent_id, 1, "h", "r", "{}", ended))
        c.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, reason, window_key, "
                  "spec_hash, binding_id, run_id, admitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ("oc-1", agent_id, "scheduled", "k1", "admitted", None, "w", "h", "b", "run-1", ended))
        if state in ("done", "failed"):    # the triggers let a run reach a terminal state only through launched
            c.execute("UPDATE agent_occurrences SET state = 'launched', launch_epoch = 0, link_id = 'l', "
                      "link_incarnation = 'i', launched_at = ?", (ended,))
        c.execute("UPDATE agent_occurrences SET state = ?, reason = 'test', ended_at = ?", (state, ended))
    return agent_id


def test_a_181_byte_error_stub_is_no_real_input(tmp_path):
    src = tmp_path / "input.json"
    src.write_bytes(STUB[:181].ljust(181, b" "))
    assert src.stat().st_size == 181
    r = g.input_liveness(src, NOW)
    assert r.value == g.QUIET and "no real input" in r.detail and "181-byte" in r.detail
    src.write_bytes(b"line of real data\n" * 100)
    assert g.input_liveness(src, NOW).value == g.OK
    assert g.input_liveness(tmp_path / "gone", NOW).value == g.DEAD


def test_a_fresh_and_a_stale_gauge_report_their_staleness(tmp_path):
    store = open_agent_store(tmp_path / "s")
    aid = seed_run(store, "done", "2026-11-02T11:00:00Z")
    with store.read() as c:
        fresh = g.run_health(c, aid, NOW, stale_after=7200)
        stale = g.run_health(c, aid, NOW, stale_after=600)
    assert (fresh.value, fresh.age_seconds, fresh.stale) == (g.OK, 3600, False)
    assert (stale.value, stale.age_seconds, stale.stale) == (g.OK, 3600, True)


def test_a_gauge_never_writes(tmp_path):
    store = open_agent_store(tmp_path / "s")
    aid = seed_run(store, "done", "2026-11-02T11:00:00Z")
    dest = tmp_path / "out.md"
    dest.write_text("x")
    src = tmp_path / "in.txt"
    src.write_bytes(STUB[:181])
    db = tmp_path / "s" / DB_NAME
    before = db.read_bytes()
    with open_agent_store_view(tmp_path / "s").read() as c:    # a mode=ro connection: any write would raise
        g.run_health(c, aid, NOW)
        g.output_arrival(c, aid, dest, NOW)
        g.fate_of_output(c, aid, {"class": "recovery", "drill_cadence": 86400}, NOW)
    g.input_liveness(src, NOW)
    assert db.read_bytes() == before and src.read_bytes() == STUB[:181] and dest.read_text() == "x"


def test_an_agent_with_no_runs_is_unknown_not_healthy(tmp_path):
    store = open_agent_store(tmp_path / "s")
    with store.write() as c:
        c.execute("INSERT INTO agents (agent_id, name, current_version, registration, operation, failure_streak, "
                  "created_at) VALUES ('ag-new','new',1,'registered','active',0,'2026-11-02T00:00:00Z')")
    with store.read() as c:
        health = g.run_health(c, "ag-new", NOW)
        arrival = g.output_arrival(c, "ag-new", tmp_path / "out", NOW)
        fate = g.fate_of_output(c, "ag-new", {"class": "recovery", "drill_cadence": 3600}, NOW)
    assert health.value == arrival.value == fate.value == g.UNKNOWN
    assert health.stale and health.observed_at is None
    assert g.input_liveness(None, NOW).value == g.UNKNOWN


def revise(store, agent_id: str) -> None:
    with store.write() as c:
        c.execute("INSERT INTO agent_specs (agent_id, version, spec_hash, role_hash, spec, created_at) "
                  "VALUES (?,2,'h2','r','{}','2026-11-02T11:30:00Z')", (agent_id,))
        c.execute("UPDATE agents SET current_version = 2 WHERE agent_id = ?", (agent_id,))


def test_a_revised_agent_that_never_ran_reads_unknown_not_the_old_runs_health(tmp_path):
    store = open_agent_store(tmp_path / "s")
    aid = seed_run(store, "done", "2026-11-02T11:00:00Z")
    dest = tmp_path / "out.md"
    dest.write_text("x")
    with store.read() as c:
        assert g.run_health(c, aid, NOW).value == g.OK
        assert g.fate_of_output(c, aid, {"class": "recovery", "drill_cadence": 86400}, NOW).value == g.OK
    revise(store, aid)    # v2 turns a push agent into a recovery one, and has never run
    with store.read() as c:
        assert g.run_health(c, aid, NOW).value == g.UNKNOWN
        assert g.fate_of_output(c, aid, {"class": "recovery", "drill_cadence": 86400}, NOW).value == g.UNKNOWN
        assert g.output_arrival(c, aid, dest, NOW).value == g.UNKNOWN


def test_a_small_real_input_that_mentions_error_reads_ok(tmp_path):
    src = tmp_path / "in.json"
    for body in (b'{"task": "fix the login error on the checkout page", "ticket": 1234, "notes": "' + b"x" * 240 + b'"}',
                 b'{"status": "ok", "error_count": 0}', b'{"message": "quarterly report"}',
                 b"the build failed twice with a timeout, please summarise the cause " * 4):
        src.write_bytes(body)
        assert g.input_liveness(src, NOW).value == g.OK, body
    for body in (b"Traceback (most recent call last):\n  File x", b"<html><body>502 Bad Gateway</body></html>",
                 b'{"error": "unauthorized"}', b"Error: 401 Unauthorized", b"HTTP/1.1 503 Service Unavailable"):
        src.write_bytes(body)
        assert g.input_liveness(src, NOW).value == g.QUIET, body


def test_an_unusable_path_reads_unknown_and_never_raises(tmp_path):
    class Odd:
        def __fspath__(self):
            return str(tmp_path / "odd.txt")
    (tmp_path / "odd.txt").write_text("real data")
    assert g.input_liveness("~no-such-user-zz/f", NOW).value == g.UNKNOWN
    assert g.input_liveness("bad\0path", NOW).value == g.UNKNOWN
    assert g.input_liveness(123.5, NOW).value == g.UNKNOWN
    assert g.input_liveness(Odd(), NOW).value == g.OK
    store = open_agent_store(tmp_path / "s")
    aid = seed_run(store, "done", "2026-11-02T11:00:00Z")
    with store.read() as c:
        assert g.output_arrival(c, aid, "~no-such-user-zz/f", NOW).value == g.UNKNOWN


def test_a_directory_or_fifo_is_not_a_file(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert g.input_liveness(tmp_path, NOW).value == g.UNKNOWN
    assert g.input_liveness(fifo, NOW).value == g.UNKNOWN    # opened non-blocking: it must not hang
    store = open_agent_store(tmp_path / "s")
    aid = seed_run(store, "done", "2026-11-02T11:00:00Z")
    with store.read() as c:
        assert g.output_arrival(c, aid, tmp_path, NOW).value == g.UNKNOWN


def test_the_size_and_the_bytes_come_from_the_same_descriptor(tmp_path, monkeypatch):
    src = tmp_path / "in.txt"
    src.write_bytes(b"x" * 2048)
    real = os.fstat

    def swap(fd):    # the producer replaces the file with an empty one after the descriptor is open
        out = real(fd)
        os.replace(src, tmp_path / "old")
        src.write_bytes(b"")
        return out
    monkeypatch.setattr(g.os, "fstat", swap)
    r = g.input_liveness(src, NOW)
    assert r.value == g.OK and "2048" in r.detail    # the descriptor still reads the 2 KB file it opened


def test_a_tie_on_the_stamp_breaks_by_insertion_and_a_launched_run_dates_from_launch(tmp_path):
    store = open_agent_store(tmp_path / "s")
    aid = seed_run(store, "done", "2026-11-02T11:00:00Z")
    with store.write() as c:    # a later claim in the same second, its id sorting BEFORE the done one
        c.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, reason, window_key, "
                  "spec_hash, binding_id, run_id, admitted_at, ended_at) VALUES ('oc-0',?,'scheduled','k2',"
                  "'skipped-overlap','overlap','w2','h','b','run-2','2026-11-02T11:00:00Z','2026-11-02T11:00:00Z')",
                  (aid,))
    with store.read() as c:
        h = g.run_health(c, aid, NOW)
    assert h.value == g.UNKNOWN and "skipped-overlap" in h.detail
    store2 = open_agent_store(tmp_path / "t")
    aid2 = seed_run(store2, "admitted", "2026-10-30T11:00:00Z", name="slow")
    with store2.write() as c:
        c.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=0, link_id='l', link_incarnation='i', "
                  "launched_at='2026-11-02T11:59:00Z', ended_at=NULL")
    with store2.read() as c:
        h2 = g.run_health(c, aid2, NOW)
    assert h2.age_seconds == 60 and not h2.stale
