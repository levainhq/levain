"""The BOUND classifier (levain.consent.bound): pure, built from strings measured in real sandboxed runs. Each test
names the evidence row it reproduces. No test here holds a real hook payload carrying a denial; that is the residue
the vendor-sandbox run discharges."""
from __future__ import annotations

import json

import pytest

from levain.consent.bound import (AGENT_SENTENCE, BOUNDS, CLASSES, MAX_DENIALS, MAX_SCAN_CHARS, BoundRecord,
                                  _touched, agent_sentence, classify, classify_payload)

CC_EPERM = "touch: /private/tmp/sbx1016.wG73lV/outside/root_control: Operation not permitted"
URL = {"url": "https://example.com", "prompt": "summarise"}
FETCH_OK = {"bytes": 577, "code": 200, "codeText": "OK", "result": "Example Domain", "durationMs": 1561,
            "url": "https://example.com"}


def cc_bash(stderr: str, *, stdout: str = "", active: bool | None = True, tool_input=None) -> BoundRecord:
    resp = {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False, "noOutputExpected": False}
    return classify("claude_code", "PostToolUse", "Bash", tool_input or {"command": "x"}, resp, bound_active=active)


def test_record_validates():
    for args in (("bogus", 0, ()), ("none", True, ()), ("touched", -1, ("other",)), ("touched", 1000, ("other",)),
                 ("touched", 1.0, ("other",)), ("touched", 1, ("nope",)), ("touched", 2, ("other", "network")),
                 ("touched", 2, ("other", "other")), ("touched", 0, ()), ("unknown", 0, ("other",)),
                 ("touched", 1, ["other"]), ("clean", 1, ())):
        with pytest.raises(ValueError):
            BoundRecord(*args)
    rec = BoundRecord("touched", 2, ("network", "other"))
    assert rec.as_dict() == {"bound": "touched", "denials": 2, "classes": ["network", "other"]}
    assert rec.classes_text() == "network,other"
    assert BoundRecord("none", 0, ()).classes_text() is None
    assert BoundRecord.from_columns(None, None, None) is None
    assert BoundRecord.from_columns("touched", 2, "network,other") == rec
    assert BOUNDS == ("none", "clean", "touched", "unknown") and "other" in CLASSES


def test_cc_masked_write_touched():  # row 1
    assert cc_bash(CC_EPERM) == BoundRecord("touched", 1, ("other",))


def test_cc_failure_error_text():  # row 1 + the Q8 failure shape
    rec = classify("claude_code", "PostToolUseFailure", "Bash", {"command": "x"},
                   error="Exit code 1\ntouch: /x/outside/f: Operation not permitted", bound_active=True)
    assert rec == BoundRecord("touched", 1, ("other",))


def test_codex_combined_output():  # row 2 + the codex fixture shape
    rec = classify("codex", "PostToolUse", "Bash", {"command": "x"}, "sandbox_apply: Operation not permitted\n",
                   bound_active=True)
    assert rec == BoundRecord("touched", 1, ("other",))
    assert classify("codex", "PostToolUse", "Bash", {"command": "x"}, "ledger-one\n", bound_active=True) \
        == BoundRecord("unknown", 0, ())


def test_linux_rofs():  # row 3
    assert cc_bash("touch: cannot touch '/tmp/s1ra_outside': Read-only file system") == BoundRecord(
        "touched", 1, ("other",))


def test_proxy_403_is_network_502_is_not():  # rows 4, 11
    assert cc_bash("curl: (56) CONNECT tunnel failed, response 403") == BoundRecord("touched", 1, ("network",))
    assert cc_bash("curl: (56) CONNECT tunnel failed, response 502") == BoundRecord("unknown", 0, ())


def test_lines_counted_once_classes_unioned():
    text = f"{CC_EPERM}\nread-only file system: Operation not permitted\ncurl: (56) CONNECT tunnel failed, response 403"
    assert cc_bash(text) == BoundRecord("touched", 3, ("network", "other"))


def test_go_lowercase_form():  # the precedent matcher in levain.session
    assert cc_bash("open /x/y: operation not permitted") == BoundRecord("touched", 1, ("other",))


def test_cc_stdout_phrase_is_not_a_denial():  # premise 5: Claude Code separates stdout from stderr
    assert cc_bash("", stdout="Operation not permitted") == BoundRecord("unknown", 0, ())


def test_shell_is_never_clean():
    assert cc_bash("", active=True) == BoundRecord("unknown", 0, ())
    assert classify("codex", "PostToolUse", "Bash", {}, "fine", bound_active=True).bound == "unknown"


def test_bound_inactive_is_none():
    assert cc_bash(CC_EPERM, active=False) == BoundRecord("none", 0, ())


def test_bound_active_none():
    assert cc_bash(CC_EPERM, active=None).bound == "touched"
    assert cc_bash("", active=None).bound == "unknown"
    assert classify("claude_code", "PostToolUse", "WebFetch", URL, FETCH_OK, bound_active=None).bound == "unknown"


def test_escape_ran_outside():
    esc = {"command": "x", "dangerouslyDisableSandbox": True}
    assert cc_bash(CC_EPERM, tool_input=esc) == BoundRecord("none", 0, ())
    assert cc_bash(CC_EPERM, tool_input={"command": "x", "dangerouslyDisableSandbox": False}).bound == "touched"


@pytest.mark.parametrize("agent,tool", [("claude_code", "Read"), ("claude_code", "Write"), ("claude_code", "Edit"),
                                        ("claude_code", "Grep"), ("claude_code", "mcp__srv__Bash"),
                                        ("codex", "WebFetch"), ("claude_code", "apply_patch")])
def test_out_of_scope_tools_are_none(agent, tool):
    rec = classify(agent, "PostToolUse", tool, {"url": "https://a.b"}, "Operation not permitted", bound_active=True)
    assert rec == BoundRecord("none", 0, ())


@pytest.mark.parametrize("text", ["proxy refused the connection", "Error: proxy refused the connection",
                                  "  proxy refused the connection\n"])
def test_webfetch_refused(text):  # row 5
    expect = BoundRecord("touched", 1, ("network",))
    assert classify("claude_code", "PostToolUseFailure", "WebFetch", URL, error=text, bound_active=True) == expect
    assert classify("claude_code", "PostToolUse", "WebFetch", URL, text, bound_active=None) == expect


def test_webfetch_refusal_is_an_exact_text():
    assert classify("claude_code", "PostToolUseFailure", "WebFetch", URL, error="see: proxy refused the connection, "
                    "retry", bound_active=True).bound == "unknown"
    page = dict(FETCH_OK, result="a page about why a proxy refused the connection")
    assert classify("claude_code", "PostToolUse", "WebFetch", URL, page, bound_active=True).bound == "clean"


def test_webfetch_admitted_clean():  # row 6
    assert classify("claude_code", "PostToolUse", "WebFetch", URL, FETCH_OK, bound_active=True) \
        == BoundRecord("clean", 0, ())


@pytest.mark.parametrize("inp", [{}, {"url": 5}, {"url": "http://localhost/"}, {"url": "https://intranet/"},
                                 {"url": "ftp://a.b/"}, {"url": "https://a.b/" + "x" * 3000}, "https://a.b", None,
                                 {"url": "http://[::1"}])
def test_webfetch_host_from_input(inp):
    rec = classify("claude_code", "PostToolUseFailure", "WebFetch", inp, error="proxy refused the connection",
                   bound_active=True)
    assert rec == BoundRecord("unknown", 0, ())


def test_record_never_holds_url_path_or_command():
    recs = [cc_bash(CC_EPERM, tool_input={"command": "touch /private/tmp/outside/x"}),
            classify("claude_code", "PostToolUseFailure", "WebFetch", URL, error="proxy refused the connection",
                     bound_active=True),
            classify("codex", "PostToolUse", "apply_patch", {"input": "*** /private/tmp/outside"},
                     "patch rejected: writing outside of the project; rejected", bound_active=True)]
    for rec in recs:
        assert rec.bound == "touched"
        blob = repr(rec) + json.dumps(rec.as_dict())
        for word in ("example.com", "/private/tmp", "tmp/outside", "touch ", "touch:"):  # "touched" / "write_outside_roots" are the record's own names
            assert word not in blob


def test_patch_rejected():  # row 7
    rec = classify("codex", "PostToolUse", "apply_patch", {"input": "p"},
                   "Script error:\npatch rejected: writing outside of the project; rejected by user approval settings",
                   bound_active=True)
    assert rec == BoundRecord("touched", 1, ("write_outside_roots",))


@pytest.mark.parametrize("line", ["ls: /Users/admin: Permission denied", "pgrep: Cannot get process list",
                                  "sysmon request failed with error: sysmond service not found",
                                  "<sandbox_violations>deny file-write-create</sandbox_violations>",
                                  "ls: /x: No such file or directory"])
def test_unmatched_rows_read_unknown(line):  # rows 8-10, 15
    assert cc_bash(line) == BoundRecord("unknown", 0, ())


def test_denials_capped_and_scan_bounded():
    assert cc_bash("Operation not permitted\n" * 5000).denials == MAX_DENIALS
    late = "x" * MAX_SCAN_CHARS + "\nOperation not permitted"
    assert cc_bash(late) == BoundRecord("unknown", 0, ())


def test_garbage_never_raises():
    for resp in (5, [1], None, b"Operation not permitted", {"stderr": 7}, {"stderr": b"x"}):
        for event in ("PostToolUse", "PostToolUseFailure", "Stop", None):
            assert isinstance(classify("claude_code", event, "Bash", "str", resp, resp, bound_active=True), BoundRecord)
            assert isinstance(classify("codex", event, "Bash", [1], resp, resp, bound_active=None), BoundRecord)
    assert isinstance(classify("openhands", "PostToolUse", "Bash", {}, "x", bound_active=True), BoundRecord)
    assert isinstance(classify("codex", "PostToolUse", None, {}, "x", bound_active=True), BoundRecord)
    assert isinstance(classify("codex", "PostToolUse", ["Bash"], {}, "x", bound_active=True), BoundRecord)
    assert isinstance(classify(None, None, {}, {}, bound_active=object()), BoundRecord)
    assert classify_payload("claude_code", [1], bound_active=True) is None
    assert classify_payload("claude_code", {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
                            bound_active=True) is None
    assert classify_payload("claude_code", {"hook_event_name": "Stop"}, bound_active=True) is None
    assert classify_payload("claude_code", {"hook_event_name": "PostToolUse", "tool_name": ""},
                            bound_active=True) is None


def test_classify_payload_shapes():
    cc = {"cwd": "/w", "duration_ms": 5, "effort": "x", "hook_event_name": "PostToolUse", "permission_mode": "default",
          "prompt_id": "p", "session_id": "s", "tool_input": {"command": "x"}, "tool_name": "Bash",
          "tool_response": {"stdout": "", "stderr": CC_EPERM}, "tool_use_id": "t", "transcript_path": "/t"}
    assert classify_payload("claude_code", cc, bound_active=True) == BoundRecord("touched", 1, ("other",))
    cx = {"session_id": "s", "turn_id": "u", "transcript_path": "/t", "cwd": "/w", "hook_event_name": "PostToolUse",
          "model": "m", "permission_mode": "default", "tool_name": "Bash", "tool_input": {"command": "x"},
          "tool_response": "sandbox_apply: Operation not permitted\n", "tool_use_id": "t"}
    assert classify_payload("codex", cx, bound_active=True) == BoundRecord("touched", 1, ("other",))
    fail = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {},
            "error": "Exit code 1\nOperation not permitted"}
    assert classify_payload("claude_code", fail, bound_active=True) == classify(
        "claude_code", "PostToolUseFailure", "Bash", {}, None, fail["error"], bound_active=True)


def test_agent_sentence():
    expect = ("levain: the sandbox refused part of this command (2 operation(s): network, other). An exit status of 0 "
              "does not mean every step ran; check that what you meant to happen did. If the step must run outside "
              "the sandbox, ask for it; it goes to the operator.")
    rec = _touched(2, ("other", "network"))
    assert agent_sentence(rec, "Bash") == expect
    assert AGENT_SENTENCE.format(n=2, classes="network, other") == expect
    assert agent_sentence(rec, "WebFetch") is None
    assert agent_sentence(rec, None) is None
    for other in (BoundRecord("unknown", 0, ()), BoundRecord("clean", 0, ()), BoundRecord("none", 0, ())):
        assert agent_sentence(other, "Bash") is None
