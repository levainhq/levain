"""A late ruling's delivery back to its session (Q14 B4, :mod:`levain.consent.delivery`).

Both tests are built from failures reproduced in B4 (project_memory/1018-q14b/b4): ``claude agents --json`` stopped
listing a ``claude -p`` session while its process was still exiting, and with no process recorded the delivery took
it for ended and resumed it (a copy: two agents on one tree); and a resume that did not start left its ruling claimed,
so neither ``levain consent deliver`` nor the session's next hook could ever deliver it.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import timedelta

from levain.consent import delivery
from levain.consent import delivery as dv
from levain.consent.adapters.claude_code import GATE_TIMEOUT
from levain.consent.record import RULING_NAMESPACE, ruling_message
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures

STARTED = "Sun Oct 18 14:17:56 2026"


def _ended_session_ruled(queue, keys, tmp_path, decision):
    """A ruled request of a Claude Code ``-p`` session whose resume inputs all hold: a UUID id, its transcript under
    its config directory, levain's gate in its project settings, a clean recorded command line."""
    session, cwd, cfg = str(uuid.uuid4()), tmp_path / "proj", tmp_path / "cfg"
    script = cwd / "activation" / "hooks" / "consent_claude_code.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    (cwd / ".claude").mkdir()
    gate = {"type": "command", "command": f"/usr/bin/python3 -EP -m levain.hookrun {script}",
            "timeout": GATE_TIMEOUT, "onFailure": "block"}      # the canonical gate entry
    (cwd / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*",
                                                                                          "hooks": [gate]}]}}))
    transcript = cfg / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    request = queue.file(agent="claude_code", agent_session_id=session, tool_name="Edit",
                         tool_input={"file_path": str(cwd / ".claude" / "settings.json")}, cwd=str(cwd),
                         hold=timedelta(minutes=5), tier="T3",
                         resume={"invocation": "-p", "permission_mode": "bypassPermissions", "cwd": str(cwd),
                                 "config_dir": str(cfg), "claude_pid": os.getpid(), "claude_started": STARTED,
                                 "claude_flags": "claude -p --dangerously-skip-permissions --model"})
    assert queue.mark_undecided(request.id).gap == 1
    payload = queue.ruling_payload(request.id, decision, keys["laptop"].fp)
    queue.answer_late(request.id, payload, keys["laptop"].sign(ruling_message(payload), RULING_NAMESPACE))
    return request, session, cwd


def gone(pid, started=None):
    return False


def test_a_live_recorded_process_vetoes_the_resume_and_the_ruling_reaches_an_ended_session_once(
        queue, keys, tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "_exe", lambda name, env: f"/opt/fake/{name}")
    request, session, cwd = _ended_session_ruled(queue, keys, tmp_path, "allow")
    launched = []

    def launch(argv, cwd, env, log):
        launched.append((argv, cwd))
        return None

    not_listed = lambda meta: {}                                            # noqa: E731
    # not listed, but the process recorded at filing still exists: never resumed, nothing claimed
    got = delivery.deliver(queue, request.id, listing=not_listed, running=lambda pid, started=None: True,
                           launch=launch)
    assert got.route == "hook" and str(os.getpid()) in got.reason
    assert launched == [] and queue.ruling_delivered(request.id) is None
    # a listing that cannot be read is never "nothing is live"
    assert delivery.deliver(queue, request.id, listing=lambda meta: None, running=gone, launch=launch).route == "hook"
    assert launched == []
    # the process gone: ended, resumed with the ruling as its prompt, in its start directory and mode
    got = delivery.deliver(queue, request.id, listing=not_listed, running=gone, launch=launch)
    assert got.route == "resumed" and got.rulings == (request.id,)
    (argv, at), = launched
    assert argv[:5] == ["/opt/fake/claude", "-p", f"--resume={session}", "--permission-mode", "bypassPermissions"]
    assert argv[5].startswith("levain: ruling on gap #1: ALLOW. Do «Edit of ") and at == str(cwd)
    assert queue.ruling_delivered(request.id) is not None
    assert [s["outcome"] for s in queue.resume_steps(request.id)] == ["launching", "started"]
    # claimed once: neither a second delivery nor the session's own next hook delivers it again
    assert delivery.deliver(queue, request.id, listing=not_listed, running=gone, launch=launch).route == "delivered"
    assert queue.claim_rulings_for("claude_code", session) == [] and len(launched) == 1


def test_a_resume_that_did_not_start_leaves_its_ruling_deliverable_once_and_an_unsettled_one_unconfirmed(
        queue, keys, tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "_exe", lambda name, env: f"/opt/fake/{name}")
    request, session, _ = _ended_session_ruled(queue, keys, tmp_path, "deny")
    launched = []

    def failing(argv, cwd, env, log):
        launched.append(argv)
        return "it exited 1 within 10 s"

    got = delivery.deliver(queue, request.id, listing=lambda meta: {}, running=gone, launch=failing)
    assert got.route == "failed" and len(launched) == 1
    assert queue.delivery_state(request.id)[0] == "failed" and queue.ruling_delivered(request.id) is None
    # still deliverable: `levain consent deliver` finds it, and the session's next hook gets the text, once
    assert [g.request.id for g in queue.undelivered_rulings()] == [request.id]
    assert [r.request_id for r in queue.claim_rulings_for("claude_code", session)] == [request.id]
    assert queue.claim_rulings_for("claude_code", session) == [] and queue.undelivered_rulings() == []
    assert [s["outcome"] for s in queue.resume_steps(request.id)] == ["launching", "failed", "hook"]
    assert delivery.deliver(queue, request.id, listing=lambda meta: {}, running=gone,
                            launch=failing).route == "delivered" and len(launched) == 1
    # a lease nobody settled (the deliverer died after the claim): unconfirmed, never delivered, never retried
    other, other_session, _ = _ended_session_ruled(queue, keys, tmp_path / "b", "deny")
    assert [r.request_id for r in queue.lease_rulings_for("claude_code", other_session)] == [other.id]
    assert queue.delivery_state(other.id)[0] == "unconfirmed" and queue.ruling_delivered(other.id) is None
    assert queue.claim_rulings_for("claude_code", other_session) == []
    assert delivery.deliver(queue, other.id, listing=lambda meta: {}, running=gone,
                            launch=failing).route == "unconfirmed" and len(launched) == 1



# ---- a Codex resume never runs looser than the session did (1019+1, measured: `codex exec resume` takes no -s and
# runs under config.toml's sandbox_mode, so an exec started -s read-only was resumed workspace-write) -------------

_THREAD = "01a152a3-b935-79e0-aa02-3c71635e0a03"


def _rollout(chome, modes, thread=_THREAD, tail=""):
    day = chome / "sessions" / "2026" / "10" / "19"
    day.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": "/w"}})]
    lines += [json.dumps({"type": "turn_context", "payload": {"sandbox_policy": m if isinstance(m, dict) else
                                                              {"type": m}}}) for m in modes]
    (day / f"rollout-2026-10-19T01-30-24-{thread}.jsonl").write_text("\n".join(lines) + "\n" + tail,
                                                                     encoding="utf-8")


def test_a_codex_resume_restores_the_sandbox_its_rollout_last_ran_under(tmp_path, monkeypatch):
    chome = tmp_path / "codex"
    _rollout(chome, ["workspace-write", "read-only"])
    monkeypatch.setattr(dv, "_exe", lambda name, env: f"/bin/{name}")
    meta = {"invocation": "-p", "cwd": str(tmp_path), "config_dir": str(chome),
            "codex_flags": "codex -s --skip-git-repo-check --strict-config --json"}
    assert dv.resume_refusal("codex", _THREAD, meta) is None
    assert dv._resume_argv("codex", _THREAD, meta, {}) == [
        "/bin/codex", "-s", "read-only", "exec", "resume", "--skip-git-repo-check", "--strict-config", _THREAD]
    # workspace-write: every recorded field is reproduced over whatever config.toml says now (L3 r1 codex HIGH)
    _rollout(chome, [{"type": "workspace-write", "writable_roots": ["/x/extra"], "network_access": False,
                      "exclude_tmpdir_env_var": True, "exclude_slash_tmp": False}])
    assert dv._resume_argv("codex", _THREAD, {**meta, "codex_flags": "codex"}, {}) == [
        "/bin/codex", "-s", "workspace-write", "exec", "resume",
        "-c", 'sandbox_workspace_write.writable_roots=["/x/extra"]', "-c", "sandbox_workspace_write.network_access=false",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true", "-c", "sandbox_workspace_write.exclude_slash_tmp=false",
        _THREAD]


def test_the_newest_turn_context_decides_and_a_field_that_cannot_be_reproduced_refuses(tmp_path):
    """L3 r1 codex HIGH: a damaged newest record never lets an older (looser) one decide."""
    chome = tmp_path / "codex"
    meta = {"config_dir": str(chome)}
    _rollout(chome, ["danger-full-access"], tail='{"type": "turn_context", "payload": {"sandbox_pol')
    assert dv.codex_sandbox_args(meta, _THREAD) is None
    _rollout(chome, ["danger-full-access", "read-only"],
             tail=json.dumps({"type": "event_msg", "payload": {"kind": "turn_context"}}) + "\n")
    assert dv.codex_sandbox_args(meta, _THREAD) == ["-s", "read-only"]      # a mention is not a record
    _rollout(chome, [{"type": "workspace-write", "network_access": False, "a_field_codex_adds": 1}])
    assert dv.codex_sandbox_args(meta, _THREAD) is None
    _rollout(chome, [{"type": "read-only", "network_access": True}])
    assert dv.codex_sandbox_args(meta, _THREAD) is None
    _rollout(chome, [{"type": "workspace-write", "writable_roots": ["relative/root"]}])
    assert dv.codex_sandbox_args(meta, _THREAD) is None


def test_a_codex_resume_is_refused_when_its_sandbox_cannot_be_read_or_an_option_cannot_be_restored(tmp_path,
                                                                                                    monkeypatch):
    chome = tmp_path / "codex"
    monkeypatch.setattr(dv, "_exe", lambda name, env: f"/bin/{name}")
    base = {"invocation": "-p", "cwd": str(tmp_path), "config_dir": str(chome), "codex_flags": "codex"}
    assert "could not be read from its rollout" in dv.resume_refusal("codex", _THREAD, base)     # no rollout
    assert dv._resume_argv("codex", _THREAD, base, {}) is None
    _rollout(chome, ["a-mode-codex-adds-later"])
    assert "could not be read from its rollout" in dv.resume_refusal("codex", _THREAD, base)
    _rollout(chome, ["read-only"])
    assert dv.resume_refusal("codex", _THREAD, base) is None
    for flags, named in (("codex -c", "-c"), ("codex --add-dir", "--add-dir"),
                         ("codex --dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass"),
                         ("codex --profile", "--profile"), ("codex --some-later-flag", "--some-later-flag")):
        assert named in dv.resume_refusal("codex", _THREAD, {**base, "codex_flags": flags}), flags
    assert "not recorded" in dv.resume_refusal("codex", _THREAD, {k: v for k, v in base.items() if k != "codex_flags"})


def test_the_tail_read_spans_blocks_and_finds_the_newest_record(tmp_path):
    chome = tmp_path / "codex"
    filler = "".join(json.dumps({"type": "event_msg", "payload": {"text": "x" * 500}}) + "\n" for _ in range(400))
    _rollout(chome, ["danger-full-access", "read-only"], tail=filler)
    assert dv.codex_sandbox_args({"config_dir": str(chome)}, _THREAD) == ["-s", "read-only"]
    big = json.dumps({"type": "turn_context", "payload": {"sandbox_policy": {"type": "workspace-write"},
                                                         "note": "y" * 200_000}})
    _rollout(chome, ["danger-full-access"], tail=big + "\n" + filler)
    assert dv.codex_sandbox_args({"config_dir": str(chome)}, _THREAD) == ["-s", "workspace-write"]
