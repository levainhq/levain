"""S1 6b: doctor's ``agent reaper``, ``agent control`` and ``agent held sessions`` rows.

The reaper's pass record is made here with the writer's own function (``Reaper.write_heartbeat``), CONTROL's answer
comes from a real ``Control`` over a real socket for the live tests, and a unit state is a seam.
"""
from __future__ import annotations

import os
import tempfile
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import doctor
from levain.contracts import canon
from levain.slot import control as C
from levain.slot import records as R
from levain.slot.reaper import HEARTBEAT, V11_S
from tests.test_doctor_slot_isolation import NOW, _store
from tests.test_slot_s2a_control import OP_UID, S1, Env, attach, cas

SESSION = "s-a1b2c3d4e5f60718"
OTHER = "s-0011223344556677"


def _beat(run: Path, *, age: float = 20, control: str = "up", held=(), skipped=(), seq: int = 7, raw: bytes | None = None):
    d = run / "reaper"
    d.mkdir(parents=True, exist_ok=True)
    obj = {"boot_id": "b", "pass_seq": seq, "started_at": R.timestamp(NOW - age - 1),
           "ended_at": R.timestamp(NOW - age), "control": control, "held": list(held), "skipped": list(skipped)}
    (d / HEARTBEAT).write_bytes(canon.canonical_bytes(obj) if raw is None else raw)


def _up(**by_name):
    return lambda host, name: by_name.get(name, True)


def _rows(tmp: Path, *, running=None, status=None, store=None):
    store = store or _store(tmp, {"e0": "reusable"}, pool=(1, 0))
    run = tmp / "run"
    run.mkdir(exist_ok=True)
    return {r.name: r for r in doctor._check_slot_services(
        host="linux", store=store, run=run, running=running or _up(), now=NOW,
        status=status or (lambda: (True, [])))}


def test_no_rows_before_the_isolation_install(tmp_path: Path):
    assert doctor._check_slot_services(host="linux", store=tmp_path / "none", run=tmp_path / "run") == []


def test_the_pass_record_name_and_tick_are_the_writers(tmp_path: Path):
    # doctor reads the file the reaper writes: write it with the writer, read it with the reader
    from levain.slot import reaper as RP

    run = tmp_path / "run"
    (run / "reaper").mkdir(parents=True)
    seams = SimpleNamespace(R=run, fsync=lambda fd: None)
    rep = RP._new_report("boot", 3, NOW - 5)
    rep["ended_at"] = R.timestamp(NOW - 4)
    RP.Reaper.write_heartbeat(SimpleNamespace(seams=seams), rep)
    kind, rec = doctor._read_beat(run)
    assert kind == "ok" and rec["pass_seq"] == 3 and rec["held"] == []
    assert V11_S > 0


def test_the_claims_the_rows_rest_on_still_hold():
    # the reaper's directory is closed to an operator outside levain-wake, and the operator's STATUS needs no argument
    from levain.slot import layout

    row = next(r for r in layout.base_run_rows("linux") if r.path.name == "reaper")
    assert (row.group, row.mode) == ("levain-wake", 0o750)
    assert "STATUS" in C.PEER_VERBS["operator"] and C._ARGS["STATUS"][1] == frozenset()


def test_fresh_pass_is_green_with_its_age_and_the_standing_seam_skips_are_not_faults(tmp_path: Path):
    _beat(tmp_path / "run", age=20, skipped=["step3b:seam-unbuilt", "step5:seam-unbuilt", "end:" + SESSION + ":end-busy"])
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and not row.warn and row.detail == "running; pass 7 ended 20 s ago"


def test_a_failed_step_in_the_pass_warns_and_names_it(tmp_path: Path):
    _beat(tmp_path / "run", skipped=["step3c:OSError", "step6:ValueError", "step7:KeyError", "step4:TypeError"])
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and row.warn and "step3c:OSError" in row.detail and "and 1 more" in row.detail


def test_a_pass_older_than_stale_is_a_warning_while_the_unit_runs(tmp_path: Path):
    _beat(tmp_path / "run", age=3600)
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and row.warn and "no pass in 60 min" in row.detail and "one long END" in row.detail


def test_a_small_future_stamp_reads_as_zero_not_a_negative_age(tmp_path: Path):
    _beat(tmp_path / "run", age=-120)
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and "ended 0 s ago" in row.detail and "-" not in row.detail.split("ended")[1]


@pytest.mark.parametrize("name,failed", [("step", False), ("step3", False), ("step3c:OSError", True),
                                         ("step12:KeyError", True), ("stepx:Err", False)])
def test_the_failed_step_filter_wants_stepN_colon(name: str, failed: bool):
    assert bool(doctor._failed_steps({"skipped": [name]})) is failed


def test_started_at_and_boot_id_are_validated_when_present(tmp_path: Path):
    for field, val in (("started_at", 123), ("boot_id", 5), ("started_at", "yesterday")):
        run = tmp_path / "run"
        obj = {"boot_id": "b", "pass_seq": 1, "started_at": R.timestamp(NOW - 5), "ended_at": R.timestamp(NOW - 4),
               "control": "up", "held": [], "skipped": []}
        obj[field] = val
        _beat(run, raw=canon.canonical_bytes(obj))
        assert doctor._read_beat(run)[0] == "bad", (field, val)
    del obj["started_at"], obj["boot_id"]
    _beat(run, raw=canon.canonical_bytes(obj))
    assert doctor._read_beat(run)[0] == "ok"


def test_the_clock_the_deadline_rides_is_monotonic():
    assert doctor._clock is time.monotonic


@pytest.mark.parametrize("val", [0, 1])
def test_a_non_bool_unit_state_is_read_as_a_bool(tmp_path: Path, val):
    _beat(tmp_path / "run", age=5)
    rows = _rows(tmp_path, running=lambda host, name: val)
    assert (rows["agent reaper"].ok is True) is bool(val) and (rows["agent control"].ok is True) is bool(val)


def test_a_pass_stamped_in_the_future_is_not_fresh(tmp_path: Path):
    _beat(tmp_path / "run", age=-3600)
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and row.warn and "future" in row.detail


def test_a_stopped_reaper_unit_is_a_failure_whatever_the_record_says(tmp_path: Path):
    _beat(tmp_path / "run", age=5)
    row = _rows(tmp_path, running=_up(reaper=False))["agent reaper"]
    assert not row.ok and "not running" in row.detail


def test_an_unknown_unit_state_is_a_warning_never_green(tmp_path: Path):
    _beat(tmp_path / "run", age=5)
    row = _rows(tmp_path, running=_up(reaper=None))["agent reaper"]
    assert row.ok and row.warn and row.detail.startswith("could not be checked")


def test_no_pass_record_yet_warns(tmp_path: Path):
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and row.warn and "no pass record" in row.detail


@pytest.mark.skipif(os.getuid() == 0, reason="root opens any directory")
def test_a_pass_record_this_account_cannot_open_is_said_so_and_is_neutral(tmp_path: Path):
    run = tmp_path / "run"
    _beat(run, age=5)
    (run / "reaper").chmod(0)
    try:
        row = _rows(tmp_path)["agent reaper"]
    finally:
        (run / "reaper").chmod(0o750)
    assert row.ok and row.neutral and "levain-wake" in row.detail


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"pass_seq": 1}',
                                 canon.canonical_bytes({"boot_id": "b", "pass_seq": 1, "started_at": "x", "ended_at": "2026-01-01T00:00:00Z",
                                                        "control": "sideways", "held": [], "skipped": []}),
                                 canon.canonical_bytes({"boot_id": "b", "pass_seq": 1, "started_at": "x", "ended_at": "yesterday",
                                                        "control": "up", "held": [], "skipped": []}),
                                 canon.canonical_bytes({"boot_id": "b", "pass_seq": 1, "started_at": "x", "ended_at": "2026-01-01T00:00:00Z",
                                                        "control": "up", "held": ["../../etc"], "skipped": []})])
def test_a_malformed_pass_record_is_a_warning_not_a_green(tmp_path: Path, raw: bytes):
    _beat(tmp_path / "run", raw=raw)
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and row.warn and "cannot be read" in row.detail


def test_a_pass_record_that_is_a_link_is_refused(tmp_path: Path):
    run = tmp_path / "run"
    (run / "reaper").mkdir(parents=True)
    os.symlink(tmp_path / "elsewhere", run / "reaper" / HEARTBEAT)
    row = _rows(tmp_path)["agent reaper"]
    assert row.ok and row.warn


# --- control ------------------------------------------------------------------------------------------------------

def test_control_that_answers_is_green(tmp_path: Path):
    row = _rows(tmp_path)["agent control"]
    assert row.ok and not row.warn and row.detail == "answers"


def test_control_that_does_not_answer_fails_with_the_reason(tmp_path: Path):
    row = _rows(tmp_path, status=lambda: (False, "no control socket"))["agent control"]
    assert not row.ok and "does not answer (no control socket)" in row.detail and "running" in row.detail


def test_a_stopped_control_unit_fails(tmp_path: Path):
    row = _rows(tmp_path, running=_up(control=False), status=lambda: (False, "no control socket"))["agent control"]
    assert not row.ok and row.detail.startswith("not running")


def test_the_reaper_reporting_control_down_while_it_answers_us_is_a_reaper_warning(tmp_path: Path):
    _beat(tmp_path / "run", control="down")
    rows = _rows(tmp_path)
    assert rows["agent control"].ok and not rows["agent control"].warn
    assert rows["agent reaper"].ok and rows["agent reaper"].warn and "answers this account now" in rows["agent reaper"].detail


def test_a_refusal_frame_means_control_answered(tmp_path: Path):
    rows = _rows(tmp_path, status=lambda: (True, "answer-too-large"))
    assert rows["agent control"].ok and not rows["agent control"].warn
    assert rows["agent held sessions"].warn and "could not list: CONTROL refused (answer-too-large)" in rows["agent held sessions"].detail


def test_run_as_root_control_is_not_asked_and_the_rows_are_neutral(tmp_path: Path):
    store = _store(tmp_path, {"e0": "reusable"}, pool=(1, 0))
    run = tmp_path / "run"
    run.mkdir()
    rows = {r.name: r for r in doctor._check_slot_services(host="linux", store=store, run=run, running=_up(), now=NOW,
                                                           uid=0)}
    assert rows["agent control"].ok and rows["agent control"].neutral and "as the operator" in rows["agent control"].hint
    assert rows["agent held sessions"].ok and rows["agent held sessions"].neutral


def test_a_status_seam_that_raises_costs_one_answer_not_the_rows(tmp_path: Path):
    def boom():
        raise RuntimeError("x")

    rows = _rows(tmp_path, status=boom)
    assert set(rows) == {"agent reaper", "agent control", "agent held sessions"}
    assert not rows["agent control"].ok and "RuntimeError" in rows["agent control"].detail


# --- held ---------------------------------------------------------------------------------------------------------

def test_no_held_session_is_none(tmp_path: Path):
    row = _rows(tmp_path)["agent held sessions"]
    assert row.ok and not row.warn and row.detail == "none"


def test_a_held_session_from_control_is_a_failure_with_its_step_and_tries(tmp_path: Path):
    recs = [{"session": SESSION, "state": "HELD", "failed_step": "freeze", "held_attempts": 3},
            {"session": OTHER, "state": "ACTIVE"}]
    row = _rows(tmp_path, status=lambda: (True, recs))["agent held sessions"]
    assert not row.ok and row.detail.startswith(f"1 stuck mid-end: {SESSION} at freeze (3 tries)")
    assert OTHER not in row.detail


def test_the_reapers_held_list_adds_sessions_control_does_not_show_this_account(tmp_path: Path):
    _beat(tmp_path / "run", held=[OTHER])
    row = _rows(tmp_path)["agent held sessions"]
    assert not row.ok and OTHER in row.detail


def test_a_stale_beat_does_not_fail_the_held_row(tmp_path: Path):
    _beat(tmp_path / "run", age=3600, held=[OTHER])
    row = _rows(tmp_path)["agent held sessions"]
    assert row.ok and row.detail == "none"


def test_a_future_beat_does_not_fail_the_held_row(tmp_path: Path):
    _beat(tmp_path / "run", age=-3600, held=[OTHER])
    row = _rows(tmp_path)["agent held sessions"]
    assert row.ok and row.detail == "none"


@pytest.mark.parametrize("state", ["ACTIVE", "ENDED", "REVOKED"])
def test_control_showing_a_beat_held_session_not_held_drops_it(tmp_path: Path, state: str):
    _beat(tmp_path / "run", held=[OTHER])
    recs = [{"session": OTHER, "state": state}]
    row = _rows(tmp_path, status=lambda: (True, recs))["agent held sessions"]
    assert row.ok and row.detail == "none"


def test_a_control_session_name_off_the_grammar_is_dropped(tmp_path: Path):
    recs = [{"session": "s-\x1b]0;PWNED\x07", "state": "HELD"}, {"session": "s-../../etc", "state": "HELD"}]
    row = _rows(tmp_path, status=lambda: (True, recs))["agent held sessions"]
    assert row.ok and row.detail == "none"


def test_a_closed_beat_and_no_held_says_it_sees_only_this_accounts_sessions(tmp_path: Path):
    run = tmp_path / "run"
    _beat(run, age=5)
    (run / "reaper").chmod(0)
    try:
        row = _rows(tmp_path)["agent held sessions"]
    finally:
        (run / "reaper").chmod(0o750)
    if os.getuid() != 0:
        assert row.ok and row.detail == "none of your sessions (CONTROL shows only yours)"


def test_many_held_sessions_are_counted_and_capped(tmp_path: Path):
    recs = [{"session": f"s-{i:016x}", "state": "HELD", "failed_step": "kill"} for i in range(8)]
    row = _rows(tmp_path, status=lambda: (True, recs))["agent held sessions"]
    assert not row.ok and row.detail.startswith("8 stuck mid-end") and "and 3 more" in row.detail


def test_nothing_readable_is_a_warning_never_none(tmp_path: Path):
    row = _rows(tmp_path, status=lambda: (False, "no control socket"))["agent held sessions"]
    assert row.ok and row.warn and row.detail.startswith("unknown:")


def test_control_down_and_an_empty_pass_record_is_unknown_never_none(tmp_path: Path):
    # the reaper skips end_and_scrub while CONTROL is down, so its empty `held` proves nothing
    _beat(tmp_path / "run", control="down", held=[])
    row = _rows(tmp_path, status=lambda: (False, "no control socket"))["agent held sessions"]
    assert row.ok and row.warn and row.detail.startswith("unknown: CONTROL did not answer")


def test_control_down_names_what_a_fresh_pass_saw_held(tmp_path: Path):
    _beat(tmp_path / "run", control="down", held=[OTHER])
    row = _rows(tmp_path, status=lambda: (False, "no control socket"))["agent held sessions"]
    assert not row.ok and OTHER in row.detail


@pytest.mark.parametrize("kind,word", [("absent", "not written yet"), ("bad", "unreadable")])
def test_the_unknown_wording_follows_the_pass_record_state(tmp_path: Path, kind: str, word: str):
    run = tmp_path / "run"
    if kind == "bad":
        _beat(run, raw=b"not json")
    row = _rows(tmp_path, status=lambda: (False, "no control socket"))["agent held sessions"]
    assert row.warn and word in row.detail and "closed to this account" not in row.detail


def test_a_row_builder_that_raises_is_its_own_failure_row(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(doctor, "_check_held", lambda *a: 1 / 0)
    rows = _rows(tmp_path)
    assert not rows["agent held sessions"].ok and "could not be built" in rows["agent held sessions"].detail
    assert rows["agent reaper"].name == "agent reaper" and "agent control" in rows


# --- the real CONTROL --------------------------------------------------------------------------------------------

class _Live(Env):
    """The control tests' own harness (a real ``Control`` and ``ControlServer``), with doctor's unnamed client taken
    for the operator, served at ``<run>/control/control.sock``."""

    def peer(self, sock):
        name = Path(sock.getpeername() or "x.0").name
        return OP_UID if name == "x.0" else super().peer(sock)

    def link_run(self, run: Path) -> Path:
        run.mkdir(parents=True, exist_ok=True)
        os.symlink(self.dir, run / "control")
        return run


@pytest.fixture
def live(tmp_path: Path):
    e = _Live()
    short = Path(tempfile.mkdtemp(prefix="lvr", dir="/tmp"))      # short: the AF_UNIX path limit
    e.run = e.link_run(short / "run")
    yield e
    e.close()
    shutil.rmtree(short, ignore_errors=True)


def test_a_real_control_answers_status_to_doctor_with_no_session(live: _Live):
    ok, recs = doctor._ask_control_status(live.run, time.monotonic() + 3)
    assert ok and recs == []


def test_a_real_held_session_reaches_the_row_with_its_step_and_tries(live: _Live):
    _op, root = attach(live)
    root.ask("REVOKE", session=S1)
    assert cas(root, S1, 1, "REVOKED", "HELD", failed_step="freeze", held_attempts=2)["ok"]
    answered = doctor._ask_control_status(live.run, time.monotonic() + 3)
    assert answered[0] and [r["state"] for r in answered[1]] == ["HELD"]
    row = doctor._check_held(answered, ("absent", None), NOW)
    assert not row.ok and row.detail.startswith(f"1 stuck mid-end: {S1} at freeze (2 tries)")
    rows = {r.name: r for r in doctor._check_slot_services(
        host="linux", store=_store(live.run.parent, {"e0": "reusable"}, pool=(1, 0)), run=live.run, now=NOW,
        running=_up(), uid=OP_UID)}
    assert rows["agent control"].ok and not rows["agent held sessions"].ok


def test_no_socket_and_a_dead_socket_are_different_words():
    short = Path(tempfile.mkdtemp(prefix="lvn", dir="/tmp"))      # short: the AF_UNIX path limit
    try:
        run = short / "run"
        ok, why = doctor._ask_control_status(run, time.monotonic() + 3)
        assert (ok, why) == (False, "no control socket")
        (run / "control").mkdir(parents=True)
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(run / "control" / "control.sock"))        # bound, never listening
        s.close()
        ok, why = doctor._ask_control_status(run, time.monotonic() + 3)
        assert not ok and why != "no control socket"
    finally:
        shutil.rmtree(short, ignore_errors=True)


def _serve_once(reply: bytes):
    """A socket at ``<d>/control/control.sock`` that reads the request then writes ``reply`` raw. -> (dir, close)."""
    import socket
    import threading

    d = Path(tempfile.mkdtemp(prefix="lvf", dir="/tmp"))
    (d / "control").mkdir()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(d / "control" / "control.sock"))
    srv.listen(1)

    def serve():
        try:
            conn = srv.accept()[0]
            conn.recv(1000)
            conn.sendall(reply)
            conn.close()
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()

    def close():
        srv.close()
        shutil.rmtree(d, ignore_errors=True)

    return d, close


def _frame(payload: bytes) -> bytes:
    return C._LEN.pack(len(payload)) + payload


@pytest.mark.parametrize("payload", [b"[]", b'"hi"', b"42", b"true", b"null"])
def test_a_non_dict_status_frame_is_a_did_not_answer_never_a_crash(payload: bytes):
    d, close = _serve_once(_frame(payload))
    try:
        ok, why = doctor._ask_control_status(d, time.monotonic() + 3)
        assert ok is False and type(why) is str
    finally:
        close()


def test_a_refusal_frame_is_read_from_the_refused_field():
    d, close = _serve_once(C.encode_frame({"id": 1, "ok": False, "refused": "answer-too-large"}))
    try:
        assert doctor._ask_control_status(d, time.monotonic() + 3) == (True, "answer-too-large")
    finally:
        close()


def test_a_refusal_without_a_name_is_unnamed_not_none():
    d, close = _serve_once(C.encode_frame({"id": 1, "ok": False}))
    try:
        assert doctor._ask_control_status(d, time.monotonic() + 3) == (True, "an unnamed refusal")
    finally:
        close()


def test_a_silent_peer_is_bounded_by_the_deadline(tmp_path: Path):
    import socket
    import threading

    d = Path(tempfile.mkdtemp(prefix="lvs", dir="/tmp"))
    try:
        (d / "control").mkdir()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(d / "control" / "control.sock"))
        srv.listen(1)
        held = []

        def serve():
            conn = srv.accept()[0]
            held.append(conn)          # kept open, never answering
            conn.recv(100)
            time.sleep(2)

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        t0 = time.monotonic()
        ok, why = doctor._ask_control_status(d, t0 + 0.5)
        assert not ok and why == "no answer in time" and time.monotonic() - t0 < 3
        srv.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_stuck_row_prints_the_commands_that_release_it_from_a_root_owned_copy():
    """np-5da552a2 (b): no verb; the hint is the runnable sequence (stop, stage a root copy, one pass from the copy, start,
    clean up, repair), in that order, and no step runs code from the operator-writable package."""
    for host in ("darwin", "linux"):
        lines = doctor.reap_held_lines(host, "/home/op/site/levain")
        stop, stage, run, start, clean, repair = lines
        assert "reaper" in stop and "reaper" in start and stop != start
        assert "cp -R /home/op/site/levain " in stage
        assert "sys.path.insert(0, '" in run and "/home/op/site" not in run and "run_pass()" in run
        assert repair.endswith("repair --source /home/op/site/levain")


def test_a_package_path_with_a_space_survives_shlex_in_the_printed_commands():
    """Diogenes 10-29 (2b7fe9d6): the operator's package path reaches sudo commands; a space must not split it."""
    import shlex
    src = "/home/op/my site/levain"
    for host in ("darwin", "linux"):
        lines = doctor.reap_held_lines(host, src)
        assert shlex.split(lines[1])[-2] == src and shlex.split(lines[5])[-1] == src


def test_a_fresh_pass_record_naming_no_held_session_is_not_called_out_of_date():
    """Diogenes 10-26 (f6738797): CONTROL silent + a FRESH record with held [] is not "out of date"."""
    rec = {"pass_seq": 7, "ended_at": R.timestamp(NOW - 30), "control": "down", "held": [], "skipped": []}
    fresh = doctor._check_held((False, "no answer in time"), ("ok", rec), NOW)
    stale = doctor._check_held((False, "no answer in time"), ("ok", dict(rec, ended_at=R.timestamp(NOW - 5000))), NOW)
    assert fresh.ok and fresh.warn and "out of date" not in fresh.detail and "is current" in fresh.detail
    assert "out of date" in stale.detail
