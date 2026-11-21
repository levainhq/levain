"""levain#191 prototype: the Secure Enclave signer's SSH signatures verify under ``ssh-keygen -Y verify``.

Zero touch: every key made here has access control ``.privateKeyUsage`` only (no ``--presence``), so no sheet can appear.
The whole module skips unless this is a Mac with swiftc and a Secure Enclave (probed by making a key)."""
from __future__ import annotations

import base64
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from levain.consent import se_signer as ses

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Secure Enclave signer is macOS only")

NS = "levain-r191-probe"
SSH_KEYGEN = "/usr/bin/ssh-keygen"


@pytest.fixture(scope="module")
def helper(tmp_path_factory) -> Path:
    if not Path(ses.SWIFTC).exists():
        pytest.skip("no swiftc")
    try:
        h = ses.build_helper(tmp_path_factory.mktemp("helper"))
    except ses.SeSignerError as exc:
        pytest.skip(f"cannot build the helper: {exc}")
    probe = tmp_path_factory.mktemp("probe") / "probe.blob"
    try:
        ses.create_key(probe, presence=False, helper=h)
    except ses.SeSignerError as exc:
        pytest.skip(f"no usable Secure Enclave: {exc}")
    return h


@pytest.fixture()
def key(tmp_path, helper):
    blob = tmp_path / "k.blob"
    ses.create_key(blob, presence=False, helper=helper)
    return blob


def _verify(tmp_path: Path, pub_line: str, sig: str, message: bytes, *, namespace: str = NS, touch_optional: bool = True) -> int:
    # Measured on this Mac (OpenSSH_10.3p1): no-touch-required is NOT an allowed_signers option here ("bad options") and
    # `-Y verify -O` takes only print-pubkey / verify-time / hashalg ("Invalid option"): ssh-keygen -Y verify does not
    # enforce the user-presence flag at all, so touch_optional only documents the intent and adds no option.
    allowed = tmp_path / "allowed"
    allowed.write_text(f'probe@levain namespaces="{NS}" {pub_line}\n')
    sigf = tmp_path / "sig"
    sigf.write_text(sig)
    proc = subprocess.run([SSH_KEYGEN, "-Y", "verify", "-f", str(allowed), "-I", "probe@levain", "-n", namespace,
                           "-s", str(sigf)], input=message, capture_output=True, timeout=30)
    return proc.returncode


def _parse(sig: str) -> tuple[int, int]:
    """(flags, counter) from the armored signature's inner sk-ecdsa signature."""
    lines = [ln for ln in sig.splitlines() if ln and not ln.startswith("-----")]
    blob = base64.b64decode("".join(lines))
    assert blob[:6] == b"SSHSIG"
    pos = 10

    def string() -> bytes:
        nonlocal pos
        (n,) = struct.unpack(">I", blob[pos:pos + 4])
        out = blob[pos + 4:pos + 4 + n]
        pos += 4 + n
        return out

    for _ in range(5):   # publickey, namespace, reserved, hash_algorithm, signature
        field = string()
    inner = field
    (n,) = struct.unpack(">I", inner[:4])
    assert inner[4:4 + n] == b"sk-ecdsa-sha2-nistp256@openssh.com"
    p = 4 + n
    (m,) = struct.unpack(">I", inner[p:p + 4])
    p += 4 + m   # skip ecdsa_signature
    flags = inner[p]
    (counter,) = struct.unpack(">I", inner[p + 1:p + 5])
    assert p + 5 == len(inner)
    return flags, counter


def test_imports_from_this_checkout():
    assert Path(ses.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]


def test_signature_verifies(tmp_path, helper, key):
    msg = b"levain consent answer \x00\xff bytes"
    sig = ses.se_signer(key, presence=False, namespace=NS, helper=helper)(msg)
    assert sig.startswith("-----BEGIN SSH SIGNATURE-----\n") and sig.endswith("-----END SSH SIGNATURE-----\n")
    line = ses.public_key_line(key, helper=helper)
    assert line.startswith("sk-ecdsa-sha2-nistp256@openssh.com ")
    assert _verify(tmp_path, line, sig, msg) == 0
    assert _parse(sig)[0] == 0x00


def test_tampered_message_fails(tmp_path, helper, key):
    sig = ses.se_signer(key, presence=False, namespace=NS, helper=helper)(b"original")
    line = ses.public_key_line(key, helper=helper)
    assert _verify(tmp_path, line, sig, b"original") == 0
    assert _verify(tmp_path, line, sig, b"0riginal") != 0


def test_wrong_namespace_fails(tmp_path, helper, key):
    sig = ses.se_signer(key, presence=False, namespace=NS, helper=helper)(b"m")
    line = ses.public_key_line(key, helper=helper)
    assert _verify(tmp_path, line, sig, b"m", namespace="levain-other") != 0


def test_counter_increments(helper, key):
    sign = ses.se_signer(key, presence=False, namespace=NS, helper=helper)
    c1 = _parse(sign(b"a"))[1]
    c2 = _parse(sign(b"a"))[1]
    assert c2 == c1 + 1
    counter = Path(str(key) + ".counter")
    assert counter.stat().st_mode & 0o777 == 0o600


def test_presence_flag_is_not_enforced_by_ssh_keygen_verify(tmp_path, helper, key):
    """The brief expected ssh-keygen to REJECT a flags-0x00 signature without no-touch-required. Measured: it does not
    (and it has no option that would). The flag is only honest if the Levain verifier reads it; this pins the finding."""
    sig = ses.se_signer(key, presence=False, namespace=NS, helper=helper)(b"m")
    line = ses.public_key_line(key, helper=helper)
    assert _parse(sig)[0] == 0x00
    assert _verify(tmp_path, line, sig, b"m", touch_optional=False) == 0


def test_create_refuses_overwrite_and_is_0600(helper, key):
    assert key.stat().st_mode & 0o777 == 0o600
    before = key.read_bytes()
    with pytest.raises(ses.SeSignerError) as ei:
        ses.create_key(key, presence=False, helper=helper)
    assert ei.value.code == "exists"
    assert key.read_bytes() == before
