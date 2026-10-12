"""Generate frozen/ed25519/vectors.json (F2's pinned profile) from the speccheck ``cases.json`` beside it.

The authority of every verdict is the published table, not a live implementation: "the Go/OpenSSL row",
``V V V V X X X X X X X V``, the 12 verdicts the speccheck README gives Go crypto/ed25519, OpenSSL 3.0,
BoringSSL and PyCA. stdlib only.
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, vector, write  # noqa: E402

HERE = Path(__file__).resolve().parent
URL = "https://github.com/novifinancial/ed25519-speccheck"
COMMIT = "65519336fda78a3d016e947df6d82848aca0c9da"
SHA256 = "08e47a36d9aead288664930505584f353fff113ab854f2800db1e4f5b3540450"
ROW = "V V V V X X X X X X X V"

raw = (HERE / "cases.json").read_bytes()
assert hashlib.sha256(raw).hexdigest() == SHA256
cases = json.loads(raw)
row = ROW.split()
assert len(cases) == len(row) == 12
V = []
for i, (c, r) in enumerate(zip(cases, row)):
    V.append(vector(f"speccheck-{i}", "spec", "verify",
                    {"pub_key": c["pub_key"], "message": c["message"], "signature": c["signature"]},
                    {"verdict": ACCEPT if r == "V" else REFUSE},
                    {"kind": "published_table", "source": f"https://eprint.iacr.org/2020/1244 ; {URL} @ {COMMIT}",
                     "clause": f"cases.json vector {i}; README results row for Go/OpenSSL-3.0/BoringSSL/PyCA: {ROW}"},
                    "ed25519"))
write("F2-ed25519", "ed25519", V, cases_json={"url": f"{URL}/blob/{COMMIT}/cases.json", "commit": COMMIT,
                                               "sha256": SHA256}, row=ROW)
