"""`levain doctor`'s ``agent fence`` row (P7-2 wiring, shape C): the operator-uid fence server the queue broker asks on
every dispatch GATE, read as the operator. One run against a real server over a real socket; the rest through the
row's seams (the socket's stat, the STATUS answer)."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from levain import doctor
from levain.daemon import DEFAULT_FENCE_LABEL

ME = os.getuid()
GID = 4321


@pytest.fixture
def install():
    top = Path(tempfile.mkdtemp(prefix="dfe-", dir="/tmp"))
    (top / "L" / "policy").mkdir(parents=True)
    (top / "R" / "fence").mkdir(parents=True)
    yield top / "L", top / "R"
    shutil.rmtree(top)


def sock_stat(mode=0o660, uid=ME, gid=GID, kind=stat.S_IFSOCK):
    return os.stat_result((kind | mode, 0, 0, 0, uid, gid, 0, 0, 0, 0))


def row(install, *, ask=None, st=None, uid=ME, gid=GID):
    L, R = install
    rows = doctor._check_agent_fence(host="darwin", store=L, run=R, uid=uid, ask=ask or (lambda p, u: pytest.fail()),
                                     lstat=(lambda p: st) if st is not None else None, qbroker_gid=lambda: gid)
    assert len(rows) == 1
    return rows[0]


def test_no_row_before_the_isolation_install(tmp_path):
    assert doctor._check_agent_fence(host="darwin", store=tmp_path, run=tmp_path, uid=ME) == []


def test_run_as_root_is_not_asked(install):
    r = row(install, uid=0)
    assert r.neutral and "run as root" in r.detail


def test_no_socket_is_not_running_and_refuses_dispatch(install):
    r = row(install)
    assert not r.ok and r.detail == "not running. Every dispatch session's tool calls are refused until it runs."
    assert DEFAULT_FENCE_LABEL in r.hint and "never root" in r.hint


@pytest.mark.parametrize("st", [sock_stat(mode=0o666), sock_stat(uid=ME + 1), sock_stat(gid=GID + 1),
                                sock_stat(kind=stat.S_IFREG)])
def test_a_socket_of_the_wrong_shape_is_named(install, st):
    r = row(install, st=st)
    assert not r.ok and "is not owner you" in r.detail and "group _levain_qbroker, 0660" in r.detail
    assert "the queue broker refuses it" in r.detail


@pytest.mark.parametrize("answer, words", [
    (("down", "ConnectionRefusedError"), "not running. Every dispatch session's tool calls are refused"),
    (("peer", 77), "served by uid 77, not you"),
    (("silent", "no answer in time"), "running but does not answer (no answer in time)"),
    (("refused", "not-permitted"), "running but does not answer (not-permitted)"),
    (("answer", {"store_error": "OperationalError: unable to open database file", "authority_error": None}),
     "running but cannot read the agent store (OperationalError: unable to open database file). Dispatch sessions "
     "are refused."),
    (("answer", {"store_error": None, "authority_error": "no authority anchor"}),
     "running but cannot read the authority log (no authority anchor)"),
])
def test_each_failure_says_dispatch_is_refused(install, answer, words):
    r = row(install, st=sock_stat(), ask=lambda p, u: answer)
    assert not r.ok and words in r.detail


def test_running_and_reading(install):
    r = row(install, st=sock_stat(), ask=lambda p, u: ("answer", {"store": "/Users/x/.levain/autonomic", "seq": 12,
                                                                  "store_error": None, "authority_error": None}))
    assert r.ok and not r.warn
    assert r.detail == ("running (store /Users/x/.levain/autonomic, authority seq 12). Dispatch sessions are checked "
                        "on every tool call.")


def test_a_row_that_raises_is_its_own_failure(monkeypatch):
    monkeypatch.setattr(doctor, "_check_agent_fence", lambda: 1 / 0)
    rows = doctor._guarded("agent fence", doctor._check_agent_fence)
    assert len(rows) == 1 and not rows[0].ok and "ZeroDivisionError" in rows[0].detail


# --- one run against the real server ------------------------------------------------------------------------------

pytest.importorskip("cryptography")
from tests.test_qbroker_fence import L, clock, keys, resolver, rig, serve_fence, world  # noqa: E402,F401


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_the_row_reads_a_real_fence_server(rig, install):
    store, run = install
    os.chmod(run / "fence", 0o750)
    gid = os.stat(run / "fence").st_gid
    srv = serve_fence(rig, run, broker_uid=ME + 100_000, operator_uid=ME)
    try:
        rows = doctor._check_agent_fence(host="darwin", store=store, run=run, uid=ME, qbroker_gid=lambda: gid)
    finally:
        srv.stop()
    assert len(rows) == 1 and rows[0].ok, rows
    assert f"store {rig.db.parent}" in rows[0].detail and "authority seq " in rows[0].detail
    assert "authority seq ?" not in rows[0].detail
    after = doctor._check_agent_fence(host="darwin", store=store, run=run, uid=ME, qbroker_gid=lambda: gid)
    assert not after[0].ok and after[0].detail.startswith("not running")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_the_row_refuses_a_socket_served_by_another_uid(rig, install):
    store, run = install
    os.chmod(run / "fence", 0o750)
    gid = os.stat(run / "fence").st_gid
    srv = serve_fence(rig, run)                     # this uid serves it; the row is told the operator is another
    try:
        kind, val = doctor._ask_fence_status(srv.path, ME + 1, doctor._clock() + 3)
    finally:
        srv.stop()
    assert (kind, val) == ("peer", ME)
