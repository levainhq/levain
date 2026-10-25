"""CH-D2 S9: K1's report (`levain.chat_egress.report`): R15-30-3 (the crash marks), R15-29-2 (the final flush's total),
R15-28-5 / R15-29-3 (`duplicate_rows`, K1 only), R15-10-A17 (the `path` wall count), R15-17-10 (credential_in_query),
R15-19-3 (cap cards), R15-25-3 (a flood's summarised walls count).

The first half feeds the report synthetic glue lines (the formats `wire.glue_event_line` encodes). The second half runs
the real glue and decider and reads the report from the files the launch left.
"""

from __future__ import annotations

import os
import signal
import socket
import time

import pytest

from levain.chat_egress import classify, report, wire
from levain.chat_egress import records as rec
from levain.chat_egress.lifecycle import Verdict

from .rig import autoanswer


def _wall(host="a.test", reason="port", rid=None, method=None):
    return wire.glue_event_line(wire.GlueWall(rid, method, host, reason))


def _summary(total, *, final, by_reason=None, hosts=None, overflow=0, minute=1_700_000_040, count=None):
    by_reason = by_reason if by_reason is not None else {"port": total}
    hosts = hosts if hosts is not None else {"a.test": total}
    return wire.glue_event_line(wire.GlueSummary(minute, total if count is None else count, total, by_reason, hosts,
                                                 overflow, final))


# --- R15-30-3 and R15-29-2, over synthetic lines ----------------------------------------------------------------

def test_a_clean_end_shows_the_final_flushs_exact_total() -> None:
    lines = [_wall() for _ in range(3)] + [_summary(150, final=True)]
    r = report.build([], lines)
    assert (r.walls.total, r.walls.exact, r.walls.crashed) == (150, True, False)
    assert r.walls.text == "150"


def test_no_wall_clean_launch_reads_zero() -> None:
    r = report.build([], [_summary(0, final=True, by_reason={}, hosts={}, count=0)])
    assert r.walls.total == 0 and r.walls.exact


def test_crash_before_any_summary_is_unknown_and_never_the_detailed_count_as_exact() -> None:
    r = report.build([], [_wall() for _ in range(7)])
    assert r.walls.total is None and not r.walls.exact and r.walls.crashed
    assert r.walls.text == "unknown (launch crashed before its first summary; at least 7)"


def test_crash_with_no_event_line_at_all_is_unknown_at_least_zero() -> None:
    r = report.build([], [])
    assert r.walls.text == "unknown (launch crashed before its first summary; at least 0)"


def test_crash_after_a_summary_shows_that_summarys_total_marked_crashed() -> None:
    lines = [_wall() for _ in range(100)] + [_summary(180, final=False)]
    r = report.build([], lines)
    assert r.walls.total == 180 and not r.walls.exact and r.walls.crashed
    assert "crashed" in r.walls.text and "180" in r.walls.text and "unknown" not in r.walls.text


def test_a_malformed_card_row_still_counts_its_names() -> None:
    rows = [_row(1, "card:credential_in_query", names='["x-amz-signature","sig","<1-4:a:backstop>"]')]
    r = report.build(rows, [_summary(0, final=True, by_reason={}, hosts={}, count=0)])
    assert r.cards == {"malformed_card_step": 1}
    assert r.signed_url_names == {"x-amz-signature": 1} and r.azure_sas_names == 1 and r.backstop_names == 1


def test_walls_after_the_last_summary_make_the_number_a_floor_not_the_summarys_total() -> None:
    r = report.build([], [_summary(2, final=False)] + [_wall() for _ in range(3)])
    assert r.walls.total == 5 and not r.walls.exact and r.walls.crashed
    assert r.walls.text.startswith("at least 5 ")


@pytest.mark.parametrize("walls_after", [0, 3])
def test_a_summary_with_no_total_keeps_the_total_unknown_not_a_crash(walls_after) -> None:
    """`wire` refuses a summary line without an int total, so `build` never gets one; `wall_total` is public and typed
    `GlueSummary`, so it must not raise on a hand-built one either."""
    ev = [wire.GlueSummary(1_700_000_040, 0, None, {}, {}, 0, False)] + [wire.GlueWall(None, None, "a.test", "port")] * walls_after
    got, _ = report.wall_total(ev)
    assert (got.total, got.exact, got.crashed) == (None, False, True)
    assert "None" not in got.text


def test_a_line_the_wire_reader_refuses_is_counted_not_skipped() -> None:
    r = report.build([], [_summary(2, final=True), b'{"t":"wall","host":"x"}\n', b"not json\n"])
    assert r.unreadable_event_lines == 2
    assert "unreadable glue event lines: 2" in r.text()


def test_the_summarys_counters_reach_the_report() -> None:
    s = _summary(10_000, final=True, by_reason={"port": 9000, "scheme": 1000},
                 hosts={f"h{i}.test": 1 for i in range(64)}, overflow=10_000 - 64)
    r = report.build([], [s])
    assert r.by_reason == {"port": 9000, "scheme": 1000} and len(r.hosts) == 64 and r.host_overflow == 9936
    assert r.walls.total == 10_000


# --- the counters read from the rows ----------------------------------------------------------------------------

def _row(i, step, *, verdict=Verdict.DENY, names="[]", host="h.test", method="GET", path="/"):
    return rec.EgressRow(gseq=i, launch_id="ln-x", request_id=f"{i:032x}", kind=rec.ATTEMPT, verdict=verdict,
                         deciding_step=step, host=host, method=method, path=path, names=names, setup_record_id=None,
                         attach=rec.Attach.NONE, open_network=False, status=None, declared_length=None, ended=None,
                         content_type_class=None, failed_effect=None, at="t")


def test_the_report_counts_path_credential_cap_and_card_rows() -> None:
    rows = [
        _row(1, "wall:path"), _row(2, "wall:path"),
        _row(3, "wall:credential_in_query"),
        _row(4, "card:credential_in_query:allowed", verdict=Verdict.ALLOW),
        _row(5, "card:credential_in_query:denied"),
        _row(6, "card:body:expired"),
        _row(7, "wall:card_cap"), _row(8, "wall:body_cap"), _row(11, "card:upload_pack_over_cap:denied"),
        _row(9, "silent", verdict=Verdict.ALLOW,
             names='["x-amz-signature","x-amz-credential","<1-4:a:backstop>"]'),
        _row(10, "audit-missing"),
        _row(12, "silent", verdict=Verdict.ALLOW, names='["sig"]'),
    ]
    r = report.build(rows, [_summary(0, final=True, by_reason={}, hosts={}, count=0)])
    assert r.path_walls == 2
    assert (r.credential_in_query_walls, r.credential_in_query_cards) == (1, 2)
    assert r.cap_walls == 2 and r.cap_cards == 1
    assert r.cards == {"credential_in_query:allowed": 1, "credential_in_query:denied": 1, "body:expired": 1,
                       "upload_pack_over_cap:denied": 1}
    assert r.signed_url_names == {"x-amz-signature": 1, "x-amz-credential": 1}
    assert r.azure_sas_names == 1 and r.signed_url_names.get("sig") is None
    assert r.backstop_names == 1 and r.audit_missing == 1 and r.attempts == 12
    text = r.text()
    assert "path walls: 2" in text and "duplicate_rows: 0" in text


def test_a_report_with_no_path_wall_still_prints_the_count() -> None:
    assert "path walls: 0" in report.build([], []).text()


# --- the real glue and decider ----------------------------------------------------------------------------------

pytestmark_srt = pytest.mark.chat_egress_srt


def _wall_once(L, port: int) -> None:
    """One plain request to a port outside the glue's HTTP set: a glue wall (reason `port`)."""
    c = socket.create_connection(("127.0.0.1", L.port), timeout=10)
    try:
        c.sendall(b"GET http://pub.rb.test:%d/ HTTP/1.1\r\nHost: pub.rb.test:%d\r\n\r\n" % (port, port))
        c.recv(4096)
    finally:
        c.close()


@pytest.mark.chat_egress_srt
def test_150_walls_then_a_clean_end_in_one_minute_read_150(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    off = origin.port + 1 if origin.port < 65000 else origin.port - 1
    for _ in range(150):
        _wall_once(L, off)
    assert L.stop() == 0
    r = report.build_from_files(L.records.rows(L.launch_id), L.events, L.records.duplicate_rows(L.launch_id))
    print("K1 report:\n" + r.text())
    assert r.walls.exact and r.walls.total == 150 and not r.walls.crashed
    assert r.by_reason == {"port": 150}
    kinds = [wire.parse_glue_event(x).__class__.__name__ for x in L.events.read_bytes().splitlines(keepends=True)]
    assert kinds.count("GlueWall") == 100                       # the 100 detailed, then summarised (R15-27-1)


@pytest.mark.chat_egress_srt
def test_a_killed_glue_leaves_no_final_flush_and_the_report_says_unknown(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    off = origin.port + 1 if origin.port < 65000 else origin.port - 1
    for _ in range(5):
        _wall_once(L, off)
    L.proc.send_signal(signal.SIGKILL)
    L.proc.wait(10)
    L.close_life()
    L.d.close(timeout_s=10)
    r = report.build_from_files(L.records.rows(L.launch_id), L.events)
    # the glue flushes a summary at a minute boundary: a kill after one reads as that summary (plus the later walls)
    assert r.walls.crashed and not r.walls.exact
    if r.walls.total is None:
        assert r.walls.text == "unknown (launch crashed before its first summary; at least 5)"
    else:
        assert r.walls.text.startswith(("at least 5 ", f"{r.walls.total} (from a crashed launch")), r.walls.text


@pytest.mark.chat_egress_srt
def test_duplicate_rows_surfaces_in_the_report_and_a_normal_launch_reads_zero(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    stop = autoanswer(L)
    try:
        assert L.request("GET", "/a")[0] == 200
        assert L.stop() == 0
    finally:
        stop.set()
    rows = L.records.rows(L.launch_id)
    assert report.build(rows, [], L.records.duplicate_rows(L.launch_id)).duplicate_rows == 0
    a = next(r for r in rows if r.kind == rec.ATTEMPT)
    same = rec.Attempt(host=a.host, method=a.method, path=a.path, names=[], setup_record_id=None,
                       attach=rec.Attach(a.attach), deciding_step=a.deciding_step, open_network=bool(a.open_network))
    assert L.records.write_attempt(L.launch_id, a.request_id, Verdict(a.verdict), same) is False
    assert L.records.write_attempt(L.launch_id, a.request_id, Verdict(a.verdict), same) is False
    assert L.records.duplicate_rows(L.launch_id) == 2
    assert "duplicate_rows: 2" in report.build(rows, [], L.records.duplicate_rows(L.launch_id)).text()


@pytest.mark.chat_egress_srt
def test_path_and_credential_walls_from_a_real_launch_reach_the_report(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin, mode=classify.OPEN_NETWORK)
    stop = autoanswer(L)
    try:
        st1 = L.request("GET", "/ok")[0]
        st2 = L.request("GET", "/x?token=abc")[0]                        # open network: a wall credential_in_query
        head = L.raw(b"GET http://pub.rb.test:%d/a//b HTTP/1.1\r\nHost: pub.rb.test:%d\r\n\r\n"
                     % (origin.port, origin.port))                       # R-2: a `//` has no canonical form: a path wall
        assert L.stop() == 0
    finally:
        stop.set()
    r = report.build_from_files(L.records.rows(L.launch_id), L.events)
    assert st1 == 200 and st2 == 403
    assert r.credential_in_query_walls == 1
    assert head.startswith(b"HTTP/1.1 403") and r.path_walls == 1, r.text()


@pytest.mark.chat_egress_srt
def test_cap_walls_from_a_real_launch_are_counted(make_origin, make_launch_) -> None:
    """The card cap and the body cap are rows with the step names `report.CAP_STEPS` lists."""
    from levain.chat_egress import decider as dm
    import threading
    origin = make_origin()
    L = make_launch_(origin, limits=dm.Limits(card_poll_s=0.1, max_cards=1, card_body_cap=1024, stop_drain_s=2.0))
    t = threading.Thread(target=lambda: L.request("POST", "/held", body=b"x", timeout=60), daemon=True)
    t.start()
    end = time.monotonic() + 10
    while not L.desk.filed and time.monotonic() < end:
        time.sleep(0.05)
    assert L.request("POST", "/second", body=b"y")[0] == 503                  # card_cap
    L.desk.answer["card-1"] = "denied"
    t.join(30)
    assert L.request("POST", "/big", body=b"z" * 5000)[0] == 503              # body_cap
    assert L.stop() == 0
    r = report.build_from_files(L.records.rows(L.launch_id), L.events)
    assert r.cap_walls == 2 and r.decider_walls.get("card_cap") == 1 and r.decider_walls.get("body_cap") == 1, r.text()
    assert r.cards == {"body:denied": 1}, r.cards


def test_a_decider_busy_row_is_a_cap_wall() -> None:
    """The decider stores `decider_busy` bare (no `wall:` prefix): `_on_req` hands `_inline` the step name itself."""
    r = report.build([_row(1, "decider_busy"), _row(2, "wall:card_cap")], [])
    assert r.cap_walls == 2 and r.decider_walls == {"decider_busy": 1, "card_cap": 1}, r.text()


def test_walls_after_the_last_summary_of_a_crashed_launch_read_as_at_least() -> None:
    lines = [_summary(2, final=False), _wall(), _wall(), _wall()]
    r = report.build([], lines)
    assert r.walls.crashed and not r.walls.exact and r.walls.text.startswith("at least 5 "), r.walls.text


def test_a_malformed_card_step_counts_under_its_own_key() -> None:
    r = report.build([_row(1, "card:"), _row(2, "card:nocolon"), _row(3, "card:body:denied")], [])
    assert r.cards == {"malformed_card_step": 2, "body:denied": 1}, r.cards
