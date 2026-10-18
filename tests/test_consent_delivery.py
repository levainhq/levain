"""A late ruling's delivery back to its session (Q14 B4, :mod:`levain.consent.delivery`).

Built from a failure reproduced in the B4 run (project_memory/1018-q14b/b4): ``claude agents --json`` stopped listing a
``claude -p`` session while its process was still exiting, and with no process recorded the delivery took it for ended
and resumed it. A resume of a running session starts a copy (two agents on one tree), so the process recorded at filing
vetoes the resume while it exists; once it is gone the session is resumed, the text claimed once.
"""
from __future__ import annotations

import os
from datetime import timedelta

from levain.consent import delivery
from levain.consent.record import RULING_NAMESPACE, ruling_message
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures


def test_a_live_recorded_process_vetoes_the_resume_and_the_ruling_reaches_an_ended_session_once(
        queue, keys, tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "_exe", lambda name, env: f"/opt/fake/{name}")
    request = queue.file(agent="claude_code", agent_session_id="sess-b4", tool_name="Edit",
                         tool_input={"file_path": str(tmp_path / ".claude" / "settings.json")}, cwd=str(tmp_path),
                         hold=timedelta(minutes=5), tier="T3",
                         resume={"invocation": "-p", "permission_mode": "bypassPermissions", "cwd": str(tmp_path),
                                 "config_dir": None, "claude_pid": os.getpid()})
    assert queue.mark_undecided(request.id).gap == 1
    payload = queue.ruling_payload(request.id, "allow", keys["laptop"].fp)
    queue.answer_late(request.id, payload, keys["laptop"].sign(ruling_message(payload), RULING_NAMESPACE))
    launched = []

    def launch(argv, cwd, env, log):
        launched.append((argv, cwd))
        return None

    not_listed = lambda meta: {}                                            # noqa: E731
    # not listed, but the process recorded at filing still exists: never resumed, nothing claimed
    got = delivery.deliver(queue, request.id, listing=not_listed, launch=launch)
    assert got.route == "hook" and str(os.getpid()) in got.reason
    assert launched == [] and queue.ruling_delivered(request.id) is None
    # a listing that cannot be read is never "nothing is live"
    assert delivery.deliver(queue, request.id, listing=lambda meta: None, running=lambda pid: False,
                            launch=launch).route == "hook"
    assert launched == []
    # the process gone: ended, resumed with the ruling as its prompt, in its start directory and mode
    got = delivery.deliver(queue, request.id, listing=not_listed, running=lambda pid: False, launch=launch)
    assert got.route == "resumed" and got.rulings == (request.id,)
    (argv, cwd), = launched
    assert argv[:6] == ["/opt/fake/claude", "-p", "--resume", "sess-b4", "--permission-mode", "bypassPermissions"]
    assert argv[6].startswith("levain: ruling on gap #1: ALLOW.") and cwd == str(tmp_path)
    assert queue.ruling_delivered(request.id) is not None
    assert queue.resume_record(request.id)["outcome"] == "started"
    # claimed once: neither a second delivery nor the session's own next hook delivers it again
    assert delivery.deliver(queue, request.id, listing=not_listed, running=lambda pid: False,
                            launch=launch).route == "delivered"
    assert queue.claim_rulings_for("claude_code", "sess-b4") == [] and len(launched) == 1
