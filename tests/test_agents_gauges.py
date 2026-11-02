"""P8b-i: ``levain.agents.gauges`` — read-only gauges that report a value and their own staleness."""
from __future__ import annotations

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
