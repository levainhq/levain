"""Board row 113 (option F2): ``levain dispatch config request`` / ``accept-approved``. A dispatch configuration item is
approved by a phone passkey (levain's own sign request in ``levain-dispatch-config-approve``) and then signed by the
pinned LAPTOP trust-root key; the trust root and ``ControlStore.accept`` are unchanged.

Real everything that matters: real ssh-keygen keys enrolled in a real signed authority log, a real control store, the
real CLI, the real Signer verifying a software P-256 passkey's WebAuthn assertion into an SSHSIG. No phone, no push, no
Touch ID (the laptop key is a file key), no live queue, store or key."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.keys import fingerprint_of
from levain.consent.cockpit_origin import write_origin
from levain.consent.sign import Signer, read_request, sign_dir
from levain.contracts.namespaces import CONFIG_APPROVE_NAMESPACE as ANS, LEVAIN_SIGN_REQUESTS
from levain.dispatch import cli as dcli
from levain.dispatch import control as ctl
from levain.dispatch import phone_config as pc
from tests.test_cockpit_sign_origin import RP, _Passkey
from tests.test_dispatch_config import (  # noqa: F401  (fixtures and helpers)
    NoTouch, body_file, configure, ddir, init, no_touch, root, run, store,
)
from tests.test_dispatch_control import VERIFIERS, tmpl
from tests.test_dispatch_contract import POLICY
from tests.test_dispatch_sign import keys, log  # noqa: F401

ORIGIN = f"https://{RP}"


@pytest.fixture(scope="module")
def phones(log):
    """A display-phone passkey (the only class a passkey can hold; covers T2), one enrolled then revoked, and one
    never enrolled."""
    good, revoked, stranger = _Passkey(), _Passkey(), _Passkey()
    for pk in (good, revoked):
        log.link({"op": "enrol", "fingerprint": pk.key.fingerprint, "public_key": pk.line, "class": "display-phone",
                  "custody": "passkey"})
    log.link({"op": "revoke_key", "fingerprint": revoked.key.fingerprint})
    return {"good": good, "revoked": revoked, "stranger": stranger}


@pytest.fixture
def signq(tmp_path):
    d = tmp_path / "signq"
    d.mkdir(mode=0o700)
    assert write_origin(d, ORIGIN) is not None
    return d


@pytest.fixture
def pushes(monkeypatch):
    from levain import webpush
    from levain.webpush import sender

    sent: list[dict] = []
    monkeypatch.setattr(webpush, "live", lambda q: True)
    monkeypatch.setattr(sender, "spawn", lambda q, **kw: sent.append({"queue": Path(q), **kw}) or True)
    return sent


def request(capsys, ddir, root, signq, tmp_path, kind="verifier", body=VERIFIERS, name="b"):
    code, out, err = run(capsys, "config", "request", kind, body_file(tmp_path, name, body), "--dispatch-dir",
                         str(ddir), "--data-root", root, "--sign-queue-dir", str(signq))
    return code, out, err


def rid_of(signq) -> str:
    [path] = [p for p in sorted(sign_dir(signq).glob("lv-*.json")) if "." not in p.stem]
    return path.stem


def phone_sign(signq, rid, pk):
    s = Signer(signq, (), RP, lambda: {pk.key.fingerprint: pk.key})
    ch = s.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": read_request(signq, rid).sha256})
    return s.answer(rid, pk.assertion(ch["challenge"], ORIGIN))


def phone_deny(signq, rid, pk):
    s = Signer(signq, (), RP, lambda: {pk.key.fingerprint: pk.key})
    s.deny(rid, {"request_sha256": read_request(signq, rid).sha256})


def approve(capsys, ddir, root, keys, signq, rid, *extra, key="laptop"):
    return run(capsys, "config", "accept-approved", rid, "--dispatch-dir", str(ddir), "--data-root", root,
               "--sign-queue-dir", str(signq), "--signing-key", str(keys[key].path), *extra)


def gen(ddir, root) -> int:
    return store(ddir, root).generation()


# --- the request ------------------------------------------------------------------------------------------------------

def test_request_files_levains_own_card_signs_nothing_and_pushes(capsys, ddir, root, keys, signq, tmp_path, pushes,
                                                                 no_touch):
    init(capsys, ddir, root, keys)
    code, out, _ = request(capsys, ddir, root, signq, tmp_path)
    assert code == 0, out
    rid = rid_of(signq)
    req = read_request(signq, rid)
    assert req.refused is None and req.namespace == ANS and req.requester == "levain"
    assert LEVAIN_SIGN_REQUESTS[ANS] and req.message.startswith("levain-dispatch-config-approve/1\nstore: ")
    assert "kind: verifier\ngeneration: 1\nprev: none\n" in req.message
    # the message is bound to the exact bytes kept for the laptop
    sha = [l for l in req.message.split("\n") if l.startswith("item-sha256: ")][0].split(": ")[1]
    raw = pc.load_pending(ddir, sha)
    assert hashlib.sha256(raw).hexdigest() == sha and ctl.parse_item(raw).kind == "verifier"
    assert gen(ddir, root) == 0 and no_touch.calls == 0               # nothing signed, nothing accepted
    assert len(pushes) == 1 and pushes[0]["url"].endswith(rid) and "accept-approved" in out


def test_request_refuses_a_body_the_registry_refuses_and_files_nothing(capsys, ddir, root, keys, signq, tmp_path):
    init(capsys, ddir, root, keys)
    code, out, _ = request(capsys, ddir, root, signq, tmp_path, "templates", [tmpl()])    # templates before a verifier
    assert code != 0 and (not sign_dir(signq).exists() or not list(sign_dir(signq).glob("lv-*.json")))
    assert gen(ddir, root) == 0 and not (ddir / pc.PENDING_DIR).exists()


# --- the happy path: phone approves, laptop key signs, the unchanged store accepts ------------------------------------

def test_a_phone_approval_then_the_laptop_key_lands_the_item(capsys, ddir, root, keys, phones, signq, tmp_path,
                                                             pushes):
    init(capsys, ddir, root, keys)
    request(capsys, ddir, root, signq, tmp_path)
    rid = rid_of(signq)
    phone_sign(signq, rid, phones["good"])
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code == 0, out
    assert "accepted generation 1 (verifier)" in out and "approved on the phone" in out and "levain-dispatch" not in out
    s = store(ddir, root)
    assert s.generation() == 1 and s.trust_root() == keys["laptop"].fp          # the root is the laptop key, unchanged
    assert not list((ddir / pc.PENDING_DIR).glob("*.item"))                     # the kept item is spent
    # the stored signature is the LAPTOP key's, in the configuration namespace
    import sqlite3
    with sqlite3.connect(ddir / ctl.DB_NAME) as c:
        text, sig = c.execute("SELECT item, signature FROM items").fetchone()
    assert ctl.parse_item(text.encode()).kind == "verifier"
    assert keys["laptop"].sign(text.encode(), ctl.CONFIG_NAMESPACE).splitlines()[0] == sig.splitlines()[0]
    # replay: the same approval lands nothing twice
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and gen(ddir, root) == 1


def test_it_chains_after_existing_configuration(capsys, ddir, root, keys, phones, signq, tmp_path, pushes):
    configure(capsys, ddir, root, keys, tmp_path)
    assert gen(ddir, root) == 3
    request(capsys, ddir, root, signq, tmp_path, "provider_policy", POLICY.to_mapping(), "p2")
    rid = rid_of(signq)
    phone_sign(signq, rid, phones["good"])
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code == 0, out
    assert gen(ddir, root) == 4


# --- refusals: each is before the laptop key is touched ---------------------------------------------------------------

def _filed(capsys, ddir, root, keys, signq, tmp_path):
    init(capsys, ddir, root, keys)
    request(capsys, ddir, root, signq, tmp_path)
    return rid_of(signq)


def test_an_unsigned_request_lands_nothing_and_never_reaches_the_key(capsys, ddir, root, keys, signq, tmp_path,
                                                                    pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "not signed yet" in out and gen(ddir, root) == 0 and no_touch.calls == 0


def test_a_denied_request_lands_nothing(capsys, ddir, root, keys, phones, signq, tmp_path, pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_deny(signq, rid, phones["good"])
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "denied on the phone" in out and gen(ddir, root) == 0 and no_touch.calls == 0


def test_a_revoked_passkey_approves_nothing(capsys, ddir, root, keys, phones, signq, tmp_path,
                                                                   pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    # the Signer is handed the key directly, so it signs for any key it is given; the CLI reads the AUTHORITY log
    phone_sign(signq, rid, phones["revoked"])
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "not an active passkey enrolled here" in out and gen(ddir, root) == 0 and no_touch.calls == 0


def test_a_stranger_passkey_approves_nothing(capsys, ddir, root, keys, phones, signq, tmp_path, pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["stranger"])
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "not an active passkey enrolled here" in out and gen(ddir, root) == 0 and no_touch.calls == 0


def test_a_tampered_signature_is_refused(capsys, ddir, root, keys, phones, signq, tmp_path, pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    other = phones["good"]
    # a signature the same passkey made over ANOTHER message in the namespace does not carry over
    request(capsys, ddir, root, signq, tmp_path, name="other")
    rid2 = [p.stem for p in sorted(sign_dir(signq).glob("lv-*.json")) if "." not in p.stem and p.stem != rid][0]
    phone_sign(signq, rid2, other)
    (sign_dir(signq) / f"{rid}.sig").write_text((sign_dir(signq) / f"{rid2}.sig").read_text())
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "does not verify" in out and gen(ddir, root) == 0 and no_touch.calls == 0


def test_an_expired_request_lands_nothing(capsys, ddir, root, keys, phones, signq, tmp_path, pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    path = sign_dir(signq) / f"{rid}.json"
    body = json.loads(path.read_text())
    body["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    path.write_text(json.dumps(body) + "\n")
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and gen(ddir, root) == 0 and no_touch.calls == 0


def test_the_approval_covers_exactly_the_kept_bytes(capsys, ddir, root, keys, phones, signq, tmp_path, pushes,
                                                    no_touch):
    """Swapping the kept item for another one changes its hash: the approval is over the first, so nothing signs."""
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    [kept] = (ddir / pc.PENDING_DIR).glob("*.item")
    s = store(ddir, root)
    evil = ctl.item_bytes("verifier", 1, VERIFIERS, datetime.now(timezone.utc), prev="", store_id=s.store_id())
    assert hashlib.sha256(evil).hexdigest() != kept.stem
    kept.write_bytes(evil)
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "not the one the approval is over" in out and gen(ddir, root) == 0 and no_touch.calls == 0


def test_a_kept_item_for_another_store_or_generation_than_approved_is_refused(capsys, ddir, root, keys, phones, signq,
                                                                               tmp_path, pushes, no_touch):
    """The approval's own fields (store, kind, generation, prev) must be the kept item's. A request is filed by hand
    over an item whose bytes hash correctly but whose fields differ from the message."""
    init(capsys, ddir, root, keys)
    s = store(ddir, root)
    raw = ctl.item_bytes("verifier", 1, VERIFIERS, datetime.now(timezone.utc), prev="", store_id=s.store_id())
    pc._write_pending(ddir, hashlib.sha256(raw).hexdigest(), raw)
    lying = (f"{pc.FORMAT_LINE}\nstore: {s.store_id()}\nkind: verifier\ngeneration: 7\nprev: none\n"
             f"item-sha256: {hashlib.sha256(raw).hexdigest()}\nchange: x\nsummary: y\n")
    from levain.consent.sign import file_levain_request
    rid = file_levain_request(signq, ANS, lying, title="t", ttl_s=600)
    phone_sign(signq, rid, phones["good"])
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "not the item" in out and gen(ddir, root) == 0 and no_touch.calls == 0


def test_a_stale_approval_after_the_store_moved_is_refused_before_the_touch(capsys, ddir, root, keys, phones, signq,
                                                                           tmp_path, pushes, monkeypatch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    code, out, _ = run(capsys, "config", "sign", "verifier", body_file(tmp_path, "v2", VERIFIERS), "--dispatch-dir",
                       str(ddir), "--data-root", root, "--signing-key", str(keys["laptop"].path), "--yes")
    assert code == 0 and gen(ddir, root) == 1
    nt = NoTouch()
    monkeypatch.setattr("levain.cockpit.keys.ssh_signer", nt)
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "stale" in out and gen(ddir, root) == 1 and nt.calls == 0


def test_only_the_pinned_root_signs_even_with_an_approval(capsys, ddir, root, keys, phones, signq, tmp_path, pushes):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid, key="phone")
    assert code != 0 and "not the pinned trust root" in out and gen(ddir, root) == 0


def test_a_phone_approval_is_not_a_configuration_signature(capsys, ddir, root, keys, phones, signq, tmp_path, pushes):
    """The trust root is unchanged: the approval's SSHSIG, handed to the store as if it were the config signature,
    is refused (wrong namespace, wrong signer), so nothing but the root's own signature lands an item."""
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    [kept] = (ddir / pc.PENDING_DIR).glob("*.item")
    sig = (sign_dir(signq) / f"{rid}.sig").read_text()
    s = store(ddir, root)
    with pytest.raises(ctl.ConfigRefused):
        s.accept(kept.read_bytes(), sig)
    assert s.generation() == 0


@pytest.mark.parametrize("verb", ["request", "accept-approved"])
def test_an_agent_cannot_request_or_accept_approved(capsys, ddir, root, keys, signq, tmp_path, monkeypatch, verb,
                                                    no_touch):
    init(capsys, ddir, root, keys)
    monkeypatch.setattr(dcli, "agent_caller", lambda: "claude_code")
    argv = (["request", "verifier", body_file(tmp_path, "v", VERIFIERS)] if verb == "request"
            else ["accept-approved", "lv-" + "0" * 32, "--signing-key", str(keys["laptop"].path)])
    code, out, _ = run(capsys, "config", *argv, "--dispatch-dir", str(ddir), "--data-root", root,
                       "--sign-queue-dir", str(signq))
    assert code == 2 and "is the operator's own act" in out and no_touch.calls == 0
    assert not (ddir / pc.PENDING_DIR).exists()


def test_the_message_is_one_ascii_line_per_field():
    assert pc._one_line("a\nb‮c") == "a b\\u202ec"
    assert len(pc._one_line("x" * 1000)) == 300


def test_an_expired_request_says_so(capsys, ddir, root, keys, phones, signq, tmp_path, pushes):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    mine = lambda: {phones["good"].key.fingerprint: phones["good"].key}  # noqa: E731
    assert pc.read_approval(signq, rid, keys=mine).request_id == rid                     # usable now
    with pytest.raises(ctl.ConfigRefused, match="expired"):                                # not two hours on
        pc.read_approval(signq, rid, keys=mine, now=datetime.now(timezone.utc) + timedelta(hours=2))


def test_a_card_in_another_namespace_with_the_same_words_approves_nothing(capsys, ddir, root, keys, phones, signq,
                                                                           tmp_path, pushes, no_touch):
    """A generic request in a namespace a caller may name, holding the approval grammar, signed by the real phone
    passkey, is not an approval: only levain's own namespace is."""
    init(capsys, ddir, root, keys)
    s = store(ddir, root)
    raw = ctl.item_bytes("verifier", 1, VERIFIERS, datetime.now(timezone.utc), prev="", store_id=s.store_id())
    sha = hashlib.sha256(raw).hexdigest()
    pc._write_pending(ddir, sha, raw)
    message = (f"{pc.FORMAT_LINE}\nstore: {s.store_id()}\nkind: verifier\ngeneration: 1\nprev: none\n"
               f"item-sha256: {sha}\nchange: x\nsummary: y\n")
    rid = "lv-" + "a" * 32
    d = sign_dir(signq)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps({
        "format": "levain-sign-request/1", "id": rid, "namespace": "flow-needs-phill", "message": message,
        "title": "t", "requester": "levain",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}))
    pk = phones["good"]
    sg = Signer(signq, ("flow-needs-phill",), RP, lambda: {pk.key.fingerprint: pk.key})
    ch = sg.challenge(rid, {"signer_fp": pk.key.fingerprint, "request_sha256": read_request(signq, rid).sha256})
    sg.answer(rid, pk.assertion(ch["challenge"], ORIGIN))
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "not a configuration approval levain filed" in out and gen(ddir, root) == 0
    assert no_touch.calls == 0


def test_a_record_not_over_the_message_is_refused(capsys, ddir, root, keys, phones, signq, tmp_path, pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    rec = sign_dir(signq) / f"{rid}.signed.json"
    data = json.loads(rec.read_text())
    data["message_sha256"] = "0" * 64
    rec.write_text(json.dumps(data))
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "is not over its message" in out and gen(ddir, root) == 0 and no_touch.calls == 0
