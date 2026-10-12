"""Generate frozen/origin/vectors.json (F6: origin grammar, the team production, the log kinds). stdlib only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, profile, spec, vector, write  # noqa: E402

NAME = spec("C2SP tlog-cosignature@v1.1.0", "A cosigner name is a non-empty UTF-8 string, at most 255 bytes long and "
                                            "containing no Unicode spaces, plus characters (U+002B), or control characters")
GRAMMAR = profile("S1_DESIGN F6", "ASCII [a-z0-9][a-z0-9._-]* segments joined by '/', at most 255 bytes",
                  rationale="a subset of the C2SP name rule chosen so every levain origin is a legal note key name")
PREFIX = profile("S1_DESIGN F6", "a levain origin carries levain-team/, levain-fleet/ or levain-receipt/",
                 rationale="frozen prefixes; a new prefix is a version, never an edit")
TEAM = profile("S1_DESIGN F6", "the team production is exactly levain-team/<32 lowercase hex>",
               rationale="128 random bits chosen at genesis")
HEX = "0123456789abcdef" * 2
V = []


def add(vid, check, origin, ok, authority, kind_out=None):
    lv = {"verdict": ACCEPT if ok else REFUSE}
    if ok and kind_out:
        lv["log_kind"] = kind_out
    V.append(vector(vid, "no_upstream", "origin", {"origin": origin, "check": check}, lv, authority, None))


add("grammar-simple", "grammar", "a", True, GRAMMAR)
add("grammar-all-classes", "grammar", "a0.b-c_d/e9", True, GRAMMAR)
add("grammar-255-bytes", "grammar", "a" * 255, True, NAME)
add("grammar-256-bytes-refused", "grammar", "a" * 256, False, NAME)
add("grammar-plus-refused", "grammar", "levain-team/a+b", False, NAME)
add("grammar-space-refused", "grammar", "levain-team/a b", False, NAME)
add("grammar-uppercase-refused", "grammar", "levain-team/ABC", False, GRAMMAR)
add("grammar-ssh-fingerprint-refused", "grammar", "levain-receipt/t/SHA256:u5DmLEHQQnGsZJ3QJ0J5w0e2mJiCixbt0bK3Wlv1xGM",
    False, GRAMMAR)
add("grammar-empty-refused", "grammar", "", False, NAME)
add("grammar-empty-segment-refused", "grammar", "a//b", False, GRAMMAR)
add("grammar-trailing-slash-refused", "grammar", "a/", False, GRAMMAR)
add("grammar-leading-dot-refused", "grammar", ".a", False, GRAMMAR)
add("grammar-non-ascii-refused", "grammar", "levain-team/é", False, GRAMMAR)
add("kind-team", "log_kind", "levain-team/" + HEX, True, PREFIX, "team")
add("kind-fleet", "log_kind", "levain-fleet/m1", True, PREFIX, "fleet")
add("kind-receipt", "log_kind", "levain-receipt/t1/" + "ab" * 32, True, PREFIX, "receipt")
add("kind-other-prefix-refused", "log_kind", "levain-other/x", False, PREFIX)
add("kind-no-prefix-refused", "log_kind", "example.com/log", False, PREFIX)
add("kind-bare-prefix-refused", "log_kind", "levain-team", False, PREFIX)
add("team-production", "team", "levain-team/" + HEX, True, TEAM)
add("team-31-hex-refused", "team", "levain-team/" + HEX[:31], False, TEAM)
add("team-33-hex-refused", "team", "levain-team/" + HEX + "0", False, TEAM)
add("team-uppercase-refused", "team", "levain-team/" + HEX.upper(), False, TEAM)
add("team-trailing-segment-refused", "team", "levain-team/" + HEX + "/x", False, TEAM)
add("team-non-hex-refused", "team", "levain-team/" + "g" * 32, False, TEAM)

write("F6", "origin", V)
