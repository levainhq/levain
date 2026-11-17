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
    assert rb.decide_pre(_ev(p, offset=1, limit=600), env)
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
    assert h["PreToolUse"][0]["matcher"] == "Read"
    assert h["PostToolUse"][0]["hooks"][0]["command"] == (
        "/bin/sh -c '/usr/bin/python3 -I /run/read_budget.py post; c=$?; [ \"$c\" -eq 2 ] && exit 1; exit \"$c\"'")
    assert h["PreToolUse"][0]["hooks"][0]["command"] == (
        "/bin/sh -c '/usr/bin/python3 -I /run/read_budget.py pre; c=$?; [ \"$c\" -eq 2 ] && exit 1; exit \"$c\"'")
    assert h["PreToolUse"][0]["hooks"][0]["timeout"] == 10 == h["PostToolUse"][0]["hooks"][0]["timeout"]
    with pytest.raises(ValueError):
        rb.settings_hooks("python3", "/x.py")


# ---- review fixes (b1686fix): each test was run failing before its fix -----------------------------------------------
import re


def _retry(out):
    m = re.search(r"offset=(\d+) limit=(\d+)", out)
    return int(m.group(1)), int(m.group(2))


def test_file_over_scan_cap_prices_the_range_and_the_retry_passes(tmp_path, env):
    p = tmp_path / "huge.log"
    p.write_text(("x" * 99 + "\n") * 90_000)                       # 9MB, over SCAN_CAP
    assert p.stat().st_size > rb.SCAN_CAP
    assert rb.decide_pre(_ev(p, offset=5, limit=10), env) is None  # a ranged read is priced by its range
    out = rb.decide_pre(_ev(p), env)
    assert out and out.startswith(rb.DENY_PREFIX)
    off, lim = _retry(out)
    assert rb.decide_pre(_ev(p, offset=off, limit=lim), env) is None


def test_spent_ledger_never_wedges_a_tiny_read_and_the_retry_passes(tmp_path, env):
    p = _big(tmp_path)
    env["LEVAIN_READ_BUDGET_SESSION"] = "5000"
    post = {"tool_name": "Read", "session_id": "s1", "cwd": "/", "tool_input": {"file_path": str(p)},
            "tool_response": "x" * 4000}
    rb.decide_post(post, env)
    tiny = tmp_path / "tiny.txt"
    tiny.write_text("one line\n")
    assert rb.decide_pre(_ev(tiny), env) is None                  # under the floor: always passes
    assert rb.decide_pre(_ev(p, offset=1, limit=1), env) is None
    out = rb.decide_pre(_ev(p, offset=1, limit=300), env)         # over the floor, ledger spent: denied...
    assert out
    off, lim = _retry(out)
    assert rb.decide_pre(_ev(p, offset=off, limit=lim), env) is None  # ...and the named retry can pass


def test_images_and_binary_are_not_denied_with_an_outline(tmp_path, env):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x01" * 60_000)
    assert rb.decide_pre(_ev(png), env) is None
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 " + b"abc" * 60_000)
    assert rb.decide_pre(_ev(pdf), env) is None
    blob = tmp_path / "blob.dat"
    blob.write_bytes(b"abc\x00" * 60_000)
    assert rb.decide_pre(_ev(blob), env) is None
    post = {"tool_name": "Read", "session_id": "s1", "cwd": "/", "tool_input": {"file_path": str(png)},
            "tool_response": "y" * 400_000}
    rb.decide_post(post, env)
    assert rb._rows(env[rb.STATE_ENV], "s1")[0]["bytes"] <= rb.FLOOR_BYTES   # a fixed small charge


def test_retry_starts_at_the_first_section_and_a_scoped_retry_keeps_its_offset(tmp_path, env):
    p = tmp_path / "pre.py"
    p.write_text("# preamble\n" * 59 + "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(400)))
    out = rb.decide_pre(_ev(p), env)
    off, lim = _retry(out)
    assert off == 60 and rb.decide_pre(_ev(p, offset=off, limit=lim), env) is None
    out = rb.decide_pre(_ev(p, offset=500, limit=900), env)
    off, lim = _retry(out)
    assert off == 500 and rb.decide_pre(_ev(p, offset=off, limit=lim), env) is None


def test_front_dense_file_retry_is_sized_by_the_real_lines(tmp_path, env):
    p = tmp_path / "dense.txt"
    p.write_text(("d" * 399 + "\n") * 100 + "t\n" * 3000)
    out = rb.decide_pre(_ev(p), env)
    off, lim = _retry(out)
    assert rb.decide_pre(_ev(p, offset=off, limit=lim), env) is None


def test_one_huge_line_is_named_plainly(tmp_path, env):
    p = tmp_path / "min.js"
    p.write_text("var a=1;" * 20_000 + "\n")                      # one 160KB line
    out = rb.decide_pre(_ev(p), env)
    assert out and "Line 1 alone" in out and "desk" in out and "offset=" not in out


def test_allow_large_unscoped_is_priced_as_the_whole_file(tmp_path, env):
    env["LEVAIN_READ_BUDGET_CALL"] = "100000"
    p = tmp_path / "three.txt"
    p.write_text("123456789\n" * 3000)                            # first 2000 lines pass, the whole file does not
    assert rb.decide_pre(_ev(p), env) is None
    assert rb.decide_pre(_ev(p, allow_large=True), env)
    assert rb.decide_pre(_ev(p, allow_large=True, offset=1, limit=100), env) is None


def test_outline_counts_lines_like_read_and_caps_text(tmp_path):
    md = tmp_path / "ff.md"
    md.write_text("# A\n\x0c\nx y\n# B\n" + "# " + "t" * 500 + "\n")
    o = rb.outline(str(md))
    assert o["lines"] == 5 and [e[0] for e in o["entries"]] == [1, 4, 5]
    assert all(len(e[2]) <= 120 for e in o["entries"])
    js = tmp_path / "k.json"
    js.write_text(json.dumps({"k" * 500: 1}))
    assert all(len(e[2]) <= 120 for e in rb.outline(str(js))["entries"])


# ---- L3 round fixes (b1686fix2): each test was run failing before its fix ---------------------------------------------
def test_a_torn_or_non_utf8_ledger_line_keeps_the_good_rows(tmp_path, env):
    state = env[rb.STATE_ENV]
    rb.charge(state, "s1", tool="Read", path="/a", nbytes=100, remaining=5)
    with open(rb._ledger_path(state, "s1"), "ab") as fh:
        fh.write(b'{"t": 1, "pri\n\xff\xfe\xfd\n')
    rb.charge(state, "s1", tool="Read", path="/b", nbytes=200, remaining=5)
    rows = rb._rows(state, "s1")
    assert [r["path"] for r in rows] == ["/a", "/b"]


def test_post_charges_the_response_when_the_target_vanished(tmp_path, env):
    p = tmp_path / "gone.txt"
    p.write_text("x\n")
    post = {"tool_name": "Read", "session_id": "s1", "cwd": "/", "tool_input": {"file_path": str(p)},
            "tool_response": "z" * 3000}
    p.unlink()
    rb.decide_post(post, env)
    rows = rb._rows(env[rb.STATE_ENV], "s1")
    assert len(rows) == 1 and rows[0]["bytes"] == 3000


def test_empty_file_gets_a_plain_message_not_none_bytes(tmp_path, env):
    p = tmp_path / "empty.txt"
    p.write_text("")
    assert rb.decide_pre(_ev(p), env) is None                     # an empty file is free
    out = rb._deny_text(str(p), 0, 0, rb.FLOOR_BYTES, "x", 5, 7)
    assert "None" not in out and "empty" in out


def test_line_number_prefixes_are_priced(tmp_path, env):
    p = tmp_path / "blank.txt"
    p.write_text("\n" * 2000)                                     # 2000 raw bytes: under the floor without the prefixes
    assert rb.decide_pre(_ev(p), env)
    out = rb.decide_pre(_ev(p), env)
    off, lim = _retry(out)
    assert lim < 2000 and rb.decide_pre(_ev(p, offset=off, limit=lim), env) is None


def _run_hook(script, arg, stdin_text, extra_env):
    import os
    import subprocess
    import sys
    ev = {"pre": "PreToolUse", "post": "PostToolUse"}[arg]
    cmd = rb.settings_hooks(sys.executable, str(script))[ev][0]["hooks"][0]["command"]
    base = {k: v for k, v in os.environ.items() if k != rb.STATE_ENV}
    return subprocess.run(cmd, shell=True, input=stdin_text, capture_output=True, text=True,
                          env={**base, **extra_env}, timeout=30)


def test_the_hook_command_fails_open_when_the_script_is_missing(tmp_path):
    got = _run_hook(tmp_path / "gone" / "read_budget.py", "pre", "{}", {})
    assert got.returncode != 2 and got.stdout == ""


def test_the_hook_command_turns_a_script_exit_2_into_1(tmp_path):
    s = tmp_path / "two.py"
    s.write_text("import sys; sys.exit(2)\n")
    got = _run_hook(s, "pre", "{}", {})
    assert got.returncode == 1 and got.stdout == ""


def test_the_hook_command_runs_the_real_script_to_the_same_output_as_main(tmp_path, env):
    import pathlib
    big = _big(tmp_path)
    raw = json.dumps(_ev(big))
    out = io.StringIO()
    assert rb.main(["pre"], io.StringIO(raw), out, env) == 0 and out.getvalue()
    script = pathlib.Path(rb.__file__)
    got = _run_hook(script, "pre", raw, {rb.STATE_ENV: env[rb.STATE_ENV], "LEVAIN_READ_BUDGET_CALL": "20000",
                                         "LEVAIN_READ_BUDGET_TURNS": "10"})
    assert got.returncode == 0 and got.stdout == out.getvalue()
    # a tiny Read passes silently through the real command too
    tiny = tmp_path / "tiny.txt"
    tiny.write_text("hi\n")
    ok = _run_hook(script, "pre", json.dumps(_ev(tiny)), {rb.STATE_ENV: env[rb.STATE_ENV]})
    assert ok.returncode == 0 and ok.stdout == ""
