"""Slot limits, the broker half (1025+6 S-a; design project_memory/1025-6/limits/DESIGN.md, rulings HEAD_RULINGS_R1.md):
the over-limit latch under ``count_crossed=False``, the limits a slot reads from ``L/policy/limits``
(:class:`levain.qbroker.limits.PolicyLimits`), and the GATE answers they give."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from levain.consent import limits as consent_limits
from levain.qbroker import broker as qbroker
import tests.test_qbroker as base
from tests.test_qbroker import NOW, make_broker
from tests.test_qbroker_gate import gate

L, clock, keys, resolver = base.L, base.clock, base.keys, base.resolver

BIG = {"calls": 1000, "active_hours": 100, "repeat": 8}


def digests():
    return iter("sha256:" + f"{i:064x}" for i in range(10**6))


def latches(L):
    db = L / "queue" / "requests" / qbroker.LIMITS_DIR / consent_limits.DB_NAME
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT kind, count FROM latches ORDER BY kind").fetchall()


# --- the latch (codex HIGH, ARCH_L3_r1; HEAD_RULINGS_R1) -----------------------------------------------------------

def test_repeat_crossing_then_different_call_is_still_over_limit(L, resolver, clock):
    b = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"repeat": 2})
    a, other = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    assert gate(b, input_digest=a)["gate"] == "go"
    got = gate(b, input_digest=a)
    assert got["gate"] == "over-limit" and got["kind"] == "repeat"
    for _ in range(3):                  # held: a different call does not free the session
        got = gate(b, input_digest=other)
        assert got["gate"] == "over-limit" and got["kind"] == "repeat"


def test_active_hours_crossing_then_gap_is_still_over_limit(L, resolver, clock):
    # the broker's clock is the counter's (Broker(clock=...) -> count_call(now=...))
    b = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"active_hours": 0.1})
    d = digests()
    assert gate(b, input_digest=next(d))["gate"] == "go"
    clock["now"] = NOW + timedelta(seconds=300)
    assert gate(b, input_digest=next(d))["gate"] == "go"            # 300 s active: under 0.1 h
    clock["now"] = NOW + timedelta(seconds=600)
    got = gate(b, input_digest=next(d))                              # 600 s active: past 0.1 h
    assert got["gate"] == "over-limit" and got["kind"] == "active_hours"
    clock["now"] = NOW + timedelta(seconds=600 + consent_limits.ACTIVE_GAP + 100)   # a gap adding no active time
    got = gate(b, input_digest=next(d))
    assert got["gate"] == "over-limit" and got["kind"] == "active_hours"


def test_a_latched_session_counts_nothing_while_held(L, resolver, clock):
    b = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 2})
    d = digests()
    assert [gate(b, input_digest=next(d))["gate"] for _ in range(6)] == ["go", "go"] + ["over-limit"] * 4
    assert latches(L) == [("calls", 3.0)]                            # the first crossing's count, never a retry's


def test_raising_the_limit_past_the_latch_frees_the_session(L, resolver, clock):
    lim = BIG | {"repeat": 2}
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(lim))
    a, other = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    gate(b, input_digest=a)
    assert gate(b, input_digest=a)["gate"] == "over-limit"
    assert gate(b, input_digest=other)["gate"] == "over-limit"
    lim["repeat"] = 3                                                # the latched count 2 no longer crosses
    assert gate(b, input_digest=other)["gate"] == "go"
    assert latches(L) == []
    assert gate(b, input_digest=a)["gate"] == "go"                   # the crossing A was never counted: 2 of 3
    assert gate(b, input_digest=a)["gate"] == "over-limit"           # 3 of 3


def test_a_lowered_then_restored_active_hours_limit(L, resolver, clock):
    lim = BIG | {"active_hours": 0.1}
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(lim))
    d = digests()
    for t in (0, 300, 600):
        clock["now"] = NOW + timedelta(seconds=t)
        last = gate(b, input_digest=next(d))
    assert last["gate"] == "over-limit"
    lim["active_hours"] = 0.2                                        # above the latched 600 s
    clock["now"] = NOW + timedelta(seconds=700)
    assert gate(b, input_digest=next(d))["gate"] == "go"


def test_the_local_path_writes_no_latch(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    full = {k: float("inf") for k in consent_limits.KINDS} | {"repeat": 2.0}
    kw = {"tool_name": "Read", "tool_input": {"a": 1}, "limits": full}
    assert consent_limits.count_call(q, "codex", "s", {}, **kw).state == "ok"
    assert consent_limits.count_call(q, "codex", "s", {}, **kw).state == "crossed"
    with sqlite3.connect(q / consent_limits.DB_NAME) as conn:
        assert conn.execute("SELECT COUNT(*) FROM latches").fetchone() == (0,)
    # count_crossed=True counted the crossing: a different call is under the repeat limit, as Q13 holds it
    assert consent_limits.count_call(q, "codex", "s", {}, tool_name="Read", tool_input={"b": 2},
                                     limits=full).state == "ok"
