"""Slot limits S-a, the L3 r1 fixes (1025+10; project_memory/1025-10/limits/HEAD_RULINGS_SA_R1.md): the limits
directory under a setgid parent, the slot path never failing open, every limits-source failure answered
``limits-failed``, an agent block that is not an object, the slot ranges in :func:`levain.qbroker.broker.slot_limits`,
and an unreadable latch row held."""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from levain.consent import limits as consent_limits
from levain.contracts.canon import canonical_bytes
from levain.qbroker import broker as qbroker
from levain.qbroker.limits import LimitsUnavailable, PolicyLimits
import tests.test_qbroker as base
from tests.test_qbroker import make_broker
from tests.test_qbroker_gate import gate
from tests.test_qbroker_limits import BIG, ME, digests, doc, policy, put

L, clock, keys, resolver = base.L, base.clock, base.keys, base.resolver


def limits_dir(L):
    return L / "queue" / "requests" / qbroker.LIMITS_DIR


def store(L):
    return limits_dir(L) / consent_limits.DB_NAME


# --- codex HIGH: requests/limits beneath a setgid 02750 parent ----------------------------------------------------

def test_the_limits_directory_check_reads_the_permission_bits_not_the_setgid_bit():
    def st(mode, uid=None):
        return os.stat_result((mode, 1, 1, 2, os.geteuid() if uid is None else uid, 0, 64, 0, 0, 0))
    assert qbroker._private_dir(st(stat.S_IFDIR | 0o2700))           # what mkdir(0o700) gives under 02750 on Linux
    assert qbroker._private_dir(st(stat.S_IFDIR | 0o700))
    for bad in (st(stat.S_IFDIR | 0o2750), st(stat.S_IFDIR | 0o710), st(stat.S_IFREG | 0o700),
                st(stat.S_IFDIR | 0o700, uid=os.geteuid() + 1)):
        assert not qbroker._private_dir(bad)


def test_a_setgid_limits_directory_counts(L, resolver, clock, monkeypatch):
    # this Mac's mkdir does not carry a parent's setgid bit (probe: a 02750 parent, mkdir(0o700) -> 0700), so the
    # Linux mode is put in by the lstat the broker and the counter read
    real = os.lstat

    def lstat(path, *a, **k):
        got = real(path, *a, **k)
        if os.fspath(path) == os.fspath(limits_dir(L)):
            return os.stat_result((got.st_mode | stat.S_ISGID,) + tuple(got)[1:10])
        return got
    monkeypatch.setattr(os, "lstat", lstat)
    b = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 2})
    d = digests()
    assert [gate(b, input_digest=next(d))["gate"] for _ in range(3)] == ["go", "go", "over-limit"]
    assert os.lstat(limits_dir(L)).st_mode & stat.S_ISGID


# --- complement MED 1: the slot path never answers GO on a counter failure ------------------------------------------

def test_a_failing_store_is_limits_failed_on_the_first_slot_call(L, resolver, clock):
    limits_dir(L).mkdir(mode=0o700, parents=True)
    store(L).mkdir()                                     # a store that can never open
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    got = [gate(b) for _ in range(3)]
    assert [g["gate"] for g in got] == ["limits-failed"] * 3
    assert "limits store" in got[0]["reason"] and "limits.db" in got[0]["reason"]


def test_a_latch_write_that_fails_after_a_crossing_is_limits_failed_never_go(L, resolver, clock):
    b = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 2})
    d = digests()
    assert [gate(b, input_digest=next(d))["gate"] for _ in range(2)] == ["go", "go"]
    with sqlite3.connect(store(L)) as conn:
        conn.execute("CREATE TRIGGER no_latch BEFORE INSERT ON latches BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    got = [gate(b, input_digest=next(d)) for _ in range(3)]
    assert [g["gate"] for g in got] == ["limits-failed"] * 3 and "disk full" in got[0]["reason"]


def test_the_local_path_keeps_its_bounded_fail_open(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    (q / consent_limits.DB_NAME).mkdir()
    kw = {"tool_name": "Read", "tool_input": {}, "limits": {k: float("inf") for k in consent_limits.KINDS}}
    got = [consent_limits.count_call(q, "codex", "s", {}, **kw).state for _ in range(consent_limits.FAIL_DENY_AFTER)]
    assert got == ["not_counting"] * (consent_limits.FAIL_DENY_AFTER - 1) + ["fail_deny"]


def test_a_not_counting_verdict_is_never_go_at_the_gate(L, resolver, clock, monkeypatch):
    monkeypatch.setattr(consent_limits, "count_call", lambda *a, **k: consent_limits.Verdict("not_counting"))
    got = gate(make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG)))
    assert got["gate"] == "limits-failed"


# --- complement MED 2, codex LOW, deepseek LOW 3: every limits-source failure answers limits-failed ------------------

@pytest.mark.parametrize("exc", [OSError(5, "EIO"), ValueError("x"), RuntimeError("y"), RecursionError()])
def test_a_settings_callable_raising_anything_is_limits_failed(L, resolver, clock, exc):
    def boom(agent, session):
        raise exc
    got = gate(make_broker(L, resolver, clock, limits=boom))
    assert got["gate"] == "limits-failed" and type(exc).__name__ in got["reason"]


def test_an_unrepresentable_limit_is_limits_failed(L, resolver, clock):
    got = gate(make_broker(L, resolver, clock, limits=lambda a, s: {"calls": 10**10000, "active_hours": 1,
                                                                    "repeat": 8}))
    assert got["gate"] == "limits-failed" and got["reason"] == qbroker.LIMITS_SETTINGS_FAILED


def test_policy_limits_turns_an_unexpected_failure_into_its_reason(tmp_path, monkeypatch):
    d = policy(tmp_path)
    put(d, doc(default={"calls": 5}))
    p = PolicyLimits(d, owner=ME)

    def boom(data, **k):
        raise RuntimeError("the parser broke")
    monkeypatch.setattr(consent_limits, "slot_doc", boom)
    with pytest.raises(LimitsUnavailable) as got:
        p("codex", "s")
    assert str(p.path) in got.value.reason and "RuntimeError" in got.value.reason
    assert "levain limits set --slots" in got.value.reason


def test_a_parser_recursion_error_is_limits_malformed(monkeypatch):
    from levain.contracts import canon

    def deep(data):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(canon, "parse", deep)
    with pytest.raises(consent_limits.LimitsDocError) as got:
        consent_limits.slot_doc(canonical_bytes(doc()))
    assert got.value.code == "limits-malformed"


def test_a_deeply_nested_document_is_limits_malformed():
    # the contracts parser bounds nesting itself (a ContractError), so this real input never reaches RecursionError
    data = b'{"format":"levain-limits/1","x":' + b"[" * 1500 + b"]" * 1500 + b"}"
    assert len(data) <= consent_limits.SLOT_LIMITS_MAX
    with pytest.raises(consent_limits.LimitsDocError) as got:
        consent_limits.slot_doc(data)
    assert got.value.code == "limits-malformed"


# --- deepseek MED: an agent outside AGENTS, a block that is not an object -------------------------------------------

def test_a_gate_naming_an_agent_outside_agents_is_refused_before_limits_are_read(L, resolver, clock):
    asked = []
    b = make_broker(L, resolver, clock, limits=lambda a, s: asked.append(a) or dict(BIG))
    for agent in ("format", "default", "nobody"):
        got = gate(b, agent=agent)
        assert got == {"id": got["id"], "ok": False, "refused": "frame-malformed"}
    assert asked == []


def test_a_slot_read_for_an_agent_naming_a_non_object_block_is_refused():
    d = doc(default={"calls": 5})
    for agent in ("format", "nobody"):
        with pytest.raises(consent_limits.LimitsDocError):
            consent_limits.parse_limits_doc(d, agent, slot=True)


def test_policy_limits_for_the_format_key_is_limits_unavailable(tmp_path):
    d = policy(tmp_path)
    put(d, doc(default={"calls": 5}))
    with pytest.raises(LimitsUnavailable):
        PolicyLimits(d, owner=ME)("format", "s")


# --- deepseek LOW 2: slot_limits holds SLOT_RANGE ------------------------------------------------------------------

@pytest.mark.parametrize("over", [{"repeat": 1}, {"repeat": consent_limits.REPEAT_WINDOW + 1}, {"calls": 0.5},
                                  {"active_hours": 0.5}, {"calls": 10**9 + 1}])
def test_slot_limits_refuses_a_value_outside_the_slot_range(over):
    assert qbroker.slot_limits(BIG | over) is None


def test_slot_limits_takes_the_range_ends():
    lo = {k: r[0] for k, r in consent_limits.SLOT_RANGE.items()}
    hi = {k: r[1] for k, r in consent_limits.SLOT_RANGE.items()}
    for v in (lo, hi):
        got = qbroker.slot_limits(v)
        assert got is not None and all(got[k] == float(v[k]) for k in consent_limits.SLOT_KINDS)


# --- complement LOW 3: an unreadable latch row is held, never deleted ----------------------------------------------

@pytest.mark.parametrize("row", [("bogus", 3.0), ("calls", "three")])
def test_an_unreadable_latch_row_is_held(L, resolver, clock, row):
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert gate(b, input_digest=next(d))["gate"] == "go"
    with sqlite3.connect(store(L)) as conn:
        conn.execute("INSERT INTO latches (agent, root, kind, count, at) "
                     "SELECT agent, root, ?, ?, 0 FROM sessions", row)
    got = [gate(b, input_digest=next(d)) for _ in range(2)]
    assert [g["gate"] for g in got] == ["limits-failed"] * 2 and "latch" in got[0]["reason"]
    with sqlite3.connect(store(L)) as conn:
        assert conn.execute("SELECT kind, count FROM latches").fetchall() == [row]
