"""S1 session 3b-6b: CONTROL's side of keys' admin wire (CONTRACT §4.9, E23): the per-``front_uid`` registration
counter, MINT's token-only prewrite and FRONT-ARM's own token.

CONTROL is driven in-process (``Control.handle`` with a ``Conn``) over a real store and real flock; keys is the
``keys_front_register`` / ``keys_front_arm`` seams, which record what CONTROL sent and what the store held when it sent
it. The admin client over a real socket is run against 3b-2b's RecordingAdminServer in the lane's DONE run; the
revocation-boundary tests below run it against :class:`FakeKeys`, an in-process admin peer on a real unix socket.
"""
from __future__ import annotations

import errno
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import control as C
from levain.slot import control_client
from levain.slot import records as R
from tests.test_slot_s2a_control import OP_UID, attach_claims, mint_claims

KEYS_UID = 777
S1, S2, S3 = "s-00000000000000b1", "s-00000000000000b2", "s-00000000000000b3"
FRONT_UID = 3989


class FakeKeys:
    """keys' admin socket in-process: every request is logged with the index of the connection it came on and acked
    ``{}``, except an op in ``nak`` (refused ``malformed``); ``end(i)`` is keys ending connection ``i``, and ``ended``
    holds the connections whose EOF keys read (CONTROL closed them)."""

    def __init__(self, path: Path) -> None:
        self.nak: set[str] = set()
        self.log: list[tuple[int, dict]] = []
        self.conns: list[socket.socket] = []
        self.ended: set[int] = set()
        self._lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._lsock.bind(str(path))
        self._lsock.listen(8)
        self._lsock.settimeout(0.2)   # the accept loop re-reads _stop; closing a listener does not wake accept on Linux
        self._stop = threading.Event()
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                s, _ = self._lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.conns.append(s)
            threading.Thread(target=self._serve, args=(s, len(self.conns) - 1), daemon=True).start()

    def _serve(self, s: socket.socket, i: int) -> None:
        try:
            while (req := C.read_frame(s)) is not None:
                self.log.append((i, req))
                if req["op"] in self.nak:
                    s.sendall(C.encode_frame({"id": req["id"], "ok": False, "refused": "malformed"}))
                else:
                    s.sendall(C.encode_frame({"id": req["id"], "ok": True}))
            self.ended.add(i)
        except (OSError, C.FrameMalformed, C.FrameTooLarge):
            pass

    def end(self, i: int) -> None:
        self.conns[i].shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self._stop.set()
        self._acceptor.join(5)
        self._lsock.close()
        for s in self.conns:
            s.close()


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(prefix="lka", dir="/tmp"))   # short: AF_UNIX path limit
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _token_resolve(control: C.Control, digest: str) -> dict:
    return control.handle(C.Conn(KEYS_UID), {"v": 1, "op": "TOKEN-RESOLVE", "id": 9, "token_digest": digest})


def _control(store_dir: Path, sent: list[tuple]) -> C.Control:
    store = C.StateStore(store_dir)

    def register(session, front_uid, pid, start, incarnation, reg_token):
        on_disk = R.parse_state((store_dir / f"{session}.json").read_bytes())
        sent.append(("register", session, front_uid, reg_token, on_disk))

    control = C.Control(store, OP_UID, keys_front_register=register,
                        keys_front_arm=lambda session, reg_token: sent.append(("arm", session, reg_token)),
                        sleep=lambda s: None)
    control.start()
    return control


def _attached(control: C.Control, session: str, **claims: object) -> C.Conn:
    op = C.Conn(OP_UID)
    nonce = control.handle(op, {"v": 1, "op": "LEASE-OPEN", "id": 1})["lease_nonce"]
    ans = control.handle(C.Conn(0), {"v": 1, "op": "ATTACH", "id": 2, "lease_nonce": nonce, "sudo_uid": OP_UID,
                                     "session": session, "claims": attach_claims(**claims)})
    assert ans["ok"], ans
    return op


def _rendering(control: C.Control, session: str) -> tuple[C.Conn, int]:
    op = _attached(control, session)
    ans = control.handle(C.Conn(0), {"v": 1, "op": "CAS", "id": 3, "session": session, "generation": 0,
                                     "from": R.ALLOCATED, "to": R.RENDERING, "fields": {}})
    assert ans["ok"], ans
    return op, ans["generation"]


def test_reg_token_counter_durable(tmp_path):
    """E23 (a): the counter is read, +1, written durably under the uid's lock BEFORE the FRONT-REGISTER carrying it,
    the session's own token is prewritten ALONE into its claims (``event_kind`` none), the merged record holds both at
    the final generation MINT answers, FRONT-ARM carries the token from the claims, and a CONTROL restart continues
    the counter (strictly increasing per ``front_uid``)."""
    store_dir = tmp_path / "sessions"
    store_dir.mkdir(mode=0o700)
    counter = store_dir / "front-reg" / f"{FRONT_UID}.json"
    sent: list[tuple] = []

    control = _control(store_dir, sent)
    _lease1, gen = _rendering(control, S1)
    ans = control.handle(C.Conn(0), {"v": 1, "op": "MINT", "id": 4, "session": S1, "generation": gen,
                                     "claims": mint_claims()})
    assert ans == {"id": 4, "ok": True, "generation": gen + 2}, ans
    (_, session, front_uid, token, prewritten), = sent
    assert (session, front_uid, token) == (S1, FRONT_UID, 1)
    assert prewritten.claims == {**attach_claims(), R.REG_TOKEN: 1}            # TOKEN-ONLY: no MINT claim
    assert prewritten.generation == gen + 1 and prewritten.event_kind == "none"
    assert not R.has_mint_claims(prewritten.claims)
    final = R.parse_state((store_dir / f"{S1}.json").read_bytes())
    assert final.generation == gen + 2 and final.claims == {**attach_claims(), **mint_claims(), R.REG_TOKEN: 1}
    assert R.parse_front_reg(counter.read_bytes(), FRONT_UID) == R.FrontReg(FRONT_UID, 1)
    ans = control.handle(C.Conn(0), {"v": 1, "op": "FRONT-ARM", "id": 5, "session": S1, "generation": gen + 2})
    assert ans["ok"] and sent[-1] == ("arm", S1, 1), (ans, sent)

    control.stop()                                              # a restart: the next start revokes S1
    sent.clear()
    control = _control(store_dir, sent)
    _lease2, gen = _rendering(control, S2)
    assert control.handle(C.Conn(0), {"v": 1, "op": "MINT", "id": 6, "session": S2, "generation": gen,
                                      "claims": mint_claims()})["ok"]
    assert [(s[1], s[3]) for s in sent] == [(S2, 2)]
    assert R.parse_front_reg(counter.read_bytes(), FRONT_UID) == R.FrontReg(FRONT_UID, 2)
    control.stop()


def _mint(control: C.Control, session: str, gen: int) -> dict:
    return control.handle(C.Conn(0), {"v": 1, "op": "MINT", "id": 4, "session": session, "generation": gen,
                                      "claims": mint_claims()})


def test_nakd_drop_is_never_forgotten_on_an_open_connection(short_dir, monkeypatch):
    """codex r1 HIGH 1: keys refusing a revoked session's DROP-SESSION past V23 must not leave keys holding the
    registration on a live connection: the debt ends only by an ack or by CONTROL closing that connection, whose EOF
    keys answers by clearing every registration it made."""
    monkeypatch.setattr(control_client, "V23_SECONDS", 0.3)
    store_dir = short_dir / "sessions"
    store_dir.mkdir(mode=0o700)
    keys = FakeKeys(short_dir / "a.sock")
    admin = C.KeysAdmin(short_dir / "a.sock", answer_s=0.2, window_s=1.0)
    control = C.Control(C.StateStore(store_dir), OP_UID, keys_admin=admin, sleep=lambda s: time.sleep(0.01))
    control.start()
    try:
        _lease, gen = _rendering(control, S1)
        assert _mint(control, S1, gen)["ok"]
        keys.nak.add("DROP-SESSION")
        assert control.handle(C.Conn(0), {"v": 1, "op": "REVOKE", "id": 5, "session": S1})["ok"]
        control.join_eof(5)
        assert [r["op"] for _, r in keys.log].count("DROP-SESSION") >= 2       # refused, retried
        assert control.drops_owed() == frozenset()
        assert not admin.connected(), "a refused drop was forgotten with keys' connection still open"
        deadline = time.monotonic() + 5
        while 0 not in keys.ended and time.monotonic() < deadline:
            time.sleep(0.01)
        assert 0 in keys.ended                                                  # keys read CONTROL's close
    finally:
        control.stop()
        keys.close()


def _record_eofs(admin: C.KeysAdmin) -> list[int]:
    """The epochs whose EOF CONTROL has handled, appended after its handler returns."""
    eofs: list[int] = []
    seen_eof = admin.on_eof

    def on_eof(no: int) -> None:
        seen_eof(no)
        eofs.append(no)

    admin.on_eof = on_eof
    return eofs


def _wait_for(cond, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.005)
    return cond()


def test_drop_debt_never_moves_to_a_newer_connection(short_dir, monkeypatch):
    """gemini r2 MED: a drop debt is bound to its connection (a fencing token). Epoch 1 ends while the retry sleeps
    and S2's MINT opens epoch 2: the debt is moot (keys cleared epoch 1 at its EOF), it is never sent on epoch 2, and
    its V23 never aborts epoch 2."""
    monkeypatch.setattr(control_client, "V23_SECONDS", 0.3)
    store_dir = short_dir / "sessions"
    store_dir.mkdir(mode=0o700)
    keys = FakeKeys(short_dir / "a.sock")
    admin = C.KeysAdmin(short_dir / "a.sock", answer_s=0.2, window_s=1.0)
    box: dict = {}
    first = threading.Event()

    def sleep(s: float) -> None:
        if threading.current_thread().name.startswith("control-drop-") and not first.is_set():
            first.set()                                                       # the retry's first sleep
            keys.end(0)
            assert _wait_for(lambda: 1 in eofs)
            box["mint2"] = _mint(box["control"], S2, box["gen2"])            # opens epoch 2
            return
        time.sleep(0.01)

    control = box["control"] = C.Control(C.StateStore(store_dir), OP_UID, keys_admin=admin, sleep=sleep)
    eofs = _record_eofs(admin)
    control.start()
    try:
        _lease1, gen = _rendering(control, S1)
        assert _mint(control, S1, gen)["ok"]                                  # registered on epoch 1
        _lease2, box["gen2"] = _rendering(control, S2)
        keys.nak.add("DROP-SESSION")
        assert control.handle(C.Conn(0), {"v": 1, "op": "REVOKE", "id": 5, "session": S1})["ok"]
        control.join_eof(3)
        assert box["mint2"]["ok"], box["mint2"]
        assert control.drops_owed() == frozenset()
        time.sleep(0.4)                                                       # past the debt's V23
        assert [i for i, r in keys.log if r["op"] == "DROP-SESSION"] == [0]
        assert 1 not in keys.ended and 2 not in eofs and admin.connected(), "the debt's V23 aborted epoch 2"
        assert control.store.get(S2).state == R.RENDERING
    finally:
        control.stop()
        keys.close()


def test_one_keys_eof_revoke_worker_per_session(short_dir):
    """codex r2 MED: S1's revoke keeps failing (the store cannot settle) while keys' connection ends three times; the
    later EOFs coalesce into the worker already retrying it, and that one worker lands the revoke once the store
    recovers."""
    store_dir = short_dir / "sessions"
    store_dir.mkdir(mode=0o700)
    failing = threading.Event()

    def fsync(fd: int) -> None:
        if failing.is_set() and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        os.fsync(fd)

    keys = FakeKeys(short_dir / "a.sock")
    admin = C.KeysAdmin(short_dir / "a.sock", answer_s=1.0)
    control = C.Control(C.StateStore(store_dir, fsync=fsync), OP_UID, keys_admin=admin,
                        sleep=lambda s: time.sleep(0.005))
    eofs = _record_eofs(admin)
    control.start()
    try:
        _lease, gen = _rendering(control, S1)
        assert _mint(control, S1, gen)["ok"]
        failing.set()
        for i in range(3):
            if i:
                admin.call("NOOP")                                            # the next connection
            keys.end(i)
            assert _wait_for(lambda i=i: i + 1 in eofs)
        workers = [t for t in threading.enumerate() if t.name == f"control-keys-eof-{S1}" and t.is_alive()]
        assert len(workers) == 1, f"{len(workers)} keys-EOF revoke workers for one session"
        failing.clear()
        control.join_eof(5)
        assert control.store.get(S1).state == R.REVOKED
    finally:
        failing.clear()
        control.stop()
        keys.close()


@pytest.mark.parametrize("case", ["unsettled-record", "newer-connection"])
def test_keys_eof_revokes_exactly_the_lost_connections_registrations(short_dir, case):
    """keys' admin EOF (§4.5). ``unsettled-record`` (codex r1 HIGH 2, glm-openrouter MED 1): an unrelated record that
    cannot settle must not make the scan find no victims; the registered session's token stops resolving at once and
    its revoke lands once the store recovers. ``newer-connection`` (codex MED 3, gemini HIGH 1): a MINT that registers
    on the NEXT connection while the old connection's EOF is being handled is not revoked by that stale EOF."""
    store_dir = short_dir / "sessions"
    store_dir.mkdir(mode=0o700)
    failing = threading.Event()

    def fsync(fd: int) -> None:
        if failing.is_set() and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        os.fsync(fd)

    keys = FakeKeys(short_dir / "a.sock")
    admin = C.KeysAdmin(short_dir / "a.sock", answer_s=1.0)
    control = C.Control(C.StateStore(store_dir, fsync=fsync), OP_UID, keys_uid=KEYS_UID, keys_admin=admin,
                        sleep=lambda s: time.sleep(0.005))
    control.start()
    handled = threading.Event()
    seen_eof = admin.on_eof
    try:
        _lease1, gen = _rendering(control, S1)
        assert _mint(control, S1, gen)["ok"]                                    # registered on connection 0
        token1 = control.handles(S1).token_digest
        assert _token_resolve(control, token1)["ok"]
        _lease2, gen2 = _rendering(control, S2)
        if case == "unsettled-record":
            _lease3 = _attached(control, S3, **{"class": "none", "slot": {"class": "none", "index": 0}})
            failing.set()
            control.handle(C.Conn(0), {"v": 1, "op": "CAS", "id": 6, "session": S3, "generation": 0,
                                       "from": R.ALLOCATED, "to": R.RENDERING, "fields": {}})
            with pytest.raises(Refused):
                control.store.get(S3)                                           # S3 cannot settle

        def at_eof(*a: object) -> None:
            if case == "newer-connection":                                     # the interleaving, injected
                assert _mint(control, S2, gen2)["ok"]                          # registers on connection 1
            seen_eof(*a)
            handled.set()

        admin.on_eof = at_eof
        keys.end(0)
        assert handled.wait(5)
        assert _token_resolve(control, token1) == {"id": 9, "ok": False, "refused": "token-revoked"}
        failing.clear()
        control.join_eof(5)
        assert control.store.get(S1).state == R.REVOKED
        if case == "newer-connection":
            assert [i for i, r in keys.log if r["op"] == "FRONT-REGISTER" and r["session"] == S2] == [1]
            assert control.store.get(S2).state == R.RENDERING, "a stale EOF revoked a newer connection's session"
            h2 = control.handles(S2)
            assert h2 is not None and _token_resolve(control, h2.token_digest)["ok"]
    finally:
        control.stop()
        keys.close()
