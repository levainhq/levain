"""V13 T12: doctor's ``agent firewall probe`` row, a GAUGE over each egress slot's last recorded probe (O4, N10).

The record is the one ``levain.slot.probe`` writes (``L/slots/<tag>/probe``, ``levain-probe-result/1``), made here
with the same canonical bytes. Doctor never probes live.
"""
from __future__ import annotations

import time
from pathlib import Path

from levain import doctor
from levain.contracts import canon
from levain.slot.probe import RECORD_FORMAT, RECORD_NAME
from tests.test_doctor_slot_isolation import NOW, _store

ROW = "agent firewall probe"
SESS = "s-a1b2c3d4e5f60718"


def _record(store: Path, tag: str, outcome: str, cause: str, age_s: float, **extra: object) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age_s))
    obj = {"format": RECORD_FORMAT, "at": stamp, "session": SESS, "outcome": outcome, "cause": cause, **extra}
    (store / "slots" / tag / RECORD_NAME).write_bytes(canon.canonical_bytes(obj))


def _row(store: Path):
    rows = {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: 100 << 30,
                                                            now=NOW)}
    return rows.get(ROW)


def test_absent_record_is_not_probed_yet_never_healthy(tmp_path: Path):
    row = _row(_store(tmp_path, {"e0": "reusable", "e1": "leased", "v1": "reusable"}, pool=(2, 1)))
    assert row is not None and row.neutral and not row.warn
    assert "e0: not probed yet" in row.detail and "e1: not probed yet" in row.detail
    assert "v1" not in row.detail                            # the none class has no probe


def test_closed_fresh_is_ok_with_its_age_and_session(tmp_path: Path):
    store = _store(tmp_path, {"e0": "leased"}, pool=(1, 0))
    _record(store, "e0", "closed", "closed", 300)
    row = _row(store)
    assert row.ok and not row.warn and not row.neutral
    assert row.detail == f"e0: closed 5 min ago (session {SESS})"


def test_closed_older_than_a_day_warns_stale(tmp_path: Path):
    store = _store(tmp_path, {"e0": "reusable"}, pool=(1, 0))
    _record(store, "e0", "closed", "closed", 25 * 3600)
    row = _row(store)
    assert row.ok and row.warn and "stale" in row.detail and "25 h ago" in row.detail


def test_open_is_bad(tmp_path: Path):
    store = _store(tmp_path, {"e0": "reusable", "e1": "reusable"}, pool=(2, 0))
    _record(store, "e0", "closed", "closed", 60)
    _record(store, "e1", "open", "firewall-open", 90)
    row = _row(store)
    assert not row.ok and "e1: OPEN" in row.detail and SESS in row.detail and row.hint


def test_unknown_and_inconclusive_warn_with_their_cause(tmp_path: Path):
    store = _store(tmp_path, {"e0": "reusable", "e1": "reusable"}, pool=(2, 0))
    _record(store, "e0", "unknown", "firewall-probe-unknown:child", 60)
    _record(store, "e1", "inconclusive", "firewall-probe-inconclusive:dns", 60)
    row = _row(store)
    assert row.ok and row.warn
    assert "firewall-probe-unknown:child" in row.detail and "firewall-probe-inconclusive:dns" in row.detail


def test_a_malformed_or_future_record_warns_and_never_reads_closed(tmp_path: Path):
    store = _store(tmp_path, {"e0": "reusable", "e1": "reusable", "e2": "reusable"}, pool=(3, 0))
    (store / "slots" / "e0" / RECORD_NAME).write_bytes(b"not json")
    _record(store, "e1", "closed", "closed", 60, extra="field")
    _record(store, "e2", "closed", "closed", -3600)
    row = _row(store)
    assert row.ok and row.warn
    assert "e0: record unreadable" in row.detail and "e1: record unreadable" in row.detail
    assert "e2: stamped in the future" in row.detail


def test_a_retired_slot_and_no_egress_slot_give_no_row_entry(tmp_path: Path):
    store = _store(tmp_path, {"e0": "retired", "v1": "reusable"}, pool=(0, 1))
    assert _row(store) is None


def test_doctor_reads_the_record_root_writes():
    assert (doctor._PROBE_RECORD_NAME, doctor._PROBE_RECORD_FORMAT) == (RECORD_NAME, RECORD_FORMAT)
