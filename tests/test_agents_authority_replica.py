"""P7-1 S10b slice B1: the dispatch authority REPLICA (levain.agents.authority_replica) and the run server's SYNC op.

Every ledger is a real signed one (the ``World`` of the agent-link tests: ssh-keygen keys, links verified through the
replay). Held: a replica built from a source ledger replays to the same ``AuthoritySnapshot``; a continuation appends;
a fork, a bad signature, a stale or rolled-back SYNC is refused with the replica unchanged; a verify that dies mid-append
commits nothing (also across a real process death); concurrent SYNCs serialise; the server's LAUNCH/RELEASE take the
ledger head and the bar from the frame and judge against the replica.

UNVERIFIED here (VM rows): real ``ssh-keygen`` under ``_levain_dispatch``, the cross-uid frame, the unit under launchd.
"""
from __future__ import annotations

import dataclasses
import hashlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import levain.authority as auth
from levain.agents import authority_replica as AR
from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_launcher as L
from levain.slot.services import dispatch_run as D
from tests.test_agents_launch import Rig
from tests.test_agents_session_launch import EXP, _occurrence
from tests.test_agents_session_launcher import FakeControl, FakeRuntime, FakeSources
from tests.test_authority_agent_link import revoke_agent

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

REPO = Path(__file__).resolve().parent.parent
NO_BAR = {"open": False, "closed_revoke_seq": 0}


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


def _rows(db: Path) -> list:
    conn = sqlite3.connect(db)
    try:
        return [AR.ReplicaRow(*r) for r in conn.execute(
            "SELECT seq, prev_hash, record, signer_fp, signature, hash FROM authority_log ORDER BY seq")]
    finally:
        conn.close()


def _source(rig) -> list:
    return _rows(rig.world.db)


def _replica(tmp_path, name="replica") -> AR.AuthorityReplica:
    return AR.AuthorityReplica(tmp_path / name / "authority")


def _stored(replica) -> list:
    return _rows(replica.db_path) if replica.db_path.exists() else []


def _anchor_bytes(replica) -> bytes:
    p = auth.anchor_path(replica.root)
    return p.read_bytes() if p.exists() else b""


def _refused(replica, rows, name):
    before = (_stored(replica), _anchor_bytes(replica))
    with pytest.raises(AR.ReplicaRefused) as exc:
        replica.append(rows)
    assert exc.value.name == name, exc.value
    assert (_stored(replica), _anchor_bytes(replica)) == before          # the replica is exactly what it was
    return exc.value


def _with_link(rig):
    rig.link()
    return _source(rig)


# --- a replica replays to its source ---------------------------------------------------------------------------------

def test_a_replica_built_from_a_signed_ledger_replays_to_the_same_snapshot_as_its_source(rig, tmp_path):
    rig.link()
    rows = _source(rig)
    replica = _replica(tmp_path)
    assert replica.head() == (0, "")
    head = replica.append(rows)
    assert head == (rows[-1].seq, rows[-1].hash) == replica.head()
    src, got = rig.authority.replay(), replica.replay()
    assert got == src and (got.seq, got.head_hash, got.store_id) == (src.seq, src.head_hash, src.store_id)
    assert replica.authority().replay() == src
    # the anchor is the replica's own, at the head
    assert auth.read_anchor(auth.anchor_path(replica.root)) == auth.Anchor(src.store_id, src.seq, src.head_hash)
    assert (replica.root.stat().st_mode & 0o777, replica.db_path.stat().st_mode & 0o777) == (0o700, 0o600)


def test_a_continuation_appends_and_an_empty_sync_reads_the_head(rig, tmp_path):
    rows = _source(rig)
    replica = _replica(tmp_path)
    cut = len(rows) // 2
    assert replica.append(rows[:cut]) == (rows[cut - 1].seq, rows[cut - 1].hash)
    assert replica.append([]) == replica.head() == (rows[cut - 1].seq, rows[cut - 1].hash)
    rig.link()                                                  # the source grows while the replica lags
    more = _source(rig)
    assert replica.append(more[cut:]) == (more[-1].seq, more[-1].hash)
    assert replica.replay() == rig.authority.replay()


def test_a_sync_on_an_empty_replica_with_no_rows_is_refused_and_creates_no_ledger(tmp_path):
    replica = _replica(tmp_path)
    with pytest.raises(AR.ReplicaRefused) as exc:
        replica.append([])
    assert (exc.value.name, exc.value.reason_class) == ("ledger-empty", "transient")
    assert replica.head() == (0, "")
    with pytest.raises(Exception):
        replica.replay()


# --- what the replica refuses ----------------------------------------------------------------------------------------

def test_a_stale_resend_and_a_gap_are_refused_transient_and_change_nothing(rig, tmp_path):
    rig.link()
    rows = _source(rig)
    assert len(rows) >= 5
    replica = _replica(tmp_path)
    replica.append(rows[:3])
    assert _refused(replica, rows[:3], "ledger-stale").reason_class == "transient"     # rows it already holds
    assert _refused(replica, rows[4:], "ledger-gap").reason_class == "transient"       # seq 4 is missing
    holes = [rows[3], dataclasses.replace(rows[4], seq=rows[4].seq + 1)]
    assert _refused(replica, holes, "ledger-gap").reason_class == "transient"          # not consecutive
    assert replica.append(rows[3:]) == (rows[-1].seq, rows[-1].hash)


def test_a_different_row_at_a_held_seq_is_a_fork(rig, tmp_path):
    base = _source(rig)
    planted = _rows(rig.world.plant({"op": "enrol", "fingerprint": "SHA256:other", "public_key": "x",
                                     "class": "display-phone", "custody": "unstated"}))
    rig.link()                                                                # the genuine seq n+1
    rows = _source(rig)
    assert planted[-1].seq == rows[-1].seq and planted[-1].hash != rows[-1].hash and len(rows) == len(base) + 1
    replica = _replica(tmp_path)
    replica.append(rows)
    got = _refused(replica, planted[-1:], "ledger-fork")
    assert got.reason_class == "standing"
    forked_early = dataclasses.replace(rows[0], record=rows[0].record + " ")
    _refused(replica, [forked_early], "ledger-fork")


def test_a_row_that_does_not_link_to_the_replicas_chain_is_a_fork(rig, tmp_path):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    broken = dataclasses.replace(rows[-1], prev_hash="f" * 64)
    _refused(replica, [broken], "ledger-fork")
    assert replica.append(rows[-1:]) == (rows[-1].seq, rows[-1].hash)         # the honest row still goes on


def test_another_stores_ledger_is_a_fork_once_the_replica_holds_one(rig, tmp_path):
    mine = _source(rig)
    replica = _replica(tmp_path)
    replica.append(mine)
    (tmp_path / "other").mkdir()
    foreign = Rig(tmp_path / "other")                                         # another cockpit: another store, another keys
    foreign.link()
    fr = _source(foreign)
    assert fr[0].hash != mine[0].hash
    _refused(replica, fr, "ledger-fork")                                      # seq 1 differs from the held seq 1
    # and a foreign continuation: the foreign rows past our head also fail to link
    long = list(fr)
    if long[-1].seq > mine[-1].seq:
        _refused(replica, long[mine[-1].seq:], "ledger-fork")


def test_a_bad_signature_is_refused_and_the_replica_is_unchanged(rig, tmp_path):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    sig = rows[-1].signature
    flipped = sig.replace("A", "B", 3) if "A" in sig else sig[:-6] + "AAAAAA"
    got = _refused(replica, [dataclasses.replace(rows[-1], signature=flipped)], "ledger-unverifiable")
    assert got.reason_class == "standing"
    forged_record = rows[-1].record.replace("create_grant", "create_grant ", 1)
    digest = hashlib.sha256((rows[-1].prev_hash + forged_record).encode()).hexdigest()
    _refused(replica, [dataclasses.replace(rows[-1], record=forged_record, hash=digest)], "ledger-unverifiable")
    assert replica.append(rows[-1:]) == (rows[-1].seq, rows[-1].hash)         # the genuine row is still accepted


def test_a_garbled_first_ledger_is_never_adopted(rig, tmp_path):
    rows = _source(rig)
    replica = _replica(tmp_path)
    bad = list(rows)
    bad[1] = dataclasses.replace(bad[1], signature=bad[1].signature[:-8] + "AAAAAAAA")
    _refused(replica, bad, "ledger-unverifiable")
    assert replica.head() == (0, "")


def test_a_rollback_of_the_replica_itself_is_refused_by_its_anchor(rig, tmp_path):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows)
    conn = sqlite3.connect(replica.db_path)                                   # a restored older database
    conn.execute("DELETE FROM authority_log WHERE seq > ?", (rows[-1].seq - 1,))
    conn.commit()
    conn.close()
    assert replica.head()[0] == rows[-1].seq - 1
    with pytest.raises(Exception):
        replica.replay()                                                      # the verify path refuses a log behind its anchor
    kept = (_stored(replica), _anchor_bytes(replica))
    for attempt in ([], [dataclasses.replace(rows[-1], record=rows[-1].record + " ")]):
        with pytest.raises(AR.ReplicaRefused) as exc:
            replica.append(attempt)
        assert exc.value.name in ("ledger-fork", "ledger-unverifiable")
        assert (_stored(replica), _anchor_bytes(replica)) == kept             # the anchor never moved down either
    assert auth.read_anchor(auth.anchor_path(replica.root)).seq == rows[-1].seq


# --- the commit happens only after the verify -------------------------------------------------------------------------

def test_a_verify_that_dies_after_the_insert_commits_nothing(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    before = (_stored(replica), _anchor_bytes(replica))

    def boom(conn, **kw):
        assert conn.execute("SELECT MAX(seq) FROM authority_log").fetchone()[0] == rows[-1].seq   # inserted, uncommitted
        raise RuntimeError("verify died")

    monkeypatch.setattr(auth, "snapshot", boom)
    with pytest.raises(RuntimeError):
        replica.append(rows[-1:])
    monkeypatch.undo()
    assert (_stored(replica), _anchor_bytes(replica)) == before
    assert replica.append(rows[-1:]) == (rows[-1].seq, rows[-1].hash)


def test_a_process_that_dies_between_the_insert_and_the_commit_leaves_the_replica_as_it_was(rig, tmp_path):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    before = (_stored(replica), _anchor_bytes(replica))
    last = rows[-1]
    script = tmp_path / "die_midway.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        import levain.authority as auth
        from levain.agents import authority_replica as AR
        real = auth.snapshot
        def snap(conn, **kw):
            real(conn, **kw)
            os._exit(9)                       # verified, uncommitted: the process is gone
        auth.snapshot = snap
        row = AR.ReplicaRow({last.seq!r}, {last.prev_hash!r}, {last.record!r}, {last.signer_fp!r}, {last.signature!r}, {last.hash!r})
        AR.AuthorityReplica({str(replica.root)!r}).append([row])
        sys.exit(0)
        """))
    done = subprocess.run([sys.executable, str(script)], cwd=str(REPO), env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
                          timeout=60)
    assert done.returncode == 9
    assert (_stored(replica), _anchor_bytes(replica)) == before               # the journal rolled the insert back
    assert replica.head() == (rows[-2].seq, rows[-2].hash)
    assert replica.append(rows[-1:]) == (last.seq, last.hash)


def test_a_commit_whose_anchor_advance_fails_keeps_the_rows_and_the_next_sync_heals_the_anchor(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])

    def fail(path, snap):
        raise OSError("disk")

    monkeypatch.setattr(auth, "advance_anchor", fail)
    assert replica.append(rows[-1:]) == (rows[-1].seq, rows[-1].hash)         # committed, answered (r1: never "unchanged")
    monkeypatch.undo()
    assert replica.head() == (rows[-1].seq, rows[-1].hash)                    # committed, the anchor lags (never leads)
    assert auth.read_anchor(auth.anchor_path(replica.root)).seq == rows[-1].seq - 1
    assert replica.replay() == rig.authority.replay()                         # a lagging anchor is not a rollback
    assert replica.append([]) == replica.head()
    assert auth.read_anchor(auth.anchor_path(replica.root)).seq == rows[-1].seq


def test_concurrent_syncs_are_serialised_one_wins_the_rest_see_it(rig, tmp_path):
    rig.link()
    rows = _source(rig)
    replica = _replica(tmp_path)
    results: list = []
    gate = threading.Barrier(6)

    def go():
        gate.wait()
        try:
            results.append(replica.append(rows))
        except AR.ReplicaRefused as exc:
            results.append(exc.name)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    heads = [r for r in results if isinstance(r, tuple)]
    assert len(results) == 6 and len(heads) == 1 and heads[0] == (rows[-1].seq, rows[-1].hash), results
    assert {r for r in results if isinstance(r, str)} == {"ledger-stale"}
    assert replica.replay() == rig.authority.replay() and _stored(replica) == rows


def test_two_replica_objects_on_one_root_serialise_through_the_file_lock(rig, tmp_path):
    rows = _source(rig)
    a, b = _replica(tmp_path), AR.AuthorityReplica(_replica(tmp_path).root)
    out: list = []

    def go(r):
        try:
            out.append(r.append(rows))
        except AR.ReplicaRefused as exc:
            out.append(exc.name)

    ts = [threading.Thread(target=go, args=(r,)) for r in (a, b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert sorted(map(str, out)) == sorted([str((rows[-1].seq, rows[-1].hash)), "ledger-stale"])


def test_a_symlinked_or_foreign_root_is_refused(rig, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(AR.ReplicaRefused) as exc:
        AR.AuthorityReplica(tmp_path / "link").append(_source(rig))
    assert exc.value.name == "replica-not-ours"


# --- the run server: SYNC, ledger_head, bar ---------------------------------------------------------------------------

class Wire:
    """The run server over socketpairs, raw frames, a replica-backed verifier (the production shape)."""

    def __init__(self, rig, tmp_path, *, replica=True, runtime=None):
        self.rig = rig
        self.replica = _replica(tmp_path, "srv") if replica else None
        self.control, self.runtime = FakeControl(), runtime or FakeRuntime()
        launcher = self.launcher = L.RealLauncher(self.control, self.runtime, FakeSources(),
                                                  L.SessionIndex(tmp_path / "idx"))
        verify = D.replica_verifier(self.replica, now=rig.now) if replica else (lambda p, e, c: None)
        self.server = D.RunServer(launcher, verify=verify, pool_free=lambda: True, peer_ok=lambda s: True,
                                  holder_idle_s=20, replica=self.replica)
        self.threads: list = []
        self.n = 0

    def conn(self):
        a, b = socket.socketpair()
        a.settimeout(20)
        t = threading.Thread(target=self.server.handle, args=(b,), daemon=True)
        t.start()
        self.threads.append(t)
        return a

    def call(self, sock, op, **fields):
        self.n += 1
        sock.sendall(C.encode_run_frame({"p": C.PROTOCOL, "op": op, "id": self.n, **fields}))
        return C.read_run_frame(sock)

    def sync(self, rows=None, sock=None):
        own = sock is None
        sock = sock or self.conn()
        try:
            rows = [r for r in _source(self.rig) if r.seq > (self.replica.head()[0] if self.replica else 0)] if rows is None else rows
            return self.call(sock, "SYNC", rows=[dataclasses.asdict(r) for r in rows])
        finally:
            if own:
                sock.close()

    def launcher_index(self, key):
        """The launcher's index record for ``key`` ({} when none)."""
        return self.launcher.index.get(key) or {}

    def head(self):
        src = self.rig.authority.replay()
        return {"seq": src.seq, "hash": src.head_hash}

    def launch(self, sock, oid, key="s-00000000000000b1", **over):
        launch = S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP)
        proof = C.link_proof(self.rig.store, self.rig.world.root, oid, now=self.rig.now())
        frame = {**C.launch_to_wire(launch, proof), "ledger_head": self.head(),
                 "bar": {"open": False, "closed_revoke_seq": 0}}
        frame.update(over)
        return self.call(sock, "LAUNCH", **{k: v for k, v in frame.items() if v is not None})

    def close(self):
        for t in self.threads:
            t.join(10)


@pytest.fixture
def wire(rig, tmp_path):
    w = Wire(rig, tmp_path)
    yield w
    w.close()


def test_a_sync_over_the_wire_answers_the_head_and_the_replica_replays_to_the_source(rig, wire):
    rig.link()
    ans = wire.sync()
    assert ans["ok"] is True and ans["head"] == wire.head()
    assert wire.replica.replay() == rig.authority.replay()
    again = wire.sync([])                                                    # rows [] is how a client reads the head
    assert again["ok"] is True and again["head"] == wire.head()


def test_a_forked_or_unverifiable_sync_is_refused_over_the_wire_standing(rig, wire):
    rows = _source(rig)
    assert wire.sync(rows)["ok"] is True
    ans = wire.sync([dataclasses.replace(rows[-1], record=rows[-1].record + " ", hash="0" * 64)])
    assert (ans["ok"], ans["refused"]) == (False, "ledger-fork") and "reason_class" not in ans
    record = rows[-1].record + " "
    ans = wire.sync([dataclasses.replace(rows[-1], seq=rows[-1].seq + 1, prev_hash=rows[-1].hash, record=record,
                                         hash=hashlib.sha256((rows[-1].hash + record).encode()).hexdigest())])
    assert (ans["ok"], ans["refused"]) == (False, "ledger-unverifiable")
    stale = wire.sync(rows[-1:])
    assert (stale["refused"], stale["reason_class"]) == ("ledger-stale", "transient")
    assert wire.replica.head() == (rows[-1].seq, rows[-1].hash)


def test_a_malformed_sync_is_a_bad_request_and_a_server_without_a_replica_has_no_sync(rig, tmp_path, wire):
    sock = wire.conn()
    ans = wire.call(sock, "SYNC", rows=[{"seq": 1}])
    assert (ans["ok"], ans["refused"]) == (False, "bad-request")
    sock.close()
    bare = Wire(rig, tmp_path / "bare", replica=False)
    ans = bare.sync()
    assert (ans["ok"], ans["refused"]) == (False, "sync-unavailable")
    bare.close()


def test_launch_is_judged_against_the_replica_with_the_frames_head_and_bar(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    sock = wire.conn()
    ans = wire.launch(sock, oid)                                             # nothing synced yet
    assert (ans["refused"], ans["reason_class"]) == ("ledger-behind", "transient")
    assert wire.control.minted == {}
    wire.sync(sock=sock)
    ans = wire.launch(sock, oid, ledger_head={"seq": 1, "hash": "0" * 64})   # a held seq on another chain
    assert ans["refused"] == "ledger-fork" and "reason_class" not in ans
    assert wire.launch(sock, oid, bar={"open": True, "closed_revoke_seq": 0})["refused"] == "agent-barred"
    big = wire.head()["seq"] + 1
    assert wire.launch(sock, oid, bar={"open": False, "closed_revoke_seq": big})["refused"] == "bar-invalid"
    for missing in ({"bar": None}, {"ledger_head": None}):                   # a bad request closes the connection
        s2 = wire.conn()
        assert wire.launch(s2, oid, **missing)["refused"] == "bad-request"
        s2.close()
    assert wire.control.minted == {} and wire.runtime.starts == []
    ok = wire.launch(sock, oid)
    assert ok["ok"] is True and len(wire.control.minted) == 1
    sock.close()


def test_a_closed_bar_in_the_frame_kills_a_link_born_before_it(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    ans = wire.launch(sock, oid, bar={"open": False, "closed_revoke_seq": wire.head()["seq"]})
    assert ans["refused"] == "link-not-live"
    sock.close()


def test_release_with_an_unchanged_ledger_releases(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    assert wire.call(sock, "RELEASE", ledger_head=wire.head(), bar=NO_BAR)["ok"] is True
    sock.close()


def test_a_revoke_signed_between_launch_and_release_refuses_at_release(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid))           # signed after LAUNCH
    s2 = wire.conn()
    assert wire.sync(sock=s2)["ok"] is True                                   # the fire SYNCs before it RELEASEs
    s2.close()
    gone = wire.call(sock, "RELEASE", ledger_head=wire.head(), bar=NO_BAR)
    assert gone["refused"] == "link-not-standing"          # a revoked link is gone from the replay
    sock.close()


def test_a_release_naming_a_head_the_replica_lacks_is_refused_and_the_session_is_ended(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid))
    stale = wire.call(sock, "RELEASE", ledger_head=wire.head(), bar=NO_BAR)   # a client that skipped its SYNC
    assert (stale["refused"], stale["reason_class"]) == ("ledger-behind", "transient")
    sock.close()


def _other_fire(rig, wire, record=None):
    """Another fire: the operator signs ``record`` (None: a new, unrelated link that bars nothing), that fire SYNCs it
    into the replica."""
    rig.link() if record is None else rig.world.link(record)
    assert wire.sync()["ok"] is True


def test_another_fires_sync_between_launch_and_release_does_not_end_this_session(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    mine = wire.head()                                                        # this fire's verified head
    _other_fire(rig, wire)                                                    # a row that does not bar this agent
    assert wire.replica.head()[0] == mine["seq"] + 1
    ans = wire.call(sock, "RELEASE", ledger_head=mine, bar=NO_BAR)            # a held ancestor of the replica's head
    assert ans["ok"] is True and wire.runtime.children[0].released is True and wire.runtime.ended == []
    sock.close()


def test_a_revoke_of_this_agent_another_fire_synced_refuses_release_under_a_held_head(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    mine = wire.head()
    _other_fire(rig, wire, revoke_agent(rig.row(oid)["agent_id"], rig.sid))   # past this fire's claimed head
    gone = wire.call(sock, "RELEASE", ledger_head=mine, bar=NO_BAR)
    assert gone["refused"] == "link-not-standing"          # judged on the replica's whole replay, not the claimed prefix
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    sock.close()


@pytest.mark.parametrize("claim,name", [("past", "ledger-behind"), ("fork", "ledger-fork"), ("zero", "ledger-behind")])
def test_a_release_naming_a_head_the_replica_does_not_hold_or_that_forks_is_refused(rig, wire, claim, name):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    h = wire.head()
    head = {"past": {"seq": h["seq"] + 1, "hash": h["hash"]}, "fork": {"seq": h["seq"] - 1, "hash": "0" * 64},
            "zero": {"seq": 0, "hash": ""}}[claim]
    ans = wire.call(sock, "RELEASE", ledger_head=head, bar=NO_BAR)
    assert ans["refused"] == name
    assert ans.get("reason_class") == ("transient" if name == "ledger-behind" else None)
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    sock.close()


@pytest.mark.parametrize("fields", [{}, {"bar": NO_BAR}, {"head": True}], ids=["neither", "no-head", "no-bar"])
def test_release_without_a_ledger_head_or_a_bar_is_a_bad_request(rig, wire, fields):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    if fields.pop("head", False):
        fields["ledger_head"] = wire.head()
    assert wire.call(sock, "RELEASE", **fields)["refused"] == "bad-request"
    sock.close()


def test_release_judges_the_bar_its_own_frame_carries_not_the_launches(rig, wire):
    # codex r1 MED: a bar opened after LAUNCH, with no ledger row yet, refuses RELEASE
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True                               # LAUNCH: no bar
    gone = wire.call(sock, "RELEASE", ledger_head=wire.head(), bar={"open": True, "closed_revoke_seq": 0})
    assert (gone["refused"], gone.get("reason_class")) == ("agent-barred", None)
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    sock.close()


def test_a_release_bar_outside_its_claimed_head_is_bar_invalid(rig, wire):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    wire.sync()
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    h = wire.head()
    ans = wire.call(sock, "RELEASE", ledger_head=h, bar={"open": False, "closed_revoke_seq": h["seq"] + 1})
    assert ans["refused"] == "bar-invalid" and wire.runtime.children[0].released is False
    sock.close()


def test_the_production_wiring_reads_nothing_from_the_operators_home():
    text = Path(D.__file__).read_text()
    body = text[text.index("def build_server("):text.index("def service_row(")]
    assert "AuthorityReplica(" in body and "AgentAuthority(" not in body and "agent_store_bar(" not in body
    # LAUNCH, RELEASE and the SYNC re-judge (levain 34, D2) are the three points of use; none is a second authority
    assert text.count("def verify_point_of_use(") == 1 and text.count("self._verify(") == 3


# --- fix round 1 (L3 s10b_b1 r1) ---------------------------------------------------------------------------------------

def test_a_replay_during_an_uncommitted_sync_reads_the_committed_head(rig, tmp_path, monkeypatch):
    """r1 #1 (refuted, pinned): the uncommitted SYNC's verify fills the shared cache first; a concurrent replay of the
    committed log is a digest prefix of it and is served, never HeadBelowCache."""
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    committed = replica.replay()
    inside, go_on = threading.Event(), threading.Event()
    real = auth.snapshot

    def paused(conn, **kw):
        state = real(conn, **kw)
        if threading.current_thread().name == "sync":
            assert state.seq == rows[-1].seq                                  # the shared cache now holds it, uncommitted
            inside.set()
            go_on.wait(30)
        return state

    monkeypatch.setattr(auth, "snapshot", paused)
    out: list = []
    t = threading.Thread(target=lambda: out.append(replica.append(rows[-1:])), name="sync")
    t.start()
    try:
        assert inside.wait(30)
        assert replica.head() == (rows[-2].seq, rows[-2].hash)
        assert replica.replay() == committed                                  # no HeadBelowCache, the committed head
        verify = D.replica_verifier(replica, now=rig.now)
        with pytest.raises(LauncherRefusedT) as exc:                          # judged against the committed head only
            verify(None, None, D.FrameClaims((rows[-1].seq, rows[-1].hash), (False, 0)))
        assert exc.value.name == "ledger-behind"
    finally:
        go_on.set()
        t.join(60)
    monkeypatch.undo()
    assert out == [(rows[-1].seq, rows[-1].hash)] and replica.replay() == rig.authority.replay()


class _NoCommit:
    """A connection whose COMMIT fails (disk full at the commit)."""

    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, *a):
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._c.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_a_commit_that_fails_leaves_the_replica_and_its_replay_as_they_were(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    committed, before = replica.replay(), (_stored(replica), _anchor_bytes(replica))
    real = sqlite3.connect
    monkeypatch.setattr(AR.sqlite3, "connect", lambda *a, **kw: _NoCommit(real(*a, **kw)))
    with pytest.raises(AR.ReplicaRefused) as exc:
        replica.append(rows[-1:])
    monkeypatch.undo()
    assert exc.value.reason_class == "transient"
    assert (_stored(replica), _anchor_bytes(replica)) == before
    assert replica.replay() == committed                                      # the cache is not ahead of the database
    assert replica.append(rows[-1:]) == (rows[-1].seq, rows[-1].hash)
    assert replica.replay() == rig.authority.replay()


def test_a_fresh_replica_is_private_from_inception(rig, tmp_path):
    replica = _replica(tmp_path)
    replica.append(_source(rig))
    anchor_dir = auth.anchor_path(replica.root).parent
    assert [p.stat().st_mode & 0o777 for p in (replica.root, anchor_dir, replica.db_path)] == [0o700, 0o700, 0o600]


@pytest.mark.parametrize("what", ["root", "anchor_dir", "db"])
def test_a_loose_pre_existing_replica_object_is_refused_never_repaired(rig, tmp_path, what):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    target = {"root": replica.root, "anchor_dir": auth.anchor_path(replica.root).parent, "db": replica.db_path}[what]
    loose = 0o755 if what != "db" else 0o644
    target.chmod(loose)
    with pytest.raises(AR.ReplicaRefused) as exc:          # another uid may already hold it open: a chmod revokes nothing
        replica.append(rows[-1:])
    assert (exc.value.name, exc.value.reason_class) == ("replica-not-private", "standing")
    assert target.stat().st_mode & 0o777 == loose                             # left as found, never repaired
    for read in (replica.head, replica.replay):
        with pytest.raises(AR.ReplicaRefused) as exc:
            read()
        assert exc.value.name == "replica-not-private"
    claims = D.FrameClaims((rows[-2].seq, rows[-2].hash), (False, 0))
    with pytest.raises(LauncherRefusedT) as lexc:          # the run server keeps the name and the class (L3 r2 #5)
        D.replica_verifier(replica, now=rig.now)(None, None, claims)
    assert (lexc.value.name, lexc.value.reason_class) == ("replica-not-private", "standing")


@pytest.mark.parametrize("what", ["db", "journal", "lock", "anchor", "anchor_dir", "hardlink"])
def test_a_symlinked_or_shared_file_in_the_replica_is_refused(rig, tmp_path, what):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    anchor = auth.anchor_path(replica.root)
    target = {"db": replica.db_path, "journal": replica.root / "cockpit.db-journal",
              "lock": replica.root / AR.LOCK_NAME, "anchor": anchor, "anchor_dir": anchor.parent}.get(what)
    if what == "hardlink":
        (elsewhere / "copy").hardlink_to(replica.db_path)
    elif what == "anchor_dir":
        shutil.move(str(target), str(elsewhere / "anchor"))
        target.symlink_to(elsewhere / "anchor")
    else:
        if target.exists():
            shutil.copy(target, elsewhere / "f")
            target.unlink()
        else:
            (elsewhere / "f").write_bytes(b"")
        target.symlink_to(elsewhere / "f")
    with pytest.raises(AR.ReplicaRefused) as exc:
        replica.append(rows[-1:])
    assert exc.value.name == "replica-not-ours"
    with pytest.raises(Exception):
        replica.head()


def test_a_commit_whose_anchor_advance_fails_answers_the_committed_head_and_a_retry_heals_it(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    real = auth.advance_anchor

    def fail(path, snap):
        raise OSError("disk")

    monkeypatch.setattr(auth, "advance_anchor", fail)
    assert replica.append(rows[-1:]) == (rows[-1].seq, rows[-1].hash)         # committed: never "unchanged"
    assert auth.read_anchor(auth.anchor_path(replica.root)).seq == rows[-1].seq - 1
    monkeypatch.setattr(auth, "advance_anchor", real)
    with pytest.raises(AR.ReplicaRefused) as exc:                             # the identical retry
        replica.append(rows[-1:])
    assert (exc.value.name, exc.value.reason_class) == ("ledger-stale", "transient")
    assert auth.read_anchor(auth.anchor_path(replica.root)).seq == rows[-1].seq   # ... healed the anchor first
    assert replica.replay() == rig.authority.replay()


def test_a_missing_verifier_is_a_transient_refusal(rig, tmp_path):
    rows = _source(rig)
    replica = AR.AuthorityReplica(tmp_path / "nokeygen" / "authority", ssh_keygen=tmp_path / "no-ssh-keygen")
    with pytest.raises(AR.ReplicaRefused) as exc:
        replica.append(rows)
    assert (exc.value.name, exc.value.reason_class) == ("ledger-verifier-unavailable", "transient")
    assert replica.head() == (0, "")


def test_head_reads_a_replica_whose_path_holds_uri_characters(rig, tmp_path):
    rows = _source(rig)
    replica = AR.AuthorityReplica(tmp_path / "a?b#c%41" / "authority")
    assert replica.append(rows) == (rows[-1].seq, rows[-1].hash)
    assert replica.head() == (rows[-1].seq, rows[-1].hash)


def test_release_that_carries_no_bar_of_its_own_fails_closed(rig, wire):
    # B2-rel/S10: RELEASE reads its own bar at RELEASE (the holder no longer carries LAUNCH claims); a frame with a
    # ledger_head but no bar is refused, never judged against a default bar (the B1 r1 property, on the later design)
    with pytest.raises(C.WireError):
        wire.server._release_claims({"ledger_head": wire.head()})
    with pytest.raises(C.WireError):
        wire.server._release_claims({})


def test_a_replica_that_moves_during_the_check_is_named_ledger_behind(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    moved = (rows[-1].seq, rows[-1].hash)
    monkeypatch.setattr(replica, "head", lambda: moved)                       # head read at N+1, the replay sees N
    with pytest.raises(LauncherRefusedT) as exc:
        D.replica_verifier(replica, now=rig.now)(None, None, D.FrameClaims(moved, (False, 0)))
    assert (exc.value.name, exc.value.reason_class) == ("ledger-behind", "transient")


# --- L3 r2 ------------------------------------------------------------------------------------------------------------

def test_an_anchor_conflict_after_the_commit_is_refused_as_committed_never_a_success(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])

    def conflict(path, snap):                              # another writer's anchor landed after the pre-COMMIT check
        raise auth.ForeignAnchor("another store's anchor")

    monkeypatch.setattr(auth, "advance_anchor", conflict)
    with pytest.raises(AR.ReplicaRefused) as exc:
        replica.append(rows[-1:])
    assert (exc.value.name, exc.value.reason_class) == ("ledger-anchor-conflict", "standing")
    assert "ARE committed" in exc.value.detail
    assert replica.head() == (rows[-1].seq, rows[-1].hash)                    # committed: the refusal says so


def test_a_resend_whose_held_first_row_matches_but_a_later_row_differs_is_a_fork(rig, tmp_path):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows)
    later = [rows[-2], dataclasses.replace(rows[-1], record=rows[-1].record + " ")]
    assert _refused(replica, later, "ledger-fork").reason_class == "standing"
    _refused(replica, rows[-2:], "ledger-stale")                              # the identical resend is still stale


def test_a_partly_held_batch_whose_new_row_does_not_link_is_a_fork(rig, tmp_path):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    broken = [rows[-2], dataclasses.replace(rows[-1], prev_hash="f" * 64)]
    _refused(replica, broken, "ledger-fork")
    _refused(replica, rows[-2:], "ledger-stale")                              # consistent overlap: the client re-reads


def test_a_replay_advances_a_lagging_anchor_and_refuses_transient_while_it_cannot(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows[:-1])
    real = auth.advance_anchor

    def fail(path, snap):
        raise OSError("disk")

    monkeypatch.setattr(auth, "advance_anchor", fail)
    replica.append(rows[-1:])                                                 # committed; the anchor lags
    with pytest.raises(AR.ReplicaRefused) as exc:          # a lag never stands under an authorising replay
        replica.replay()
    assert (exc.value.name, exc.value.reason_class) == ("ledger-anchor-lagging", "transient")
    head = (rows[-1].seq, rows[-1].hash)
    with pytest.raises(LauncherRefusedT) as lexc:
        D.replica_verifier(replica, now=rig.now)(None, None, D.FrameClaims(head, (False, 0)))
    assert (lexc.value.name, lexc.value.reason_class) == ("ledger-anchor-lagging", "transient")
    monkeypatch.setattr(auth, "advance_anchor", real)
    assert auth.read_anchor(auth.anchor_path(replica.root)).seq == rows[-2].seq
    assert replica.replay() == rig.authority.replay()                         # the read path heals it
    assert auth.read_anchor(auth.anchor_path(replica.root)).seq == rows[-1].seq


def test_a_standing_replica_refusal_from_the_replay_keeps_its_name_and_class(rig, tmp_path, monkeypatch):
    rows = _with_link(rig)
    replica = _replica(tmp_path)
    replica.append(rows)

    def not_ours():
        raise AR.ReplicaRefused("replica-not-ours", "cockpit.db is a symlink")

    monkeypatch.setattr(replica, "replay", not_ours)
    with pytest.raises(LauncherRefusedT) as exc:
        D.replica_verifier(replica, now=rig.now)(None, None, D.FrameClaims(replica.head(), (False, 0)))
    assert (exc.value.name, exc.value.reason_class) == ("replica-not-ours", "standing")


LauncherRefusedT = L.LauncherRefused
