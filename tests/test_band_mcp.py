"""The question band's MCP door (QMCP-1): ``levain.consent.band_mcp`` over a real pipe, and the agent process the resume
meta names when the caller is an MCP server (its parent), never ``CLAUDE_PID``."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from levain.consent import band
from levain.consent.questions import QuestionStore

SESSION = "ceedbaa9-2e6d-433f-b374-0ad96dd5f62d"
OPTIONS = [{"key": "a", "label": "merge now"}, {"key": "b", "label": "wait for the run"}]
ARGS = {"what": "merge the branch?", "options": OPTIONS, "rec": "a", "blocks": "the merge"}


def _talk(tmp_path: Path, lines: list[str], agent: str = "claude_code", env: dict[str, str | None] | None = None):
    """Send ``lines`` to a server on a pipe, close its stdin, and return (replies, exit code)."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "LEVAIN"))}
    base.update({"LEVAIN_HOME": str(tmp_path / "home"), "CLAUDE_CODE_SESSION_ID": SESSION})
    for k, v in (env or {}).items():
        base.pop(k, None)
        if v is not None:
            base[k] = v
    proc = subprocess.run([sys.executable, "-P", "-m", "levain.consent.band_mcp", "--agent", agent],
                          input="".join(line + "\n" for line in lines), capture_output=True, text=True, env=base,
                          timeout=60, cwd=str(tmp_path))
    return [json.loads(line) for line in proc.stdout.splitlines()], proc.returncode


def _req(rid, method, params=None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, **({} if params is None else {"params": params})})


def _store(tmp_path: Path) -> QuestionStore:
    return QuestionStore(tmp_path / "home" / "consent")


def test_a_conversation_over_a_pipe_files_the_question_with_its_session(tmp_path):
    replies, code = _talk(tmp_path, [
        _req(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        _req(2, "tools/list"),
        _req(3, "tools/call", {"name": "ask", "arguments": ARGS}),
        _req(4, "ping"),
    ])
    assert code == 0
    assert [r["id"] for r in replies] == [1, 2, 3, 4]              # the notification got no reply
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    tool = replies[1]["result"]["tools"][0]
    assert tool["name"] == "ask" and tool["inputSchema"]["required"] == ["what", "blocks"]
    call = replies[2]["result"]
    assert call["isError"] is False
    text = call["content"][0]["text"]
    assert text.startswith("levain ask: question #1 filed (") and "the answer arrives in your context" in text
    assert replies[3]["result"] == {}
    (q,) = _store(tmp_path).open_questions()
    assert (q.agent, q.agent_session_id, q.what, q.rec, q.blocks) == \
        ("claude_code", SESSION, "merge the branch?", "a", "the merge")
    assert list(q.options) == [("a", "merge now"), ("b", "wait for the run")]


def test_a_refusal_is_a_tool_error_with_the_same_words_as_levain_ask(tmp_path):
    one_option = {**ARGS, "options": OPTIONS[:1]}
    replies, _ = _talk(tmp_path, [_req(1, "tools/call", {"name": "ask", "arguments": one_option})])
    result = replies[0]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"].startswith("levain ask: refused, nothing was filed: ")
    assert not _store(tmp_path).open_questions()


def test_a_codex_server_with_no_thread_names_no_session_and_says_so(tmp_path):
    replies, _ = _talk(tmp_path, [_req(1, "tools/call", {"name": "ask", "arguments": ARGS})], agent="codex",
                       env={"CLAUDE_CODE_SESSION_ID": None})
    assert replies[0]["result"]["isError"] is False
    assert "recorded but not delivered to you" in replies[0]["result"]["content"][0]["text"]
    (q,) = _store(tmp_path).open_questions()
    assert (q.agent, q.agent_session_id) == ("codex", band.NO_SESSION)


def test_unknown_methods_malformed_lines_and_bad_params_are_errors_never_a_crash(tmp_path):
    replies, code = _talk(tmp_path, [
        _req(1, "server/discover"),
        "{not json",
        "[1, 2]",
        _req(2, "tools/call", {"name": "ask", "arguments": {"what": "x"}}),
        _req(3, "tools/call", {"name": "other", "arguments": {}}),
        _req(4, "tools/call", ["nope"]),
        _req(5, "ping"),
    ])
    assert code == 0
    assert replies[0]["error"]["code"] == -32601 and replies[0]["id"] == 1
    assert replies[1]["error"]["code"] == -32700 and replies[1]["id"] is None
    assert replies[2]["error"]["code"] == -32600
    assert [r["error"]["code"] for r in replies[3:6]] == [-32602, -32602, -32602]
    assert replies[6]["result"] == {}                                 # the server went on after every one of them


def test_eof_on_stdin_is_a_clean_exit(tmp_path):
    replies, code = _talk(tmp_path, [])
    assert (replies, code) == ([], 0)


def test_an_mcp_servers_agent_process_is_its_parent_never_claude_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setenv("CLAUDE_PID", "4242")                          # inherited from an outer claude
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))
    # the server's nearest ancestor is the claude that started it: the pid the meta names is that one's
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(os.getpid(), ["claude", "-p"]), (1111, ["zsh"])]))
    assert band.ask_resume_meta("claude_code", SESSION)["claude_pid"] == os.getpid()


# ---- L1 fix lane (1018+51): nesting, line separators, keys, the subagent refusal's words ------------------------

def test_nesting_deep_enough_to_overflow_the_decoder_is_a_parse_error_and_the_server_goes_on(tmp_path):
    replies, code = _talk(tmp_path, ["[" * 200000, _req(1, "ping")])
    assert code == 0
    assert replies[0]["error"]["code"] == -32700 and replies[0]["id"] is None
    assert replies[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def _raw_talk(tmp_path: Path, lines: list[str]) -> str:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "LEVAIN"))}
    base.update({"LEVAIN_HOME": str(tmp_path / "home"), "CLAUDE_CODE_SESSION_ID": SESSION})
    proc = subprocess.run([sys.executable, "-P", "-m", "levain.consent.band_mcp", "--agent", "claude_code"],
                          input="".join(x + "\n" for x in lines).encode("utf-8"), capture_output=True, env=base,
                          timeout=60, cwd=str(tmp_path))
    return proc.stdout.decode("utf-8")


def test_a_line_separator_in_the_question_round_trips_and_never_splits_the_reply(tmp_path):
    """The store refuses U+2028 in a question (one line of plain text), so it comes back as the refusal; other
    non-ASCII text files and comes back intact; and a separator a reply DOES echo (an unknown method's name) stays one
    line for any splitter, Python's str.splitlines included."""
    sep = "merge\u2028the\u2029branch\x85?"
    accented = "fusionner la branche d\u00e9j\u00e0 \u2014 maintenant?"
    method = "tools/\u2028list"
    raw = json.dumps({"jsonrpc": "2.0", "id": 3, "method": method}, ensure_ascii=False)
    out = _raw_talk(tmp_path, [
        _req(1, "tools/call", {"name": "ask", "arguments": {**ARGS, "what": sep}}),
        _req(2, "tools/call", {"name": "ask", "arguments": {**ARGS, "what": accented, "blocks": accented}}),
        raw,
    ])
    assert out.isascii() and len(out.splitlines()) == 3            # three replies, three lines, for any splitter
    r1, r2, r3 = (json.loads(x) for x in out.splitlines())
    assert r1["result"]["isError"] is True and "shows as itself" in r1["result"]["content"][0]["text"]
    assert r2["result"]["isError"] is False and accented in r2["result"]["content"][0]["text"]
    assert r3["error"]["message"] == f"method not found: {method}"
    (q,) = _store(tmp_path).open_questions()
    assert q.what == accented


def test_option_keys_are_stripped_as_levain_ask_strips_them(tmp_path):
    spaced = [{"key": " a ", "label": " merge now "}, {"key": "b\t", "label": "wait"}]
    replies, _ = _talk(tmp_path, [_req(1, "tools/call", {"name": "ask", "arguments": {**ARGS, "options": spaced}})])
    assert replies[0]["result"]["isError"] is False, replies
    (q,) = _store(tmp_path).open_questions()
    assert list(q.options) == band._options([" a = merge now ", "b\t=wait"]) == [("a", "merge now"), ("b", "wait")]


def test_a_subagent_session_refusal_names_no_flag_the_tool_cannot_pass(tmp_path):
    replies, _ = _talk(tmp_path, [_req(1, "tools/call", {"name": "ask", "arguments": ARGS})],
                       env={"CLAUDE_CODE_SESSION_ID": SESSION + "#agent:x1"})
    result = replies[0]["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert text.startswith("levain ask: refused") and "--session" not in text
    assert "the session in this server's environment must name the main session" in text


THREAD = "01a15281-3cff-7f23-807f-5c03bb8821a0"


def _call_with_meta(meta):
    return _req(1, "tools/call", {"name": "ask", "arguments": ARGS, "_meta": meta})


def test_a_codex_call_names_its_thread_from_meta_never_the_environment(tmp_path):
    """R-q19 amended: Codex sends the thread id only in params._meta (measured, codex-cli 0.162.1)."""
    meta = {"threadId": THREAD, "sessionId": THREAD, "progressToken": 0,
            "x-codex-turn-metadata": {"thread_id": THREAD, "turn_id": "t1"}}
    replies, _ = _talk(tmp_path, [_call_with_meta(meta)], agent="codex",
                       env={"CLAUDE_CODE_SESSION_ID": None, "CODEX_THREAD_ID": "stale-env-thread"})
    assert replies[0]["result"]["isError"] is False
    assert "the answer arrives in your context" in replies[0]["result"]["content"][0]["text"]
    (q,) = _store(tmp_path).open_questions()
    assert (q.agent, q.agent_session_id) == ("codex", THREAD)
    meta = _store(tmp_path).resume_meta(q.n)                        # the _meta thread is the caller's own
    assert meta is not None and meta["config_dir"] is None          # this test's env names no CODEX_HOME


def test_meta_session_id_is_read_when_no_thread_id_and_wins_over_a_stale_environment(tmp_path):
    replies, _ = _talk(tmp_path, [_call_with_meta({"sessionId": THREAD})])     # env names SESSION
    assert replies[0]["result"]["isError"] is False
    (q,) = _store(tmp_path).open_questions()
    assert q.agent_session_id == THREAD


def test_a_malformed_or_subagent_meta_session_falls_through_to_the_environment(tmp_path):
    bad = [{"threadId": ""}, {"threadId": 7}, {"threadId": "a\nb"}, {"threadId": "x" * 300},
           {"threadId": THREAD + "#agent:x1"}, {"threadId": band.NO_SESSION}, "not-a-dict"]
    replies, _ = _talk(tmp_path, [_req(i, "tools/call", {"name": "ask", "arguments": ARGS, "_meta": m})
                                  for i, m in enumerate(bad, 1)])
    assert all(r["result"]["isError"] is False for r in replies), replies
    assert {q.agent_session_id for q in _store(tmp_path).open_questions()} == {SESSION}
