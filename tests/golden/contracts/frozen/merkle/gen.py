"""Generate frozen/merkle/vectors.json (F1). Run from anywhere: ``python gen.py``. stdlib + cryptography only."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import (ACCEPT, REFUSE, derivation, leaves, lh, mth, path, profile, spec, subproof,  # noqa: E402
                     vector, write)

RFC = "RFC 9162 §2.1 (https://www.rfc-editor.org/rfc/rfc9162)"
WIT = "C2SP tlog-witness@v1.1.0"
INCL = spec(RFC + " §2.1.3.2", "inclusion proof verification algorithm")
CONS = spec(RFC + " §2.1.4.2", "consistency proof verification, 0 < first < second")
TESTONLY = ("github.com/transparency-dev/merkle testonly/constants.go @ v0.0.2 "
            "(commit 036047b5d2f7faf3b1ee643d391e60fe5b1defcf)")
EMPTY = hashlib.sha256(b"").digest().hex()
RULE = "d_i = bytes([i % 256]) * (i+1)"
OK = {"verdict": ACCEPT}
NO = {"verdict": REFUSE}
V = []

for n in [*range(9), 15, 16, 17, 255, 256, 257, 1023, 1024, 1025, 1000]:
    V.append(vector(f"root-{n}", "spec", "root", {"leaf_rule": RULE, "size": n},
                    {"verdict": ACCEPT, "root": mth(leaves(n)).hex()},
                    derivation(RFC + " §2.1.1", "MTH({}) = SHA-256(); MTH(D_n) = SHA-256(0x01 || MTH(D[0:k]) || "
                               "MTH(D[k:n])), k the largest power of two smaller than n; leaf = SHA-256(0x00 || d)"),
                    "merkle"))

ref_leaves = ["", "00", "10", "2021", "3031", "40414243", "5051525354555657", "606162636465666768696a6b6c6d6e6f"]
ref_roots = [EMPTY, "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
             "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
             "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
             "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
             "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
             "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
             "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
             "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328"]
for n in range(9):
    d = [bytes.fromhex(x) for x in ref_leaves[:n]]
    assert mth(d).hex() == ref_roots[n]   # the published table and the independent arithmetic agree
    V.append(vector(f"rfc6962-reference-root-{n}", "spec", "root_explicit", {"leaves": ref_leaves[:n]},
                    {"verdict": ACCEPT, "root": ref_roots[n]},
                    {"kind": "published_table", "source": TESTONLY, "clause": f"RootHashes()[{n}] over LeafInputs()[:{n}]"},
                    "merkle"))


def incl(vid, kind, size, index, leaf, proof, root, levain, authority=INCL):
    inp = {"size": size, "index": index, "leaf_hash": leaf, "proof": proof, "root": root}
    V.append(vector(vid, kind, "inclusion", inp, levain, authority, None if kind == "no_upstream" else "merkle"))


for n, idxs in ((7, range(7)), (8, range(8)), (1000, (0, 511, 512, 999))):
    d = leaves(n)
    r = mth(d).hex()
    for i in idxs:
        incl(f"inclusion-{n}-{i}", "spec", n, i, lh(d[i]).hex(), [p.hex() for p in path(i, d)], r, OK)

d7 = leaves(7)
r7 = mth(d7).hex()
p5 = [p.hex() for p in path(5, d7)]
l5 = lh(d7[5]).hex()
flip = p5[:1] + [("ff" if p5[1][:2] != "ff" else "00") + p5[1][2:]] + p5[2:]
incl("inclusion-refuse-index-eq-size", "spec", 7, 7, l5, p5, r7, NO)
incl("inclusion-refuse-index-above-size", "spec", 7, 9, l5, p5, r7, NO)
incl("inclusion-refuse-proof-short", "spec", 7, 5, l5, p5[:-1], r7, NO)
incl("inclusion-refuse-proof-long", "spec", 7, 5, l5, p5 + [p5[0]], r7, NO)
incl("inclusion-refuse-tampered-proof", "spec", 7, 5, l5, flip, r7, NO)
incl("inclusion-refuse-wrong-leaf", "spec", 7, 5, lh(d7[4]).hex(), p5, r7, NO)
incl("inclusion-refuse-leaf-hash-31-bytes", "spec", 7, 5, l5[:62], p5, r7, NO,
     spec(RFC + " §2.1", "hashes are SHA-256 outputs (32 bytes)"))
incl("inclusion-refuse-proof-hash-31-bytes", "spec", 7, 5, l5, [p5[0][:62]] + p5[1:], r7, NO,
     spec(RFC + " §2.1", "hashes are SHA-256 outputs (32 bytes)"))
INTP = profile("S1_DESIGN §1.1", "integers in a frozen structure are type(x) is int, 0 <= x <= 2^63-1",
               rationale="a bool or a negative is not a tree index; Go's uint64 cannot express either")
incl("inclusion-refuse-index-true", "no_upstream", 7, True, l5, p5, r7, NO, INTP)
incl("inclusion-refuse-index-negative", "no_upstream", 7, -1, l5, p5, r7, NO, INTP)


def cons(vid, kind, m, n, proof, old, new, levain, authority=CONS):
    inp = {"m": m, "n": n, "proof": proof, "old_root": old, "new_root": new}
    V.append(vector(vid, kind, "consistency", inp, levain, authority, "merkle" if kind != "no_upstream" else None))


for n in range(1, 9):
    d = leaves(n)
    for m in range(1, n + 1):
        pr = [] if m == n else [p.hex() for p in subproof(m, d, True)]
        cons(f"consistency-{m}-{n}", "spec", m, n, pr, mth(d[:m]).hex(), mth(d).hex(), OK,
             CONS if m < n else spec(WIT, "If the old size matches the checkpoint size, the witness MUST check "
                                          "that the root hashes are also identical"))
d1000 = leaves(1000)
r1000 = mth(d1000).hex()
for m in (1, 511, 512, 513, 999):
    cons(f"consistency-{m}-1000", "spec", m, 1000, [p.hex() for p in subproof(m, d1000, True)],
         mth(d1000[:m]).hex(), r1000, OK)

ZERO = spec(WIT, "If the old size is zero, the consistency proof MUST be empty")
ZROOT = spec(WIT, "A checkpoint of size zero MUST have the root hash of the empty tree, which [RFC 6962], "
                  "Section 2.1 defines as the hash of the empty string")
EQ = spec(WIT, "If the old size matches the checkpoint size, the witness MUST check that the root hashes are also identical")
d5 = leaves(5)
r5 = mth(d5).hex()
r3 = mth(d5[:3]).hex()
p35 = [p.hex() for p in subproof(3, d5, True)]
cons("consistency-0-0", "spec", 0, 0, [], EMPTY, EMPTY, OK, ZERO)
cons("consistency-0-5", "spec", 0, 5, [], EMPTY, r5, OK, ZERO)
cons("consistency-refuse-0-5-nonempty-proof", "spec", 0, 5, p35, EMPTY, r5, NO, ZERO)
cons("consistency-refuse-0-5-old-root-not-empty", "upstream_lax", 0, 5, [], r3, r5, NO, ZROOT)
cons("consistency-refuse-0-0-roots-not-empty", "upstream_lax", 0, 0, [], r5, r5, NO, ZROOT)
cons("consistency-refuse-5-5-nonempty-proof", "spec", 5, 5, p35, r5, r5, NO, EQ)
cons("consistency-refuse-5-5-roots-differ", "spec", 5, 5, [], r3, r5, NO, EQ)
cons("consistency-refuse-m-above-n", "spec", 5, 3, [], r5, r3, NO, CONS)
cons("consistency-refuse-empty-proof", "spec", 3, 5, [], r3, r5, NO, spec(RFC + " §2.1.4.2", "If consistency_path is an empty array, stop and fail the proof verification"))
cons("consistency-refuse-proof-short", "spec", 3, 5, p35[:-1], r3, r5, NO)
cons("consistency-refuse-proof-long", "spec", 3, 5, p35 + [p35[0]], r3, r5, NO)
cons("consistency-refuse-tampered-proof", "spec", 3, 5, [("ff" if p35[0][:2] != "ff" else "00") + p35[0][2:]] + p35[1:], r3, r5, NO)
cons("consistency-refuse-wrong-old-root", "spec", 3, 5, p35, mth(d5[:2]).hex(), r5, NO)
cons("consistency-refuse-wrong-new-root", "spec", 3, 5, p35, r3, mth(leaves(6)).hex(), NO)
cons("consistency-refuse-m-true", "no_upstream", True, 5, p35, r3, r5, NO, INTP)

write("F1", "merkle", V, spec_urls=[RFC, "https://www.rfc-editor.org/rfc/rfc6962", WIT], leaf_rule=RULE)
