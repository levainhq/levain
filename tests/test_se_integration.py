"""levain#191: the Secure Enclave helper (levain-sign) wired into the terminal key's custody, signer, status and creator.

Zero touch. Every key made here is a no-presence key under tmp_path (never ``--presence``), the stub helper stands in
wherever a presence signature is needed (it is software, so it can set the presence flag without a sheet), and nothing
under ``~/.levain`` is read. The tests that need the real helper skip unless this is a Mac with swiftc and a Secure
Enclave. The reproduced failure is the 11-20 empty signature (``EMPTY_SIG``, copied from a ``sigfail-*.json`` record):
it must never come back from the levain-sign path as a signature."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from levain.cockpit import custody
from levain.cockpit import keys as ckeys
from levain.cockpit.keys import KeyError_
from levain.consent import se_signer as ses
from levain.consent import terminal_sign as ts

STUB_SOURCE = r'''
import hashlib, json, os, struct, sys, time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

argv = sys.argv[1:]
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
def s(b): return struct.pack(">I", len(b)) + b
def mp(b):
    b = b.lstrip(b"\0")
    if b and b[0] & 0x80: b = b"\0" + b
    return s(b)
def sha(b): return hashlib.sha256(b).digest()
def out(o, code=0):
    sys.stdout.write(json.dumps(o) + "\n"); sys.exit(code)
def side(key, suffix):
    p = key + suffix
    return open(p).read().strip() if os.path.exists(p) else None

sub, key = argv[0], opt("--key")
with open(key + ".calls", "a") as fh:
    fh.write(" ".join(argv) + "\n")
if sub == "status":
    mode = side(key, ".status")
    if mode == "gated": out({"ok": True, "presence_gated": True})
    if mode == "open": out({"ok": True, "presence_gated": False})
    if mode == "gone": out({"ok": False, "code": "key_load", "error": "not this Mac's"}, 1)
    out({"ok": False, "code": "weird", "error": "?"}, 1)
if sub != "sign":
    out({"ok": False, "code": "usage", "error": "stub"}, 1)
msg = sys.stdin.buffer.read()
if not (opt("--wall-ms") or "").isdigit():
    out({"ok": False, "code": "usage", "error": "missing --wall-ms"}, 1)
if side(key, ".sleep"): time.sleep(float(side(key, ".sleep")))
if side(key, ".replay"):
    sys.stdout.write(side(key, ".replay") + "\n"); sys.exit(0)
if side(key, ".fail"):
    o = {"ok": False, "code": side(key, ".fail"), "error": "stub refusal", "attempts": 3, "evaluated": False}
    o.update(json.loads(side(key, ".fail-extra") or "{}"))
    out(o, 1)
priv = serialization.load_pem_private_key(open(key, "rb").read(), None)
pt = priv.public_key().public_numbers()
x963 = b"\x04" + pt.x.to_bytes(32, "big") + pt.y.to_bytes(32, "big")
KT = b"sk-ecdsa-sha2-nistp256@openssh.com"
app, ns = opt("--application").encode(), opt("--namespace").encode()
meta = side(key, ".meta.json")
if meta is None:
    out({"ok": False, "code": "meta_missing", "error": "stub: no meta"}, 1)
presence = bool(json.loads(meta)["presence"])
flags = 1 if presence else 0
counter = int(side(key, ".counter-value") or 1)
signed = b"SSHSIG" + s(ns) + s(b"") + s(b"sha512") + s(hashlib.sha512(msg).digest())
u2f = sha(app) + bytes([flags]) + struct.pack(">I", counter) + sha(signed)
r, sv = decode_dss_signature(priv.sign(u2f, ec.ECDSA(hashes.SHA256())))
inner = mp(r.to_bytes(32, "big")) + mp(sv.to_bytes(32, "big"))
pubblob = s(KT) + s(b"nistp256") + s(x963) + s(app)
sigfield = s(KT) + s(inner) + bytes([flags]) + struct.pack(">I", counter)
blob = b"SSHSIG" + struct.pack(">I", 1) + s(pubblob) + s(ns) + s(b"") + s(b"sha512") + s(sigfield)
import base64
b64 = base64.b64encode(blob).decode()
armored = "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76)) + "\n-----END SSH SIGNATURE-----\n"
out({"ok": True, "signature": armored, "counter": counter, "flags": flags, "attempts": int(side(key, ".attempts") or 1),
     "evaluated": presence, "evaluations": int(side(key, ".evaluations") or (1 if presence else 0)),
     "presence": presence})
'''

# One of the 11-20 field records (sigfail-20261119T155850636576-0000030967-1.json, reason "empty ECDSA signature"):
# flags 0x21 and an sk-ecdsa signature whose r and s have zero length.
EMPTY_SIG = (
    "-----BEGIN SSH SIGNATURE-----\n"
    "U1NIU0lHAAAAAQAAAH8AAAAic2stZWNkc2Etc2hhMi1uaXN0cDI1NkBvcGVuc3NoLmNvbQ\n"
    "AAAAhuaXN0cDI1NgAAAEEEi2ZjCBmtXEGT20tLO5gpIZUeCTZC55d93v5s03hyPSwYa5S4\n"
    "iIfXRMj9cC+6KDnKBN5KAd5x01vHOYql1e3BbQAAAARzc2g6AAAAFmxldmFpbi10ZXJtaW\n"
    "5hbC1hbnN3ZXIAAAAAAAAABnNoYTUxMgAAADcAAAAic2stZWNkc2Etc2hhMi1uaXN0cDI1\n"
    "NkBvcGVuc3NoLmNvbQAAAAgAAAAAAAAAACEAAAAB\n"
    "-----END SSH SIGNATURE-----\n"
)


def _empty_sig() -> str:
    return EMPTY_SIG


NS = ts.NAMESPACE
APPLE = custody.APPLE_PROVIDER
LA_CANCEL = {"error_domain": "com.apple.LocalAuthentication", "error_code": -2}      # LAError.userCancel


# ---------------------------------------------------------------------------------------------------
# fixtures


def _keydir(tmp_path: Path, name: str = "keys") -> Path:
    d = tmp_path / name
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    return d


def _sidecar_body(**over) -> dict:
    body = {"format": custody.SIDECAR_FORMAT, "custody": "touch-id", "provider": custody.LEVAIN_SIGN,
            "label": "levain-terminal", "signer": custody.SIGNER_FORMAT}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def _write_sidecar(key: Path, **over) -> None:
    custody.sidecar_path(key).write_text(json.dumps(_sidecar_body(**over)))


@pytest.fixture(scope="module")
def stub_helper(tmp_path_factory) -> Path:
    pytest.importorskip("cryptography")
    d = tmp_path_factory.mktemp("stubhelper")
    p = d / "stub-helper"
    p.write_text(f"#!{sys.executable}\n" + STUB_SOURCE)
    p.chmod(0o755)
    return p


def _soft_key(d: Path, stub_helper_unused=None, label: str = "levain-terminal", presence: bool = True,
              **sidecar_over) -> Path:
    """A software P-256 key standing in for the helper's blob: the PEM is the blob, the .pub the sk-ecdsa line."""
    import base64
    import struct

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    key = d / label
    key.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    key.chmod(0o600)
    pn = priv.public_key().public_numbers()
    x963 = b"\x04" + pn.x.to_bytes(32, "big") + pn.y.to_bytes(32, "big")

    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    kt = "sk-ecdsa-sha2-nistp256@openssh.com"
    line = f"{kt} {base64.b64encode(s(kt.encode()) + s(b'nistp256') + s(x963) + s(b'ssh:')).decode()} {label}\n"
    (d / (label + ".pub")).write_text(line)
    _write_sidecar(key, **sidecar_over)
    if sidecar_over.get("provider", custody.LEVAIN_SIGN) == custody.LEVAIN_SIGN:     # an Apple key has no meta file
        Path(str(key) + ".meta.json").write_text(json.dumps({"format": "levain-sign-key/1", "presence": presence,
                                                             "created": "2026-11-21T00:00:00Z"}))
    return key


def _stub_log(key: Path) -> list[str]:
    p = Path(str(key) + ".calls")
    return p.read_text().splitlines() if p.exists() else []


def _set(key: Path, suffix: str, text: str) -> None:
    Path(str(key) + suffix).write_text(text)


def _signers(key: Path) -> str:
    return ts.signers_line(" ".join(Path(str(key) + ".pub").read_text().split()[:2]))


def _log(d: Path) -> list[dict]:
    p = d / ses.SIGN_LOG
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


# ---------------------------------------------------------------------------------------------------
# R1: the sidecar accept / refuse matrix

OLD = {"format": custody.SIDECAR_FORMAT, "custody": "touch-id", "provider": APPLE, "ctk_hash": "A" * 40, "label": "k"}


def _read(tmp_path: Path, body: dict):
    key = tmp_path / "k"
    custody.sidecar_path(key).write_text(json.dumps(body))
    return custody.read_sidecar(key)


def test_sidecar_accepts_exactly_three_shapes(tmp_path):
    old = _read(tmp_path, OLD)
    assert (old.custody, old.provider, old.ctk_hash, old.signer) == ("touch-id", APPLE, "A" * 40, None)
    new = _read(tmp_path, _sidecar_body())
    assert (new.custody, new.provider, new.ctk_hash, new.signer) == ("touch-id", "levain-sign", None, "levain-sign/1")
    pw = _read(tmp_path, {"format": custody.SIDECAR_FORMAT, "custody": "passphrase", "label": "k"})
    assert pw.custody == "passphrase" and pw.provider is None and pw.signer is None


@pytest.mark.parametrize("body", [
    _sidecar_body(ctk_hash="A" * 40),                                   # helper key naming a CTK identity
    _sidecar_body(signer=None),                                         # helper key with no signer format
    _sidecar_body(signer="levain-sign/2"),                              # an unknown signer format
    _sidecar_body(custody="passphrase"),                                # a passphrase key claiming the helper
    {**OLD, "signer": "levain-sign/1"},                                 # an Apple key claiming a signer
    {**OLD, "provider": "/tmp/evil.dylib"},                             # a library levain does not load
    {**OLD, "provider": None},                                          # touch-id with no provider
    {**OLD, "ctk_hash": None},                                          # Apple provider without its CTK hash
    {**OLD, "ctk_hash": "zz"},
    {"format": custody.SIDECAR_FORMAT, "custody": "passphrase", "signer": "levain-sign/1"},
    {"format": custody.SIDECAR_FORMAT, "custody": "secure-enclave", "provider": "levain-sign",
     "signer": "levain-sign/1"},
])
def test_sidecar_refuses_everything_else(tmp_path, body):
    with pytest.raises(KeyError_):
        _read(tmp_path, body)


def test_a_levain_sign_name_is_never_a_library_ssh_keygen_loads():
    assert custody.LEVAIN_SIGN not in custody._PROVIDERS


# ---------------------------------------------------------------------------------------------------
# R2: the header readers decide by the sidecar first and never parse a blob


def _openssh_plain_key(key: Path) -> None:
    subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "x", "-f", str(key)], check=True)


def test_header_readers_never_parse_a_levain_sign_blob(tmp_path):
    key = tmp_path / "blob"
    _openssh_plain_key(key)                       # an unencrypted OpenSSH key: the header parses when no sidecar says otherwise
    assert custody._private_header(key) is not None and custody.is_encrypted_file(key) is False
    _write_sidecar(key)                           # now the sidecar says this file is a levain-sign blob
    assert custody._private_header(key) is None
    assert custody.is_encrypted_file(key) is False
    pub = (tmp_path / "blob.pub").read_text().strip()
    why = custody.passphrase_problem(key, pub)
    assert why is not None and "not an OpenSSH-format private key" in why


def test_the_blob_is_not_even_opened(tmp_path, monkeypatch):
    key = tmp_path / "blob"
    key.write_bytes(b"\x00\x01 not ascii \xff" * 10)
    _write_sidecar(key)
    real_open = Path.open

    def guarded(self, *a, **kw):
        assert self != key, "the levain-sign blob was opened by a header reader"
        return real_open(self, *a, **kw)
    monkeypatch.setattr(Path, "open", guarded)
    assert custody._private_header(key) is None
    assert custody.is_encrypted_file(key) is False


def test_a_malformed_sidecar_is_not_a_helper_key_for_the_header_readers(tmp_path):
    key = tmp_path / "blob"
    _openssh_plain_key(key)
    custody.sidecar_path(key).write_text("{not json")
    assert custody._private_header(key) is not None      # read_sidecar is the refuser, not the header reader


# ---------------------------------------------------------------------------------------------------
# R3 / R4: signing through keys.ssh_signer


def _signer(key: Path, stub: Path, **kw):
    return ckeys.ssh_signer(key, namespace=kw.pop("namespace", NS), helper=stub, **kw)


def test_a_levain_sign_key_signs_through_ssh_signer_and_verify_accepts_it(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    fp, sign = _signer(key, stub_helper)
    msg = b"levain-terminal-answer/1\nanswer: yes\n"
    sig = sign(msg)
    res = ts.verify(msg, sig, _signers(key), "phill", ctk=lambda pk: (True, "stub"))
    assert res.ok and res.fingerprint == fp and res.user_present is True
    calls = _stub_log(key)
    assert len(calls) == 1 and "--presence" not in calls[0] and f"--namespace {NS}" in calls[0]


def test_the_desk_namespace_is_passed_as_given(tmp_path, stub_helper):
    key = _soft_key(_keydir(tmp_path))
    _fp, sign = _signer(key, stub_helper, namespace=ts.DESK_NAMESPACE)
    msg = b"desk"
    sig = sign(msg)
    assert ts.verify(msg, sig, _signers(key), "phill", namespace=ts.DESK_NAMESPACE, ctk=lambda pk: (True, "s")).ok


def test_the_pub_path_signs_the_same_blob(tmp_path, stub_helper):
    key = _soft_key(_keydir(tmp_path))
    _fp, sign = _signer(Path(str(key) + ".pub"), stub_helper)
    assert sign(b"m").startswith("-----BEGIN SSH SIGNATURE-----")
    assert len(_stub_log(key)) == 1


def test_one_sheet_per_sign_no_python_retry(tmp_path, stub_helper):
    key = _soft_key(_keydir(tmp_path))
    _set(key, ".fail", "presence_denied")
    _set(key, ".fail-extra", json.dumps(LA_CANCEL))
    _fp, sign = _signer(key, stub_helper)
    with pytest.raises(KeyError_) as ei:
        sign(b"m")
    assert len(_stub_log(key)) == 1                       # the helper was asked once; no second sheet
    assert ts._classify(ei.value)[0] == "cancelled"
    assert "nothing was signed" in str(ei.value)


def test_helper_failures_keep_the_classifier_working(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _fp, sign = _signer(key, stub_helper, timeout=4.0)     # about 2 s for the helper after the two verify reserves
    _set(key, ".sleep", "6")
    with pytest.raises(KeyError_) as ei:
        sign(b"m")
    assert ts._classify(ei.value)[0] == "timeout"
    Path(str(key) + ".sleep").unlink()
    _set(key, ".fail", "key_load")
    with pytest.raises(KeyError_) as ei:
        sign(b"m")
    assert "not this Mac's" in str(ei.value) and ts._classify(ei.value)[0] == "signer-error"
    Path(str(key) + ".fail").unlink()
    _fp, sign_missing = ckeys.ssh_signer(key, namespace=NS, helper=tmp_path / "no-such-helper")
    with pytest.raises(KeyError_) as ei:
        sign_missing(b"m")
    assert "helper" in str(ei.value) and "nothing was signed" in str(ei.value)
    assert [r["outcome"] for r in _log(d)] == ["timeout", "key-not-here", "helper-error"]


def test_a_missing_swiftc_is_a_plain_refusal_naming_xcode_select(tmp_path, monkeypatch):
    monkeypatch.setattr(ses, "SWIFTC", str(tmp_path / "no-swiftc"))
    with pytest.raises(ses.SeSignerError, match="xcode-select --install"):
        ses.ensure_helper(tmp_path / "cache")


def test_the_sign_log_line_shape(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _set(key, ".attempts", "3")                           # the helper retried silently twice inside its one sheet
    _fp, sign = _signer(key, stub_helper)
    msg = b"never in the log"
    sig = sign(msg)
    rows = _log(d)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"time", "outcome", "attempts", "silent_retries", "elapsed_s", "evaluated", "evaluations", "code",
                      "pid"}
    assert (row["outcome"], row["attempts"], row["silent_retries"], row["evaluated"], row["code"], row["pid"]) \
        == ("signed", 3, 2, True, "ok", os.getpid())
    assert row["elapsed_s"] >= 0 and row["time"].endswith("+00:00")
    raw = (d / ses.SIGN_LOG).read_text()
    assert "never in the log" not in raw and "BEGIN SSH SIGNATURE" not in raw and sig.splitlines()[1] not in raw
    assert stat.S_IMODE(os.stat(d / ses.SIGN_LOG).st_mode) == 0o600


def test_the_sign_log_is_pruned_to_its_newest_500(tmp_path):
    d = _keydir(tmp_path)
    for i in range(520):
        assert ses.append_sign_log(d, ses.sign_log_record(outcome=f"n{i}", attempts=1, evaluated=True, elapsed_s=0.1,
                                                          code="ok")) is None
    rows = _log(d)
    assert len(rows) == ses.SIGN_LOG_KEEP == 500
    assert rows[0]["outcome"] == "n20" and rows[-1]["outcome"] == "n519"
    assert stat.S_IMODE(os.stat(d / ses.SIGN_LOG).st_mode) == 0o600


def test_a_failed_sign_log_write_never_fails_the_sign(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    (d / ses.SIGN_LOG).mkdir()                            # the log path is a directory: the write cannot happen
    _fp, sign = _signer(key, stub_helper)
    assert sign(b"m").startswith("-----BEGIN SSH SIGNATURE-----")


# --- the reproduced failure: the 11-20 empty signature can never be returned -------------------------


@pytest.mark.parametrize("replayed", [_empty_sig(), "", "  \n", _empty_sig().replace("\n", "")])
def test_the_11_20_empty_signature_is_never_returned(tmp_path, stub_helper, replayed):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _set(key, ".replay", json.dumps({"ok": True, "signature": replayed, "attempts": 1, "evaluated": True}))
    _fp, sign = _signer(key, stub_helper)
    returned = []
    with pytest.raises(KeyError_) as ei:
        returned.append(sign(b"levain-terminal-answer/1\n"))
    assert returned == []                                 # nothing came back as a signature
    assert "nothing was signed" in str(ei.value)
    assert len(_stub_log(key)) == 1                       # and it was not asked for again
    row = _log(d)[-1]
    assert row["outcome"] in ("no-signature", "unverified") and row["code"] != "ok"


def test_the_empty_record_is_the_empty_kind_and_a_real_sig_for_another_key_is_unverified(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key, other = _soft_key(d), _soft_key(d, label="other")
    _fp, sign = _signer(other, stub_helper)
    foreign = sign(b"m")                                  # a good signature, but by another key
    _set(key, ".replay", json.dumps({"ok": True, "signature": foreign, "attempts": 1, "evaluated": True}))
    _fp, sign2 = _signer(key, stub_helper)
    with pytest.raises(KeyError_, match="does not verify"):
        sign2(b"m")
    assert _log(d)[-1]["outcome"] == "unverified"
    _set(key, ".replay", json.dumps({"ok": True, "signature": _empty_sig(), "attempts": 1, "evaluated": True}))
    with pytest.raises(KeyError_, match="no signature"):
        sign2(b"m")
    assert _log(d)[-1]["code"] == "empty-signature"


# --- the real helper, no presence ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_helper(tmp_path_factory) -> Path:
    if sys.platform != "darwin" or not Path(ses.SWIFTC).exists():
        pytest.skip("needs a Mac with swiftc")
    try:
        h = ses.build_helper(tmp_path_factory.mktemp("realhelper"))
        probe = tmp_path_factory.mktemp("probe") / "p.blob"
        ses.create_key(probe, presence=False, helper=h)
    except ses.SeSignerError as exc:
        pytest.skip(f"no usable helper or Secure Enclave: {exc}")
    return h


def test_creating_a_levain_sign_key_writes_the_three_files(tmp_path, real_helper):
    d = _keydir(tmp_path)
    made = custody.create_levain_sign(d, "levain-terminal", helper=real_helper, presence=False)
    assert made.path == d / "levain-terminal" and made.custody == "touch-id" and made.ctk_hash is None
    for p in (made.path, made.public_path, custody.sidecar_path(made.path)):
        assert p.is_file()
    assert stat.S_IMODE(os.stat(made.path).st_mode) == 0o600
    assert made.public_key.startswith("sk-ecdsa-sha2-nistp256@openssh.com ")
    sc = custody.read_sidecar(made.path)
    assert (sc.custody, sc.provider, sc.signer, sc.ctk_hash) == ("touch-id", "levain-sign", "levain-sign/1", None)
    with pytest.raises(KeyError_, match="already exists"):
        custody.create_levain_sign(d, "levain-terminal", helper=real_helper, presence=False)


def test_a_real_helper_key_signs_through_ssh_signer_to_the_presence_check(tmp_path, real_helper):
    d = _keydir(tmp_path)
    made = custody.create_levain_sign(d, "levain-terminal", helper=real_helper, presence=False)
    fp, sign = ckeys.ssh_signer(made.path, namespace=NS, helper=real_helper)
    # the no-presence key signs (the dispatch, the blob and the namespace all work), but its signature carries flags 0x00,
    # and the verify-before-return refuses it for lacking the user-presence flag: it is not returned
    with pytest.raises(KeyError_, match="without the user-presence flag"):
        sign(b"levain-terminal-answer/1\n")
    row = _log(d)[-1]
    assert (row["outcome"], row["code"], row["attempts"]) == ("unverified", "no-presence-flag", 1)


def test_the_real_helper_status_of_a_no_presence_key_is_false(tmp_path, real_helper):
    d = _keydir(tmp_path)
    made = custody.create_levain_sign(d, "levain-terminal", helper=real_helper, presence=False)
    # it signs with no interaction, so the probe says "not presence-gated": the probe distinguishes
    assert ses.key_status(made.path, helper=real_helper) == (False, "not presence-gated: the Secure Enclave key "
                                                                    "signed without any interaction")
    assert custody.ctk_status(made.public_key, identities=[], platform="darwin", key_dir=d,
                              helper=real_helper)[0] is False
    junk = tmp_path / "junk.blob"
    junk.write_bytes(b"not a blob")
    assert ses.key_status(junk, helper=real_helper)[0] is None            # not this Mac's


# ---------------------------------------------------------------------------------------------------
# R5: ctk_status dispatch to the helper status, all three answers (stub helper)


@pytest.mark.parametrize("mode,expected", [("gated", True), ("open", False), ("gone", None)])
def test_ctk_status_asks_the_helper_when_no_ctk_identity_matches(tmp_path, stub_helper, mode, expected):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _set(key, ".status", mode)
    pub = " ".join((d / "levain-terminal.pub").read_text().split()[:2])
    ok, note = custody.ctk_status(pub, identities=[], platform="darwin", key_dir=d, helper=stub_helper)
    assert ok is expected
    assert note == {"gated": "presence-gated Secure Enclave key on this Mac",
                    "open": "not presence-gated: the Secure Enclave key signed without any interaction",
                    "gone": "not this Mac's: the Secure Enclave key does not load here"}[mode]
    assert _stub_log(key) == [f"status --key {key}"]


def test_a_matching_ctk_identity_wins_and_the_helper_is_not_asked(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    pub = " ".join((d / "levain-terminal.pub").read_text().split()[:2])
    ident = custody.Identity(custody.point_sha1(pub), "bio", "x")
    assert custody.ctk_status(pub, identities=[ident], platform="darwin", key_dir=d, helper=stub_helper)[0] is True
    assert _stub_log(key) == []


def test_no_levain_sign_key_for_it_keeps_the_old_answer(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    other = _soft_key(_keydir(tmp_path, "elsewhere"), label="levain-terminal")
    pub = " ".join((other.parent / "levain-terminal.pub").read_text().split()[:2])
    ok, note = custody.ctk_status(pub, identities=[], platform="darwin", key_dir=d, helper=stub_helper)
    assert ok is None and "no CTK identity for it on this Mac" in note
    assert _stub_log(key) == []


def test_an_apple_provider_key_is_never_sent_to_the_helper(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d, provider=APPLE, signer=None, ctk_hash="A" * 40)
    pub = " ".join((d / "levain-terminal.pub").read_text().split()[:2])
    assert custody.ctk_status(pub, identities=[], platform="darwin", key_dir=d, helper=stub_helper)[0] is None
    assert _stub_log(key) == []


def test_without_a_key_dir_a_test_run_never_looks_in_the_real_one(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    _soft_key(d)
    pub = " ".join((d / "levain-terminal.pub").read_text().split()[:2])
    ok, note = custody.ctk_status(pub, identities=[], platform="darwin", helper=stub_helper)
    assert ok is None and "no CTK identity for it on this Mac" in note


def test_terminal_status_with_the_stub(tmp_path, stub_helper, monkeypatch):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    monkeypatch.setattr(ses, "require_helper", lambda cache_dir=None: stub_helper)
    _set(key, ".status", "gated")
    assert ts.status(d, identities=[], platform="darwin").state == "enrolled"
    _set(key, ".status", "open")
    rep = ts.status(d, identities=[], platform="darwin")
    assert rep.state == "warning" and "not presence-gated" in rep.text
    _set(key, ".status", "gone")
    assert ts.status(d, identities=[], platform="darwin").state == "warning"


# ---------------------------------------------------------------------------------------------------
# R6: the creator default


def test_default_creator_is_the_helper_on_a_mac_with_swiftc_and_a_secure_enclave(tmp_path):
    swiftc = tmp_path / "swiftc"
    swiftc.write_text("")
    creator, why = ts.default_creator(platform="darwin", swiftc=swiftc, machine="arm64")
    assert creator is custody.create_levain_sign and why is None


@pytest.mark.parametrize("kw,needle", [
    ({"platform": "linux", "machine": "arm64"}, "macOS only"),
    ({"platform": "darwin", "machine": "arm64", "swiftc": "/nonexistent/swiftc"}, "xcode-select --install"),
    ({"platform": "darwin", "machine": "x86_64"}, "no Secure Enclave"),
])
def test_default_creator_falls_back_to_the_apple_path_and_says_why(tmp_path, kw, needle):
    kw = dict(kw)
    if "swiftc" not in kw:
        sw = tmp_path / "swiftc"
        sw.write_text("")
        kw["swiftc"] = sw
    creator, why = ts.default_creator(**kw)
    assert creator is custody.create_touch_id and why is not None and needle in why and "\n" not in why


def test_create_uses_the_default_creator_and_prints_why_on_a_fallback(tmp_path, monkeypatch):
    from levain.cockpit.custody import CreatedKey
    calls = []

    def fake(d, label):
        calls.append((d, label))
        pub = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBExample"
        return CreatedKey(Path(d) / label, Path(d) / (label + ".pub"), pub, "touch-id", label, None)
    monkeypatch.setattr(ts, "default_creator", lambda: (fake, "the helper was not used, for a test reason"))
    monkeypatch.setattr("levain.consent.band.agent_session_caller", lambda: False)
    rep = ts.create(tmp_path / "keys", in_agent_session=lambda: False)
    assert rep.created and calls == [(tmp_path / "keys", "levain-terminal")]
    assert "note: the helper was not used, for a test reason" in rep.text
    monkeypatch.setattr(ts, "default_creator", lambda: (fake, None))
    rep2 = ts.create(tmp_path / "keys2", in_agent_session=lambda: False)
    assert rep2.created and "note:" not in rep2.text


def test_an_injected_creator_still_wins(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("default_creator must not run when a creator is injected")
    monkeypatch.setattr(ts, "default_creator", boom)
    from levain.cockpit.custody import CreatedKey
    pub = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBExample"
    rep = ts.create(tmp_path / "keys", creator=lambda d, label: CreatedKey(Path(d) / label, Path(d) / (label + ".pub"),
                                                                           pub, "touch-id", label, None),
                    in_agent_session=lambda: False)
    assert rep.created


# ---------------------------------------------------------------------------------------------------
# R7: shipping


def test_the_swift_source_ships_as_package_data():
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text()
    assert '"native/*.swift"' in text
    assert (root / "levain" / "native" / "levain_sign.swift").is_file()
    assert (root / "levain" / "native" / "__init__.py").is_file()
    assert ses.SOURCE == root / "levain" / "native" / "levain_sign.swift"


def test_the_helper_cache_is_the_users_not_the_key_directory():
    cache = ses.default_cache_dir()
    assert cache.parts[-3:] == ("Library", "Caches", "levain") or cache.parts[-4:] == ("Library", "Caches", "levain",
                                                                                      "native")
    assert ".levain" not in cache.parts


# ---------------------------------------------------------------------------------------------------
# levain#191 review round 1 (se-fix1): M2 no compile in a sign, M4 custody by the key's own dir, the signing.py refusal,
# the failure evidence record, the build point


def _fake_sc_auth(tmp_path: Path, monkeypatch) -> Path:
    """A path that exists (never run: the spawn keyguard denies sc_auth) and no CTK identities listed."""
    p = tmp_path / "sc_auth"
    p.write_text("")
    monkeypatch.setattr(custody, "list_identities", lambda sc_auth=None: [])
    return p


def _pub_of(key: Path) -> str:
    return " ".join(Path(str(key) + ".pub").read_text().split()[:2])


@pytest.mark.parametrize("mode,ok", [("gated", True), ("open", False)])
def test_declared_custody_judges_a_levain_sign_key_by_the_helper_status_in_its_own_directory(tmp_path, stub_helper,
                                                                                           monkeypatch, mode, ok):
    d = _keydir(tmp_path, "not-the-default-key-dir")
    key = _soft_key(d)
    _set(key, ".status", mode)
    monkeypatch.setattr(ses, "require_helper", lambda cache_dir=None: stub_helper)
    args = (key, None, "laptop-presence", _pub_of(key))
    kw = dict(sc_auth=_fake_sc_auth(tmp_path, monkeypatch), platform="darwin")
    if ok:
        assert custody.declared_custody(*args, **kw) == "touch-id"
    else:
        with pytest.raises(KeyError_, match="not presence-gated"):
            custody.declared_custody(*args, **kw)
    assert _stub_log(key) == [f"status --key {key}"]                      # the helper was asked, in the key's own directory


def test_custody_note_uses_the_helper_status_for_a_levain_sign_key(tmp_path, stub_helper, monkeypatch):
    from types import SimpleNamespace
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _set(key, ".status", "gated")
    monkeypatch.setattr(ses, "require_helper", lambda cache_dir=None: stub_helper)
    pub = _pub_of(key)
    row = SimpleNamespace(custody="touch-id", public_key=pub, fingerprint="SHA256:x")
    notes = custody.custody_note([row], sc_auth=_fake_sc_auth(tmp_path, monkeypatch), platform="darwin", key_dir=d)
    assert notes == {"SHA256:x": "presence-gated Secure Enclave key on this Mac"}          # not the CTK sentence


def test_a_sign_never_compiles_a_missing_helper_is_a_plain_refusal(tmp_path, monkeypatch):
    d = _keydir(tmp_path)
    key = _soft_key(d)

    def boom(*a, **k):
        raise AssertionError("the sign path built the helper")
    monkeypatch.setattr(ses, "build_helper", boom)
    monkeypatch.setattr(ses, "ensure_helper", boom)
    monkeypatch.setattr(ses, "default_cache_dir", lambda: tmp_path / "empty-cache")
    _fp, sign = ckeys.ssh_signer(key, namespace=NS)                     # no helper= : the default lookup
    with pytest.raises(KeyError_, match=r"levain doctor --fix") as ei:
        sign(b"m")
    assert "nothing was signed" in str(ei.value)
    assert _log(d)[-1]["outcome"] == "helper-missing"
    # and the status path says it in words instead of compiling
    ok, note = custody._levain_sign_status(_pub_of(key), d, None)
    assert ok is None and "levain doctor --fix" in note


def test_the_build_point_builds_once_and_says_so(tmp_path, monkeypatch):
    from levain import doctor
    built = []
    monkeypatch.setattr(ses, "require_helper", lambda cache_dir=None: (_ for _ in ()).throw(ses.SeSignerError("no")))
    monkeypatch.setattr(ses, "ensure_helper", lambda cache_dir=None: built.append(1) or tmp_path / "h")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doctor.build_signing_helper() == (True, f"signing helper: built ({tmp_path / 'h'})") and built == [1]
    monkeypatch.setattr(ses, "require_helper", lambda cache_dir=None: tmp_path / "h")
    assert doctor.build_signing_helper() == (True, "signing helper: already built") and built == [1]
    monkeypatch.setattr(ses, "require_helper", lambda cache_dir=None: (_ for _ in ()).throw(ses.SeSignerError("no")))
    monkeypatch.setattr(ses, "ensure_helper", lambda cache_dir=None: (_ for _ in ()).throw(
        ses.SeSignerError("missing swiftc (xcode-select --install)")))
    ok, said = doctor.build_signing_helper()
    assert ok is False and "NOT built" in said


def test_doctor_has_a_fix_flag_that_reaches_the_build():
    from levain import cli
    p = cli.build_parser() if hasattr(cli, "build_parser") else None
    if p is None:
        pytest.skip("no parser factory")
    assert p.parse_args(["doctor", "--fix"]).fix is True and p.parse_args(["doctor"]).fix is False


def test_signing_env_and_prove_can_sign_refuse_a_levain_sign_key_in_plain_words(tmp_path):
    from levain.team import signing as S
    key = _soft_key(_keydir(tmp_path))
    words = "this key signs through Levain's Touch ID helper and cannot sign git commits; use a cockpit key"
    with pytest.raises(S.SigningError) as ei:
        S.signing_env(str(key))
    assert str(ei.value) == words
    with pytest.raises(S.SigningError, match="cannot sign git commits"):
        S.prove_can_sign(str(key), Path(str(key) + ".pub").read_text().strip())
    # the pub path names the same key; a key with no sidecar, an Apple key and a literal are unchanged
    with pytest.raises(S.SigningError):
        S.signing_env(str(key) + ".pub")
    apple = _soft_key(_keydir(tmp_path, "apple"), provider=APPLE, signer=None, ctk_hash="A" * 40)
    assert S.signing_env(str(apple))["SSH_SK_PROVIDER"] == APPLE
    assert "SSH_SK_PROVIDER" not in S.signing_env("key::sk-ecdsa-sha2-nistp256@openssh.com AAAA")
    assert S.signing_env() and S.signing_env(str(tmp_path / "no-such-key"))


def _evidence(d: Path) -> list[dict]:
    f = sorted((d / "sign-failures").glob("sigfail-*.json")) if (d / "sign-failures").is_dir() else []
    return [json.loads(p.read_text()) for p in f]


def test_a_failed_helper_sign_writes_the_sign_failures_record_with_the_helper_fields(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    other = _soft_key(d, label="other")
    _fp, osign = _signer(other, stub_helper)
    foreign = osign(b"m")
    _set(key, ".replay", json.dumps({"ok": True, "signature": foreign, "attempts": 2, "evaluated": True,
                                     "evaluations": 1, "flags": 1, "counter": 9}))
    _fp, sign = _signer(key, stub_helper)
    with pytest.raises(KeyError_):
        sign(b"secret message bytes")
    (rec,) = _evidence(d)
    assert rec["format"] == "levain-sign-failure/1" and rec["signer"] == "levain-sign"
    assert rec["helper_code"] == "verify" and rec["helper_attempts"] == 2 and rec["flags"] == "0x01" and rec["counter"] == 9
    assert rec["message_sha256"] and "secret message bytes" not in json.dumps(rec)
    # a helper error code is carried the same way
    Path(str(key) + ".replay").unlink()
    _set(key, ".fail", "key_load")
    with pytest.raises(KeyError_):
        sign(b"m")
    assert [r["helper_code"] for r in _evidence(d)].count("key_load") == 1
    # a person cancelling is not a failure to keep evidence of
    Path(str(key) + ".fail").unlink()
    before = len(_evidence(d))
    _set(key, ".fail", "presence_denied")
    _set(key, ".fail-extra", json.dumps(LA_CANCEL))
    with pytest.raises(KeyError_):
        sign(b"m")
    assert len(_evidence(d)) == before


def test_the_signer_refuses_an_answer_that_reports_two_presence_evaluations(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _set(key, ".evaluations", "2")
    _fp, sign = _signer(key, stub_helper)
    with pytest.raises(KeyError_, match="evaluated presence"):
        sign(b"m")
    assert _log(d)[-1]["outcome"] == "unverified" and _log(d)[-1]["code"] == "too-many-evaluations"


# ---------------------------------------------------------------------------------------------------
# levain#191 review round 2 (serot-fix2): the wall follows the budget, cancel is only a cancel, the toolchain probe,
# the locked sign log, doctor --fix's exit, create's leftovers and cleanup, the cache dir with no passwd entry

LA = "com.apple.LocalAuthentication"


def test_a_sign_with_no_time_left_is_refused_before_the_helper_runs(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _fp, sign = _signer(key, stub_helper, timeout=2.5)          # 2.5 s less two 1 s verify reserves: under a second
    with pytest.raises(KeyError_, match="no time left to sign") as ei:
        sign(b"m")
    assert _stub_log(key) == [] and "nothing was signed" in str(ei.value)
    assert _log(d)[-1]["outcome"] == "no-time"


def test_the_helper_is_always_given_a_wall_ms(tmp_path, stub_helper):
    key = _soft_key(_keydir(tmp_path))
    _fp, sign = _signer(key, stub_helper)
    sign(b"m")
    (call,) = _stub_log(key)
    words = call.split()
    assert int(words[words.index("--wall-ms") + 1]) > 0


@pytest.mark.parametrize("extra,kind,code", [
    (LA_CANCEL, "cancelled", -2),
    ({"error_domain": LA, "error_code": -4}, "cancelled", -4),
    ({"error_domain": LA, "error_code": -9}, "cancelled", -9),
    ({"error_domain": LA, "error_code": -8}, "signer-error", -8),         # biometryLockout
    ({"error_domain": LA, "error_code": -6}, "signer-error", -6),         # biometryNotAvailable (a closed lid)
    ({"error_domain": LA, "error_code": -7}, "signer-error", -7),         # biometryNotEnrolled
    ({"error_domain": "NSOSStatusErrorDomain", "error_code": -2}, "signer-error", -2),   # -2, but not LocalAuthentication
    ({}, "signer-error", None),                                          # no domain, no code
])
def test_presence_denied_is_a_cancel_only_for_the_three_la_cancel_codes(tmp_path, stub_helper, extra, kind, code):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _set(key, ".fail", "presence_denied")
    _set(key, ".fail-extra", json.dumps(extra))
    _fp, sign = _signer(key, stub_helper)
    with pytest.raises(KeyError_) as ei:
        sign(b"m")
    said = str(ei.value)
    assert ts._classify(ei.value)[0] == kind and "nothing was signed" in said
    row = _log(d)[-1]
    if kind == "cancelled":
        assert row["outcome"] == "cancelled" and _evidence(d) == []
    else:
        assert row["outcome"] == "presence-failed" and "cancel" not in said.lower()
        (rec,) = _evidence(d)
        assert rec["signer"] == "levain-sign" and rec["helper_code"].startswith("presence_denied")
        if code is not None:
            assert f"error {code} (" in said and str(code) in row["code"] and str(code) in rec["helper_code"]


def test_an_ok_answer_with_no_signature_is_a_signer_error_not_a_cancel(tmp_path, stub_helper):
    d = _keydir(tmp_path)
    key = _soft_key(d)
    _set(key, ".replay", json.dumps({"ok": True, "attempts": 1, "evaluated": True, "evaluations": 1}))
    fp, sign = _signer(key, stub_helper)
    with pytest.raises(KeyError_) as ei:
        sign(b"m")
    assert str(ei.value) == f"the Secure Enclave helper answered with no signature for {fp}, so nothing was signed"
    assert ts._classify(ei.value)[0] == "signer-error"
    assert len(_evidence(d)) == 1 and _log(d)[-1]["outcome"] == "no-signature"


def test_the_default_swiftc_is_never_run_without_a_toolchain(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake(cmd, **kw):
        calls.append(list(cmd))
        if cmd[0] == ses.XCODE_SELECT:
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="xcode-select: error: no developer tools")
        raise AssertionError(f"ran {cmd}")
    monkeypatch.setattr(ses.subprocess, "run", fake)
    monkeypatch.setattr(ses, "SWIFTC", ses.DEFAULT_SWIFTC)
    assert ses.toolchain_present() is False
    with pytest.raises(ses.SeSignerError, match="xcode-select --install") as ei:
        ses.build_helper(tmp_path / "cache")
    assert ei.value.code == "no_toolchain"
    assert calls and all(c[0] == ses.XCODE_SELECT and c[1:] == ["-p"] for c in calls)     # never swiftc
    creator, why = ts.default_creator(platform="darwin", machine="arm64")
    assert creator is custody.create_touch_id and "xcode-select --install" in why and "\n" not in why


def test_toolchain_present_needs_exit_0_and_a_directory_that_exists(tmp_path, monkeypatch):
    answer: dict = {}

    def fake(cmd, **kw):
        assert cmd == [ses.XCODE_SELECT, "-p"] and kw["timeout"] == ses.TOOLCHAIN_PROBE_TIMEOUT
        if answer.get("raise"):
            raise answer["raise"]
        return subprocess.CompletedProcess(cmd, answer["rc"], stdout=answer["out"], stderr="")
    monkeypatch.setattr(ses.subprocess, "run", fake)
    answer.update(rc=0, out=f"{tmp_path}\n")
    assert ses.toolchain_present() is True
    answer.update(rc=0, out=f"{tmp_path / 'gone'}\n")
    assert ses.toolchain_present() is False
    answer.update(rc=0, out="")
    assert ses.toolchain_present() is False
    answer.update(rc=2, out=f"{tmp_path}\n")
    assert ses.toolchain_present() is False
    answer.update({"raise": subprocess.TimeoutExpired("xcode-select", 10)})
    assert ses.toolchain_present() is False
    # with the toolchain there, default_creator takes the helper
    answer.clear()
    answer.update(rc=0, out=f"{tmp_path}\n")
    monkeypatch.setattr(ses, "SWIFTC", ses.DEFAULT_SWIFTC)
    assert ts.default_creator(platform="darwin", machine="arm64") == (custody.create_levain_sign, None)


def test_each_build_compiles_into_its_own_tmp_and_a_failed_one_leaves_none(tmp_path, monkeypatch):
    swiftc = tmp_path / "swiftc"
    swiftc.write_text("")
    monkeypatch.setattr(ses, "SWIFTC", str(swiftc))
    outs: list[str] = []

    def fake(cmd, **kw):
        outs.append(cmd[cmd.index("-o") + 1])
        assert Path(outs[-1]).exists()                      # made O_EXCL before swiftc runs
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: stub")
    monkeypatch.setattr(ses.subprocess, "run", fake)
    cache = tmp_path / "cache"
    for _ in range(2):
        with pytest.raises(ses.SeSignerError, match="swiftc failed"):
            ses.build_helper(cache)
    assert len(set(outs)) == 2 and list(cache.iterdir()) == []


def test_two_signers_at_once_lose_no_sign_log_line(tmp_path):
    import threading
    d = _keydir(tmp_path)
    n = 300
    errors: list[str] = []

    def run(t: int) -> None:
        for i in range(n):
            why = ses.append_sign_log(d, ses.sign_log_record(outcome=f"t{t}-{i}", attempts=1, evaluated=True,
                                                             elapsed_s=0.1, code="ok"))
            if why is not None:
                errors.append(why)
    threads = [threading.Thread(target=run, args=(t,)) for t in (0, 1)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    rows = [r["outcome"] for r in _log(d)]
    assert len(rows) == ses.SIGN_LOG_KEEP and len(set(rows)) == len(rows)
    for t in (0, 1):
        mine = [int(o.split("-")[1]) for o in rows if o.startswith(f"t{t}-")]
        assert mine == list(range(n - len(mine), n))      # the newest of what that signer wrote, in order, no gap
    assert stat.S_IMODE(os.lstat(d / ses.SIGN_LOG_LOCK).st_mode) == 0o600
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


def test_doctor_fix_with_the_helper_not_built_exits_broken_even_when_the_checks_pass(tmp_path, monkeypatch, capsys):
    from levain import doctor
    monkeypatch.setattr(doctor, "_run_doctor", lambda path, invoke=False, repin_hooks=False: doctor.EXIT_HEALTHY)
    monkeypatch.setattr(doctor, "build_signing_helper", lambda: (False, "signing helper: NOT built (stub)"))
    assert doctor.run_doctor(tmp_path, fix=True) == doctor.EXIT_BROKEN
    assert "NOT built" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "build_signing_helper", lambda: (True, "signing helper: already built"))
    assert doctor.run_doctor(tmp_path, fix=True) == doctor.EXIT_HEALTHY
    assert doctor.run_doctor(tmp_path) == doctor.EXIT_HEALTHY          # no --fix: the helper is not consulted


def test_doctor_fix_says_a_compile_can_take_minutes_before_it_builds(tmp_path, monkeypatch, capsys):
    from levain import doctor
    order: list[str] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ses, "require_helper", lambda cache_dir=None: (_ for _ in ()).throw(ses.SeSignerError("no")))

    def build(cache_dir=None):
        order.append("built:" + capsys.readouterr().out)
        return tmp_path / "h"
    monkeypatch.setattr(ses, "ensure_helper", build)
    assert doctor.build_signing_helper()[0] is True
    assert order == ["built:building the signing helper (a compile can take minutes)…\n"]


def test_a_leftover_meta_or_counter_refuses_the_create_and_survives(tmp_path):
    d = _keydir(tmp_path)
    for suffix in (".meta.json", ".counter"):
        left = d / ("levain-terminal" + suffix)
        left.write_text("leftover")
        with pytest.raises(KeyError_, match=suffix.replace(".", r"\.")):
            custody.create_levain_sign(d, "levain-terminal", helper=tmp_path / "never-run", platform="darwin")
        assert left.read_text() == "leftover" and not (d / "levain-terminal").exists()
        left.unlink()


FAILING_CREATE = r'''
import json, sys
argv = sys.argv[1:]
out = argv[argv.index("--out") + 1]
open(out, "w").write("blob")
open(out + ".meta.json", "w").write("{}")
sys.stdout.write(json.dumps({"ok": False, "code": "write", "error": "stub: failed after writing both"}) + "\n")
sys.exit(1)
'''


def test_a_create_key_failure_leaves_no_key_and_no_meta(tmp_path):
    d = _keydir(tmp_path)
    helper = tmp_path / "failing-helper"
    helper.write_text(f"#!{sys.executable}\n" + FAILING_CREATE)
    helper.chmod(0o755)
    with pytest.raises(KeyError_, match="could not make the key"):
        custody.create_levain_sign(d, "levain-terminal", helper=helper, platform="darwin")
    assert sorted(p.name for p in d.iterdir()) == []


def test_create_levain_sign_applies_the_real_key_gate_first(tmp_path, monkeypatch):
    def refuse(path, what="the key directory"):
        raise ckeys.RealKeyUnmarked("stub: the real key directory")
    monkeypatch.setattr(custody, "refuse_real_key_dir_unless_live", refuse)
    with pytest.raises(ckeys.RealKeyUnmarked):
        custody.create_levain_sign(_keydir(tmp_path), "levain-terminal", helper=tmp_path / "never-run",
                                   platform="darwin")


def test_default_creator_off_a_mac_points_at_the_passphrase_key():
    creator, why = ts.default_creator(platform="linux", machine="x86_64")
    assert why == "terminal-key create is macOS only; use `levain cockpit key create --custody passphrase`"


def test_the_helper_cache_dir_with_no_passwd_entry_is_a_signer_error(monkeypatch):
    import pwd

    def nobody(uid):
        raise KeyError(f"getpwuid(): uid not found: {uid}")
    monkeypatch.setattr(pwd, "getpwuid", nobody)
    with pytest.raises(ses.SeSignerError) as ei:
        ses.default_cache_dir()
    assert ei.value.code == "no_home"
    with pytest.raises(ses.SeSignerError):
        ses.require_helper()
