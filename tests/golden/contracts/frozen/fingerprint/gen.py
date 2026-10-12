"""Generate frozen/fingerprint/*.pub and vectors.json (F7 fp_of_blob, F6 fp_hex).

One deterministic throwaway public key per type. The expected fingerprint is what ``ssh-keygen -lf`` prints at
generation time (recorded with ``ssh -V``), and the generator also checks it against the formula computed here
with hashlib. sk-* keys need no hardware: a public key line is only parsed, so the blob is built by hand from
PROTOCOL.u2f's field order. stdlib + cryptography only.
"""
import base64
import hashlib
import re
import struct
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, UPSTREAM, pub, seed, vector, write  # noqa: E402

HERE = Path(__file__).resolve().parent
SSH_V = subprocess.run(["ssh", "-V"], capture_output=True, text=True).stderr.strip().split(",")[0]
assert SSH_V == UPSTREAM["ssh-keygen"][1], f"regenerating under {SSH_V}: update _genlib.UPSTREAM first"
FORMULA = {"kind": "byte_derivation", "source": "S1_DESIGN F7 / F6",
           "clause": "fp_of_blob = 'SHA256:' + base64(SHA-256(wire blob)) without '=' padding; "
                     "fp_hex = lowercase hex of the same SHA-256"}


def s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def ec_point(curve, label):
    n = {"secp256r1": 2**256, "secp384r1": 2**384, "secp521r1": 2**521}[curve.name]
    k = int.from_bytes(hashlib.sha512(seed(label)).digest() * 2, "big") % (n // 4) + 1
    key = ec.derive_private_key(k, curve)
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


blobs = {
    "ssh-ed25519": s(b"ssh-ed25519") + s(pub(seed("fp-ed25519"))),
    "ecdsa-sha2-nistp256": s(b"ecdsa-sha2-nistp256") + s(b"nistp256") + s(ec_point(ec.SECP256R1(), "fp-p256")),
    "ecdsa-sha2-nistp384": s(b"ecdsa-sha2-nistp384") + s(b"nistp384") + s(ec_point(ec.SECP384R1(), "fp-p384")),
    "ecdsa-sha2-nistp521": s(b"ecdsa-sha2-nistp521") + s(b"nistp521") + s(ec_point(ec.SECP521R1(), "fp-p521")),
    "sk-ssh-ed25519@openssh.com": s(b"sk-ssh-ed25519@openssh.com") + s(pub(seed("fp-sk-ed25519"))) + s(b"ssh:"),
    "sk-ecdsa-sha2-nistp256@openssh.com": s(b"sk-ecdsa-sha2-nistp256@openssh.com") + s(b"nistp256")
    + s(ec_point(ec.SECP256R1(), "fp-sk-p256")) + s(b"ssh:"),
}
V = []
for typ, blob in blobs.items():
    fname = typ.split("@")[0] + ".pub"
    line = f"{typ} {base64.b64encode(blob).decode()} golden-{typ.split('@')[0]}"
    (HERE / fname).write_text(line + "\n")
    out = subprocess.run(["ssh-keygen", "-lf", str(HERE / fname)], capture_output=True, text=True, check=True).stdout
    fp = re.search(r"(SHA256:[A-Za-z0-9+/]+)", out).group(1)
    assert fp == "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("="), (typ, out)
    V.append(vector(f"fingerprint-{typ.split('@')[0]}", "spec", "fingerprint", {"line": line, "file": fname},
                    {"verdict": ACCEPT, "fp": fp, "fp_hex": hashlib.sha256(blob).hexdigest()}, FORMULA, "ssh-keygen"))

# declared type != the blob's embedded type
mismatch = f"ssh-ed25519 {base64.b64encode(blobs['ecdsa-sha2-nistp256']).decode()} mismatch"
tmp = HERE / "_mismatch.tmp"
tmp.write_text(mismatch + "\n")
rc = subprocess.run(["ssh-keygen", "-lf", str(tmp)], capture_output=True, text=True).returncode
tmp.unlink()
assert rc != 0, "ssh-keygen accepted a type-mismatched line"
V.append(vector("declared-type-differs-from-blob-refused", "spec", "fingerprint", {"line": mismatch},
                {"verdict": REFUSE},
                {"kind": "spec", "source": "S1_DESIGN F7", "clause": "the parser requires the blob's embedded type "
                 "string to equal the line's declared type"}, "ssh-keygen"))
write("F7", "fingerprint", V, ssh_version=SSH_V)
