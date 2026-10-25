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


# --- PolicyLimits: L/policy/limits (DESIGN §3; R0 rulings 1-2; R1 F1, F4) -------------------------------------------

import logging  # noqa: E402
import os  # noqa: E402

import pytest  # noqa: E402

from levain.contracts.canon import canonical_bytes  # noqa: E402
from levain.qbroker import __main__ as qmain  # noqa: E402
from levain.qbroker.limits import POLICY_LIMITS, LimitsUnavailable, PolicyLimits  # noqa: E402

ME = os.geteuid()
SLOT_DEFAULTS = {k: float(consent_limits.DEFAULTS[k]) for k in consent_limits.SLOT_KINDS}


def policy(tmp_path):
    d = tmp_path / "policy"
    d.mkdir(mode=0o755, exist_ok=True)
    d.chmod(0o755)
    return d


def put(d, data, mode=0o644):
    """Replace the file as the root writer does (a new inode, renamed in)."""
    tmp = d / "limits.tmp"
    tmp.write_bytes(data if isinstance(data, bytes) else canonical_bytes(data))
    tmp.chmod(mode)
    os.replace(tmp, d / POLICY_LIMITS)


def doc(**blocks):
    return {"format": consent_limits.LIMITS_FORMAT, **blocks}


def test_policy_limits_missing_is_defaults(tmp_path):
    got = PolicyLimits(policy(tmp_path), owner=ME)("codex", "s")
    assert got == SLOT_DEFAULTS


def test_policy_limits_reads_the_agents_block(tmp_path):
    d = policy(tmp_path)
    put(d, doc(default={"calls": 40}, codex={"calls": 20, "repeat": 5}))
    p = PolicyLimits(d, owner=ME)
    assert p("codex", "s") == SLOT_DEFAULTS | {"calls": 20.0, "repeat": 5.0}
    assert p("claude_code", "s") == SLOT_DEFAULTS | {"calls": 40.0}


def test_policy_limits_never_returns_none_or_a_partial_mapping(tmp_path):
    d = policy(tmp_path)
    for body in (doc(), doc(default={"calls": 3}), doc(codex={"repeat": 2})):
        put(d, body)
        got = PolicyLimits(d, owner=ME)("codex", "s")
        assert got is not None and set(got) == set(consent_limits.SLOT_KINDS)


def test_policy_limits_reloads_on_change_and_reads_an_unchanged_file_once(tmp_path, monkeypatch):
    d = policy(tmp_path)
    put(d, doc(default={"calls": 5}))
    reads = []
    real = consent_limits.slot_doc
    monkeypatch.setattr(consent_limits, "slot_doc", lambda data, **k: reads.append(1) or real(data, **k))
    p = PolicyLimits(d, owner=ME)
    assert [p("codex", "s")["calls"] for _ in range(3)] == [5.0] * 3 and len(reads) == 1
    put(d, doc(default={"calls": 6}))
    assert p("codex", "s")["calls"] == 6.0 and len(reads) == 2


@pytest.mark.parametrize("spoil,why", [
    ("symlink", "a symlink"),
    ("wide", "writable by group or others"),
    ("dir", "not a regular file"),
    ("fifo", "not a regular file"),
    ("oversize", "over"),
    ("spaced", "canonical"),
    ("float", "limits-malformed"),
    ("token", "limits-not-counted:fresh_tokens"),
    ("repeat11", "limits-value:default.repeat"),
    ("garbage", "limits-malformed"),
])
def test_policy_limits_present_but_unusable_fails_closed(tmp_path, spoil, why):
    d = policy(tmp_path)
    target = d / POLICY_LIMITS
    if spoil == "symlink":
        (tmp_path / "real").write_bytes(canonical_bytes(doc()))
        target.symlink_to(tmp_path / "real")
    elif spoil == "wide":
        put(d, doc(), mode=0o666)
    elif spoil == "dir":
        target.mkdir()
    elif spoil == "fifo":
        os.mkfifo(target)                    # O_NONBLOCK: opened without a writer, never a hang
    elif spoil == "oversize":
        put(d, b"{" + b" " * consent_limits.SLOT_LIMITS_MAX + b"}")
    elif spoil == "spaced":
        put(d, b'{"format": "levain-limits/1"}')
    elif spoil == "float":
        put(d, b'{"default":{"active_hours":1.5},"format":"levain-limits/1"}')
    elif spoil == "token":
        put(d, doc(default={"fresh_tokens": 9}))
    elif spoil == "repeat11":
        put(d, doc(default={"repeat": 11}))
    else:
        put(d, b"\x00garbage")
    with pytest.raises(LimitsUnavailable) as e:
        PolicyLimits(d, owner=ME)("codex", "s")
    assert why in e.value.reason and str(target) in e.value.reason
    assert "levain limits set --slots" in e.value.reason


def test_policy_limits_another_owner_fails_closed(tmp_path):
    d = policy(tmp_path)
    put(d, doc())
    with pytest.raises(LimitsUnavailable) as e:      # the directory is checked first; both are this uid's, not root's
        PolicyLimits(d, owner=ME + 1)("codex", "s")
    assert "policy directory" in e.value.reason


def test_policy_limits_a_good_file_replaced_by_a_bad_one_has_no_last_good(tmp_path):
    d = policy(tmp_path)
    put(d, doc(default={"calls": 5}))
    p = PolicyLimits(d, owner=ME)
    assert p("codex", "s")["calls"] == 5.0
    put(d, b"{nope")
    with pytest.raises(LimitsUnavailable):
        p("codex", "s")
    put(d, doc(default={"calls": 7}))                 # set-limits restores it: the next call reads it
    assert p("codex", "s")["calls"] == 7.0


@pytest.mark.parametrize("spoil", ["absent", "symlink", "wide", "file"])
def test_policy_limits_an_unsound_policy_directory_fails_closed(tmp_path, spoil):
    d = tmp_path / "policy"
    if spoil == "symlink":
        (tmp_path / "x").mkdir()
        policy(tmp_path / "x")
        d.symlink_to(tmp_path / "x" / "policy")
    elif spoil == "wide":
        policy(tmp_path).chmod(0o777)
    elif spoil == "file":
        d.write_text("x")
    with pytest.raises(LimitsUnavailable) as e:
        PolicyLimits(d, owner=ME)("codex", "s")
    assert "policy directory" in e.value.reason


def test_policy_limits_logs_an_unusable_file_once_per_identity(tmp_path, caplog):
    d = policy(tmp_path)
    put(d, b"{nope")
    p = PolicyLimits(d, owner=ME)
    caplog.set_level(logging.WARNING, logger="levain.qbroker")
    for _ in range(3):
        with pytest.raises(LimitsUnavailable):
            p("codex", "s")
    put(d, b"{nope!")
    with pytest.raises(LimitsUnavailable):
        p("codex", "s")
    assert sum("fails closed" in r.getMessage() for r in caplog.records) == 2


# --- the GATE with PolicyLimits ------------------------------------------------------------------------------------

def test_gate_enforces_the_policy_file(L, resolver, clock):
    d = policy(L)
    put(d, doc(default={"calls": 2}))
    b = make_broker(L, resolver, clock, limits=PolicyLimits(d, owner=ME))
    d_ = digests()
    got = [gate(b, input_digest=next(d_)) for _ in range(4)]
    assert [g["gate"] for g in got] == ["go", "go", "over-limit", "over-limit"] and got[2]["kind"] == "calls"
    put(d, doc(default={"calls": 4}))                 # the operator raises it: the latched session goes on
    assert gate(b, input_digest=next(d_))["gate"] == "go"


def test_gate_with_an_unusable_policy_file_is_limits_failed_never_go(L, resolver, clock):
    d = policy(L)
    put(d, b"{nope")
    b = make_broker(L, resolver, clock, limits=PolicyLimits(d, owner=ME))
    got = [gate(b) for _ in range(3)]
    assert [g["gate"] for g in got] == ["limits-failed"] * 3
    assert str(d / POLICY_LIMITS) in got[0]["reason"]


@pytest.mark.parametrize("answer", [None, {}, {"calls": 2}, {"calls": 2, "active_hours": 1},
                                    {"calls": 2, "active_hours": 1, "repeat": "8"},
                                    {"calls": 2, "active_hours": 1, "repeat": True},
                                    {"calls": 2, "active_hours": float("nan"), "repeat": 8},
                                    {"calls": 2, "active_hours": float("inf"), "repeat": 8},
                                    {"calls": 0, "active_hours": 1, "repeat": 8}])
def test_gate_refuses_a_settings_answer_missing_or_spoiling_a_slot_kind(L, resolver, clock, answer):
    b = make_broker(L, resolver, clock, limits=lambda a, s: answer)
    got = gate(b)
    assert got["gate"] == "limits-failed" and got["reason"] == qbroker.LIMITS_SETTINGS_FAILED


def test_gate_says_why_the_settings_could_not_be_read(L, resolver, clock):
    def boom(agent, session):
        raise LimitsUnavailable("the file is on fire")
    got = gate(make_broker(L, resolver, clock, limits=boom))
    assert got["gate"] == "limits-failed" and got["reason"] == "the file is on fire"


# --- __main__: the production broker (R1 F1) ----------------------------------------------------------------------

def test_the_production_broker_has_policy_limits(L, tmp_path):
    args = qmain.parse_args(["--store", str(L), "--run", str(tmp_path / "run"), "--slot", "0"])
    b = qmain.build_broker(args)
    assert b._limits is not None and isinstance(b._limits, PolicyLimits)
    assert b._limits.path == L / "policy" / POLICY_LIMITS and b._limits._owner == 0


def test_the_production_broker_enforces_the_policy_file(L, tmp_path, monkeypatch):
    # build_broker as main builds it (owner: this uid, as a test cannot be root), the cap resolved by a stand-in
    from tests.test_qbroker import SESSION_A
    d = policy(L)
    put(d, doc(default={"calls": 2}))
    args = qmain.parse_args(["--store", str(L), "--run", str(tmp_path / "run"), "--slot", "1"])
    b = qmain.build_broker(args, limits_owner=ME)
    monkeypatch.setattr(b, "_resolve", lambda slot, cap: SESSION_A)
    d_ = digests()
    assert [gate(b, input_digest=next(d_))["gate"] for _ in range(3)] == ["go", "go", "over-limit"]


def test_the_slot_texts_name_the_slot_limits_settings():
    from levain.consent import broker_client
    from tests.test_consent_broker_client import Scripted
    kw = {"agent": "codex", "tool_name": "Bash", "tool_input": {}, "tool_use_id": None}
    over = broker_client.gate_check(Scripted([{"gate": "over-limit", "kind": "repeat"}]), **kw)
    assert "levain limits set --slots" in over and "levain limits extend" not in over
    failed = broker_client.gate_check(Scripted([{"gate": "limits-failed", "reason": "x"}]), **kw)
    assert "L/policy/limits" in failed and "levain limits set --slots" in failed and "limits.db" in failed
