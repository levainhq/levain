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

    def without_door(key_class, custody, key_type, *, public_key, legacy=False):
        # the door's other rules, with key material that is never a passkey's (an empty line)
        return real(key_class, custody, key_type, legacy=legacy, public_key="")

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


def envelope_verdict(tmp: Path, custody: str, **sig) -> str | None:
    """``None`` when the coordinator takes ``pk``'s envelope signature (``pk`` display-phone at ``custody``)."""
    from levain.coordinator import envelope as env
    from tests.test_dispatch_contract import NOW

    pk = WebAuthnKey()
    snapshot = _log(tmp, pk, "display-phone", custody).source()
    body = {"store_id": "st-custody", "envelope_id": "ev-custody", "words_sha256": "sha256:" + "0" * 64}
    payload = env.sign_payload(body, signer_fp=pk.fp, signed_at=NOW)
    return env.refusal(payload, pk.sign(env.sign_message(payload), env.SIGN_NAMESPACE, **sig), snapshot, body)


def adopt_verdict(tmp: Path, custody: str, **sig) -> str | None:
    """``None`` when adoption takes ``pk``'s authorisation to land a run (``pk`` display-phone at ``custody``)."""
    from levain.dispatch import adopt
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
    artifact = "sha256:" + "a" * 64
    payload = {"format": adopt.AUTHORISE_FORMAT, "store_id": job.store_id, "job_id": job.id, "run": "0",
               "contract_digest": job.digest, "artifact_digest": artifact, "onto": "main", "signer_fp": pk.fp,
               "signed_at": utc_stamp(NOW + timedelta(minutes=1))}
    signature = pk.sign(adopt.authorise_message(payload), adopt.DISPATCH_AUTHORISE_NAMESPACE, **sig)
    return adopt.authorise_refusal(payload, signature, snapshot, job, run=0, artifact=artifact, onto="main", tier="T2")


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
        for site, verdict in (("consent", consent_verdict), ("sign", dispatch_verdict),
                              ("envelope", envelope_verdict), ("adopt", adopt_verdict)):
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


@pytest.mark.parametrize("site", ["envelope", "adopt"])
@pytest.mark.parametrize("custody", [auth.UNSTATED, "fido"])
@pytest.mark.parametrize("name", list(SIGS))
def test_an_envelope_or_an_authorisation_refuses_a_passkey_stored_at_another_custody(tmp_path, old_log, site, custody,
                                                                                    name):
    verdict = envelope_verdict if site == "envelope" else adopt_verdict
    got = verdict(tmp_path, custody, **SIGS[name])
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


# --- the CLI shows the door's refusal before anything is signed --------------------------------------------------------

def _cockpit(tmp: Path, monkeypatch):
    """A reported cockpit journal whose log's genesis enrols an ssh laptop-presence key, and a counter of every link
    signature that key makes (through the CLI's own signer)."""
    from levain.cockpit import install
    from levain.cockpit import keys as ckeys
    from levain.cockpit.firejournal import CockpitDB
    from tests.cockpit_authority_rig import RealKey, enrol as genesis_enrol
    from tests.test_cockpit_keys import NOW as KNOW

    monkeypatch.setattr(install, "marker_dir", lambda: tmp / "m")   # no real governed marker dir
    root = tmp / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    laptop = RealKey(tmp, "laptop")
    genesis_enrol(CockpitDB(root), laptop, "laptop-presence", now=KNOW)
    signed = []
    real = ckeys.ssh_signer

    def counting(*a, **kw):
        fp, sign = real(*a, **kw)

        def wrapped(message):
            signed.append(message)
            return sign(message)
        return fp, wrapped

    monkeypatch.setattr(ckeys, "ssh_signer", counting)
    return root, laptop, signed


@pytest.mark.parametrize("given", [None, "fido"])
def test_the_cli_refuses_to_enrol_a_passkey_at_another_custody_before_signing(tmp_path, monkeypatch, capsys, given):
    """Its pre-sign check (``declared_custody``) refuses now; before it did, ``enroll`` ran the replay's step (the
    door) on the unsigned link, so this held on the base as well and is pinned here, not proven red."""
    from levain import cli

    root, laptop, signed = _cockpit(tmp_path, monkeypatch)
    pub = tmp_path / "phone.pub"
    pub.write_text(WebAuthnKey().public_key + "\n")
    extra = [] if given is None else ["--custody", given]
    rc = cli.main(["cockpit", "enroll", str(pub), "--class", "display-phone", "--signing-key", str(laptop.path),
                   *extra, "--data-root", str(root)])
    err = capsys.readouterr().err
    assert rc == 1 and "WebAuthn passkey" in err and f"custody is {auth.PASSKEY}" in err, err
    assert signed == [], "the enrolment was signed before the refusal"
    assert cli.main(["cockpit", "enroll", str(pub), "--class", "display-phone", "--signing-key", str(laptop.path),
                     "--custody", auth.PASSKEY, "--data-root", str(root)]) == 0
    assert len(signed) == 1 and "custody passkey" in capsys.readouterr().out


def test_enroll_refuses_a_passkey_at_another_custody_before_it_signs(tmp_path, monkeypatch):
    """``enroll`` and the CLI's pre-sign check (``declared_custody``, also the ledger init signer's) refuse before
    signing. ``declared_custody`` took such a key at the base (red there)."""
    from levain.cockpit.custody import declared_custody
    from levain.cockpit.keys import KeyError_, enroll
    from tests.test_cockpit_keys import NOW as KNOW

    root, laptop, _signed = _cockpit(tmp_path, monkeypatch)
    pk = WebAuthnKey()
    calls = []

    def sign(message):
        calls.append(message)
        return laptop.sign_link(message)
    with pytest.raises(KeyError_, match="WebAuthn passkey"):
        enroll(root, pk.public_key, "display-phone", custody=auth.UNSTATED, signer_fp=laptop.fingerprint, sign=sign,
               clock=lambda: KNOW, marker_directory=tmp_path / "m")
    assert calls == []
    pub = tmp_path / "p.pub"
    pub.write_text(pk.public_key + "\n")
    with pytest.raises(KeyError_, match="WebAuthn passkey"):
        declared_custody(pub, None, "display-phone", pk.public_key)
    assert declared_custody(pub, auth.PASSKEY, "display-phone", pk.public_key) == auth.PASSKEY
    with pytest.raises(KeyError_, match="WebAuthn passkey"):
        declared_custody(pub, "fido", "laptop-presence", pk.public_key)   # the ledger init signer's check


# --- the 1022+18 L3 r1 sites: the cockpit broker's pending ALLOW, the anchor reset, the signers snapshot door ----------

def cockpit_verdict(home: Path, spores: Path, keydir: Path, custody: str, **sig) -> str | None:
    """``None`` when the cockpit broker takes ``pk``'s signature over a waiting pending (``pk`` display-phone at
    ``custody`` in the broker's own signed log, beside an ssh laptop-presence genesis key), else the refusal."""
    from levain.autonomic.confirm import COCKPIT_NAMESPACE
    from levain.cockpit.broker import BrokerError
    from tests.cockpit_authority_rig import RealKey, enrol, link
    from tests.test_cockpit_k2b_broker import _broker, _move

    b = _broker(home, spores)
    try:
        root = RealKey(keydir, "root")
        enrol(b.db, root, "laptop-presence")
        pk = WebAuthnKey()
        link(b.db, {"op": "enrol", "fingerprint": pk.fp, "public_key": pk.public_key, "class": "display-phone",
                    "custody": custody}, root)
        pid = _move(b, spores)["pending_id"]
        signature = pk.sign(b.outcome(pid)["record"].encode(), COCKPIT_NAMESPACE, **sig)
        try:
            out = b.submit(pid, signature)
        except BrokerError as exc:
            return f"{exc.code}: {exc}"
        return None if out["state"] == "COMMITTED" else f"state {out['state']}"
    finally:
        b.stop()


@pytest.fixture
def broker_home():
    from tests.test_cockpit_k2b_broker import account_home
    import tempfile

    d = Path(tempfile.mkdtemp(prefix=".levain-custody-broker-", dir=account_home()))
    try:
        (d / "keys").mkdir(mode=0o700)
        from anneal_memory.spores import SporeStore

        st = SporeStore(d / "spores.json")
        st.add(type="thought", text="a tray seed", disposition="seed", origin_key="k.seed")
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("custody", [auth.UNSTATED, "fido"])
@pytest.mark.parametrize("name", list(SIGS))
def test_the_cockpit_broker_never_takes_a_passkey_stored_at_another_custody(broker_home, old_log, custody, name):
    """A WebAuthn key never enters the pending ALLOW's signers file, whatever custody the log stores for it."""
    got = cockpit_verdict(broker_home, broker_home / "spores.json", broker_home / "keys", custody, **SIGS[name])
    assert got is not None and "custody its key material shows" in got, got


def anchor_reset_verdict(tmp: Path, custody: str, **sig) -> str | None:
    """``None`` when :func:`levain.authority.verify_anchor_reset` takes ``pk``'s reset signature (``pk``
    laptop-presence at ``custody``), the snapshot read through a replay cache warmed before the door (the log
    itself would not replay fresh now)."""
    pk = WebAuthnKey()
    with pre_door():
        lg = _log(tmp, pk, "laptop-presence", custody)
        lg.source()
    snap = lg.source()                      # the warmed cache: its verified prefix, no fresh replay
    assert snap.key(pk.fp) is not None
    message = auth.reset_message(snap.store_id, None, snap.seq, snap.head_hash)
    try:
        auth.verify_anchor_reset(snap, None, signer_fp=pk.fp, signature=pk.sign(message, auth.RESET_NAMESPACE, **sig))
    except auth.AnchorError as exc:
        return str(exc)
    return None


@pytest.mark.parametrize("name", list(SIGS))
def test_an_anchor_reset_refuses_a_passkey_stored_at_custody_fido(tmp_path, name):
    got = anchor_reset_verdict(tmp_path, "fido", **SIGS[name])
    assert got is not None and "custody its key material shows" in got, got


def test_the_enrol_door_has_no_form_without_the_public_key():
    """The door's key-material rule cannot be skipped by leaving ``public_key`` out: it is a required argument."""
    with pytest.raises(TypeError):
        auth.custody_problem("display-phone", "fido", "sk-ecdsa-sha2-nistp256@openssh.com")  # type: ignore[call-arg]


def test_a_signers_snapshot_refuses_webauthn_material_at_another_custody():
    from levain.qbroker.signers import SignersError, _signer

    pk = WebAuthnKey()
    key_type, key_b64 = pk.public_key.split()
    entry = {"principal": "phone", "key_type": key_type, "key_b64": key_b64, "key_class": "display-phone",
             "namespaces": [ANSWER_NAMESPACE]}
    for custody in (auth.UNSTATED, "fido"):
        with pytest.raises(SignersError, match="WebAuthn passkey"):
            _signer({**entry, "custody": custody})
    assert _signer({**entry, "custody": auth.PASSKEY}).custody == auth.PASSKEY
