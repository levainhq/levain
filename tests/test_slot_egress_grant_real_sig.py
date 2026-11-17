"""Host grants (board levain#66 slice S2, plan hgs2_plan_v2 T1 and T2, the BLOCKING real-verifier run): the whole
``egress-grant`` verb against a REAL ``ssh-keygen -Y verify`` and signatures made by scratch keys generated under the
test's own temp dir (``tests.cockpit_authority_rig.RealKey``: never the keychain, never the operator's key, never a
provider flag: the spawn keyguard in ``tests/conftest.py`` judges every spawn and passes only scratch keys). Nothing
here replaces ``verify_link_sig``; the other verb tests spy on it."""
from __future__ import annotations

import pytest

from levain.slot import egress_grants as eg
from levain.slot import install_helper as IH
from levain.slot import runtime_helper as RH
from tests.cockpit_authority_rig import RealKey
from tests.test_slot_egress_grant_verbs import (  # noqa: F401 - the verb tests' world and helpers
    S, World, entries, gid_for, raw_message, refused, tap, used_ids, world)

NS = eg.NAMESPACE


@pytest.fixture
def real(tmp_path_factory, world):
    d = tmp_path_factory.mktemp("rk")
    good, other, authority_only = RealKey(d, "good"), RealKey(d, "other"), RealKey(d, "authonly")
    signers = world.L / "policy" / "authority_signers"
    signers.write_text(f'phill namespaces="{NS}" {good.public}\n'
                       f'phill namespaces="levain-authority" {authority_only.public}\n')
    signers.chmod(0o644)
    return world, good, other, authority_only


def deliver(w, key, *, choice="s", signed_choice=None, namespace=NS, fp=None, host="files.example.org", msg=None,
            sign_msg=None):
    msg = msg if msg is not None else raw_message(host)
    sig = key.sign(eg.signed_bytes(sign_msg if sign_msg is not None else msg, signed_choice or choice), namespace)
    return tap(w, host, choice, msg=msg, sig=sig, fp=fp or IH.blob_fingerprint(key.public))


def test_a_real_signature_in_the_egress_namespace_by_the_enrolled_key_is_admitted(real):
    w, good, _o, _a = real
    line = deliver(w, good)
    assert line[0] == RH.EXIT_OK and line[1]["host"] == "files.example.org", line
    assert set(entries(w)) == {"files.example.org"} and used_ids(w) == [gid_for(1)]


@pytest.mark.parametrize("choice", ["s", "d"])
def test_both_choices_verify_over_their_own_bytes(real, choice):
    w, good, _o, _a = real
    assert deliver(w, good, choice=choice)[0] == RH.EXIT_OK


def test_a_signature_made_in_another_namespace_is_refused_and_nothing_is_written(real):
    w, good, _o, _a = real
    refused(deliver(w, good, namespace="levain-authority"), "egress-grant-sig")
    refused(deliver(w, good, namespace="levain-confirm"), "egress-grant-sig")
    assert entries(w) is None and used_ids(w) == []


def test_a_signature_by_a_key_that_is_not_enrolled_is_refused_by_name_of_either_failure(real):
    w, good, other, _a = real
    refused(deliver(w, other), "egress-grant-unenrolled")                               # its own fingerprint: no line
    refused(deliver(w, other, fp=IH.blob_fingerprint(good.public)), "egress-grant-sig")  # the enrolled key's fingerprint
    assert entries(w) is None and used_ids(w) == []


def test_t2_a_key_enrolled_for_levain_authority_only_is_not_enrolled_for_host_grants(real):
    w, _g, _o, authority_only = real
    refused(deliver(w, authority_only), "egress-grant-unenrolled")
    assert entries(w) is None


def test_a_signature_over_one_choice_does_not_verify_the_other(real):
    w, good, _o, _a = real
    refused(deliver(w, good, choice="d", signed_choice="s"), "egress-grant-sig")
    refused(deliver(w, good, choice="s", signed_choice="d"), "egress-grant-sig")
    assert entries(w) is None and used_ids(w) == []


def test_a_message_changed_after_signing_does_not_verify(real):
    w, good, _o, _a = real
    signed_for = raw_message("files.example.org")
    other = raw_message("files.example.org", grant=gid_for(2))
    refused(deliver(w, good, msg=other, sign_msg=signed_for), "egress-grant-sig")
    assert entries(w) is None


def test_a_tampered_signature_is_refused_and_does_not_burn_the_tap(real):
    w, good, _o, _a = real
    msg = raw_message("files.example.org")
    sig = good.sign(eg.signed_bytes(msg, "s"), NS)
    bad = sig.replace("A", "B", 3) if sig.count("A") >= 3 else sig[:-30] + "AAAA" + sig[-26:]
    refused(tap(w, "files.example.org", "s", msg=msg, sig=bad, fp=IH.blob_fingerprint(good.public)),
            "egress-grant-sig")
    assert used_ids(w) == []
    ok = tap(w, "files.example.org", "s", msg=msg, sig=sig, fp=IH.blob_fingerprint(good.public))
    assert ok[0] == RH.EXIT_OK                                   # the same tap, intact, still admitted


# --- levain#109: the enrolled line's principal travels with the key; no request creates a verify lock file -------------

def _grants(w):
    return sorted(p.name for p in (w.L / "routes" / "grants").iterdir())


def _enrol(w, principal, key, options=f'namespaces="{NS}"'):
    signers = w.L / "policy" / "authority_signers"
    signers.write_text(f"{principal} {options} {key.public}\n")
    signers.chmod(0o644)


def test_a_bad_signature_leaves_no_verify_lock_file_and_a_good_one_none_either(real):
    w, good, _o, _a = real
    before = _grants(w)
    refused(deliver(w, good, namespace="levain-authority"), "egress-grant-sig")      # reaches the real ssh-keygen
    assert _grants(w) == before and "verify.lock" not in _grants(w)
    assert deliver(w, good)[0] == RH.EXIT_OK                                        # a good one verifies
    assert "verify.lock" not in _grants(w)


def test_the_verify_lock_is_one_at_a_time_install_wide_while_a_real_verify_would_run(real):
    import fcntl
    import os
    w, good, _o, _a = real
    fd = os.open(w.L / "routes" / "grants", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        refused(deliver(w, good), "egress-grant-busy")
    finally:
        os.close(fd)
    assert deliver(w, good)[0] == RH.EXIT_OK


@pytest.mark.parametrize("principal", ["phill", "alice@example.org", "a.b_c-d+e"])
def test_the_enrolled_lines_own_principal_is_the_identity_verified_under(real, principal):
    w, good, _o, _a = real
    _enrol(w, principal, good)
    assert IH._signer_fields(f'{principal} namespaces="{NS}" {good.public}'.encode())[0] == principal
    from levain.slot import egress_verify as EVY
    assert EVY.enrolled_key(w_ctx(w), IH.blob_fingerprint(good.public)) == (principal, good.public.split()[0] + " "
                                                                           + good.public.split()[1])
    assert deliver(w, good)[0] == RH.EXIT_OK


def w_ctx(w):
    class _Ctx:
        seams = w.seams
    return _Ctx()


@pytest.mark.parametrize("principals", ["*", "a,b", "!x", "*@example.org", '"two words"'])
def test_a_line_whose_principals_are_not_one_literal_word_is_no_enrolment(real, principals):
    w, good, _o, _a = real
    _enrol(w, principals, good)
    refused(deliver(w, good), "egress-grant-unenrolled")
    assert entries(w) is None and used_ids(w) == []


def test_the_principal_is_passed_to_the_verifier_and_a_signature_is_not_valid_under_another_identity(real):
    from levain.authority_sig import verify_link_sig
    w, good, _o, _a = real
    _enrol(w, "phill", good)
    msg = eg.signed_bytes(raw_message("files.example.org"), "s")
    sig = good.sign(msg, NS)
    kw = dict(public_key=good.public, fingerprint=IH.blob_fingerprint(good.public), namespace=NS)
    assert verify_link_sig(msg, sig, principal="phill", **kw) is True
    assert verify_link_sig(msg, sig, principal="authority-signer", **kw) is True      # the identity is the caller's pick
    assert verify_link_sig(msg, sig, principal="*", **kw) is False                     # never a pattern
    assert verify_link_sig(msg, sig, principal="a b", **kw) is False
