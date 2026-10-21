"""The dispatch MCP door: initialize, tools/list (no sign tool, no human cancel), each tool over a real store and a real
git repo, the session read per call from ``_meta`` then the environment, and malformed calls answered as protocol
errors. One test runs the real server as a child process over stdio."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.dispatch import capability as caps
from levain.dispatch import cli as dcli
from levain.dispatch import mcp
from levain.dispatch.jobs import JobStore
from tests.test_dispatch_contract import POLICY, make_repo, registry, submission

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("dispatch-mcp-repo"))


@pytest.fixture
def ddir(tmp_path, monkeypatch):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dcli, "resolvers", lambda *a, **k: (registry(), POLICY))
    return d


def contract(repo, **over):
    path, base = repo
    return submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)), **over)


def call(ddir, name, arguments, *, agent="codex", meta=None, rid=1):
    params = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return mcp.handle(agent, {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params}, directory=ddir)


def test_initialize_and_tools_list(ddir):
    init = mcp.handle("codex", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18"}}, directory=ddir)
    assert init["result"]["serverInfo"]["name"] == "levain_dispatch"
    tools = mcp.handle("codex", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, directory=ddir)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"dispatch_file", "dispatch_show", "dispatch_list", "dispatch_recommend", "dispatch_cancel",
                     "dispatch_land"}
    assert not any("sign" in n for n in names)
    by = {t["name"]: t["description"] for t in tools}
    assert "must sign on their own device before anything runs" in by["dispatch_file"]
    assert "confers no authority" in by["dispatch_file"] and "confers no authority" in by["dispatch_recommend"]


def test_file_keeps_the_capability_in_the_session_named_in_meta(ddir, repo):
    reply = call(ddir, "dispatch_file", {"contract": contract(repo)}, meta={"threadId": "thread-7"})
    result = reply["result"]
    assert result["isError"] is False, result
    got = result["structuredContent"]["result"]
    assert caps.recall(ddir, "codex", "thread-7", got["id"]) == got["capability"]
    # a later call from the same thread cancels with no capability passed: the record supplies it
    out = call(ddir, "dispatch_cancel", {"job": got["id"]}, meta={"threadId": "thread-7"})["result"]
    assert out["isError"] is False and "as its planner" in out["content"][0]["text"]
    assert JobStore(ddir).get(got["id"]).history[-1].cause == "planner_cancel"


def test_the_session_falls_back_to_the_environment(ddir, repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session")
    got = call(ddir, "dispatch_file", {"contract": contract(repo)}, agent="claude_code")["result"]
    job = got["structuredContent"]["result"]
    assert caps.recall(ddir, "claude_code", "env-session", job["id"]) == job["capability"]


def test_no_session_keeps_no_record_and_another_session_cannot_cancel(ddir, repo):
    got = call(ddir, "dispatch_file", {"contract": contract(repo)})["result"]
    job = got["structuredContent"]["result"]
    assert "not kept" in job["capability_kept"] and not (ddir / "capabilities").exists()
    out = call(ddir, "dispatch_cancel", {"job": job["id"]}, meta={"threadId": "someone-else"})["result"]
    assert out["isError"] is True and "holds no capability" in out["content"][0]["text"]
    out = call(ddir, "dispatch_cancel", {"job": job["id"], "capability": "x" * 43})["result"]
    assert out["isError"] is True and "Refused" in out["content"][0]["text"]
    out = call(ddir, "dispatch_cancel", {"job": job["id"], "capability": job["capability"]})["result"]
    assert out["isError"] is False


def test_show_list_and_recommend(ddir, repo):
    job = call(ddir, "dispatch_file", {"contract": contract(repo)}, meta={"threadId": "t"})["result"][
        "structuredContent"]["result"]
    shown = call(ddir, "dispatch_show", {"job": job["id"]})["result"]
    assert shown["isError"] is False and "Fix the lint in a.py." in shown["content"][0]["text"]
    assert shown["structuredContent"]["result"]["digest"] == job["digest"]
    listed = call(ddir, "dispatch_list", {})["result"]
    assert [r["id"] for r in listed["structuredContent"]["result"]] == [job["id"]]
    rec = call(ddir, "dispatch_recommend", {"job": job["id"]}, meta={"threadId": "t"})["result"]
    assert rec["isError"] is True and "only for a verified job" in rec["content"][0]["text"]
    missing = call(ddir, "dispatch_show", {"job": "dj-" + "0" * 32})["result"]
    assert missing["isError"] is True


def test_a_refused_filing_is_the_tools_error_and_no_registry_says_so(ddir, repo, monkeypatch):
    out = call(ddir, "dispatch_file", {"contract": contract(repo, grant=["Bash"])})["result"]
    assert out["isError"] is True and out["content"][0]["text"].startswith("Refused")
    monkeypatch.setattr(dcli, "resolvers", lambda *a, **k: None)
    out = call(ddir, "dispatch_file", {"contract": contract(repo)})["result"]
    assert out["isError"] is True and "no template registry is set up" in out["content"][0]["text"]


def test_a_machine_fault_is_named_as_one(ddir):
    os.chmod(ddir, 0o755)
    out = call(ddir, "dispatch_list", {})["result"]
    assert out["isError"] is True and out["content"][0]["text"].startswith("This machine could not do it")


@pytest.mark.parametrize("msg, code", [
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dispatch_sign", "arguments": {}}},
     mcp.INVALID_PARAMS),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dispatch_show", "arguments": {}}},
     mcp.INVALID_PARAMS),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
      "params": {"name": "dispatch_show", "arguments": {"job": 7}}}, mcp.INVALID_PARAMS),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
      "params": {"name": "dispatch_cancel", "arguments": {"job": "x", "signature": "s"}}}, mcp.INVALID_PARAMS),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "nope"}, mcp.INVALID_PARAMS),
    ({"jsonrpc": "2.0", "id": 1, "method": "nope"}, mcp.METHOD_NOT_FOUND),
    ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, mcp.INVALID_REQUEST),
    ({"jsonrpc": "2.0", "id": True, "method": "ping"}, mcp.INVALID_REQUEST),
])
def test_malformed_calls_are_protocol_errors(ddir, msg, code):
    assert mcp.handle("codex", msg, directory=ddir)["error"]["code"] == code


def test_serve_answers_a_parse_error_and_skips_notifications(ddir):
    stdin = io.StringIO('{not json\n{"jsonrpc": "2.0", "method": "notifications/initialized"}\n'
                        '{"jsonrpc": "2.0", "id": 3, "method": "ping"}\n')
    stdout = io.StringIO()
    mcp.serve("codex", stdin, stdout, directory=ddir)
    lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == mcp.PARSE_ERROR and lines[1] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_the_real_server_over_stdio(ddir):
    from levain.launch import child_env

    env = child_env()
    env["PYTHONPATH"] = str(ROOT)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "dispatch_list", "arguments": {"all": True}}}]
    cp = subprocess.run([sys.executable, "-P", "-m", "levain.dispatch.mcp", "--agent", "codex", "--dispatch-dir",
                         str(ddir)], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True,
                        text=True, timeout=60, env=env, cwd=str(ROOT))
    assert cp.returncode == 0, cp.stderr
    replies = [json.loads(x) for x in cp.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[2]["result"]["isError"] is False and replies[2]["result"]["content"][0]["text"] == "No jobs."


# --- fix round 1 ---------------------------------------------------------------------------------------------------

def test_show_puts_the_state_first_and_labels_the_goal_unverified(ddir, repo):
    # L2 Q4: a planner read another filing's goal as if it were the door's own text
    job_id = call(ddir, "dispatch_file", {"contract": contract(repo)}, meta={"threadId": "t-1"})["result"][
        "structuredContent"]["result"]["id"]
    text = call(ddir, "dispatch_show", {"job": job_id})["result"]["content"][0]["text"]
    lines = text.splitlines()
    assert lines[0] == "state: filed"
    assert "goal: the filer's text (unverified):" in text and "sent to the provider" not in text


def test_the_file_description_keeps_r2_and_claims_no_present_refusal():
    # L1 LOW: .capitalize() lowered "R2", and "filing refuses" goes false when the registry lands
    desc = next(t for t in mcp.TOOLS if t["name"] == "dispatch_file")["description"]
    assert "R2" in desc and "r2" not in desc
    assert "filing refuses" not in desc.lower() and "Filing needs a registered template" in desc


def test_a_value_error_from_below_is_a_fault(ddir, monkeypatch):
    def broken(self, **kw):
        raise ValueError("a malformed row")

    monkeypatch.setattr(JobStore, "list", broken)
    result = call(ddir, "dispatch_list", {})["result"]
    assert result["isError"] is True and result["content"][0]["text"].startswith("This machine could not do it "
                                                                                 "(ValueError)")


# --- F11-s2b: dispatch_land and the recommend's proposal -------------------------------------------------------------

@pytest.fixture
def lw(tmp_path, monkeypatch):
    from tests.test_dispatch_adopt import LandWorld

    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(var, raising=False)
    return LandWorld(tmp_path)


def test_dispatch_land_takes_no_human_item_and_no_data_root():
    tool = next(t for t in mcp.TOOLS if t["name"] == "dispatch_land")
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"job", "onto", "capability"} and schema["additionalProperties"] is False
    assert "never this call" in tool["description"] and "OPERATOR runs" in tool["description"]


def test_dispatch_land_lands_a_planners_job_by_the_rule(lw):
    job, cap = lw.verified()
    staged = lw.jstore.run_record(job.id, 0).staged_commit
    out = call(lw.ddir, "dispatch_land", {"job": job.id, "onto": "main"}, agent="claude_code",
               meta={"sessionId": "someone-else"})["result"]
    assert out["isError"] is True and "holds no capability" in out["content"][0]["text"]
    out = call(lw.ddir, "dispatch_land", {"job": job.id, "onto": "main", "capability": cap + "x"},
               agent="claude_code")["result"]
    assert out["isError"] is True and "not job" in out["content"][0]["text"]
    assert lw.jstore.get(job.id).state == "verified"
    out = call(lw.ddir, "dispatch_land", {"job": job.id, "onto": "main", "capability": cap}, agent="claude_code")[
        "result"]
    assert out["isError"] is False and out["structuredContent"]["result"]["outcome"] == "adopted", out
    assert lw.main() == staged


def test_dispatch_land_refused_by_the_floor_is_the_tools_error(lw):
    job, cap = lw.verified()
    out = call(lw.ddir, "dispatch_land", {"job": job.id, "onto": "work", "capability": cap}, agent="claude_code")[
        "result"]
    assert out["isError"] is True and out["content"][0]["text"].startswith("Not landed, refused: criterion onto")


def test_dispatch_land_onto_a_checked_out_branch_returns_the_operators_merge(lw, tmp_path):
    from tests.test_dispatch_adopt import git

    job, cap = lw.verified()
    git(lw.repo, "worktree", "add", "-q", str(tmp_path / "wt"), "main")
    out = call(lw.ddir, "dispatch_land", {"job": job.id, "onto": "main", "capability": cap}, agent="claude_code")[
        "result"]
    data = out["structuredContent"]["result"]
    assert out["isError"] is False and data["outcome"] == "merge" and data["command"].startswith("git -C ")
    assert "OPERATOR runs that merge" in out["content"][0]["text"]
    assert lw.jstore.get(job.id).state == "authorised"


def test_dispatch_recommend_carries_the_proposal_and_an_integer_is_never_a_boolean(lw):
    job, cap = lw.verified()
    out = call(lw.ddir, "dispatch_recommend", {"job": job.id, "capability": cap, "onto": "main", "run": True},
               agent="claude_code")
    assert out["error"]["code"] == mcp.INVALID_PARAMS and "run must be an integer" in out["error"]["message"]
    out = call(lw.ddir, "dispatch_recommend", {"job": job.id, "capability": cap, "onto": "levain/x"},
               agent="claude_code")["result"]
    assert out["isError"] is True and "under levain/" in out["content"][0]["text"]
    out = call(lw.ddir, "dispatch_recommend", {"job": job.id, "capability": cap, "onto": "main", "run": 1},
               agent="claude_code")["result"]
    assert out["isError"] is False and out["structuredContent"]["result"]["onto"] == "main"
    assert json.loads(lw.jstore.get(job.id).history[-1].evidence) == {"onto": "main", "run": 1}

