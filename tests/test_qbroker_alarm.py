"""Row 166: the broker's ALARM verb, the one record a slot hook can leave on the operator's side.

A slot hook reaches only the broker. ``ALARM {cap, kind, reason}`` appends one line to ``L/queue/requests/alarms.jsonl``
(broker-written, operator-read, shown by doctor's ``slot alarms`` row), bounded per slot by a token bucket and per
(session, kind, reason) by a dedupe window, answered with a bare ack: it can never grant, file or answer anything.
"""
from __future__ import annotations

import io
import json
import os
from datetime import timedelta

import pytest

from levain import doctor
from levain.consent import broker_client
from levain.consent.broker_client import BrokerClient, BrokerError
from levain.qbroker import broker as qbroker
from levain.qbroker import operator
from levain.qbroker.store import ALARM_LOG, ROW_MAX, QueueStore
from levain.slot import hook_run
import tests.test_qbroker as base
from tests.test_qbroker import NOW, connect, listen, make_broker
from tests.test_consent_broker_client import Scripted

L, broker, clock, keys, resolver, run_dir = base.L, base.broker, base.clock, base.keys, base.resolver, base.run_dir

_ids = iter(range(1, 10**6))


def alarm(b, slot=1, cap="cap-a", kind="config-change", reason="source=local_settings file_path=/r/x", **over):
    msg = {"v": 1, "op": "ALARM", "id": next(_ids), "cap": cap, "kind": kind, "reason": reason, **over}
    return b.handle(slot, {k: v for k, v in msg.items() if v is not None})


def records(L):
    return operator.alarms(L / "queue")


def gate(b, slot=1, cap="cap-a"):
    return b.handle(slot, {"v": 1, "op": "GATE", "id": next(_ids), "cap": cap, "agent": "claude_code",
                           "tool_name": "Read", "input_digest": "sha256:" + "0" * 64})


def test_one_alarm_is_one_operator_record_answered_with_a_bare_ack(L, broker):
    got = alarm(broker)
    assert got == {"id": got["id"], "ok": True}
    [rec] = records(L)
    assert rec["format"] == qbroker.ALARM_FORMAT
    assert rec["session"] == "sess-a" and rec["kind"] == "config-change"
    assert rec["reason"] == "source=local_settings file_path=/r/x"
    assert rec["slot"] == {"class": "egress", "index": 1} and "run_id" not in rec


def test_a_dispatch_sessions_alarm_names_its_run(L, broker):
    assert alarm(broker, slot=2, cap="cap-two")["ok"] is True
    [rec] = records(L)
    assert rec["session"] == "sess-2" and rec["run_id"] == "run-1"


def test_the_same_alarm_repeated_is_one_record(L, broker):
    for _ in range(4):
        assert alarm(broker)["ok"] is True
    assert len(records(L)) == 1


def test_the_same_alarm_after_the_dedupe_window_is_a_second_record(L, resolver, clock):
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    alarm(b)
    mono["t"] += qbroker.ALARM_DEDUPE_WINDOW + 1
    alarm(b)
    assert len(records(L)) == 2


def test_a_flood_of_distinct_alarms_is_capped_by_the_slots_bucket(L, resolver, clock):
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    got = [alarm(b, reason=f"r{i}") for i in range(50)]
    assert sum(g["ok"] for g in got) == qbroker.ALARM_BURST
    assert {g["refused"] for g in got if not g["ok"]} == {"rate-limited"}
    assert len(records(L)) == qbroker.ALARM_BURST
    mono["t"] += 1.0 / qbroker.ALARM_RATE
    assert alarm(b, reason="later")["ok"] is True
    assert len(records(L)) == qbroker.ALARM_BURST + 1


def test_the_alarm_log_stays_bounded_on_disk(L, resolver, clock):
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    reason = "x" * qbroker.ALARM_REASON_MAX
    for i in range(600):
        mono["t"] += 1.0 / qbroker.ALARM_RATE
        assert alarm(b, reason=reason[:-6] + "%06d" % i)["ok"] is True
    req = L / "queue" / "requests"
    assert (req / ALARM_LOG).stat().st_size <= ROW_MAX
    assert (req / (ALARM_LOG + ".1")).stat().st_size <= ROW_MAX
    assert not (req / (ALARM_LOG + ".2")).exists()
    kept = records(L)
    assert kept and kept[-1]["reason"].endswith("%06d" % 599)


@pytest.mark.parametrize("over", [
    {"grant": {}}, {"decision": "allow"}, {"request": {}}, {"session": "sess-b"}, {"payload": "x"},
    {"kind": "anything"}, {"kind": None}, {"reason": None}, {"reason": ""}, {"reason": 7},
    {"reason": "x" * (qbroker.ALARM_REASON_MAX + 1)}, {"reason": "two\nlines"}, {"reason": "esc\x1b[2J"},
])
def test_an_alarm_out_of_shape_is_refused_and_writes_nothing(L, broker, over):
    got = alarm(broker, **over)
    assert got["ok"] is False and got["refused"] == "frame-malformed"
    assert records(L) == []


def test_an_alarm_with_an_unknown_cap_is_refused_and_writes_nothing(L, broker):
    assert alarm(broker, cap="nope")["refused"] == "cap-unknown"
    assert records(L) == []


def test_an_alarm_grants_nothing(L, broker):
    req, ans = L / "queue" / "requests", L / "queue" / "answers"
    before_gate = gate(broker)
    before = set(os.listdir(req))
    for i in range(3):
        got = alarm(broker, reason=f"r{i}")
        assert set(got) == {"id", "ok"} and got["ok"] is True
    assert set(os.listdir(req)) - before == {ALARM_LOG}
    assert QueueStore(L / "queue").rows() == [] and list(ans.iterdir()) == []
    after_gate = gate(broker)
    assert {k: v for k, v in after_gate.items() if k != "id"} == {k: v for k, v in before_gate.items() if k != "id"}


def test_the_client_takes_only_a_bare_ack():
    Scripted([{}]).alarm("config-change", "r")
    for answer in ({"go": True}, {"decision": "allow"}, "rate-limited"):
        with pytest.raises(BrokerError):
            Scripted([answer]).alarm("config-change", "r")


def test_the_client_cuts_a_long_reason_to_the_brokers_bound():
    sent = {}

    class Rec(BrokerClient):
        def call(self, op, **fields):
            sent.update(op=op, **fields)
            return {"id": 1, "ok": True}
    Rec("/x", "cap").alarm("config-change", "y" * 5000)
    assert sent["op"] == "ALARM" and len(sent["reason"]) == qbroker.ALARM_REASON_MAX


# --- the ConfigChange guard, end to end through a live listener --------------------------------------------------

PAYLOAD = json.dumps({"hook_event_name": "ConfigChange", "source": "local_settings",
                      "file_path": "/s/repo/.claude/settings.local.json", "session_id": "abc", "cwd": "/s/repo"})


def test_a_slot_config_change_leaves_exactly_one_operator_record(L, broker, run_dir, monkeypatch):
    monkeypatch.setattr(broker_client, "broker_uid", os.getuid)
    lst = listen(broker, run_dir, os.getuid())
    lst.start()
    try:
        env = {broker_client.SOCKET_ENV: str(lst.path), broker_client.CAP_ENV: "cap-a"}
        for _ in range(3):                                    # the same change retried: one record
            err = io.StringIO()
            assert hook_run.config_guard("levain_config", stdin=io.StringIO(PAYLOAD), stderr=err,
                                         environ=env) == hook_run.REFUSED_EXIT
            assert err.getvalue().count("\n") == 1 and "blocked a settings change" in err.getvalue()
    finally:
        lst.stop()
    [rec] = records(L)
    assert rec["kind"] == "config-change" and rec["session"] == "sess-a"
    assert "local_settings" in rec["reason"] and "/s/repo/.claude/settings.local.json" in rec["reason"]


def test_a_config_change_whose_alarm_cannot_be_sent_is_still_blocked_and_says_so(tmp_path):
    env = {broker_client.SOCKET_ENV: str(tmp_path / "no.sock"), broker_client.CAP_ENV: "cap-a"}
    err = io.StringIO()
    assert hook_run.config_guard("levain_config", stdin=io.StringIO(PAYLOAD), stderr=err, environ=env) \
        == hook_run.REFUSED_EXIT
    lines = err.getvalue().splitlines()
    assert len(lines) == 2 and "blocked a settings change" in lines[0] and "alarm not recorded" in lines[1]


def test_the_alarms_wait_ends_well_inside_the_hooks_own_timeout():
    from levain.slot.hook_templates import CONFIG_TIMEOUT
    assert broker_client.IO_TIMEOUT <= hook_run.ALARM_TIMEOUT < CONFIG_TIMEOUT / 2


def test_outside_a_slot_the_guard_sends_no_alarm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no broker outside a slot")
    monkeypatch.setattr(broker_client.BrokerClient, "from_env", classmethod(boom))
    err = io.StringIO()
    assert hook_run.config_guard("levain_config", stdin=io.StringIO(PAYLOAD), stderr=err, environ={}) \
        == hook_run.REFUSED_EXIT
    assert err.getvalue().count("\n") == 1


# --- doctor's slot alarms row ---------------------------------------------------------------------------------------

def test_doctor_shows_a_recent_alarm_as_a_warning(L, broker, clock):
    alarm(broker)
    [row] = doctor._check_slot_alarms(store=L, now=clock["fn"]())
    assert row.name == "slot alarms" and row.warn and "sess-a" in row.detail and "config-change" in row.detail


def test_doctor_is_quiet_with_no_recent_alarm(L, broker, clock):
    [row] = doctor._check_slot_alarms(store=L, now=NOW)
    assert not row.warn and "none" in row.detail
    alarm(broker)
    [row] = doctor._check_slot_alarms(store=L, now=NOW + doctor.SLOT_ALARM_WINDOW + timedelta(seconds=1))
    assert not row.warn


def test_doctor_has_no_row_before_the_isolation_install(tmp_path):
    assert doctor._check_slot_alarms(store=tmp_path, now=NOW) == []
