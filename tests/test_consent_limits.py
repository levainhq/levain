"""Q13 session 1: the per-session counter (levain.consent.limits) and the incremental spend reader it shares with the
cockpit meter (levain.cockpit.session_context.spend_step)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain.cockpit import session_context as sc
from levain.consent import limits as L


def _qdir(tmp_path: Path) -> Path:
    q = tmp_path / "consent"
    q.mkdir(mode=0o700, parents=True)
    return q


def _ev(i: int | None = None, transcript: str | None = None) -> dict:
    e: dict = {"hook_event_name": "PreToolUse"}
    if i is not None:
        e["tool_use_id"] = f"toolu_{i}"
    if transcript:
        e["transcript_path"] = transcript
    return e


def _count(q: Path, i, *, session="s1", tool="Read", inp=None, now=1000.0, limits=None, transcript=None,
           agent="claude_code"):
    return L.count_call(q, agent, session, _ev(i, transcript), tool_name=tool,
                        tool_input=inp if inp is not None else {"file_path": f"/x/{i}"}, now=now,
                        limits=limits)


def test_calls_cross_after_the_limit_and_subagents_share_the_root(tmp_path):
    q = _qdir(tmp_path)
    lim = dict(L.DEFAULTS, calls=3)
    assert _count(q, 1, limits=lim).state == "ok"
    assert _count(q, 2, session="s1#agent:abc", limits=lim).state == "ok"
    assert _count(q, 3, limits=lim).state == "ok"
    v = _count(q, 4, session="s1#agent:def", limits=lim)
    assert (v.state, v.kind, v.count, v.limit) == ("crossed", "calls", 4, 3)
    assert _count(q, 5, session="s2", limits=lim).state == "ok"   # another session is its own count


def test_a_call_id_counts_once(tmp_path):
    q = _qdir(tmp_path)
    for _ in range(3):
        v = _count(q, 7)
    assert v.counts["calls"] == 1
    assert _count(q, None).counts["calls"] == 2
    assert _count(q, None).counts["calls"] == 3   # no id: every attempt counts


def test_loop_guard_eight_in_ten_including_alternation(tmp_path):
    q = _qdir(tmp_path)
    same = {"file_path": "/same"}
    states = [_count(q, i, inp=same if i % 5 else {"file_path": f"/o{i}"}).state for i in range(1, 11)]
    assert states[:8] == ["ok"] * 8 and states[8] == "crossed"   # the 8th recurrence of one call in 10
    q2 = _qdir(tmp_path / "b")
    alt = [_count(q2, i, inp={"file_path": "/a" if i % 2 else "/b"}).state for i in range(1, 16)]
    assert "crossed" not in alt   # A,B,A,B: each recurs 5 in 10
    q3 = _qdir(tmp_path / "c")
    assert [_count(q3, i, inp={"p": 1}).kind for i in range(1, 9)][-1] == "repeat"


def test_extend_doubles_and_repeat_window_doubles(tmp_path):
    q = _qdir(tmp_path)
    lim = dict(L.DEFAULTS, calls=2)
    for i in range(1, 4):
        v = _count(q, i, limits=lim)
    assert v.state == "crossed"
    assert L.extend(q, "claude_code", "s1#agent:x", "calls", "req-a") == 2
    assert _count(q, 4, limits=lim).state == "ok"
    v = _count(q, 5, limits=lim)
    assert (v.state, v.limit) == ("crossed", 4)
    q2 = _qdir(tmp_path / "r")
    for i in range(1, 9):
        v = _count(q2, i, inp={"p": 1})
    assert v.kind == "repeat"
    L.extend(q2, "claude_code", "s1", "repeat", "req-b")
    for i in range(9, 16):
        v = _count(q2, i, inp={"p": 1})
    assert v.state == "ok" and v.limits["repeat"] == 16
    assert _count(q2, 16, inp={"p": 1}).kind == "repeat"


def test_active_hours_skip_long_gaps(tmp_path):
    q = _qdir(tmp_path)
    _count(q, 1, now=0)
    _count(q, 2, now=300)
    _count(q, 3, now=300 + 3 * 86400)     # a weekend later: not active time
    v = _count(q, 4, now=300 + 3 * 86400 + 60)
    assert v.counts["active_hours"] == pytest.approx(360 / 3600)


def test_halt_denies_and_counts_nothing(tmp_path):
    q = _qdir(tmp_path)
    _count(q, 1)
    conn = L._connect(q)
    conn.execute("INSERT INTO halts (agent, root, kind, request_id, at) VALUES ('claude_code','s1','calls','r1',1)")
    conn.close()
    v = _count(q, 2, session="s1#agent:z")
    assert v.state == "halted" and v.halt["request_id"] == "r1" and v.counts["calls"] == 1


def test_store_failure_fails_open_then_denies(tmp_path, monkeypatch):
    q = _qdir(tmp_path)
    _count(q, 1)   # the failure file is pre-allocated at the first counted call
    fail = L._fail_path(q, "claude_code", "s1")
    assert fail.stat().st_size == 8

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(L, "_count", boom)
    states = [_count(q, i).state for i in range(2, 2 + L.FAIL_DENY_AFTER)]
    assert states[:-1] == ["not_counting"] * (L.FAIL_DENY_AFTER - 1) and states[-1] == "fail_deny"
    monkeypatch.undo()
    assert _count(q, 99).state == "ok"
    assert L._fail_read(fail) == 0


def test_limits_json_overlays_and_bad_file_keeps_defaults(tmp_path):
    q = _qdir(tmp_path)
    assert L.load_limits(q, "codex") == (L.DEFAULTS, "defaults")
    (q / L.LIMITS_NAME).write_text(json.dumps({"format": L.LIMITS_FORMAT, "default": {"calls": 10},
                                               "codex": {"calls": 20, "repeat": 0}}))
    got, src = L.load_limits(q, "codex")
    assert got["calls"] == 20 and got["repeat"] == 8 and "codex.repeat" in src
    assert L.load_limits(q, "claude_code")[0]["calls"] == 10
    (q / L.LIMITS_NAME).write_text("{nope")
    got, src = L.load_limits(q, "codex")
    assert got == L.DEFAULTS and "unreadable" in src


# --- the incremental spend reader ---------------------------------------------------------------------------------

def _asst(mid: str, i: int, cr: int, cc: int, o: int) -> str:
    return json.dumps({"type": "assistant", "uuid": mid + str(o), "message": {
        "id": mid, "model": "m", "usage": {"input_tokens": i, "cache_read_input_tokens": cr,
                                           "cache_creation_input_tokens": cc, "output_tokens": o}}}) + "\n"


def test_spend_step_matches_iter_usage_incrementally(tmp_path):
    t = tmp_path / "sid.jsonl"
    sub = tmp_path / "sid" / "subagents"
    sub.mkdir(parents=True)
    t.write_text(_asst("m1", 10, 100, 5, 1) + _asst("m1", 10, 100, 5, 7))
    st = sc.spend_step(t)
    assert (st["fresh"], st["cache_read"]) == (22, 100)
    with t.open("a") as f:
        f.write(_asst("m1", 10, 100, 5, 9) + _asst("m2", 3, 0, 0, 2) + '{"type":"assist')   # unfinished line
    (sub / "agent-a.jsonl").write_text(_asst("m3", 1, 50, 1, 1))
    st = sc.spend_step(t, state=st)
    with t.open("a") as f:
        f.write('ant"}\n')
    st = sc.spend_step(t, state=st)
    want = list(sc.iter_usage(t, include_subagents=True))
    assert st["fresh"] == sum(r["input"] + r["output"] + r["cache_creation"] for r in want)
    assert st["cache_read"] == sum(r["cache_read"] for r in want)


def test_spend_step_codex_cumulative(tmp_path):
    t = tmp_path / "rollout-x.jsonl"
    rec = lambda i, c, o: json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {  # noqa: E731
        "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}}) + "\n"
    t.write_text(rec(100, 60, 10) + rec(300, 200, 30))
    st = sc.spend_step(t, "codex")
    assert (st["fresh"], st["cache_read"]) == (130, 200)


def test_spend_step_budget_reads_the_rest_later(tmp_path):
    t = tmp_path / "sid.jsonl"
    t.write_text("".join(_asst(f"m{i}", 1, 1, 1, 1) for i in range(200)))
    st = sc.spend_step(t, step_bytes=4000)
    assert st["behind"] and st["fresh"] < 600
    while st.get("behind"):
        st = sc.spend_step(t, state=st, step_bytes=4000)
    assert st["fresh"] == 600 and st["cache_read"] == 200


def test_tokens_cross_through_the_counter(tmp_path):
    q = _qdir(tmp_path)
    t = tmp_path / "sid.jsonl"
    t.write_text(_asst("m1", 10, 100, 0, 0))
    lim = dict(L.DEFAULTS, fresh_tokens=50)
    assert _count(q, 1, transcript=str(t), limits=lim).state == "ok"
    with t.open("a") as f:
        f.write(_asst("m2", 40, 0, 0, 10))
    v = _count(q, 2, transcript=str(t), limits=lim)
    assert (v.state, v.kind, v.count) == ("crossed", "fresh_tokens", 60)
    os.unlink(t)                      # a missing transcript reads nothing and keeps the totals
    assert _count(q, 3, transcript=str(t), limits=lim).counts["fresh_tokens"] == 60


# --- L1 r1 findings ------------------------------------------------------------------------------------------------

def test_an_oversized_limit_value_keeps_the_defaults_and_counting(tmp_path):
    q = _qdir(tmp_path)
    (q / L.LIMITS_NAME).write_text('{"format": "%s", "default": {"calls": 1%s}}' % (L.LIMITS_FORMAT, "0" * 400))
    got, src = L.load_limits(q, "claude_code")
    assert got["calls"] == L.DEFAULTS["calls"] and "default.calls" in src
    v = L.count_call(q, "claude_code", "s1", _ev(1), tool_name="Read", tool_input={})
    assert v.state == "ok" and v.limits["calls"] == L.DEFAULTS["calls"]


def test_switching_transcripts_never_recounts_a_file(tmp_path):
    q = _qdir(tmp_path)
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text(_asst("ma", 1000, 0, 0, 0))
    b.write_text(_asst("mb", 500, 0, 0, 0))
    for i in range(6):
        v = _count(q, i, transcript=str(a if i % 2 == 0 else b))
    assert v.counts["fresh_tokens"] == 1500


def test_count_call_never_raises_on_an_unreadable_failure_dir(tmp_path):
    q = _qdir(tmp_path)
    _count(q, 1)
    os.chmod(q / L.FAIL_DIR, 0)
    try:
        assert _count(q, 2).state == "ok"
    finally:
        os.chmod(q / L.FAIL_DIR, 0o700)


def test_a_store_that_never_worked_is_still_capped(tmp_path, monkeypatch):
    q = _qdir(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("the limits store is 'levain-limits-store/9'")
    monkeypatch.setattr(L, "_count", boom)
    states = [_count(q, i).state for i in range(L.FAIL_DENY_AFTER)]
    assert states[-1] == "fail_deny" and "moves that file aside" in _count(q, 99).reason
