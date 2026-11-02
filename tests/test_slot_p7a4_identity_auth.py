"""P7-A4 (spec N-A1, Phill's np-26f9a7f9 (a)): the identity call is the unmodified binary's own
``claude auth status --json``, asked AS THE SLOT; Levain never opens or refreshes the login. Plus the baseline's
``dispatch`` mark and its writer.
"""
from __future__ import annotations

import ast
import io
import json
import re
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import identity as ID
from levain.slot import identity_child as IC
from levain.slot import records as R
from levain.slot import root_tree
from levain.slot import runtime_helper as H
from tests.test_slot_idg1_identity import E0, _baseline, _login, env  # noqa: F401 - env is a fixture
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

ORG = "4f6c1a2e-0b7d-4c9e-8a31-2d5e6f708192"
REAL_SHAPE = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "analyticsDisabled": False,
              "projectsDirectory": "/p", "configDirectory": "/c", "email": "x@example.invalid", "orgId": ORG,
              "orgName": "x", "subscriptionType": "max"}


def _runner(code=0, obj=None, raw=None, exc=None):
    calls = []

    def run(argv, env, timeout_s):
        calls.append((tuple(argv), dict(env), timeout_s))
        if exc is not None:
            raise exc
        return code, raw if raw is not None else json.dumps(obj).encode()
    run.calls = calls
    return run


# --- the child --------------------------------------------------------------------------------------------------------

def test_the_child_runs_exactly_auth_status_json_and_reports_a_local_data_claim():
    run = _runner(obj=REAL_SHAPE)
    ans = IC.fetch({"HOME": "/h"}, runner=run, binary="/bin/claude-x")
    assert run.calls[0][0] == ("/bin/claude-x", "auth", "status", "--json") and run.calls[0][1] == {"HOME": "/h"}
    assert ans == {"claim": "local-data", "logged_in": True, "org_id": ORG}


def test_the_childs_line_carries_nothing_else_the_binary_printed():
    out = io.StringIO()
    assert IC.main(env={}, out=out, runner=_runner(obj=REAL_SHAPE)) == 0
    line = out.getvalue()
    assert "example.invalid" not in line and "max" not in line and json.loads(line) == {
        "claim": "local-data", "logged_in": True, "org_id": ORG}


def test_logged_out_is_a_claim_whatever_the_exit_code():
    for code in (0, 1):
        assert IC.fetch({}, runner=_runner(code, {"loggedIn": False}), binary="x") == {
            "claim": "local-data", "logged_in": False}
    out = io.StringIO()
    assert IC.main(env={}, out=out, runner=_runner(1, {"loggedIn": False})) == 1


@pytest.mark.parametrize("obj", [{}, {"loggedIn": "yes", "orgId": ORG}, {"loggedIn": True}, {"loggedIn": True, "orgId": 7},
                                 {"loggedIn": True, "orgId": "not-a-uuid"}, {"loggedIn": 1, "orgId": ORG}, [], "x"])
def test_an_unknown_shape_fails_closed(obj):
    want = "not-json" if not isinstance(obj, dict) else "bad-shape"
    assert IC.fetch({}, runner=_runner(obj=obj), binary="x") == {"error": want}


@pytest.mark.parametrize("kw,err", [(dict(code=1, obj=REAL_SHAPE), "exit-nonzero"),
                                    (dict(raw=b"not json"), "not-json"), (dict(code=2, raw=b"x"), "exit-nonzero"),
                                    (dict(exc=TimeoutError()), "timeout"), (dict(exc=OSError(2, "x")), "spawn-failed"),
                                    (dict(raw=b"x" * (IC.OUT_MAX + 1)), "output-too-large")])
def test_each_failure_is_a_fixed_name(kw, err):
    assert IC.fetch({}, runner=_runner(**kw), binary="x") == {"error": err}


def test_no_binary_is_a_fixed_name(tmp_path):
    assert IC.fetch({}, binary=str(tmp_path / "absent")) == {"error": "no-binary"}


def test_the_default_binary_is_the_vendor_prefix_copy():
    for host, name in (("darwin", "darwin"), ("linux", "linux")):
        assert IC.VENDOR_BINARY[host] == f"{root_tree.VENDOR_PREFIX[name]}/bin/claude"


# --- the retired refresh: a guard that fails if it comes back -------------------------------------------------------------

_CHILD = Path(IC.__file__)


def test_the_child_names_neither_the_token_endpoint_nor_a_credential_file():
    tree = ast.parse(_CHILD.read_text())
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    for bad in ("oauth/token", ".credentials", "credentials.json", "refresh_token", "claudeAiOauth", "grant_type"):
        assert not [x for x in strings if bad in x], bad
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"urlopen", "build_opener", "Request", "_refresh", "TOKEN_URL", "CREDENTIALS_NAME"}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in imported if m and m.split(".")[0] in {"urllib", "http", "socket", "ssl"}}


def test_the_child_never_opens_a_file():
    tree = ast.parse(_CHILD.read_text())
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "open" not in calls


# --- root's judgement --------------------------------------------------------------------------------------------------

def _line(**kw):
    return json.dumps(kw).encode()


def test_root_takes_the_org_id_of_a_local_data_claim():
    assert ID.parse_auth_answer(0, _line(claim="local-data", logged_in=True, org_id=ORG)) == ORG
    assert ID.CLAIM == "local-data"


@pytest.mark.parametrize("code,out,name", [
    (1, _line(claim="local-data", logged_in=False), "identity-expired"),
    (0, _line(claim="local-data", logged_in=False), "identity-expired"),
    (1, _line(error="timeout"), "identity-unreachable"),
    (1, _line(error="no-binary"), "identity-unparsable"),
    (1, _line(error="bad-shape"), "identity-unparsable"),
    (0, _line(claim="server-confirmed", logged_in=True, org_id=ORG), "identity-unparsable"),
    (0, _line(logged_in=True, org_id=ORG), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True, org_id="a b"), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True, org_id=ORG, extra=1), "identity-unparsable"),
    (1, _line(claim="local-data", logged_in=True, org_id=ORG), "identity-unparsable"),
    (0, b"x" * (ID.PROFILE_ANSWER_MAX + 1), "identity-unparsable"),
    (0, b"\xff", "identity-unparsable"), (0, b"[]", "identity-unparsable"),
    (0, ("[" * 200_000).encode()[:60_000], "identity-unparsable"),
    (1, _line(error="Bad Name"), "identity-unparsable"),
])
def test_root_refuses_each_other_line_by_name(code, out, name):
    with pytest.raises(Refused) as e:
        ID.parse_auth_answer(code, out)
    assert e.value.name == name


def test_the_old_profile_parser_is_gone():
    assert not hasattr(ID, "parse_profile_answer")
    assert not hasattr(IC, "TOKEN_URL") and not hasattr(IC, "TEMP_NAME") and not hasattr(IC, "CLIENT_ID")


# --- the baseline's dispatch mark ---------------------------------------------------------------------------------------

def _obj(**extra):
    o = {"format": ID.BASELINE_FORMAT, "vendor": "claude", "observed_id": ORG, "confirmed_at": "2026-11-02T10:00:00Z",
         "session": "s-0000000000000001"}
    o.update(extra)
    return o


def test_check_baseline_accepts_the_mark_only_as_yes():
    assert ID.BASELINE_DISPATCH_KEY == "dispatch"
    assert ID.check_baseline(_obj()) and ID.check_baseline(_obj(dispatch="yes"))["dispatch"] == "yes"
    for bad in ("no", "", True, 1, None, "Yes"):
        with pytest.raises(Refused) as e:
            ID.check_baseline(_obj(dispatch=bad))
        assert e.value.name == "bad-baseline"
    with pytest.raises(Refused):
        ID.check_baseline(_obj(other="x"))
    with pytest.raises(Refused):
        ID.check_baseline(_obj(dispatch="yes", other="x"))


def test_baseline_bytes_writes_the_mark_only_when_asked():
    kw = dict(vendor="claude", observed_id=ORG, confirmed_at="2026-11-02T10:00:00Z", session="s-0000000000000001")
    assert "dispatch" not in canon.parse(ID.baseline_bytes(**kw))
    assert "dispatch" not in canon.parse(ID.baseline_bytes(dispatch=False, **kw))
    assert canon.parse(ID.baseline_bytes(dispatch=True, **kw))["dispatch"] == "yes"


def test_confirm_yes_dispatch_writes_the_mark_and_a_later_plain_confirm_drops_it(env):
    w, la, _ = env
    s = _login(w, la, "codex")
    assert w.main(["confirm-identity", s, "yes", "--dispatch"])[1] == {"ok": True, "baseline": "written"}
    assert canon.parse(_baseline(w).read_bytes())["dispatch"] == "yes"
    view = H.read_view(H.Store(w.seams), E0)
    assert view.baseline == "codex" and view.dispatch == "codex"
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": True, "baseline": "written"}
    assert "dispatch" not in canon.parse(_baseline(w).read_bytes())
    assert H.read_view(H.Store(w.seams), E0).dispatch is None


def test_a_baseline_with_the_mark_still_reads_as_the_vendors(env):
    w, la, _ = env
    s = _login(w, la, "codex")
    w.main(["confirm-identity", s, "yes", "--dispatch"])
    assert ID.baseline_vendor(_baseline(w).read_bytes()) == "codex"
