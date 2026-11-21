"""levain#191 property tests (Phill's signed np-1246f428 (a) "both"). ``hypothesis`` is not in the venv, so each property is a
seeded loop of 200 random cases (``random.Random(1121)``): the same cases every run, a failure names its case number.

Zero touch: the real helper runs on no-presence keys under tmp_path only (``evaluations`` is 0 on that path, asserted);
the stub helper stands in for everything that needs a presence flag or a hostile answer.

* for random messages and namespaces the helper's signature verifies;
* any single-byte mutation of the message or of the signature fails every cryptographic verifier;
* a helper output that does not verify against the ``.pub`` makes ``se_signer`` raise and never return (corrupted, empty,
  foreign-key and replayed 11-20 empty signatures included);
* a sign never reports more than one presence evaluation (the Swift source has exactly one evaluate call, the context is
  made non-interactive straight after it, and a stub that reports two is refused)."""
from __future__ import annotations

import base64
import json
import random
import re
import sys
from pathlib import Path

import pytest

from levain import authority_sig as asig
from levain.cockpit import keys as ckeys
from levain.cockpit.keys import KeyError_
from levain.consent import se_signer as ses
from levain.consent import terminal_sign as ts
from tests.test_se_differential import (OLD_REAL_SIG, OldPath, _keygen, new_sig, real_helper, up_helper, blob,  # noqa: F401
                                        new_pub)
from tests.test_se_integration import EMPTY_SIG, _keydir, _set, _soft_key, stub_helper  # noqa: F401

CASES = 200
_NS_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789.-"


def _case(i: int) -> tuple[bytes, str]:
    rng = random.Random(1121 * 1000 + i)
    msg = bytes(rng.randrange(256) for _ in range(rng.choice([0, 1, 2, 31, 32, 33, 100, 255, 300])))
    if i % 2 == 0:
        ns = rng.choice([ts.NAMESPACE, ts.DESK_NAMESPACE])
    else:
        # never a leading "-": ssh-keygen would read the value as an option, and the spawn keyguard names it
        ns = rng.choice(_NS_CHARS[:-2]) + "".join(rng.choice(_NS_CHARS) for _ in range(rng.randrange(0, 24)))
    return msg, ns


def _armor(blob_bytes: bytes) -> str:
    b = base64.b64encode(blob_bytes).decode()
    return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b[i:i + 70] for i in range(0, len(b), 70)) \
        + "\n-----END SSH SIGNATURE-----\n"


def _dearmor(sig: str) -> bytes:
    return base64.b64decode("".join(ln for ln in sig.splitlines() if ln and not ln.startswith("-----")))


def _verifiers(sig: str, msg: bytes, ns: str, pub: str, tmp: Path) -> dict[str, bool]:
    """Every CRYPTOGRAPHIC verifier's yes/no (the presence-only checks, has_presence and presence_missing, read a flags
    byte and verify nothing, so they are not in this list)."""
    keyline = " ".join(pub.split()[:2])
    fp = ckeys.fingerprint_of(pub)
    out = {}
    if ns in (ts.NAMESPACE, ts.DESK_NAMESPACE):
        out["terminal_verify"] = ts.verify(msg, sig, ts.signers_line(keyline), "phill", ctk=lambda pk: (True, "i"),
                                           namespace=ns).ok
    out["link_verify"] = asig.verify_link_sig(msg, sig, public_key=keyline, fingerprint=fp, namespace=ns)
    allowed, sigf = tmp / "allowed", tmp / "sig"
    allowed.write_text(f"v@levain {keyline}\n")
    sigf.write_text(sig)
    out["keygen_verify"] = _keygen(["-Y", "verify", "-f", str(allowed), "-I", "v@levain", "-n", ns, "-s", str(sigf)],
                                   stdin=msg, tmp=tmp)[0] == 0
    out["keygen_check_novalidate"] = _keygen(["-Y", "check-novalidate", "-n", ns, "-s", str(sigf)], stdin=msg,
                                             tmp=tmp)[0] == 0
    return out


# ---------------------------------------------------------------------------------------------------
# the helper's signatures verify; any one-byte mutation fails every verifier


@pytest.mark.skipif(sys.platform != "darwin", reason="the Secure Enclave helper is macOS only")
def test_random_messages_and_namespaces_verify_and_every_single_byte_mutation_fails(tmp_path, blob, up_helper, new_pub):
    last_counter = 0
    for i in range(CASES):
        msg, ns = _case(i)
        out = ses.sign_once(blob, msg, namespace=ns, helper=up_helper)
        sig = out["signature"]
        assert out["evaluations"] == 0 and out["evaluated"] is False and out["attempts"] == 1, f"case {i}"
        assert out["counter"] > last_counter, f"case {i}: the counter moves forward"
        last_counter = out["counter"]
        good = _verifiers(sig, msg, ns, new_pub, tmp_path)
        assert all(good.values()), f"case {i}: {good}"
        # one byte of the message (an empty message gets one)
        rng = random.Random(7000 + i)
        if msg:
            k = rng.randrange(len(msg))
            bad_msg = msg[:k] + bytes([msg[k] ^ (1 << rng.randrange(8))]) + msg[k + 1:]
        else:
            bad_msg = b"\x00"
        assert not any(_verifiers(sig, bad_msg, ns, new_pub, tmp_path).values()), f"case {i}: a mutated message verified"
        # one byte of the signature blob (re-armored: a flipped base64 padding bit is not a mutation)
        raw = bytearray(_dearmor(sig))
        k = rng.randrange(len(raw))
        raw[k] ^= 1 << rng.randrange(8)
        bad = _verifiers(_armor(bytes(raw)), msg, ns, new_pub, tmp_path)
        assert not any(bad.values()), f"case {i}: signature byte {k} mutated and still verified: {bad}"


@pytest.mark.skipif(sys.platform != "darwin", reason="the Secure Enclave helper is macOS only")
def test_the_shipped_helper_on_a_no_presence_key_verifies_under_ssh_keygen_for_random_cases(tmp_path, blob, real_helper,
                                                                                           new_pub):
    for i in range(CASES):
        msg, ns = _case(i)
        out = ses.sign_once(blob, msg, namespace=ns, helper=real_helper)
        assert out["evaluations"] == 0 and out["flags"] == 0 and out["presence"] is False, f"case {i}"
        good = _verifiers(out["signature"], msg, ns, new_pub, tmp_path)
        assert good["keygen_verify"] and good["keygen_check_novalidate"], f"case {i}"
        assert not good["link_verify"], f"case {i}: flags 0x00 must be refused by the presence rule"


def test_the_old_path_loop_is_a_control_the_mutations_really_fail_and_the_unmutated_verifies(tmp_path):
    pytest.importorskip("cryptography")
    old = OldPath()
    for i in range(20):
        msg, ns = _case(i)
        sig = old.sign(msg, ns)
        assert all(_verifiers(sig, msg, ns, old.pub, tmp_path).values()), f"case {i}"
        raw = bytearray(_dearmor(sig))
        raw[random.Random(i).randrange(len(raw))] ^= 0x10
        assert not any(_verifiers(_armor(bytes(raw)), msg, ns, old.pub, tmp_path).values()), f"case {i}"


# ---------------------------------------------------------------------------------------------------
# se_signer never returns a signature that does not verify against the .pub


def _replay(key: Path, out: dict) -> None:
    _set(key, ".replay", json.dumps(out))


def _bad_outputs(key: Path, d: Path) -> list[tuple[str, dict]]:
    other = _soft_key(d, label="other")
    foreign = ses.se_signer(other, namespace=ts.NAMESPACE, helper=_STUB[0])(b"m")        # a good signature, by another key
    good = ses.se_signer(key, namespace=ts.NAMESPACE, helper=_STUB[0])(b"m")
    base = {"ok": True, "attempts": 1, "evaluated": True, "evaluations": 1}
    cases = [("empty string", {**base, "signature": ""}), ("whitespace", {**base, "signature": " \n "}),
             ("no field", dict(base)), ("null", {**base, "signature": None}), ("number", {**base, "signature": 7}),
             ("the 11-20 empty signature", {**base, "signature": EMPTY_SIG}),
             ("the 11-20 empty signature, unwrapped", {**base, "signature": EMPTY_SIG.replace("\n", "")}),
             ("the real 11-20 old-path signature (another key's)", {**base, "signature": OLD_REAL_SIG}),
             ("a foreign key's good signature", {**base, "signature": foreign}),
             ("a good signature over other bytes", {**base, "signature": ses.se_signer(
                 key, namespace=ts.NAMESPACE, helper=_STUB[0])(b"other bytes")}),
             ("a good signature in another namespace", {**base, "signature": ses.se_signer(
                 key, namespace=ts.DESK_NAMESPACE, helper=_STUB[0])(b"m")}),
             ("truncated", {**base, "signature": good[:len(good) // 2]}),
             ("garbage", {**base, "signature": "-----BEGIN SSH SIGNATURE-----\n!!!!\n-----END SSH SIGNATURE-----\n"}),
             ("evaluations 2", {**base, "signature": good, "evaluations": 2}),
             ("evaluations 5", {**base, "signature": good, "evaluations": 5}),
             ("evaluations a string", {**base, "signature": good, "evaluations": "1"}),
             ("evaluations True", {**base, "signature": good, "evaluations": True})]
    rng = random.Random(1120)
    raw = _dearmor(good)
    for n in range(100):                                                           # one-byte mutations of a good signature
        m = bytearray(raw)
        m[rng.randrange(len(m))] ^= 1 << rng.randrange(8)
        cases.append((f"mutation {n}", {**base, "signature": _armor(bytes(m))}))
    return cases


_STUB: list[Path] = []


@pytest.fixture()
def _stub_registry(stub_helper):  # noqa: F811
    _STUB[:] = [stub_helper]
    yield stub_helper
    _STUB.clear()


def test_se_signer_raises_for_every_output_that_does_not_verify_against_the_pub_and_never_returns(tmp_path, _stub_registry):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    # the control: the stub's honest answer verifies and is returned
    assert ses.se_signer(key, namespace=ts.NAMESPACE, helper=_stub_registry)(b"m").startswith("-----BEGIN SSH SIGNATURE")
    returned: list[str] = []
    for label, out in _bad_outputs(key, d):
        _replay(key, out)
        try:
            returned.append(ses.se_signer(key, namespace=ts.NAMESPACE, helper=_stub_registry)(b"m"))
        except ses.SeSignerError:
            continue
        pytest.fail(f"se_signer returned for {label}")
    assert returned == []


def test_the_product_signer_refuses_the_same_outputs_too(tmp_path, _stub_registry):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _fp, sign = ckeys.ssh_signer(key, namespace=ts.NAMESPACE, helper=_stub_registry)
    assert sign(b"m").startswith("-----BEGIN SSH SIGNATURE")
    for label, out in _bad_outputs(key, d)[:17]:                    # the named ones; the mutations are the loop above
        _replay(key, out)
        with pytest.raises(KeyError_):
            sign(b"m")


def test_se_signer_needs_the_pub_and_never_signs_without_one(tmp_path, _stub_registry):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    Path(str(key) + ".pub").unlink()
    with pytest.raises(ses.SeSignerError) as ei:
        ses.se_signer(key, namespace=ts.NAMESPACE, helper=_stub_registry)(b"m")
    assert ei.value.code == "no_pub"


def test_a_stub_that_reports_evaluations_is_counted_at_most_once(tmp_path, _stub_registry):
    key = _soft_key(_keydir(tmp_path))
    sign = ses.se_signer(key, namespace=ts.NAMESPACE, helper=_stub_registry)
    for evaluations in ("0", "1"):
        _set(key, ".evaluations", evaluations)
        out = ses.sign_once(key, b"m", namespace=ts.NAMESPACE, helper=_stub_registry)
        assert out["evaluations"] <= 1
        assert sign(b"m")
    _set(key, ".evaluations", "2")
    with pytest.raises(ses.SeSignerError) as ei:
        sign(b"m")
    assert ei.value.code == "too_many_evaluations"


def test_the_sign_log_never_takes_a_malformed_helper_field_as_it_is():
    rec = ses.sign_log_record(outcome="x" * 500, attempts="3", evaluated="yes", elapsed_s=1.0, code="c" * 500,
                              evaluations={"a": 1})
    assert rec["attempts"] is None and rec["silent_retries"] is None and rec["evaluated"] is None
    assert rec["evaluations"] is None and len(rec["code"]) <= 80 and len(rec["outcome"]) <= 40


# ---------------------------------------------------------------------------------------------------
# the Swift source: one evaluate, then no interaction; one access-control builder; no presence flag on sign


def _sign_case(src: str) -> str:
    a = src.index('case "sign":')
    return src[a:src.index('case "status":')]


def test_the_swift_helper_evaluates_once_and_then_forbids_interaction():
    src = ses.SOURCE.read_text()
    sign = _sign_case(src)
    assert src.count("evaluateAccessControl(") == 1 and sign.count("evaluateAccessControl(") == 1
    ev, off, load = sign.index("evaluateAccessControl("), sign.index("interactionNotAllowed = true"), sign.index("loadKey(")
    assert ev < off < load                                  # no key use, and so no sign, before the context is closed
    assert sign.count("c.invalidate()") == 1 and "presence_timeout" in sign
    assert "sem.wait(timeout: .now() + .milliseconds(wallMs)) == .timedOut" in sign   # the caller's wall, invalidated on expiry
    # the wall is the caller's --wall-ms (required on sign), clamped to 1 s .. PRESENCE_WALL_SECONDS, which stays the ceiling
    assert 'Int(need("--wall-ms"))' in sign
    assert "let wallMs = min(max(wallAsked, 1000), PRESENCE_WALL_SECONDS * 1000)" in sign
    assert re.search(r"^let PRESENCE_WALL_SECONDS = 60\b", src, re.M) is not None
    assert "evaluations += 1" in sign and sign.count("evaluations += 1") == 1
    # no loop around the evaluate
    assert re.search(r"(while|for|repeat)\b[^\n]*\n(?:[^\n]*\n){0,12}?[^\n]*evaluateAccessControl", sign) is None


def test_the_swift_helper_has_one_access_control_builder_biometry_only_and_reads_presence_from_the_meta_file():
    src = ses.SOURCE.read_text()
    assert src.count("SecAccessControlCreateWithFlags(") == 1
    builder = src[src.index("func makeAccessControl"):src.index("// ---- <blob>.meta.json")]
    assert ".biometryAny" in builder and ".privateKeyUsage" in builder
    assert ".userPresence" not in re.sub(r"//[^\n]*", "", src) and "devicePasscode" not in src
    assert src.count("makeAccessControl(presence:") == 3     # the definition, create, and the sign's evaluate
    sign = _sign_case(src)
    assert "flag(\"--presence\")" not in sign and "readPresence(keyPath)" in sign
    create = src[src.index('case "create":'):src.index('case "pubkey":')]
    assert 'flag("--presence")' in create and "writeMeta(out, presence: presence)" in create


def test_the_swift_retry_is_on_named_transient_codes_only_at_most_twice_more():
    sign = _sign_case(ses.SOURCE.read_text())
    assert "let transient: Set<Int> = [-36, -108, -2070]" in sign
    assert re.search(r"^let SIGN_MAX_ATTEMPTS = 3\b", ses.SOURCE.read_text(), re.M) is not None
    assert sign.count("attempts < SIGN_MAX_ATTEMPTS") == 2 and sign.count("try key.signature(for: u2fData)") == 1
    assert "transient.contains(c)" in sign
    assert "evaluateAccessControl" not in sign[sign.index("while attempts"):]      # no new evaluate inside the retry


def test_the_python_side_never_passes_presence_to_a_sign():
    import inspect
    assert "--presence" not in inspect.getsource(ses.sign_once)
    assert "presence" not in inspect.signature(ses.sign_once).parameters
    assert "presence" not in inspect.signature(ses.se_signer).parameters


def test_every_fail_after_the_evaluate_carries_evaluated_and_evaluations():
    # levain#191 F7: key_load, counter, sign_failed and sign_unverified all come after the evaluate; fail() merges the
    # context the sign sets right after it, so each of them says whether presence was evaluated
    src = ses.SOURCE.read_text()
    fail_fn = src[src.index("func fail("):src.index("// ---- SSH wire encoding")]
    assert "for (k, v) in failContext { o[k] = v }" in fail_fn
    assert src.index("var failContext") < src.index("func fail(")
    sign = _sign_case(src)
    ctx = sign.index('failContext = ["evaluated": evaluations > 0, "evaluations": evaluations]')
    assert sign.index("interactionNotAllowed = true") < ctx < sign.index("loadKey(")
    for code in ('"sign_failed"', '"sign_unverified"'):
        assert ctx < sign.index(code)
    assert ctx < sign.index("nextCounter(keyPath)")


def test_the_swift_create_never_unlinks_a_meta_file_it_did_not_create():
    # levain#191 F6: an open that fails (EEXIST) touches nothing; only a meta this call created and could not fill goes
    src = ses.SOURCE.read_text()
    wm = src[src.index("func writeMeta("):src.index("func readPresence(")]
    assert "if fd < 0 { return false }" in wm and "O_EXCL" in wm and "O_NOFOLLOW" in wm
    assert wm.count("unlink(") == 1 and "if n != d.count + 1 { unlink(metaPath(blob)); return false }" in wm
    create = src[src.index('case "create":'):src.index('case "pubkey":')]
    assert "unlink(metaPath(out))" not in create
    assert 'if lstat(metaPath(out), &mst) == 0 { fail("exists"' in create
    assert create.index("lstat(metaPath(out)") < create.index("SecureEnclave.P256.Signing.PrivateKey(accessControl:")
