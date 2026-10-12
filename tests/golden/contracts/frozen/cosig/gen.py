"""Generate frozen/cosig/vectors.json (F4, tlog-cosignature@v1.1.0 Ed25519 cosignature/v1). stdlib + cryptography."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import (ACCEPT, REFUSE, b64, cosig_line, kid, mth, leaves, note_sig, profile, pub, seed,  # noqa: E402
                     sig_line, spec, subproof, vector, vkey, write)

CS = "C2SP tlog-cosignature@v1.1.0"
MSG = spec(CS, "The signature value is computed according to [RFC 8032] over the concatenation of the following lines. "
               "Each line is terminated by a newline (U+000A): cosignature/v1; time <ASCII decimal>; the log origin; "
               "the tree size; the base64 encoding of the tree hash; zero or more extension lines")
FUTURE = profile("S1_DESIGN F4", "a cosignature timestamped above now + 600 is refused; now is an argument",
                 quote="Verifiers MAY reject checkpoint cosignatures with timestamps in the future.")
TYPE = profile("S1_DESIGN F4", "a cosigner is configured with a type 0x04 key; a 0x01 vkey is refused",
               rationale="one key, one signature type: Go's NewVerifierForCosignatureV1 silently re-reads a 0x01 vkey "
                         "as 0x04, so the configured key ID and the verified one differ")
BAD = spec("C2SP signed-note@v1.1.0", "If a signature from a known key fails to verify, clients SHOULD reject the whole "
                                      "note. (taken by levain, as in F2)")
EQ = spec("C2SP tlog-witness@v1.1.0", "If the old size matches the checkpoint size, the witness MUST check that the root "
                                      "hashes are also identical")

SOFA = "example.com/behind-the-sofa"
LOGSOFA = seed("log-sofa")
TS = 1679315147
SOFA_TEXT = f"{SOFA}\n20852163\nCsUYapGGPo4dkMgIAUqom/Xajj7h2fB2MPA3j2jxq2I=\n"
# hand-written from the spec's example signature input, not computed:
SOFA_MSG = ("cosignature/v1\ntime 1679315147\nexample.com/behind-the-sofa\n20852163\n"
            "CsUYapGGPo4dkMgIAUqom/Xajj7h2fB2MPA3j2jxq2I=\n")
W = [(f"witness.example/w{i}", seed(f"cosig-{i}")) for i in (1, 2, 3)]
V = []


def add(vid, kind, note, trusted, now, levain, authority, extra=None):
    inp = {"note": note, "trusted": trusted, "now": now}
    if extra:
        inp.update(extra)
    V.append(vector(vid, kind, "verify_cosignatures", inp, levain, authority, "cosig"))


def recs(*items):
    return {"verdict": ACCEPT, "records": [{"cosigner_name": n, "key_id": kid(n, 4, pub(s)).hex(), "timestamp": t}
                                           for n, s, t in items]}


def tv(n, s):
    return vkey(n, 4, pub(s))


log_line = note_sig(SOFA_TEXT, SOFA, LOGSOFA)
n1, s1 = W[0]
add("spec-example-input", "spec", SOFA_TEXT + "\n" + log_line + cosig_line(SOFA_TEXT, n1, s1, TS), [tv(n1, s1)], TS,
    recs((n1, s1, TS)), MSG, {"signed_message": SOFA_MSG})

ORIGIN = "levain-team/" + "0123456789abcdef" * 2
LOG = seed("log-team")
R = mth(leaves(9))
TEXT = f"{ORIGIN}\n9\n{b64(R)}\n"
NOW = 1760000000
base = TEXT + "\n" + note_sig(TEXT, ORIGIN, LOG)
three = base + "".join(cosig_line(TEXT, n, s, NOW - i) for i, (n, s) in enumerate(W))
add("three-cosignatures", "spec", three, [tv(n, s) for n, s in W], NOW,
    recs(*[(n, s, NOW - i) for i, (n, s) in enumerate(W)]), MSG)
add("timestamp-now-plus-600-accepted", "spec", base + cosig_line(TEXT, n1, s1, NOW + 600), [tv(n1, s1)], NOW,
    recs((n1, s1, NOW + 600)), FUTURE)
add("timestamp-now-plus-601-refused", "levain_profile", base + cosig_line(TEXT, n1, s1, NOW + 601), [tv(n1, s1)], NOW,
    {"verdict": REFUSE}, FUTURE)
add("type-04-signature-under-type-01-vkey-refused", "levain_profile", base + cosig_line(TEXT, n1, s1, NOW),
    [vkey(n1, 1, pub(s1))], NOW, {"verdict": REFUSE}, TYPE)
bad = sig_line(n1, kid(n1, 4, pub(s1)), NOW.to_bytes(8, "big") + bytes(64))
add("trusted-cosigner-bad-signature-refused", "spec", base + bad, [tv(n1, s1)], NOW, {"verdict": REFUSE}, BAD)
other = base + cosig_line(TEXT, n1, s1, NOW) + cosig_line(TEXT, "witness.example/unknown", seed("cosig-x"), NOW)
add("unknown-cosigner-ignored", "spec", other, [tv(n1, s1)], NOW, recs((n1, s1, NOW)),
    spec("C2SP signed-note@v1.1.0", "Verifiers MUST ignore signatures from unknown keys"))


def adv(vid, kind, held, new, proof, ok, authority):
    V.append(vector(vid, kind, "check_advance", {"held": held, "new": new, "proof": [p.hex() for p in proof]},
                    {"verdict": ACCEPT if ok else REFUSE}, authority, "merkle"))


d = leaves(9)
t = lambda n: f"{ORIGIN}\n{n}\n{b64(mth(d[:n]))}\n"  # noqa: E731
CONS = spec("RFC 9162 §2.1.4.2", "consistency proof verification")
adv("advance-consistent-accepted", "spec", t(5), t(9), subproof(5, d, True), True, CONS)
adv("advance-same-size-same-root-accepted", "spec", t(9), t(9), [], True, EQ)
adv("advance-same-size-different-root-refused", "spec", t(9), f"{ORIGIN}\n9\n{b64(hashlib.sha256(b'fork').digest())}\n",
    [], False, EQ)
adv("advance-smaller-refused", "spec", t(9), t(5), [], False,
    spec("C2SP tlog-cosignature@v1.1.0", "checkpoint cosignatures MUST be monotonically increasing"))
adv("advance-inconsistent-refused", "spec", t(5), f"{ORIGIN}\n9\n{b64(hashlib.sha256(b'fork').digest())}\n",
    subproof(5, d, True), False, CONS)

write("F4", "cosig", V)
