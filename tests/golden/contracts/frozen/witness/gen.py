"""Generate frozen/witness/vectors.json (F5, tlog-witness@v1.1.0 add-checkpoint request body). stdlib + cryptography.

Every expected body is written out as text here, line by line, never produced by the encoder under test.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, b64, leaves, mth, note_sig, seed, spec, subproof, vector, write  # noqa: E402

TW = "C2SP tlog-witness@v1.1.0"
BODY = spec(TW, "The request body MUST be a sequence of an old size line, zero or more consistency proof lines, and an "
                "empty line, followed by a [checkpoint]. Each line MUST terminate in a newline character (U+000A).")
V = []


def add(vid, kind, old, proof, cp, body, authority=BODY):
    lv = {"verdict": ACCEPT, "body": body} if body is not None else {"verdict": REFUSE}
    V.append(vector(vid, kind, "add_checkpoint", {"old_size": old, "proof": proof, "checkpoint": cp}, lv, authority,
                    "witness" if kind != "no_upstream" else None))


# The spec's own example, transcribed from the spec text.
EX_PROOF_B64 = ["PlRNCrwHpqhGrupue0L7gxbjbMiKA9temvuZZDDpkaw=", "jrJZDmY8Y7SyJE0MWLpLozkIVMSMZcD5kvuKxPC3swk=",
                "5+pKlUdi2LeF/BcMHBn+Ku6yhPGNCswZZD1X/6QgPd8=", "/6WVhPs2CwSsb5rYBH5cjHV/wSmA79abXAwhXw3Kj/0="]
EX_CP = ("example.com/behind-the-sofa\n20852163\nCsUYapGGPo4dkMgIAUqom/Xajj7h2fB2MPA3j2jxq2I=\n\n"
         "— example.com/behind-the-sofa Az3grlgtzPICa5OS8npVmf1Myq/5IZniMp+ZJurmRDeOoRDe4URYN7u5/Zhcyv2q1gGzGku9nTo+zyWE+xeMcTOAYQ8=\n"
         "— example.com/behind-the-sofa opLqBQsREYCgu6xQkYQwJr9fo45a62DN9EdmHXnZdXNqlcVGlCum2Wks+49/V6267UEjw6QUXTS5Rovnzv++qbSzm9Q=\n")
EX_BODY = ("old 20852014\n"
           "PlRNCrwHpqhGrupue0L7gxbjbMiKA9temvuZZDDpkaw=\n"
           "jrJZDmY8Y7SyJE0MWLpLozkIVMSMZcD5kvuKxPC3swk=\n"
           "5+pKlUdi2LeF/BcMHBn+Ku6yhPGNCswZZD1X/6QgPd8=\n"
           "/6WVhPs2CwSsb5rYBH5cjHV/wSmA79abXAwhXw3Kj/0=\n"
           "\n" + EX_CP)
import base64  # noqa: E402
add("spec-example", "spec", 20852014, [base64.b64decode(x).hex() for x in EX_PROOF_B64], EX_CP, EX_BODY,
    spec(TW, "Example request body"))

ORIGIN = "levain-team/" + "0123456789abcdef" * 2
LOG = seed("log-team")
d = leaves(9)


def cp(n):
    text = f"{ORIGIN}\n{n}\n{b64(mth(d[:n]))}\n"
    return text + "\n" + note_sig(text, ORIGIN, LOG)


add("old-size-0", "spec", 0, [], cp(9), "old 0\n\n" + cp(9))
add("old-equals-new", "spec", 9, [], cp(9), "old 9\n\n" + cp(9))
p = subproof(5, d, True)
add("old-5-new-9", "spec", 5, [x.hex() for x in p], cp(9), "old 5\n" + "".join(b64(x) + "\n" for x in p) + "\n" + cp(9))
add("old-size-0-with-proof-refused", "no_upstream", 0, [p[0].hex()], cp(9), None,
    spec(TW, "If the old size is zero, the consistency proof MUST be empty"))
add("old-above-new-refused", "no_upstream", 10, [], cp(9), None,
    spec(TW, "The old size MUST be equal to or lower than the checkpoint size."))
add("sixty-four-proof-lines-refused", "no_upstream", 5, [p[0].hex()] * 64, cp(9), None,
    spec(TW, "The client MUST NOT send more than 63 consistency proof lines."))
add("proof-hash-31-bytes-refused", "no_upstream", 5, [p[0].hex()[:62]], cp(9), None,
    spec(TW, "Each consistency proof line MUST encode a single hash in base64."))

write("F5", "witness", V)
