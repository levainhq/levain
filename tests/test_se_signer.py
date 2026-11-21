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
    Path(str(blob) + ".pub").write_text(ses.public_key_line(blob, helper=helper) + "\n")   # se_signer verifies against it
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
    sig = ses.se_signer(key, namespace=NS, helper=helper)(msg)
    assert sig.startswith("-----BEGIN SSH SIGNATURE-----\n") and sig.endswith("-----END SSH SIGNATURE-----\n")
    line = ses.public_key_line(key, helper=helper)
    assert line.startswith("sk-ecdsa-sha2-nistp256@openssh.com ")
    assert _verify(tmp_path, line, sig, msg) == 0
    assert _parse(sig)[0] == 0x00


def test_tampered_message_fails(tmp_path, helper, key):
    sig = ses.se_signer(key, namespace=NS, helper=helper)(b"original")
    line = ses.public_key_line(key, helper=helper)
    assert _verify(tmp_path, line, sig, b"original") == 0
    assert _verify(tmp_path, line, sig, b"0riginal") != 0


def test_wrong_namespace_fails(tmp_path, helper, key):
    sig = ses.se_signer(key, namespace=NS, helper=helper)(b"m")
    line = ses.public_key_line(key, helper=helper)
    assert _verify(tmp_path, line, sig, b"m", namespace="levain-other") != 0


def test_counter_increments(helper, key):
    sign = ses.se_signer(key, namespace=NS, helper=helper)
    c1 = _parse(sign(b"a"))[1]
    c2 = _parse(sign(b"a"))[1]
    assert c2 == c1 + 1
    counter = Path(str(key) + ".counter")
    assert counter.stat().st_mode & 0o777 == 0o600


def test_presence_flag_is_not_enforced_by_ssh_keygen_verify(tmp_path, helper, key):
    """The brief expected ssh-keygen to REJECT a flags-0x00 signature without no-touch-required. Measured: it does not
    (and it has no option that would). The flag is only honest if the Levain verifier reads it; this pins the finding."""
    sig = ses.se_signer(key, namespace=NS, helper=helper)(b"m")
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


# ---------------------------------------------------------------------------------------------------
# levain#191 review round 1: the key's presence comes from <blob>.meta.json, never from a flag; status is matched on the
# one measured error; the helper is built outside any sign


def test_create_writes_the_meta_file_the_helper_reads_the_presence_from(tmp_path, helper, key):
    import json
    meta = Path(str(key) + ".meta.json")
    body = json.loads(meta.read_text())
    assert body["format"] == "levain-sign-key/1" and body["presence"] is False and body["created"].endswith("Z")
    assert meta.stat().st_mode & 0o777 == 0o600


def test_a_sign_with_no_meta_file_is_refused_not_guessed(tmp_path, helper, key):
    Path(str(key) + ".meta.json").unlink()
    with pytest.raises(ses.SeSignerError) as ei:
        ses.sign_once(key, b"m", namespace=NS, helper=helper)
    assert ei.value.code == "meta_missing"
    Path(str(key) + ".meta.json").write_text('{"format": "other", "presence": true}')
    with pytest.raises(ses.SeSignerError) as ei:
        ses.sign_once(key, b"m", namespace=NS, helper=helper)
    assert ei.value.code == "meta_bad"


def test_a_presence_flag_on_the_command_line_of_a_sign_is_ignored(tmp_path, helper, key):
    """The old CLI took ``--presence`` on sign; the helper now reads the key's meta. A no-presence key stays flags 0x00,
    evaluations 0, whatever the command line says (so no sheet can be asked for by a caller's argument)."""
    out = ses._run(helper, ["sign", "--key", str(key), "--application", "ssh:", "--namespace", NS, "--reason", "r",
                            "--wall-ms", "5000", "--presence"], stdin=b"m", timeout=30)
    assert out["flags"] == 0 and out["evaluations"] == 0 and out["evaluated"] is False and out["presence"] is False
    assert _parse(out["signature"])[0] == 0x00


@pytest.mark.parametrize("wall", [None, "soon", ""])
def test_the_real_helper_refuses_a_sign_without_a_whole_number_wall_ms(tmp_path, helper, key, wall):
    """levain#191 F1: --wall-ms is required on sign (no-presence key: no sheet either way)."""
    args = ["sign", "--key", str(key), "--application", "ssh:", "--namespace", NS, "--reason", "r"]
    if wall is not None:
        args += ["--wall-ms", wall]
    with pytest.raises(ses.SeSignerError) as ei:
        ses._run(helper, args, stdin=b"m", timeout=30)
    assert ei.value.code == "usage" and "--wall-ms" in str(ei.value)


def test_the_helper_says_how_many_presence_evaluations_it_made(tmp_path, helper, key):
    out = ses.sign_once(key, b"m", namespace=NS, helper=helper)
    assert out["evaluations"] == 0 and out["attempts"] == 1


def test_status_of_a_presence_key_is_true_on_the_measured_error_and_shows_no_sheet(tmp_path, helper):
    """MEASURED 2026-11-21 (macOS 27): `create --out K --presence` shows no sheet; `status --key K` (interactionNotAllowed)
    shows none and the sign raises NSError domain com.apple.LocalAuthentication code -1004 (LAError.notInteractive).
    A no-presence key signs, so it is False. The helper matches that (domain, code) as numbers."""
    k = tmp_path / "gated.blob"
    try:
        ses.create_key(k, presence=True, helper=helper)
    except ses.SeSignerError as exc:
        pytest.skip(f"this Mac cannot make a biometry key here: {exc}")
    assert Path(str(k) + ".meta.json").read_text().count('"presence":true') == 1
    assert ses.key_status(k, helper=helper, timeout=20) == (True, "presence-gated Secure Enclave key on this Mac")


def test_the_status_match_is_on_the_exact_measured_code_not_a_string_search():
    src = ses.SOURCE.read_text()
    start = src.index("func needsInteraction")
    body = src[start:src.index("guard let blob = FileManager", start)]
    assert "LAError.errorDomain" in body and "LAError.notInteractive.rawValue" in body
    assert "contains(" not in body and "-25308" not in body
    assert "MEASURED 2026-11-21" in src and "-1004" in src


def test_require_helper_never_compiles_and_names_the_command_that_builds_it(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a compile was started")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ses.SeSignerError) as ei:
        ses.require_helper(tmp_path / "empty-cache")
    assert ei.value.code == "helper_not_built" and "levain doctor --fix" in str(ei.value)
    (tmp_path / "stale").mkdir()
    (tmp_path / "stale" / "levain-sign-0000000000000000").write_text("#!/bin/sh\n")
    (tmp_path / "stale" / "levain-sign-0000000000000000").chmod(0o755)       # built from some other source: stale
    with pytest.raises(ses.SeSignerError):
        ses.require_helper(tmp_path / "stale")


def test_require_helper_finds_what_ensure_helper_built(tmp_path, helper):
    cache = tmp_path / "cache"
    built = ses.ensure_helper(cache)
    assert ses.require_helper(cache) == built and ses.helper_path(cache) == built


def test_sign_once_passes_a_presence_wall_below_its_own_subprocess_timeout(tmp_path, monkeypatch):
    # levain#191 F1: the helper's own presence_timeout must fire before Python kills it mid-sheet
    seen: dict = {}

    def fake(cmd, **kw):
        seen["cmd"], seen["timeout"] = list(cmd), kw["timeout"]
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"ok": true, "signature": "x"}', stderr=b"")
    monkeypatch.setattr(ses.subprocess, "run", fake)
    for timeout in (ses.SIGN_TIMEOUT, 18.0, 5.0, 3.5):
        ses.sign_once(tmp_path / "k", b"m", namespace=NS, helper=tmp_path / "h", timeout=timeout)
        wall_ms = int(seen["cmd"][seen["cmd"].index("--wall-ms") + 1])
        assert seen["timeout"] == timeout
        assert wall_ms == int((timeout - ses.WALL_MARGIN_S) * 1000) and wall_ms / 1000 < timeout
    ses.sign_once(tmp_path / "k", b"m", namespace=NS, helper=tmp_path / "h", timeout=2.5)
    assert int(seen["cmd"][seen["cmd"].index("--wall-ms") + 1]) == int(ses.WALL_FLOOR_S * 1000)     # the floor


def test_the_terminal_sign_bound_gives_the_levain_sign_helper_a_wall_inside_its_timeout(tmp_path, monkeypatch):
    # levain#191 F1, through keys' levain-sign signer at the terminal sign's own bound (20 s): the helper is given
    # bound less two verify reserves, and its wall is under that
    from levain.cockpit import keys as ckeys
    from levain.consent import terminal_sign as ts
    seen: dict = {}

    def fake_run(helper, args, *, stdin=b"", timeout):
        seen["args"], seen["timeout"] = list(args), timeout
        raise ses.SeSignerError("stub refusal", "stub")
    monkeypatch.setattr(ses, "_run", fake_run)
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    sign = ckeys._levain_sign_signer(d / "k", "sk-ecdsa-sha2-nistp256@openssh.com AAAA", "SHA256:x", NS,
                                     ts.SIGN_BOUND_S, SSH_KEYGEN, tmp_path / "h")
    with pytest.raises(ckeys.KeyError_):
        sign(b"m")
    wall = int(seen["args"][seen["args"].index("--wall-ms") + 1]) / 1000
    assert seen["timeout"] <= ts.SIGN_BOUND_S - 2 * ckeys._VERIFY_RESERVE_S
    assert wall < seen["timeout"] and wall <= seen["timeout"] - ses.WALL_MARGIN_S + 0.001
