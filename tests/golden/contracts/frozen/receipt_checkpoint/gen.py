"""Generate frozen/receipt_checkpoint/vectors.json (F10). stdlib + cryptography only.

Receipt logs are RFC 9162 trees over ``_genlib.leaves``-style data, roots and consistency proofs computed by
``_genlib``'s independent MTH/SUBPROOF, checkpoints signed as C2SP notes with key name = origin. Origins come from the
PROVISIONAL receipt production (provisional/receipt_origin/origins.json, P12); the checks do not depend on its form.
Each ``anchors`` vector walks leaves in team-log order; every expected verdict is written here from S1_DESIGN F10.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import (ACCEPT, REFUSE, UNVERIFIABLE, b64, canon_json, mth, note_sig, profile, pub, seed,  # noqa: E402
                     subproof, vector, vkey, write)

PROV = json.loads((Path(__file__).resolve().parents[2] / "provisional" / "receipt_origin" / "origins.json").read_text())
OA, OB = PROV["origins"]["device-a"], PROV["origins"]["device-b"]
KA, KB = seed(PROV["seed_labels"]["device-a"]), seed(PROV["seed_labels"]["device-b"])
VA, VB = vkey(OA, 1, pub(KA)), vkey(OB, 1, pub(KB))
WRONG = seed("receipt-log-wrong-key")
F10 = profile("S1_DESIGN F10 / Shadow Receipt §7", "the note verifies under the directory-bound key; old_size equals "
              "the same origin's previous anchor's size in team-log order (0 first); the proof verifies old -> new "
              "(F1, m == 0 and m == n included); equal size + different root (a fork) and a size below the previous "
              "anchor are refused", rationale="levain's own anchor rule; SR §7.1 asks S1 for it")
BOUND = profile("S1_DESIGN F10 rev 4 V11", "an anchored checkpoint's size is at most 2^53-1",
                rationale="the next anchor's old_size is a JSON-leaf integer (F8: at most 2^53-1)")
SHAPE = profile("S1_DESIGN F10 / F8", "a (receipt_checkpoint, 1) leaf has exactly leaf, v, checkpoint, old_size, "
                "consistency; old_size is an F8 integer; each proof line is canonical base64 of 32 bytes",
                rationale="one leaf object has one leaf hash")
REG = profile("S1_DESIGN F8", "an unknown (leaf, v) is refused at ingest and UNVERIFIABLE at verify",
              rationale="an old levain never calls a newer leaf tampering")


def data(n, salt=b""):
    return [salt + bytes([i % 256]) * (i + 1) for i in range(n)]


D = data(20)
FORK = data(20, salt=b"fork")


def cp_note(origin, size, root, sd, ext=""):
    text = f"{origin}\n{size}\n{b64(root)}\n{ext}"
    return text + "\n" + note_sig(text, origin, sd)


def leaf(origin, size, old, proof, sd=None, root=None, tree=D, ext="", **over):
    sd = sd or (KA if origin == OA else KB)
    root = root if root is not None else mth(tree[:size])
    obj = {"leaf": "receipt_checkpoint", "v": 1, "checkpoint": cp_note(origin, size, root, sd, ext),
           "old_size": old, "consistency": [b64(p) for p in proof]}
    obj.update(over)
    return canon_json(obj).encode("utf-8")


def proof(m, n, tree=D):
    return [] if m == 0 or m == n else subproof(m, tree[:n], True)


V = []


def anchors(vid, leaves, verdicts, authority=F10, at="ingest", first_unverifiable=None):
    lv = {"verdict": verdicts[-1], "verdicts": verdicts}
    if first_unverifiable is not None:
        lv["first_unverifiable"] = first_unverifiable
    V.append(vector(vid, "no_upstream", "anchors",
                    {"at": at, "leaves": [{"data_hex": d.hex(), "vkey": k} for d, k in leaves]}, lv, authority, None))


A5 = leaf(OA, 5, 0, [])
A9 = leaf(OA, 9, 5, proof(5, 9))
anchors("first-anchor", [(A5, VA)], [ACCEPT])
anchors("second-anchor-with-proof", [(A5, VA), (A9, VA)], [ACCEPT, ACCEPT])
anchors("second-anchor-with-proof-at-verify", [(A5, VA), (A9, VA)], [ACCEPT, ACCEPT], at="verify")
anchors("equal-size-same-root-re-anchor-accepted", [(A5, VA), (A9, VA), (leaf(OA, 9, 9, []), VA)],
        [ACCEPT, ACCEPT, ACCEPT])
anchors("equal-size-different-root-refused", [(A5, VA), (leaf(OA, 5, 5, [], root=mth(FORK[:5])), VA)],
        [ACCEPT, REFUSE])
anchors("wrong-old-size-refused", [(A5, VA), (leaf(OA, 9, 4, proof(4, 9)), VA)], [ACCEPT, REFUSE])
anchors("first-anchor-old-size-not-zero-refused", [(leaf(OA, 9, 5, proof(5, 9)), VA)], [REFUSE])
# old_size alone is wrong; the proof is the valid one from the previous anchor (only the old_size rule refuses these)
anchors("old-size-wrong-with-the-valid-proof-refused", [(A5, VA), (leaf(OA, 9, 4, proof(5, 9)), VA)], [ACCEPT, REFUSE])
anchors("first-anchor-old-size-not-zero-empty-proof-refused", [(leaf(OA, 5, 3, []), VA)], [REFUSE])
anchors("forked-second-anchor-refused", [(A5, VA), (leaf(OA, 9, 5, proof(5, 9, FORK), tree=FORK), VA)],
        [ACCEPT, REFUSE])
anchors("truncated-anchor-refused", [(A5, VA), (A9, VA), (leaf(OA, 5, 9, []), VA)], [ACCEPT, ACCEPT, REFUSE])
anchors("a-refused-anchor-moves-nothing",
        [(A5, VA), (leaf(OA, 9, 5, proof(5, 9), sd=WRONG), VA), (A9, VA)], [ACCEPT, REFUSE, ACCEPT])
anchors("two-origins-anchor-independently",
        [(A5, VA), (leaf(OB, 7, 0, []), VB), (A9, VA), (leaf(OB, 12, 7, proof(7, 12)), VB)],
        [ACCEPT, ACCEPT, ACCEPT, ACCEPT])
anchors("size-2-53-minus-1-accepted", [(leaf(OA, 2**53 - 1, 0, [], root=b"\x07" * 32), VA)], [ACCEPT], BOUND)
anchors("size-2-53-refused", [(leaf(OA, 2**53, 0, [], root=b"\x07" * 32), VA)], [REFUSE], BOUND)
anchors("note-signed-by-another-key-refused", [(leaf(OA, 5, 0, [], sd=WRONG), VA)], [REFUSE])
anchors("checkpoint-extension-line-refused", [(leaf(OA, 5, 0, [], ext="x\n"), VA)], [REFUSE])
anchors("old-zero-with-a-proof-refused", [(leaf(OA, 5, 0, proof(2, 5)), VA)], [REFUSE])
anchors("proof-one-hash-short-refused", [(A5, VA), (leaf(OA, 9, 5, proof(5, 9)[:-1]), VA)], [ACCEPT, REFUSE])
pr = [b64(p) for p in proof(5, 9)]
assert pr[0].endswith("=")
pad_bits = pr[0][:-2] + chr(ord(pr[0][-2]) + 1) + "="
assert __import__("base64").b64decode(pad_bits) == __import__("base64").b64decode(pr[0])
anchors("proof-line-non-canonical-base64-refused", [(A5, VA), (leaf(OA, 9, 5, [], consistency=[pad_bits] + pr[1:]), VA)],
        [ACCEPT, REFUSE], SHAPE)
anchors("proof-line-31-bytes-refused", [(A5, VA), (leaf(OA, 9, 5, [], consistency=[b64(b"\x01" * 31)] + pr[1:]), VA)],
        [ACCEPT, REFUSE], SHAPE)
anchors("extra-key-refused", [(leaf(OA, 5, 0, [], n=1), VA)], [REFUSE], SHAPE)
anchors("old-size-true-refused", [(leaf(OA, 1, 0, [], old_size=True), VA)], [REFUSE], SHAPE)
anchors("old-size-2-53-refused", [(leaf(OA, 5, 0, [], old_size=2**53), VA)], [REFUSE], SHAPE)
anchors("consistency-not-a-list-refused", [(leaf(OA, 5, 0, [], consistency=""), VA)], [REFUSE], SHAPE)
anchors("v2-refused-at-ingest", [(leaf(OA, 5, 0, [], v=2), VA)], [REFUSE], REG)
anchors("v2-unverifiable-at-verify", [(leaf(OA, 5, 0, [], v=2), VA)], [UNVERIFIABLE], REG, at="verify")

# rev 11: at verify an UNVERIFIABLE anchor leaf poisons every later leaf of its origin; an unknown (leaf, v) has no
# origin levain can read, so it poisons every origin. Ingest is unchanged.
CASCADE = profile("S1_DESIGN F10 rev 11", "at verify, once a receipt_checkpoint leaf is UNVERIFIABLE every later leaf "
                  "of its origin is UNVERIFIABLE, naming the first; an unknown (leaf, v), whose origin levain cannot "
                  "read, poisons every origin",
                  rationale="the unknown leaf could have moved its origin's previous anchor; the rule is positional")
A5_V2 = leaf(OA, 5, 0, [], v=2)
anchors("v2-then-a-dependent-anchor-unverifiable-at-verify", [(A5_V2, VA), (A5, VA), (A9, VA)],
        [UNVERIFIABLE, UNVERIFIABLE, UNVERIFIABLE], CASCADE, at="verify", first_unverifiable=0)
anchors("v2-then-another-origin-unverifiable-at-verify", [(A5, VA), (A5_V2, VA), (leaf(OB, 7, 0, []), VB)],
        [ACCEPT, UNVERIFIABLE, UNVERIFIABLE], CASCADE, at="verify", first_unverifiable=1)
anchors("v2-then-a-dependent-anchor-at-ingest-unchanged", [(A5_V2, VA), (A5, VA), (A9, VA)],
        [REFUSE, ACCEPT, ACCEPT], CASCADE)

write("F10", "receipt_checkpoint", V, provisional_origins="provisional/receipt_origin/origins.json")
