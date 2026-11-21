"""levain#191 differential tests (Phill's signed np-1246f428 (a) "both"): the same payloads signed by the OLD path and by
the new levain-sign helper produce signatures every existing verifier treats identically.

Zero touch. The new path is the REAL helper on a no-presence key under tmp_path (no sheet can appear: the helper reads
``presence: false`` from the key's ``.meta.json``). The old path is a software emulation of what the Apple provider
signs through ssh-keygen (flags 0x21: user-presence + the resident-key bit, application ``ssh:``, sha512 SSHSIG,
counter), because the Apple provider needs a touch; the one REAL old-path signature on record (the 11-20 sigdiag-last
capture, a good non-empty one) is also compared structurally (its message is unknown: only its hash was logged, so it
cannot be cryptographically verified).

Verifiers compared: ``terminal_sign.verify`` (ctk injected), authority_sig ``verify_link_sig`` / ``presence_missing`` /
``sk_ecdsa_signature_empty`` (the broker's FIDO check calls the same ``presence_missing``), ``team.signing.has_presence``,
``ssh-keygen -Y check-novalidate`` and ``-Y verify`` with an allowed-signers line.

APPROVED DIFFERENCES (everything else in a verdict must be identical): the fingerprint and public key (two different
keys), the flags byte (old 0x21 vs helper 0x01: the resident-key bit 0x20), and the counter. The helper's no-presence
test key signs flags 0x00; the verifiers that require presence must REFUSE that, and accept the 0x01 signature made by
the TEST-ONLY build (``-D LEVAIN_SIGN_TESTONLY_FORCE_UP``: it exists only as a separately named binary built here; the
shipped helper has no such path)."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from levain import authority_sig as asig
from levain.cockpit import keys as ckeys
from levain.consent import se_signer as ses
from levain.consent import terminal_sign as ts
from levain.team import signing as team_signing

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the Secure Enclave helper is macOS only")

SSH_KEYGEN = "/usr/bin/ssh-keygen"
KT = "sk-ecdsa-sha2-nistp256@openssh.com"
APPROVED_DIFFERENCES = ("fingerprint", "public_key", "flags", "counter")

# The REAL old-path (Apple provider via ssh-keygen) good signature from the 11-20 sigdiag-last capture
# (project_memory/1120-58_l191/sigdiag-last.txt): namespace levain-diagnostic-125, flags 0x21, counter 1, 41-byte message
# of which only the sha512 was logged. Non-empty r and s.
OLD_REAL_SIG = (
    "-----BEGIN SSH SIGNATURE-----\n"
    "U1NIU0lHAAAAAQAAAH8AAAAic2stZWNkc2Etc2hhMi1uaXN0cDI1NkBvcGVuc3NoLmNvbQ\n"
    "AAAAhuaXN0cDI1NgAAAEEEi2ZjCBmtXEGT20tLO5gpIZUeCTZC55d93v5s03hyPSwYa5S4\n"
    "iIfXRMj9cC+6KDnKBN5KAd5x01vHOYql1e3BbQAAAARzc2g6AAAAFWxldmFpbi1kaWFnbm\n"
    "9zdGljLTEyNQAAAAAAAAAGc2hhNTEyAAAAdwAAACJzay1lY2RzYS1zaGEyLW5pc3RwMjU2\n"
    "QG9wZW5zc2guY29tAAAASAAAACB4wXWThnopZJn7DC0XyL+wtBh8W3xf8Ap30gSre3nRmA\n"
    "AAACBNMSPw8/efJ+2fUewX6ySlXlHFUycTmJ5Wm2mhhz5WkSEAAAAB\n"
    "-----END SSH SIGNATURE-----\n"
)


# ---------------------------------------------------------------------------------------------------
# the two paths


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _mp(n: int) -> bytes:
    b = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return _s(b"\x00" + b if b[0] & 0x80 else b)


class OldPath:
    """What the Apple provider + ssh-keygen sign: a software P-256 key laid out as an sk-ecdsa key (application ssh:),
    flags 0x21 by default."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec
        self.priv = ec.generate_private_key(ec.SECP256R1())
        pn = self.priv.public_key().public_numbers()
        x963 = b"\x04" + pn.x.to_bytes(32, "big") + pn.y.to_bytes(32, "big")
        blob = _s(KT.encode()) + _s(b"nistp256") + _s(x963) + _s(b"ssh:")
        self.pub = f"{KT} {base64.b64encode(blob).decode()} old-path"

    def sign(self, msg: bytes, ns: str, *, flags: int = 0x21, counter: int = 7) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        signed = b"SSHSIG" + _s(ns.encode()) + _s(b"") + _s(b"sha512") + _s(hashlib.sha512(msg).digest())
        u2f = hashlib.sha256(b"ssh:").digest() + bytes([flags]) + struct.pack(">I", counter) + hashlib.sha256(signed).digest()
        r, v = decode_dss_signature(self.priv.sign(u2f, ec.ECDSA(hashes.SHA256())))
        sig = _s(KT.encode()) + _s(_mp(r) + _mp(v)) + bytes([flags]) + struct.pack(">I", counter)
        blob = (b"SSHSIG" + struct.pack(">I", 1) + _s(base64.b64decode(self.pub.split()[1])) + _s(ns.encode()) + _s(b"")
                + _s(b"sha512") + _s(sig))
        b = base64.b64encode(blob).decode()
        return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b[i:i + 70] for i in range(0, len(b), 70)) \
            + "\n-----END SSH SIGNATURE-----\n"


@pytest.fixture(scope="module")
def real_helper(tmp_path_factory) -> Path:
    pytest.importorskip("cryptography")
    if not Path(ses.SWIFTC).exists():
        pytest.skip("needs swiftc")
    try:
        h = ses.build_helper(tmp_path_factory.mktemp("diffhelper"))
        ses.create_key(tmp_path_factory.mktemp("diffprobe") / "p.blob", presence=False, helper=h)
    except ses.SeSignerError as exc:
        pytest.skip(f"no usable helper or Secure Enclave: {exc}")
    return h


@pytest.fixture(scope="module")
def up_helper(tmp_path_factory, real_helper) -> Path:
    """TEST ONLY: the helper built with -D LEVAIN_SIGN_TESTONLY_FORCE_UP, which stamps flags 0x01 on a no-presence key.
    A separately named binary in a tmp cache; no product code can name it."""
    try:
        return ses.build_helper(tmp_path_factory.mktemp("uphelper"), defines=("LEVAIN_SIGN_TESTONLY_FORCE_UP",),
                                name_suffix="-testonly-up")
    except ses.SeSignerError as exc:
        pytest.skip(f"cannot build the test-only helper: {exc}")


@pytest.fixture(scope="module")
def blob(tmp_path_factory, real_helper) -> Path:
    b = tmp_path_factory.mktemp("diffkey") / "k.blob"
    ses.create_key(b, presence=False, helper=real_helper)
    return b


@pytest.fixture(scope="module")
def new_pub(blob, real_helper) -> str:
    return ses.public_key_line(blob, helper=real_helper)


@pytest.fixture(scope="module")
def old() -> OldPath:
    return OldPath()


def new_sig(blob: Path, helper: Path, msg: bytes, ns: str) -> str:
    return str(ses.sign_once(blob, msg, namespace=ns, helper=helper)["signature"])


# ---------------------------------------------------------------------------------------------------
# the verdict of every existing verifier on one (signature, message, namespace, key)


def _keygen(args: list[str], *, stdin: bytes, tmp: Path) -> tuple[int, str]:
    cp = subprocess.run([SSH_KEYGEN, *args], input=stdin, capture_output=True, timeout=30)
    return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", "replace")


def verdicts(sig: str, msg: bytes, ns: str, pub: str, tmp: Path) -> dict:
    fp = ckeys.fingerprint_of(pub)
    keyline = " ".join(pub.split()[:2])
    out: dict = {}
    tv = ts.verify(msg, sig, ts.signers_line(keyline), "phill", ctk=lambda pk: (True, "injected"), namespace=ns)
    d = dataclasses.asdict(tv)
    for k in APPROVED_DIFFERENCES:
        d.pop(k, None)
    for k, v in list(d.items()):                      # the fingerprint inside the claim is the key's, an approved difference
        if isinstance(v, str):
            d[k] = v.replace(fp, "<FP>")
    out["terminal_verify"] = d
    try:
        out["link_verify"] = asig.verify_link_sig(msg, sig, public_key=keyline, fingerprint=fp, namespace=ns)
    except asig.VerifierUnavailable:
        out["link_verify"] = "unavailable"
    out["presence_missing"] = asig.presence_missing(sig, keyline)       # the broker's FIDO check is this function
    out["has_presence"] = team_signing.has_presence(sig)
    out["empty"] = asig.sk_ecdsa_signature_empty(sig)
    allowed = tmp / "allowed"
    allowed.write_text(f"v@levain {keyline}\n")
    sigf = tmp / "sig"
    sigf.write_text(sig)
    rc, text = _keygen(["-Y", "verify", "-f", str(allowed), "-I", "v@levain", "-n", ns, "-s", str(sigf)], stdin=msg, tmp=tmp)
    out["keygen_verify"] = rc == 0
    rc, text = _keygen(["-Y", "check-novalidate", "-n", ns, "-s", str(sigf)], stdin=msg, tmp=tmp)
    out["keygen_check_novalidate"] = (rc == 0, "Good" in text)
    claimed_key, _claimed_fp, sig_type = asig.sshsig_public_key(sig)
    out["key_type"], out["sig_type"] = claimed_key.split()[0], sig_type
    return out


def flags_of(sig: str) -> int:
    return asig._sshsig_type_flags_counter(sig)[1]


PAYLOADS = [b"levain-terminal-answer/1\nanswer: yes\n", b"", b"\x00\xff\x80 binary \n", b"x" * 4096,
            "unicode ☃ answer".encode()]
NAMESPACES = [ts.NAMESPACE, ts.DESK_NAMESPACE]


@pytest.mark.parametrize("ns", NAMESPACES)
@pytest.mark.parametrize("msg", PAYLOADS, ids=["answer", "empty", "binary", "4k", "unicode"])
def test_old_path_and_helper_with_the_presence_flag_get_identical_verdicts(tmp_path, old, blob, up_helper, new_pub, msg, ns):
    o = old.sign(msg, ns)                                   # flags 0x21
    n = new_sig(blob, up_helper, msg, ns)                   # flags 0x01 from the TEST-ONLY build
    assert (flags_of(o), flags_of(n)) == (0x21, 0x01)       # the approved difference: the resident-key bit
    vo, vn = verdicts(o, msg, ns, old.pub, tmp_path), verdicts(n, msg, ns, new_pub, tmp_path)
    assert vo["terminal_verify"]["ok"] is True and vo["link_verify"] is True and vo["keygen_verify"] is True
    assert vo == vn


@pytest.mark.parametrize("ns", NAMESPACES)
def test_a_no_presence_signature_is_refused_by_exactly_the_verifiers_that_require_presence(tmp_path, old, blob, real_helper,
                                                                                            new_pub, ns):
    msg = b"levain-terminal-answer/1\nanswer: yes\n"
    n = new_sig(blob, real_helper, msg, ns)                 # the no-presence key: flags 0x00
    assert flags_of(n) == 0x00
    vn = verdicts(n, msg, ns, new_pub, tmp_path)
    # the presence rule refuses it everywhere it is enforced ...
    assert vn["terminal_verify"]["ok"] is False and "user-presence flag" in vn["terminal_verify"]["problem"]
    assert vn["link_verify"] is False and vn["presence_missing"] is True and vn["has_presence"] is False
    # ... and ssh-keygen, which does not enforce the flag, accepts it (measured, test_se_signer)
    assert vn["keygen_verify"] is True and vn["keygen_check_novalidate"] == (True, True)
    # an OLD-path signature with no presence (a no-touch key) gets the very same verdicts: the paths agree
    vo = verdicts(old.sign(msg, ns, flags=0x00), msg, ns, old.pub, tmp_path)
    assert vo == vn


@pytest.mark.parametrize("sign_ns", NAMESPACES + ["levain-authority-link"])
@pytest.mark.parametrize("verify_ns", NAMESPACES + ["levain-authority-link", "git"])
def test_namespace_handling_is_identical_signed_in_one_verified_in_another(tmp_path, old, blob, up_helper, new_pub,
                                                                           sign_ns, verify_ns):
    msg = b"levain namespace matrix"
    o, n = old.sign(msg, sign_ns), new_sig(blob, up_helper, msg, sign_ns)
    vo, vn = verdicts(o, msg, verify_ns, old.pub, tmp_path), verdicts(n, msg, verify_ns, new_pub, tmp_path)
    assert vo == vn
    accepted = sign_ns == verify_ns
    assert vo["keygen_verify"] is accepted
    if verify_ns in NAMESPACES:                              # terminal_sign.verify takes only its two namespaces
        assert vo["terminal_verify"]["ok"] is accepted
    else:
        assert vo["terminal_verify"]["ok"] is False and "namespace" in vo["terminal_verify"]["problem"]


def test_a_tampered_message_is_refused_by_the_same_verifiers_on_both_paths(tmp_path, old, blob, up_helper, new_pub):
    msg, bad = b"levain-terminal-answer/1\nanswer: yes\n", b"levain-terminal-answer/1\nanswer: no!\n"
    ns = ts.NAMESPACE
    o, n = old.sign(msg, ns), new_sig(blob, up_helper, msg, ns)
    vo, vn = verdicts(o, bad, ns, old.pub, tmp_path), verdicts(n, bad, ns, new_pub, tmp_path)
    assert vo == vn
    assert vo["terminal_verify"]["ok"] is False and vo["link_verify"] is False and vo["keygen_verify"] is False
    assert vo["keygen_check_novalidate"][0] is False


def test_another_keys_signers_line_refuses_both_paths_the_same(tmp_path, old, blob, up_helper, new_pub):
    msg, ns = b"m", ts.NAMESPACE
    stranger = OldPath()
    o, n = old.sign(msg, ns), new_sig(blob, up_helper, msg, ns)
    # verified against a key that did not sign: ssh-keygen -Y verify refuses both (check-novalidate has no key and still holds)
    for sig in (o, n):
        keyline = " ".join(stranger.pub.split()[:2])
        allowed = tmp_path / "allowed-stranger"
        allowed.write_text(f"v@levain {keyline}\n")
        sigf = tmp_path / "sig-s"
        sigf.write_text(sig)
        rc, _ = _keygen(["-Y", "verify", "-f", str(allowed), "-I", "v@levain", "-n", ns, "-s", str(sigf)], stdin=msg, tmp=tmp_path)
        assert rc != 0
        assert ts.verify(msg, sig, ts.signers_line(keyline), "phill", ctk=lambda pk: (True, "x")).ok is False


def _structure(sig: str) -> dict:
    """The wire layout of an armored sk-ecdsa SSHSIG, field by field (not the values that differ per key and per sign)."""
    lines = [ln for ln in sig.splitlines() if ln and not ln.startswith("-----")]
    blob = base64.b64decode("".join(lines))
    w = asig._Wire(blob)
    magic, version = w.take(6), w.u32()
    pub = asig._Wire(w.string())
    key_type, curve, point, app = pub.string(), pub.string(), pub.string(), pub.string()
    pub.end()
    ns, reserved, hashalg = w.string(), w.string(), w.string()
    inner = asig._Wire(w.string())
    w.end()
    sig_type, ecdsa = inner.string(), asig._Wire(inner.string())
    r, s = ecdsa.string(), ecdsa.string()
    ecdsa.end()
    flags, counter = inner.take(1)[0], inner.u32()
    inner.end()
    canonical = all(not (len(v) > 1 and v[0] == 0 and not v[1] & 0x80) and not v[0] & 0x80 for v in (r, s))
    return {"magic": magic, "version": version, "key_type": key_type, "curve": curve, "point_len": len(point),
            "point_prefix": point[:1], "app": app, "reserved": reserved, "hashalg": hashalg, "sig_type": sig_type,
            "r_nonempty": len(r) > 0, "s_nonempty": len(s) > 0, "mpint_canonical": canonical,
            "flags_has_up": bool(flags & 1), "counter_nonzero": counter > 0, "ns_len_positive": len(ns) > 0}


def test_the_real_old_path_signature_and_the_helpers_have_the_same_wire_structure(tmp_path, blob, up_helper, new_pub):
    real = _structure(OLD_REAL_SIG)
    mine = _structure(new_sig(blob, up_helper, b"41 bytes of a diagnostic message, more or less", "levain-diagnostic-125"))
    assert real == mine
    assert flags_of(OLD_REAL_SIG) == 0x21
    # and the structural verifiers read the real one exactly as they read the helper's (flags are the approved difference)
    keyline = asig.sshsig_public_key(OLD_REAL_SIG)[0]
    assert asig.presence_missing(OLD_REAL_SIG, keyline) is False and team_signing.has_presence(OLD_REAL_SIG) is True
    assert asig.sk_ecdsa_signature_empty(OLD_REAL_SIG) is False
    helper_sig = new_sig(blob, up_helper, b"m", "levain-diagnostic-125")
    assert asig.presence_missing(helper_sig, new_pub) is False and team_signing.has_presence(helper_sig) is True
    assert asig.sk_ecdsa_signature_empty(helper_sig) is False
    assert asig.sshsig_public_key(OLD_REAL_SIG)[2] == asig.sshsig_public_key(helper_sig)[2] == KT


def test_the_test_only_force_flag_exists_only_in_the_separately_built_binary(real_helper, up_helper):
    src = ses.SOURCE.read_text()
    assert src.count("LEVAIN_SIGN_TESTONLY_FORCE_UP") == 2          # the #if and its explanatory comment
    assert "#if LEVAIN_SIGN_TESTONLY_FORCE_UP" in src
    assert real_helper != up_helper and up_helper.name.endswith("-testonly-up")
    assert ses.default_cache_dir() not in up_helper.parents and ses.default_cache_dir() not in real_helper.parents
