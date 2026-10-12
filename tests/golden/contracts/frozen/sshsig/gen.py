"""Generate frozen/sshsig/vectors.json (F12: SSHSIG verification for ``device_sig``). stdlib + cryptography only.

Every blob is assembled here from PROTOCOL.sshsig §2-§3 field by field (``_genlib``), signed with deterministic
Ed25519 seeds, so a regeneration writes the same bytes. The one exception is the ECDSA blob, which the design asks
to be made by ``ssh-keygen -Y sign`` with a throwaway key: it is made once into ``ecdsa-p256.sig`` (armored) and
reused, since ECDSA signing is randomised. Every vector with an upstream is run through ``ssh-keygen -Y
check-novalidate`` on its armored form here, and the generator refuses to write a vector whose measured verdict
disagrees with its classification (``ssh -V`` must equal the recorded version).
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import (ACCEPT, REFUSE, UPSTREAM, armor, b64, dearmor, derivation, device_message,  # noqa: E402
                     ed_key_blob, ed_sign, ed_sshsig, fp, profile, pub, seed, spec, ssh_str, sshsig_blob,
                     sshsig_signed_data, vector, write)

HERE = Path(__file__).resolve().parent
SSH_V = subprocess.run(["ssh", "-V"], capture_output=True, text=True).stderr.strip().split(",")[0]
assert SSH_V == UPSTREAM["sshsig"][1], f"regenerating under {SSH_V}: update _genlib.UPSTREAM first"
NS = b"levain-team-entry"
PS = "OpenSSH PROTOCOL.sshsig (openssh-portable master 73c1399e, $OpenBSD: PROTOCOL.sshsig,v 1.5 2026/10/10$)"
FIELDS = derivation(f"{PS} §2 / S1_DESIGN F12",
                    "byte[6] 'SSHSIG' · uint32 1 · string publickey · string namespace · string reserved · string "
                    "hash_algorithm · string signature; the outer blob, the publickey blob and the signature blob "
                    "are each read to their last byte")
SIGNED = derivation(f"{PS} §3 / S1_DESIGN F12",
                    "'SSHSIG' || string(namespace) || string(reserved) || string(hash_algorithm) || string(H(message))")
NSRULE = spec(PS + " §2", "The purpose of the namespace value is to specify an unambiguous interpretation domain for "
                          "the signature")
VERIFY = derivation(f"{PS} §3 / S1_DESIGN F12 + F2",
                    "the Ed25519 signature inside the blob verifies over §3's signed data under the embedded key, "
                    "with levain's one Ed25519 profile")
SHA256 = profile("S1_DESIGN F12", "the hash algorithm is sha512",
                 quote='The supported hash algorithms are "sha256" and "sha512".')
RESERVED = profile("S1_DESIGN F12", "the reserved field is empty",
                   quote="Implementations should ignore the reserved field if it is not empty.")
KEYTYPE = profile("S1_DESIGN F12", "the embedded key is ssh-ed25519; a P-256 device line is P11's, as a new (line, v)",
                  rationale="FL l.264: device keys are Ed25519; ECDSA's (r, s) / (r, n-s) malleability would give "
                            "one signed entry two leaf hashes unless a low-S rule froze with it")
BINDING = profile("S1_DESIGN F12", "fp_of_blob(embedded public key) == the leaf's device_fp",
                  rationale="the leaf names its device by device_fp; ssh-keygen -Y check-novalidate has no such input")
SPLUSL = spec("RFC 8032 §5.1.7 (pinned in levain by the speccheck row, S1_DESIGN F2)",
              "Decode the first half as a point R, and the second half as an integer S, in the range 0 <= s < L. "
              "... If any of the decodings fail (including S being out of range), the signature is invalid.")
L_ORDER = 2**252 + 27742317777372353535851937790883648493

ORIGIN = "levain-team/" + "a" * 32
FILE = "ledger/dana/findings.jsonl"
CHAIN = "00" * 31 + "01"
MSG = device_message(ORIGIN, FILE, CHAIN)
DEV = seed("sshsig-device")
DEV_BLOB = ed_key_blob(pub(DEV))
DEV_FP = fp(DEV_BLOB)
OTHER = seed("sshsig-other-key")
V = []


def check_novalidate(blob: bytes, msg: bytes = MSG) -> str:
    with tempfile.TemporaryDirectory() as td:
        sig = Path(td) / "sig"
        sig.write_text(armor(blob))
        r = subprocess.run(["ssh-keygen", "-Y", "check-novalidate", "-n", NS.decode(), "-s", str(sig)], input=msg,
                           capture_output=True)
    return ACCEPT if r.returncode == 0 else REFUSE


def add(vid, kind, blob, verdict, authority, *, device_fp=DEV_FP, msg=MSG, upstream="sshsig"):
    if kind == "no_upstream":
        upstream = None
    else:
        measured = check_novalidate(blob, msg)
        want = verdict if kind == "spec" else ACCEPT
        assert measured == want, (vid, kind, measured)
    V.append(vector(vid, kind, "verify", {"blob_b64": b64(blob), "device_fp": device_fp,
                                           "namespace": NS.decode(), "message_hex": msg.hex()},
                    {"verdict": verdict}, authority, upstream))


# §3 signed data for F9's fixed message, lengths written by hand (S1_DESIGN F12)
digest = hashlib.sha512(MSG).hexdigest()
SIGNED_HEX = ("535348534947"                                   # "SSHSIG"
              + "00000011" + NS.hex()                          # string("levain-team-entry"), 17 bytes
              + "00000000"                                     # string(""), the reserved field
              + "00000006" + b"sha512".hex()                   # string("sha512")
              + "00000040" + digest)                           # string(SHA-512(message)), 64 bytes
assert bytes.fromhex(SIGNED_HEX) == sshsig_signed_data(NS, MSG)
V.append(vector("signed-data-fixed-message", "no_upstream", "signed_data",
                {"namespace": NS.decode(), "message_hex": MSG.hex()},
                {"verdict": ACCEPT, "bytes_hex": SIGNED_HEX}, SIGNED, None))

GOOD = ed_sshsig(DEV, NS, MSG)
add("good", "spec", GOOD, ACCEPT, VERIFY)
add("wrong-namespace-refused", "spec", ed_sshsig(DEV, b"levain-team-other", MSG), REFUSE, NSRULE)

sig256 = ed_sign(DEV, sshsig_signed_data(NS, MSG, alg=b"sha256"))
add("hash-sha256-refused", "levain_profile",
    sshsig_blob(DEV_BLOB, NS, ssh_str(b"ssh-ed25519") + ssh_str(sig256), alg=b"sha256"), REFUSE, SHA256)
# reserved "x" in the blob over §3 data with an empty reserved field: what OpenSSH signs and checks (it ignores the
# blob's reserved value), so ssh-keygen accepts and the difference is levain's rule alone
sig_r = ed_sign(DEV, sshsig_signed_data(NS, MSG))
add("reserved-non-empty-refused", "levain_profile",
    sshsig_blob(DEV_BLOB, NS, ssh_str(b"ssh-ed25519") + ssh_str(sig_r), reserved=b"x"), REFUSE, RESERVED)
add("embedded-key-not-device-fp-refused", "no_upstream", GOOD, REFUSE, BINDING, device_fp=fp(ed_key_blob(pub(OTHER))))

ECDSA = HERE / "ecdsa-p256.sig"
if not ECDSA.exists():   # made once: ssh-keygen with a throwaway key that never leaves the temp dir
    with tempfile.TemporaryDirectory() as td:
        key, msgf = Path(td) / "k", Path(td) / "m"
        msgf.write_bytes(MSG)
        subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-C", "", "-f", str(key)], check=True)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-n", NS.decode(), "-f", str(key), str(msgf)], check=True,
                       capture_output=True)
        tmp = ECDSA.with_suffix(".partial")
        tmp.write_text(Path(str(msgf) + ".sig").read_text())
        os.replace(tmp, ECDSA)
ecdsa_blob = dearmor(ECDSA.read_text())
ecdsa_key = ecdsa_blob[10 + 4:10 + 4 + int.from_bytes(ecdsa_blob[10:14], "big")]
assert ecdsa_key.startswith(ssh_str(b"ecdsa-sha2-nistp256"))
add("ecdsa-p256-key-refused", "levain_profile", ecdsa_blob, REFUSE, KEYTYPE, device_fp=fp(ecdsa_key))

tampered = bytearray(GOOD)
tampered[-1] ^= 0x01
add("one-byte-tampered-refused", "spec", bytes(tampered), REFUSE, VERIFY)
sig_other = ed_sign(OTHER, sshsig_signed_data(NS, MSG))
add("wrong-key-refused", "spec", sshsig_blob(DEV_BLOB, NS, ssh_str(b"ssh-ed25519") + ssh_str(sig_other)), REFUSE,
    VERIFY)

# one trailing byte at each of the three levels, each beside the valid control ("good")
good_sig = ed_sign(DEV, sshsig_signed_data(NS, MSG))
add("trailing-byte-outer-blob-refused", "spec", GOOD + b"\x00", REFUSE, FIELDS)
add("trailing-byte-key-blob-refused", "spec",
    sshsig_blob(DEV_BLOB + b"\x00", NS, ssh_str(b"ssh-ed25519") + ssh_str(good_sig)), REFUSE, FIELDS)
add("trailing-byte-signature-blob-refused", "spec",
    sshsig_blob(DEV_BLOB, NS, ssh_str(b"ssh-ed25519") + ssh_str(good_sig) + b"\x00"), REFUSE, FIELDS)

# S + L: the same signature with its scalar raised by the group order. The pinned row refuses it; OpenSSH_10.3p1's
# ssh-keygen accepts it (measured), so the vector is upstream_lax under RFC 8032's range rule.
s_plus_l = (int.from_bytes(good_sig[32:], "little") + L_ORDER).to_bytes(32, "little")
assert good_sig == ed_sign(DEV, sshsig_signed_data(NS, MSG))   # the control is the unmodified "good" blob
add("s-plus-l-mutation-refused", "upstream_lax",
    sshsig_blob(DEV_BLOB, NS, ssh_str(b"ssh-ed25519") + ssh_str(good_sig[:32] + s_plus_l)), REFUSE, SPLUSL)

write("F12", "sshsig", V, ssh_version=SSH_V, message={"origin": ORIGIN, "file": FILE, "chain_hash": CHAIN})
