"""P7-2's VM driver (``scripts/vm_harness/p7_2_fence_driver.py``): the pure decision 'a tool call was refused with the fence's reason'
from a recorded transcript, the argument handling, and the exit codes. NO guest, NO vendor session, NO fence server runs here:
``main`` is driven with a fake facts probe and a fake run. The two rig tests build the real world (ssh-keygen links, the real agent
store) to read back what the driver's clock helpers stamp, and to check the revoke it appends is one the fence answers."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.consent.broker_client import SLOT_FENCED_TEXT
from scripts.vm_harness import p7_2_fence_driver as D

DRIVER = Path(D.__file__)
WHY = "the agent is barred"


def _use(i, name="Bash"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": {"command": "sleep 15"}}]}}


def _result(i, text, **extra):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": text, **extra}]}}


def _lines(*records):
    return [json.dumps(r) for r in records]


REFUSED = _lines(
    {"type": "user", "message": {"role": "user", "content": "Use the Bash tool to run sleep 15"}},
    _use(1), _result(1, "ok"),
    _use(2), _result(2, "ok"),
    _use(3), _result(3, SLOT_FENCED_TEXT.format(why=WHY), is_error=True),
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "It was refused."}]}},
)
NOT_REFUSED = _lines(_use(1), _result(1, "ok"), _use(2), _result(2, "ok"),
                     {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}})


# --- the pure decision -------------------------------------------------------------------------------------------------

def test_a_transcript_with_a_refused_call_is_read_with_its_reason_and_what_ran_around_it():
    o = D.observe_refusal(REFUSED)
    assert (o.refused, o.reason, o.tool, o.tool_use_id) == (True, WHY, "Bash", "toolu_3")
    assert (o.ran_before, o.ran_after, o.problem) == (2, 0, None)
    assert D.raw_line(o) == "P7-2 refused=1 ran_before=2 ran_after=0 reason=set"
    assert D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


def test_a_transcript_with_no_refused_call_is_not_a_pass():
    o = D.observe_refusal(NOT_REFUSED)
    assert (o.refused, o.reason, o.ran_before, o.ran_after) == (False, None, 2, 0)
    assert o.problem and "fence's words" in o.problem
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0]


def test_a_reason_that_holds_a_parenthesis_is_read_whole():
    why = "the occurrence left launched (now stopped)"
    o = D.observe_refusal(_lines(_use(1), _result(1, "ok"), _use(2), _result(2, SLOT_FENCED_TEXT.format(why=why))))
    assert o.reason == why


def test_a_refusal_that_is_the_first_call_is_not_a_pass():
    o = D.observe_refusal(_lines(_use(1), _result(1, SLOT_FENCED_TEXT.format(why=WHY))))
    assert (o.refused, o.ran_before) == (True, 0)
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0] and "before the session ran a call" in D.diagnose(o)


def test_a_call_that_completes_after_the_refusal_is_not_a_pass():
    o = D.observe_refusal(_lines(_use(1), _result(1, "ok"), _use(2), _result(2, SLOT_FENCED_TEXT.format(why=WHY)),
                                 _use(3), _result(3, "ok")))
    assert (o.refused, o.ran_before, o.ran_after) == (True, 1, 1)
    assert not D.judge([D.raw_line(o)], 0, D.VERDICT)[0] and "AFTER the refusal" in D.diagnose(o)


def test_the_models_own_quote_of_the_words_is_not_a_refused_call():
    quote = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": SLOT_FENCED_TEXT.format(why=WHY)}]}}
    o = D.observe_refusal(_lines(_use(1), _result(1, "ok"), quote))
    assert o.refused is False


def test_a_refusal_kept_only_in_a_hook_record_counts_and_only_the_first_is_read():
    hook = {"type": "system", "subtype": "hook", "content": SLOT_FENCED_TEXT.format(why=WHY)}
    o = D.observe_refusal(_lines(_use(1), _result(1, "ok"), hook, hook))
    assert (o.refused, o.reason, o.tool_use_id, o.ran_before, o.ran_after) == (True, WHY, None, 1, 0)


def test_a_result_whose_content_is_a_list_of_text_parts_is_read():
    o = D.observe_refusal(_lines(_use(1), _result(1, "ok"), _use(2),
                                 _result(2, [{"type": "text", "text": SLOT_FENCED_TEXT.format(why=WHY)}])))
    assert (o.refused, o.reason) == (True, WHY)


@pytest.mark.parametrize("lines", [[], [""], ["not json", "{"], ["[1, 2]"]])
def test_a_transcript_with_no_record_is_not_a_pass(lines):
    o = D.observe_refusal(lines)
    assert o.refused is False and o.problem == "the transcript holds no JSON record"


def test_a_fence_text_with_no_reason_is_not_matched():
    head, tail = SLOT_FENCED_TEXT.split("{why}")
    o = D.observe_refusal(_lines(_use(1), _result(1, "ok"), _use(2), _result(2, head + tail)))
    assert o.refused is False


def test_the_pattern_is_built_from_the_broker_clients_words():
    rx = D.fenced_pattern()
    assert rx.search(SLOT_FENCED_TEXT.format(why="x")).group("why") == "x"
    assert rx.search("levain: STOPPED by a budget the operator set") is None


# --- import and arguments ----------------------------------------------------------------------------------------------

def test_the_driver_runs_as_a_script_under_isolated_python_and_prints_its_usage():
    """The guest runs it ``python -I <tools dir>/p7_2_fence_driver.py`` with host_profile.py and runbook.py beside it."""
    r = subprocess.run([sys.executable, "-I", str(DRIVER), "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    for flag in ("--workspace-repo", "--evidence", "--vendor-ready", "--transcript", "--revoke-after-s"):
        assert flag in r.stdout


def _facts(**over):
    base = dict(run_socket=True, euid=501, vendor_ready=True, policy_ok=True, repo_ok=True, tests_importable=True,
                fence_dir=True, fence_free=True)
    base.update(over)
    return lambda cfg: D.Facts(**base)


def _run_with(transcript, **kw):
    return lambda cfg: D.Run(0, "oc-1", "s-0123456789abcdef", transcript, {}, "t")


def test_no_evidence_path_from_an_argument_or_the_environment_refuses_without_writing(tmp_path, capsys):
    rc = D.main(["--workspace-repo", "/r"], read_facts_fn=_facts(), run_once=_run_with(REFUSED), env={})
    assert rc == 2 and "no evidence directory" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


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
    assert f"refused with reason: {WHY}" in out.out and "tool=Bash tool_use_id=toolu_3" in out.out
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
    assert rc == 2 and "--vendor-ready not given" in out.err and "fence server is already bound" in out.err
    assert (tmp_path / "ev" / "EXPECTED.txt").exists()


def test_a_run_that_raises_is_incomplete_not_a_pass(tmp_path, capsys):
    def boom(cfg):
        raise RuntimeError("the fence server did not come up")
    rc, out = _exit(tmp_path, capsys, run_once=boom, extra=["--vendor-ready"])
    assert rc == 3 and "RESULT: INCOMPLETE" in out.out and "did not come up" in out.out


def test_a_transcript_file_argument_is_read_instead_of_the_retained_tree(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(REFUSED) + "\n")
    cfg = D.FenceConfig("/r", tmp_path / "ev", transcript=f)
    assert D.read_transcript(cfg, "s-0123456789abcdef") == REFUSED


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
