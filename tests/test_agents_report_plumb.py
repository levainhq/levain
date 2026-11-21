"""levain#225, the plumb of the session report channel: ``HelperSlotRuntime.read_report`` (the ``read-report`` verb's line),
``RealLauncher.report``, the run server's ``REPORT`` op and ``DispatchSessionLauncher.report``.

The report is UNTRUSTED DATA at every layer: these layers judge the wire (one base64 field, the byte cap, the record's
state), never what the text says. The root verb itself is another lane's; here its answer is a scripted line off the
``Helper`` fake of the dispatch runtime tests.

UNVERIFIED here (VM rows): a real ``sudo -n runtime-helper read-report`` as ``_levain_dispatch``.
"""
from __future__ import annotations

import base64
import shutil

import pytest

from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_launcher as L
from levain.slot import Refused
from levain.slot import dispatch_runtime as DR
from tests.test_agents_session_launcher import KEY, FakeRuntime, build, ended, launch
from tests.test_slot_dispatch_runtime import SESSION, line, rt_rig  # noqa: F401
from tests.test_agents_launch import Rig  # noqa: F401
from tests.test_agents_session_launch import EXP, _occurrence, rig  # noqa: F401

from levain.slot.runtime_grammar import REPORT_CAP as CAP


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---- HelperSlotRuntime.read_report: the verb's line ---------------------------------------------------------------------

def test_an_absent_report_is_none_and_the_verb_is_asked_with_the_session(rt_rig):
    h, _lease, make = rt_rig
    h.script_("read-report", line=line(ok=True, present=False))
    assert make().read_report(SESSION) is None
    assert h.calls()[-1]["argv"] == ["read-report", SESSION]


def test_a_present_report_is_its_exact_bytes(rt_rig):
    h, _lease, make = rt_rig
    raw = "# done\nline two é\n".encode()
    h.script_("read-report", line=line(ok=True, present=True, b64=b64(raw)))
    assert make().read_report(SESSION) == raw


def test_a_report_of_exactly_the_cap_passes_and_one_more_is_oversize(rt_rig):
    h, _lease, make = rt_rig
    h.script_("read-report", line=line(ok=True, present=True, b64=b64(b"a" * CAP)))
    assert make().read_report(SESSION) == b"a" * CAP
    h.script_("read-report", line=line(ok=True, present=True, b64=b64(b"a" * (CAP + 1))))
    with pytest.raises(Refused) as e:
        make().read_report(SESSION)
    assert e.value.name == "report-oversize"


@pytest.mark.parametrize("name", ["report-unreadable", "report-oversize", "report-malformed", "session-busy",
                                  "intent-in-flight", "not-retained:trash", "no-session"])
def test_a_refusal_of_the_helper_keeps_its_name(rt_rig, name):
    h, _lease, make = rt_rig
    h.script_("read-report", line=line(ok=False, refused=name))
    with pytest.raises(Refused) as e:
        make().read_report(SESSION)
    assert e.value.name == name


@pytest.mark.parametrize("bad", [
    line(ok=True),
    line(ok=True, present=True),
    line(ok=True, present=False, b64="AAAA"),
    line(ok=True, present="yes", b64="AAAA"),
    line(ok=True, present=True, b64=5),
    line(ok=True, present=True, b64="not base64!!"),
    line(ok=True, present=True, b64="QUJD", extra=1),
])
def test_an_answer_off_its_shape_is_malformed_not_a_report(rt_rig, bad):
    h, _lease, make = rt_rig
    h.script_("read-report", line=bad)
    with pytest.raises(Refused) as e:
        make().read_report(SESSION)
    assert e.value.name == "helper-output-malformed"


def test_a_session_off_the_grammar_runs_no_helper(rt_rig):
    h, _lease, make = rt_rig
    with pytest.raises(Refused):
        make().read_report("../etc")
    assert h.verbs() == []


def test_an_ok_line_from_a_failing_helper_is_no_report(rt_rig):
    h, _lease, make = rt_rig
    h.script_("read-report", line=line(ok=True, present=False), rc=1)
    with pytest.raises(Refused) as e:
        make().read_report(SESSION)
    assert e.value.name == "helper-status"


# ---- RealLauncher.report -------------------------------------------------------------------------------------------------

def test_the_launcher_reads_the_report_of_the_recorded_session(tmp_path):
    ln, _, runtime = build(tmp_path)
    ended(ln.spawn(launch()))
    runtime.reports["sess-1"] = b"the report"
    assert ln.report(KEY) == b"the report"
    runtime.reports["sess-1"] = None
    assert ln.report(KEY) is None


def test_the_launcher_refuses_a_run_not_proven_ended(tmp_path):
    ln, _, runtime = build(tmp_path)
    ln.spawn(launch())                                     # live: END not proven
    runtime.reports["sess-1"] = b"early"
    with pytest.raises(L.LauncherRefused) as e:
        ln.report(KEY)
    assert e.value.name == "report-end-unproven"


@pytest.mark.parametrize("key", ["s-ffffffffffffffff", "../x", "", "S-UP"])
def test_the_launcher_refuses_an_unknown_key(tmp_path, key):
    ln, _, runtime = build(tmp_path)
    ended(ln.spawn(launch()))
    with pytest.raises(L.LauncherRefused) as e:
        ln.report(key)
    assert e.value.name in ("report-unindexed", "report-index-unreadable")


def test_the_launcher_refuses_a_record_of_the_other_auth(tmp_path):
    ln, _, _ = build(tmp_path)
    ended(ln.spawn(launch()))
    other, _, _ = build(tmp_path, auth="brokered")       # the same index directory, the other auth
    with pytest.raises(L.LauncherRefused) as e:
        other.report(KEY)
    assert e.value.name == "report-auth-mismatch"


def test_the_launcher_keeps_the_runtimes_refusal_under_its_name(tmp_path):
    ln, _, runtime = build(tmp_path)
    ended(ln.spawn(launch()))
    runtime.reports["sess-1"] = Refused("report-malformed", "x")
    with pytest.raises(L.LauncherRefused) as e:
        ln.report(KEY)
    assert e.value.name == "report-malformed"


def test_a_non_bytes_answer_is_malformed(tmp_path):
    ln, _, runtime = build(tmp_path)
    ended(ln.spawn(launch()))
    runtime.reports["sess-1"] = "text, not bytes"
    with pytest.raises(L.LauncherRefused) as e:
        ln.report(KEY)
    assert e.value.name == "report-malformed"


def test_a_runtime_with_no_reader_is_refused(tmp_path):
    class NoReader(FakeRuntime):
        read_report = None

    ln, _, _ = build(tmp_path, runtime=NoReader())
    ended(ln.spawn(launch()))
    with pytest.raises(L.LauncherRefused) as e:
        ln.report(KEY)
    assert e.value.name == "report-unsupported"


# ---- the REPORT op, over the real client and the real server -------------------------------------------------------------

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def env(rig, tmp_path):  # noqa: F811
    from tests.test_slot_dispatch_run_server import Env
    return Env(rig, tmp_path)


def _ended_session(rig, env, key="s-00000000000000a1"):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    child.release()
    assert env.client().end_session(key, 5).terminated is True
    child._conn.close()
    env.drain()
    return key


@needs_keygen
def test_report_over_the_wire_is_the_exact_bytes_or_none(rig, env):
    key = _ended_session(rig, env)
    raw = b"\x00 not text, but the wire only carries it \xff"
    env.runtime.reports["sess-1"] = raw
    assert env.client().report(key) == raw
    env.runtime.reports["sess-1"] = None
    assert env.client().report(key) is None
    env.drain()


@needs_keygen
def test_a_refusal_crosses_under_its_name(rig, env):
    key = _ended_session(rig, env)
    env.runtime.reports["sess-1"] = Refused("report-oversize", "x")
    with pytest.raises(L.LauncherRefused) as e:
        env.client().report(key)
    assert e.value.name == "report-oversize"
    env.drain()


@needs_keygen
def test_an_unknown_key_or_a_bad_key_is_refused_never_none(rig, env):
    with pytest.raises(L.LauncherRefused) as e:
        env.client().report("s-00000000000000ff")
    assert e.value.name == "report-unindexed"
    with pytest.raises(L.LauncherRefused) as e2:
        env.client().report("../x")
    assert e2.value.name == "report-bad-key"
    env.drain()


@needs_keygen
def test_an_unreachable_server_is_a_named_refusal(rig, tmp_path):
    def down():
        raise ConnectionRefusedError("no server")

    cl = C.DispatchSessionLauncher(down, lambda occ: None)
    with pytest.raises(L.LauncherRefused) as e:
        cl.report("s-00000000000000a1")
    assert e.value.name == "report-unreachable"
