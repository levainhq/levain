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
import tempfile
import threading
import urllib.error
import urllib.request
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
    d = Path(tempfile.mkdtemp(prefix=".levain-k8a-", dir=Path.home()))
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
                            "spore_ascend", "spore_seed"}
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
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from levain import install as install_mod
    from levain.cli import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("LEVAIN_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert main(["init", "--answers-template"]) in (0, None)
    answers = json.loads(capsys.readouterr().out)
    answers.update(OPERATOR_NAME="Test", ENTITY_NAME="test")
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    inst = tmp_path / "inst"
    assert main(["init", "--path", str(inst), "--adapter", "claude-code", "--answers", str(tmp_path / "answers.json"),
                 "--consent-hook", "on", "--force"]) in (0, None)
    store = SporeStore(inst / ".levain" / "memory.spores.json")
    store.add(type="task", text="tray one", disposition="seed")
    root = inst / ".levain" / "cockpit"
    root.mkdir(parents=True, exist_ok=True)
    db = CockpitDB(root, install_class="reported")
    db.set_install_class("reported")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    laptop = RealKey(keys, "laptop")
    enrol_any(db, laptop, "laptop-presence")
    phone = SoftPasskey()
    enrol(db, phone, "display-phone", by=root_of(db), custody="passkey")    # type: ignore[arg-type]
    return SimpleNamespace(inst=inst, qdir=install_mod._consent_queue_dir(), root=root, phone=phone, laptop=laptop,
                           store=inst / ".levain" / "memory.spores.json")


_TOK = "k8a-run-token"


def _serve(qdir: Path) -> tuple[str, Any]:
    from levain.phone_server import make_phone_server

    httpd = make_phone_server(qdir, "localhost", port=0, token=_TOK)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://localhost:{httpd.server_address[1]}", httpd


def _call(base: str, path: str, body: dict | None = None, token: str | None = _TOK) -> tuple[int, dict]:
    from levain.phone_server import PHONE_TOKEN_HEADER

    headers = {"Content-Type": "application/json", "Origin": base} if body is not None else {}
    if token is not None:
        headers[PHONE_TOKEN_HEADER] = token
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET", headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _fire(base: str, body: dict) -> tuple[int, dict]:
    """The laptop page's envelope: a dry run previews the tier and gesture, the fire names them with an intent."""
    status, dry = _call(base, "/cockpit/verb", {**body, "dry_run": True})
    if status != 200:
        return status, dry
    assert dry["gesture"] == "sign", dry            # a device's write is always signed
    return _call(base, "/cockpit/verb", {**body, "intent": "sign", "tier_seen": dry["tier"],
                                         "gesture_seen": dry["gesture"]})


def _tray_row(base: str) -> dict:
    status, board = _call(base, "/app/board.json")
    assert status == 200, board
    return next(p for p in board["panels"] if p["id"] == "tray")["rows"][0]


def test_the_phone_edits_a_tray_item_end_to_end_signed_by_a_virtual_passkey(install):
    w = install
    base, httpd = _serve(w.qdir)
    try:
        row = _tray_row(base)
        verb = {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray", "row_id": row["id"],
                "row_version": row["version"], "params": {"text": "from the phone"}}
        assert _call(base, "/cockpit/verb", verb, token=None)[0] == 403          # the transport gate holds
        status, out = _fire(base, verb)
        assert status == 200 and out["state"] == "WAITING", out
        assert SporeStore(w.store).get(row["id"].split(":", 1)[1])["text"] == "tray one"
        status, pend = _call(base, f"/cockpit/pending/{out['pending_id']}")
        assert status == 200 and pend["record_text"] == out["record"] and pend["rp_id"] == "localhost"
        # the challenge is the server's, from the stored record, in the phone namespace
        assert _unb64u(pend["challenge"]) == P.challenge(pend["record_text"].encode(), COCKPIT_PHONE_NAMESPACE)
        ad, cdj, der = w.phone.assertion(_unb64u(pend["challenge"]), origin=base)
        assertion = {"fingerprint": w.phone.fingerprint, "authenticator_data": _b64u(ad),
                     "client_data_json": _b64u(cdj), "signature": _b64u(der)}
        status, done = _call(base, f"/cockpit/pending/{out['pending_id']}/signature", assertion)
        assert status == 200 and done["state"] == "COMMITTED", done
        assert SporeStore(w.store).get(row["id"].split(":", 1)[1])["text"] == "from the phone"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_phone_port_refuses_what_the_kernel_keeps_on_the_laptop_and_answers_standby(install):
    w = install
    base, httpd = _serve(w.qdir)
    try:
        row = _tray_row(base)
        fp = w.phone.fingerprint
        status, out = _call(base, "/cockpit/verb", {"fingerprint": fp, "verb": "spore_touch", "panel_id": "tray",
                                                   "row_id": row["id"], "row_version": row["version"]})
        assert (status, out["error"]) == (403, "laptop_only"), out
        status, out = _call(base, "/cockpit/verb", {"fingerprint": fp, "verb": "operator_state", "panel_id": "state",
                                                   "params": {"text": "x"}})
        assert status in (403, 404) and out["error"] in ("laptop_only", "not_offered"), out
        # a no-UV assertion is refused at conversion, before the broker
        status, out = _fire(base, {"fingerprint": fp, "verb": "spore_update", "panel_id": "tray",
                                                   "row_id": row["id"], "row_version": row["version"],
                                                   "params": {"text": "no uv"}})
        assert status == 200 and out["state"] == "WAITING", out
        pend = _call(base, f"/cockpit/pending/{out['pending_id']}")[1]
        ad, cdj, der = w.phone.assertion(_unb64u(pend["challenge"]), origin=base, uv=False)
        status, bad = _call(base, f"/cockpit/pending/{out['pending_id']}/signature",
                            {"fingerprint": fp, "authenticator_data": _b64u(ad), "client_data_json": _b64u(cdj),
                             "signature": _b64u(der)})
        assert status == 422 and "verify the user" in bad["message"], bad
        # while another broker (the laptop's cockpit) holds the write lock, the phone's write is standby
        holder = Broker(w.root, stores={"spores": SporeEffectStore(w.store)}, store_paths=[w.store],
                        install_class="reported")
        holder.start()
        try:
            status, out = _fire(base, {"fingerprint": fp, "verb": "spore_update", "panel_id": "tray",
                                                       "row_id": row["id"], "row_version": row["version"],
                                                       "params": {"text": "held"}})
            assert (status, out["error"]) == (503, "standby"), out
        finally:
            holder.stop()
    finally:
        httpd.shutdown()
        httpd.server_close()
