"""Generate frozen/note/vectors.json (F2 signed notes; F13's note-side goldens). stdlib + cryptography only."""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import (ACCEPT, REFUSE, b64, kid, note_sig, profile, pub, seed, sig_line, spec,  # noqa: E402
                     vector, vkey, write)

SN = "C2SP signed-note@v1.1.0"
FMT = spec(SN, "A signed note consists of a text ending in newline (U+000A), followed by a blank line (only a "
               "newline), followed by one or more signature lines of this form: em dash (U+2014), space (U+0020), "
               "key name, space, base64-encoded signature, newline.")
CTRL = spec(SN, "Signed notes MUST be valid UTF-8 and MUST NOT contain any ASCII control characters (those below "
                "U+0020) other than newline.")
UNKNOWN = spec(SN, "Verifiers MUST ignore signatures from unknown keys, even if they share a name or ID (but not "
                   "both) with a known key.")
BAD = spec(SN, "If a signature from a known key fails to verify, clients SHOULD reject the whole note. "
               "(levain takes the SHOULD; Go's sumdb/note returns InvalidSignatureError)")
NONE = spec(SN, "If no signature from a known key verifies successfully, clients MUST reject the note.")
NAME = spec(SN, "Key names MUST be non-empty and MUST NOT contain any Unicode spaces or plus (U+002B) characters.")
B64 = spec(SN, "Encoders MUST generate canonical base64 according to RFC 4648, Section 3.5, and decoders MUST reject "
               "non-canonical encodings.")
COUNT = profile("S1_DESIGN F2", "at most 16 signature lines, each at most 8 KiB (UTF-8 bytes, its newline excluded)",
                quote="Verifiers SHOULD apply a maximum limit to the number of signatures in a note (or, equivalently, "
                      "to the overall size of a note), but MUST accept at least up to 16 signatures.")
MAXLINE = 8 * 1024

A, B, C = seed("note-known-a"), seed("note-known-b"), seed("note-unknown-c")
NA, NB, NC = "golden.example/a", "golden.example/b", "golden.example/c"
VA, VB = vkey(NA, 1, pub(A)), vkey(NB, 1, pub(B))
TEXT = "levain golden note\nsecond line\n"
V = []


def add(vid, kind, note, known, verified, authority, *, raw=False):
    inp = {"note_hex": note.hex()} if raw else {"note": note}
    inp["known"] = known
    lv = {"verdict": ACCEPT, "verified": verified} if verified is not None else {"verdict": REFUSE}
    V.append(vector(vid, kind, "verify_note", inp, lv, authority, "note"))


ex_vkey = "example.com/foo+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k"
ex_note = ("This is an example message.\n\n— example.com/foo Uw2QOkn8srV1yJGh2VYRlL1Tnagv1YEq6TfXppzi2ONncAlTgK7"
           "Ztg1ERYNZXsYjOBH3mFXmRKuwHjG1Yu72IneyaQM=\n")
add("spec-example", "spec", ex_note, [ex_vkey], ["example.com/foo"], spec(SN, "§Verifier keys, Example"))

sa, sb, sc = note_sig(TEXT, NA, A), note_sig(TEXT, NB, B), note_sig(TEXT, NC, C)
add("three-signatures-one-unknown", "spec", TEXT + "\n" + sa + sc + sb, [VA, VB], [NA, NB], UNKNOWN)

junk = bytes(range(64))
name_only = sig_line(NA, b"\x00\x01\x02\x03" if kid(NA, 1, pub(A)) != b"\x00\x01\x02\x03" else b"\x09" * 4, junk)
add("name-only-match-ignored", "spec", TEXT + "\n" + name_only + sa, [VA], [NA], UNKNOWN)
id_only = sig_line("golden.example/other", kid(NA, 1, pub(A)), junk)
add("id-only-match-ignored", "spec", TEXT + "\n" + id_only + sa, [VA], [NA], UNKNOWN)


def big_unknown(i, nbytes):
    return sig_line(f"golden.example/pq{i}", bytes([i, 0, 0, 1]), bytes([i]) * nbytes)


lines16 = sa + "".join(big_unknown(i, 3700) for i in range(15))
assert all(4900 < len(x.encode()) < MAXLINE for x in lines16.splitlines()[1:])
add("sixteen-lines-pq-size", "spec", TEXT + "\n" + lines16, [VA], [NA],
    spec(SN, "MUST accept at least up to 16 signatures. Remember that post-quantum signatures can be up to nearly 5kB in size."))
add("seventeen-lines-refused", "levain_profile", TEXT + "\n" + sa + "".join(big_unknown(i, 64) for i in range(16)),
    [VA], None, COUNT)

# A line of exactly MAXLINE bytes (accepted) and one byte more (refused). Line = "— " + name + " " + base64.
def line_of(total, i):
    name = f"golden.example/len{i}"
    head = len("— ".encode()) + len(name) + 1
    for n in range(1, 7000):
        ln = sig_line(name, b"\x07\x07\x07\x07", b"\x05" * n).rstrip("\n")
        if len(ln.encode()) >= total - 3:
            break
    while len(ln.encode()) < total:                  # pad the name to land on the exact byte count
        name += "x"
        ln = sig_line(name, b"\x07\x07\x07\x07", b"\x05" * n).rstrip("\n")
    assert len(ln.encode()) == total, (len(ln.encode()), total, head)
    return ln + "\n"


add("line-exactly-8-kib", "spec", TEXT + "\n" + sa + line_of(MAXLINE, 1), [VA], [NA], COUNT)
add("line-over-8-kib-refused", "levain_profile", TEXT + "\n" + sa + line_of(MAXLINE + 1, 2), [VA], None, COUNT)

cr_text = "carriage\rreturn\n"
add("cr-in-text-refused", "spec", cr_text + "\n" + note_sig(cr_text, NA, A), [VA], None, CTRL)
nul_text = "nul\x00byte\n"
add("nul-in-text-refused", "spec", nul_text + "\n" + note_sig(nul_text, NA, A), [VA], None, CTRL)
add("missing-blank-line-refused", "spec", TEXT + sa, [VA], None, FMT)
add("plus-in-key-name-refused", "spec", TEXT + "\n" + sa + sig_line("golden.example/x+y", b"\x01\x02\x03\x04", junk),
    [VA], None, NAME)
raw = base64.b64decode(sa.split(" ")[-1])
canon = b64(raw)
assert canon.endswith("=") and not canon.endswith("==")
alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
last = canon[-2]
noncanon = canon[:-2] + alphabet[alphabet.index(last) | 1] + "="   # set a pad bit (68 bytes leave 2 spare bits)
assert noncanon != canon and base64.b64decode(noncanon) == raw
add("non-canonical-base64-refused", "upstream_lax", TEXT + "\n" + f"— {NA} {noncanon}\n", [VA], None, B64)
bad = sig_line(NA, kid(NA, 1, pub(A)), bytes(64))
add("known-key-bad-signature-refused", "spec", TEXT + "\n" + bad + sb, [VA, VB], None, BAD)
add("no-known-key-refused", "spec", TEXT + "\n" + sc, [VA], None, NONE)
add("signature-line-without-dash-refused", "spec", TEXT + "\n" + sa[2:], [VA], None, FMT)
add("invalid-utf8-refused", "spec", (TEXT.encode() + b"\xff\n" + b"\n" + sa.encode()), [VA], None, CTRL, raw=True)

# F13: an SSHSIG signed-data preamble is binary (length fields hold 0x00), so it can never be a note text.
pre = b"SSHSIG" + len(b"levain-team-entry").to_bytes(4, "big") + b"levain-team-entry"
pre_text = pre.decode("latin-1") + "\n"
add("sshsig-preamble-text-refused", "spec", pre_text + "\n" + note_sig(pre_text, NA, A), [VA], None,
    CTRL | {"clause": CTRL["clause"] + " (F13 cross-protocol: an SSHSIG preamble carries 0x00 bytes)"})

# F13: the ssh-agent round trip. tests/test_contracts_note.py signs TEXT through a throwaway ssh-agent holding
# this seed's key and requires these exact bytes (Ed25519 is deterministic).
AG = seed("agent-ed25519")
NAG = "golden.example/agent"
add("agent-roundtrip", "spec", TEXT + "\n" + note_sig(TEXT, NAG, AG), [vkey(NAG, 1, pub(AG))], [NAG],
    spec("S1_DESIGN F13", "the agent's string(\"ssh-ed25519\") || string(sig) is unwrapped to the 64 sig bytes"))

write("F2", "note", V, agent_seed_label="agent-ed25519")
