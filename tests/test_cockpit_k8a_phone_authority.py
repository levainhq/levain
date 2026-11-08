"""K8a: the phone's write authority (K8_AUTHORITY_DESIGN.md v2, B5's conditions in K8_AUTHORITY_B5.md).

Every device write is a broker pending its passkey signs, C1 included; a passkey signs only in
``levain-cockpit-phone`` (its signer line names nothing else), only a pending its own phone issued, only a phone verb,
T2 at most, and only with WebAuthn user verification from the key's own origin, checked before ssh-keygen runs. The
passkey here is a software P-256 key driven exactly as a WebAuthn authenticator signs (authenticatorData ||
SHA-256(clientDataJSON)), converted by levain's own ``to_sshsig`` and verified by the real ``/usr/bin/ssh-keygen``:
a virtual authenticator, not a mock. The browser ceremony and the real iPhone are not run here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from anneal_memory.spores import SporeStore  # noqa: E402

import levain.consent.passkey as P  # noqa: E402
from levain.autonomic.confirm import COCKPIT_NAMESPACE, COCKPIT_PHONE_NAMESPACE  # noqa: E402
from levain.cockpit.broker import Broker, BrokerError  # noqa: E402
from levain.cockpit.effects import SporeEffectStore  # noqa: E402
from levain.cockpit.firejournal import PENDING_CAP, CockpitDB  # noqa: E402
from levain.cockpit.keys import fingerprint_of  # noqa: E402
from levain.cockpit.verbs import DEVICE_VERBS, KERNEL_VERBS, gesture_for  # noqa: E402
from levain.contracts.namespaces import REGISTRY  # noqa: E402
from tests.autonomic_confirm_keys import anchor_scratch  # noqa: E402
from tests.cockpit_authority_rig import RealKey, enrol, enrol_any, root_of  # noqa: E402

BROWSER = {"class": "token", "name": "browser"}


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _armor(blob: bytes) -> str:
    text = base64.b64encode(blob).decode("ascii")
    return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(text[i:i + 70] for i in range(0, len(text), 70)) \
        + "\n-----END SSH SIGNATURE-----\n"


class SoftPasskey:
    """A P-256 key that signs as a WebAuthn platform authenticator does, on ``rp_id``."""

    def __init__(self, rp_id: str = "localhost") -> None:
        self.priv = ec.generate_private_key(ec.SECP256R1())
        spki = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        self.rp_id = rp_id
        self.public = P.public_key_line(spki, rp_id)
        self.fingerprint = fingerprint_of(self.public)
        self.counter = 0

    def assertion(self, chal: bytes, *, origin: str | None = None, uv: bool = True) -> tuple[bytes, bytes, bytes]:
        self.counter += 1
        flags = 0x01 | (0x04 if uv else 0)
        ad = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.counter)
        cdj = json.dumps({"type": "webauthn.get", "challenge": _b64u(chal),
                          "origin": origin or f"http://{self.rp_id}", "crossOrigin": False},
                         separators=(",", ":")).encode()
        der = self.priv.sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return ad, cdj, der

    def sign(self, message: bytes, namespace: str = COCKPIT_PHONE_NAMESPACE, **kw: Any) -> str:
        """An SSHSIG over ``message`` in ``namespace``, assembled WITHOUT to_sshsig's own checks (so a no-UV or
        wrong-origin signature reaches the broker, which must refuse it itself)."""
        ad, cdj, der = self.assertion(P.challenge(message, namespace), **kw)
        key_blob, _app = P._key_parts(self.public)
        origin = json.loads(cdj)["origin"]
        sig = (P._string(P.SIG_TYPE) + P._string(P._ecdsa_signature(der)) + bytes([ad[32]]) + ad[33:37]
               + P._string(origin) + P._string(cdj) + P._string(b""))
        return _armor(b"SSHSIG" + struct.pack(">I", 1) + P._string(key_blob) + P._string(namespace) + P._string(b"")
                      + P._string("sha512") + P._string(sig))


# --- the broker, against real anneal spores and real keys ---------------------------------------------------------

@pytest.fixture
def home() -> Iterator[Path]:
    d = anchor_scratch("k8a-")   # a signers dir needs ancestors nobody else writes
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def spores(home: Path) -> Path:
    p = home / "spores.json"
    st = SporeStore(p)
    for i in range(PENDING_CAP + 3):
        st.add(type="thought", text=f"seed {i}", disposition="seed", origin_key=f"k.s{i}")
    return p


@pytest.fixture
def world(home: Path, spores: Path) -> Iterator[SimpleNamespace]:
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
               install_class="reported")
    b.start()
    keys = home / "keys"
    keys.mkdir(mode=0o700)
    laptop = RealKey(keys, "laptop")
    assert b.db is not None
    enrol_any(b.db, laptop, "laptop-presence")
    phone = SoftPasskey()
    enrol(b.db, phone, "display-phone", by=laptop, custody="passkey")    # type: ignore[arg-type]
    try:
        yield SimpleNamespace(b=b, laptop=laptop, phone=phone, keys=keys, spores=spores,
                              dev={"class": "device", "fingerprint": phone.fingerprint, "transport": "paired-a"})
    finally:
        b.stop()


def _target(spores: Path, i: int) -> dict[str, Any]:
    row = SporeStore(spores).get_by_origin_key(f"k.s{i}")
    assert row is not None
    version = SporeEffectStore(spores).snapshot(f"spore:k.s{i}").version
    return {"kind": "row", "panel_id": "tray", "row_id": f"spore:{row['id']}", "row_version": version}


def _text(spores: Path, i: int) -> str:
    row = SporeStore(spores).get_by_origin_key(f"k.s{i}")
    assert row is not None
    return str(row.get("text"))


def _edit(w: SimpleNamespace, i: int = 0, *, cred: dict | None = None, tier: str = "C1", text: str = "edited") -> dict:
    return w.b.issue(verb="spore_update", params={"text": text}, target=_target(w.spores, i), tier=tier,
                     credential=cred or w.dev)


def _refused(code: str, fn: Any, *a: Any, **kw: Any) -> BrokerError:
    with pytest.raises(BrokerError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code, f"{ei.value.code}: {ei.value}"
    return ei.value


def _state(w: SimpleNamespace, pid: str) -> str:
    return w.b.outcome(pid)["state"]


def test_a_device_c1_is_a_pending_that_applies_only_once_its_passkey_signs(world):
    w = world
    out = _edit(w)
    assert out["state"] == "WAITING" and _text(w.spores, 0) == "seed 0"      # nothing on the bearer token alone
    done = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert (done["state"], done["decision_kind"]) == ("COMMITTED", "ALLOWED")
    assert _text(w.spores, 0) == "edited"
    assert w.b.db is not None and w.b.db.load(out["pending_id"]).signer_fp == w.phone.fingerprint
    # a token's C1 is still never a broker write; the gesture for a device's C1 is sign, never direct
    _refused("bad_request", _edit, w, 1, cred=BROWSER)
    assert gesture_for("C1", "device", "reported") == "sign" and gesture_for("C1", "token", "reported") == "direct"
    assert gesture_for("T3", "device", "reported") == "refused"


def test_a_passkey_signature_in_the_laptop_or_consent_namespace_does_not_verify(world):
    w = world
    out = _edit(w)
    rec = out["record"].encode()
    for ns in (COCKPIT_NAMESPACE, "levain-consent-answer", "levain-authority"):
        _refused("bad_signature", w.b.submit, out["pending_id"], w.phone.sign(rec, ns))
        assert _state(w, out["pending_id"]) == "WAITING" and _text(w.spores, 0) == "seed 0"
    # the passkey's signature over other bytes does not verify either
    _refused("bad_signature", w.b.submit, out["pending_id"], w.phone.sign(rec + b" "))
    assert "levain-cockpit-phone" in REGISTRY


def test_a_laptop_key_signs_as_before_and_never_in_the_phone_namespace(world):
    w = world
    # a laptop key signs a token's T2 pending as before
    t2 = _edit(w, 1, cred=BROWSER, tier="T2", text="by the laptop")
    assert w.b.submit(t2["pending_id"], w.laptop.sign(t2["record"].encode()))["state"] == "COMMITTED"
    assert _text(w.spores, 1) == "by the laptop"
    # a laptop key's signature in the phone namespace does not verify; in its own it signs a phone's pending
    out = _edit(w, 2, text="phone filed, laptop signed")
    _refused("bad_signature", w.b.submit, out["pending_id"], w.laptop.sign(out["record"].encode(),
                                                                           COCKPIT_PHONE_NAMESPACE))
    assert w.b.submit(out["pending_id"], w.laptop.sign(out["record"].encode()))["state"] == "COMMITTED"


def test_a_passkey_never_signs_a_t3_a_laptop_pending_another_phones_or_a_non_phone_verb(world):
    w = world
    t3 = _edit(w, 0, tier="T3")
    e = _refused("bad_signature", w.b.submit, t3["pending_id"], w.phone.sign(t3["record"].encode()))
    assert "T3" in str(e) and _state(w, t3["pending_id"]) == "WAITING"
    laptop_pending = _edit(w, 1, cred=BROWSER, tier="T2")
    e = _refused("bad_signature", w.b.submit, laptop_pending["pending_id"],
                 w.phone.sign(laptop_pending["record"].encode()))
    assert "its own phone" in str(e)
    other = _edit(w, 2, cred={"class": "device", "fingerprint": "SHA256:" + "A" * 43, "transport": "run"})
    _refused("bad_signature", w.b.submit, other["pending_id"], w.phone.sign(other["record"].encode()))
    # the verb set: the kernel's device verbs and own cancel, nothing B5 excluded
    key = w.b.capture().authority.key(w.phone.fingerprint)
    view = SimpleNamespace(principal=f"device:{w.phone.fingerprint}@run", record={"verb": "section_edit", "tier": "T2"})
    assert "not a phone verb" in str(w.b._passkey_refusal(key, "x", view))
    assert DEVICE_VERBS == {"spore_descend", "spore_surface_at", "spore_update", "spore_set_disposition",
                            "spore_ascend", "spore_seed", "list_intake"}
    assert not {"section_edit", "episode_tombstone", "undo", "question_answer", "operator_state", "config",
                "entity_name", "spore_touch"} & DEVICE_VERBS
    assert all(v.floor != "T3" for v in KERNEL_VERBS if v.device_ok)


def test_uv_missing_or_another_origin_is_refused_before_ssh_keygen_runs(world):
    w = world
    out = _edit(w)
    rec = out["record"].encode()
    w.b.ssh_keygen = Path("/nonexistent/ssh-keygen")      # were ssh-keygen reached, the answer would be a 503
    e = _refused("bad_signature", w.b.submit, out["pending_id"], w.phone.sign(rec, uv=False))
    assert "did not verify the user" in str(e)
    e = _refused("bad_signature", w.b.submit, out["pending_id"], w.phone.sign(rec, origin="https://evil.example"))
    assert "not https://localhost" in str(e) or "page that is not" in str(e)
    assert _state(w, out["pending_id"]) == "WAITING"


def test_a_token_holder_claiming_the_phones_key_cannot_cancel_its_pending(world):
    w = world
    mine = _edit(w)["pending_id"]
    thief = {"class": "device", "fingerprint": w.phone.fingerprint, "transport": "run"}
    # another transport claiming the phone's key: a cancel is a pending it cannot sign
    c = w.b.cancel(mine, credential=thief)
    assert c["state"] == "WAITING" and _state(w, mine) == "WAITING"
    stranger = SoftPasskey()
    _refused("bad_signature", w.b.submit, c["pending_id"], stranger.sign(c["record"].encode()))
    assert _state(w, mine) == "WAITING"
    # the phone's own cancel is never applied on the claim either: it waits for its passkey, then applies
    own = w.b.cancel(mine, credential=w.dev)
    assert own["state"] == "WAITING" and _state(w, mine) == "WAITING"
    assert w.b.submit(own["pending_id"], w.phone.sign(own["record"].encode()))["state"] == "COMMITTED"
    assert _state(w, mine) == "CANCELLED"


def test_the_pending_cap_is_held_per_transport_so_a_claim_cannot_fill_the_phones(world):
    w = world
    thief = {"class": "device", "fingerprint": w.phone.fingerprint, "transport": "run"}
    for i in range(PENDING_CAP):
        _edit(w, i, cred=thief)
    _refused("pending_cap", _edit, w, PENDING_CAP, cred=thief)
    assert _edit(w, PENDING_CAP + 1)["state"] == "WAITING"        # the phone's own transport still files


def test_a_paired_device_whose_key_is_not_enrolled_cannot_write(world):
    w = world
    loose = SoftPasskey()
    out = _edit(w, cred={"class": "device", "fingerprint": loose.fingerprint, "transport": "paired-b"})
    _refused("bad_signature", w.b.submit, out["pending_id"], loose.sign(out["record"].encode()))
    _refused("bad_signature", w.b.submit, out["pending_id"], "not a signature")
    assert _state(w, out["pending_id"]) == "WAITING" and _text(w.spores, 0) == "seed 0"


def test_a_c1_record_parses_only_as_a_signed_pending():
    # K8 moves the journal's contract: a device's C1 is journalled, signed; a reported C1 is still corrupt
    from levain.autonomic.journal import JournalCorruptError
    from levain.cockpit.consent import encode
    from levain.cockpit.firejournal import _parse_record, new_pending_id, record_sha256

    pid = new_pending_id()
    rec = {"operation_id": pid, "verb": "spore_update", "intent": "sign", "tier": "C1",
           "issued_at": "2026-10-10T20:00:00.000000Z", "expires_at": "2026-10-10T20:15:00.000000Z", "effects": []}
    _parse_record(pid, encode(rec), record_sha256(encode(rec)))
    rec["intent"] = "reported"
    with pytest.raises(JournalCorruptError):
        _parse_record(pid, encode(rec), record_sha256(encode(rec)))


def test_the_consent_queue_still_takes_no_c1():
    # the journal's C1 (a device's signed pending) never widens the consent queue's tiers, which read _TIER_NEEDS
    from levain.cockpit.firejournal import _TIER_NEEDS
    from levain.consent.queue import class_covers

    assert set(_TIER_NEEDS) == {"T2", "T3"} and not class_covers("display-phone", "C1")


def test_a_device_principal_names_a_well_formed_transport():
    assert Broker.principal_base_of({"class": "device", "fingerprint": "SHA256:x", "transport": "run"}) \
        == "device:SHA256:x@run"
    for bad in ({"class": "device", "fingerprint": "SHA256:x@run"},
                {"class": "device", "fingerprint": "SHA256:x", "transport": "a b"},
                {"class": "device", "fingerprint": "SHA256:x", "transport": 3}):
        assert Broker.principal_base_of(bad) is None
    assert Broker.claimed_fingerprint("device:SHA256:x@run") == "SHA256:x"
    assert Broker.claimed_fingerprint("token:browser:1") is None


# --- end to end over the phone port: the same dispatch and broker, a virtual passkey ------------------------------

@pytest.fixture
def install(home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from levain import install as install_mod
    from levain.cli import main

    user_home = home / "home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    for var in ("LEVAIN_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert main(["init", "--answers-template"]) in (0, None)
    answers = json.loads(capsys.readouterr().out)
    answers.update(OPERATOR_NAME="Test", ENTITY_NAME="test")
    (home / "answers.json").write_text(json.dumps(answers))
    inst = home / "inst"
    assert main(["init", "--path", str(inst), "--adapter", "claude-code", "--answers", str(home / "answers.json"),
                 "--consent-hook", "on", "--force"]) in (0, None)
    store = SporeStore(inst / ".levain" / "memory.spores.json")
    store.add(type="task", text="tray one", disposition="seed")
    root = inst / ".levain" / "cockpit"
    root.mkdir(parents=True, exist_ok=True)
    db = CockpitDB(root, install_class="reported")
    db.set_install_class("reported")
    keys = home / "keys"
    keys.mkdir(mode=0o700)
    laptop = RealKey(keys, "laptop")
    enrol_any(db, laptop, "laptop-presence")
    phone = SoftPasskey()
    enrol(db, phone, "display-phone", by=root_of(db), custody="passkey")    # type: ignore[arg-type]
    return SimpleNamespace(inst=inst, qdir=install_mod._consent_queue_dir(), root=root, phone=phone, laptop=laptop,
                           store=inst / ".levain" / "memory.spores.json")


_TOK = "k8a-run-token"














# --- K8a fix 1: B6's three fixes and the L3 round 1 findings (k-reviews/K8a_FIX1.md) -------------------------------

def test_a_device_cancels_only_a_pending_its_own_phone_issued(world):
    # B6-A (run by B6: a phone passkey cancelled a laptop token's T2 pending): a device cancels only its own pendings
    w = world
    laptop_t2 = _edit(w, 1, cred=BROWSER, tier="T2", text="laptop wants this")["pending_id"]
    laptop_t3 = _edit(w, 2, cred=BROWSER, tier="T3")["pending_id"]
    for pid in (laptop_t2, laptop_t3):              # the T3 one also files no unsignable pending in the cap
        e = _refused("refused", w.b.cancel, pid, credential=w.dev)
        assert "own" in str(e) and _state(w, pid) == "WAITING"
    # a cancel pending filed around the route (the broker's issue) is held by the passkey's own check
    c = w.b.issue(verb="pending_cancel", params={}, target={"kind": "pending", "pending_id": laptop_t2}, tier="T2",
                  credential=w.dev)
    e = _refused("bad_signature", w.b.submit, c["pending_id"], w.phone.sign(c["record"].encode()))
    assert "own" in str(e) and _state(w, laptop_t2) == "WAITING"
    # the same passkey's pending from another transport (an earlier run) is still its own
    old = _edit(w, 3, cred={**w.dev, "transport": "run-0123456789abcdef"})["pending_id"]
    c = w.b.cancel(old, credential=w.dev)
    assert w.b.submit(c["pending_id"], w.phone.sign(c["record"].encode()))["state"] == "COMMITTED"
    assert _state(w, old) == "CANCELLED"


def test_a_key_whose_material_is_a_passkey_never_signs_as_a_laptop_key(world):
    # B6-C: the custody that picks the namespace and the checks is derived from the key material and must equal the
    # stored one: a WebAuthn passkey (an rp-host application) declared custody fido does not sign in levain-cockpit
    # Since custody K1 the enrol door refuses that enrolment, so the key exists only in a log written before the door
    # (pre_door), read fresh by this levain: the broker refuses it as QUARANTINED, a reason that names the custody.
    import levain.authority as auth
    from tests.test_passkey_custody_derived import pre_door

    w = world
    mis = SoftPasskey()
    assert w.b.db is not None
    with pre_door():
        enrol(w.b.db, mis, "display-phone", by=w.laptop, custody="fido")    # type: ignore[arg-type]
    auth.PROCESS_CACHE.clear()
    t2 = _edit(w, 1, cred=BROWSER, tier="T2")
    e = _refused("bad_signature", w.b.submit, t2["pending_id"], mis.sign(t2["record"].encode(), COCKPIT_NAMESPACE))
    assert "quarantined" in str(e) and _state(w, t2["pending_id"]) == "WAITING" and _text(w.spores, 1) == "seed 1"


def test_a_device_at_its_pending_cap_can_still_cancel_its_own_pending_once(world):
    # L-2: a device at PENDING_CAP files one cancel per own pending (never an unlimited run of them for one target)
    w = world
    pids = [_edit(w, i)["pending_id"] for i in range(PENDING_CAP)]
    _refused("pending_cap", _edit, w, PENDING_CAP)
    c = w.b.cancel(pids[0], credential=w.dev)
    assert c["state"] == "WAITING", c
    # a second cancel of the same target, under a fresh key, counts against the cap like any pending
    _refused("pending_cap", w.b.cancel, pids[0], credential=w.dev, idempotency_key="again")
    # another own target still gets its one cancel
    assert w.b.cancel(pids[1], credential=w.dev)["state"] == "WAITING"
    assert w.b.submit(c["pending_id"], w.phone.sign(c["record"].encode()))["state"] == "COMMITTED"
    assert _state(w, pids[0]) == "CANCELLED"


def test_the_reserved_cancel_slot_frees_a_waiting_write_and_nothing_else(world):
    # the L-2 slot exists to free a held write: a cancel of a cancel (each "the first for its target") or of a pending
    # that is no longer WAITING frees nothing, so it meets the cap; without this a token holder claiming the phone's
    # key could chain cancels past the cap without end
    w = world
    done = _edit(w, PENDING_CAP)
    assert w.b.submit(done["pending_id"], w.phone.sign(done["record"].encode()))["state"] == "COMMITTED"
    pids = [_edit(w, i)["pending_id"] for i in range(PENDING_CAP)]
    _refused("pending_cap", _edit, w, PENDING_CAP)
    c = w.b.cancel(pids[0], credential=w.dev)
    assert c["state"] == "WAITING", c
    _refused("pending_cap", w.b.cancel, c["pending_id"], credential=w.dev)
    # an ended target is refused before any slot is weighed (the broker's own WAITING check)
    _refused("refused", w.b.cancel, done["pending_id"], credential=w.dev)


def test_a_device_c1_marked_reported_is_refused_on_the_broker(world):
    # L-3 (glm): a device write with intent reported. The broker's reported refusal already holds it (complement);
    # these two tests pin that, on the broker and over the phone port, with nothing stored
    w = world
    e = _refused("refused", w.b.issue, verb="spore_update", params={"text": "x"}, target=_target(w.spores, 0),
                 tier="C1", credential=w.dev, intent="reported")
    assert "device signs" in str(e)
    assert w.b.db is not None
    with w.b.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pendings").fetchone()[0] == 0






def test_the_broker_pins_a_localhost_passkey_to_the_phone_ports_origin(world):
    # L-5 (glm): the broker's own passkey check takes the phone port's origin, not only the port's conversion
    w = world
    out = _edit(w)
    rec = out["record"].encode()
    e = _refused("bad_signature", w.b.submit, out["pending_id"], w.phone.sign(rec, origin="http://localhost:9999"),
                 page_origin="http://localhost:1234")
    assert _state(w, out["pending_id"]) == "WAITING", e
    done = w.b.submit(out["pending_id"], w.phone.sign(rec, origin="http://localhost:1234"),
                      page_origin="http://localhost:1234")
    assert done["state"] == "COMMITTED"








# --- K8a L3 round 1 fixes (k8a/L3_r1_TRIAGE.md K-1..K-7) ------------------------------------------------------------

def test_a_cancel_from_another_transport_never_takes_the_reserved_slot(world):
    # K-1 (complement): own is the same FULL principal, transport included (B5 4). A token holder claiming the phone's
    # key on its own transport, at its own cap, files no cancel of the phone's pendings in the reserved slot
    w = world
    thief = {"class": "device", "fingerprint": w.phone.fingerprint, "transport": "run"}
    phones = [_edit(w, i)["pending_id"] for i in range(2)]
    for i in range(2, PENDING_CAP + 2):
        _edit(w, i, cred=thief)
    _refused("pending_cap", _edit, w, PENDING_CAP + 2, cred=thief)
    for pid in phones:
        _refused("pending_cap", w.b.cancel, pid, credential=thief)
    # the phone's own transport still takes its one reserved cancel per pending
    assert w.b.cancel(phones[0], credential=w.dev)["state"] == "WAITING"
