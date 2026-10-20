"""CONTRACT §3.1, §3.3, §11 V23: the CONTROL client (``levain/slot/control_client.py``) against a REAL
:class:`levain.slot.control.ControlServer` on a tmp unix socket. A test cannot be root, so ONE Control is
served on two sockets: one whose peer reader says root, one whose says the operator. The off-shape and
silent-server cases run against a raw socket this test answers by hand."""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import control as C
from levain.slot import control_client as CC
from levain.slot import records as R
from levain.slot.control_client import ControlClient

OP_UID = os.getuid() if os.getuid() != 0 else 501
S1 = f"s-{1:016x}"


def attach_claims() -> dict:
    return {"operator_uid": OP_UID, "supervisor_uid": OP_UID, "slot": {"class": "egress", "index": 0},
            "class": "egress", "kind": "interactive", "vendor": "claude", "auth": "brokered",
            "lease_seq": 1, "slot_rev": 1}


class Env:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="lvf", dir="/tmp"))   # short: AF_UNIX path limit
        (self.dir / "s").mkdir(mode=0o700)
        self.store = C.StateStore(self.dir / "s")
        self.control = C.Control(self.store, OP_UID)
        self.root_path, self.op_path = self.dir / "root.sock", self.dir / "op.sock"
        self.root_srv = C.ControlServer(self.control, self.root_path, peer_uid=lambda s: 0)
        self.op_srv = C.ControlServer(self.control, self.op_path, peer_uid=lambda s: OP_UID)
        self.root_srv.start()
        self.op_srv.start()
        self.clients: list[ControlClient] = []

    def client(self, who: str, **kw) -> ControlClient:
        c = ControlClient(self.root_path if who == "root" else self.op_path, **kw)
        self.clients.append(c)
        return c

    def attached(self) -> tuple[ControlClient, ControlClient]:
        op, root = self.client("op"), self.client("root")
        nonce = op.open_lease()
        assert root.call("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S1,
                         claims=attach_claims()) == {"generation": 0}
        return op, root

    def close(self) -> None:
        for c in self.clients:
            c.close()
        self.root_srv.stop()
        self.op_srv.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def env():
    e = Env()
    yield e
    e.close()


def test_lease_open_and_status(env):
    op = env.client("op")
    nonce = op.open_lease()
    assert type(nonce) is str and len(nonce) == 43 and op.lease_nonce == nonce
    assert op.call("STATUS") == {"records": []}


def test_attach_and_cas_return_the_answer_minus_id_and_ok(env):
    _op, root = env.attached()
    assert root.call("CAS", session=S1, generation=0, **{"from": R.ALLOCATED}, to=R.RENDERING,
                     fields={}) == {"generation": 1}


def test_a_refusal_raises_by_name_and_the_connection_stays_usable(env):
    op = env.client("op")
    with pytest.raises(Refused) as e:
        op.call("REVOKED-SET")
    assert e.value.name == "verb-not-allowed-for-peer"
    with pytest.raises(Refused) as e:
        op.call("STATUS", session="nope")
    assert e.value.name != "control-down"
    assert op.call("STATUS") == {"records": []}


def test_a_second_lease_open_is_refused(env):
    op = env.client("op")
    op.open_lease()
    with pytest.raises(Refused) as e:
        op.open_lease()
    assert e.value.name == "lease-exists"


def test_closing_the_lease_connection_revokes_its_session(env):
    op, root = env.attached()
    op.close()
    deadline = time.monotonic() + 5
    while env.store.get(S1).state != R.REVOKED and time.monotonic() < deadline:
        env.control.join_eof(0.1)
    assert env.store.get(S1).state == R.REVOKED
    with pytest.raises(Refused) as e:
        op.call("STATUS")
    assert e.value.name == "control-down"


def hold_lock(env: Env, seconds: float) -> threading.Thread:
    got = threading.Event()

    def run():
        with env.store.lock(S1):
            got.set()
            time.sleep(seconds)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert got.wait(2)
    return t


def test_locked_is_retried_until_the_lock_frees(env):
    _op, root = env.attached()
    sleeps: list[float] = []
    real_sleep = root.sleep
    root.sleep = lambda s: (sleeps.append(s), real_sleep(s))
    t = hold_lock(env, 0.3)
    t0 = time.monotonic()
    assert root.call("CAS", session=S1, generation=0, **{"from": R.ALLOCATED}, to=R.RENDERING,
                     fields={}) == {"generation": 1}
    assert time.monotonic() - t0 >= 0.2
    assert len(sleeps) >= 3 and sleeps[1] == 2 * sleeps[0]
    t.join()


def test_locked_gives_up_after_v23_in_total(env):
    """A fake clock that each backoff sleep advances: the default window is V23 = 10 s, total."""
    _op, root = env.attached()
    now = {"t": 0.0}
    root.clock = lambda: now["t"]
    root.sleep = lambda s: now.__setitem__("t", now["t"] + s)
    t = hold_lock(env, 1.0)
    with pytest.raises(Refused) as e:
        root.call("CAS", session=S1, generation=0, **{"from": R.ALLOCATED}, to=R.RENDERING, fields={})
    assert e.value.name == "locked"
    assert CC.V23_SECONDS == 10.0 and root.retry_window_s == 10.0
    assert now["t"] == pytest.approx(10.0)
    t.join()
    assert type(root.call("REVOKED-SET")) is dict        # a refusal by name leaves the connection good


def test_a_short_window_gives_up_in_real_time(env):
    _op, root = env.attached()
    root.retry_window_s = 0.2
    t = hold_lock(env, 1.0)
    t0 = time.monotonic()
    with pytest.raises(Refused) as e:
        root.call("CAS", session=S1, generation=0, **{"from": R.ALLOCATED}, to=R.RENDERING, fields={})
    assert e.value.name == "locked"
    assert 0.2 <= time.monotonic() - t0 < 0.9
    t.join()


def test_no_server_is_control_down(env):
    with pytest.raises(Refused) as e:
        ControlClient(env.dir / "absent.sock")
    assert e.value.name == "control-down"


def test_a_server_that_goes_away_is_control_down_and_closes(env):
    op = env.client("op")
    op.open_lease()
    env.op_srv.stop()
    with pytest.raises(Refused) as e:
        op.call("STATUS")
    assert e.value.name == "control-down"
    with pytest.raises(Refused):
        op.fileno()


def test_fileno_is_close_on_exec(env):
    op = env.client("op")
    assert os.get_inheritable(op.fileno()) is False


def test_the_frames_own_keys_are_refused_before_sending(env):
    op = env.client("op")
    for k in ("v", "id"):
        with pytest.raises(Refused) as e:
            op.call("STATUS", **{k: 1})
        assert e.value.name == "bad-request"
    assert op.call("STATUS") == {"records": []}


def test_an_over_cap_request_is_refused_unsent(env):
    op = env.client("op")
    with pytest.raises(Refused) as e:
        op.call("STATUS", session="x" * (C.FRAME_MAX + 1))
    assert e.value.name == "frame-too-large"
    assert op.call("STATUS") == {"records": []}


# --- a hand-answered socket -------------------------------------------------------------------------------

class Raw:
    """Accepts one connection; ``answer(req) -> dict | bytes | None`` (None = say nothing)."""

    def __init__(self, answer) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="lvr", dir="/tmp"))
        self.path = self.dir / "raw.sock"
        self.lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lst.bind(str(self.path))
        self.lst.listen(1)
        self.answer = answer
        self.done = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        s, _ = self.lst.accept()
        try:
            while True:
                req = C.read_frame(s)
                if req is None:
                    return
                out = self.answer(req)
                if out == b"":
                    return                       # close with no answer: the client reads EOF
                if out is None:
                    self.done.wait(5)
                    return
                s.sendall(out if isinstance(out, bytes) else C.encode_frame(out))
        except (OSError, C.FrameMalformed):
            return
        finally:
            s.close()

    def close(self):
        self.done.set()
        self.lst.close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.mark.parametrize("bad", [
    lambda r: {"id": r["id"] + 1, "ok": True},                    # another request's answer
    lambda r: {"id": r["id"], "ok": "yes"},
    lambda r: {"id": r["id"], "ok": False},                       # a refusal with no name
    lambda r: {"id": r["id"], "ok": False, "refused": 7},
    lambda r: b"\x00\x00\x00\x02{}",                              # a frame, but no id / ok
    lambda r: b"\x00\x00\x00\x05{bad}",
])
def test_an_answer_off_the_wire_is_control_down(bad):
    raw = Raw(bad)
    try:
        c = ControlClient(raw.path)
        with pytest.raises(Refused) as e:
            c.call("STATUS")
        assert e.value.name == "control-down"
        with pytest.raises(Refused):
            c.fileno()
    finally:
        raw.close()


def test_eof_instead_of_an_answer_is_control_down():
    raw = Raw(lambda r: b"")
    try:
        c = ControlClient(raw.path)
        with pytest.raises(Refused) as e:
            c.call("STATUS")
        assert e.value.name == "control-down" and "EOF" in e.value.detail
        with pytest.raises(Refused):
            c.fileno()
    finally:
        raw.close()


def test_an_over_cap_refusal_with_null_id_closes():
    raw = Raw(lambda r: {"id": None, "ok": False, "refused": "frame-too-large"})
    try:
        c = ControlClient(raw.path)
        with pytest.raises(Refused) as e:
            c.call("STATUS")
        assert e.value.name == "frame-too-large"
        with pytest.raises(Refused):
            c.fileno()
    finally:
        raw.close()


def test_a_silent_server_times_out_as_control_down():
    raw = Raw(lambda r: None)
    try:
        c = ControlClient(raw.path, timeout=0.2)
        t0 = time.monotonic()
        with pytest.raises(Refused) as e:
            c.call("STATUS")
        assert e.value.name == "control-down"
        assert time.monotonic() - t0 < 2
    finally:
        raw.close()


def test_lease_open_without_a_nonce_is_control_down():
    raw = Raw(lambda r: {"id": r["id"], "ok": True})
    try:
        c = ControlClient(raw.path)
        with pytest.raises(Refused) as e:
            c.open_lease()
        assert e.value.name == "control-down"
    finally:
        raw.close()
