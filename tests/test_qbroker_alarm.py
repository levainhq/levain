"""Row 166: the broker's ALARM verb, the one record a slot hook can leave on the operator's side.

A slot hook reaches only the broker. ``ALARM {cap, kind, reason}`` appends one line to ``L/queue/requests/alarms.jsonl``
(broker-written, operator-read, shown by doctor's ``slot alarms`` row), bounded per slot by a token bucket and per
(session, kind, reason) by a dedupe window, answered with a bare ack: it can never grant, file or answer anything.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
from datetime import timedelta

import pytest

from levain import doctor
from levain.consent import broker_client
from levain.consent.broker_client import BrokerClient, BrokerError
from levain.qbroker import broker as qbroker
from levain.qbroker import operator
from levain.contracts.canon import canonical_bytes
from levain.qbroker.store import ALARM_LOG, ALARM_LOG_MAX, QueueStore
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


def test_the_alarm_log_stays_bounded_on_disk(L):
    store = QueueStore(L / "queue")
    reason = "\U0001F600" * qbroker.ALARM_REASON_MAX
    line = len(canonical_bytes(_record(reason, 0))) + 1
    for i in range(2 * ALARM_LOG_MAX // line + 50):
        store.append_alarm(_record(reason, i))
    req = L / "queue" / "requests"
    assert (req / ALARM_LOG).stat().st_size <= ALARM_LOG_MAX
    assert (req / (ALARM_LOG + ".1")).stat().st_size <= ALARM_LOG_MAX
    assert not (req / (ALARM_LOG + ".2")).exists()
    kept = records(L)
    assert kept and kept[-1]["n"] == i


def _record(reason, n, session="s-" + "f" * 62):
    return {"format": qbroker.ALARM_FORMAT, "at": "2026-11-09T00:00:00Z", "slot": {"class": "egress", "index": 32},
            "session": session, "run_id": "r" * 64, "kind": "config-change", "reason": reason, "n": n}


def test_every_slots_budget_fits_the_log_at_the_largest_record():
    from levain.slot.grammar import MAX_SLOTS
    biggest = len(canonical_bytes(_record("\U0001F600" * qbroker.ALARM_REASON_MAX, 10**6))) + 1
    summary = len(canonical_bytes({"format": qbroker.ALARM_FORMAT, "at": "2026-11-09T00:00:00Z",
                                   "slot": {"class": "egress", "index": 32}, "session": "s-" + "f" * 62,
                                   "suppressed": 10**9})) + 1
    most_suppressed = qbroker.ALARM_RATE * qbroker.ALARM_BUDGET_WINDOW + qbroker.ALARM_BURST
    summaries = int(math.log2(most_suppressed)) + 1                # one at each doubling of the count
    per_slot = qbroker.ALARM_SLOT_BUDGET * biggest + summaries * summary
    # the newest ALARM_LOG_MAX - biggest bytes always survive a roll
    assert MAX_SLOTS * per_slot <= ALARM_LOG_MAX - biggest


def test_bad_cap_frames_never_spend_the_real_alarms_tokens(L, resolver, clock, caplog):
    caplog.set_level(logging.WARNING, logger="levain.qbroker")
    b = make_broker(L, resolver, clock, monotonic=lambda: 1000.0)
    assert alarm(b, reason="first")["ok"] is True                 # the slot's real cap has resolved once
    for i in range(50):
        assert alarm(b, cap=f"bad-{i}")["refused"] in ("cap-unknown", "rate-limited")
    assert alarm(b, reason="second")["ok"] is True
    assert len(records(L)) == 2
    assert sum("could not be resolved" in r.getMessage() for r in caplog.records) == 1


def test_a_flood_of_distinct_bad_caps_is_bounded_by_the_slots_bucket_at_control(L, resolver, clock):
    b = make_broker(L, resolver, clock, monotonic=lambda: 1000.0)
    got = [alarm(b, cap=f"bad-{i}") for i in range(50)]
    assert len(resolver.calls) <= qbroker.ALARM_BURST
    assert {g["refused"] for g in got[qbroker.ALARM_BURST:]} == {"rate-limited"}
    assert records(L) == []


def test_a_repeated_alarm_spends_no_token(L, resolver, clock):
    b = make_broker(L, resolver, clock, monotonic=lambda: 1000.0)
    for _ in range(20):
        assert alarm(b)["ok"] is True
    for i in range(qbroker.ALARM_BURST - 1):
        assert alarm(b, reason=f"r{i}")["ok"] is True
    assert len(records(L)) == qbroker.ALARM_BURST


def test_one_slots_flood_never_evicts_another_slots_alarm_nor_its_own_first(L, resolver, clock):
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    assert alarm(b, reason="first")["ok"] is True
    pad = "x" * (qbroker.ALARM_REASON_MAX - 6)
    flood = 600
    for i in range(flood):
        mono["t"] += 1.0 / qbroker.ALARM_RATE
        assert alarm(b, reason=pad + "%06d" % i)["ok"] is True
    assert alarm(b, slot=2, cap="cap-two", reason="second slot")["ok"] is True
    got = records(L)
    reasons = [r.get("reason") for r in got]
    assert "first" in reasons and "second slot" in reasons
    assert len([r for r in got if r.get("session") == "sess-a" and "reason" in r]) == qbroker.ALARM_SLOT_BUDGET
    suppressed = 1 + flood - qbroker.ALARM_SLOT_BUDGET
    shown = 1 << int(math.log2(suppressed))                      # the last doubling the log carries
    assert [r["suppressed"] for r in got if "suppressed" in r][-1] == shown
    [row] = doctor._check_slot_alarms(store=L, now=clock["fn"]())
    assert row.warn and f"at least {shown} more suppressed" in row.detail


def test_doctor_says_when_the_alarm_log_is_not_readable_by_this_user(L, broker, clock):
    alarm(broker)
    log_path = L / "queue" / "requests" / ALARM_LOG
    log_path.chmod(0)
    try:
        [row] = doctor._check_slot_alarms(store=L, now=clock["fn"]())
    finally:
        log_path.chmod(0o640)
    assert row.ok and not row.warn
    assert "not readable by this user" in row.detail and "levain-queue" in row.detail
    assert "Look at" not in (row.hint or "")


def test_doctor_says_there_is_no_alarm_log_when_the_broker_never_made_one(tmp_path):
    (tmp_path / "queue").mkdir()
    [row] = doctor._check_slot_alarms(store=tmp_path, now=NOW)
    assert not row.warn and "no alarm log exists yet" in row.detail and "none in the last" not in row.detail


def test_other_slots_legal_traffic_never_evicts_a_live_dedupe_stamp(L, resolver, clock):
    from tests.test_qbroker import Resolved
    resolver.add(3, "cap-three", Resolved(session="sess-3", kind="interactive", run=None))
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    slots = ((1, "cap-a"), (2, "cap-two"), (3, "cap-three"))
    for slot, cap in slots:
        for i in range(qbroker.ALARM_BURST):
            assert alarm(b, slot=slot, cap=cap, reason=f"s{slot}-b{i}")["ok"] is True
    rounds = int(0.75 * qbroker.ALARM_DEDUPE_WINDOW * qbroker.ALARM_RATE)
    for r in range(rounds):                                          # every slot inside its own bucket
        mono["t"] += 1.0 / qbroker.ALARM_RATE
        for slot, cap in slots:
            assert alarm(b, slot=slot, cap=cap, reason=f"s{slot}-r{r}")["ok"] is True
    assert mono["t"] - 1000.0 < qbroker.ALARM_DEDUPE_WINDOW
    before = len(records(L))
    assert alarm(b, reason="s1-b0")["ok"] is True                    # still in its window: deduped
    assert len(records(L)) == before


def test_past_the_budget_the_broker_log_gets_a_line_per_record_not_per_alarm(L, resolver, clock, caplog):
    caplog.set_level(logging.WARNING, logger="levain.qbroker")
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    for i in range(qbroker.ALARM_SLOT_BUDGET + 40):
        mono["t"] += 1.0 / qbroker.ALARM_RATE
        assert alarm(b, reason=f"r{i}")["ok"] is True
    lines = [r for r in caplog.records if r.getMessage().startswith("qbroker: slot 1")]
    assert len(lines) == len(records(L)) < qbroker.ALARM_SLOT_BUDGET + 40


def test_an_alarm_whose_append_failed_is_written_by_its_retry_and_charges_no_budget(L, resolver, clock, monkeypatch):
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    real, fails = b._store.append_alarm, {"n": 1}

    def flaky(record):
        if fails["n"]:
            fails["n"] -= 1
            raise OSError("disk full")
        real(record)
    monkeypatch.setattr(b._store, "append_alarm", flaky)
    assert alarm(b, reason="first")["refused"] == "broker-error"
    assert records(L) == []
    assert alarm(b, reason="first")["ok"] is True                  # the retry is not deduped away
    assert [r.get("reason") for r in records(L)] == ["first"]
    for i in range(qbroker.ALARM_SLOT_BUDGET - 1):
        mono["t"] += 1.0 / qbroker.ALARM_RATE
        assert alarm(b, reason=f"r{i}")["ok"] is True
    assert len([r for r in records(L) if "reason" in r]) == qbroker.ALARM_SLOT_BUDGET


def test_doctor_says_when_the_queue_is_not_searchable_by_this_user(L, broker, clock):
    alarm(broker)
    queue = L / "queue"
    mode = queue.stat().st_mode & 0o777
    queue.chmod(0o600)
    try:
        [row] = doctor._check_slot_alarms(store=L, now=clock["fn"]())
    finally:
        queue.chmod(mode)
    assert row.ok and not row.warn
    assert "not readable by this user" in row.detail and "levain-queue" in row.detail
    assert "no alarm log exists yet" not in row.detail


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
    assert hook_run.ALARM_TIMEOUT < CONFIG_TIMEOUT / 2


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
