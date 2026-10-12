"""Generate frozen/grammar/vectors.json (§1.1: ASCII decimal, strict base64, the integer domain). stdlib only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, profile, spec, vector, write  # noqa: E402

CP = "C2SP tlog-checkpoint@v1.1.0"
NOTE = "C2SP signed-note@v1.1.0"
DEC = spec(CP, "A non-negative integer encoded as an ASCII decimal is a string containing the base-10 representation "
               "of the integer with no extra leading zeros. Zero is encoded as `0`, not the empty string.")
B64 = spec(NOTE, "Encoders MUST generate canonical base64 according to RFC 4648, Section 3.5, and decoders MUST "
                 "reject non-canonical encodings.")
NOTE_MAX = profile("S1_DESIGN §1.1", "sizes, indices and timestamps in note text are 0 <= x <= 2^63-1",
                   rationale="RFC 9162 serialises uint64 and Go's tlog uses int64; 2^63-1 is representable by both")
JSON_MAX = profile("S1_DESIGN §1.1 / F8", "integers inside a JSON leaf are 0 <= x <= 2^53-1",
                   rationale="the largest integer an IEEE double holds exactly, so every JSON reader agrees")
TYPE = profile("S1_DESIGN §1.1", "an integer in a frozen structure is type(x) is int, so True is refused",
               rationale="a JSON or Python boolean is not a size")
V = []


def dec(vid, kind, text, value, authority=DEC):
    lv = {"verdict": ACCEPT, "value": value} if value is not None else {"verdict": REFUSE}
    V.append(vector(f"decimal-{vid}", kind, "decimal", {"text": text}, lv, authority, "decimal"))


dec("zero", "spec", "0", 0)
dec("one", "spec", "1", 1)
dec("max-note", "spec", "9223372036854775807", 2**63 - 1)
dec("leading-zero", "upstream_lax", "01", None)
dec("double-zero", "upstream_lax", "00", None)
dec("plus-sign", "spec", "+1", None)
dec("leading-space", "spec", " 1", None)
dec("underscore", "spec", "1_0", None)
dec("fullwidth-digits", "spec", "１２", None)
dec("negative", "spec", "-1", None)
dec("empty", "spec", "", None)
dec("trailing-newline", "spec", "1\n", None)
dec("above-note-max", "levain_profile", "9223372036854775808", None, NOTE_MAX)
dec("above-uint64", "spec", "18446744073709551616", None,
    spec("RFC 9162 §4.9 (https://www.rfc-editor.org/rfc/rfc9162)", "TreeHeadSignature: uint64 tree_size"))


def b64(vid, kind, text, raw):
    lv = {"verdict": ACCEPT, "bytes": raw} if raw is not None else {"verdict": REFUSE}
    V.append(vector(f"b64-{vid}", kind, "b64", {"text": text}, lv, B64, "b64"))


b64("one-byte", "spec", "AQ==", "01")
b64("three-bytes", "spec", "AAAA", "000000")
b64("empty", "spec", "", "")
b64("nonzero-pad-bits", "upstream_lax", "AB==", None)
b64("embedded-newline", "upstream_lax", "AA\nAA==", None)
b64("embedded-cr", "upstream_lax", "AA\rAA==", None)
b64("unpadded", "spec", "AAA", None)
b64("url-alphabet", "spec", "-_8=", None)
b64("pad-in-middle", "spec", "AA==AA==", None)
b64("space", "spec", "AA AA===", None)


def num(vid, value, domain, ok, authority):
    lv = {"verdict": ACCEPT if ok else REFUSE}
    V.append(vector(f"int-{vid}", "no_upstream", "int", {"value": value, "domain": domain}, lv, authority, None))


num("note-max", 2**63 - 1, "note", True, NOTE_MAX)
num("note-above-max", 2**63, "note", False, NOTE_MAX)
num("json-max", 2**53 - 1, "json", True, JSON_MAX)
num("json-above-max", 2**53, "json", False, JSON_MAX)
num("zero", 0, "note", True, NOTE_MAX)
num("negative-note", -1, "note", False, NOTE_MAX)
num("negative-json", -1, "json", False, JSON_MAX)
num("true-note", True, "note", False, TYPE)
num("true-json", True, "json", False, TYPE)
num("false-note", False, "note", False, TYPE)
num("float-note", 1.0, "note", False, TYPE)
num("string-note", "1", "note", False, TYPE)

write("§1.1", "grammar", V)
