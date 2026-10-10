"""K2b-1 broker (design §3, §4.2, §6, §10 item 1): the reported spore DONE lines against real anneal
SporeStores and real ssh-keygen keys, in throwaway directories under the home directory (a signers
file is a trust anchor, and a world-writable ancestor such as /tmp is refused). The one allowed mock
is statfs, for the network-filesystem refusal. A fault is injected by wrapping the real spore
adapter, never by mocking anneal."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from anneal_memory.spores import SporeStore

from levain.autonomic.confirm import NOT_VERIFIED, UNAVAILABLE, VERIFIED, verify_cockpit_signature
from levain.cockpit import install
from levain.cockpit.broker import STANDBY_FAULT, Broker, BrokerError, Lifecycle, label_class
from levain.cockpit.consent import value_sha256
from levain.cockpit.effects import SporeEffectStore, TransientApplyFault
from levain.cockpit.results import Fault

BROWSER = {"class": "token", "name": "browser"}
PHONE = {"class": "token", "name": "flowconnect"}


@pytest.fixture
def home() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix=".levain-k2b-broker-", dir=Path.home()))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def spores(home: Path) -> Path:
    p = home / "spores.json"
    st = SporeStore(p)
    st.add(type="task", text="a loop", origin_key="k.loop")
    st.add(type="thought", text="a tray seed", disposition="seed", origin_key="k.seed")
    st.add(type="thought", text="another seed", disposition="seed", origin_key="k.seed2")
    return p


def _broker(home: Path, spores: Path, store: Any = None, **kw: Any) -> Broker:
    kw.setdefault("install_class", "reported")
    b = Broker(home / "data", stores={"spores": store or SporeEffectStore(spores)},
               store_paths=[spores], **kw)
    b.start()
    return b


@pytest.fixture
def broker(home: Path, spores: Path) -> Iterator[Broker]:
    b = _broker(home, spores)
    try:
        yield b
    finally:
        b.stop()


def _target(spores: Path, key: str) -> dict[str, Any]:
    row = SporeStore(spores).get_by_origin_key(key)
    assert row is not None
    version = SporeEffectStore(spores).snapshot(f"spore:{key}").version
    return {"kind": "row", "panel_id": "tray", "row_id": f"spore:{row['id']}", "row_version": version}


def _move(b: Broker, spores: Path, key: str = "k.seed", *, cred: dict = BROWSER, **kw: Any) -> dict:
    kw.setdefault("tier", "T2")
    return b.issue(verb="spore_set_disposition", params={"disposition": "note"},
                   target=_target(spores, key), credential=cred, **kw)


def _refused(code: str, fn: Any, *a: Any, **kw: Any) -> BrokerError:
    with pytest.raises(BrokerError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code, f"{ei.value.code}: {ei.value}"
    return ei.value


def _rows(b: Broker, sql: str, *args: Any) -> list[tuple]:
    conn = sqlite3.connect(b.data_root / "cockpit.db")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _disposition(spores: Path, key: str) -> str | None:
    row = SporeStore(spores).get_by_origin_key(key)
    assert row is not None
    return row.get("disposition")


def _wait(pred: Any, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _label(b: Broker, resource_key: str, field: str, value: Any) -> str:
    """The label class of a field's current value (§5), read from the journal's label rows."""
    assert b.db is not None
    row = b.db.label_for(resource_key, field, value_sha256(value))
    return "unverified" if row is None else label_class(row.decision_kind, b.install_class)


def _state(b: Broker, pid: str) -> str:
    return _rows(b, "SELECT state FROM pendings WHERE pending_id = ?", pid)[0][0]


# --- keys --------------------------------------------------------------------------------------------

class Key:
    """A throwaway ed25519 key, enrolled into the broker's ``keys`` table at a class."""

    def __init__(self, d: Path, name: str) -> None:
        self.path = d / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.path)],
                       check=True, timeout=30, capture_output=True)
        self.public = (d / f"{name}.pub").read_text().strip()
        out = subprocess.run(["ssh-keygen", "-lf", str(d / f"{name}.pub")], check=True, timeout=30,
                             capture_output=True, text=True).stdout.split()
        self.fingerprint = out[1]
        self.principal = "k" + hashlib.sha256(self.fingerprint.encode()).hexdigest()[:32]

    def sign(self, message: bytes, namespace: str = "levain-cockpit") -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.path), "-n", namespace],
                              input=message, capture_output=True, timeout=30, check=True).stdout.decode()

    def enroll(self, b: Broker, cls: str) -> "Key":
        assert b.db is not None
        with b.db.write() as conn:
            conn.execute("INSERT INTO keys (fingerprint, principal, public_key, class, enrolled_at, "
                         "enrolled_via) VALUES (?, ?, ?, ?, '2026-10-10T00:00:00.000000Z', 'laptop cli')",
                         (self.fingerprint, self.principal, self.public, cls))
        return self


@pytest.fixture
def keydir(home: Path) -> Path:
    d = home / "keys"
    d.mkdir(mode=0o700)
    return d


# --- DONE: a T2 on the token fires as ALLOWED-REPORTED and reads `claims cockpit token` ----------------

def test_a_t2_on_the_token_fires_allowed_reported_and_reads_claims_cockpit_token(broker, spores):
    out = _move(broker, spores, intent="reported")
    assert (out["state"], out["decision_kind"], out["label"]) == ("COMMITTED", "ALLOWED_REPORTED",
                                                                  "claims cockpit token")
    assert _disposition(spores, "k.seed") == "note"
    assert _label(broker, "spore:k.seed", "disposition", "note") == "claims cockpit token"
    # the fire is journalled: no fire without a pending (a handler fired directly fails this line)
    assert _rows(broker, "SELECT verb, intent, state FROM pendings") == [
        ("spore_set_disposition", "reported", "COMMITTED")]
    # a later value written outside the broker is unlabelled
    SporeStore(spores).update(SporeStore(spores).get_by_origin_key("k.seed")["id"], disposition="seed")
    assert _label(broker, "spore:k.seed", "disposition", "seed") == "unverified"


def test_a_seed_created_on_the_token_is_journalled_with_its_native_id(broker, spores):
    out = broker.issue(verb="spore_seed", params={"text": "captured", "type": "task"},
                       target={"kind": "create", "panel_id": "tray"}, tier="T2", credential=BROWSER,
                       intent="reported")
    assert out["state"] == "COMMITTED"
    key = "ck." + out["pending_id"][2:] + ".0"
    row = SporeStore(spores).get_by_origin_key(key)
    assert row is not None and row["text"] == "captured"
    assert _rows(broker, "SELECT native_id, status FROM effects") == [(row["id"], "APPLIED")]
    assert _label(broker, f"spore:{key}", "text", "captured") == "claims cockpit token"


# --- DONE: sign returns a pending that fires only on a valid signature; a bad one spends nothing ------

def test_sign_returns_a_pending_that_fires_only_on_a_valid_signature(broker, spores, keydir):
    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    stranger = Key(keydir, "stranger")
    out = _move(broker, spores, intent="sign")
    pid = out["pending_id"]
    assert out["state"] == "WAITING" and _disposition(spores, "k.seed") == "seed"
    record = out["record"].encode()

    # a bad signature leaves WAITING and spends nothing: not enrolled, the wrong namespace, other bytes
    for bad in (stranger.sign(record), phone.sign(record, "levain-confirm"), phone.sign(record + b" "),
                "not a signature"):
        _refused("bad_signature", broker.submit, pid, bad)
        assert _rows(broker, "SELECT state, signature, decided_at FROM pendings") == [("WAITING", None, None)]
        assert _disposition(spores, "k.seed") == "seed"

    done = broker.submit(pid, phone.sign(record))
    assert (done["state"], done["decision_kind"], done["label"]) == ("COMMITTED", "ALLOWED", "signed · reported")
    assert _disposition(spores, "k.seed") == "note"
    assert _rows(broker, "SELECT signer_fp FROM pendings") == [(phone.fingerprint,)]
    # a resubmitted signature after the outcome returns it and fires nothing again
    before = spores.read_bytes()
    again = broker.submit(pid, phone.sign(record))
    assert (again["state"], again["replay"]) == ("COMMITTED", True) and spores.read_bytes() == before


def test_a_presence_key_cannot_sign_a_t2_and_a_revoked_key_is_not_in_the_signers_file(broker, spores, keydir):
    weak = Key(keydir, "weak").enroll(broker, "presence")
    pid = _move(broker, spores)["pending_id"]
    rec = broker.outcome(pid)["record"].encode()
    out = broker.submit(pid, weak.sign(rec))
    assert (out["state"], out["reason"]) == ("REFUSED", "key class presence does not cover T2")
    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    with broker.db.write() as conn:
        conn.execute("UPDATE keys SET revoked_at = '2026-10-10T00:00:00.000000Z' WHERE fingerprint = ?",
                     (phone.fingerprint,))
    pid2 = _move(broker, spores, "k.seed2")["pending_id"]
    _refused("bad_signature", broker.submit, pid2, phone.sign(broker.outcome(pid2)["record"].encode()))
    assert _state(broker, pid2) == "WAITING"


# --- DONE: replay; no intent = sign; T3 on the token refused --------------------------------------------

def test_a_replayed_token_post_returns_the_same_fired_pending(broker, spores):
    first = _move(broker, spores, intent="reported", idempotency_key="k-1")
    after = spores.read_bytes()
    again = _move(broker, spores, intent="reported", idempotency_key="k-1")
    assert (again["pending_id"], again["state"], again["replay"]) == (first["pending_id"], "COMMITTED", True)
    assert spores.read_bytes() == after
    assert len(_rows(broker, "SELECT 1 FROM pendings")) == 1
    # the same key with other params is a conflict, never a second fire
    _refused("idempotency_conflict", broker.issue, verb="spore_set_disposition", params={"disposition": "seed"},
             target=_target(spores, "k.seed"), tier="T2", credential=BROWSER, intent="reported",
             idempotency_key="k-1")


def test_no_intent_is_sign(broker, spores):
    out = _move(broker, spores)
    assert (out["state"], out["intent"]) == ("WAITING", "sign")
    assert _disposition(spores, "k.seed") == "seed"


def test_a_t3_on_the_token_alone_is_refused_and_records_nothing(broker, spores):
    e = _refused("refused", _move, broker, spores, intent="reported", tier="T3")
    assert "T3" in str(e)
    assert _rows(broker, "SELECT 1 FROM pendings") == []
    _refused("refused", broker.issue, verb="spore_set_disposition", params={"disposition": "note"},
             target=_target(spores, "k.seed"), tier="T2", credential={"class": "device", "fingerprint": "SHA256:x"},
             intent="reported")


def test_a_row_without_an_origin_key_is_refused(broker, spores):
    # anneal keys an unkeyed row on read unless its save fails; a read-only directory makes it fail,
    # so the row reaches the broker with no origin_key, and none is invented
    data = json.loads(spores.read_text())
    for s in data["spores"]:
        if s.get("origin_key") == "k.loop":
            del s["origin_key"]
            sid = s["id"]
    spores.write_text(json.dumps(data))
    spores.parent.chmod(0o500)
    try:
        e = _refused("refused", broker.issue, verb="spore_set_disposition", params={"disposition": "note"},
                     target={"kind": "row", "panel_id": "loops", "row_id": f"spore:{sid}"}, tier="T2",
                     credential=BROWSER, intent="reported")
    finally:
        spores.parent.chmod(0o700)
    assert "no label resource yet" in str(e)
    assert _rows(broker, "SELECT 1 FROM pendings") == []


# --- DONE: caps; cancel own; cross-principal cancel; rotation -----------------------------------------

def test_the_cap_holds_per_token_and_a_reported_fire_still_goes_through(broker, spores):
    for i in range(8):
        _move(broker, spores, idempotency_key=f"s{i}")
    _refused("pending_cap", _move, broker, spores, idempotency_key="s8")
    # the other surface's token is not blocked, and a reported write never counts against the cap
    assert _move(broker, spores, cred=PHONE)["state"] == "WAITING"
    assert _move(broker, spores, intent="reported", idempotency_key="r")["state"] == "COMMITTED"
    # cancelling one occupying pending frees a slot and records who cancelled it
    pid = _rows(broker, "SELECT pending_id FROM pendings WHERE idempotency_key = 's0'")[0][0]
    assert broker.cancel(pid, credential=BROWSER)["state"] == "CANCELLED"
    assert _rows(broker, "SELECT cancelled_by FROM pendings WHERE pending_id = ?", pid) == [("token:browser:1",)]
    assert _move(broker, spores, "k.seed2", idempotency_key="s9")["state"] == "WAITING"


def test_cancel_after_the_commit_decision_is_refused(broker, spores):
    pid = _move(broker, spores, intent="reported")["pending_id"]
    _refused("refused", broker.cancel, pid, credential=BROWSER)
    assert _state(broker, pid) == "COMMITTED"


def test_a_cross_principal_cancel_is_a_pending_of_the_targets_tier(broker, spores):
    pid = _move(broker, spores)["pending_id"]
    out = broker.cancel(pid, credential=PHONE)
    assert out["verb"] == "pending_cancel" and out["tier"] == "T2" and out["state"] == "WAITING"
    assert _state(broker, pid) == "WAITING"
    done = broker.cancel(pid, credential=PHONE, intent="reported")
    assert done["state"] == "COMMITTED"
    assert _rows(broker, "SELECT state, cancelled_by FROM pendings WHERE pending_id = ?", pid) == [
        ("CANCELLED", "token:flowconnect:1")]
    # the first cross-principal pending now targets a cancelled pending: deciding it is STALE
    assert broker.cancel(out["pending_id"], credential=PHONE)["state"] == "CANCELLED"


def test_rotation_cancels_the_old_generations_waiting_rows_in_one_act(broker, spores, keydir):
    laptop = Key(keydir, "laptop").enroll(broker, "laptop-presence")
    phone = Key(keydir, "phone").enroll(broker, "display-phone")
    olds = [_move(broker, spores, idempotency_key=f"o{i}")["pending_id"] for i in range(2)]
    other = _move(broker, spores, cred=PHONE)["pending_id"]
    _refused("refused", broker.rotate_token, "browser", credential=BROWSER, intent="reported")
    rot = broker.rotate_token("browser", credential=PHONE)
    assert (rot["tier"], rot["state"]) == ("T3", "WAITING")
    weak = broker.submit(rot["pending_id"], phone.sign(rot["record"].encode()))
    assert (weak["state"], weak["reason"]) == ("REFUSED", "key class display-phone does not cover T3")
    rot = broker.rotate_token("browser", credential=PHONE, idempotency_key="rot-2")
    assert broker.submit(rot["pending_id"], laptop.sign(rot["record"].encode()))["state"] == "COMMITTED"
    assert _rows(broker, "SELECT generation FROM tokens WHERE name = 'browser'") == [(2,)]
    for pid in olds:
        assert _rows(broker, "SELECT state, reason FROM pendings WHERE pending_id = ?", pid) == [
            ("CANCELLED", "token rotated")]
    assert _state(broker, other) == "WAITING"
    # the token's next pending is the new generation's
    new = _move(broker, spores, idempotency_key="after")["pending_id"]
    assert _rows(broker, "SELECT principal FROM pendings WHERE pending_id = ?", new) == [("token:browser:2",)]


# --- DONE: lock, STANDBY, heal, DRAINING ---------------------------------------------------------------

def test_a_second_broker_on_the_root_is_standby_opens_no_journal_and_heals(home, spores):
    first = _broker(home, spores)
    pid = _move(first, spores)["pending_id"]
    second = _broker(home, spores, lock_poll=0.05)
    try:
        assert second.state is Lifecycle.STANDBY and second.db is None
        assert second.outcome(pid) == Fault(STANDBY_FAULT)
        _refused("standby", _move, second, spores, "k.seed2")
        first.stop()
        assert _wait(lambda: second.state is Lifecycle.ACTIVE, 3.0)
        assert second.outcome(pid)["state"] == "WAITING"
    finally:
        first.stop()
        second.stop()


def test_a_standby_broker_opens_no_cockpit_db(home, spores, monkeypatch):
    first = _broker(home, spores, max_wait=60.0)
    time.sleep(0.3)          # the first broker's thread has made its pass and waits: it opens nothing now
    opened: list[Path] = []
    import levain.cockpit.firejournal as fj
    real = fj.open_durable
    monkeypatch.setattr(fj, "open_durable", lambda p: (opened.append(Path(p)), real(p))[1])
    second = _broker(home, spores, lock_poll=0.05)
    try:
        second.outcome("cp" + "0" * 32)
        time.sleep(0.2)
        assert opened == [] and second.state is Lifecycle.STANDBY
    finally:
        first.stop()
        second.stop()


class Blocking(SporeEffectStore):
    """The real adapter, held inside apply_unit until released."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()

    def apply_unit(self, unit: Any) -> Any:
        self.entered.set()
        self.release.wait(10)
        return super().apply_unit(unit)


def test_draining_holds_the_lock_while_an_admitted_settler_is_blocked(home, spores):
    store = Blocking(spores)
    first = _broker(home, spores, store, drain_timeout=10)
    t = threading.Thread(target=lambda: _move(first, spores, intent="reported"), daemon=True)
    t.start()
    assert store.entered.wait(5)
    stopper = threading.Thread(target=first.stop, daemon=True)
    stopper.start()
    assert _wait(lambda: first.state is Lifecycle.DRAINING, 2)
    second = _broker(home, spores, lock_poll=0.05)
    try:
        time.sleep(0.3)
        assert second.state is Lifecycle.STANDBY       # the lock is still the draining broker's
        _refused("broker_unavailable", _move, first, spores, "k.seed2")
        store.release.set()
        t.join(5)
        stopper.join(5)
        assert first.state is Lifecycle.CLOSED
        assert _wait(lambda: second.state is Lifecycle.ACTIVE, 3.0)
        assert _disposition(spores, "k.seed") == "note"
    finally:
        store.release.set()
        second.stop()


def test_a_settler_that_outlives_the_drain_keeps_the_lock(home, spores):
    store = Blocking(spores)
    first = _broker(home, spores, store, drain_timeout=0.3)
    t = threading.Thread(target=lambda: _move(first, spores, intent="reported"), daemon=True)
    t.start()
    assert store.entered.wait(5)
    assert first.stop() is Lifecycle.CLOSED
    second = _broker(home, spores, lock_poll=0.05)
    try:
        store.release.set()
        t.join(5)
        time.sleep(0.3)
        assert second.state is Lifecycle.STANDBY
    finally:
        second.stop()


# --- DONE: re-drive ------------------------------------------------------------------------------------

class FaultOnce(SporeEffectStore):
    """The real adapter, raising one transient fault on its first apply."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.calls = 0

    def apply_unit(self, unit: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise TransientApplyFault("OSError", "injected")
        return super().apply_unit(unit)


def test_a_fault_with_no_reads_retries_by_itself(home, spores):
    store = FaultOnce(spores)
    b = _broker(home, spores, store)
    try:
        out = _move(b, spores, intent="reported")
        assert out["state"] == "applying"
        assert _rows(b, "SELECT attempts, status FROM effects") == [(1, "PLANNED")]
        # no read of any kind from here: only the thread can finish it, at next_attempt_at (5 s)
        assert _wait(lambda: _state(b, out["pending_id"]) == "COMMITTED", 9.0)
        assert store.calls == 2 and _disposition(spores, "k.seed") == "note"
    finally:
        b.stop()


def test_a_new_earlier_deadline_cuts_a_long_wait(home, spores):
    store = FaultOnce(spores)
    b = _broker(home, spores, store, max_wait=60.0)
    try:
        time.sleep(0.3)                     # the thread is now in a 60 s wait (nothing is due)
        out = _move(b, spores, intent="reported")
        assert out["state"] == "applying"
        assert _wait(lambda: _state(b, out["pending_id"]) == "COMMITTED", 9.0)
    finally:
        b.stop()


def test_listings_wake_but_never_spend_an_attempt(home, spores):
    class AlwaysFault(SporeEffectStore):
        calls = 0

        def apply_unit(self, unit: Any) -> Any:
            type(self).calls += 1
            raise TransientApplyFault("OSError", "down")

    b = _broker(home, spores, AlwaysFault(spores))
    try:
        pid = _move(b, spores, intent="reported")["pending_id"]
        for _ in range(40):
            assert b.outcome(pid)["state"] == "applying"
            time.sleep(0.02)
        assert _rows(b, "SELECT attempts FROM effects") == [(1,)]
    finally:
        b.stop()


def test_start_recovery_finishes_a_decided_op(home, spores):
    store = FaultOnce(spores)
    b = _broker(home, spores, store)
    pid = _move(b, spores, intent="reported")["pending_id"]
    b.stop()
    assert _state(b, pid) == "DECIDED"
    with sqlite3.connect(b.data_root / "cockpit.db") as conn:
        conn.execute("UPDATE effects SET next_attempt_at = '2000-01-01T00:00:00.000000Z'")
    again = _broker(home, spores, store)
    try:
        assert _state(again, pid) == "COMMITTED" and _disposition(spores, "k.seed") == "note"
    finally:
        again.stop()


# --- DONE: install class and the network-FS refusal -----------------------------------------------------

def test_a_network_fs_data_root_is_refused(home, spores, monkeypatch):
    monkeypatch.setattr(install, "statfs_type", lambda p: "nfs" if install.sys.platform == "darwin" else "0x6969")
    b = _broker(home, spores)
    assert b.state is Lifecycle.INSTALL_DISABLED
    assert "network filesystem" in (b.disabled_reason or "")
    assert not (home / "data" / "cockpit.lock").exists() and not (home / "data" / "cockpit.db").exists()
    b.stop()
    assert b.state is Lifecycle.INSTALL_DISABLED


def test_a_local_fs_is_accepted(home, spores):
    assert install.network_fs_problem([home, spores]) is None


def test_no_declaration_disables_the_broker_with_run_the_installer(home, spores):
    b = _broker(home, spores, install_class=None)
    assert b.state is Lifecycle.INSTALL_DISABLED and "run the installer" in (b.disabled_reason or "")
    assert not (home / "data").exists()
    e = _refused("install_disabled", _move, b, spores)
    assert "run the installer" in str(e)


def test_a_governed_marker_is_a_typed_refusal_and_beats_reported(home, spores):
    markers = home / "markers"
    markers.mkdir()
    path = install.marker_path(home / "data", markers)
    assert path is not None
    path.write_text("{}")
    assert install.decide_install(None, home / "data", marker_directory=markers).reason == \
        "governed install not built"
    assert "cannot declare it away" in (install.decide_install(
        "reported", home / "data", marker_directory=markers).reason or "")
    # another data root's marker is ignored
    assert install.decide_install("reported", home / "other", marker_directory=markers).install_class == "reported"


# --- verify_cockpit_signature (§6.5) -------------------------------------------------------------------

@contextmanager
def _signers(d: Path, lines: list[str]) -> Iterator[Path]:
    p = d / "signers"
    p.write_text("".join(f'{ln}\n' for ln in lines))
    p.chmod(0o600)
    yield p


def test_verify_cockpit_signature_names_the_signer_from_the_signature(keydir):
    a, b = Key(keydir, "a"), Key(keydir, "b")
    me = frozenset({0, __import__("os").getuid()})
    msg = b"the stored bytes"
    with _signers(keydir, [f'{a.principal} namespaces="levain-cockpit" {a.public}',
                           f'{b.principal} namespaces="levain-cockpit" {b.public}']) as f:
        v = verify_cockpit_signature(msg, b.sign(msg), f, owners=me)
        assert (v.status, v.principal, v.fingerprint) == (VERIFIED, b.principal, b.fingerprint)
        assert verify_cockpit_signature(msg, a.sign(msg, "levain-confirm"), f, owners=me).status == NOT_VERIFIED
        assert verify_cockpit_signature(msg + b"!", a.sign(msg), f, owners=me).status == NOT_VERIFIED
        # root-only owners refuse an operator-owned signers file: the reported owners are stated, never implied
        assert verify_cockpit_signature(msg, a.sign(msg), f).status == UNAVAILABLE


# --- wiring: the POST routes ---------------------------------------------------------------------------

def test_the_post_routes_reach_the_broker_only_in_a_declared_install(tmp_path, monkeypatch):
    from tests.test_cockpit_k2a import _install, _post, _serve

    root, src, ids = _install(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    body = {"verb": "spore_set_disposition", "panel_id": "tray", "params": {"disposition": "note"}}

    def row(url: str) -> dict:
        from tests.test_cockpit_k2a import _get
        rows = _get(f"{url}/cockpit/panel/tray.json", token="T")["rows"]
        return next(r for r in rows if r["id"] == f"spore:{ids['seed']}")

    # with the broker stubbed out (no declaration) the POST handler fires nothing
    with _serve(src, surface_tokens={"browser": "T"}) as (url, httpd):
        r = row(url)
        status, out = _post(f"{url}/cockpit/verb", {**body, "row_id": r["id"], "row_version": r["version"],
                                                    "intent": "reported"}, token="T")
        assert (status, out["error"]) == (403, "needs_broker") and "run the installer" in out["message"]
        assert store.get(ids["seed"]).get("disposition") == "seed"

    # a reported install: no intent returns a pending; the token fires; the pending routes answer
    with _serve(src, surface_tokens={"browser": "T", "flowconnect": "P"}, install_class="reported") as (url, httpd):
        r = row(url)
        at = {**body, "row_id": r["id"], "row_version": r["version"]}
        status, held = _post(f"{url}/cockpit/verb", at, token="T")
        assert (status, held["state"], held["intent"]) == (200, "WAITING", "sign"), held
        pid = held["pending_id"]
        status, out = _post(f"{url}/cockpit/verb", {**at, "intent": "reported", "idempotency_key": "i1"}, token="T")
        assert (status, out["state"], out["label"]) == (200, "COMMITTED", "claims cockpit token"), out
        assert store.get(ids["seed"]).get("disposition") == "note"
        status, again = _post(f"{url}/cockpit/verb", {**at, "intent": "reported", "idempotency_key": "i1"}, token="T")
        assert (status, again["pending_id"], again["replay"]) == (200, out["pending_id"], True)
        status, bad = _post(f"{url}/cockpit/pending/{pid}/signature", {"signature": "nope"}, token="T")
        assert (status, bad["error"]) == (400, "bad_signature")
        status, other = _post(f"{url}/cockpit/pending/{pid}/cancel", {}, token="P")
        assert (status, other["verb"], other["state"]) == (200, "pending_cancel", "WAITING"), other
        status, mine = _post(f"{url}/cockpit/pending/{pid}/cancel", {}, token="T")
        assert (status, mine["state"]) == (200, "CANCELLED")
        status, _none = _post(f"{url}/cockpit/pending/{pid}/cancel", {}, token=None)
        assert status == 403
        broker = httpd.broker
    assert broker.state is Lifecycle.CLOSED

