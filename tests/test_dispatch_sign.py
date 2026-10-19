"""The operator's one signature over a filed contract's digest: its own namespace, the job's fields, the signer checks
in the consent queue's order (as the module intends; this file pins it). Keys are real ssh-keygen keys enrolled in a real signed authority log (the consent
queue tests' ``Key`` and ``Log``), and the job is a real filed job over a real git repo."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import AuthoritySnapshot
from levain.cockpit.consent import utc_stamp
from levain.cockpit.keys import fingerprint_of
from levain.consent.passkey import public_key_line, to_sshsig
from levain.dispatch import sign as dsign
from levain.dispatch.jobs import UNSIGNED_LEASE, JobStore
from tests.test_consent_queue import Key, Log
from tests.test_dispatch_contract import NOW, POLICY, make_repo, registry, submission

NS = dsign.DISPATCH_SIGN_NAMESPACE
_FIX = json.loads((Path(__file__).parent / "fixtures" / "passkey_assertion_virtual.json").read_text(encoding="utf-8"))
_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("dispatch-keys")
    return {n: Key(d, n) for n in ("laptop", "phone", "presence", "revoked", "stranger")}


def passkey_line() -> str:
    return public_key_line(_SPKI_PREFIX + bytes.fromhex(_FIX["pub_point_hex"]), _FIX["rp_id"])


@pytest.fixture(scope="module")
def log(keys, tmp_path_factory):
    """laptop (genesis, laptop-presence), phone (display-phone), presence (presence: below T2), a revoked
    display-phone key, and the recorded passkey (display-phone, custody passkey)."""
    lg = Log(tmp_path_factory.mktemp("dispatch-auth"), keys)
    for name, cls in (("laptop", "laptop-presence"), ("phone", "display-phone"), ("presence", "presence"),
                      ("revoked", "display-phone")):
        lg.enrol(name, cls)
    lg.revoke("revoked")
    pk = passkey_line()
    lg.link({"op": "enrol", "fingerprint": fingerprint_of(pk), "public_key": pk, "class": "display-phone",
             "custody": auth.PASSKEY})
    return lg


@pytest.fixture(scope="module")
def snapshot(log) -> AuthoritySnapshot:
    return log.source()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("dispatch-sign-repo"))


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return JobStore(d, clock=Clock(NOW))


@pytest.fixture
def job(store, repo):
    path, base = repo
    j, _cap = store.file(submission(path, base), resolve_template=registry(), policy=POLICY, filer_session="sess-1")
    return j


def signed(job, key, *, at=None, **over):
    payload = dsign.sign_payload(job, signer_fp=key.fp, signed_at=at or NOW + timedelta(minutes=1))
    payload.update(over)
    return payload, key.sign(dsign.sign_message(payload), NS)


def test_a_display_phone_and_a_laptop_key_sign(job, keys, snapshot):
    for name in ("phone", "laptop"):
        payload, sig = signed(job, keys[name])
        assert dsign.refusal(payload, sig, snapshot, job) is None, name


def test_the_payload_is_exactly_the_seven_fields(job, keys):
    payload = dsign.sign_payload(job, signer_fp=keys["phone"].fp, signed_at=NOW)
    assert set(payload) == dsign.PAYLOAD_KEYS and payload["format"] == "levain-dispatch-sign/1"
    assert payload["digest"] == job.digest and payload["expires_at"] == job.expires_at
    assert payload["store_id"] == job.store_id


@pytest.mark.parametrize("over, match", [
    ({"format": "levain-dispatch-sign/2"}, "format"),
    ({"store_id": "0" * 32}, "another dispatch store"),
    ({"job_id": "dj-" + "0" * 32}, "another job"),
    ({"digest": "sha256:" + "0" * 64}, "another contract digest"),
    ({"expires_at": utc_stamp(NOW + timedelta(hours=1))}, "another expiry"),
    ({"signed_at": "yesterday"}, "not a timestamp"),
    ({"signed_at": utc_stamp(NOW - timedelta(seconds=1))}, "before the contract was filed"),
    ({"signed_at": utc_stamp(NOW + UNSIGNED_LEASE)}, "after the job's lease ended"),
    ({"signer_fp": 7}, "every field"),
])
def test_payload_refusals(job, keys, snapshot, over, match):
    payload, sig = signed(job, keys["phone"], **over)
    assert match in dsign.refusal(payload, sig, snapshot, job)


def test_extra_or_missing_keys_refuse(job, keys, snapshot):
    payload, sig = signed(job, keys["phone"])
    assert "not a dispatch signature record" in dsign.refusal({**payload, "decision": "allow"}, sig, snapshot, job)
    del payload["signed_at"]
    assert "not a dispatch signature record" in dsign.refusal(payload, sig, snapshot, job)
    assert "not a dispatch signature record" in dsign.refusal(None, sig, snapshot, job)


def test_signer_refusals_in_the_queues_order(job, keys, snapshot):
    payload, sig = signed(job, keys["stranger"])
    assert dsign.refusal(payload, sig, snapshot, job) == "the signer is not an enrolled key"
    payload, sig = signed(job, keys["revoked"])
    assert dsign.refusal(payload, sig, snapshot, job) == "the signer's key is revoked"
    payload, sig = signed(job, keys["presence"])
    assert dsign.refusal(payload, sig, snapshot, job) == "key class presence does not cover T2"
    payload, _sig = signed(job, keys["phone"])
    assert dsign.refusal(payload, "", snapshot, job) == "the contract signature is empty"
    assert dsign.refusal(payload, None, snapshot, job) == "the contract signature is empty"


def test_a_signature_over_another_digest_does_not_verify(job, keys, snapshot):
    payload, _ = signed(job, keys["phone"])
    other = dict(payload, digest="sha256:" + "0" * 64)
    sig = keys["phone"].sign(dsign.sign_message(other), NS)
    assert dsign.refusal(payload, sig, snapshot, job) == "the contract signature does not verify"


def test_a_signature_in_another_namespace_does_not_verify(job, keys, snapshot):
    payload, _ = signed(job, keys["phone"])
    for ns in ("levain-consent-answer", "levain-authority", "levain-consent-ruling"):
        sig = keys["phone"].sign(dsign.sign_message(payload), ns)
        assert dsign.refusal(payload, sig, snapshot, job) == "the contract signature does not verify", ns


def test_another_keys_signature_does_not_verify(job, keys, snapshot):
    payload, _ = signed(job, keys["phone"])
    sig = keys["laptop"].sign(dsign.sign_message(payload), NS)
    assert dsign.refusal(payload, sig, snapshot, job) == "the contract signature does not verify"


def test_a_verifier_that_cannot_run_is_a_refusal(job, keys, snapshot, tmp_path):
    payload, sig = signed(job, keys["phone"])
    problem = dsign.refusal(payload, sig, snapshot, job, ssh_keygen=tmp_path / "no-ssh-keygen")
    assert problem is not None and problem.startswith("the contract signature could not be verified")


def test_no_snapshot_refuses(job, keys, log):
    payload, sig = signed(job, keys["phone"])
    assert "no authority snapshot" in dsign.refusal(payload, sig, log.source, job)


def test_a_passkey_key_gets_the_passkey_checks(job, keys, snapshot):
    pk = passkey_line()
    fp = fingerprint_of(pk)
    assert snapshot.key(fp).custody == auth.PASSKEY and snapshot.key(fp).key_class == "display-phone"
    payload = dsign.sign_payload(job, signer_fp=fp, signed_at=NOW + timedelta(minutes=1))
    # a plain ssh signature where the passkey's WebAuthn one belongs
    plain = keys["phone"].sign(dsign.sign_message(payload), NS)
    assert dsign.refusal(payload, plain, snapshot, job) == "a passkey answer must be a WebAuthn signature"
    # a real WebAuthn assertion (recorded, over another message in another namespace) passes the passkey checks and
    # is then refused by the signature check itself
    recorded = to_sshsig(pk, bytes.fromhex(_FIX["authData_hex"]), _FIX["clientDataJSON"].encode(),
                         bytes.fromhex(_FIX["sig_der_hex"]), namespace=_FIX["namespace"])
    assert dsign.refusal(payload, recorded, snapshot, job) == "the contract signature does not verify"


# --- fix round 2 (L3 r1) -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("field", sorted(dsign.PAYLOAD_KEYS))
def test_an_oversized_payload_field_is_refused_before_anything_is_compared(job, keys, snapshot, field):
    # glm: no bound on a payload string or the signature before verifying and storing
    payload, sig = signed(job, keys["phone"], **{field: "x" * (dsign.FIELD_MAX + 1)})
    assert dsign.refusal(payload, sig, snapshot, job) == (
        f"a field of the dispatch signature record is over {dsign.FIELD_MAX} characters")


def test_an_oversized_signature_is_refused_before_it_is_verified(job, keys, snapshot, monkeypatch):
    payload, _sig = signed(job, keys["phone"])
    called = []
    monkeypatch.setattr(dsign, "verify_link_sig", lambda *a, **k: called.append(1) or True)
    assert dsign.refusal(payload, "A" * (dsign.SIGNATURE_MAX + 1), snapshot, job) == (
        f"the contract signature is over {dsign.SIGNATURE_MAX} characters")
    assert called == []


# --- the human cancel (slice b1) ----------------------------------------------------------------------------------

CNS = dsign.DISPATCH_CANCEL_NAMESPACE


def cancelled(job, key, *, at=None, ns=CNS, **over):
    payload = dsign.cancel_payload(job, signer_fp=key.fp, signed_at=at or NOW + timedelta(minutes=1), reason="wrong")
    payload.update(over)
    return payload, key.sign(dsign.sign_message(payload), ns)


def test_a_cancel_is_its_own_payload_in_its_own_namespace(job, keys, snapshot):
    payload, sig = cancelled(job, keys["laptop"])
    assert set(payload) == dsign.CANCEL_KEYS and payload["format"] == "levain-dispatch-cancel/1"
    assert CNS == "levain-dispatch-cancel" and CNS != NS
    assert dsign.cancel_refusal(payload, sig, snapshot, job) is None
    # an empty reason is a reason
    payload, sig = cancelled(job, keys["phone"], reason="")
    assert dsign.cancel_refusal(payload, sig, snapshot, job) is None


def test_a_contract_signature_is_never_a_cancel_and_a_cancel_never_a_contract_signature(job, keys, snapshot):
    # the same key, each payload signed in the OTHER namespace: neither verifies
    payload, sig_in_sign_ns = cancelled(job, keys["phone"], ns=NS)
    assert dsign.cancel_refusal(payload, sig_in_sign_ns, snapshot, job) == "the cancel signature does not verify"
    spayload, _ = signed(job, keys["phone"])
    sig_in_cancel_ns = keys["phone"].sign(dsign.sign_message(spayload), CNS)
    assert dsign.refusal(spayload, sig_in_cancel_ns, snapshot, job) == "the contract signature does not verify"
    # and each payload shape is refused by the other check before any signature is looked at
    good_sign = keys["phone"].sign(dsign.sign_message(spayload), NS)
    assert dsign.cancel_refusal(spayload, good_sign, snapshot, job) == "the cancel payload is not a dispatch cancel record"
    good_cancel = keys["phone"].sign(dsign.sign_message(payload), CNS)
    assert dsign.refusal(payload, good_cancel, snapshot, job) == "the signature payload is not a dispatch signature record"


@pytest.mark.parametrize("over, match", [
    ({"format": "levain-dispatch-sign/1"}, "cancel format"),
    ({"store_id": "0" * 32}, "another dispatch store"),
    ({"job_id": "dj-" + "0" * 32}, "another job"),
    ({"digest": "sha256:" + "0" * 64}, "another contract digest"),
    ({"signed_at": "yesterday"}, "not a timestamp"),
    ({"signed_at": utc_stamp(NOW - timedelta(seconds=1))}, "before the contract was filed"),
    ({"reason": "two\nlines"}, "one line"),
    ({"reason": "x" * (dsign.FIELD_MAX + 1)}, f"over {dsign.FIELD_MAX} characters"),
    ({"signer_fp": 7}, "every field"),
])
def test_cancel_payload_refusals(job, keys, snapshot, over, match):
    payload, sig = cancelled(job, keys["phone"], **over)
    assert match in dsign.cancel_refusal(payload, sig, snapshot, job)


def test_a_cancel_has_no_lease_window(job, keys, snapshot):
    # signed long after the unsigned lease: the cancel is still the operator's (the store decides if the job is live)
    payload, sig = cancelled(job, keys["phone"], at=NOW + UNSIGNED_LEASE + timedelta(hours=5))
    assert dsign.cancel_refusal(payload, sig, snapshot, job) is None


def test_cancel_signer_refusals_in_the_queues_order(job, keys, snapshot):
    for name, want in (("stranger", "the signer is not an enrolled key"), ("revoked", "the signer's key is revoked"),
                       ("presence", "key class presence does not cover T2")):
        payload, sig = cancelled(job, keys[name])
        assert dsign.cancel_refusal(payload, sig, snapshot, job) == want
    payload, _sig = cancelled(job, keys["phone"])
    assert dsign.cancel_refusal(payload, "", snapshot, job) == "the cancel signature is empty"
    assert dsign.cancel_refusal(payload, "A" * (dsign.SIGNATURE_MAX + 1), snapshot, job) == (
        f"the cancel signature is over {dsign.SIGNATURE_MAX} characters")
    assert "no authority snapshot" in dsign.cancel_refusal(payload, _sig, None, job)
    other = keys["laptop"].sign(dsign.sign_message(payload), CNS)
    assert dsign.cancel_refusal(payload, other, snapshot, job) == "the cancel signature does not verify"


def test_a_passkey_cancel_gets_the_passkey_checks(job, keys, snapshot):
    fp = fingerprint_of(passkey_line())
    payload = dsign.cancel_payload(job, signer_fp=fp, signed_at=NOW + timedelta(minutes=1))
    plain = keys["phone"].sign(dsign.sign_message(payload), CNS)
    assert dsign.cancel_refusal(payload, plain, snapshot, job) == "a passkey answer must be a WebAuthn signature"


def test_a_cancel_verifier_that_cannot_run_is_raised_never_a_refusal(job, keys, snapshot, tmp_path):
    payload, sig = cancelled(job, keys["phone"])
    with pytest.raises(auth.VerifierUnavailable):
        dsign.cancel_refusal(payload, sig, snapshot, job, ssh_keygen=tmp_path / "no-ssh-keygen")
