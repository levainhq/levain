"""P7-1 S10b slice B2: the operator-side client SYNCs the dispatch server's authority replica before LAUNCH and RELEASE,
and ``main`` no longer reads the operator's home.

The client is the REAL ``DispatchSessionLauncher`` with an ``AuthorityFeed``, the server the REAL ``RunServer`` over a
replica-backed verifier (the production shape), both in-process over socketpairs. Not held here (VM): the real cross-uid frame.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from levain.agents import bars
from levain.agents import controller as CTL
from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_wire as W
from levain.slot.services import dispatch_run as D
from tests.test_agents_authority_replica import Wire, _source, rig  # noqa: F401  (rig is the fixture)
from tests.test_agents_session_launch import EXP, _occurrence
from tests.test_authority_agent_link import revoke_agent

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def wire(rig, tmp_path):
    w = Wire(rig, tmp_path)
    yield w
    w.close()


def _client(rig, wire):
    return C.DispatchSessionLauncher(wire.conn, lambda occ: C.link_proof(rig.store, rig.world.root, occ, now=rig.now()),
                                     C.AuthorityFeed(rig.store, rig.world.root))


def _launch(oid, key="s-00000000000000d1"):
    return S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP)


def test_the_client_syncs_an_empty_replica_then_launches_and_releases(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    assert wire.replica.head() == (0, "")
    child = _client(rig, wire).spawn(_launch(oid))
    assert child.session == "sess-1" and wire.replica.head()[0] == wire.head()["seq"]
    child.release()
    assert child.released is True and wire.runtime.children[0].released is True
    child._conn.close()


def test_the_client_syncs_in_chunks_when_the_ledger_is_large(rig, wire, monkeypatch):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    monkeypatch.setattr(C, "_SYNC_CHUNK_ROWS", 1)
    sent = []
    real = C._Conn.request

    def spy(self, op, expect, **kw):
        if op == "SYNC" and kw["rows"]:
            sent.append(len(kw["rows"]))
        return real(self, op, expect, **kw)
    monkeypatch.setattr(C._Conn, "request", spy)
    child = _client(rig, wire).spawn(_launch(oid))
    assert len(sent) == len(_source(rig)) and set(sent) == {1} and len(sent) > 1
    child._conn.close()


def test_a_revoke_appended_between_launch_and_release_refuses_at_release(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = _client(rig, wire).spawn(_launch(oid))
    rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid))             # signed after LAUNCH
    with pytest.raises(RuntimeError, match="link-not-standing"):                # the client SYNCed it; the server re-verified
        child.release()
    assert wire.replica.head()[0] == wire.head()["seq"]
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    child.abort()
    child._conn.close()


def test_ledger_stale_is_already_synced_after_one_more_head_read(rig, wire, monkeypatch):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    real = C._Conn.request
    state = {"n": 0}

    def spy(self, op, expect, **kw):
        if op == "SYNC" and kw["rows"] and state["n"] == 0:
            state["n"] = 1
            wire.sync()                                      # another fire SYNCs the same rows first
        return real(self, op, expect, **kw)
    monkeypatch.setattr(C._Conn, "request", spy)
    child = _client(rig, wire).spawn(_launch(oid))
    assert state["n"] == 1 and child.session == "sess-1"
    child._conn.close()


def test_a_server_ledger_that_forked_is_a_named_standing_refusal(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    # a different operator ledger at the same seq: the replica's own head is not on this chain
    rows = _source(rig)
    db = sqlite3.connect(wire.replica.db_path)
    db.execute("UPDATE authority_log SET hash = ? WHERE seq = ?", ("0" * 64, rows[-1].seq))
    db.commit()
    db.close()
    with pytest.raises(C.LauncherRefused) as exc:
        _client(rig, wire).spawn(_launch(oid))
    assert (exc.value.name, exc.value.reason_class) == ("ledger-fork", "standing")
    assert wire.runtime.children == []


def test_a_server_ahead_of_the_client_on_another_chain_is_a_standing_fork(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    feed = C.AuthorityFeed(rig.store, rig.world.root)
    real = feed.read

    def stale(occ):
        r = real(occ)
        return C.AuthorityRead((r.head[0] - 1, "x"), r.bar)
    feed.read = stale
    cl = C.DispatchSessionLauncher(wire.conn, lambda occ: C.link_proof(rig.store, rig.world.root, occ, now=rig.now()), feed)
    with pytest.raises(C.LauncherRefused) as exc:
        cl.spawn(_launch(oid))
    assert exc.value.name == "ledger-fork" and exc.value.reason_class == "standing"
    assert wire.runtime.children == []


def _other_fire_before_release(rig, wire, monkeypatch, record=None):
    """Another fire signs ``record`` (None: a new, unrelated link that bars nothing) and SYNCs it after this fire's own SYNC, right before its RELEASE frame."""
    real = C._Conn.request
    state = {"n": 0}

    def spy(self, op, expect, **kw):
        if op == "RELEASE" and state["n"] == 0:
            state["n"] = 1
            rig.link() if record is None else rig.world.link(record)
            assert wire.sync()["ok"] is True
        return real(self, op, expect, **kw)
    monkeypatch.setattr(C._Conn, "request", spy)
    return state


def test_another_fires_sync_between_this_fires_sync_and_release_does_not_end_its_session(rig, wire, monkeypatch):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = _client(rig, wire).spawn(_launch(oid))
    state = _other_fire_before_release(rig, wire, monkeypatch)
    child.release()
    assert state["n"] == 1 and child.released is True and wire.runtime.ended == []
    child._conn.close()


def test_a_fire_whose_read_predates_another_fires_sync_still_releases(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    feed = C.AuthorityFeed(rig.store, rig.world.root)
    cl = C.DispatchSessionLauncher(wire.conn, lambda occ: C.link_proof(rig.store, rig.world.root, occ, now=rig.now()), feed)
    child = cl.spawn(_launch(oid))
    before = feed.read(oid)
    rig.link()                                                                  # the other fire's row, SYNCed by it
    assert wire.sync()["ok"] is True
    feed.read = lambda occ: before                                              # this fire read its head before them
    child.release()
    assert child.released is True and wire.runtime.ended == []
    child._conn.close()


def test_a_revoke_of_this_agent_synced_by_another_fire_before_release_still_refuses(rig, wire, monkeypatch):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = _client(rig, wire).spawn(_launch(oid))
    _other_fire_before_release(rig, wire, monkeypatch, revoke_agent(rig.row(oid)["agent_id"], rig.sid))
    with pytest.raises(RuntimeError, match="link-not-standing"):
        child.release()
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    child.abort()
    child._conn.close()


# --- L3 r1 fixes (RETURN_s10b_b2_fix) ----------------------------------------------------------------------------------

def _open_bar(rig, agent_id):
    """A revoke's first step only: the agent store's open bar, no ledger row (revoke_agent_begin before its append)."""
    db = sqlite3.connect(rig.store.directory / "autonomic.db", isolation_level=None)
    try:
        bars.open_bar(db, agent_id, head_seq=0, now=rig.now())
    finally:
        db.close()


def test_a_bar_opened_after_launch_without_a_ledger_row_refuses_release(rig, wire):
    # codex r1 MED / complement 4: RELEASE judged the LAUNCH-time bar, so a bar opened in between was not seen
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = _client(rig, wire).spawn(_launch(oid))
    _open_bar(rig, rig.row(oid)["agent_id"])
    with pytest.raises(RuntimeError, match="agent-barred"):
        child.release()
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    child.abort()
    child._conn.close()


def test_an_open_bar_at_launch_surfaces_as_its_named_standing_refusal_not_a_down_child(rig, wire):
    # deepseek r1 3: a classified refusal of LAUNCH is the operator's to see, never a lost answer
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    _open_bar(rig, rig.row(oid)["agent_id"])
    with pytest.raises(C.LauncherRefused) as exc:
        _client(rig, wire).spawn(_launch(oid))
    assert (exc.value.name, exc.value.reason_class) == ("agent-barred", "standing")
    assert wire.runtime.children == []


def _bad_sync_answers(monkeypatch, bad):
    """From now on every SYNC is answered ``bad`` after a well-shaped frame (the wire passed; the fields did not)."""
    real = C._Conn.request

    def spy(self, op, expect, **kw):
        if op == "SYNC":
            return bad
        return real(self, op, expect, **kw)
    monkeypatch.setattr(C._Conn, "request", spy)


@pytest.mark.parametrize("bad", [{"head": {"seq": "1", "hash": ""}}, {}], ids=["wire-error", "key-error"])
def test_a_malformed_sync_answer_at_release_raises_runtime_error_and_loses_the_child(rig, wire, monkeypatch, bad):
    # complement r1 1 / deepseek r1 1: a WireError or KeyError out of _sync_to escaped release() raw
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = _client(rig, wire).spawn(_launch(oid))
    _bad_sync_answers(monkeypatch, bad)
    with pytest.raises(RuntimeError, match="unreachable at RELEASE"):
        child.release()
    assert child.released is False and child._conn.alive is False
    assert child.process.returncode == CTL.GATE_UNPROVEN


@pytest.mark.parametrize("bad", [{"head": {"seq": "1", "hash": ""}}, {}], ids=["wire-error", "key-error"])
def test_a_malformed_sync_answer_at_launch_is_a_transient_refusal(rig, wire, monkeypatch, bad):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    _bad_sync_answers(monkeypatch, bad)
    with pytest.raises(C.LauncherRefused) as exc:
        _client(rig, wire).spawn(_launch(oid))
    assert (exc.value.name, exc.value.reason_class) == ("dispatch-unreachable", "transient")
    assert wire.runtime.children == []


@pytest.mark.parametrize("value", ["abc", b"\x01"], ids=["text", "blob"])
def test_a_closed_bar_whose_revoke_seq_is_not_an_integer_is_proof_unavailable(rig, value):
    # complement r1 2 / codex r1 2: int(closed) sat outside the guarded read
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    db = sqlite3.connect(rig.store.directory / "autonomic.db", isolation_level=None)
    try:
        db.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar, revoke_seq, closed_at)"
                   " VALUES ('bar-x', ?, '2026-01-01T00:00:00Z', 0, ?, '2026-01-01T00:00:01Z')",
                   (rig.row(oid)["agent_id"], value))
    finally:
        db.close()
    with pytest.raises(C.ProofUnavailable) as exc:
        C.AuthorityFeed(rig.store, rig.world.root).read(oid)
    assert exc.value.standing is True


def test_the_launch_frame_size_check_uses_worst_case_claims(rig, wire, monkeypatch):
    # complement r1 6: seq 1 placeholders let a body near the frame limit pass, then overflow with the real values
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    seen = []
    real = C.encode_run_frame

    def spy(obj):
        if obj.get("op") == "LAUNCH" and obj.get("id") == 1 and "ledger_head" in obj:
            seen.append((obj["ledger_head"], obj["bar"]))
        return real(obj)
    monkeypatch.setattr(C, "encode_run_frame", spy)
    child = _client(rig, wire).spawn(_launch(oid))
    head, bar = seen[0]
    assert head["seq"] == bar["closed_revoke_seq"] == C._INT_MAX and len(head["hash"]) == 128
    child._conn.close()


def test_the_registered_launcher_builds_with_the_feed(rig):
    W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher=C.LAUNCHER_NAME)
    got = W.resolve_launcher(rig.store, W.read_install(rig.store.directory))
    assert isinstance(got._feed, C.AuthorityFeed)


def test_main_takes_neither_the_agent_store_nor_the_cockpit_root_and_opens_nothing_of_the_operators_home(tmp_path, monkeypatch):
    ap = D._parser("darwin")
    for flag in ("--agent-store-dir", "--cockpit-root"):
        with pytest.raises(SystemExit):
            ap.parse_args([flag, "/x"])
    opened = []
    import builtins
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    import pwd
    home_calls = []
    real_pw = pwd.getpwuid
    monkeypatch.setattr(pwd, "getpwuid", lambda u: (home_calls.append(u), real_pw(u))[1])
    seen = {}
    monkeypatch.setattr(D, "build_server", lambda host, store, ctl, uid, *a, **k: seen.update(args=(store, ctl, uid, a, k)) or (_ for _ in ()).throw(OSError("stop")))
    monkeypatch.setattr(D.os, "geteuid", lambda: 4242)
    monkeypatch.setattr(D.pwd, "getpwnam", lambda n: type("P", (), {"pw_uid": 4242})())
    rc = D.main(["--operator-uid", "501", "--store-dir", str(tmp_path / "L"), "--control", str(tmp_path / "c.sock"),
                 "--socket", str(tmp_path / "r.sock")])
    assert rc == D.EX_CONFIG
    store, ctl, uid, a, k = seen["args"]
    assert (uid, a, k) == (501, (), {"brokered": False}) and home_calls == []   # a non-brokered start names brokered=False
    home = Path.home()
    assert not [p for p in opened if p.startswith(str(home / ".levain"))]
    assert not [x for x in (str(store), str(ctl), *sys.argv) if "/.levain/autonomic" in x]
