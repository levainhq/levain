from __future__ import annotations

import json
import os
import types
from pathlib import Path

from levain.cockpit import session_context
from levain.cockpit.session_context import CONTEXT_WARN_TOKENS, context_chip, find_transcript, iter_usage, last_context


def _asst(mid: str, out: int, inp: int = 3, read: int = 200_000, create: int = 50) -> str:
    return json.dumps({"type": "assistant", "message": {"id": mid, "model": "claude-x", "usage": {
        "input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": create,
        "output_tokens": out}}}) + "\n"


def test_claude_context_is_the_last_prompt_sum_and_a_message_in_two_records_counts_once(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text(_asst("m1", 5) + _asst("m2", 10, read=1000) + _asst("m2", 40, read=1000))
    ctx, why = last_context(t)
    assert (ctx.tokens, ctx.window, ctx.model, why) == (3 + 1000 + 50, None, "claude-x", "")
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(_asst("m9", 7))
    # a zeroed <synthetic> / API-error record newer than a real reply is not a reading
    syn = json.loads(_asst("x", 0, inp=0, read=0, create=0))
    syn["message"]["model"] = "<synthetic>"
    err = json.loads(_asst("y", 0, inp=0, read=0, create=0))
    err["isApiErrorMessage"] = True
    t.write_text(_asst("m1", 5) + json.dumps(syn) + "\n" + json.dumps(err) + "\n")
    assert last_context(t)[0].tokens == 3 + 200_000 + 50
    # a compaction boundary newer than the newest usable reply says so, with no number; the next reply is a number again
    boundary = json.dumps({"type": "system", "subtype": "compact_boundary",
                           "compactMetadata": {"trigger": "auto", "preTokens": 900_000, "postTokens": 10_000}}) + "\n"
    t.write_text(_asst("m1", 5) + boundary + json.dumps(syn) + "\n")
    chip, ctx = context_chip({"agent": "claude_code", "session_id": "s", "transcript": str(t)})
    assert (chip, ctx.tokens, ctx.compacted) == ("context: just compacted", 0, True)
    t.write_text(_asst("m1", 5) + boundary + _asst("m3", 5, read=90_000))
    assert last_context(t)[0].tokens == 3 + 90_000 + 50 and not last_context(t)[0].compacted
    t.write_text(_asst("m1", 5) + _asst("m2", 10, read=1000) + _asst("m2", 40, read=1000))
    assert [r["output"] for r in iter_usage(t)] == [5, 40]
    # id-less rows are separate rows, and an over-long line is skipped, not held
    noid = json.loads(_asst("z", 1))
    del noid["message"]["id"]
    big = b'{"type":"assistant","pad":"' + b"x" * (5 << 20) + b'"}\n'
    u = tmp_path / "u.jsonl"
    u.write_bytes((json.dumps(noid) + "\n").encode() * 2 + big + (json.dumps(noid) + "\n").encode())
    assert len(list(iter_usage(u))) == 3
    assert sorted(r["output"] for r in iter_usage(t, include_subagents=True)) == [5, 7, 40]


def test_codex_context_is_the_newest_last_token_usage_input_with_its_own_window(tmp_path):
    def ev(n: int) -> str:
        return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": n, "cached_input_tokens": n // 2}, "model_context_window": 258400}}}) + "\n"
    r = tmp_path / "rollout-2026-10-19T00-00-00-abc.jsonl"
    r.write_text(ev(10) + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}}) + "\n" + ev(220_000))
    chip, ctx = context_chip({"agent": "codex", "session_id": "abc", "transcript": str(r)})
    assert (ctx.tokens, ctx.window) == (220_000, 258400)
    assert chip == f"context 220K of 258K, past the {CONTEXT_WARN_TOKENS // 1000}K warn line"
    # right after a compaction the 0-input reading is skipped; the compaction itself reads "just compacted"
    compacted = json.dumps({"type": "compacted", "payload": {"message": ""}}) + "\n"
    r.write_text(ev(220_000) + compacted + ev(0))
    assert context_chip({"agent": "codex", "session_id": "abc", "transcript": str(r)})[0] == "context: just compacted"
    r.write_text(ev(220_000) + compacted + ev(0) + ev(87_000))
    assert last_context(r, "codex")[0].tokens == 87_000
    # a rollout is found by session id, newest date first, and a remembered path is re-checked on use
    home = tmp_path / "ch"
    old, new = home / "sessions/2026/01/01", home / "sessions/2026/10/19"
    old.mkdir(parents=True), new.mkdir(parents=True)
    (old / "rollout-a-sid1.jsonl").write_text("")
    env = {"CODEX_HOME": str(home)}
    rec = {"agent": "codex", "session_id": "sid1"}
    assert find_transcript(rec, env)[0] == old / "rollout-a-sid1.jsonl"
    (old / "rollout-a-sid1.jsonl").rename(new / "rollout-b-sid1.jsonl")
    assert find_transcript(rec, env)[0] == new / "rollout-b-sid1.jsonl"
    assert find_transcript({"agent": "codex", "session_id": "*"}, env)[0] is None


def test_a_missing_or_symlinked_transcript_is_not_read(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text(_asst("m1", 1))
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert last_context(tmp_path / "none.jsonl")[0] is None and last_context(link)[0] is None
    chip, ctx = context_chip({"agent": "claude_code", "session_id": "s", "cwd": "/nowhere/x"}, {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert ctx is None and chip.startswith("context not read: ")


def test_a_bool_is_not_a_token_count_and_the_label_floors(tmp_path):
    t = tmp_path / "r.jsonl"
    t.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": True}, "model_context_window": True}}}) + "\n")
    assert last_context(t, "codex")[0] is None
    t.write_text(_asst("m1", 1, inp=0, read=199_600, create=0))
    chip, ctx = context_chip({"agent": "claude_code", "session_id": "s", "transcript": str(t)})
    assert (chip, ctx.tokens) == ("context 199K", 199_600)       # under the warn line, so it is not rounded up to it
    t.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "model": "x" * 500, "usage": {"input_tokens": 5}}}) + "\n")
    assert len(last_context(t)[0].model) == 64


def test_the_cache_tells_a_replaced_same_size_file_apart(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text(_asst("m1", 1, read=1000))
    assert last_context(t)[0].tokens == 1053
    mtime = t.stat().st_mtime_ns
    other = tmp_path / "other.jsonl"
    other.write_text(_asst("m1", 1, read=2000))
    assert other.stat().st_size == t.stat().st_size
    os.utime(other, ns=(mtime, mtime))
    os.replace(other, t)
    assert last_context(t)[0].tokens == 2053


def test_a_codex_rollout_symlink_is_not_found_and_misses_back_off(tmp_path, monkeypatch):
    sid = "zq-sid-1"
    day = tmp_path / "sessions" / "2026" / "10" / "19"
    day.mkdir(parents=True)
    real = tmp_path / "real.jsonl"
    real.write_text("")
    (day / f"rollout-2026-10-19T00-00-00-{sid}.jsonl").symlink_to(real)
    rec = {"agent": "codex", "session_id": sid}
    env = {"CODEX_HOME": str(tmp_path)}
    assert find_transcript(rec, env)[0] is None
    clock = [1000.0]
    monkeypatch.setattr(session_context.time, "monotonic", lambda: clock[0])
    session_context._rollouts.clear()
    find_transcript(rec, env)
    clock[0] += 11
    find_transcript(rec, env)
    assert session_context._rollouts[(str(tmp_path), sid)][2] == 2
    clock[0] += 11                                              # inside the doubled window: no new walk
    find_transcript(rec, env)
    assert session_context._rollouts[(str(tmp_path), sid)][2] == 2
    (day / f"rollout-2026-10-19T00-00-00-{sid}.jsonl").unlink()
    (day / f"rollout-2026-10-19T00-00-00-{sid}.jsonl").write_text("")
    clock[0] += 400
    assert find_transcript(rec, env)[0] is not None


def test_spend_reads_stop_at_the_opened_size_and_report_what_they_left_out(tmp_path, monkeypatch):
    t = tmp_path / "s.jsonl"
    first = _asst("m1", 1)
    t.write_text(first + _asst("m2", 2))
    real = session_context._open_regular

    def snapshot(path):
        fd, st = real(path)
        return fd, types.SimpleNamespace(st_size=len(first.encode()))
    monkeypatch.setattr(session_context, "_open_regular", snapshot)
    assert [r["id"] for r in iter_usage(t)] == ["m1"]           # the line appended after the snapshot is not read
    monkeypatch.undo()
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    for i in range(3):
        (sub / f"a{i}.jsonl").write_text(_asst(f"s{i}", 1))
    monkeypatch.setattr(session_context, "_SUBAGENT_FILES", 2)
    stats: dict = {}
    assert len(list(iter_usage(t, include_subagents=True, stats=stats))) == 4
    assert stats["skipped_files"] == 1
    monkeypatch.setattr(session_context, "_SPEND_CAP", len(first.encode()) * 3)
    stats = {}
    list(iter_usage(t, include_subagents=True, stats=stats))
    assert stats["skipped_files"] >= 1


def test_a_recorded_transcript_over_the_length_cap_or_with_a_nul_is_not_trusted(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    for bad in ("/" + "a" * 1100 + ".jsonl", "/t/a\0b.jsonl"):
        path, _why = find_transcript({"agent": "claude_code", "session_id": "s", "cwd": "/w", "transcript": bad}, env)
        assert path == tmp_path / "projects" / "-w" / "s.jsonl"


# --- tokens today over a busy session's subagent lanes ----------------------------------------------------------

def _stamped(mid: str, out: int, at: str) -> str:
    return json.dumps({"type": "assistant", "timestamp": at, "message": {"id": mid, "usage": {
        "input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": out}}}) + "\n"


def _busy_session(tmp_path, today_names):
    """A transcript with 70 subagent lanes; those in ``today_names`` carry 10 tokens stamped today, the rest are
    old (stamp and mtime)."""
    from datetime import datetime, timezone
    since = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t = tmp_path / "s.jsonl"
    t.write_text(_stamped("main", 1, "2026-01-01T10:00:00Z"))
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    old = since.timestamp() - 86400
    for i in range(70):
        name = f"agent-{i:03d}.jsonl"
        f = sub / name
        if name in today_names:
            f.write_text(_stamped(f"m{i}", 10, "2026-01-02T09:00:00Z"))
        else:
            f.write_text(_stamped(f"m{i}", 10, "2026-01-01T09:00:00Z"))
            os.utime(f, (old, old))
    return t, since


def test_tokens_today_counts_every_subagent_lane_not_the_first_64_by_name(tmp_path):
    late = {f"agent-{i:03d}.jsonl" for i in range(66, 70)}      # sort after position 64
    t, since = _busy_session(tmp_path, late)
    assert session_context.tokens_since(t, "claude_code", since) == (40, "")
    assert session_context.tokens_since_detail(t, "claude_code", since) == (40, "", 0)


def test_a_subagent_file_last_written_before_since_is_not_read(tmp_path, monkeypatch):
    t, since = _busy_session(tmp_path, {"agent-069.jsonl"})
    opened = []
    real = session_context._lines_of
    monkeypatch.setattr(session_context, "_lines_of", lambda f, b: (opened.append(f.name), real(f, b))[1])
    assert session_context.tokens_since_detail(t, "claude_code", since)[0] == 10
    assert sorted(opened) == ["agent-069.jsonl", "s.jsonl"]


def test_an_exhausted_budget_says_how_many_lanes_it_left_out(tmp_path, monkeypatch):
    late = {f"agent-{i:03d}.jsonl" for i in range(60, 70)}
    t, since = _busy_session(tmp_path, late)
    one = (tmp_path / "s" / "subagents" / "agent-069.jsonl").stat().st_size
    monkeypatch.setattr(session_context, "_SPEND_CAP", t.stat().st_size + 3 * one)
    got, why, skipped = session_context.tokens_since_detail(t, "claude_code", since)
    assert (got, why, skipped) == (30, "", 7)


def _bump(f, text, when=None):
    with open(f, "a") as fh:
        fh.write(text)
    if when:
        os.utime(f, (when, when))


def test_a_lane_appending_while_the_parent_is_idle_is_read_again(tmp_path):
    t, since = _busy_session(tmp_path, {"agent-069.jsonl"})
    first = session_context.tokens_since_detail(t, "claude_code", since)[0]
    lane = tmp_path / "s" / "subagents" / "agent-069.jsonl"
    _bump(lane, _stamped("late", 100, "2026-01-02T10:00:00Z"))
    assert session_context.tokens_since_detail(t, "claude_code", since)[0] == first + 100


def test_a_parent_written_before_since_still_reads_a_lane_written_after(tmp_path):
    t, since = _busy_session(tmp_path, set())
    old = since.timestamp() - 3600
    os.utime(t, (old, old))
    assert session_context.tokens_since_detail(t, "claude_code", since)[0] == 0
    lane = tmp_path / "s" / "subagents" / "agent-000.jsonl"
    _bump(lane, _stamped("new", 7, "2026-01-02T10:00:00Z"), since.timestamp() + 60)
    assert session_context.tokens_since_detail(t, "claude_code", since)[0] == 7


def test_the_lane_count_is_bounded_and_the_rest_counted(tmp_path, monkeypatch):
    late = {f"agent-{i:03d}.jsonl" for i in range(60, 70)}
    t, since = _busy_session(tmp_path, late)
    monkeypatch.setattr(session_context, "_LANE_FILES", 4)
    opened = []
    real = session_context._lines_of
    monkeypatch.setattr(session_context, "_lines_of", lambda f, b: (opened.append(f.name), real(f, b))[1])
    got, why, skipped = session_context.tokens_since_detail(t, "claude_code", since)
    assert (got, why, skipped) == (40, "", 6)
    assert len(opened) == 5


def test_an_unlistable_lane_directory_counts_as_one_skipped(tmp_path, monkeypatch):
    t, since = _busy_session(tmp_path, set())
    def boom(*a, **k):
        raise PermissionError("no")
    monkeypatch.setattr(session_context.os, "scandir", boom)
    assert session_context.tokens_since_detail(t, "claude_code", since)[2] == 1


def test_a_lane_that_will_not_stat_counts_as_skipped(tmp_path, monkeypatch):
    t, since = _busy_session(tmp_path, {"agent-069.jsonl"})
    class E:
        def __init__(self, e): self._e, self.name = e, e.name
        def stat(self):
            if self.name == "agent-069.jsonl":
                raise FileNotFoundError("gone")
            return self._e.stat()
    real_scandir = os.scandir
    class S:
        def __init__(self, p): self._c = real_scandir(p)
        def __enter__(self): return (E(e) for e in self._c.__enter__())
        def __exit__(self, *a): return self._c.__exit__(*a)
    monkeypatch.setattr(session_context.os, "scandir", S)
    assert session_context.tokens_since_detail(t, "claude_code", since)[2] == 1
