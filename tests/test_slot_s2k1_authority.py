"""S1 session 2, fix round 2, lane K1: the authority layer's L3 r1 findings (S2_L3_r1_g1: codex 1-7, complement 1-6).

CONTROL is driven in-process (``Control.handle`` with a ``Conn``) where the order of events must be exact, and over
the real socket (``tests.test_slot_s2a_control.Env``) for the server's own bounds. The runtime helper runs in the 2f2
``World`` (real CONTROL, real flock, root acts recorded). Real flock throughout.
"""
from __future__ import annotations

import errno
import fcntl
import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from levain.slot.ids import SlotId
from tests.test_slot_s2a_control import Env, attach_claims, mint_claims
from tests.test_slot_s2f2_runtime_helper import ACCOUNTS, OP_UID, _blob, _prepared, world  # noqa: F401 - the fixture

S1, S2 = "s-00000000000000a1", "s-00000000000000a2"


class Box:
    """CONTROL over a real store in ``tmp``, its seams recorded, its backoff sleep a no-op."""

    def __init__(self, tmp: Path, **kw) -> None:
        self.dir = tmp / "sessions"
        self.dir.mkdir(mode=0o700, exist_ok=True)
        self.calls: list[tuple] = []
        self.fail_dir_fsync = 0
        self.store = C.StateStore(self.dir, fsync=self._fsync)
        seams = dict(drop_session=lambda s: self.calls.append(("drop", s)),
                     wake_reaper=lambda: self.calls.append(("wake",)),
                     keys_front_register=lambda *a: self.calls.append(("register",) + a),
                     keys_front_arm=lambda s, t: self.calls.append(("arm", s)),
                     sleep=lambda s: None)
        seams.update(kw)
        self.control = C.Control(self.store, OP_UID, **seams)

    def _fsync(self, fd: int) -> None:
        if self.fail_dir_fsync and stat.S_ISDIR(os.fstat(fd).st_mode):
            self.fail_dir_fsync -= 1
            raise OSError(errno.EIO, "injected directory fsync failure")
        os.fsync(fd)

    def ask(self, conn: C.Conn, op: str, **args) -> dict:
        return self.control.handle(conn, {"v": 1, "op": op, "id": 1, **args})

    def lease(self) -> tuple[C.Conn, str]:
        op = C.Conn(OP_UID)
        return op, self.ask(op, "LEASE-OPEN")["lease_nonce"]

    def attach(self, session: str = S1) -> C.Conn:
        op, nonce = self.lease()
        ans = self.ask(C.Conn(0), "ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=session,
                       claims=attach_claims())
        assert ans["ok"], ans
        return op

    def cas(self, session: str, gen: int, frm: str, to: str, **fields) -> dict:
        return self.ask(C.Conn(0), "CAS", session=session, generation=gen, fields=fields, **{"from": frm, "to": to})

    def disk(self, session: str) -> R.SessionState:
        return R.parse_state((self.dir / f"{session}.json").read_bytes())


@pytest.fixture
def box(tmp_path) -> Box:
    return Box(tmp_path)


# --- c1 / comp1: one render per session; the CAS shortcut is a lost-ack retry only -------------------------------------

def test_cas_shortcut_only_for_a_lost_ack_retry(box):
    box.attach()
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING") == {"id": 1, "ok": True, "generation": 1}
    # the same CAS again, one generation behind: the retry of a lost answer converges
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING") == {"id": 1, "ok": True, "generation": 1}
    root = C.Conn(0)
    assert box.ask(root, "MINT", session=S1, generation=1, claims=mint_claims())["ok"]      # generation 2
    # a caller at the OLD generation, the record now two past it: never told it won a move another made
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING")["refused"] == "stale-generation"


def test_render_lock_is_ends_lock(world):
    """One path: the file render / resume lock is the file END locks."""
    s = _prepared(world)
    store = H.Store(world.seams)
    try:
        ctx = H.Ctx(world.seams, store, OP_UID, "operator", OP_UID, {})
        a = world.alloc(s)
        assert SS.end_facts(ctx, a).lock == world.L / "alloc" / H.session_lock_name(s)
    finally:
        store.close()


def _hold(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_render_and_resume_refused_while_end_holds_the_session(world):
    s = _prepared(world)
    fd = _hold(world.L / "alloc" / f"{s}.lock")                         # END (or another render) holds it
    try:
        world.calls.clear()
        assert world.main(["render", s], _blob())[1] == {"ok": False, "refused": "session-busy"}
        assert world.main(["resume", s])[1] == {"ok": False, "refused": "session-busy"}
        assert world.calls == []                                        # nothing reached CONTROL
        assert world.state(s).state == R.ALLOCATED and world.alloc(s).policy is None
    finally:
        os.close(fd)
    assert world.main(["render", s], _blob())[1] == {"ok": True}


def test_second_render_beside_the_first_is_busy(world):
    """codex 1: a second render arriving while the first is past step (1) can neither pass the CAS nor write alloc."""
    s = _prepared(world)
    second: dict = {}

    def at(step):
        if step == "policy" and not second:
            second["line"] = world.main(["render", s], _blob())[1]

    world.on_step = at
    assert world.main(["render", s], _blob())[1] == {"ok": True}
    assert second["line"] == {"ok": False, "refused": "session-busy"}
    assert world.state(s).state == R.RENDERED
    assert len(world.fronts.procs) == 1 and len(world.registered) == 1
    a = world.alloc(s)
    assert (a.policy.front.pid, a.policy.front.start) == (world.registered[0][2], world.registered[0][3])


# --- c2 / comp2: DROP-SESSION is replayed and retried, never assumed -----------------------------------------------------

def test_failed_drop_is_retried_until_acked(tmp_path):
    tries: list[str] = []

    def drop(session):
        tries.append(session)
        if len(tries) <= 2:
            raise OSError(errno.ECONNREFUSED, "keys down")

    box = Box(tmp_path, drop_session=drop)
    box.attach()
    assert box.ask(C.Conn(0), "REVOKE", session=S1)["ok"]
    box.control.join_eof(5)
    assert tries == [S1, S1, S1]                                        # failed twice, acked the third time
    assert box.control.drops_owed() == frozenset()
    assert [e[0] for e in box.control.seam_errors] == ["drop_session"]  # noted once, never recorded as done


def test_drop_owed_while_keys_stays_down(tmp_path):
    gate = threading.Event()

    def drop(session):
        raise OSError(errno.ECONNREFUSED, "keys down")

    box = Box(tmp_path, drop_session=drop, sleep=lambda s: gate.wait(0.01))
    box.attach()
    assert box.ask(C.Conn(0), "REVOKE", session=S1)["ok"]
    assert box.control.drops_owed() == {S1}
    box.control.stop()
    box.control.join_eof(5)
    assert box.control.drops_owed() == frozenset()                      # stopped: the next start owes it again


@pytest.mark.parametrize("state", [R.REVOKED, R.FROZEN, R.DEAD, R.CREDENTIAL_PARKED, R.HELD, R.RETAINED])
def test_restart_replays_the_drop_for_every_record_past_live(tmp_path, state):
    """CONTROL died after the REVOKED write and before its drop: the next start sends it."""
    first = Box(tmp_path)
    rec = R.SessionState(session=S1, generation=3, state=state, claims={}, lease_live=False, event_kind="state",
                         updated_at="2026-10-20T00:00:00Z")
    first.store.put(rec)
    again = Box(tmp_path)
    again.control.start()
    assert again.calls == [("drop", S1), ("wake",)]
    assert again.disk(S1) == rec                                         # nothing rewritten


def test_restart_revokes_live_then_drops_once(tmp_path):
    first = Box(tmp_path)
    first.attach()
    again = Box(tmp_path)
    again.control.start()
    assert again.disk(S1).state == R.REVOKED
    assert again.calls.count(("drop", S1)) == 1


# --- c3: a write that failed after its rename is never served from the old memory ---------------------------------------

def test_post_rename_failure_reloads_before_serving(box):
    box.attach()
    box.fail_dir_fsync = 1
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING")["refused"] == "internal-error"
    assert box.disk(S1).state == R.RENDERING                             # the rename landed
    rec = box.store.get(S1)
    assert (rec.state, rec.generation) == (R.RENDERING, 1)               # memory reloaded from disk
    # the retry converges, and the next CAS moves from the reloaded generation
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING")["generation"] == 1
    assert box.ask(C.Conn(0), "MINT", session=S1, generation=1, claims=mint_claims())["ok"]


def test_unsettled_session_refused_until_reload_succeeds(box):
    box.attach()
    box.fail_dir_fsync = 5                                               # the write, then four reload attempts
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING")["refused"] == "internal-error"
    for _ in range(2):
        assert box.cas(S1, 1, "RENDERING", "RENDERED")["refused"] == "record-unsettled"
        assert box.ask(C.Conn(0), "REVOKED-SET")["refused"] == "record-unsettled"
    assert box.ask(C.Conn(0), "MINT", session=S1, generation=1, claims=mint_claims())["ok"]


def test_post_rename_revoke_still_owes_its_after_acts(box):
    """A REVOKE whose write failed past the rename answered an error and dropped nothing; the reload owes the drop."""
    box.attach()
    box.fail_dir_fsync = 1
    assert box.ask(C.Conn(0), "REVOKE", session=S1)["refused"] == "internal-error"
    assert ("drop", S1) not in box.calls
    assert box.ask(C.Conn(0), "REVOKED-SET") == {"id": 1, "ok": True, "sessions": [S1]}
    box.control.join_eof(5)
    assert ("drop", S1) in box.calls and ("wake",) in box.calls


def test_not_durable_is_raised_only_after_the_rename(tmp_path):
    p = tmp_path / "r.json"
    p.write_bytes(b"old")

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "dir")

    with pytest.raises(R.NotDurable):
        R.write_durably(p, b"new", fsync=fsync)
    assert p.read_bytes() == b"new"

    def fsync_file(fd):
        raise OSError(errno.EIO, "file")

    with pytest.raises(OSError) as e:
        R.write_durably(p, b"newer", fsync=fsync_file)
    assert not isinstance(e.value, R.NotDurable) and p.read_bytes() == b"new"


# --- c4 / c5: prepare's clean failure gives the slot back; the alloc is created fresh ---------------------------------------

E0 = SlotId("egress", 0)


def test_clean_prepare_failure_reverts_the_lease(world):
    before = world.slot_state(E0)

    def fault(step):
        if step == "attach":
            raise Refused("fault-injected")

    world.on_step = fault
    assert world.prepare()["refused"] == "fault-injected"
    after = world.slot_state(E0)
    assert (after.state, after.session, after.rev, after.lease_seq) == ("reusable", None, before.rev + 2,
                                                                         before.lease_seq + 1)
    assert sorted(os.listdir(world.L / "alloc")) == []                   # the alloc it created is gone


def test_control_refused_attach_reverts_the_lease(world):
    line = world.prepare(nonce=C.new_handle())                          # a nonce CONTROL never issued
    assert line["refused"] == "nonce-unknown"
    assert world.slot_state(E0).state == "reusable"
    assert [n for n in os.listdir(world.L / "alloc") if n.endswith(".json")] == []


def test_ambiguous_attach_keeps_the_lease(world):
    """An answer lost on the wire: CONTROL may hold the record, so the slot and alloc stay for the orphan path."""

    def lost(op, fields):
        if op == "ATTACH":
            raise Refused("control-down", "the answer was lost")

    world.on_call = lost
    assert world.prepare()["refused"] == "control-down"
    st = world.slot_state(E0)
    assert st.state == "leased"
    world.alloc(st.session)


def test_revert_never_writes_over_a_changed_state(world):
    def fault(step):
        if step == "revert-locked":
            p = E0.slot_dir(world.L) / "state"
            cur = world.slot_state(E0)
            p.write_bytes(R.slot_state_bytes(R.SlotState(E0, cur.rev + 5, cur.lease_seq, "retired", False,
                                                         "2026-10-20T00:00:00Z", None)))
        if step == "attach":
            raise Refused("fault-injected")

    world.on_step = fault
    assert world.prepare()["refused"] == "fault-injected"
    assert world.slot_state(E0).state == "retired"


def test_session_id_collision_retries_before_leasing(world):
    s = _prepared(world)
    taken = (world.L / "alloc" / f"{s}.json").read_bytes()
    ids = iter([s, s, "s-00000000000000ff"])
    world.seams.new_session = lambda: next(ids)
    line = world.prepare()
    assert line["ok"] and line["session"] == "s-00000000000000ff"
    assert (world.L / "alloc" / f"{s}.json").read_bytes() == taken      # the other session's alloc untouched


def test_alloc_created_fresh_never_over_an_existing_one(world):
    planted = b"another session's alloc"

    def race(step):
        if step == "alloc":
            sess = world.slot_state(E0).session
            (world.L / "alloc" / f"{sess}.json").write_bytes(planted)

    world.on_step = race
    assert world.prepare()["refused"] == "session-exists"
    names = [n for n in os.listdir(world.L / "alloc") if n.endswith(".json")]
    assert len(names) == 1 and (world.L / "alloc" / names[0]).read_bytes() == planted
    assert world.slot_state(E0).state == "reusable"                      # given back
    assert not any(n.endswith(".new") for n in os.listdir(world.L / "alloc"))


# --- c6 / comp3: MINT and FRONT-ARM re-check the lease after the keys wire ----------------------------------------------------

def test_lease_eof_during_front_register_publishes_no_handle(tmp_path):
    box: Box

    def register(*a):
        box.control.lease_eof(op.lease)

    box = Box(tmp_path, keys_front_register=register)
    op = box.attach()
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING")["ok"]
    ans = box.ask(C.Conn(0), "MINT", session=S1, generation=1, claims=mint_claims())
    assert ans["refused"] == "lease-dead"
    assert box.control.handles(S1) is None
    box.control.join_eof(5)
    assert box.disk(S1).state == R.REVOKED and box.control.handles(S1) is None


def test_front_arm_rechecks_the_lease_after_the_relay(tmp_path):
    box: Box

    def arm(session, reg_token):
        box.control.lease_eof(op.lease)

    box = Box(tmp_path, keys_front_arm=arm)
    op = box.attach()
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING")["ok"]
    assert box.ask(C.Conn(0), "MINT", session=S1, generation=1, claims=mint_claims())["ok"]
    assert box.ask(C.Conn(0), "FRONT-ARM", session=S1, generation=3)["refused"] == "lease-dead"


def test_front_arm_rechecks_the_record_after_the_relay(tmp_path):
    box: Box

    def arm(session, reg_token):
        assert box.ask(C.Conn(0), "REVOKE", session=session)["ok"]       # root's teardown, the lease still live

    box = Box(tmp_path, keys_front_arm=arm)
    box.attach()
    assert box.cas(S1, 0, "ALLOCATED", "RENDERING")["ok"]
    assert box.ask(C.Conn(0), "MINT", session=S1, generation=1, claims=mint_claims())["ok"]
    assert box.ask(C.Conn(0), "FRONT-ARM", session=S1, generation=3)["refused"] == "not-rendering"


# --- c7 / comp4: the lifetime collections are bounded ------------------------------------------------------------------------

def test_threads_and_leases_do_not_accumulate(box):
    for i in range(5):
        s = f"s-{i + 1:016x}"
        op = box.attach(s)
        box.control.lease_eof(op.lease)
        box.control.join_eof(5)
        assert box.disk(s).state == R.REVOKED
    assert box.control._eof_threads == []
    assert box.control._session_lease == {}
    assert len(box.control._leases) == 5                                 # retired, bounded: still lease-dead


def test_retired_leases_are_bounded_then_nonce_unknown(box, monkeypatch):
    monkeypatch.setattr(C, "RETIRED_LEASES_MAX", 3)
    nonces = []
    for _ in range(5):
        conn, nonce = box.lease()
        box.control.lease_eof(conn.lease)
        nonces.append(nonce)
    assert len(box.control._leases) == 3

    def attach(nonce):
        return box.ask(C.Conn(0), "ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S2,
                       claims=attach_claims())["refused"]

    assert [attach(n) for n in nonces] == ["nonce-unknown"] * 2 + ["lease-dead"] * 3


def test_a_live_lease_survives_its_sessions_revoke(box):
    """REVOKE by root while the supervisor's connection is open: the lease stays bound until its own EOF."""
    op = box.attach()
    assert box.ask(C.Conn(0), "REVOKE", session=S1)["ok"]
    assert box.control._session_lease[S1] is op.lease
    box.control.lease_eof(op.lease)
    box.control.join_eof(5)
    assert S1 not in box.control._session_lease


# --- comp5: a stalled frame is cut; the connection count is capped --------------------------------------------------------------

@pytest.fixture
def senv():
    e = Env()
    e.server.stop()
    e.server = C.ControlServer(e.control, e.sock_path, peer_uid=e.peer, max_connections=3, frame_read_s=0.3)
    e.server.start()
    yield e
    e.close()


def test_partial_frame_closed_idle_connection_kept(senv):
    idle = senv.client("op")
    stall = senv.client("op")
    stall.raw(b"\x00\x00")                                               # half a length, then nothing
    deadline = time.time() + 5
    stall.sock.settimeout(5)
    assert stall.closed()
    assert time.time() < deadline
    time.sleep(0.5)                                                      # past the frame deadline, between frames
    assert idle.ask("LEASE-OPEN")["ok"]


def test_connections_over_the_cap_closed_at_accept(senv):
    held = [senv.client("op") for _ in range(3)]
    for c in held:
        assert c.ask("STATUS")["ok"]
    extra = senv.client("op")
    extra.sock.settimeout(5)
    assert extra.closed()
    held[0].close()
    deadline = time.time() + 5
    while True:                                                          # the freed seat is served again
        c = senv.client("op")
        c.sock.settimeout(5)
        try:
            if c.ask("STATUS")["ok"]:
                break
        except (AssertionError, OSError, C.FrameMalformed):
            pass
        assert time.time() < deadline
        time.sleep(0.05)


def test_server_threads_pruned(senv):
    for _ in range(6):
        c = senv.client("op")
        assert c.ask("STATUS")["ok"]
        c.close()
    deadline = time.time() + 5
    while len(senv.server._threads) > 1 and time.time() < deadline:      # the accept loop alone
        time.sleep(0.02)
    assert len(senv.server._threads) == 1


# --- comp6: a FIFO under a record's name does not block the reader -----------------------------------------------------------------

def test_read_record_refuses_a_fifo_without_blocking(tmp_path):
    p = tmp_path / f"{S1}.json"
    os.mkfifo(p)
    out: dict = {}

    def read():
        try:
            R.read_record(p)
        except Refused as e:
            out["refused"] = e.name

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(2)
    if t.is_alive():                                                     # unblock the reader before failing
        os.close(os.open(p, os.O_WRONLY | os.O_NONBLOCK))
        t.join(2)
        pytest.fail("read_record blocked on a FIFO")
    assert out == {"refused": "bad-root"}


# --- the onboarding pre-seed's dir is the session owner's, not root's (VM X4b: every real render was bad-root) ----------

ME = os.getuid()


def _owned_dir(tmp_path: Path, mode: int = 0o700) -> tuple[int, str]:
    d = tmp_path / "claude"
    d.mkdir()
    os.chmod(d, mode)
    return os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY), "claude"


def test_slot_owned_dir_passes_only_for_alloc_owner(tmp_path):
    """No seam: real dirs this test owns, checked against the uid alloc names."""
    parent, name = _owned_dir(tmp_path)
    try:
        os.close(H.open_slot_owned_dir(parent, name, ME, "claude"))
        for other in (ME + 1, 0):
            with pytest.raises(Refused) as e:
                H.open_slot_owned_dir(parent, name, other, "claude")
            assert e.value.name == "bad-root"
        for bad_mode in (0o770, 0o702, 0o720):
            os.chmod(tmp_path / name, bad_mode)
            with pytest.raises(Refused) as e:
                H.open_slot_owned_dir(parent, name, ME, "claude")
            assert e.value.name == "bad-root"
        os.chmod(tmp_path / name, 0o700)
        os.symlink(tmp_path / name, tmp_path / "link")
        with pytest.raises(Refused) as e:
            H.open_slot_owned_dir(parent, "link", ME, "link")
        assert e.value.name == "bad-root"
    finally:
        os.close(parent)


def test_session_owner_from_alloc(world):
    import dataclasses

    s = _prepared(world)
    a = world.alloc(s)
    assert H.session_owner_uid(a) == a.slot_uid                         # egress: the slot
    v0 = SlotId("none", 0)
    v = dataclasses.replace(a, slot=v0, child_uid=892, netns=v0.netns, cgroup=R.cgroup_path(v0, s))
    assert H.session_owner_uid(v) == 892                                 # verifier: the child, never the driver


def _as_owner(world, s: str, uid: int) -> None:
    """Rewrite alloc so the session's owner is ``uid`` (the dirs on disk are this test's own)."""
    import dataclasses

    a = world.alloc(s)
    (world.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(dataclasses.replace(a, slot_uid=uid, slot_gid=uid)))


def _root_check_refusing_owner_dirs(world, s: str):
    """The trusted seam as production's ``_root_trusted`` answers on this store: every dir root made passes (the test
    uid stands for root), and the session owner's vendor dirs FAIL, as they do for real (owned by the slot)."""
    owned = {os.stat(world.L / "live" / s / rel).st_ino for rel in H.SLOT_OWNED_RENDER_DIRS}
    # the REAL owner check: alloc's uid as is; R/gw/slot0 is this test's, standing for the front account (3b-8a)
    world.seams.chowned_uid = lambda uid: ME if uid == ACCOUNTS["_levain_gw0"] else uid
    return lambda st: st.st_uid == ME and not st.st_mode & 0o022 and st.st_ino not in owned


def test_render_preseed_under_the_root_check(world):
    """X4b: render through a trusted check that refuses the owner's dirs, as the real one does."""
    s = _prepared(world)
    _as_owner(world, s, ME)
    world.seams.trusted = _root_check_refusing_owner_dirs(world, s)
    assert world.main(["render", s], _blob())[1] == {"ok": True}, world.last_err
    assert (world.L / "live" / s / "config" / "claude" / ".claude.json").exists()
    assert world.chown_of(f"live/{s}/config/claude/.claude.json") == (ME, ME)


def test_render_preseed_refused_when_alloc_names_another_owner(world):
    s = _prepared(world)                                                 # alloc names _levain_s0 (899), the dir is ours
    world.seams.trusted = _root_check_refusing_owner_dirs(world, s)
    line = world.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "bad-root"}
    assert "config/claude" in world.last_err
    assert not (world.L / "live" / s / "config" / "claude" / ".claude.json").exists()


def test_render_step3_files_with_the_root_check(world):
    """render_step3_files run directly, the trusted seam left as the root check (not the permissive test one)."""
    s = _prepared(world)
    _as_owner(world, s, ME)
    world.seams.trusted = _root_check_refusing_owner_dirs(world, s)
    store = H.Store(world.seams)
    try:
        ctx = H.Ctx(world.seams, store, OP_UID, "operator", OP_UID, {})
        run = H.RenderRun(ctx, world.alloc(s), canon_blob())
        H.render_step2_policy(run)
        H.render_step3_files(run)
    finally:
        store.close()
    assert (world.L / "live" / s / "config" / "claude" / ".claude.json").read_bytes()
    assert (world.L / "live" / s / "root" / "settings.json").exists()


def canon_blob():
    from levain.slot.runtime_grammar import parse_stdin

    return parse_stdin("render", _blob())
