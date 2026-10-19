from __future__ import annotations

import json
from pathlib import Path

from levain.cockpit.session_context import CONTEXT_WARN_TOKENS, context_chip, iter_usage, last_context


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
    assert [r["output"] for r in iter_usage(t)] == [5, 40]
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


def test_a_missing_or_symlinked_transcript_is_not_read(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text(_asst("m1", 1))
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert last_context(tmp_path / "none.jsonl")[0] is None and last_context(link)[0] is None
    chip, ctx = context_chip({"agent": "claude_code", "session_id": "s", "cwd": "/nowhere/x"}, {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert ctx is None and chip.startswith("context not read: ")
