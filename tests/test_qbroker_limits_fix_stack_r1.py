"""Slot limits stack, the L3 r1 fixes (1025+16; project_memory/1025-16/limits/HEAD_RULINGS_STACK_R1.md): an explicit
null block refused, a GATE never clearing a latch with an older policy snapshot, a session's agent fixed at its first
GATE, an impossible latch count held, the read-back after a slot write guarded, and a fractional slot limit refused."""
from __future__ import annotations

import io
import json
import sqlite3
import threading

import pytest

from levain.consent import limits as consent_limits
from levain.qbroker import broker as qbroker
from levain.qbroker.limits import LimitsUnavailable, PolicyLimits
from levain.slot import install_helper as IH
from levain.slot import limits_doc
import tests.test_qbroker as base
from tests.test_qbroker import make_broker
from tests.test_qbroker_gate import gate
from tests.test_qbroker_limits import BIG, ME, digests, doc, policy, put
from tests.test_qbroker_limits_fix_r1 import store

L, clock, keys, resolver = base.L, base.clock, base.keys, base.resolver


# --- ruling 1, codex HIGH: an explicit null block is refused, never read as absent -----------------------------------

NULL_DOCS = [b'{"default":null,"format":"levain-limits/1"}', b'{"codex":null,"format":"levain-limits/1"}',
             b'{"claude_code":null,"default":{"calls":5},"format":"levain-limits/1"}']


@pytest.mark.parametrize("data", NULL_DOCS)
@pytest.mark.parametrize("canonical", [True, False])
def test_slot_doc_refuses_a_null_block(data, canonical):
    with pytest.raises(limits_doc.LimitsDocError) as got:
        limits_doc.slot_doc(data, canonical=canonical)
    assert got.value.code.startswith("limits-value:")


@pytest.mark.parametrize("block", ["default", "claude_code", "codex"])
def test_a_slot_read_refuses_a_null_block_whatever_the_agent(block):
    d = doc(**{block: None})
    for agent in (None, *limits_doc.AGENTS):
        with pytest.raises(limits_doc.LimitsDocError) as got:
            limits_doc.parse_limits_doc(d, agent, slot=True)
        assert got.value.code == f"limits-value:{block}"


def test_the_local_reading_names_a_null_block_as_skipped():
    # Q13's limits.json skips and names a block that is not an object (the other blocks stay in force); null is one
    out, bad = limits_doc.parse_limits_doc(doc(default=None, codex={"calls": 7}), "codex")
    assert bad == ["default"] and out["calls"] == 7


@pytest.mark.parametrize("data", NULL_DOCS)
def test_policy_limits_with_a_null_block_is_limits_failed_at_the_gate(L, resolver, clock, data):
    put(L / "policy", data)
    b = make_broker(L, resolver, clock, limits=PolicyLimits(L / "policy", owner=ME))
    got = gate(b)
    assert got["gate"] == "limits-failed" and "limits-value:" in got["reason"]


@pytest.mark.parametrize("data", NULL_DOCS)
def test_the_install_helper_refuses_a_null_block(data):
    with pytest.raises(IH.Refused) as got:
        IH.read_limits(lambda n: data)
    assert got.value.name.startswith("limits-value:")


# --- ruling 2, codex HIGH: a GATE never deletes a latch with a policy snapshot older than the crossing --------------

def test_a_gate_paused_on_an_older_limit_never_clears_the_newer_crossing(L, resolver, clock):
    a, b_, c = ("sha256:" + ch * 64 for ch in "abc")
    limit = {"repeat": 3}
    paused, resume = threading.Event(), threading.Event()
    armed = {"on": False}

    def limits(agent, session):
        got = BIG | dict(limit)                         # the snapshot this GATE runs with
        if armed["on"]:
            armed["on"] = False
            paused.set()
            assert resume.wait(10)
        return got
    br = make_broker(L, resolver, clock, limits=limits)
    assert gate(br, input_digest=a)["gate"] == "go"     # the window is [A]
    out = {}
    armed["on"] = True
    g1 = threading.Thread(target=lambda: out.__setitem__("g1", gate(br, input_digest=b_)))
    g1.start()
    assert paused.wait(10)                              # GATE 1 holds the snapshot repeat 3
    limit["repeat"] = 2                                 # the operator lowers it
    g2 = threading.Thread(target=lambda: out.__setitem__("g2", gate(br, input_digest=a)))
    g2.start()
    g2.join(1.0)                                        # unserialised, GATE 2 crosses at repeat 2 and latches here
    resume.set()
    g1.join(10)
    g2.join(10)
    assert out["g1"]["gate"] == "go" and out["g2"]["gate"] == "over-limit"
    got = gate(br, input_digest=c)                      # the latch stands: every later call is held
    assert got["gate"] == "over-limit" and got["kind"] == "repeat"


def test_gates_of_different_sessions_are_not_serialised_together(L, resolver, clock):
    paused, resume = threading.Event(), threading.Event()

    def limits(agent, session):
        if session == "sess-a":
            paused.set()
            assert resume.wait(10)
        return dict(BIG)
    br = make_broker(L, resolver, clock, limits=limits)
    out = {}
    g1 = threading.Thread(target=lambda: out.__setitem__("a", gate(br)))
    g1.start()
    assert paused.wait(10)
    try:
        assert gate(br, cap="cap-a2")["gate"] == "go"   # sess-b is not held behind sess-a's GATE
    finally:
        resume.set()
        g1.join(10)
    assert out["a"]["gate"] == "go"


# --- ruling 3, complement MED: a session's agent is fixed at its first GATE -----------------------------------------

def test_a_gate_naming_another_agent_for_the_session_is_limits_failed(L, resolver, clock):
    br = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 2})
    d = digests()
    assert gate(br, input_digest=next(d))["gate"] == "go"
    got = gate(br, agent="codex", input_digest=next(d))
    assert got["gate"] == "limits-failed" and "claude_code" in got["reason"] and "codex" in got["reason"]
    with sqlite3.connect(store(L)) as conn:             # nothing counted for the other agent
        assert conn.execute("SELECT agent, calls FROM sessions").fetchall() == [("claude_code", 1)]
    assert [gate(br, input_digest=next(d))["gate"] for _ in range(2)] == ["go", "over-limit"]
    assert gate(br, agent="codex", input_digest=next(d))["gate"] == "limits-failed"


def test_the_first_agent_of_a_session_holds_across_a_broker_restart(L, resolver, clock):
    br = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 1})
    d = digests()
    assert [gate(br, input_digest=next(d))["gate"] for _ in range(2)] == ["go", "over-limit"]
    again = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 1})
    assert gate(again, agent="codex", input_digest=next(d))["gate"] == "limits-failed"
    assert gate(again, input_digest=next(d))["gate"] == "over-limit"


def test_each_session_takes_its_own_first_agent(L, resolver, clock):
    br = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    assert gate(br)["gate"] == "go"
    assert gate(br, cap="cap-a2", agent="codex")["gate"] == "go"
    assert gate(br, cap="cap-a2")["gate"] == "limits-failed"


def test_a_session_whose_store_holds_two_agents_is_limits_failed(L, resolver, clock):
    br = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    assert gate(br)["gate"] == "go"
    with sqlite3.connect(store(L)) as conn:             # a store written before the agent was fixed
        conn.execute("INSERT INTO sessions (agent, root) SELECT 'codex', root FROM sessions")
    again = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    for agent in limits_doc.AGENTS:
        assert gate(again, agent=agent)["gate"] == "limits-failed"


def test_an_unreadable_store_fixes_no_agent_and_fails_closed(L, resolver, clock):
    from tests.test_qbroker_limits_fix_r1 import limits_dir
    limits_dir(L).mkdir(mode=0o700, parents=True)
    store(L).mkdir()                                    # a store that can never open
    got = gate(make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG)))
    assert got["gate"] == "limits-failed"


# --- ruling 4, codex MED: a latch row with an impossible count is held, never deleted ------------------------------

@pytest.mark.parametrize("row", [("repeat", -1), ("calls", 2.5), ("repeat", 1.5), ("active_hours", -0.5),
                                 ("calls", float("inf")), ("active_hours", float("inf")), ("calls", -1)])
def test_a_latch_row_with_an_impossible_count_is_held(L, resolver, clock, row):
    br = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert gate(br, input_digest=next(d))["gate"] == "go"
    with sqlite3.connect(store(L)) as conn:
        conn.execute("INSERT INTO latches (agent, root, kind, count, at) "
                     "SELECT agent, root, ?, ?, 0 FROM sessions", row)
    got = [gate(br, input_digest=next(d)) for _ in range(2)]
    assert [g["gate"] for g in got] == ["limits-failed"] * 2 and "latch" in got[0]["reason"]
    with sqlite3.connect(store(L)) as conn:
        assert conn.execute("SELECT kind, count FROM latches").fetchall() == [row]


def test_a_fractional_active_hours_latch_is_read(L, resolver, clock):
    br = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"active_hours": 1})
    d = digests()
    assert gate(br, input_digest=next(d))["gate"] == "go"
    with sqlite3.connect(store(L)) as conn:
        conn.execute("INSERT INTO latches (agent, root, kind, count, at) "
                     "SELECT agent, root, 'active_hours', 1.25, 0 FROM sessions")
    got = gate(br, input_digest=next(d))
    assert got["gate"] == "over-limit" and got["kind"] == "active_hours"


# --- ruling 5: the read-back after a successful slot write is guarded ------------------------------------------------

def test_a_read_back_that_fails_after_the_write_says_so_and_exits_zero(tmp_path):
    import pwd
    from tests.test_limits_cli_slots import Helper, _run
    root = tmp_path / "L"
    (root / "policy").mkdir(parents=True)
    (root / "policy").chmod(0o755)
    (root / "policy" / "operator").write_text(IH.operator_record_text(pwd.getpwuid(ME).pw_name, ME))
    real = Helper(root)

    def helper(data):
        got = real(data)
        (root / "policy" / "limits").chmod(0o666)      # the file root wrote, made unusable before the read-back
        return got
    code, text = _run(["set", "--slots", "default", "calls", "5"], root, helper)
    assert code == 0, text
    assert "default calls = 5" in text
    assert "the slot limits were written, but could not be read back:" in text
    assert "writable by group or others" in text
    assert json.loads((root / "policy" / "limits").read_bytes()) == {"default": {"calls": 5},
                                                                      "format": limits_doc.LIMITS_FORMAT}


# --- ruling 6: slot_limits refuses a non-integral number, takes an integral float -----------------------------------

@pytest.mark.parametrize("over", [{"calls": 1.5}, {"active_hours": 1.5}, {"repeat": 2.5}, {"calls": 3000.0001}])
def test_slot_limits_refuses_a_fractional_value(over):
    assert qbroker.slot_limits(BIG | over) is None


def test_slot_limits_takes_an_integral_float():
    got = qbroker.slot_limits({"calls": 2.0, "active_hours": 3.0, "repeat": 2.0})
    assert got is not None and (got["calls"], got["active_hours"], got["repeat"]) == (2.0, 3.0, 2.0)


def test_a_fractional_limit_is_limits_failed_at_the_gate(L, resolver, clock):
    got = gate(make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 1.5}))
    assert got["gate"] == "limits-failed" and got["reason"] == qbroker.LIMITS_SETTINGS_FAILED
