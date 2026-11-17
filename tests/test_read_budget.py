"""The position-aware read budget (``levain.desk.read_budget``, levain row 1686). The frame's cases as unit tests; the
recorded scratch-seat run under the real hook is NOT here (it is the row's UNDISCHARGED residue)."""
from __future__ import annotations

import io
import json

import pytest

from levain.desk import read_budget as rb


@pytest.fixture
def env(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    return {rb.STATE_ENV: str(state), "LEVAIN_READ_BUDGET_CALL": "20000", "LEVAIN_READ_BUDGET_TURNS": "10"}


def _big(tmp_path, name="big.py", defs=400):
    p = tmp_path / name
    p.write_text("".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(defs)))
    return p


def _ev(path, sid="s1", **ti):
    return {"hook_event_name": "PreToolUse", "tool_name": "Read", "session_id": sid, "cwd": "/",
            "tool_input": {"file_path": str(path), **ti}}


def test_price_is_tokens_times_remaining_turns():
    assert rb.price(4000, 10) == 10_000 and rb.price(1, 0) == 1 and rb.price(-5, 3) == 0


def test_large_unscoped_read_is_denied_with_outline_and_exact_retry(tmp_path, env):
    p = _big(tmp_path)
    out = rb.decide_pre(_ev(p), env)
    assert out and out.startswith(rb.DENY_PREFIX)
    assert "def f0" in out and "1-2  def f0" in out and "1200 lines" in out and f"{p.stat().st_size} bytes" in out
    # the retry's slice prices within the ceiling, and is itself allowed
    import re
    m = re.search(r"offset=(\d+) limit=(\d+)", out)
    off, lim = int(m.group(1)), int(m.group(2))
    assert off == 1 and lim >= 1
    assert rb.decide_pre(_ev(p, offset=off, limit=lim), env) is None


def test_scoped_read_is_allowed_and_small_file_is_allowed(tmp_path, env):
    p = _big(tmp_path)
    assert rb.decide_pre(_ev(p, offset=1, limit=20), env) is None
    s = tmp_path / "small.txt"
    s.write_text("hello\n")
    assert rb.decide_pre(_ev(s), env) is None


def test_allow_large_is_priced_not_an_escape(tmp_path, env):
    p = _big(tmp_path)
    assert rb.decide_pre(_ev(p, allow_large=True), env)


def test_ledger_charge_by_post_hook_and_session_budget_denies_a_later_scoped_read(tmp_path, env):
    p = _big(tmp_path)
    env["LEVAIN_READ_BUDGET_SESSION"] = "5000"
    post = {"tool_name": "Read", "session_id": "s1", "cwd": "/", "tool_input": {"file_path": str(p), "limit": 10},
            "tool_response": "x" * 4000}
    note = rb.decide_post(post, env)
    rows = rb._rows(env[rb.STATE_ENV], "s1")
    assert len(rows) == 1 and rows[0]["bytes"] == 4000 and rows[0]["price"] == rb.price(4000, 10)
    assert note is None                                   # 10_000 <= the 20_000 ceiling: no note
    # the session has 0 left: even a scoped read priced above what is left is denied; another session is untouched
    assert rb.decide_pre(_ev(p, offset=1, limit=200), env)
    assert rb.decide_pre(_ev(p, sid="other", offset=1, limit=20), env) is None


def test_heavy_post_charge_returns_a_note(tmp_path, env):
    p = _big(tmp_path)
    post = {"tool_name": "Read", "session_id": "s1", "cwd": "/", "tool_input": {"file_path": str(p)},
            "tool_response": {"file": {"content": "y" * 40_000}}}
    assert "token-turns" in rb.decide_post(post, env)


def test_granted_path_is_allowed_and_expiry_is_honoured(tmp_path, env):
    p = _big(tmp_path)
    (tmp_path / "state" / "grants.json").write_text(json.dumps({"paths": {str(p.resolve()): None}}))
    assert rb.decide_pre(_ev(p), env) is None
    (tmp_path / "state" / "grants.json").write_text(json.dumps({"paths": {str(p.resolve()): 5}}))
    assert rb.decide_pre(_ev(p), env, now=10)
    assert rb.decide_pre(_ev(p), env, now=1) is None       # before the expiry: granted


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"tool_name": "Read"}',
                                 '{"tool_name":"Read","session_id":"s","tool_input":{"file_path":7}}'])
def test_garbage_input_fails_open_silently(raw, env):
    out = io.StringIO()
    assert rb.main(["pre"], io.StringIO(raw), out, env) == 0 and out.getvalue() == ""
    assert rb.main(["post"], io.StringIO(raw), out, env) == 0 and out.getvalue() == ""


def test_no_state_dir_or_missing_file_is_inert(tmp_path, env):
    assert rb.decide_pre(_ev(tmp_path / "nope.py"), env) is None
    assert rb.decide_pre(_ev(_big(tmp_path)), {}) is None


def test_main_pre_prints_the_documented_deny_shape(tmp_path, env):
    out = io.StringIO()
    assert rb.main(["pre"], io.StringIO(json.dumps(_ev(_big(tmp_path)))), out, env) == 0
    h = json.loads(out.getvalue())["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
    assert h["permissionDecisionReason"].startswith(rb.DENY_PREFIX)


def test_outlines_markdown_json_and_unstructured(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# A\ntext\n## B\nmore\n```\n# not a head\n```\n# C\nend\n")
    o = rb.outline(str(md))
    assert o["kind"] == "markdown" and [e[:2] for e in o["entries"]] == [(1, 7), (3, 7), (8, 9)]
    js = tmp_path / "a.json"
    js.write_text('{"a": 1,\n "b": 2}\n')
    assert rb.outline(str(js))["kind"] == "json"
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n" + "x\n" * 30)
    o = rb.outline(str(bad))
    assert o["kind"] == "head" and len(o["entries"]) == 20
    # the same bytes give the same outline
    assert rb.outline(str(bad)) == o


def test_settings_hooks_shape_and_absolute_paths():
    h = rb.settings_hooks("/usr/bin/python3", "/run/read_budget.py")
    assert h["PreToolUse"][0]["matcher"] == "Read" and h["PostToolUse"][0]["hooks"][0]["command"].endswith(" post")
    with pytest.raises(ValueError):
        rb.settings_hooks("python3", "/x.py")
