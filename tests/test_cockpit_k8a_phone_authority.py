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


def _call(base: str, path: str, body: dict | None = None, token: str | None = _TOK, *,
          timeout: float = 60) -> tuple[int, dict]:
    from levain.phone_server import PHONE_TOKEN_HEADER

    headers = {"Content-Type": "application/json", "Origin": base} if body is not None else {}
    if token is not None:
        headers[PHONE_TOKEN_HEADER] = token
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET", headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


def _drive_levainsign() -> dict:
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent
    cp = subprocess.run([node, str(root / "_phone_levainsign_drive.js"),
                         str(root.parent / "levain" / "templates" / "web" / "phone.js")],
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def test_levainsign_signs_only_what_a_review_sheet_showed_and_only_a_waiting_pending():
    got = _drive_levainsign()
    # B6-B: no review callback, no signature: nothing filed, no passkey prompt, a refusal said
    nr = got["no_review"]
    assert (nr["issued"], nr["webauthn"]) == (0, 0), nr
    assert nr["said"] and nr["said"][-1][1] == "bad" and nr["done"] and nr["done"][-1]["ok"] is False, nr
    # L-1: a stored pending that is no longer WAITING gets no review sheet and no passkey prompt
    nw = got["not_waiting"]
    assert (nw["reviewed"], nw["webauthn"]) == (0, 0) and "EXPIRED" in nw["said"][-1][0], nw
    # L-1: the outcome names the signer the server persisted, never the fingerprint the page submitted
    s = got["signed"]
    assert s["webauthn"] == 1 and s["posted"]["fingerprint"] == "SHA256:a", s
    assert "SHA256:persisted" in s["said"][-1][0] and "SHA256:a" not in s["said"][-1][0], s


def test_a_key_whose_material_is_a_passkey_never_signs_as_a_laptop_key(world):
    # B6-C: the custody that picks the namespace and the checks is derived from the key material and must equal the
    # stored one: a WebAuthn passkey (an rp-host application) declared custody fido does not sign in levain-cockpit
    w = world
    mis = SoftPasskey()
    assert w.b.db is not None
    enrol(w.b.db, mis, "display-phone", by=w.laptop, custody="fido")    # type: ignore[arg-type]
    t2 = _edit(w, 1, cred=BROWSER, tier="T2")
    e = _refused("bad_signature", w.b.submit, t2["pending_id"], mis.sign(t2["record"].encode(), COCKPIT_NAMESPACE))
    assert "custody" in str(e) and _state(w, t2["pending_id"]) == "WAITING" and _text(w.spores, 1) == "seed 1"


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


def test_a_device_c1_marked_reported_is_refused_over_the_phone_port(install):
    i = install
    base, httpd = _serve(i.qdir)
    try:
        row = _tray_row(base)
        body = {"fingerprint": i.phone.fingerprint, "verb": "spore_update", "panel_id": "tray", "row_id": row["id"],
                "row_version": row["version"], "params": {"text": "reported"}}
        status, dry = _call(base, "/cockpit/verb", {**body, "dry_run": True})
        assert status == 200 and dry["gesture"] == "sign", dry
        status, out = _call(base, "/cockpit/verb", {**body, "intent": "reported", "tier_seen": dry["tier"],
                                                    "gesture_seen": dry["gesture"]})
        assert status in (400, 403) and out["error"] in ("refused", "bad_request"), out
        db = CockpitDB(i.root)
        with db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM pendings").fetchone()[0] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_slow_signature_check_does_not_hold_the_phones_reads(install, monkeypatch):
    # L-4 (glm): ssh-keygen runs twice, 10 s each at most; the board and pending reads answer meanwhile
    import levain.cockpit.broker as broker_mod

    w = install
    entered, release = threading.Event(), threading.Event()
    real = broker_mod.verify_cockpit_signature

    def slow(*a: Any, **kw: Any) -> Any:
        entered.set()
        release.wait(20)
        return real(*a, **kw)

    monkeypatch.setattr(broker_mod, "verify_cockpit_signature", slow)
    base, httpd = _serve(w.qdir)
    try:
        row = _tray_row(base)
        status, out = _fire(base, {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray",
                                   "row_id": row["id"], "row_version": row["version"], "params": {"text": "slow"}})
        assert status == 200, out
        pend = _call(base, f"/cockpit/pending/{out['pending_id']}")[1]
        ad, cdj, der = w.phone.assertion(_unb64u(pend["challenge"]), origin=base)
        done: dict = {}
        t = threading.Thread(target=lambda: done.update(r=_call(
            base, f"/cockpit/pending/{out['pending_id']}/signature",
            {"fingerprint": w.phone.fingerprint, "authenticator_data": _b64u(ad), "client_data_json": _b64u(cdj),
             "signature": _b64u(der)})))
        t.start()
        try:
            assert entered.wait(20)
            status, board = _call(base, "/app/board.json", timeout=3)
            assert status == 200, board
            status, again = _call(base, f"/cockpit/pending/{out['pending_id']}", timeout=3)
            assert status == 200 and again["state"] == "WAITING", again
        finally:
            release.set()
            t.join(30)
        assert done["r"][0] == 200 and done["r"][1]["state"] == "COMMITTED", done
    finally:
        httpd.shutdown()
        httpd.server_close()


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


def test_a_run_tokens_transport_names_its_run(install):
    # L-6 (codex): a restart's fresh run token is a fresh transport (its own cap and idempotency namespace)
    from levain.phone_app import transport_of

    a, b = transport_of("run", None, run_token="token-a"), transport_of("run", None, run_token="token-a")
    assert a == b and a != transport_of("run", None, run_token="token-b") and a.startswith("run-")
    assert "token" not in a and Broker.principal_base_of({"class": "device", "fingerprint": "SHA256:x",
                                                           "transport": a}) == f"device:SHA256:x@{a}"
    w = install
    base, httpd = _serve(w.qdir)
    try:
        row = _tray_row(base)
        status, out = _fire(base, {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray",
                                   "row_id": row["id"], "row_version": row["version"], "params": {"text": "run"}})
        assert status == 200, out
        assert CockpitDB(w.root).load(out["pending_id"]).principal \
            == f"device:{w.phone.fingerprint}@{transport_of('run', None, run_token=_TOK)}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_pending_read_answers_effective_state_and_the_persisted_signer(install, monkeypatch):
    # L-1 (codex MED + complement LOW): an expired or decided pending gets no challenge; the signature's answer
    # names the signer the journal persisted
    import time

    import levain.cockpit.broker as broker_mod
    from datetime import timedelta

    w = install
    base, httpd = _serve(w.qdir)
    try:
        row = _tray_row(base)
        verb = {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray", "row_id": row["id"],
                "row_version": row["version"], "params": {"text": "signed"}}
        status, out = _fire(base, verb)
        assert status == 200, out
        pend = _call(base, f"/cockpit/pending/{out['pending_id']}")[1]
        ad, cdj, der = w.phone.assertion(_unb64u(pend["challenge"]), origin=base)
        status, done = _call(base, f"/cockpit/pending/{out['pending_id']}/signature",
                             {"fingerprint": w.phone.fingerprint, "authenticator_data": _b64u(ad),
                              "client_data_json": _b64u(cdj), "signature": _b64u(der)})
        assert status == 200 and done["state"] == "COMMITTED" and done["signer_fp"] == w.phone.fingerprint, done
        status, after = _call(base, f"/cockpit/pending/{out['pending_id']}")
        assert status == 200 and after["state"] == "COMMITTED" and "challenge" not in after, after
        # an expired pending, still stored WAITING while no broker ran, reads EXPIRED with no challenge
        monkeypatch.setattr(broker_mod, "PENDING_TTL", timedelta(seconds=1))
        row = _tray_row(base)
        status, out = _fire(base, {**verb, "row_version": row["version"], "params": {"text": "late"}})
        assert status == 200, out
        time.sleep(1.2)
        status, late = _call(base, f"/cockpit/pending/{out['pending_id']}")
        assert status == 200 and late["state"] == "EXPIRED" and "challenge" not in late, late
        ad, cdj, der = w.phone.assertion(P.challenge(late["record_text"].encode(), COCKPIT_PHONE_NAMESPACE),
                                         origin=base)
        status, done = _call(base, f"/cockpit/pending/{out['pending_id']}/signature",
                             {"fingerprint": w.phone.fingerprint, "authenticator_data": _b64u(ad),
                              "client_data_json": _b64u(cdj), "signature": _b64u(der)})
        assert status == 200 and done["state"] == "EXPIRED" and done["signer_fp"] is None, done
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_pending_read_keeps_one_journal_reader(install, monkeypatch):
    # L-7 (complement): one journal reader for the port's pending reads, never a new opener (and schema check) per GET
    import levain.cockpit.firejournal as fj

    w = install
    made: list = []

    class Counting(fj.CockpitDB):
        def __init__(self, *a: Any, **kw: Any) -> None:
            made.append(a[0] if a else kw.get("data_root"))
            super().__init__(*a, **kw)

    base, httpd = _serve(w.qdir)
    try:
        row = _tray_row(base)
        status, out = _fire(base, {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray",
                                   "row_id": row["id"], "row_version": row["version"], "params": {"text": "x"}})
        assert status == 200, out
        monkeypatch.setattr(fj, "CockpitDB", Counting)
        for _ in range(3):
            assert _call(base, f"/cockpit/pending/{out['pending_id']}")[0] == 200
        assert len(made) <= 1, made
    finally:
        httpd.shutdown()
        httpd.server_close()


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


def test_a_retried_signature_after_a_revoke_answers_the_persisted_outcome(install, monkeypatch):
    # K-4 (codex MED): the signature committed but its answer was lost on the phone's network; before the retry the
    # passkey is revoked, or the authority cannot be read. The retry of the same assertion gets the persisted terminal
    # outcome and signer, applying nothing and verifying nothing new; a WAITING pending still needs the full check
    from levain.phone_app import PhoneApp, PhoneAppError
    from tests.cockpit_authority_rig import revoke_any

    w = install
    base, httpd = _serve(w.qdir)
    try:
        row = _tray_row(base)
        status, out = _fire(base, {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray",
                                   "row_id": row["id"], "row_version": row["version"], "params": {"text": "once"}})
        assert status == 200, out
        pid = out["pending_id"]
        pend = _call(base, f"/cockpit/pending/{pid}")[1]
        ad, cdj, der = w.phone.assertion(_unb64u(pend["challenge"]), origin=base)
        body = {"fingerprint": w.phone.fingerprint, "authenticator_data": _b64u(ad), "client_data_json": _b64u(cdj),
                "signature": _b64u(der)}
        status, first = _call(base, f"/cockpit/pending/{pid}/signature", body)
        assert status == 200 and first["state"] == "COMMITTED", first
        # a second WAITING pending, signed only after the revoke: refused, nothing applied
        row = _tray_row(base)
        status, late = _fire(base, {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray",
                                    "row_id": row["id"], "row_version": row["version"], "params": {"text": "late"}})
        assert status == 200, late
        lpend = _call(base, f"/cockpit/pending/{late['pending_id']}")[1]
        revoke_any(CockpitDB(w.root), w.phone)                              # type: ignore[arg-type]
        status, again = _call(base, f"/cockpit/pending/{pid}/signature", body)
        assert status == 200 and again["state"] == "COMMITTED" and again["replay"] is True, again
        assert again["signer_fp"] == w.phone.fingerprint, again
        ad, cdj, der = w.phone.assertion(_unb64u(lpend["challenge"]), origin=base)
        status, no = _call(base, f"/cockpit/pending/{late['pending_id']}/signature",
                           {**body, "authenticator_data": _b64u(ad), "client_data_json": _b64u(cdj),
                            "signature": _b64u(der)})
        assert status == 422 and no["error"] == "no_passkey", no
        assert _call(base, f"/cockpit/pending/{late['pending_id']}")[1]["state"] == "WAITING"

        # an authority that cannot be read now: the terminal retry still answers, the WAITING one is a 503
        def unreadable(self: Any, cockpit: Any) -> None:
            raise PhoneAppError(503, "authority_unreadable", "cannot be read now")

        monkeypatch.setattr(PhoneApp, "_governed_refusal", unreadable)
        status, again = _call(base, f"/cockpit/pending/{pid}/signature", body)
        assert status == 200 and again["state"] == "COMMITTED" and again["signer_fp"] == w.phone.fingerprint, again
        status, no = _call(base, f"/cockpit/pending/{late['pending_id']}/signature",
                           {**body, "authenticator_data": _b64u(ad), "client_data_json": _b64u(cdj),
                            "signature": _b64u(der)})
        assert status == 503 and no["error"] == "authority_unreadable", no
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_concurrent_signatures_never_take_the_read_slots(install, monkeypatch):
    # K-3 (codex MED, complement LOW 4): signatures for several pendings at once each held a read slot while waiting on
    # the write lock, so the board answered 503 for as long as ssh-keygen ran. A cockpit write now runs one at a time on
    # its own gate, never a read slot, and a queued writer waits a bounded time, then answers busy (503)
    import time

    import levain.cockpit.broker as broker_mod
    import levain.phone_server as ps

    monkeypatch.setattr(ps, "_WRITE_WAIT", 1.0)
    w = install
    entered, release = threading.Event(), threading.Event()
    real = broker_mod.verify_cockpit_signature

    def slow(*a: Any, **kw: Any) -> Any:
        entered.set()
        release.wait(20)
        return real(*a, **kw)

    base, httpd = _serve(w.qdir)
    try:
        bodies = []
        for i in range(ps._MAX_INFLIGHT + 1):
            row = _tray_row(base)
            status, out = _fire(base, {"fingerprint": w.phone.fingerprint, "verb": "spore_update", "panel_id": "tray",
                                       "row_id": row["id"], "row_version": row["version"], "params": {"text": f"c{i}"}})
            assert status == 200, out
            pend = _call(base, f"/cockpit/pending/{out['pending_id']}")[1]
            ad, cdj, der = w.phone.assertion(_unb64u(pend["challenge"]), origin=base)
            bodies.append((out["pending_id"], {"fingerprint": w.phone.fingerprint, "authenticator_data": _b64u(ad),
                                               "client_data_json": _b64u(cdj), "signature": _b64u(der)}))
        monkeypatch.setattr(broker_mod, "verify_cockpit_signature", slow)
        got: dict = {}
        threads = [threading.Thread(target=lambda p=p, b=b: got.update({p: _call(base, f"/cockpit/pending/{p}/signature",
                                                                                     b)})) for p, b in bodies]
        for t in threads:
            t.start()
        try:
            assert entered.wait(20)
            time.sleep(0.3)                    # the other signatures reach their wait
            for _ in range(3):
                status, board = _call(base, "/app/board.json", timeout=3)
                assert status == 200, board
            # each queued writer is answered busy within its bound while the first still verifies
            deadline = time.monotonic() + 10
            while len(got) < len(bodies) - 1 and time.monotonic() < deadline:
                time.sleep(0.1)
            busy = [r for r in got.values() if r[0] == 503]
            assert len(busy) == len(bodies) - 1 and all(r[1]["error"] == "busy" for r in busy), got
        finally:
            release.set()
            for t in threads:
                t.join(30)
        assert sum(1 for r in got.values() if r[0] == 200 and r[1]["state"] == "COMMITTED") == 1, got
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_levainsign_signs_once_tells_unknown_after_a_send_and_holds_an_open_review():
    got = _drive_levainsign()
    assert got["unhandled"] == [], got["unhandled"]
    # K-5: a double tap on Sign is one passkey ceremony and one outcome
    dt = got["double_tap"]
    assert dt["webauthn"] == 1 and len(dt["done"]) == 1 and dt["said"][-1][1] == "ok", dt
    # K-6: a fault after the signature was sent says the result is unknown, never "not applied"
    s = got["after_send"]
    assert s["webauthn"] == 1 and "unknown" in s["said"][-1][0] and "not applied" not in s["said"][-1][0], s
    # K-7: an event-driven review holds the page until its Sign tap settles: a second write started meanwhile files
    # nothing, the tap signs, and the next write files
    ar = got["async_review"]
    assert ar["issued_while_open"] == 1 and ar["webauthn_after_tap"] == 1 and ar["issued"] == 2, ar
    # K-7: a review dismissed without signing releases the page, with no passkey prompt
    d = got["dismissed"]
    assert d["issued_while_open"] == 1 and d["issued"] == 2 and d["webauthn"] == 0, d


def test_levainsign_runs_one_sign_lifecycle_per_review():
    """L3 r2 (codex MED, complement item 1): sign / dismiss / busy are one state machine per review."""
    got = _drive_levainsign()
    assert got["unhandled"] == [], got["unhandled"]
    # a Sign tap after dismiss runs no passkey ceremony and posts nothing; the page is free
    s = got["stale_sign"]
    assert s["webauthn"] == 0 and s["posts"] == 0 and s["free_after"] is True, s
    # a dismiss during an in-flight sign: the page stays held until that sign settles, then is released exactly once
    d = got["dismiss_in_flight"]
    assert d["free_in_flight"] is False and d["webauthn"] == 1, d
    assert d["free_after"] is True and d["free_again"] is True and len(d["done"]) == 1, d
    # a 4xx re-arms Sign without releasing the page; the retry signs and its terminal outcome releases it once
    r = got["retry_held"]
    assert r["free_after_4xx"] is False, r
    assert r["webauthn"] == 2 and r["posts"] == 2, r
    assert [a["status"] for a in r["done"]] == [409, 200] and r["done"][1]["body"]["state"] == "COMMITTED", r
    assert r["free_after_retry"] is True and r["free_again"] is True, r


def test_levainsign_review_is_dismissed_when_the_page_hides_or_the_view_changes():
    """L3 r3 (consensus MED): an open review's hold never outlives a hidden page or a view change."""
    got = _drive_levainsign()
    assert got["unhandled"] == [], got["unhandled"]
    # (a) a hide dismisses the open review and frees the page; (c) its listener is gone after the release, so a
    # later hide releases nothing twice; the sheet's stale Sign then signs nothing
    h = got["hidden_review"]
    assert h["listeners_open"] == 1, h
    assert h["free_after_hide"] is True and h["listeners_after"] == 0, h
    assert h["free_again"] is True and h["webauthn"] == 0 and h["posts"] == 0, h
    # (b) a hide during an in-flight sign defers the release until that sign settles, then releases once
    f = got["hidden_in_flight"]
    assert f["free_in_flight"] is False and f["listeners_in_flight"] == 1, f
    assert f["free_after"] is True and f["free_again"] is True and f["listeners_after"] == 0, f
    assert f["webauthn"] == 1 and f["posts"] == 1 and len(f["done"]) == 1, f
    # (d) the view-change hook point dismisses every open review
    v = got["view_change"]
    assert v["has_hook"] is True and v["free_before"] is False, v
    assert v["dismissed_count"] == 1 and v["free_after"] is True and v["listeners_after"] == 0, v
    assert v["dismissed_again"] == 0, v


def test_levainsign_tells_the_sheet_and_the_caller_when_a_review_is_dismissed():
    """L3 r4 (complement MED + LOW): a dismiss the sheet did not make (the page hid, the view changed) is never silent.
    The sheet's closed() is told, so its Sign is never shown live over a dead review; done() gets a dismissed answer,
    so an awaiting caller never hangs; and a refused passkey keeps this browser's record of it (1022+19 Q2)."""
    got = _drive_levainsign()
    assert got["unhandled"] == [], got["unhandled"]
    h = got["hidden_review"]
    assert h["closed"] == ["dismissed"], h
    assert "dismissed" in h["said_at_hide"][0] and h["said_at_hide"][1] == "bad", h
    # the stale Sign tap after the hide still signs nothing, and says why
    assert h["webauthn"] == 0 and h["posts"] == 0 and "dismissed" in h["said"][-1][0], h
    assert [a["body"].get("state") for a in h["done"]] == ["DISMISSED"] and h["done"][0]["ok"] is False, h
    v = got["view_change"]
    assert [a["body"].get("state") for a in v["done"]] == ["DISMISSED"], v
    # a hide during an in-flight sign whose answer is a 4xx: the sheet is told only once it settles, the 409 is still
    # said with the dismissal, done gets the 409 and then the dismissal, and a later tap signs nothing
    f = got["hidden_in_flight_4xx"]
    assert f["closed_in_flight"] == 0 and f["closed"] == ["dismissed"] and f["free_after"] is True, f
    assert [a["status"] for a in f["done"]] == [409, 0] and f["done"][1]["body"]["state"] == "DISMISSED", f
    assert any("stale" in t and "dismissed" in t for t, _k in f["said"]), f
    assert f["webauthn"] == 1 and f["posts"] == 1, f
    # a review that ends with the server's terminal answer tells the sheet "answered" and never "dismissed"
    d = got["dismiss_in_flight"]
    assert len(d["done"]) == 1 and d["done"][0]["body"]["state"] == "COMMITTED", d
    r = got["refused_keeps"]
    assert r["webauthn"] == 1 and r["mapping"] == {"SHA256:a": "cmF3aWQ"}, r
    assert "did not sign (NotAllowedError)" in r["said"][-1][0], r
