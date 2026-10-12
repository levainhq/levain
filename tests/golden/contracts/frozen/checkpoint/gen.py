"""Generate frozen/checkpoint/vectors.json (F3, tlog-checkpoint@v1.1.0 + levain's profile). stdlib + cryptography."""
import base64
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import (ACCEPT, REFUSE, b64, kid, note_sig, profile, pub, seed, sig_line, spec,  # noqa: E402
                     vector, vkey, write)

CPS = "C2SP tlog-checkpoint@v1.1.0"
TEXT = spec(CPS, "The note text of a checkpoint is a sequence of at least three non-empty lines ... origin ... tree "
                 "size ... encoded as an ASCII decimal ... root hash, the base64 encoding of the root of the [RFC 6962] "
                 "Merkle hash tree")
DEC = spec(CPS, "A non-negative integer encoded as an ASCII decimal is a string containing the base-10 representation "
                "of the integer with no extra leading zeros.")
B64 = spec(CPS, "decoders MUST reject non-canonical encodings")
DUP = spec(CPS, "there MUST NOT be multiple signature lines with both the same key name and the same key id")
MAYDUP = spec(CPS, "There MAY be multiple signature lines with the same key name.")
EXT = profile("S1_DESIGN F3", "a levain-origin checkpoint carries zero extension lines",
              quote="The use of extension lines is NOT RECOMMENDED")
KEYNAME = profile("S1_DESIGN F3", "the log's key name equals the origin", quote="The key name SHOULD be the log origin.")
PREFIX = profile("S1_DESIGN F3/F6", "a verifier configured for a levain log refuses an origin without a frozen prefix",
                 rationale="a levain verifier reads only levain logs; the prefix is how it knows one")
TEAM = profile("S1_DESIGN F3/F6", "a team-log verifier requires exactly levain-team/<32 lowercase hex>",
               rationale="the frozen team production; any other form is not a levain team log")
NOTE_MAX = profile("S1_DESIGN §1.1", "a note-text size is at most 2^63-1",
                   rationale="RFC 9162 serialises uint64 and Go's tlog uses int64")

TEAM_ID = "0123456789abcdef" * 2
ORIGIN = "levain-team/" + TEAM_ID
LOG = seed("log-team")
EMPTY = hashlib.sha256(b"").digest()
R = hashlib.sha256(b"golden root").digest()
V = []


def signed(text, name=ORIGIN, sd=LOG):
    return text + "\n" + note_sig(text, name, sd)


def add(vid, kind, note, levain, authority, *, name=ORIGIN, sd=LOG, team=True):
    V.append(vector(vid, kind, "verify_checkpoint", {"note": note, "log_vkey": vkey(name, 1, pub(sd)), "team": team},
                    levain, authority, "checkpoint"))


def ok(origin, size, root):
    return {"verdict": ACCEPT, "origin": origin, "size": size, "root": root.hex()}


NO = {"verdict": REFUSE}
for size, root in ((0, EMPTY), (1, R), (2**20 + 1, R)):
    add(f"team-size-{size}", "spec", signed(f"{ORIGIN}\n{size}\n{b64(root)}\n"), ok(ORIGIN, size, root), TEXT)
add("size-max-note-int", "spec", signed(f"{ORIGIN}\n{2**63 - 1}\n{b64(R)}\n"), ok(ORIGIN, 2**63 - 1, R), NOTE_MAX)
add("size-above-max-note-int", "levain_profile", signed(f"{ORIGIN}\n{2**63}\n{b64(R)}\n"), NO, NOTE_MAX)
for vid, s, kind in (("leading-zero", "01", "upstream_lax"), ("negative", "-1", "spec"), ("plus", "+1", "spec"),
                     ("space", " 1", "spec"), ("underscore", "1_0", "spec"), ("fullwidth", "１", "spec")):
    add(f"size-{vid}-refused", kind, signed(f"{ORIGIN}\n{s}\n{b64(R)}\n"), NO, DEC)
add("extension-line-refused", "levain_profile", signed(f"{ORIGIN}\n1\n{b64(R)}\nan extension\n"), NO, EXT)
add("root-31-bytes-refused", "upstream_lax", signed(f"{ORIGIN}\n1\n{b64(R[:31])}\n"), NO, TEXT)
canon = b64(R)
alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
noncanon = canon[:-2] + alphabet[alphabet.index(canon[-2]) | 1] + "="
assert noncanon != canon and base64.b64decode(noncanon) == R
add("root-non-canonical-base64-refused", "upstream_lax", signed(f"{ORIGIN}\n1\n{noncanon}\n"), NO, B64)

OTHER = "levain-team/" + "f" * 32
add("origin-not-key-name-refused", "levain_profile", signed(f"{OTHER}\n1\n{b64(R)}\n"), NO, KEYNAME)
for vid, origin in (("no-levain-prefix", "example.com/log"), ("prefix-only", "levain-team/")):
    add(f"origin-{vid}-refused", "levain_profile", signed(f"{origin}\n1\n{b64(R)}\n", origin), NO, PREFIX,
        name=origin, team=False)
for vid, origin in (("31-hex", "levain-team/" + "a" * 31), ("33-hex", "levain-team/" + "a" * 33),
                    ("uppercase-hex", "levain-team/" + "A" * 32), ("trailing-segment", ORIGIN + "/x"),
                    ("fleet-prefix", "levain-fleet/" + TEAM_ID)):
    add(f"team-verifier-{vid}-refused", "levain_profile", signed(f"{origin}\n1\n{b64(R)}\n", origin), NO, TEAM,
        name=origin)
add("fleet-origin-at-prefix-verifier", "spec", signed(f"levain-fleet/m1\n1\n{b64(R)}\n", "levain-fleet/m1"),
    ok("levain-fleet/m1", 1, R), PREFIX, name="levain-fleet/m1", team=False)

text = f"{ORIGIN}\n7\n{b64(R)}\n"
line = note_sig(text, ORIGIN, LOG)
k = kid(ORIGIN, 1, pub(LOG))
add("duplicate-identical-line-refused", "upstream_lax", text + "\n" + line + line, NO, DUP)
add("duplicate-name-and-id-different-bytes-refused", "upstream_lax",
    text + "\n" + line + sig_line(ORIGIN, k, bytes(64)), NO, DUP)
other_id = b"\x00\x00\x00\x00" if k != b"\x00\x00\x00\x00" else b"\x01" * 4
add("same-name-different-id-accepted", "spec", text + "\n" + line + sig_line(ORIGIN, other_id, bytes(64)),
    ok(ORIGIN, 7, R), MAYDUP)
add("different-name-same-id-accepted", "spec", text + "\n" + line + sig_line("golden.example/w", k, bytes(64)),
    ok(ORIGIN, 7, R), spec(CPS, "MUST NOT be multiple signature lines with both the same key name and the same key id "
                                "(a different name with the same ID is a different key)"))

write("F3", "checkpoint", V, team_origin=ORIGIN)
