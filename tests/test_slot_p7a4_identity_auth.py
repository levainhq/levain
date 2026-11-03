"""P7-A4 (spec N-A1, Phill's np-26f9a7f9 (a)): the identity call is the unmodified binary's own
``claude auth status --json``, asked AS THE SLOT; Levain never opens or refreshes the login. Plus the baseline's
``dispatch`` mark and its writer.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import signal
import stat
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import identity as ID
from levain.slot import identity_child as IC
from levain.slot import root_tree
from levain.slot import runtime_helper as H
from tests.test_slot_idg1_identity import E0, _baseline, _login, env  # noqa: F401 - env is a fixture
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

ORG = "4f6c1a2e-0b7d-4c9e-8a31-2d5e6f708192"
EMAIL = "Zed.Quill-Unique@Example.invalid"
#: the keys of Claude Code's real ``auth status --json`` (values invented; the real ones were never recorded).
REAL_SHAPE = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "analyticsDisabled": False,
              "projectsDirectory": "/p", "configDirectory": "/c", "email": EMAIL, "orgId": ORG,
              "orgName": "x", "subscriptionType": "max"}
DIGEST = hashlib.sha256(ORG.encode() + b"\0" + EMAIL.lower().encode()).hexdigest()
VERSION_OK = b"2.1.316 (Claude Code)\n"


def _runner(code=0, obj=None, raw=None, exc=None, version=(0, VERSION_OK)):
    """``--version`` answers ``version``; ``auth status`` answers the rest."""
    calls = []

    def run(argv, env, timeout_s):
        calls.append((tuple(argv), dict(env), timeout_s))
        if tuple(argv[1:]) == IC.VERSION_ARGS:
            if isinstance(version, BaseException):
                raise version
            return version
        if exc is not None:
            raise exc
        return code, raw if raw is not None else json.dumps(obj).encode()
    run.calls = calls
    return run


# --- the child --------------------------------------------------------------------------------------------------------

def test_the_child_runs_version_then_exactly_auth_status_json_and_reports_a_local_data_claim():
    run = _runner(obj=REAL_SHAPE)
    ans = IC.fetch({"HOME": "/h"}, runner=run, binary="/bin/claude-x")
    assert [c[0] for c in run.calls] == [("/bin/claude-x", "--version"), ("/bin/claude-x", "auth", "status", "--json")]
    assert all(c[1] == {"HOME": "/h"} for c in run.calls)
    assert ans == {"claim": "local-data", "logged_in": True, "id_digest": DIGEST}


def test_the_childs_line_carries_nothing_else_the_binary_printed():
    out = io.StringIO()
    assert IC.main(env={}, out=out, runner=_runner(obj=REAL_SHAPE)) == 0
    line = out.getvalue()
    assert "example.invalid" not in line.lower() and "max" not in line and ORG not in line
    assert json.loads(line) == {"claim": "local-data", "logged_in": True, "id_digest": DIGEST}


# --- L3 r1 (1102-5): orgId is organisation-level; the observed id is per sign-in ---------------------------------------

def _observed(obj) -> str:
    """The child's answer for ``obj`` as root judges it: what the gate compares with the baseline."""
    out = io.StringIO()
    code = IC.main(env={}, out=out, runner=_runner(obj=obj))
    return ID.parse_auth_answer(code, out.getvalue().encode())


def test_two_members_of_one_org_read_as_two_sign_ins():
    a = _observed(dict(REAL_SHAPE, email="alice@example.invalid"))
    b = _observed(dict(REAL_SHAPE, email="bob@example.invalid"))
    assert a != b and re.fullmatch(r"[0-9a-f]{64}", a) and re.fullmatch(r"[0-9a-f]{64}", b)


def test_the_digest_ignores_case_of_the_email_and_the_org_id():
    assert _observed(REAL_SHAPE) == _observed(dict(REAL_SHAPE, email=EMAIL.upper(), orgId=ORG.upper())) == DIGEST


def test_the_same_member_of_another_org_reads_as_another_sign_in():
    assert _observed(dict(REAL_SHAPE, orgId="0" * 8 + ORG[8:])) != DIGEST


@pytest.mark.parametrize("email", ["<absent>", None, "", 7, "no-at-sign", " x@y.z", "x@y.z\n", "a\0@b.c",
                                   "x" * 320 + "@y.z", "\ud800@x.y"])
def test_an_org_only_answer_fails_closed(email):
    obj = dict(REAL_SHAPE)
    if email == "<absent>":
        del obj["email"]
    else:
        obj["email"] = email
    assert IC.fetch({}, runner=_runner(obj=obj), binary="x") == {"error": "bad-shape"}
    with pytest.raises(Refused) as e:
        _observed(obj)
    assert e.value.name == "identity-unparsable"


def test_no_email_shaped_string_reaches_the_baseline_or_the_record():
    """The email is hashed in the child: root's line, the record's identity and the baseline hold the digest only."""
    out = io.StringIO()
    IC.main(env={}, out=out, runner=_runner(obj=REAL_SHAPE))
    line = out.getvalue().encode()
    oid = ID.parse_auth_answer(0, line)
    data = ID.baseline_bytes(vendor="claude", observed_id=oid, confirmed_at="2026-11-02T10:00:00Z",
                             session="s-0000000000000001")
    record = json.dumps({"identity": {"observed_id": oid, "result": ID.GATE_MATCHED}}).encode()
    local = EMAIL.split("@")[0].encode()
    for blob in (line, data, record):
        assert b"@" not in blob and local.lower() not in blob.lower() and b"example" not in blob.lower()
        assert ORG.encode() not in blob


# --- L3 r1 (1102-5): the minimum version ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [(b"2.1.300 (Claude Code)\n", "binary-too-old"), (b"2.0.999\n", "binary-too-old"),
                                      (b"1.9.400\n", "binary-too-old"), (b"2.1.301 (Claude Code)\n", None),
                                      (b"2.2.0\n", None), (b"v3.0.0\n", None), (b"Claude Code\n", "version-unparsable"),
                                      (b"", "version-unparsable"), (b"2.1\n", "version-unparsable"),
                                      (b"2.1.3010\n", None), (b"2.1.30x\n", "version-unparsable")])
def test_a_binary_older_than_the_minimum_is_never_asked_auth_status(raw, want):
    run = _runner(obj=REAL_SHAPE, version=(0, raw))
    ans = IC.fetch({}, runner=run, binary="x")
    if want is None:
        assert ans["logged_in"] is True
    else:
        assert ans == {"error": want}
        assert [c[0][1:] for c in run.calls] == [IC.VERSION_ARGS]       # auth status never ran


def test_the_minimum_is_2_1_301():
    assert IC.MIN_VERSION == (2, 1, 301)


@pytest.mark.parametrize("version,want", [((1, VERSION_OK), "version-unparsable"), (TimeoutError(), "timeout"),
                                          (OSError(2, "x"), "spawn-failed"),
                                          ((0, b"2" * (IC.OUT_MAX + 1)), "output-too-large")])
def test_a_version_run_that_fails_is_named_and_auth_status_never_runs(version, want):
    run = _runner(obj=REAL_SHAPE, version=version)
    assert IC.fetch({}, runner=run, binary="x") == {"error": want}
    assert len(run.calls) == 1


def test_root_names_a_too_old_binary_unreachable_with_its_own_detail():
    with pytest.raises(Refused) as e:
        ID.parse_auth_answer(1, _line(error="binary-too-old"))
    assert e.value.name == "identity-unreachable" and e.value.detail.startswith("binary-too-old")
    with pytest.raises(Refused) as e:
        ID.parse_auth_answer(1, _line(error="version-unparsable"))
    assert e.value.name == "identity-unparsable"


# --- L3 r1 (1102-5): the run's stdout is bounded while it is read; its group dies with it -------------------------------

def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(stat.S_IRWXU)
    return str(p)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_binary_printing_50_mb_is_cut_at_the_cap_and_killed(tmp_path):
    exe = _script(tmp_path, "fat", "head -c 52428800 /dev/zero\n")
    t0 = time.monotonic()
    code, raw = IC._run([exe], {"PATH": "/usr/bin:/bin"}, 30.0)
    assert len(raw) == IC.OUT_MAX + 1 and code != 0 and time.monotonic() - t0 < 10.0


def test_a_descendant_holding_the_pipe_is_a_timeout_and_dies_with_the_group(tmp_path):
    pidfile = tmp_path / "pid"
    exe = _script(tmp_path, "hold", f"sleep 300 &\necho $! > {pidfile}\necho '{{}}'\nexit 0\n")
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        IC._run([exe], {"PATH": "/usr/bin:/bin"}, 1.5)
    assert time.monotonic() - t0 < 10.0
    pid = int(pidfile.read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.05)
    try:
        assert not _alive(pid)
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_run_that_ends_is_its_exit_status_and_its_whole_stdout(tmp_path):
    exe = _script(tmp_path, "ok", "printf '2.1.316 (Claude Code)\\n'\nexit 3\n")
    assert IC._run([exe], {"PATH": "/usr/bin:/bin"}, 10.0) == (3, b"2.1.316 (Claude Code)\n")


def test_the_real_run_end_to_end_through_a_fake_binary(tmp_path):
    exe = _script(tmp_path, "claude", 'if [ "$1" = "--version" ]; then echo "2.1.316 (Claude Code)"; exit 0; fi\n'
                  f"echo '{json.dumps(REAL_SHAPE)}'\n")
    assert IC.fetch({"PATH": "/usr/bin:/bin"}, binary=exe) == {"claim": "local-data", "logged_in": True,
                                                                "id_digest": DIGEST}


def test_logged_out_is_a_claim_whatever_the_exit_code():
    for code in (0, 1):
        assert IC.fetch({}, runner=_runner(code, {"loggedIn": False}), binary="x") == {
            "claim": "local-data", "logged_in": False}
    out = io.StringIO()
    assert IC.main(env={}, out=out, runner=_runner(1, {"loggedIn": False})) == 1


@pytest.mark.parametrize("obj", [{}, {"loggedIn": "yes", "orgId": ORG, "email": EMAIL}, {"loggedIn": True},
                                 {"loggedIn": True, "orgId": 7, "email": EMAIL}, {"loggedIn": True, "orgId": ORG},
                                 {"loggedIn": True, "orgId": "not-a-uuid", "email": EMAIL},
                                 {"loggedIn": 1, "orgId": ORG, "email": EMAIL}, [], "x"])
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


def test_root_takes_the_id_digest_of_a_local_data_claim():
    assert ID.parse_auth_answer(0, _line(claim="local-data", logged_in=True, id_digest=DIGEST)) == DIGEST
    assert ID.CLAIM == "local-data"


@pytest.mark.parametrize("code,out,name", [
    (1, _line(claim="local-data", logged_in=False), "identity-expired"),
    (0, _line(claim="local-data", logged_in=False), "identity-expired"),
    (1, _line(error="timeout"), "identity-unreachable"),
    (1, _line(error="no-binary"), "identity-unparsable"),
    (1, _line(error="bad-shape"), "identity-unparsable"),
    (0, _line(claim="server-confirmed", logged_in=True, id_digest=DIGEST), "identity-unparsable"),
    (0, _line(logged_in=True, id_digest=DIGEST), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True, id_digest="a b"), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True, id_digest=DIGEST, extra=1), "identity-unparsable"),
    (1, _line(claim="local-data", logged_in=True, id_digest=DIGEST), "identity-unparsable"),
    (0, b"x" * (ID.PROFILE_ANSWER_MAX + 1), "identity-unparsable"),
    (0, b"\xff", "identity-unparsable"), (0, b"[]", "identity-unparsable"),
    (0, ("[" * 200_000).encode()[:60_000], "identity-unparsable"),
    (1, _line(error="Bad Name"), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True, org_id=ORG), "identity-unparsable"),          # the old line
    (0, _line(claim="local-data", logged_in=True, id_digest=DIGEST.upper()), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True, id_digest=DIGEST[:63]), "identity-unparsable"),
    (0, _line(claim="local-data", logged_in=True, id_digest=ORG), "identity-unparsable"),
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
    o = {"format": ID.BASELINE_FORMAT, "vendor": "claude", "observed_id": DIGEST, "confirmed_at": "2026-11-02T10:00:00Z",
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
    kw = dict(vendor="claude", observed_id=DIGEST, confirmed_at="2026-11-02T10:00:00Z", session="s-0000000000000001")
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
