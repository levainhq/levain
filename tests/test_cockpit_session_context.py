from __future__ import annotations

import json
from pathlib import Path

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
