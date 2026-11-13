"""RED (DIOGAP stage 1, finding X1): a repo ``PermissionRequest`` hook's deny, and a failure of the repo filter, leave
the filter as exit 2 + stderr. Claude Code's hooks reference says exit 2 "isn't honored" for ``PermissionRequest`` ("the
permission flow proceeds unchanged ... Deny through the ``decision`` object instead"; a hook that exits 2 without a
decision object leaves the flow unchanged and its stderr is discarded), so the deny never reaches Claude Code. The fix
lane makes the filter answer a ``PermissionRequest`` deny, and its own failure, with the decision object on stdout and
exit 0, and keeps exit 2 for ``PreToolUse``. These tests fail on the code as it stands."""
from __future__ import annotations

import io
import json

import pytest

from levain.slot import hook_run


def _filter(command: str, timeout: int = 5) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PermissionRequest", str(timeout), command],
                                stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _decision(out: str) -> dict:
    doc = json.loads(out)
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    return hso["decision"]


@pytest.mark.parametrize("command", [
    "echo nope-from-repo >&2; exit 2",
    "printf '%s' '{\"hookSpecificOutput\":{\"hookEventName\":\"PermissionRequest\","
    "\"decision\":{\"behavior\":\"deny\",\"message\":\"nope-from-repo\"}}}'",
])
def test_a_repo_permission_request_deny_is_a_decision_object_on_stdout_and_exit_0(command):
    code, out, _err = _filter(command)
    assert code == 0
    got = _decision(out)
    assert got["behavior"] == "deny" and "nope-from-repo" in got["message"]


def test_a_filter_failure_on_permission_request_is_a_deny_decision_object_and_exit_0():
    code, out, _err = _filter("sleep 5", timeout=1)
    assert code == 0
    got = _decision(out)
    assert got["behavior"] == "deny" and hook_run.REPO_FAILED in got["message"]


def test_pre_tool_use_keeps_exit_2_and_stderr():
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PreToolUse", "5", "echo nope >&2; exit 2"],
                                stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    assert code == 2 and out.getvalue() == "" and "nope" in err.getvalue()
