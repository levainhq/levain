"""P7-2's VM driver (``scripts/vm_harness/p7_2_fence_driver.py``): the pure decision 'a tool call was refused with the fence's reason'
from a recorded transcript, the argument handling, and the exit codes. NO guest, NO vendor session, NO fence server runs here:
``main`` is driven with a fake facts probe and a fake run. The two rig tests build the real world (ssh-keygen links, the real agent
store) to read back what the driver's clock helpers stamp, and to check the revoke it appends is one the fence answers. The
process handling (the children's stop, the signal handlers) is run against real scratch processes that only sleep: no fence
server and no vendor session is started."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.consent.broker_client import SLOT_FENCED_TEXT
from scripts.vm_harness import p7_2_fence_driver as D

DRIVER = Path(D.__file__)
WHY = "the agent is barred"
REVOKED = "2026-11-20T10:00:00+00:00"
EARLY = "2026-11-20T09:59:50.250Z"
LATE = "2026-11-20T10:00:30.250Z"
CALL = "2026-11-20T09:59:40.000Z"          # a call asked of the gate before the revoke
CALL_LATE = "2026-11-20T10:00:10.000Z"     # a call asked of the gate after the revoke
ORIGINAL_ANSWERS = getattr(D, "socket_answers", None)


def _use(i, name="Bash", at=CALL):
    rec = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": {"command": "sleep 15"}}]}}
    if at is not None:
        rec["timestamp"] = at
    return rec


def _result(i, text, at=None, **extra):
    rec = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": text, **extra}]}}
    if at is not None:
        rec["timestamp"] = at
    return rec


def _fenced(i, at=LATE, why=WHY, **extra):
    return _result(i, SLOT_FENCED_TEXT.format(why=why), at, **{"is_error": True, **extra})


def _observe(*records, **kw):
    return D.observe_refusal(_lines(*records), revoked_at=REVOKED, **kw)


def _lines(*records):
    return [json.dumps(r) for r in records]


REFUSED = _lines(
    {"type": "user", "message": {"role": "user", "content": "Use the Bash tool to run sleep 15"}},
    _use(1), _result(1, "ok"),
    _use(2), _result(2, "ok"),
    _use(3), _fenced(3, is_error=True),
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "It was refused."}]}},
)
NOT_REFUSED = _lines(_use(1), _result(1, "ok"), _use(2), _result(2, "ok"),
                     {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}})


# --- the pure decision -------------------------------------------------------------------------------------------------

def test_a_transcript_with_a_refused_call_is_read_with_its_reason_and_what_ran_around_it():
    o = D.observe_refusal(REFUSED, revoked_at=REVOKED)
    assert (o.refused, o.reason, o.tool, o.tool_use_id) == (True, WHY, "Bash", "toolu_3")
    assert (o.ran_before, o.ran_after, o.problem, o.refused_at, o.after_revoke) == (2, 0, None, LATE, True)
    assert D.raw_line(o) == ("P7-2 refused=1 ran_before=2 ran_after_revoke=0 ran_unplaced=0 ran_after=0 "
                             "after_revoke=1")
    assert D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


def test_a_transcript_with_no_refused_call_is_not_a_pass():
    o = D.observe_refusal(NOT_REFUSED, revoked_at=REVOKED)
    assert (o.refused, o.reason, o.ran_before, o.ran_after) == (False, None, 2, 0)
    assert o.problem and "fence's words" in o.problem
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


def test_a_reason_that_holds_a_parenthesis_is_read_whole():
    why = "the occurrence left launched (now stopped)"
    o = _observe(_use(1), _result(1, "ok"), _use(2), _fenced(2, why=why))
    assert o.reason == why


def test_a_refusal_that_is_the_first_call_is_not_a_pass():
    o = _observe(_use(1), _fenced(1))
    assert (o.refused, o.ran_before) == (True, 0)
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0] and "no call stamped before the revoke" in D.diagnose(o)


def test_a_call_that_completes_after_the_refusal_is_not_a_pass():
    o = _observe(_use(1), _result(1, "ok"), _use(2), _fenced(2), _use(3), _result(3, "ok"))
    assert (o.refused, o.ran_before, o.ran_after) == (True, 1, 1)
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0] and "AFTER the refusal" in D.diagnose(o)


def test_the_models_own_quote_of_the_words_is_not_a_refused_call():
    quote = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": SLOT_FENCED_TEXT.format(why=WHY)}]}}
    o = _observe(_use(1), _result(1, "ok"), quote)
    assert o.refused is False


def test_a_refusal_kept_only_in_a_hook_record_counts_and_only_the_first_is_read():
    hook = {"type": "system", "subtype": "hook", "content": SLOT_FENCED_TEXT.format(why=WHY), "timestamp": LATE}
    o = _observe(_use(1), _result(1, "ok"), hook, hook)
    assert (o.refused, o.reason, o.tool_use_id, o.ran_before, o.ran_after, o.after_revoke) == (True, WHY, None, 1, 0, True)


def test_a_result_whose_content_is_a_list_of_text_parts_is_read():
    o = _observe(_use(1), _result(1, "ok"), _use(2),
                 _result(2, [{"type": "text", "text": SLOT_FENCED_TEXT.format(why=WHY)}], LATE, is_error=True))
    assert (o.refused, o.reason) == (True, WHY)


def test_a_result_that_is_an_error_is_not_a_call_that_ran():
    o = _observe(_use(1), _result(1, "ok"), _use(2), _result(2, "boom", is_error=True), _use(3), _fenced(3),
                 _use(4), _result(4, "boom", is_error=True))
    assert (o.ran_before, o.ran_after, o.not_ran) == (1, 0, 2)
    assert D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


def test_a_gates_own_denial_is_not_a_call_that_ran():
    from levain.consent.broker_client import SLOT_LIMITS_FAILED_TEXT
    denial = SLOT_LIMITS_FAILED_TEXT.format(why="the counter")
    o = _observe(_use(1), _result(1, denial), _use(2), _fenced(2), _use(3), _result(3, denial))
    assert (o.ran_before, o.ran_after, o.not_ran) == (0, 0, 2)
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0] and "errors or other gate answers" in D.diagnose(o)


def test_calls_that_all_errored_before_the_refusal_are_not_a_pass():
    o = _observe(_use(1), _result(1, "denied", is_error=True), _use(2), _fenced(2))
    assert o.ran_before == 0 and not D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


def test_a_refusal_stamped_before_the_revoke_is_not_a_pass():
    o = _observe(_use(1), _result(1, "ok"), _use(2), _fenced(2, at=EARLY))
    assert (o.refused, o.ran_before, o.ran_after, o.after_revoke) == (True, 1, 0, False)
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0]
    assert "at or after the revoke" in D.diagnose(o)


def test_a_refusal_at_the_instant_of_the_revoke_follows_it():
    assert _observe(_use(1), _result(1, "ok"), _use(2), _fenced(2, at=REVOKED)).after_revoke is True


@pytest.mark.parametrize("at", [None, "", "yesterday", "2026-11-20T10:00:30"])
def test_a_refusal_with_no_readable_timestamp_is_not_a_pass(at):
    o = _observe(_use(1), _result(1, "ok"), _use(2), _fenced(2, at=at))
    assert o.refused and o.after_revoke is False
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


def test_a_judgement_with_no_revoke_instant_is_not_a_pass():
    o = D.observe_refusal(REFUSED)
    assert o.refused and o.after_revoke is False and not D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


@pytest.mark.parametrize("lines", [[], [""], ["not json", "{"], ["[1, 2]"]])
def test_a_transcript_with_no_record_is_not_a_pass(lines):
    o = D.observe_refusal(lines, revoked_at=REVOKED)
    assert o.refused is False and o.problem == "the transcript holds no JSON record"


def test_a_fence_text_with_no_reason_is_not_matched():
    head, tail = SLOT_FENCED_TEXT.split("{why}")
    o = _observe(_use(1), _result(1, "ok"), _use(2), _result(2, head + tail, LATE))
    assert o.refused is False


def test_the_pattern_is_built_from_the_broker_clients_words():
    rx = D.fenced_pattern()
    assert rx.search(SLOT_FENCED_TEXT.format(why="x")).group("why") == "x"
    assert rx.search("levain: STOPPED by a budget the operator set") is None


def test_a_call_that_executed_after_the_revoke_then_a_fenced_call_is_not_a_pass():
    o = _observe(_use(1), _result(1, "ok"), _use(2, at=CALL_LATE), _result(2, "ok", LATE), _use(3, at=CALL_LATE), _fenced(3))
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0]
    assert (o.refused, o.ran_before, o.ran_after_revoke, o.ran_unplaced, o.ran_after, o.after_revoke) == (True, 1, 1, 0, 0, True)
    assert "stamped at or after the revoke EXECUTED" in D.diagnose(o)


def test_a_call_already_running_when_the_revoke_landed_counts_as_before_it():
    """Its result is stamped after the revoke, but it was asked of the gate before: the clean sequence."""
    o = _observe(_use(1), _result(1, "ok", EARLY), _use(2), _result(2, "ok", LATE), _use(3, at=CALL_LATE), _fenced(3))
    assert (o.ran_before, o.ran_after_revoke, o.ran_unplaced, o.ran_after) == (2, 0, 0, 0)
    assert D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


@pytest.mark.parametrize("stamp", [None, "", "yesterday", "2026-11-20T09:59:40"])
def test_an_executed_call_with_no_readable_stamp_is_not_placed_and_is_not_a_pass(stamp):
    o = _observe(_use(1), _result(1, "ok"), _use(2, at=stamp), _result(2, "ok"), _use(3, at=CALL_LATE), _fenced(3))
    assert (o.ran_before, o.ran_after_revoke, o.ran_unplaced) == (1, 0, 1)
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0] and "no readable stamp" in D.diagnose(o)


def test_the_fences_words_in_a_result_that_is_not_an_error_are_a_call_that_ran():
    words = SLOT_FENCED_TEXT.format(why=WHY)
    o = _observe(_use(1), _result(1, words, LATE), _use(2), _result(2, "echo: " + words, LATE))
    assert (o.refused, o.ran_before) == (False, 2) and o.problem and "fence's words" in o.problem


def test_the_fences_words_in_an_error_result_are_the_refusal_and_a_later_clean_result_does_not_undo_it():
    o = _observe(_use(1), _result(1, "ok"), _use(2), _fenced(2), _use(3), _result(3, "ok"))
    assert (o.refused, o.tool_use_id, o.ran_after) == (True, "toolu_2", 1)


# --- import and arguments ----------------------------------------------------------------------------------------------

def test_the_driver_runs_as_a_script_under_isolated_python_and_prints_its_usage():
    """The guest runs it ``python -I <tools dir>/p7_2_fence_driver.py`` with host_profile.py and runbook.py beside it."""
    r = subprocess.run([sys.executable, "-I", str(DRIVER), "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    for flag in ("--workspace-repo", "--evidence", "--vendor-ready", "--revoke-after-s"):
        assert flag in r.stdout
    assert "--transcript" not in r.stdout


def _facts(**over):
    base = dict(run_socket=True, euid=501, vendor_ready=True, policy_ok=True, repo_ok=True, tests_importable=True,
                fence_dir=True, fence_free=True)
    base.update(over)
    return lambda cfg: D.Facts(**base)


def _run_with(transcript, **kw):
    return lambda cfg: D.Run(0, "oc-1", "s-0123456789abcdef", transcript, {}, REVOKED)


def test_no_evidence_path_from_an_argument_or_the_environment_refuses_without_writing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = D.main(["--workspace-repo", "/r"], read_facts_fn=_facts(), run_once=_run_with(REFUSED), env={})
    assert rc == 2 and "no evidence directory" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_the_transcript_option_is_gone(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        D.main(["--workspace-repo", "/r", "--evidence", str(tmp_path / "ev"), "--transcript", str(tmp_path / "t.jsonl")],
               read_facts_fn=_facts(), run_once=_run_with(REFUSED), env={})
    assert stop.value.code == 2 and "--transcript" in capsys.readouterr().err
    assert not (tmp_path / "ev" / "EXPECTED.txt").exists()


def test_the_evidence_path_comes_from_the_argument_or_the_environment(tmp_path):
    by_arg, by_env = tmp_path / "arg", tmp_path / "env"
    assert D.main(["--workspace-repo", "/r", "--evidence", str(by_arg), "--vendor-ready"], read_facts_fn=_facts(),
                  run_once=_run_with(REFUSED), env={}) == 0
    assert D.main(["--workspace-repo", "/r", "--vendor-ready"], read_facts_fn=_facts(), run_once=_run_with(REFUSED),
                  env={D.EVIDENCE_ENV: str(by_env)}) == 0
    for d in (by_arg, by_env):
        assert {p.name for p in d.iterdir()} == {"EXPECTED.txt", "steps.log", "RESULT.txt", "transcript.jsonl"}


def test_the_argument_beats_the_environment(tmp_path):
    arg, env = tmp_path / "arg", tmp_path / "env"
    D.main(["--workspace-repo", "/r", "--evidence", str(arg)], read_facts_fn=_facts(), run_once=_run_with(REFUSED),
           env={D.EVIDENCE_ENV: str(env)})
    assert arg.is_dir() and not env.exists()


def test_the_driver_names_no_path_under_project_memory():
    assert "project_memory" not in DRIVER.read_text()


# --- the exit codes ----------------------------------------------------------------------------------------------------

def _exit(tmp_path, capsys, *, facts=None, run_once=None, extra=()):
    rc = D.main(["--workspace-repo", "/r", "--evidence", str(tmp_path / "ev"), *extra], read_facts_fn=facts or _facts(),
                run_once=run_once or _run_with(REFUSED), env={})
    return rc, capsys.readouterr()


def test_a_run_that_observes_the_refused_call_exits_zero_and_records_the_reason(tmp_path, capsys):
    rc, out = _exit(tmp_path, capsys, extra=["--vendor-ready"])
    assert rc == 0 and "RESULT: PASS" in out.out
    assert f"refused with reason: {WHY}" in out.out and f"tool=Bash tool_use_id=toolu_3 at={LATE}" in out.out
    result = (tmp_path / "ev" / "RESULT.txt").read_text()
    assert "expected sha256 " + (tmp_path / "ev" / "EXPECTED.txt").read_text().split("sha256: ")[1].strip() in result
    assert (tmp_path / "ev" / "transcript.jsonl").read_text().splitlines() == REFUSED


def test_a_run_that_does_not_observe_the_refused_call_exits_nonzero(tmp_path, capsys):
    rc, out = _exit(tmp_path, capsys, run_once=_run_with(NOT_REFUSED), extra=["--vendor-ready"])
    assert rc == 1 and "RESULT: FAIL" in out.out and "diagnosis:" in out.out


def test_a_missing_precondition_refuses_with_exit_two_and_fires_nothing(tmp_path, capsys):
    def boom(cfg):
        raise AssertionError("fired")
    rc, out = _exit(tmp_path, capsys, facts=_facts(vendor_ready=False, fence_free=False), run_once=boom)
    assert rc == 2 and "--vendor-ready not given" in out.err and "not free for this sitting" in out.err
    assert (tmp_path / "ev" / "EXPECTED.txt").exists()


def test_a_run_that_raises_is_incomplete_not_a_pass(tmp_path, capsys):
    def boom(cfg):
        raise RuntimeError("the fence server did not come up")
    rc, out = _exit(tmp_path, capsys, run_once=boom, extra=["--vendor-ready"])
    assert rc == 3 and "RESULT: INCOMPLETE" in out.out and "did not come up" in out.out
    assert "--revoke-after-s" in out.out


def test_a_run_whose_refusal_is_stamped_before_the_revoke_exits_nonzero(tmp_path, capsys):
    early = _lines(_use(1), _result(1, "ok"), _use(2), _fenced(2, at=EARLY))
    rc, out = _exit(tmp_path, capsys, run_once=_run_with(early), extra=["--vendor-ready"])
    assert rc == 1 and "RESULT: FAIL" in out.out and "at or after the revoke" in out.out


def test_a_run_ended_by_a_signal_is_incomplete(tmp_path, capsys):
    def killed(cfg):
        raise SystemExit(143)
    rc, out = _exit(tmp_path, capsys, run_once=killed, extra=["--vendor-ready"])
    assert rc == 3 and "RESULT: INCOMPLETE" in out.out and "SystemExit" in out.out


def test_an_exception_after_the_run_returns_is_incomplete_with_the_reason_in_result_txt(tmp_path, capsys):
    bad = lambda cfg: D.Run(0, "oc-1", "s-0123456789abcdef", REFUSED, {"occurrences": 5}, REVOKED)    # noqa: E731
    rc, out = _exit(tmp_path, capsys, run_once=bad, extra=["--vendor-ready"])
    result = (tmp_path / "ev" / "RESULT.txt").read_text()
    assert rc == 3 and result.startswith("RESULT: INCOMPLETE") and "TypeError" in result and "RESULT: INCOMPLETE" in out.out


def _real_find_and_cat(seen):
    """The driver's ``sudo -n`` calls, run for real on a scratch tree: the ``sudo -n`` words are dropped, the rest is executed."""
    real = subprocess.run

    def run(argv, **kw):
        seen.append(argv)
        assert argv[:2] == ["sudo", "-n"]
        return real(argv[2:], **kw)
    return run


def _scratch_store(tmp_path, key="s-0123456789abcdef"):
    profile = dataclasses.replace(D.profile_for_platform("darwin"), store=str(tmp_path / "store"))
    tree = tmp_path / "store" / "retained" / key / "tree"
    return D.FenceConfig("/r", tmp_path / "ev", profile=profile), tree


def _put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_the_transcript_is_read_from_the_fired_sessions_claude_config_only(tmp_path, monkeypatch):
    seen: list = []
    cfg, tree = _scratch_store(tmp_path)
    projects = tree / "config" / "claude" / "projects"
    _put(projects / "-work-repo" / "abc.jsonl", '{"a": 1}\n{"a": 2}\n')
    _put(projects / "-work-repo" / "abc" / "subagents" / "agent-1.jsonl", '{"sub": 1}\n')
    _put(tree / "repo" / "projects" / "fake.jsonl", '{"fake": 1}\n')
    _put(tree / "repo" / "projects" / "x" / "fake2.jsonl", '{"fake": 2}\n')
    monkeypatch.setattr(D, "TRANSCRIPT_WAIT_S", 0.0)
    monkeypatch.setattr(D.subprocess, "run", _real_find_and_cat(seen))
    assert D.read_transcript(cfg, "s-0123456789abcdef") == ['{"a": 1}', '{"a": 2}']
    assert seen[0][3] == str(projects)


def test_two_top_level_transcripts_are_incomplete_and_none_is_read(tmp_path, monkeypatch):
    seen: list = []
    cfg, tree = _scratch_store(tmp_path)
    projects = tree / "config" / "claude" / "projects"
    _put(projects / "-work-repo" / "abc.jsonl", '{"a": 1}\n')
    _put(projects / "-other" / "def.jsonl", '{"b": 1}\n')
    monkeypatch.setattr(D, "TRANSCRIPT_WAIT_S", 0.0)
    monkeypatch.setattr(D.subprocess, "run", _real_find_and_cat(seen))
    with pytest.raises(RuntimeError, match="exactly one top-level session transcript"):
        D.read_transcript(cfg, "s-0123456789abcdef")
    assert not any(argv[2].endswith("cat") for argv in seen)


def test_a_transcript_only_in_the_workspace_repo_is_not_found(tmp_path, monkeypatch):
    cfg, tree = _scratch_store(tmp_path)
    _put(tree / "repo" / "projects" / "fake.jsonl", '{"fake": 1}\n')
    monkeypatch.setattr(D, "TRANSCRIPT_WAIT_S", 0.0)
    monkeypatch.setattr(D.subprocess, "run", _real_find_and_cat([]))
    with pytest.raises(RuntimeError, match="no transcript file"):
        D.read_transcript(cfg, "s-0123456789abcdef")


def test_no_retained_transcript_is_incomplete_and_does_not_point_at_a_removed_option(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "TRANSCRIPT_WAIT_S", 0.0)
    monkeypatch.setattr(D.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))
    with pytest.raises(RuntimeError) as err:
        D.read_transcript(D.FenceConfig("/r", tmp_path / "ev"), "s-0123456789abcdef")
    assert "--transcript" not in str(err.value) and "fresh world" in str(err.value)


def test_a_session_key_that_is_not_an_s1_id_is_refused_before_any_path_is_built(tmp_path):
    with pytest.raises(RuntimeError, match="not an S1 session id"):
        D.read_transcript(D.FenceConfig("/r", tmp_path / "ev"), "../../etc")


# --- the real world: the clock helpers and a revoke the fence answers -------------------------------------------------------

needs_world = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@needs_world
def test_prepare_stamps_the_launch_at_the_wall(tmp_path):
    pytest.importorskip("cryptography")
    from tests.test_agents_launch import Rig
    rig = Rig(tmp_path)
    D.anchor_clock(rig)
    oid, agent = D.prepare(rig, str(tmp_path))
    launched = datetime.strptime(rig.row(oid)["launched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((launched - datetime.now(timezone.utc)).total_seconds()) < 10, launched
    assert rig.row(oid)["agent_id"] == agent


@needs_world
def test_the_revoke_link_is_stamped_at_the_wall_and_the_fence_refuses_the_session_after_it(tmp_path):
    pytest.importorskip("cryptography")
    from levain.qbroker.broker import Resolved
    from levain.qbroker.fence import AgentFences
    from tests.test_agents_launch import Rig
    rig = Rig(tmp_path)
    D.anchor_clock(rig)
    oid, agent = D.prepare(rig, str(tmp_path))
    key = "s-0123456789abcdef"
    with rig.store.write() as c:      # what the fire records when it opens the slot session
        c.execute("UPDATE agent_occurrences SET session_key = ? WHERE occurrence_id = ?", (key, oid))
    fence = AgentFences(rig.store.directory / "autonomic.db", rig.authority, refresh_interval=0.0)
    who = Resolved(session=key, kind="dispatch", run={"run_id": "r-0123456789abcdef"})
    assert fence(who) is None
    D.revoke_mid_session(rig, agent)
    assert abs((rig.world.clock.now - datetime.now(timezone.utc)).total_seconds()) < 10
    reason = fence(who)
    assert isinstance(reason, str) and reason
    # the words the hook would send for that reason are the words the driver reads back
    assert D.fenced_pattern().search(SLOT_FENCED_TEXT.format(why=reason)).group("why") == reason


# --- the preconditions and the probes --------------------------------------------------------------------------------------

@pytest.mark.parametrize("over, needle", [
    ({"run_socket": False}, "no dispatch run socket"), ({"euid": 0}, "running as root"),
    ({"vendor_ready": False}, "--vendor-ready not given"), ({"policy_ok": False}, "provider policy"),
    ({"repo_ok": False}, "workspace repo"), ({"tests_importable": False}, "tests tree"),
    ({"world_fresh": False}, "fire world already exists"), ({"fence_dir": False}, "no R/fence/ directory"),
    ({"fence_free": False}, "not free for this sitting")])
def test_each_missing_precondition_is_worded_on_its_own(over, needle):
    lines = D.preconditions(_facts(**over)(None))
    assert len(lines) == 1 and needle in lines[0]


def test_the_fence_notes_reach_the_refusal_text():
    f = D.Facts(True, 501, True, True, True, True, True, False, details={"fence_free": "(/x/fence.sock: a server answers there)"})
    assert "a server answers there" in D.preconditions(f)[0]


@pytest.fixture
def short_dir():
    d = tempfile.mkdtemp(prefix="p72", dir="/tmp")      # a unix socket path has a short limit
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def test_off_a_guest_only_the_facts_that_are_known_are_worded(short_dir, tmp_path):
    profile = dataclasses.replace(D.profile_for_platform("darwin"), run_dir=str(short_dir), tools_dir=str(tmp_path))
    facts = D.read_facts(D.FenceConfig("/r", tmp_path / "ev", vendor_ready=True, profile=profile))
    assert facts.probed is False
    lines = D.preconditions(facts)
    assert len(lines) == 1 and "no dispatch run socket" in lines[0] and str(short_dir) in lines[0]
    assert not any(w in " ".join(lines) for w in ("fence", "policy", "repo", "tests tree"))


def test_a_path_with_nothing_on_it_is_free(short_dir):
    free, note = D.fence_socket_free(short_dir / "fence.sock")
    assert free and "absent" in note


def test_a_socket_that_answers_is_not_free_and_is_left_in_place(short_dir):
    path = short_dir / "fence.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        free, note = D.fence_socket_free(path)
    assert not free and "answers" in note and path.exists()


def test_a_socket_that_refuses_a_connect_is_passed_through_and_never_removed(short_dir):
    path = short_dir / "fence.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
    before = os.lstat(path)
    free, note = D.fence_socket_free(path)
    assert free and "refuses" in note and "left in place" in note
    assert (os.lstat(path).st_dev, os.lstat(path).st_ino) == (before.st_dev, before.st_ino)


def test_the_probe_calls_no_removal(short_dir, monkeypatch):
    path = short_dir / "fence.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
    def refuse(*a, **k):
        raise AssertionError("the probe removed something")
    with monkeypatch.context() as scoped:
        for name in ("unlink", "remove", "rmdir", "rename", "replace"):
            scoped.setattr(D.os, name, refuse)
        free = D.fence_socket_free(path)[0]
    assert free and path.exists()


def test_socket_answers_is_true_only_for_a_listening_server(short_dir):
    path = short_dir / "fence.sock"
    assert D.socket_answers(path) is False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        assert D.socket_answers(path) is False      # bound, not yet listening
        server.listen(1)
        assert D.socket_answers(path) is True
    assert D.socket_answers(path) is False


def test_a_file_that_is_not_a_socket_is_not_free_and_not_touched(short_dir):
    path = short_dir / "fence.sock"
    path.write_text("x")
    free, note = D.fence_socket_free(path)
    assert not free and "not a socket" in note and path.read_text() == "x"


# --- the processes: stopped on every way out, signals included ----------------------------------------------------------------

SLEEP = [sys.executable, "-c", "import time; time.sleep(300)"]
EXIT = lambda code: [sys.executable, "-c", f"raise SystemExit({code})"]    # noqa: E731


def _fake_rig(root, key="s-0123456789abcdef"):
    return types.SimpleNamespace(store=types.SimpleNamespace(directory=root), world=types.SimpleNamespace(root=root),
                                 row=lambda oid: {"session_key": key})


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The driver's process seams pointed at scratch commands; every process it starts is recorded."""
    made, revokes = [], []
    original = subprocess.Popen

    class Recording(original):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)
    sock = tmp_path / "fence.sock"
    sock.touch()
    monkeypatch.setattr(D.subprocess, "Popen", Recording)
    monkeypatch.setattr(D, "_fence_socket", lambda profile: sock)
    monkeypatch.setattr(D, "socket_answers", lambda path: True)
    monkeypatch.setattr(D, "revoke_mid_session", lambda rig, agent: revokes.append(agent))
    for name, value in (("FENCE_UP_TIMEOUT_S", 5.0), ("START_TIMEOUT_S", 5.0), ("STOP_GRACE_S", 5.0)):
        monkeypatch.setattr(D, name, value)

    def wire(fence, fire):
        def refuse(*a, **k):
            raise AssertionError("the fire was started")
        monkeypatch.setattr(D, "fence_server_command", lambda *a, **k: fence)
        monkeypatch.setattr(D, "fire_command", (lambda *a, **k: fire) if fire is not None else refuse)
    return types.SimpleNamespace(made=made, revokes=revokes, wire=wire, root=tmp_path,
                                 cfg=D.FenceConfig("/r", tmp_path / "ev", revoke_after_s=0.0, fire_timeout_s=30.0))


def _drive(w, rig):
    with D.stops_children_on_signals() as children:
        return D.fire_and_revoke(w.cfg, rig, "oc", "ag", os.environ, children)


def test_a_fence_server_that_exits_at_once_is_an_error_and_the_fire_is_never_started(wired, monkeypatch):
    wired.wire(EXIT(7), None)
    monkeypatch.setattr(D, "socket_answers", lambda path: False)
    with pytest.raises(RuntimeError, match=r"did not come up \(exit 7"):
        _drive(wired, _fake_rig(wired.root))
    assert len(wired.made) == 1 and all(p.poll() is not None for p in wired.made) and wired.revokes == []


def test_a_fire_that_records_no_session_is_an_error_and_nothing_is_left_running(wired):
    wired.wire(SLEEP, EXIT(0))
    with pytest.raises(RuntimeError, match="recorded no session"):
        _drive(wired, _fake_rig(wired.root, key=""))
    assert len(wired.made) == 2 and all(p.poll() is not None for p in wired.made) and wired.revokes == []


def test_a_fire_that_outlives_the_revoke_is_an_error_and_nothing_is_left_running(wired):
    wired.wire(SLEEP, SLEEP)
    wired.cfg.fire_timeout_s = 0.5
    with pytest.raises(RuntimeError, match="still running"):
        _drive(wired, _fake_rig(wired.root))
    assert len(wired.made) == 2 and all(p.poll() is not None for p in wired.made) and wired.revokes == ["ag"]


def test_a_fire_that_ends_after_the_revoke_returns_its_session_and_the_instant_before_the_revoke(wired):
    wired.wire(SLEEP, [sys.executable, "-c", "import time; time.sleep(0.3)"])
    before = datetime.now(timezone.utc)
    rc, key, revoked_at = _drive(wired, _fake_rig(wired.root))
    assert (rc, key, wired.revokes) == (0, "s-0123456789abcdef", ["ag"])
    assert before <= datetime.fromisoformat(revoked_at) <= datetime.now(timezone.utc)
    assert all(p.poll() is not None for p in wired.made)      # the fence server too: stopped on the way out


def test_a_child_that_ignores_the_terminate_is_killed(wired, monkeypatch):
    monkeypatch.setattr(D, "STOP_GRACE_S", 0.5)
    stubborn = [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"]
    wired.wire(stubborn, SLEEP)
    wired.cfg.fire_timeout_s = 0.5
    with pytest.raises(RuntimeError, match="still running"):
        _drive(wired, _fake_rig(wired.root))
    assert all(p.poll() is not None for p in wired.made)


def test_the_fence_is_not_up_while_its_socket_file_exists_but_nothing_answers(wired, monkeypatch, short_dir):
    stale = short_dir / "fence.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(stale))
    monkeypatch.setattr(D, "socket_answers", ORIGINAL_ANSWERS)
    monkeypatch.setattr(D, "_fence_socket", lambda profile: stale)
    monkeypatch.setattr(D, "FENCE_UP_TIMEOUT_S", 1.0)
    wired.wire(SLEEP, None)
    with pytest.raises(RuntimeError, match="did not come up"):
        _drive(wired, _fake_rig(wired.root))
    assert len(wired.made) == 1 and all(p.poll() is not None for p in wired.made) and stale.exists()


def test_the_fence_is_up_once_its_socket_answers(wired, monkeypatch, short_dir):
    path = short_dir / "fence.sock"
    monkeypatch.setattr(D, "socket_answers", ORIGINAL_ANSWERS)
    monkeypatch.setattr(D, "_fence_socket", lambda profile: path)
    wired.wire(SLEEP, [sys.executable, "-c", "import time; time.sleep(0.3)"])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(4)
        rc, key, _ = _drive(wired, _fake_rig(wired.root))
    assert (rc, key) == (0, "s-0123456789abcdef")


def test_the_stop_reaches_what_the_fire_started_in_its_group(wired, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    fire = [sys.executable, "-c",
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(300)"]
    wired.wire(SLEEP, fire)
    wired.cfg.fire_timeout_s = 4.0
    try:
        with pytest.raises(RuntimeError, match="still running"):
            _drive(wired, _fake_rig(wired.root))
        assert pidfile.exists()
        grandchild = int(pidfile.read_text())
        assert _wait_gone(grandchild)
    finally:
        if pidfile.exists() and not _gone(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_the_earlier_signal_handlers_are_put_back():
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    with D.stops_children_on_signals():
        assert signal.getsignal(signal.SIGTERM) is not before[signal.SIGTERM]
    assert {s: signal.getsignal(s) for s in before} == before


#: the driver's own cleanup path under a scratch process: the fence and the fire are two sleeping children
SCRATCH = """
import os, subprocess, sys
from pathlib import Path
from scripts.vm_harness import p7_2_fence_driver as D
work = Path(sys.argv[1])
original = subprocess.Popen
class Recording(original):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        with open(work / "pids", "a") as f:
            f.write(str(self.pid) + "\\n")
subprocess.Popen = Recording
sleeper = [sys.executable, "-c", "import time; time.sleep(300)"]
sock = work / "fence.sock"
sock.touch()
D._fence_socket = lambda profile: sock
D.socket_answers = lambda path: True
D.fence_server_command = lambda *a, **k: sleeper
D.fire_command = lambda *a, **k: sleeper
rig = type("Rig", (), {"store": type("S", (), {"directory": work}), "world": type("W", (), {"root": work}),
                       "row": lambda self, oid: {"session_key": "s-0123456789abcdef"}})()
cfg = D.FenceConfig("/r", work / "ev", revoke_after_s=300.0)
with D.stops_children_on_signals() as children:
    D.fire_and_revoke(cfg, rig, "oc", "ag", dict(os.environ), children)
"""


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _wait_gone(pid, seconds=10.0):
    end = time.monotonic() + seconds
    while not _gone(pid) and time.monotonic() < end:
        time.sleep(0.1)
    return _gone(pid)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_signal_stops_the_fence_and_the_fire_and_ends_the_run(tmp_path, sig):
    script = tmp_path / "scratch.py"
    script.write_text(SCRATCH)
    work = tmp_path / "w"
    work.mkdir()
    env = {**os.environ, "PYTHONPATH": str(DRIVER.parents[2])}
    proc = subprocess.Popen([sys.executable, str(script), str(work)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    pids: list[int] = []
    try:
        end = time.monotonic() + 90
        while len(pids) < 2 and proc.poll() is None and time.monotonic() < end:
            if (work / "pids").exists():
                pids = [int(x) for x in (work / "pids").read_text().split()]
            time.sleep(0.1)
        assert len(pids) == 2, proc.poll()
        proc.send_signal(sig)
        assert proc.wait(timeout=60) == 128 + sig
        assert all(_gone(pid) for pid in pids)
    finally:
        if proc.poll() is None:
            proc.kill()
        for pid in pids:        # before the pipes are drained: a surviving child holds them open
            if not _gone(pid):
                os.kill(pid, signal.SIGKILL)
        proc.communicate()


#: a run whose world preparation is where the signal lands: the rig's constructor signals this very process
PREPARE_SCRATCH = """
import dataclasses, os, signal, subprocess, sys, types
from pathlib import Path
from scripts.vm_harness import p7_2_fence_driver as D
work = Path(sys.argv[1])
original = subprocess.Popen
class Recording(original):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        with open(work / "pids", "a") as f:
            f.write(str(self.pid) + "\\\\n")
subprocess.Popen = Recording
class Rig:
    def __init__(self, root):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the signal did not end the run")
package = types.ModuleType("tests")
package.__path__ = []
module = types.ModuleType("tests.test_agents_launch")
module.Rig = Rig
sys.modules["tests"], sys.modules["tests.test_agents_launch"] = package, module
D._stage_paths = lambda profile: None
profile = dataclasses.replace(D.profile_for_platform(sys.platform), tools_dir=str(work))
cfg = D.FenceConfig("/r", work / "ev", vendor_ready=True, profile=profile)
(work / "ev").mkdir()
status, path = D.run(cfg, D.Facts(True, 501, True, True, True, True, True, True), run_once=D.real_run_once)
print(status)
print(path.read_text())
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_world_preparation_ends_the_run_incomplete_and_leaves_no_child(tmp_path, sig):
    script = tmp_path / "scratch.py"
    script.write_text(PREPARE_SCRATCH.replace("signal.SIGTERM)\n        raise", f"signal.{sig.name})\n        raise"))
    work = tmp_path / "w"
    work.mkdir()
    env = {**os.environ, "PYTHONPATH": str(DRIVER.parents[2])}
    r = subprocess.run([sys.executable, str(script), str(work)], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stdout.splitlines()[0] == "INCOMPLETE" and "SystemExit" in r.stdout
    assert (work / "ev" / "RESULT.txt").read_text().startswith("RESULT: INCOMPLETE")
    assert not (work / "pids").exists()
