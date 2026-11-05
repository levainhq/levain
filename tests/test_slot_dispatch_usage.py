"""S10d: DispatchUsageSources. None until final, ints otherwise, never a quiet zero for anything unreadable."""
from __future__ import annotations

import json
from collections import OrderedDict
from types import MappingProxyType

import pytest

from levain.agents.session_launcher import UsageSources
from levain.slot.dispatch_usage import DispatchUsageSources


def _src(run_state=None, log_path=None, seconds=None, total=None):
    return DispatchUsageSources(
        run_state=run_state or (lambda rid: {"calls": 4, "known": True}),
        tunnel_log=log_path or (lambda s: "/log/" + s),
        seconds_of=seconds or (lambda s: 61),
        read_total=total if total is not None else (lambda p, s: 2048))


def test_satisfies_the_launcher_protocol_and_reads_ints():
    s = _src()
    assert isinstance(s, UsageSources)
    assert (s.calls("run-1"), s.tunnel_bytes("S1"), s.seconds("S1")) == (4, 2048, 61)


def test_zero_is_a_real_answer_and_passes_through():
    s = _src(run_state=lambda r: {"calls": 0, "known": True}, total=lambda p, s: 0, seconds=lambda s: 0)
    assert (s.calls("r"), s.tunnel_bytes("S"), s.seconds("S")) == (0, 0, 0)


def test_tunnel_bytes_is_none_until_the_front_final_total_exists():
    s = _src(total=lambda p, s: None)
    assert s.tunnel_bytes("S1") is None
    # a path source that fails is also unreadable, never zero
    def boom(sess):
        raise OSError("no log dir")
    assert _src(log_path=boom).tunnel_bytes("S1") is None


def test_calls_unknown_run_or_unreadable_state_is_none_not_zero():
    assert _src(run_state=lambda r: {"calls": 0, "known": False}).calls("r") is None

    def boom(r):
        raise RuntimeError("OperatorError: state not readable")
    assert _src(run_state=boom).calls("r") is None
    for bad in (None, [], {"known": True}, {"calls": True, "known": True}, {"calls": -1, "known": True},
                {"calls": "3", "known": True}, {"calls": 2.0, "known": True}):
        assert _src(run_state=lambda r, b=bad: b).calls("r") is None


@pytest.mark.parametrize("bad", [None, True, -1, 1.5, "7"])
def test_seconds_and_bytes_reject_non_counts(bad):
    s = _src(seconds=lambda x: bad, total=lambda p, s: bad)
    assert s.seconds("S1") is None
    assert s.tunnel_bytes("S1") is None


def test_empty_or_non_string_keys_are_none_and_no_reader_is_called():
    asked = []
    s = _src(run_state=lambda r: asked.append(r), seconds=lambda x: asked.append(x), log_path=lambda x: asked.append(x))
    for bad in ("", None, 5):
        assert s.calls(bad) is None and s.tunnel_bytes(bad) is None and s.seconds(bad) is None
    assert asked == []


def test_default_reader_is_the_helpers_final_tunnel_line(tmp_path):
    log = tmp_path / "tunnel.log"
    old = json.dumps({"event": "tunnel", "session": "S1", "passthrough_total": 99}) + "\n"
    log.write_text(old)          # a front older than the open record (S10b C FIX ROUND 2): not provably final
    s0 = DispatchUsageSources(run_state=lambda r: {}, tunnel_log=lambda sess: log, seconds_of=lambda sess: 1)
    assert s0.tunnel_bytes("S1") is None
    log.write_text(_rec("S1", 99))
    s = DispatchUsageSources(run_state=lambda r: {"calls": 1, "known": True}, tunnel_log=lambda sess: log,
                             seconds_of=lambda sess: 1)
    assert s.tunnel_bytes("S1") == 99
    log.write_text("not json\n")
    assert s.tunnel_bytes("S1") is None
    assert DispatchUsageSources(run_state=lambda r: {}, tunnel_log=lambda sess: tmp_path / "missing",
                                seconds_of=lambda sess: 1).tunnel_bytes("S1") is None


def _rec(session, total, tid=1, event="tunnel"):
    """One passthrough tunnel opened then closed, as the front logs it (S10b C FIX ROUND 2)."""
    return (json.dumps({"event": "tunnel-open", "session": session, "tunnel_id": tid}) + "\n"
            + json.dumps({"event": event, "session": session, "passthrough": True, "tunnel_id": tid,
                          "passthrough_total": total}) + "\n")


def test_shared_slot_log_returns_only_the_asked_sessions_final_total(tmp_path):
    log = tmp_path / "tunnel.log"
    # slot reuse: A, then B, then A's later record, then B again, all in one append-only file
    log.write_text(_rec("A", 100) + _rec("B", 7) + _rec("A", 900000, 2) + "torn{\n" + _rec("B", 1000, 2)
                   + json.dumps({"event": "other", "session": "A", "passthrough_total": 5}) + "\n")
    s = DispatchUsageSources(run_state=lambda r: {}, tunnel_log=lambda sess: log, seconds_of=lambda sess: 1)
    assert s.tunnel_bytes("A") == 900000
    assert s.tunnel_bytes("B") == 1000
    assert s.tunnel_bytes("C") is None
    log.write_text(_rec("B", 1000))  # A's records rotated away: None, never B's total
    assert s.tunnel_bytes("A") is None


def test_run_state_may_be_any_mapping_but_is_still_strict():
    for doc in (OrderedDict(known=True, calls=4), MappingProxyType({"known": True, "calls": 4})):
        assert _src(run_state=lambda r, d=doc: d).calls("r") == 4
    for bad in (OrderedDict(known=1, calls=4), MappingProxyType({"known": True, "calls": True}),
                OrderedDict(known="true", calls=4)):
        assert _src(run_state=lambda r, d=bad: d).calls("r") is None


def test_a_falsey_injected_reader_is_still_used():
    class Falsey:
        def __bool__(self):
            return False

        def __call__(self, p, s):
            return 42
    assert _src(total=Falsey()).tunnel_bytes("S1") == 42
