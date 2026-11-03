"""The operator-uid agent fence server (levain.qbroker.fence_server) and the broker's client of it
(levain.qbroker.fence.RemoteFences): P7-2 wiring, shape C (project_memory/1102-7/P72_WIRING_DESIGN.md).

Every test here runs a real server over a real unix socket, with today's AgentFences reading a real autonomic store and
a real authority log (tests/test_qbroker_fence.py's rig). Both ends run as this uid, so "the broker's uid" is this uid
and "the operator's" a uid no process has, or the other way round; the kernel's peer credential is read for real. The
one thing a laptop cannot show is the check between two REAL accounts (the VM row)."""
from __future__ import annotations

import os
import shutil
import socket
import stat
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

from levain import agent_authority, daemon
from levain.consent import broker_client
from levain.qbroker import fence as F
from levain.qbroker import fence_server as FS
from levain.qbroker import wire
from levain.qbroker.broker import FenceUnavailable, Resolved

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import AGENT, revoke_agent  # noqa: E402
from tests.test_qbroker_fence import (L, SESSION, clock, fence_run, go2, keys, resolver, rig,  # noqa: E402,F401
                                      serve_fence, world)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

ME = os.geteuid()
OTHER = ME + 100_000            # a uid no process here has
RUN = {"run_id": "r-0123456789abcdef"}


def who(session=SESSION, run=RUN):
    return Resolved(session=session, kind="dispatch" if run is not None else "interactive", run=run)


@pytest.fixture
def served(rig, fence_run):
    srv = serve_fence(rig, fence_run)
    yield srv, F.RemoteFences(F.fence_socket_path(fence_run), ME)
    srv.stop()


def raw(path, payload: bytes, *, timeout=5.0):
    """``payload`` sent as is on a fresh connection; the server's answer frame, or None when it closed unanswered."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(payload)
        return wire.read_frame(s, timeout=timeout)


def frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


def bar(rig):
    rig.conn.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES ('b1', ?, 't', 1)",
                     (AGENT,))


# --- the verdicts: AgentFences' own, carried over the socket ------------------------------------------------------

def test_go_then_fenced_after_a_bar_through_a_real_broker_gate(rig, served):
    _srv, client = served
    b = rig.broker(fence=client)
    assert go2(b)[0]["gate"] == "go"
    bar(rig)
    got = go2(b)[0]
    assert (got["gate"], got["reason"]) == ("fenced", "the agent is barred")
    assert rig.calls()["calls"] == 1


def test_a_revoke_is_fenced_through_the_server(rig, served):
    _srv, client = served
    assert client(who()) is None
    rig.world.link(revoke_agent())
    assert "no longer live" in client(who())


def test_a_session_with_no_agent_row_is_current_as_agent_fences_answers_it(rig, served):
    _srv, client = served
    other = who("s-fedcba9876543210")
    assert rig.fences(other) is None and client(other) is None


def test_an_interactive_session_is_never_asked(served, monkeypatch):
    srv, client = served
    monkeypatch.setattr(srv, "fences", lambda w: pytest.fail("asked"))
    assert client(who(run=None)) is None


# --- refusals: every one is "cannot decide" (limits-failed), never go ---------------------------------------------

def test_the_server_down_is_limits_failed(rig, fence_run):
    client = F.RemoteFences(F.fence_socket_path(fence_run), ME)
    got = go2(rig.broker(fence=client))[0]
    assert got["gate"] == "limits-failed" and "the agent fence is not running (FileNotFoundError)" in got["reason"]
    assert rig.calls()["known"] is False


def test_a_stale_socket_with_no_server_is_limits_failed(fence_run):
    path = F.fence_socket_path(fence_run)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()                       # the path stays, nothing listens
    with pytest.raises(FenceUnavailable, match="not running"):
        F.RemoteFences(path, ME)(who())


@pytest.mark.parametrize("accepts", [False, True])
def test_a_stalled_server_times_out_and_is_limits_failed(rig, fence_run, accepts):
    path = F.fence_socket_path(fence_run)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(4)
    held = []
    if accepts:                     # reads the request, never answers
        threading.Thread(target=lambda: held.append(s.accept()), daemon=True).start()
    try:
        t = time.monotonic()
        got = go2(rig.broker(fence=F.RemoteFences(path, ME, timeout_s=0.3)))[0]
        took = time.monotonic() - t
    finally:
        s.close()
    assert got["gate"] == "limits-failed" and "did not answer within 0.3 s" in got["reason"]
    assert took < 2.0


def test_a_server_of_another_uid_is_refused_before_the_session_is_sent(served, monkeypatch):
    srv, _ = served
    asked = []
    monkeypatch.setattr(srv, "handle", lambda role, m: asked.append(m) or {})
    with pytest.raises(FenceUnavailable, match=f"served by uid {ME}, not the operator's uid {OTHER}"):
        F.RemoteFences(srv.path, OTHER)(who())
    time.sleep(0.05)
    assert asked == []


def test_a_peer_of_another_uid_is_closed_unread_and_unanswered(rig, fence_run):
    srv = serve_fence(rig, fence_run, broker_uid=OTHER, operator_uid=OTHER + 1)
    try:
        with pytest.raises(FenceUnavailable, match="closed the connection without an answer"):
            F.RemoteFences(srv.path, ME)(who())
        assert srv.refused_peers == 1
    finally:
        srv.stop()


def test_a_peer_credential_that_cannot_be_read_is_refused_on_both_ends(rig, fence_run):
    # the one seam the uid checks pass through (levain.qbroker.peercred.peer_uid), failing
    def unreadable(sock):
        raise OSError("no credential")
    srv = serve_fence(rig, fence_run, peer_uid=unreadable)
    try:
        with pytest.raises(FenceUnavailable, match="closed the connection without an answer"):
            F.RemoteFences(srv.path, ME)(who())
        with pytest.raises(FenceUnavailable, match="peer cannot be read"):
            F.RemoteFences(srv.path, ME, peer_uid=unreadable)(who())
    finally:
        srv.stop()


@pytest.mark.parametrize("n", [F.REQUEST_MAX + 1, 300, wire.MAX_FRAME])
def test_a_frame_over_256_bytes_is_refused_before_its_body_is_read(served, n):
    srv, _ = served
    got = raw(srv.path, struct.pack(">I", n))          # the length alone: the body is never awaited
    assert got == {"id": None, "ok": False, "refused": "frame-too-large"}


def test_a_garbled_frame_is_refused(served):
    srv, _ = served
    assert raw(srv.path, frame(b'{"id":1, "op":"FENCE"}'))["refused"] == "frame-malformed"
    assert raw(srv.path, frame(b"\xff\xfe"))["refused"] == "frame-malformed"


@pytest.mark.parametrize("session", ["s-0123456789ABCDEF", "s-0123456789abcde", "s-0123456789abcdef0", "../../etc",
                                     "s-0123456789abcdeg", "", 7, None, ["s-0123456789abcdef"]])
def test_a_session_not_s1_shaped_is_refused(served, session, monkeypatch):
    srv, _ = served
    monkeypatch.setattr(srv, "fences", lambda w: pytest.fail("asked"))
    got = srv.handle("broker", {"v": 1, "op": "FENCE", "id": 4, "session": session})
    assert got == {"id": 4, "ok": False, "refused": "bad-session"}


def test_the_client_never_sends_a_session_not_s1_shaped(served):
    _, client = served
    with pytest.raises(FenceUnavailable, match="not an S1 session id"):
        client(who("sess-0"))


@pytest.mark.parametrize("message", [
    {"v": 1, "op": "FENCE", "id": 1, "session": SESSION, "run": "x"},
    {"v": 1, "op": "FENCE", "id": 1},
    {"v": 2, "op": "FENCE", "id": 1, "session": SESSION},
    {"v": True, "op": "FENCE", "id": 1, "session": SESSION},
    {"v": 1, "op": "FENCE", "id": -1, "session": SESSION},
    {"v": 1, "op": "FENCE", "id": True, "session": SESSION},
    {"v": 1, "op": 3, "id": 1, "session": SESSION},
    ["FENCE"],
])
def test_a_request_off_its_shape_is_refused(served, message):
    srv, _ = served
    assert srv.handle("broker", message)["refused"] == "bad-request"


def test_each_verb_is_answered_to_its_one_peer_only(served):
    srv, _ = served
    assert srv.handle("broker", {"v": 1, "op": "STATUS", "id": 1})["refused"] == "not-permitted"
    assert srv.handle("operator", {"v": 1, "op": "FENCE", "id": 1, "session": SESSION})["refused"] == "not-permitted"
    assert srv.handle("broker", {"v": 1, "op": "REVOKE", "id": 1})["refused"] == "not-permitted"


def test_status_over_the_socket_from_the_operator_uid_only(rig, fence_run):
    srv = serve_fence(rig, fence_run, broker_uid=OTHER, operator_uid=ME)
    try:
        got = raw(srv.path, wire.encode({"v": 1, "op": "STATUS", "id": 9}))
        assert got["ok"] is True and got["store"] == str(rig.db.parent) and type(got["seq"]) is int
        assert (got["store_error"], got["authority_error"], got["last_error"]) == (None, None, None)
        assert raw(srv.path, wire.encode({"v": 1, "op": "FENCE", "id": 1, "session": SESSION}))["refused"] \
            == "not-permitted"
    finally:
        srv.stop()


def test_status_names_what_the_server_cannot_read(rig, fence_run, tmp_path):
    srv = FS.FenceServer(rig.fences, F.fence_socket_path(fence_run), broker_uid=OTHER, operator_uid=ME,
                         expected_gid=0, store=tmp_path / "nope", cockpit_root=tmp_path / "nope2")
    got = srv.status()
    assert got["store_error"] and "OperationalError" in got["store_error"]
    assert got["authority_error"] == "no authority anchor" and got["seq"] is None


# --- what crosses back: bounded, printable, and only AgentFences' own words -----------------------------------------

def test_a_fence_that_cannot_decide_is_refused_with_its_why_and_remembered(served, monkeypatch):
    srv, client = served

    def unavailable(w):
        raise FenceUnavailable("more than one agent occurrence names session " + w.session)
    monkeypatch.setattr(srv, "fences", unavailable)
    with pytest.raises(FenceUnavailable) as exc:
        client(who())
    assert str(exc.value) == f"the agent fence refused (fence-unavailable): more than one agent occurrence names " \
                             f"session {SESSION}"
    assert srv.last_error.startswith("more than one agent occurrence")


def test_a_fence_that_raises_sends_its_type_only(served, monkeypatch):
    srv, client = served

    def boom(w):
        raise RuntimeError("/Users/someone/.levain/autonomic secret path")
    monkeypatch.setattr(srv, "fences", boom)
    with pytest.raises(FenceUnavailable) as exc:
        client(who())
    assert "RuntimeError" in str(exc.value) and "secret" not in str(exc.value)


@pytest.mark.parametrize("reason", ["", 3])
def test_a_fence_answering_garbage_is_refused(served, monkeypatch, reason):
    srv, client = served
    monkeypatch.setattr(srv, "fences", lambda w: reason)
    with pytest.raises(FenceUnavailable, match="neither current nor a reason"):
        client(who())


def test_a_reason_is_sent_printable_and_bounded(served, monkeypatch):
    srv, client = served
    monkeypatch.setattr(srv, "fences", lambda w: "barred\n\x1b[31m" + "x" * 500)
    got = client(who())
    assert got.startswith("barred??[31m") and len(got) == F.TEXT_MAX and F.TEXT_RE.fullmatch(got)


@pytest.mark.parametrize("answer", [
    None, [], {"id": 2, "ok": True, "current": True}, {"id": 1, "ok": 1, "current": True},
    {"id": True, "ok": True, "current": True}, {"id": 1, "ok": True, "current": False},
    {"id": 1, "ok": True, "current": 1}, {"id": 1, "ok": True, "current": True, "fenced": "x"},
    {"id": 1, "ok": True, "fenced": ""}, {"id": 1, "ok": True, "fenced": "a\nb"},
    {"id": 1, "ok": True, "fenced": "x" * (F.TEXT_MAX + 1)}, {"id": 1, "ok": True},
    {"id": 1, "ok": False, "refused": "Bad Name"}, {"id": 1, "ok": False, "refused": "x", "why": "a\x00"},
    {"id": 1, "ok": False, "refused": "x", "extra": 1}, {"id": 1, "ok": False},
])
def test_the_client_refuses_every_answer_off_its_shape(answer):
    with pytest.raises(FenceUnavailable):
        F.RemoteFences._verdict(1, answer)


def test_the_client_reads_the_two_verdicts():
    assert F.RemoteFences._verdict(1, {"id": 1, "ok": True, "current": True}) is None
    assert F.RemoteFences._verdict(1, {"id": 1, "ok": True, "fenced": "the agent is barred"}) == "the agent is barred"


def test_an_answer_over_512_bytes_is_refused(fence_run):
    path = F.fence_socket_path(fence_run)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)

    def answer():
        c, _ = s.accept()
        wire.read_frame(c, timeout=5)
        c.sendall(struct.pack(">I", F.ANSWER_MAX + 1) + b"x" * (F.ANSWER_MAX + 1))
        c.close()
    t = threading.Thread(target=answer, daemon=True)
    t.start()
    try:
        with pytest.raises(FenceUnavailable, match="frame-too-large"):
            F.RemoteFences(path, ME)(who())
    finally:
        t.join(5)
        s.close()


# --- the listener -------------------------------------------------------------------------------------------------

def test_a_client_that_never_sends_is_dropped(rig, fence_run):
    srv = serve_fence(rig, fence_run, read_timeout=0.2)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(srv.path))
            t = time.monotonic()
            assert s.recv(10) == b""
            assert time.monotonic() - t < 2.0
    finally:
        srv.stop()


def test_one_connection_past_the_bound_is_closed_and_is_limits_failed(rig, fence_run):
    srv = serve_fence(rig, fence_run, max_connections=1, read_timeout=5.0)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as hog:
            hog.connect(str(srv.path))
            time.sleep(0.1)
            with pytest.raises(FenceUnavailable):
                F.RemoteFences(srv.path, ME)(who())
    finally:
        srv.stop()


def test_the_socket_is_0660_and_unlinked_on_stop(rig, fence_run):
    srv = serve_fence(rig, fence_run)
    st = os.lstat(srv.path)
    assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o660
    srv.stop()
    assert not srv.path.exists()


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o700, 0o751])
def test_a_fence_dir_not_0750_refuses_to_serve(rig, fence_run, mode):
    os.chmod(fence_run / "fence", mode)
    with pytest.raises(PermissionError, match="not 0750"):
        serve_fence(rig, fence_run)
    assert not F.fence_socket_path(fence_run).exists()


def test_a_fence_dir_in_another_group_refuses_to_serve(rig, fence_run):
    gid = os.stat(fence_run / "fence").st_gid
    with pytest.raises(PermissionError, match="is in group"):
        FS.FenceServer(rig.fences, F.fence_socket_path(fence_run), broker_uid=ME, operator_uid=OTHER,
                       expected_gid=gid + 1, store=rig.db.parent, cockpit_root=rig.world.root).start()


def test_a_fence_dir_that_is_a_link_refuses_to_serve(rig, fence_run):
    real = fence_run / "real"
    (fence_run / "fence").rename(real)
    (fence_run / "fence").symlink_to(real)
    with pytest.raises(PermissionError, match="not a directory"):
        serve_fence(rig, fence_run)


def test_the_dir_shape(tmp_path):
    st = os.stat(tmp_path)
    fake = lambda mode, uid=ME, gid=st.st_gid: os.stat_result((stat.S_IFDIR | mode, 0, 0, 0, uid, gid, 0, 0, 0, 0))
    assert FS.dir_problem(fake(0o750), owner=ME, gid=st.st_gid) is None
    assert FS.dir_problem(fake(0o2750), owner=ME, gid=st.st_gid) is None
    assert "owned by" in FS.dir_problem(fake(0o750, uid=0), owner=ME, gid=st.st_gid)


def test_a_file_at_the_socket_path_refuses(rig, fence_run):
    F.fence_socket_path(fence_run).write_text("x")
    with pytest.raises(FileExistsError):
        serve_fence(rig, fence_run)


def test_one_uid_for_broker_and_operator_is_refused(rig, fence_run):
    with pytest.raises(ValueError):
        serve_fence(rig, fence_run, broker_uid=ME, operator_uid=ME)


# --- main and the unit --------------------------------------------------------------------------------------------

def test_main_never_runs_as_root(monkeypatch):
    monkeypatch.setattr(FS.os, "geteuid", lambda: 0)
    assert FS.main(["--agent-store", "/x", "--cockpit-root", "/y"]) == FS.EX_NOPERM


def test_main_without_the_broker_account_exits_config(monkeypatch):
    def missing(name):
        raise KeyError(name)
    monkeypatch.setattr(FS.pwd, "getpwnam", missing)
    assert FS.main(["--agent-store", "/x", "--cockpit-root", "/y"]) == FS.EX_CONFIG


def test_main_refuses_a_fence_dir_it_cannot_find(monkeypatch, tmp_path):
    import grp
    import pwd
    monkeypatch.setattr(FS.pwd, "getpwnam", lambda n: pwd.struct_passwd(("q", "*", OTHER, 0, "", "/", "/bin/sh")))
    monkeypatch.setattr(FS.grp, "getgrnam", lambda n: grp.struct_group(("q", "*", os.getegid(), [])))
    assert FS.main(["--agent-store", str(tmp_path), "--cockpit-root", str(tmp_path), "--run", str(tmp_path / "R")]) \
        == FS.EX_NOPERM


def test_the_timeout_covers_a_refresh_and_ends_before_the_hooks():
    # a refresh's busy wait plus the fence's own store waits fit; the hook's whole GATE deadline is not reached first
    assert agent_authority._REFRESH_BUSY_TIMEOUT + 2 * F.FENCE_DB_TIMEOUT < F.FENCE_TIMEOUT_S < broker_client.IO_TIMEOUT


def test_the_fence_unit_is_resident_per_user_and_runs_the_server(tmp_path):
    spec = daemon.build_fence_spec(agent_store=tmp_path / "a", cockpit_root=tmp_path / "c", log_dir=tmp_path)
    assert spec.label == daemon.DEFAULT_FENCE_LABEL and spec.keep_alive and spec.run_at_login
    assert spec.start_interval is None
    assert spec.argv[:3] == [sys.executable, "-m", "levain.qbroker.fence_server"] == [sys.executable, "-m",
                                                                                     FS.__name__]
    assert spec.argv[3:] == ["--agent-store", str((tmp_path / "a").resolve()),
                             "--cockpit-root", str((tmp_path / "c").resolve())]
    args = FS.parse_args(spec.argv[3:])
    assert (args.agent_store, args.cockpit_root) == (spec.argv[4], spec.argv[6])
    with_run = daemon.build_fence_spec(agent_store=tmp_path / "a", cockpit_root=tmp_path / "c", run_dir=Path("/r"))
    assert with_run.argv[-2:] == ["--run", "/r"]


def test_the_fence_socket_is_under_R():
    assert F.fence_socket_path("/run/levain") == Path("/run/levain/fence/fence.sock")
