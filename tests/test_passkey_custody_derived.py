"""A passkey's checks are chosen by the custody its key material shows, held equal to the stored one (head ruling K1,
the broker's B6-C shape carried to the consent queue and dispatch).

``ssh-keygen -Y verify`` checks a WebAuthn signature's ECDSA, challenge and rpId, but not user verification and not
what the origin is: those are levain's (:func:`levain.consent.passkey.answer_signature_problem`). Before this file
each site chose to run them by the key's STORED custody, and the authority log admits WebAuthn key material at
custody ``unstated`` (display-phone) or ``fido`` (any class), so such a key's signature skipped them; the authority
replay itself took links such a key signed. Now the enrol door (:func:`levain.authority.custody_problem`) refuses that
enrolment, and every verify site holds the derived custody equal to the stored one
(:func:`levain.authority.custody_mismatch`). A verify site is reached here through a replay without the door
(:func:`pre_door`, the replay code before it), since with the door no log holds such a key. Keys here are a software
P-256 key answering as an authenticator does, its flags and origin chosen per case, and the sites read a real signed
authority log (the consent queue tests' ``Key`` and ``Log``)."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shutil
import struct
from datetime import timedelta
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

import levain.authority as auth  # noqa: E402
from levain.cockpit.consent import utc_stamp  # noqa: E402
from levain.cockpit.keys import fingerprint_of  # noqa: E402
from levain.consent import ANSWER_NAMESPACE, AnswerRefused, ConsentQueue, answer_message, answer_payload  # noqa: E402
from levain.consent.passkey import SIG_TYPE, _ecdsa_signature, _key_parts, challenge, public_key_line  # noqa: E402
from levain.consent.receipt_log import head as receipts_head  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

RP = "phone.example.ts.net"
ORIGIN = f"https://{RP}"
OTHER_ORIGIN = "https://evil.example"
_UP, _UV = 0x01, 0x04


def _string(data: bytes | str) -> bytes:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return struct.pack(">I", len(raw)) + raw


class WebAuthnKey:
    """A P-256 key that answers a WebAuthn ``get`` with the flags and origin a case chooses, wrapped as an armored
    SSHSIG byte for byte as :func:`levain.consent.passkey.to_sshsig` wraps one (built here so a signature that
    function refuses can still be made: no UV, another origin)."""

    def __init__(self, rp_id: str = RP) -> None:
        self.rp_id = rp_id
        self.priv = ec.generate_private_key(ec.SECP256R1())
        spki = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        self.public_key = public_key_line(spki, rp_id)
        self.fp = fingerprint_of(self.public_key)
        self.counter = 0

    def sign(self, message: bytes, namespace: str, *, uv: bool = True, origin: str = ORIGIN) -> str:
        self.counter += 1
        chal = base64.urlsafe_b64encode(challenge(message, namespace)).decode("ascii").rstrip("=")
        cdj = json.dumps({"type": "webauthn.get", "challenge": chal, "origin": origin, "crossOrigin": False},
                         separators=(",", ":")).encode()
        flags = _UP | (_UV if uv else 0)
        counter = struct.pack(">I", self.counter)
        auth_data = hashlib.sha256(self.rp_id.encode("ascii")).digest() + bytes([flags]) + counter
        der = self.priv.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        inner = (_string(SIG_TYPE) + _string(_ecdsa_signature(der)) + bytes([flags]) + counter + _string(origin)
                 + _string(cdj) + _string(b""))
        blob = (b"SSHSIG" + struct.pack(">I", 1) + _string(_key_parts(self.public_key)[0]) + _string(namespace)
                + _string(b"") + _string("sha512") + _string(inner))
        text = base64.b64encode(blob).decode("ascii")
        body = "\n".join(text[i:i + 70] for i in range(0, len(text), 70))
        return f"-----BEGIN SSH SIGNATURE-----\n{body}\n-----END SSH SIGNATURE-----\n"


# the three signatures every site is shown: a good one, one without user verification, one from another origin
SIGS = {"good": {}, "no-uv": {"uv": False}, "other-origin": {"origin": OTHER_ORIGIN}}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@contextlib.contextmanager
def pre_door():
    """The replay as it was before the enrol door: ``custody_problem`` without its key-material rule, so a log may
    hold WebAuthn material at another custody (a log written by an older levain). The verify sites' own check is
    untouched."""
    from unittest import mock

    real = auth.custody_problem

    def without_door(key_class, custody, key_type, *, legacy=False, public_key=None):
        return real(key_class, custody, key_type, legacy=legacy)

    with mock.patch.object(auth, "custody_problem", without_door):
        yield


def _log(tmp: Path, pk: WebAuthnKey, cls: str, custody: str):
    """A real signed log: an ssh laptop key (genesis, laptop-presence) and ``pk`` enrolled ``cls`` at ``custody``."""
    from tests.test_consent_queue import Key, Log

    (tmp / "keys").mkdir(parents=True)
    (tmp / "auth").mkdir()
    lg = Log(tmp / "auth", {"laptop": Key(tmp / "keys", "laptop")})
    lg.enrol("laptop", "laptop-presence")
    lg.link({"op": "enrol", "fingerprint": pk.fp, "public_key": pk.public_key, "class": cls, "custody": custody})
    return lg


def consent_verdict(tmp: Path, custody: str, **sig) -> str | None:
    """``None`` when the consent queue takes ``pk``'s allow (``pk`` display-phone at ``custody``), else the refusal."""
    from tests.test_consent_queue import NOW, FakeAgent

    pk = WebAuthnKey()
    lg = _log(tmp, pk, "display-phone", custody)
    clock = Clock(NOW)
    q = ConsentQueue(tmp / "home" / "consent", authority=lg.source, clock=clock)
    request = FakeAgent(q).ask()
    payload = answer_payload(queue_id=q.queue_id, request=request, decision="allow", signer_fp=pk.fp,
                             signed_at=utc_stamp(clock()), receipts_head=receipts_head(q))
    try:
        q.answer(request.id, payload, pk.sign(answer_message(payload), ANSWER_NAMESPACE, **sig))
    except AnswerRefused as exc:
        return str(exc)
    return None


def dispatch_verdict(tmp: Path, custody: str, which: str = "sign", **sig) -> str | None:
    """``None`` when dispatch takes ``pk``'s contract signature (``which`` ``"sign"``) or cancel (``"cancel"``)."""
    from levain.dispatch import sign as dsign
    from levain.dispatch.jobs import JobStore
    from tests.test_dispatch_contract import NOW, POLICY, make_repo, registry, submission

    pk = WebAuthnKey()
    snapshot = _log(tmp, pk, "display-phone", custody).source()
    repo, base = make_repo(tmp / "repo")
    d = tmp / "dispatch"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    job, _cap = JobStore(d, clock=Clock(NOW)).file(submission(repo, base), resolve_template=registry(), policy=POLICY,
                                                   filer_session="sess-1")
    at = NOW + timedelta(minutes=1)
    if which == "sign":
        payload = dsign.sign_payload(job, signer_fp=pk.fp, signed_at=at)
        return dsign.refusal(payload, pk.sign(dsign.sign_message(payload), dsign.DISPATCH_SIGN_NAMESPACE, **sig),
                             snapshot, job)
    payload = dsign.cancel_payload(job, signer_fp=pk.fp, signed_at=at, reason="wrong")
    return dsign.cancel_refusal(payload, pk.sign(dsign.sign_message(payload), dsign.DISPATCH_CANCEL_NAMESPACE, **sig),
                                snapshot, job)


def control_verdict(tmp: Path, custody: str, **sig) -> str | None:
    """``None`` when a dispatch configuration store pinned to ``pk`` (laptop-presence at ``custody``) takes ``pk``'s
    configuration item, else the refusal (at init or at accept)."""
    from levain.dispatch import control as ctl
    from tests.test_dispatch_contract import NOW
    from tests.test_dispatch_control import VERIFIERS

    pk = WebAuthnKey()
    lg = _log(tmp, pk, "laptop-presence", custody)
    d = tmp / "control"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    try:
        ctl.ControlStore.init(d, pk.fp, authority=lg.source)
        store = ctl.ControlStore(d, authority=lg.source, clock=Clock(NOW))
        item = ctl.item_bytes("verifier", 1, VERIFIERS,
                              NOW - timedelta(minutes=1), prev="", store_id=store.store_id())
        store.accept(item, pk.sign(item, ctl.CONFIG_NAMESPACE, **sig))
    except ctl.ConfigRefused as exc:
        return str(exc)
    return None


class _LinkSigner:
    def __init__(self, pk: WebAuthnKey, **sig) -> None:
        self.pk, self.sig, self.fp = pk, sig, pk.fp

    def sign(self, message: bytes, namespace: str) -> str:
        return self.pk.sign(message, namespace, **self.sig)


def authority_link_verdict(tmp: Path, **sig) -> str | None:
    """``None`` when the authority replay takes an ``enrol`` link signed by ``pk`` (enrolled laptop-presence at custody
    fido, which needs :func:`pre_door`), else the refusal."""
    pk = WebAuthnKey()
    lg = _log(tmp, pk, "laptop-presence", "fido")
    other = WebAuthnKey("other.example.ts.net")
    lg.keys["pk"] = _LinkSigner(pk, **sig)
    try:
        lg.link({"op": "enrol", "fingerprint": other.fp, "public_key": other.public_key, "class": "laptop-presence",
                 "custody": "fido"}, signer="pk")
    except auth.AuthorityError as exc:
        return str(exc)
    return None if lg.source().key(other.fp) is not None else "not in the replay"


@pytest.fixture
def old_log():
    with pre_door():
        yield


# --- the enrol door ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cls, custody", [("display-phone", auth.UNSTATED), ("display-phone", "fido"),
                                          ("laptop-presence", "fido"), ("laptop-presence", "touch-id"),
                                          ("presence", "windows-hello")])
def test_the_log_refuses_webauthn_material_enrolled_at_any_custody_but_passkey(tmp_path, cls, custody):
    with pytest.raises(auth.AuthorityError, match="WebAuthn passkey"):
        _log(tmp_path, WebAuthnKey(), cls, custody)


def test_a_log_holding_such_an_enrolment_from_before_the_door_fails_closed(tmp_path):
    """A fresh replay (a new process) refuses the log at that enrolment. A replay cache warmed before the door keeps
    its verified prefix, which is why every verify site holds the same rule (:func:`levain.authority.custody_mismatch`)."""
    from levain.consent import authority_source

    with pre_door():
        lg = _log(tmp_path, WebAuthnKey(), "display-phone", "fido")
        lg.source()
    with pytest.raises(auth.AuthorityError, match="WebAuthn passkey"):
        authority_source(lg.path, anchor=None, cache=auth.ReplayCache())()


# --- the stored custody agrees with the key material: the passkey's checks run, a good signature is taken ------------

def test_a_passkey_at_custody_passkey_is_taken_with_uv_from_its_own_origin_only(tmp_path):
    for name, sig in SIGS.items():
        for site, verdict in (("consent", consent_verdict), ("sign", dispatch_verdict)):
            got = verdict(tmp_path / f"{site}-{name}", auth.PASSKEY, **sig)
            assert (got is None) == (name == "good"), (site, name, got)
    assert "verify the user" in consent_verdict(tmp_path / "c-uv", auth.PASSKEY, uv=False)
    assert f"not {ORIGIN}" in dispatch_verdict(tmp_path / "d-o", auth.PASSKEY, origin=OTHER_ORIGIN)


# --- WebAuthn key material stored at another custody: refused whatever its signature (K1) ----------------------------

@pytest.mark.parametrize("custody", [auth.UNSTATED, "fido"])
@pytest.mark.parametrize("name", list(SIGS))
def test_the_consent_queue_refuses_a_passkey_stored_at_another_custody(tmp_path, old_log, custody, name):
    got = consent_verdict(tmp_path, custody, **SIGS[name])
    assert got is not None and "custody" in got, got


@pytest.mark.parametrize("which", ["sign", "cancel"])
@pytest.mark.parametrize("custody", [auth.UNSTATED, "fido"])
@pytest.mark.parametrize("name", list(SIGS))
def test_dispatch_refuses_a_passkey_stored_at_another_custody(tmp_path, old_log, which, custody, name):
    got = dispatch_verdict(tmp_path, custody, which, **SIGS[name])
    assert got is not None and "custody" in got, got


@pytest.mark.parametrize("name", list(SIGS))
def test_a_passkey_stored_at_custody_fido_is_never_a_configuration_root(tmp_path, old_log, name):
    got = control_verdict(tmp_path, "fido", **SIGS[name])
    assert got is not None and "custody" in got, got


@pytest.mark.parametrize("name", list(SIGS))
def test_the_replay_refuses_a_link_signed_by_a_passkey_stored_at_custody_fido(tmp_path, old_log, name):
    got = authority_link_verdict(tmp_path, **SIGS[name])
    assert got is not None and "custody its key material shows" in got, got


def test_an_openssh_fido_key_at_custody_fido_still_signs(tmp_path):
    """The derived custody of an OpenSSH FIDO key (application ``ssh:``) is not passkey: custody fido agrees with it.
    Pinned with the helper alone, since no hardware key is here to sign."""
    from levain.authority import custody_mismatch

    sk = public_key_line(WebAuthnKey().priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo), RP)
    blob = base64.b64decode(sk.split()[1])
    ssh_app = blob[:blob.rfind(_string(RP))] + _string("ssh:")
    fido = f"{sk.split()[0]} {base64.b64encode(ssh_app).decode()}"
    assert custody_mismatch(fido, "fido") is None
    assert auth.custody_problem("laptop-presence", "fido", fido.split()[0], public_key=fido) is None
    assert "WebAuthn passkey" in auth.custody_problem("laptop-presence", "fido", sk.split()[0], public_key=sk)
    assert custody_mismatch(fido, auth.PASSKEY) is not None
    assert custody_mismatch(sk, auth.PASSKEY) is None
    for custody in (auth.UNSTATED, "fido", "touch-id", None):
        assert custody_mismatch(sk, custody) is not None, custody
    assert custody_mismatch("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==", "passphrase") is None
