"""The unsigned record of what the agent's own dialog did with a request levain left to it.

Built from the runs in levain project_memory/1017-presence/AUTOAWAY_RUN.md, part 2 (real Claude Code 2.1.302 and
Codex 0.162.1, the real activation hook scripts and the real consent hook)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from levain.cli import main as cli
from levain.consent.dialog import observe
from levain.consent.listing import audit_line, read_listing
from levain.consent.queue import ConsentQueue, authority_source
from tests._h4 import h4_home

TEMPLATES = Path(__file__).resolve().parent.parent / "levain" / "templates"


def _queue(tmp_path: Path) -> ConsentQueue:
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    os.chmod(q, 0o700)
    return ConsentQueue(q, authority=authority_source(q, anchor=None), home=h4_home())


def _left(queue, agent, session, tool_name, tool_input, *, hold=60.0, withdraw=True):
    """A request levain filed and left to the agent's dialog (withdrawn at once, as with no grant and no hold)."""
    from datetime import timedelta
    r = queue.file(agent=agent, agent_session_id=session, tool_name=tool_name, tool_input=tool_input, cwd="/w",
                   hold=timedelta(seconds=hold))
    if withdraw:
        assert queue.withdraw(r.id, note="the hook stopped waiting; the native dialog stands")
    return r


def test_claude_code_yes_no_and_a_yes_while_held(tmp_path, capsys):
    # runs C1 (allow once -> ran), C2 (no -> nothing until the next prompt -> did_not_run), C3 (allow for the
    # session: the next call files no request, so nothing is recorded for it) and C4 (yes in the dialog while
    # the hook held it: ran, and the request stops being answerable)
    q = _queue(tmp_path)
    write = {"file_path": "/w/c1.txt", "content": "hi"}
    yes = _left(q, "claude_code", "s", "Write", write)
    time.sleep(0.6)  # a person's click, not the agent's own approval
    assert observe("claude_code", "PostToolUse", {"session_id": "s", "tool_name": "Write", "tool_input": write},
                   q.directory) == (yes.id, "ran")
    no = _left(q, "claude_code", "s", "Write", {"file_path": "/w/c2.txt", "content": "ho"})
    assert observe("claude_code", "Stop", {"session_id": "other"}, q.directory) == []  # another session's turn
    assert observe("claude_code", "PostToolUse", {"session_id": "s", "tool_name": "Write",
                                                  "tool_input": {"file_path": "/w/c4.txt", "content": "b"}},
                   q.directory) is None  # a call levain never filed
    assert observe("claude_code", "UserPromptSubmit", {"session_id": "s", "prompt": "next"}, q.directory) == [no.id]
    bash = {"command": "echo c5 > c5.txt", "description": "d"}
    held = _left(q, "claude_code", "s", "Bash", bash, withdraw=False)
    assert observe("claude_code", "PostToolUse", {"session_id": "s", "tool_name": "Bash", "tool_input": bash},
                   q.directory) == (held.id, "ran")
    assert q.get(held.id)[1] == "withdrawn" and q.dialog_outcome(yes.id)[0] == "ran"
    assert q.dialog_outcome(no.id)[0] == "did_not_run"
    # the listing's data and `levain consent pending` say it, as unsigned
    entries = {e.request.id: e for e in read_listing(q.directory, now=datetime.now(UTC)).recent}
    assert entries[no.id].dialog == "did_not_run"
    assert audit_line(entries[yes.id]) == ("withdrawn to the agent's own dialog; ran: answered yes in the agent's "
                                           "dialog, unsigned")
    assert cli(["consent", "pending", "--queue-dir", str(q.directory)]) == 0
    out = capsys.readouterr().out
    assert "left to the agent's own dialog (last 24 h, UNSIGNED: not answers levain verified):" in out
    assert f"{no.id}  claude_code  Write" in out and "did not run: no or abandoned in the agent's dialog, unsigned" in out


def test_codex_through_the_installed_hook_scripts(tmp_path):
    # runs X4-X6 through the real Codex template scripts (LEVAIN_HOME naming the queue): a yes after a person's
    # pause is ran; a call Codex holds a session approval for runs 0.0-0.1 s after levain leaves it: ran_unasked
    q = _queue(tmp_path)
    patch = {"command": "*** Begin Patch\n*** Add File: k1.txt\n+one\n*** End Patch\n"}
    hooks = TEMPLATES / "adapters" / "codex" / "activation" / "hooks"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "LEVAIN_HOME": str(tmp_path)}

    def run(script, payload):
        subprocess.run([sys.executable, str(hooks / script)], input=json.dumps(payload), text=True, env=env,
                       capture_output=True, timeout=60, check=True)

    asked = _left(q, "codex", "t", "apply_patch", patch)
    time.sleep(0.6)
    run("post_tool_use.py", {"hook_event_name": "PostToolUse", "session_id": "t", "tool_name": "apply_patch",
                             "tool_input": patch})
    unasked = _left(q, "codex", "t", "apply_patch", patch)
    assert observe("codex", "PostToolUse", {"session_id": "t", "tool_name": "apply_patch", "tool_input": patch},
                   q.directory) == (unasked.id, "ran_unasked")
    denied = _left(q, "codex", "t", "apply_patch", {"command": "*** Begin Patch\n*** Add File: k2.txt\n+two\n"
                                                               "*** End Patch\n"})
    run("user_prompt_submit.py", {"hook_event_name": "UserPromptSubmit", "session_id": "t", "prompt": "ok"})
    assert q.dialog_outcome(asked.id)[0] == "ran" and q.dialog_outcome(denied.id)[0] == "did_not_run"


def test_an_identical_retry_is_the_call_that_ran_not_the_declined_one(tmp_path):
    # run_h2 R1 (reproduced at the hook seam before the fix: the OLDEST identical request got ran_unasked and the
    # approved retry did_not_run): a first request declined in the dialog and left, then the same call retried
    # and approved. The PostToolUse is the retry's.
    q = _queue(tmp_path)
    write = {"file_path": "/w/r.txt", "content": "hi"}
    declined = _left(q, "claude_code", "s", "Write", write)
    time.sleep(0.01)
    approved = _left(q, "claude_code", "s", "Write", write)
    got = observe("claude_code", "PostToolUse", {"session_id": "s", "tool_name": "Write", "tool_input": write,
                                                 "tool_use_id": "toolu_B"}, q.directory)
    assert got is not None and got[0] == approved.id
    observe("claude_code", "Stop", {"session_id": "s"}, q.directory)
    assert q.dialog_outcome(approved.id)[0] in ("ran", "ran_unasked")
    assert q.dialog_outcome(declined.id)[0] == "did_not_run"


def test_a_still_pending_request_is_taken_before_a_newer_left_one(tmp_path):
    q = _queue(tmp_path)
    write = {"file_path": "/w/p.txt", "content": "hi"}
    held = _left(q, "claude_code", "s", "Write", write, withdraw=False)
    time.sleep(0.01)
    _left(q, "claude_code", "s", "Write", write)
    assert observe("claude_code", "PostToolUse", {"session_id": "s", "tool_name": "Write", "tool_input": write},
                   q.directory)[0] == held.id


def test_tool_use_ids_match_when_both_sides_carry_one(tmp_path):
    # a request filed with the id Claude Code sent matches the PostToolUse of that id, whichever is newer; a
    # request carrying another call's id is never taken; one carrying none stays eligible
    from datetime import timedelta
    q = _queue(tmp_path)
    write = {"file_path": "/w/i.txt", "content": "hi"}

    def filed(event_id):
        r = q.file(agent="claude_code", agent_session_id="s", tool_name="Write", tool_input=write, cwd="/w",
                   hold=timedelta(seconds=60), event_id=event_id)
        assert q.withdraw(r.id)
        time.sleep(0.01)
        return r
    first, second, bare = filed("toolu_1"), filed("toolu_2"), filed(None)
    post = {"session_id": "s", "tool_name": "Write", "tool_input": write}
    assert observe("claude_code", "PostToolUse", {**post, "tool_use_id": "toolu_1"}, q.directory)[0] == first.id
    assert observe("claude_code", "PostToolUse", {**post, "tool_use_id": "toolu_9"}, q.directory)[0] == bare.id
    assert observe("claude_code", "PostToolUse", {**post, "tool_use_id": "toolu_9"}, q.directory) is None
    assert observe("claude_code", "PostToolUse", post, q.directory)[0] == second.id
