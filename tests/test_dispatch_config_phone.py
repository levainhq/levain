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


def _soon(minutes: int = 10) -> str:
    from levain.cockpit.consent import utc_stamp
    return utc_stamp(datetime.now(timezone.utc) + timedelta(minutes=minutes))


class DuringTouch:
    """The real ``ssh_signer`` whose sign runs ``hook`` first: what happens while the operator's touch waits."""

    def __init__(self, hook):
        from levain.cockpit import keys as ck
        self.real, self.hook, self.calls = ck.ssh_signer, hook, 0

    def __call__(self, path, namespace):
        fp, sign = self.real(path, namespace=namespace)

        def touched(message: bytes) -> str:
            self.calls += 1
            self.hook()
            return sign(message)
        return fp, touched


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
             f"item-sha256: {hashlib.sha256(raw).hexdigest()}\nexpires: {_soon()}\nchange: x\nsummary: y\n")
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
    mine = lambda rp: {phones["good"].key.fingerprint: phones["good"].key}  # noqa: E731
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
               f"item-sha256: {sha}\nexpires: {_soon()}\nchange: x\nsummary: y\n")
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


# --- L3 round (cfgsign): the point of use, the lock, the bounded reads, the signed deadline ----------------------------

def test_a_passkey_revoked_while_the_touch_waits_lands_nothing(capsys, ddir, root, keys, phones, log, signq, tmp_path,
                                                               pushes, monkeypatch):
    """codex/gemini HIGH: the approval held when read; the passkey is revoked during the laptop touch. The approval
    is judged again after the touch, so the signature is discarded and the store is never given the item."""
    pk = _Passkey()
    log.link({"op": "enrol", "fingerprint": pk.key.fingerprint, "public_key": pk.line, "class": "display-phone",
              "custody": "passkey"})
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, pk)
    touch = DuringTouch(lambda: log.link({"op": "revoke_key", "fingerprint": pk.key.fingerprint}))
    monkeypatch.setattr("levain.cockpit.keys.ssh_signer", touch)
    code, out, err = approve(capsys, ddir, root, keys, signq, rid)
    assert touch.calls == 1                                          # it got as far as the touch, then refused
    assert code != 0 and "discarded" in out and "not an active passkey" in out and gen(ddir, root) == 0
    assert list((ddir / pc.PENDING_DIR).glob("*.item"))                # nothing was spent


def test_an_approval_that_expires_while_the_touch_waits_lands_nothing(capsys, ddir, root, keys, phones, signq, tmp_path,
                                                                      pushes, monkeypatch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    touch = DuringTouch(lambda: monkeypatch.setattr(pc, "_now", lambda: later))
    monkeypatch.setattr("levain.cockpit.keys.ssh_signer", touch)
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert touch.calls == 1 and code != 0 and "expired" in out and "discarded" in out and gen(ddir, root) == 0


def test_the_deadline_is_the_signed_one_editing_the_request_file_cannot_extend_it(capsys, ddir, root, keys, phones,
                                                                                    signq, tmp_path, pushes):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    path = sign_dir(signq) / f"{rid}.json"
    body = json.loads(path.read_text())
    body["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    path.write_text(json.dumps(body) + "\n")
    mine = lambda rp: {phones["good"].key.fingerprint: phones["good"].key}  # noqa: E731
    with pytest.raises(ctl.ConfigRefused, match="expired"):
        pc.read_approval(signq, rid, keys=mine, now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert "\nexpires: " in read_request(signq, rid).message                    # the phone signs the deadline


def test_a_second_accept_approved_on_a_held_item_is_refused_before_the_touch(capsys, ddir, root, keys, phones, signq,
                                                                            tmp_path, pushes, no_touch):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    [kept] = (ddir / pc.PENDING_DIR).glob("*.item")
    with pc.claimed(ddir, kept.stem):
        code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "holds this item" in out and no_touch.calls == 0 and gen(ddir, root) == 0


def test_the_item_is_shown_in_full_before_the_touch(capsys, ddir, root, keys, phones, signq, tmp_path, pushes,
                                                   monkeypatch):
    """complement MED: no y/N, so the view above the touch is the operator's only look; it is printed before the
    signer is even built."""
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    seen: list[str] = []
    touch = DuringTouch(lambda: seen.append(capsys.readouterr().out))
    monkeypatch.setattr("levain.cockpit.keys.ssh_signer", touch)
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code == 0, out
    [shown] = seen
    assert "levain dispatch config: the item" in shown and "new kind: no verifier item is active" in shown
    assert '+  "verifiers": [' in shown                                      # the body itself, not a count


def test_an_approval_reads_back_after_the_cockpit_withdrew_its_origin(capsys, ddir, root, keys, phones, signq, tmp_path,
                                                                      pushes):
    """codex MED: the cockpit stopped between the phone tap and the laptop touch (shutdown withdraws the origin)."""
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    (signq / "cockpit-origin.json").unlink()
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code == 0 and gen(ddir, root) == 1, out


@pytest.mark.parametrize("shape", ["fifo", "symlink", "huge"])
def test_result_files_are_read_bounded_regular_and_unfollowed(capsys, ddir, root, keys, phones, signq, tmp_path,
                                                              pushes, no_touch, shape):
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    rec = sign_dir(signq) / f"{rid}.signed.json"
    real = rec.read_text()
    rec.unlink()
    if shape == "fifo":
        os.mkfifo(rec)
    elif shape == "symlink":
        other = tmp_path / "rec.json"
        other.write_text(real)
        rec.symlink_to(other)
    else:
        rec.write_text(real[:-2] + " " * (1 << 17) + "}\n")
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code != 0 and "cannot be read" in out and no_touch.calls == 0 and gen(ddir, root) == 0


def test_a_request_the_queue_cannot_take_keeps_no_item_and_raises_no_traceback(capsys, ddir, root, keys, signq,
                                                                              tmp_path):
    init(capsys, ddir, root, keys)
    d = sign_dir(signq)
    d.mkdir(mode=0o700)
    d.chmod(0o500)
    try:
        code, out, err = request(capsys, ddir, root, signq, tmp_path)
    finally:
        d.chmod(0o700)
    assert code != 0 and "could not be filed" in out and "Traceback" not in err
    assert not list((ddir / pc.PENDING_DIR).glob("*"))


def test_a_grammar_refusal_keeps_no_item(ddir, monkeypatch, tmp_path, signq):
    from levain.consent import sign as csign
    raw = ctl.item_bytes("verifier", 1, VERIFIERS, datetime.now(timezone.utc), prev="", store_id="0" * 32)

    def boom(*a, **k):
        raise ValueError("outside the grammar")
    monkeypatch.setattr(csign, "file_levain_request", boom)
    with pytest.raises(ValueError):
        pc.file_request(ddir, signq, raw, change="c", summary="s")
    assert not list((ddir / pc.PENDING_DIR).glob("*"))


def test_kept_items_are_written_under_a_private_temporary_and_pruned_when_stale(ddir, monkeypatch):
    raw = ctl.item_bytes("verifier", 1, VERIFIERS, datetime.now(timezone.utc), prev="", store_id="0" * 32)
    sha = hashlib.sha256(raw).hexdigest()
    d = ddir / pc.PENDING_DIR
    d.mkdir(mode=0o700, parents=True)
    (d / f".{sha}.tmp").symlink_to("/nonexistent")       # the old fixed name: another writer's, never reused
    assert pc._write_pending(ddir, sha, raw) is True and pc._write_pending(ddir, sha, raw) is False
    assert pc.load_pending(ddir, sha) == raw and [p.name for p in d.glob("*.tmp")] == [f".{sha}.tmp"]
    real_replace = os.replace
    monkeypatch.setattr(pc.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        pc._write_pending(ddir, "f" * 64, raw)
    monkeypatch.setattr(pc.os, "replace", real_replace)
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp") and not p.is_symlink()]
    # stale: older than any approval over it can be used, so the next request removes it (a fresh one stays)
    old = __import__("time").time() - pc._PRUNE_AGE_S - 60
    os.utime(d / f"{sha}.item", (old, old))
    from levain.consent import sign as csign
    monkeypatch.setattr(csign, "file_levain_request", lambda *a, **k: "lv-" + "b" * 32)
    fresh = ctl.item_bytes("verifier", 1, VERIFIERS, datetime.now(timezone.utc) + timedelta(seconds=1), prev="",
                           store_id="0" * 32)
    assert pc.file_request(ddir, ddir, fresh, change="c", summary="s") == "lv-" + "b" * 32
    assert not (d / f"{sha}.item").exists() and (d / f"{hashlib.sha256(fresh).hexdigest()}.item").exists()


def test_the_class_floor_is_judged(signq, capsys, ddir, root, keys, phones, tmp_path, pushes):
    """complement LOW: a passkey is always display-phone today, so the floor is fed a key of a class below it."""
    from types import SimpleNamespace
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    fp = phones["good"].key.fingerprint
    low = SimpleNamespace(key_class="no-such-class", public_key=phones["good"].line, fingerprint=fp)
    with pytest.raises(ctl.ConfigRefused, match="does not cover T2"):
        pc.read_approval(signq, rid, keys=lambda rp: {fp: low})


def test_an_agent_holding_a_signed_approval_still_cannot_accept_it(capsys, ddir, root, keys, phones, signq, tmp_path,
                                                                   pushes, monkeypatch, no_touch):
    """gemini HIGH (verify): through the real dispatch, with an approval that WOULD land for the operator."""
    rid = _filed(capsys, ddir, root, keys, signq, tmp_path)
    phone_sign(signq, rid, phones["good"])
    monkeypatch.setattr(dcli, "agent_caller", lambda: "claude_code")
    code, out, _ = approve(capsys, ddir, root, keys, signq, rid)
    assert code == 2 and "is the operator's own act" in out and no_touch.calls == 0 and gen(ddir, root) == 0
    assert list((ddir / pc.PENDING_DIR).glob("*.item"))
