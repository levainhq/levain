"""S1 session 2f2: the runtime helper's ``__main__`` and its verbs prepare, render, resume, status (CONTRACT §5.1).

Every test runs unprivileged in a tmp store: the helper's root acts are seams (owners RECORDED, not applied; the
stage reader runs on a thread, not a dropped fork; the front is a fake process table). CONTROL is the REAL
:class:`levain.slot.control.Control` over a real state store, reached through an in-process client with the F1
``ControlClient`` shape (``call(op, **fields)`` -> answer, ``Refused(name)`` on a refusal, ``locked`` retried).
Real flock throughout. What only a VM run can show (the drop, the detached front, a cross-uid EACCES) is the 2g
row, named in each test that stands in for one.
"""
from __future__ import annotations

import errno
import hashlib
import io
import os
import threading
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import runtime_helper as H
from levain.slot import stage as ST
from levain.slot import tickets as T
from levain.slot.end import V5_FLOOR_BYTES
from levain.slot.ids import SlotId
from levain.slot.install_helper import operator_record_text

OP_UID = os.getuid() if os.getuid() != 0 else 501
OP_GID = os.getgid() if os.getuid() != 0 else 20
DISPATCH_UID = 799
#: Linux §1.1 ranges: slot accounts top-down from 899, the shared groups from 789.
ACCOUNTS = {"_levain_s0": 899, "_levain_gw0": 898, "_levain_s1": 897, "_levain_gw1": 896,
            "_levain_s2": 895, "_levain_gw2": 894, "_levain_v0": 893, "_levain_c0": 892,
            "_levain_dispatch": DISPATCH_UID}
GROUPS = {"levain-gwfronts": 789, "levain-slots": 788}
BOOT = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
#: A header render's bundlefmt parse accepts (HEAD naming the one branch ref), then stand-in pack bytes.
BUNDLE_OID = b"0123456789abcdef0123456789abcdef01234567"
BUNDLE = (b"# v2 git bundle\n" + BUNDLE_OID + b" HEAD\n" + BUNDLE_OID + b" refs/heads/main\n\n"
          + b"bundle-bytes " * 64)
#: a base map the front's parser takes (render copies the session's entries into R/gw/slot<n>/session.json, 3b-8a)
BASE = {"format": "levain-routes-base/1", "routes": [
    {"route": "anthropic", "type": "brokered", "hosts": ["api.anthropic.com"],
     "upstream": {"scheme": "https", "host": "api.anthropic.com", "base_path": ""}, "credential": "anthropic",
     "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}], "headers": {}, "body_cap": 1048576,
                  "body_codec": "anthropic-messages"},
     "max_call_microdollars": 2000000, "max_session_microdollars": 50000000},
    {"route": "github", "type": "egress", "hosts": ["github.com"]},
    {"route": "claude-sub", "type": "subscription_passthrough", "hosts": ["claude.ai"]}]}


def _blob(**kw) -> bytes:
    obj = {"format": "levain-session-render/1", "vendor": "claude", "routes": ["anthropic", "github"]}
    obj.update(kw)
    return canon.canonical_bytes(obj)


def _slot_state(slot: SlotId, state: str = "reusable", *, rev: int = 0, seq: int = 0, session=None) -> bytes:
    return R.slot_state_bytes(R.SlotState(slot, rev, seq, state, True, "2026-10-20T00:00:00Z", session))


class Fronts:
    """A fake process table for the detached front: spawn -> a pid, live until killed; the GO pipe kept to read."""

    def __init__(self) -> None:
        self.procs: dict[int, dict] = {}
        self.next_pid = 40000

    def spawn(self, argv, spec_json, go_rfd) -> int:
        pid = self.next_pid
        self.next_pid += 1
        spec = canon.parse(spec_json)
        go = os.dup(go_rfd)
        os.set_blocking(go, False)
        self.procs[pid] = {"argv": tuple(argv), "spec": spec, "alive": True, "start": 7000 + pid, "go": go}
        return pid

    def identity(self, pid):
        p = self.procs.get(pid)
        return (p["spec"]["uid"], p["start"]) if p and p["alive"] else None

    def kill(self, pid) -> None:
        self.procs[pid]["alive"] = False

    def alive(self) -> list[int]:
        return [pid for pid, p in self.procs.items() if p["alive"]]

    def go_bytes(self, pid) -> bytes | None:
        """What the front would read: the bytes so far, b"" at EOF, None while the writer is open and silent."""
        try:
            return os.read(self.procs[pid]["go"], 16)
        except BlockingIOError:
            return None


class Client:
    """The F1 ``ControlClient`` shape over the real CONTROL, as root (peer uid 0)."""

    def __init__(self, world: "World") -> None:
        self.world = world

    def call(self, op, **fields):
        w = self.world
        w.calls.append((op, fields))
        if w.on_call:
            w.on_call(op, fields)
        req = {"v": 1, "op": op, "id": len(w.calls), **fields}
        for _ in range(100):
            ans = w.control.handle(C.Conn(0), req)
            if ans.get("refused") != "locked":
                break
        if not ans["ok"]:
            raise Refused(ans["refused"], op)
        return {k: v for k, v in ans.items() if k not in ("id", "ok")}

    def close(self) -> None:
        pass


class World:
    def __init__(self, tmp: Path, *, host: str = "linux", egress: int = 3, verifier: int = 1) -> None:
        self.L, self.R = tmp / "L", tmp / "R"
        L = self.L
        for d, mode in (("", 0o755), ("alloc", 0o700), ("live", 0o711), ("staging", 0o711), ("slots", 0o755),
                        ("policy", 0o755), ("routes", 0o755), ("sessions", 0o700)):
            (L / d).mkdir(parents=True, exist_ok=True)
            os.chmod(L / d, mode)
        (self.R / "control").mkdir(parents=True)
        (self.R / "gw").mkdir(mode=0o755)
        (L / "gw").mkdir(mode=0o755)
        for i in range(egress):   # §2.2 R/gw/slot<n>: the front account's (here: this test's uid, chowned_uid)
            (self.R / "gw" / f"slot{i}").mkdir(mode=0o750)
            (L / "gw" / f"slot{i}").mkdir(mode=0o750)    # 3b-8a T1: the front's log dir, setup's row
        (L / "slots" / "pool.lock").write_bytes(b"")
        for slot in [SlotId("egress", i) for i in range(egress)] + [SlotId("none", i) for i in range(verifier)]:
            d = slot.slot_dir(L)
            d.mkdir()
            (d / "lock").write_bytes(b"")
            if slot.slot_class == "egress":
                (d / "keep").mkdir(mode=0o700)
            (d / "state").write_bytes(_slot_state(slot))
        (L / "policy" / "operator").write_text(operator_record_text("phill", OP_UID))
        (L / "routes" / "base.json").write_bytes(canon.canonical_bytes(BASE))
        self.chowns: list[tuple[str, int, int]] = []
        self.calls: list[tuple[str, dict]] = []
        self.steps: list[str] = []
        self.on_call = None
        self.on_step = None
        self.fronts = Fronts()
        self.free = 100 << 30
        self.registered: list[tuple] = []
        self.armed: list[str] = []
        self.control = C.Control(C.StateStore(L / "sessions"), OP_UID,
                                 keys_front_register=lambda *a: self.registered.append(a),
                                 keys_front_arm=lambda s, t: self.armed.append(s))
        self.run_as_uids: list[int] = []
        n = iter(range(1, 1 << 30))
        self.seams = H.Seams(
            host=host, store=L, run=self.R, euid=lambda: 0,
            trusted=lambda st: st.st_uid == os.getuid() and not st.st_mode & 0o022,
            chown=lambda fd, u, g, rel: self.chowns.append((rel, u, g)), chowned_uid=lambda uid: os.getuid(),
            free_bytes=lambda fd: self.free, boot_id=lambda: BOOT,
            new_session=lambda: f"s-{next(n):016x}",
            account_uid=self._account, group_gid=lambda g: GROUPS[g], user_gid=lambda uid: OP_GID,
            client_factory=lambda path: Client(self), stage_run_as=self._run_as,
            spawn_front=self.fronts.spawn, proc_identity=self.fronts.identity, kill=self.fronts.kill,
            var_folders=lambda uid, gid: f"/private/var/folders/zz/u{uid}", wake_reaper=lambda: None,
            at=self._at)

    @staticmethod
    def _account(name: str) -> int:
        if name not in ACCOUNTS:
            raise Refused("account-missing", name)
        return ACCOUNTS[name]

    def _run_as(self, uid, gid, fn, wfd, keep=()):
        self.run_as_uids.append(uid)
        return ST.thread_run_as(uid, gid, fn, wfd)

    def _at(self, step: str) -> None:
        self.steps.append(step)
        if self.on_step:
            self.on_step(step)

    # -- the supervisor's half ----------------------------------------------------------------------------------------

    def lease(self) -> tuple[str, C.Conn]:
        conn = C.Conn(OP_UID)
        ans = self.control.handle(conn, {"v": 1, "op": "LEASE-OPEN", "id": 1})
        return ans["lease_nonce"], conn

    def main(self, argv, stdin: bytes = b"", *, uid: int = OP_UID) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        code = H.main(argv, stdin=stdin, env={"SUDO_UID": str(uid)}, seams=self.seams, out=out, err=err)
        text = out.getvalue()
        assert text.endswith("\n") and text.count("\n") == 1, text      # exactly one line
        line = canon.parse(text[:-1].encode())
        assert canon.canonical(line) == text[:-1]                        # canonical JSON
        assert (code == 0) == line["ok"]
        self.last_err = err.getvalue()
        return code, line

    def prepare(self, *args: str, nonce: str | None = None) -> dict:
        if nonce is None:
            nonce, self.conn = self.lease()
        code, line = self.main(["prepare", *(args or ("egress", "interactive", "claude", "brokered"))],
                               canon.canonical_bytes({"lease_nonce": nonce}))
        return line

    def stage(self, session: str, data: bytes = BUNDLE) -> None:
        (self.L / "staging" / session / "repo.bundle").write_bytes(data)

    def state(self, session: str) -> R.SessionState | None:
        return self.control.store.get(session)

    def alloc(self, session: str) -> R.Alloc:
        return R.parse_alloc((self.L / "alloc" / f"{session}.json").read_bytes())

    def slot_state(self, slot: SlotId) -> R.SlotState:
        return H.parse_slot_state_of((slot.slot_dir(self.L) / "state").read_bytes(), slot)

    def give_credential(self, slot: SlotId, vendor: str = "claude", *, baseline: bool = True) -> None:
        d = slot.slot_dir(self.L)
        (d / "keep" / H.CREDENTIAL_NAME[vendor]).write_bytes(b"{}")
        if baseline:
            (d / "baseline").write_bytes(canon.canonical_bytes(
                {"vendor": vendor, "account_id": "acct-1", "confirmed_at": "2026-10-20T00:00:00Z"}))

    def mode(self, rel: str) -> int:
        return os.stat(self.L / rel, follow_symlinks=False).st_mode & 0o7777

    def chown_of(self, rel: str) -> tuple[int, int]:
        got = [(u, g) for r, u, g in self.chowns if r == rel]
        assert len(got) == 1, (rel, got)
        return got[0]


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def _prepared(world: World, *args: str) -> str:
    line = world.prepare(*args)
    assert line["ok"], line
    world.stage(line["session"])
    return line["session"]


# --- the entry ----------------------------------------------------------------------------------------------------

def test_one_canonical_line_and_refusal_names(world):
    code, line = world.main(["frobnicate"])
    assert (code, line) == (H.EXIT_REFUSED, {"ok": False, "refused": "unknown-verb"})
    assert world.calls == [] and world.steps == []


def test_caller_from_sudo_uid(world):
    assert world.main(["status"], uid=OP_UID + 4242)[1]["refused"] == "unknown-caller"
    out, err = io.StringIO(), io.StringIO()
    assert H.main(["status"], stdin=b"", env={}, seams=world.seams, out=out, err=err) == H.EXIT_REFUSED
    assert canon.parse(out.getvalue().strip().encode())["refused"] == "no-sudo-uid"
    for bad in ("0", "²", f"0{OP_UID}", f" {OP_UID}", "-1"):
        assert world.main(["status"], uid=bad)[1]["refused"] == "no-sudo-uid", bad
    world.seams.euid = lambda: OP_UID
    assert world.main(["status"])[1]["refused"] == "not-root"


def test_configured_slots_read_from_store(world):
    # e3 is not a configured slot (no L/slots/e3/), so the grammar refuses it before anything acts.
    assert world.prepare("egress", "interactive", "claude", "brokered", "3")["refused"] == "slot-not-configured"
    assert world.calls == [] and "floor" not in world.steps


def test_unbuilt_verbs_answer_by_name(world, monkeypatch):
    """A grammar verb with no handler answers ``verb-unbuilt:<verb>`` (every runtime verb is built since 1027+18
    idg1's confirm-identity, so the handler is taken away here)."""
    s = _prepared(world)
    monkeypatch.delitem(H.VERB_HANDLERS, "confirm-identity")
    assert world.main(["confirm-identity", s, "yes"])[1]["refused"] == "verb-unbuilt:confirm-identity"


# The ticketed prepare (S1 3c-7) is tests/test_pool.py's.


# --- prepare ------------------------------------------------------------------------------------------------------

def test_prepare_result_and_records(world):
    line = world.prepare()
    assert line["ok"] and line["format"] == H.PREPARE_RESULT_FORMAT
    s = line["session"]
    assert line["slot"] == {"class": "egress", "index": 0} and line["gwport"] == 47100
    assert line["staging"] == str(world.L / "staging" / s)
    a = world.alloc(s)
    assert (a.slot_uid, a.slot_gid, a.operator_uid, a.supervisor_uid, a.staging_uid) == (899, 899, OP_UID, OP_UID,
                                                                                          OP_UID)
    st = world.slot_state(SlotId("egress", 0))
    assert (st.state, st.session, st.lease_seq, st.rev) == ("leased", s, 1, 1)
    assert (a.lease_seq, a.slot_rev) == (st.lease_seq, st.rev)
    rec = world.state(s)
    assert (rec.state, rec.generation) == (R.ALLOCATED, 0)
    assert rec.claims["lease_seq"] == 1 and rec.claims["slot_rev"] == 1 and rec.claims["kind"] == "interactive"
    assert [op for op, _ in world.calls] == ["ATTACH"]
    assert world.registered == []                                       # no front, no handle
    assert world.control.handles(s) is None


def test_prepare_refuses_below_floor(world):
    world.free = V5_FLOOR_BYTES - 1
    line = world.prepare()
    assert line["refused"] == "below-floor"
    assert world.calls == [] and os.listdir(world.L / "alloc") == [] and os.listdir(world.L / "live") == []
    assert world.slot_state(SlotId("egress", 0)).rev == 0
    world.free = V5_FLOOR_BYTES
    assert world.prepare()["ok"]


def test_passthrough_no_baseline_refused_at_prepare(world):
    args = ("egress", "interactive", "claude", "passthrough")
    assert world.prepare(*args)["refused"] == "no-baseline:egress"
    world.give_credential(SlotId("egress", 1), baseline=False)          # a credential with no baseline
    assert world.prepare(*args)["refused"] == "no-baseline:egress"
    world.give_credential(SlotId("egress", 2), vendor="codex")          # another vendor's
    assert world.prepare(*args)["refused"] == "no-baseline:egress"
    assert world.prepare(*args, "0")["refused"] == "no-baseline:e0"
    assert world.calls == [] and os.listdir(world.L / "alloc") == []
    assert all(world.slot_state(SlotId("egress", i)).state == "reusable" for i in range(3))
    world.give_credential(SlotId("egress", 2))
    line = world.prepare(*args)
    assert line["ok"] and line["slot"]["index"] == 2


def test_passthrough_baseline_slot_must_be_reusable(world):
    world.give_credential(SlotId("egress", 1))
    (SlotId("egress", 1).slot_dir(world.L) / "state").write_bytes(
        _slot_state(SlotId("egress", 1), "leased", rev=3, seq=2, session="s-00000000000000aa"))
    assert world.prepare("egress", "interactive", "claude", "passthrough")["refused"] == "no-baseline:egress"


def test_login_then_session_same_slot(world):
    e0, e1 = SlotId("egress", 0), SlotId("egress", 1)
    other = world.prepare()                                             # someone holds e0
    assert other["slot"]["index"] == 0
    login = world.prepare("egress", "login", "claude", "passthrough")
    assert login["ok"] and login["slot"]["index"] == 1                  # login needs no baseline
    # confirm-identity yes + END (F3's, the reaper's): e1 parks the credential, writes the baseline, goes reusable;
    # e0's session ends too, so the lowest REUSABLE is e0 again.
    world.give_credential(e1)
    for slot in (e0, e1):
        st = world.slot_state(slot)
        (slot.slot_dir(world.L) / "state").write_bytes(_slot_state(slot, rev=st.rev + 2, seq=st.lease_seq))
    line = world.prepare("egress", "interactive", "claude", "passthrough")
    assert line["ok"] and line["slot"]["index"] == 1                    # the credential + baseline slot, not e0
    assert world.prepare("egress", "interactive", "claude", "brokered")["slot"]["index"] == 0


def test_named_slot_taken_when_reusable(world):
    assert world.prepare("egress", "interactive", "claude", "brokered", "2")["slot"]["index"] == 2


def test_named_slot_not_reusable_refused_never_replaced(world):
    """Lane F3 a (head ruling on §5.1 "the named slot if REUSABLE"): naming a slot asks for THAT slot's
    credential, so a named slot that is leased is refused by its tag, never swapped for another slot."""
    assert world.prepare("egress", "interactive", "claude", "brokered", "2")["slot"]["index"] == 2
    line = world.prepare("egress", "interactive", "claude", "brokered", "2")
    assert line["refused"] == "slot-not-reusable:e2"
    assert world.slot_state(SlotId("egress", 0)).state == "reusable"          # nothing else was leased
    assert world.slot_state(SlotId("egress", 1)).state == "reusable"
    os.unlink(SlotId("egress", 1).slot_dir(world.L) / "state")               # absent = not reusable, by name too
    assert world.prepare("egress", "interactive", "claude", "brokered", "1")["refused"] == "slot-not-reusable:e1"


def test_no_free_slot(tmp_path):
    w = World(tmp_path, egress=1)
    assert w.prepare()["ok"]
    assert w.prepare()["refused"] == "no-free-slot:egress"


def test_absent_state_file_is_not_reusable(world):
    os.unlink(SlotId("egress", 0).slot_dir(world.L) / "state")
    assert world.prepare()["slot"]["index"] == 1


def test_slot_allocation_locked(tmp_path):
    """Two prepares, real flock: the second waits on the pool lock while the first holds it, then sees the first's
    lease. With one reusable slot the second is refused by name, never handed the same slot."""
    for egress, want in ((2, {0, 1}), (1, None)):
        w = World(tmp_path / f"w{egress}", egress=egress)
        paused, go = threading.Event(), threading.Event()
        seen: list[tuple[str, str]] = []

        def on_step(step):
            seen.append((threading.current_thread().name, step))
            if step == "slot-leased" and threading.current_thread().name == "A":
                paused.set()
                assert go.wait(10)

        w.on_step = on_step
        results = {}
        nonces = {k: w.lease()[0] for k in "AB"}

        def body(k):
            code, line = w.main(["prepare", "egress", "interactive", "claude", "brokered"],
                                canon.canonical_bytes({"lease_nonce": nonces[k]}))
            results[k] = line

        a = threading.Thread(target=body, args=("A",), name="A")
        a.start()
        assert paused.wait(10)
        b = threading.Thread(target=body, args=("B",), name="B")
        b.start()
        b.join(0.5)
        assert b.is_alive() and ("B", "pool-locked") not in seen        # B is waiting on the pool lock
        go.set()
        a.join(10)
        b.join(10)
        assert results["A"]["ok"]
        if want:
            assert {results[k]["slot"]["index"] for k in "AB"} == want
        else:
            assert results["B"]["refused"] == "no-free-slot:egress"


def test_concurrent_prepares_keep_operator_slot(tmp_path):
    """§3.5 Pool: a ticketed prepare refuses ``operator-slot-reserved`` if taking a slot would leave no reusable
    credential-free egress slot, checked UNDER the pool lock, so two concurrent ticketed prepares cannot both take
    one and leave the operator none. (Ticketed ``prepare`` is session 2f's: this drives the pool step itself.)"""
    w = World(tmp_path, egress=3)
    w.give_credential(SlotId("egress", 2))                              # the operator's passthrough slot
    store = H.Store(w.seams)
    paused, go = threading.Event(), threading.Event()
    results: dict = {}

    def on_step(step):
        if step == "slot-leased" and threading.current_thread().name == "A":
            paused.set()
            assert go.wait(10)

    w.on_step = on_step

    def body(k, session):
        try:
            leased = H.lease_slot(store, slot_class="egress", kind="dispatch", vendor="claude", auth="brokered",
                                  named=None, ticketed=True, session=session)
            leased.release()
            results[k] = leased.slot
        except Refused as e:
            results[k] = e.name

    a = threading.Thread(target=body, args=("A", "s-00000000000000a1"), name="A")
    a.start()
    assert paused.wait(10)
    b = threading.Thread(target=body, args=("B", "s-00000000000000b1"), name="B")
    b.start()
    go.set()
    a.join(10)
    b.join(10)
    assert results == {"A": SlotId("egress", 0), "B": "operator-slot-reserved"}
    assert w.slot_state(SlotId("egress", 1)).state == "reusable"        # the operator keeps a credential-free slot
    assert w.slot_state(SlotId("egress", 2)).state == "reusable"        # and its credential slot is never ticketed
    store.close()


def test_pick_slot_rules():
    def v(i, state="reusable", creds=(), baseline=None):
        slot = SlotId("egress", i)
        return H.SlotView(R.SlotState(slot, 0, 0, state, True, "2026-10-20T00:00:00Z",
                                      "s-0000000000000001" if state == "leased" else None),
                          slot, frozenset(creds), baseline)

    kw = dict(slot_class="egress", kind="interactive", vendor="claude", auth="brokered", named=None, ticketed=False)
    assert H.pick_slot([v(1), v(0)], **kw) == SlotId("egress", 0)
    assert H.pick_slot([v(0, "scrubbed"), v(1)], **kw) == SlotId("egress", 1)
    assert H.pick_slot([v(0), v(1, creds=["claude"], baseline="claude")], **{**kw, "auth": "passthrough"}) \
        == SlotId("egress", 1)
    assert H.pick_slot([v(0), v(1, creds=["claude"], baseline="?")], **{**kw, "auth": "passthrough",
                                                                        "kind": "login"}) == SlotId("egress", 0)
    with pytest.raises(Refused) as e:
        H.pick_slot([v(0), v(1)], **{**kw, "ticketed": True, "named": 1})
    assert e.value.name == "ticket-slot-argument"
    assert H.pick_slot([v(0, creds=["codex"]), v(1), v(2)], **{**kw, "ticketed": True}) == SlotId("egress", 1)
    with pytest.raises(Refused) as e:
        H.pick_slot([v(0, baseline="claude"), v(1)], **{**kw, "ticketed": True})
    assert e.value.name == "operator-slot-reserved"


def test_staging_owned_by_lease_peer(world):
    s = _prepared(world)
    assert world.chown_of(f"staging/{s}") == (OP_UID, OP_GID)
    assert world.mode(f"staging/{s}") == 0o700
    assert world.alloc(s).staging_uid == OP_UID
    assert world.main(["render", s], _blob())[1]["ok"]
    assert world.run_as_uids == [OP_UID]                                # the reader ran as the lease peer
    # [VM 2g: the reader's real drop to staging_uid; a dispatch lease's own bundle once 2f builds tickets]


def test_alloc_unreadable_to_control(world):
    s = _prepared(world)
    assert world.mode("alloc") == 0o700 and world.mode(f"alloc/{s}.json") == 0o600
    assert not [r for r, _, _ in world.chowns if r.startswith("alloc")]   # root-created, root-owned: never chowned
    assert world.main(["render", s], _blob())[1]["ok"]
    assert world.mode(f"alloc/{s}.json") == 0o600


def test_other_slot_cannot_list_session(world):
    s = _prepared(world)
    assert world.chown_of(f"live/{s}") == (0, 899) and world.mode(f"live/{s}") == 0o750
    assert 899 != ACCOUNTS["_levain_s1"] and 899 not in GROUPS.values()   # the slot's OWN group, no shared one
    rows = H.session_dir_rows(SlotId("none", 0), {"_levain_v0": 893, "_levain_c0": 892})
    assert rows[0] == H.DirRow("", 0, 0, 0o711) and rows[1] == H.DirRow("driver", 893, 893, 0o700)
    assert world.main(["render", s], _blob())[1]["ok"]
    assert world.chown_of(f"live/{s}/root") == (0, 899) and world.mode(f"live/{s}/root") == 0o750
    assert world.chown_of(f"live/{s}/root/env") == (0, 899) and world.mode(f"live/{s}/root/env") == 0o640


def test_vendor_config_dir_writable(world):
    s = _prepared(world)
    for d in ("config/claude", "config/codex", "xdg/data", "xdg/cache", "xdg/config", "xdg/state", "tmp", "sock"):
        assert world.chown_of(f"live/{s}/{d}") == (899, 899) and world.mode(f"live/{s}/{d}") == 0o700, d
    for d in ("config", "xdg"):
        assert world.chown_of(f"live/{s}/{d}") == (0, 899) and world.mode(f"live/{s}/{d}") == 0o750, d
    assert world.main(["render", s], _blob())[1]["ok"]
    assert world.chown_of(f"live/{s}/config/claude/.claude.json") == (899, 899)


# --- render -------------------------------------------------------------------------------------------------------

def test_render_unknown_key_refused(world):
    s = _prepared(world)
    world.calls.clear()
    world.steps.clear()
    code, line = world.main(["render", s], _blob(driver_input={"x": 1}))
    assert (code, line) == (H.EXIT_REFUSED, {"ok": False, "refused": "unknown-key"})
    assert world.calls == [] and world.steps == []                      # refused before (1)
    assert world.state(s).state == R.ALLOCATED and world.alloc(s).policy is None


def test_render_order_observed(world):
    s = _prepared(world)
    world.calls.clear()
    world.steps.clear()
    go_at_arm: list = []
    world.on_call = lambda op, f: go_at_arm.append(world.fronts.go_bytes(next(iter(world.fronts.procs)))) \
        if op == "FRONT-ARM" else None
    code, line = world.main(["render", s], _blob())
    assert line == {"ok": True}
    assert world.steps == [name for name, _ in H.RENDER_STEPS]
    assert [(op, f.get("from"), f.get("to")) for op, f in world.calls] == [
        ("CAS", R.ALLOCATED, R.RENDERING), ("MINT", None, None), ("FRONT-ARM", None, None),
        ("CAS", R.RENDERING, R.RENDERED)]
    (pid,) = world.fronts.procs
    assert go_at_arm == [None]                                          # no go byte before FRONT-ARM answers
    assert world.fronts.go_bytes(pid) == H.GO_BYTE and world.fronts.go_bytes(pid) == b""   # then the byte, EOF
    assert world.registered == [(s, 898, pid, 7000 + pid, 1, 1)] and world.armed == [s]
    mint = dict(world.calls[1][1]["claims"])
    assert mint == {"routes": ["anthropic", "github"], "queue_grant": False, "incarnation": 1,
                    "egress_routes": ["github"], "front": {"uid": 898, "pid": pid, "start": 7000 + pid}}
    a = world.alloc(s)
    assert a.policy.front == R.Front(898, True, pid, 7000 + pid) and a.policy.egress_routes == ("github",)
    assert world.state(s).state == R.RENDERED
    assert world.control.handles(s).token is not None                   # brokered route: a token, in CONTROL only
    spec = world.fronts.procs[pid]["spec"]
    assert (spec["target"], spec["uid"], spec["keep_fds"], spec["extra_env"], spec["profile"]) == \
        ("front", 898, [H.GO_FD], {H.GO_FD_ENV: str(H.GO_FD)}, None)
    # 3b-8a: the gateway front runs with root's args (its listener, policy, log, keys' socket, the GO fd).
    gw = world.R / "gw" / "slot0"
    assert spec["argv"][-13:] == [H.FRONT_MODULE, "--unix", str(gw / "gw.sock"), "--slot-uid", "899",
                                  "--policy", str(gw / "session.json"),
                                  "--log", str(world.L / "gw" / "slot0" / "front.log"),          # 3b-8a T1
                                  "--keys", str(world.R / "keys" / "front.sock"), "--go-fd", str(H.GO_FD)]
    assert world.fronts.alive() == [pid]                                # left running, detached


def test_render_front_program_is_a_parameter(world):
    s = _prepared(world)
    world.seams.front_module = "levain.gateway.front"
    world.seams.front_args = lambda host, slot, **kw: ("--config-fd", "5")
    assert world.main(["render", s], _blob())[1]["ok"]
    (p,) = world.fronts.procs.values()
    assert p["spec"]["argv"][-3:] == ["levain.gateway.front", "--config-fd", "5"]


def test_staging_emptied_at_render(world):
    s = _prepared(world)
    assert world.main(["render", s], _blob())[1]["ok"]
    assert os.listdir(world.L / "staging" / s) == []
    assert (world.L / "live" / s / "in" / "repo.bundle").read_bytes() == BUNDLE
    assert world.chown_of(f"live/{s}/in/repo.bundle") == (0, 899) and world.mode(f"live/{s}/in/repo.bundle") == 0o640


def test_staging_kept_until_every_destination_durable(world):
    """A rendered file failing its fsync (after the bundle copied) leaves staging holding the bundle."""
    s = _prepared(world)

    def chown(fd, u, g, rel):
        if rel.endswith("root/gitconfig"):
            raise OSError(errno.EIO, "fault")
        world.chowns.append((rel, u, g))

    world.seams.chown = chown
    code, line = world.main(["render", s], _blob())
    assert code == H.EXIT_INTERNAL
    assert (world.L / "live" / s / "in" / "repo.bundle").exists()
    assert os.listdir(world.L / "staging" / s) == ["repo.bundle"]


def test_base_recorded_only_when_step3_completes(world):
    """A valid bundle copied, then a later step-(3) file faulting: alloc carries no base, so adopt answers
    base-unrecorded instead of admitting a half-rendered session."""
    s = _prepared(world)

    def chown(fd, u, g, rel):
        if rel.endswith("root/gitconfig"):
            raise OSError(errno.EIO, "fault")
        world.chowns.append((rel, u, g))

    world.seams.chown = chown
    assert world.main(["render", s], _blob())[0] == H.EXIT_INTERNAL
    assert (world.L / "live" / s / "in" / "repo.bundle").exists()
    assert world.alloc(s).base is None


def test_none_class_no_front_no_handle(world, request):
    """A verifier (none class) render: no front started, no FRONT-ARM, MINT claims with no front, no handle.

    3c-5 fix 4: MINT checks a ticketed session's claims against the ticket CONTROL holds BOUND to it (``run-unknown``
    for one it does not), and production only reaches a verifier session through ATTACH of a ticket-bound lease, which
    binds the ticket in the journal. So the ticket is minted and bound in a real TicketStore, the store halves the
    verbs call (``TicketStore.mint`` / ``bind``): ``RUN-TICKET-MINT`` itself answers ``verifier-unknown`` until a
    verifier generation can be activated (3c-8), so the store's mint stands in for that verb here."""
    s = "s-00000000000000c1"
    slot = SlotId("none", 0)
    (world.L / "policy" / "dispatch_ceiling").write_bytes(canon.canonical_bytes(
        {"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": 5_000_000, "max_open_tickets": 4,
         "max_runs_per_day": 50}))
    os.chmod(world.L / "policy" / "dispatch_ceiling", 0o644)      # read_ceiling refuses g/o-write; not the umask's call
    tickets = T.TicketStore(world.L / "sessions" / T.JOURNAL, world.L / "policy" / "dispatch_ceiling",
                            ceiling_uid=os.getuid())
    tickets.start()
    request.addfinalizer(tickets.close)                                 # the journal flock and thread, pass or fail
    world.control.tickets = tickets
    facts = T.check_facts({"job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
                           "kind": "verifier", "egress_routes": [], "reserved_microdollars": 0,
                           "verifier_generation": 1, "verifier_id": "pytest-report",
                           "verifier_steps": [{"argv": ["pytest", "-q"], "wall_s": 60}],
                           "input_bundle_digest": "sha256:" + "2" * 64, "driver_input_digest": "sha256:" + "3" * 64})
    ticket = tickets.mint("a-verifier-1", facts, C.new_handle, resolved=VERIFIER_ENTRY).ticket
    tickets.bind(ticket, s)                                              # what ATTACH of the ticket-bound lease journals
    a = R.Alloc(session=s, slot=slot, lease_seq=1, slot_rev=1, slot_uid=893, slot_gid=893, kind="verifier",
                vendor="claude", auth="none", operator_uid=OP_UID, supervisor_uid=DISPATCH_UID,
                staging_uid=OP_UID, created_at="2026-10-20T00:00:00Z", boot_id=BOOT, child_uid=892, ticket=ticket,
                netns=slot.netns, cgroup=R.cgroup_path(slot, s), ticket_facts=dict(facts, verifier=VERIFIER_ENTRY))
    (world.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(a))
    claims = {"operator_uid": OP_UID, "supervisor_uid": DISPATCH_UID, "slot": R.slot_obj(slot), "class": "none",
              "kind": "verifier", "vendor": "claude", "auth": "none", "lease_seq": 1, "slot_rev": 1,
              "ticket": ticket}
    world.control.store.put(R.SessionState(session=s, generation=0, state=R.ALLOCATED, claims=claims,
                                           lease_live=True, event_kind="state", updated_at="2026-10-20T00:00:00Z"))
    lease = C.Lease(C.new_handle(), DISPATCH_UID, used=True, session=s, ticket=ticket)   # the dispatch lease holding it
    world.control._session_lease[s] = lease
    store = H.Store(world.seams)
    request.addfinalizer(store.close)
    ctx = H.Ctx(world.seams, store, DISPATCH_UID, "dispatch", OP_UID, {})
    H.make_session_dirs(ctx, s, H.session_dir_rows(slot, {"_levain_v0": 893, "_levain_c0": 892}))
    (world.L / "staging" / s).mkdir()
    world.stage(s)
    (world.L / "staging" / s / "driver_input.json").write_bytes(b"{}")
    blob = _blob(routes=[], verifier_steps=[{"argv": ["pytest", "-q"], "wall_s": 60}], driver="output_compare")
    code, line = world.main(["render", s], blob, uid=DISPATCH_UID)
    assert line == {"ok": True}, world.last_err
    assert world.fronts.procs == {} and world.registered == [] and world.armed == []
    assert [op for op, _ in world.calls] == ["CAS", "MINT", "CAS"]
    assert "front" not in world.calls[1][1]["claims"] and world.calls[1][1]["claims"]["queue_grant"] is False
    assert world.control.handles(s) == C.Handles(None, None)            # neither a token nor a queue cap
    assert world.alloc(s).policy.front is None and world.state(s).state == R.RENDERED
    assert world.chown_of(f"live/{s}/root") == (0, 0) and world.mode(f"live/{s}/root") == 0o755
    assert world.chown_of(f"live/{s}/in/repo.bundle") == (0, 892)
    assert os.listdir(world.L / "staging" / s) == ["driver_input.json"]   # unstaged here (2f's): never removed


#: a resolved verifier entry (§3.5 VERIFIER IDENTITY) as CONTROL adds it to a verifier ticket's facts, which alloc
#: carries: render's ticket check holds the blob's driver to its `driver_template` (3c-7 fix r1)
#: 3c-8: the entry CONTROL resolves at mint, consistent with the ticket's facts (`tickets.check_resolved`), naming a
#: template this levain ships with its real digest (render's `driver-template-digest-mismatch`)
VERIFIER_ENTRY = {"verifier_generation": 1, "verifier_id": "pytest-report",
                  "verifier_artifact_digest": "sha256:" + "5" * 64, "declared_kind": "black-box",
                  "driver_template": "output_compare",
                  "driver_template_digest": "sha256:" + hashlib.sha256(
                      RD.read_driver_template("output_compare") or b"").hexdigest(),
                  "steps_digest": "sha256:" + hashlib.sha256(canon.canonical_bytes(
                      [{"argv": ["pytest", "-q"], "wall_s": 60}])).hexdigest(),
                  "driver_input_digest": "sha256:" + "3" * 64}


def test_render_refused_for_another_callers_session(world):
    s = _prepared(world)
    assert world.main(["render", s], _blob(), uid=DISPATCH_UID)[1]["refused"] == "no-session"
    assert world.state(s).state == R.ALLOCATED


def test_dispatch_rule_needs_a_ticketed_alloc(world):
    """§5.1: ``_levain_dispatch``'s rule covers only sessions whose alloc carries a ticket, even one it supervises."""
    s = "s-00000000000000d1"
    slot = SlotId("egress", 1)
    a = R.Alloc(session=s, slot=slot, lease_seq=1, slot_rev=1, slot_uid=897, slot_gid=897, kind="interactive",
                vendor="claude", auth="brokered", operator_uid=OP_UID, supervisor_uid=DISPATCH_UID,
                staging_uid=DISPATCH_UID, created_at="2026-10-20T00:00:00Z", boot_id=BOOT, netns=slot.netns,
                cgroup=R.cgroup_path(slot, s))
    (world.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(a))
    assert world.main(["render", s], _blob(), uid=DISPATCH_UID)[1]["refused"] == "no-session"
    assert world.main(["status", s], uid=DISPATCH_UID)[1]["refused"] == "no-session"
    assert world.main(["status"], uid=DISPATCH_UID)[1]["records"] == []
    assert world.calls == []


@pytest.mark.parametrize("step", [name for name, _ in H.RENDER_STEPS])
def test_lease_eof_during_render_leaves_no_front_or_token(world, step):
    """The lease dies just before each of render's steps: the render is refused, no front is left running, no
    handle survives, and alloc still parses."""
    s = _prepared(world)

    def eof(name):
        if name == step:
            world.control.lease_eof(world.conn.lease)
            world.control.join_eof(10)

    world.on_step = eof
    code, line = world.main(["render", s], _blob())
    # CONTROL's CAS checks the generation before the lease, and the EOF's own writes moved it: a CAS after the EOF
    # reads stale-generation, MINT / FRONT-ARM read lease-dead. Either way the render stops there.
    assert not line["ok"] and line["refused"] in ("lease-dead", "stale-generation"), world.last_err
    assert world.fronts.alive() == []
    assert world.control.handles(s) is None
    assert world.state(s).state == R.REVOKED
    world.alloc(s)                                                      # consistent: parses
    for pid in world.fronts.procs:
        # before (5b) the go pipe reads EOF with no go byte; an EOF at (6) comes after the byte, and the kill ends it
        assert world.fronts.go_bytes(pid) == (H.GO_BYTE if step == "cas-rendered" else b"")


#: Each render step's observable effect, so a refusal just before it can be shown to leave it (and every later
#: one) undone.
def _effects(world: World, s: str) -> dict:
    a = world.alloc(s)
    st = world.state(s)
    return {"cas-rendering": st.state != R.ALLOCATED, "policy": a.policy is not None,
            "files": (world.L / "live" / s / "root").exists(), "front": bool(world.fronts.procs),
            "mint": "routes" in st.claims, "arm": bool(world.armed), "cas-rendered": st.state == R.RENDERED}


@pytest.mark.parametrize("k", range(len(H.RENDER_STEPS)))
def test_render_refusal_at_every_step_acts_nothing_after(world, k):
    s = _prepared(world)
    names = [n for n, _ in H.RENDER_STEPS]

    def fault(name):
        if name == names[k]:
            raise Refused("fault-injected")

    world.on_step = fault
    code, line = world.main(["render", s], _blob())
    assert line == {"ok": False, "refused": "fault-injected"}
    eff = _effects(world, s)
    assert [eff[n] for n in names] == [True] * k + [False] * (len(names) - k), eff
    assert world.fronts.alive() == []                                   # a started front is killed


def test_front_killed_on_mint_refusal_uid_and_start_checked(world):
    s = _prepared(world)
    world.control._keys_front_register = lambda *a: (_ for _ in ()).throw(Refused("keys-unavailable"))
    assert world.main(["render", s], _blob())[1]["refused"] == "keys-unavailable"
    assert world.fronts.alive() == []
    # a pid whose start time is no longer the recorded one, or whose uid is another's, is never signalled
    for moved in ("start", "uid"):
        s2 = _prepared(world)
        asked: dict = {}

        def identity(pid, moved=moved):
            asked[pid] = asked.get(pid, 0) + 1
            uid, start = world.fronts.identity(pid)
            if asked[pid] == 1:
                return uid, start                                      # step (4) records the real start
            return (uid, start + 1) if moved == "start" else (uid + 1000, start)   # exactly one field moved

        world.seams.proc_identity = identity
        assert world.main(["render", s2], _blob())[1]["refused"] == "keys-unavailable"
        assert len(world.fronts.alive()) == 1
        world.fronts.kill(world.fronts.alive()[0])


def test_render_twice_refused(world):
    s = _prepared(world)
    assert world.main(["render", s], _blob())[1]["ok"]
    world.calls.clear()
    assert world.main(["render", s], _blob())[1]["refused"] == "not-allocated"
    assert world.calls == []


def test_render_darwin_profile_and_var_folders(tmp_path):
    w = World(tmp_path, host="darwin")
    ACC = {"_levain_s0": 3999, "_levain_gw0": 3998, "_levain_dispatch": 3799}
    w.seams.account_uid = lambda name: ACC[name]
    w.seams.group_gid = lambda g: {"levain-gwfronts": 3789}[g]
    s = _prepared(w)
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": True}, w.last_err
    assert (w.L / "live" / s / "root" / "profile.sb").exists()
    a = w.alloc(s)
    assert a.netns is None and a.cgroup is None
    (p,) = w.fronts.procs.values()
    assert p["argv"][0] != "/bin/launchctl" and p["spec"]["profile"] is None   # the front: no asuser, no profile


# --- resume, status -------------------------------------------------------------------------------------------------

def test_braked_resume_after_space(world):
    s = _prepared(world)
    assert world.main(["render", s], _blob())[1]["ok"]
    g = world.control.cas(s, world.state(s).generation, R.RENDERED, R.ACTIVE, {})
    world.control.cas(s, g, R.ACTIVE, R.BRAKED, {"brake_reason": "low-space"})
    world.free = V5_FLOOR_BYTES - 1
    world.calls.clear()
    assert world.main(["resume", s])[1]["refused"] == "below-floor"
    assert world.calls == [] and world.state(s).state == R.BRAKED
    world.free = V5_FLOOR_BYTES + 1
    assert world.main(["resume", s])[1] == {"ok": True}
    rec = world.state(s)
    assert rec.state == R.ACTIVE and "brake_reason" not in rec.fields


def test_status_read_only(world):
    s = _prepared(world)
    before = {p: p.read_bytes() for p in world.L.rglob("*") if p.is_file()}
    world.calls.clear()
    world.steps.clear()
    line = world.main(["status"])[1]
    assert line["ok"] and [r["session"] for r in line["records"]] == [s]
    assert world.main(["status", s])[1]["records"][0]["slot"] == {"class": "egress", "index": 0}
    assert world.main(["status", "s-00000000000000ff"])[1]["refused"] == "no-session"
    assert world.main(["status"], uid=DISPATCH_UID)[1]["records"] == []   # not the operator's sessions
    assert {p: p.read_bytes() for p in world.L.rglob("*") if p.is_file()} == before
    assert world.calls == [] and world.steps == []


def test_store_links_refused(world):
    os.rename(world.L / "alloc", world.L / "alloc.real")
    os.symlink(world.L / "alloc.real", world.L / "alloc")
    assert world.prepare()["refused"] == "bad-root"


@pytest.mark.parametrize("fmt", ["levain-routes-base/1", "levain-routes-base/2"])
def test_base_route_types_reads_both_formats(world, fmt):
    # 3c-2a: layout writes /2; a store that kept its /1 map is still read
    (world.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(dict(BASE, format=fmt)))
    assert H.base_route_types(H.Store(world.seams)) == {r["route"]: r["type"] for r in BASE["routes"]}
