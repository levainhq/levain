"""Generate frozen/canon/vectors.json (F8: canonical JSON, parse refusals, leaf framing, chain_hash). stdlib only.

Every expected canonical byte string below is written by hand as a string literal, never produced by
``json.dumps``: the point of these vectors is to pin what that call emits.
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, UNVERIFIABLE, profile, vector, write  # noqa: E402

FORM = {"kind": "byte_derivation", "source": "S1_DESIGN F8",
        "clause": "json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False), UTF-8; keys sorted by "
                  "Unicode code point; '\"' and '\\\\' escaped; \\b \\f \\n \\r \\t short forms; other U+0000-U+001F "
                  "as lowercase \\u00xx; everything else raw"}
REF = profile("S1_DESIGN F8", "parse refuses duplicate keys, lone surrogates, floats, NaN/Infinity, |n| > 2^53-1, a BOM",
              rationale="json.loads accepts each of these (measured), and each gives one leaf two readings")
DEPTH = profile("S1_DESIGN F8", "nesting depth above 32 is refused, measured iteratively", rationale=(
    "no specification governs JSON nesting; an interpreter's recursion limit must never be the rule"))
CANON = profile("S1_DESIGN F8", "ingest and verify refuse unless canonical(parse(d)) == d byte for byte",
                rationale="one leaf object has one leaf hash")
REG = profile("S1_DESIGN F8", "one global registry of (leaf, v); an unknown (leaf, v) is refused at ingest and "
                              "UNVERIFIABLE at verify; a leaf on a log kind its entry does not list is refused",
              rationale="the same bytes never carry two meanings, and an old levain never calls a newer leaf tampering")
CHAIN = {"kind": "byte_derivation", "source": "S1_DESIGN F8 / levain team/entry.py:85-86 @ 31dea1f",
         "clause": "chain_hash(prev, entry) = lowercase hex SHA-256 of UTF-8(prev || canonical(entry without 'hash'))"}
V = []


def canon(vid, value, expected):
    V.append(vector(f"canonical-{vid}", "no_upstream", "canonical", {"value": value},
                    {"verdict": ACCEPT, "bytes_hex": expected.encode("utf-8").hex()}, FORM, None))


canon("code-point-key-order", {"b": 1, "a": 2, "é": 3, "z": 4, "\U0001F600": 5, "ｚ": 6},
      '{"a":2,"b":1,"z":4,"é":3,"ｚ":6,"\U0001F600":5}')
canon("escape-set", {"s": "\"\\\b\f\n\r\t\x00\x1f\x7f  <>&/"},
      '{"s":"\\"\\\\\\b\\f\\n\\r\\t\\u0000\\u001f\x7f  <>&/"}')
canon("emoji-value", {"v": "\U0001F35E"}, '{"v":"\U0001F35E"}')
canon("integers", {"n": 9007199254740991, "m": 0, "k": -5}, '{"k":-5,"m":0,"n":9007199254740991}')
canon("nesting-and-literals", {"f": False, "e": True, "c": [1, [2, {"d": None}]], "b": {}, "a": []},
      '{"a":[],"b":{},"c":[1,[2,{"d":null}]],"e":true,"f":false}')


def leaf(vid, data: bytes, at, verdict, authority, log="team"):
    V.append(vector(f"leaf-{vid}", "no_upstream", "leaf", {"data_hex": data.hex(), "log_kind": log, "at": at},
                    {"verdict": verdict}, authority, None))


OK = b'{"leaf":"line","v":1}'
leaf("canonical-accepted", OK, "ingest", ACCEPT, CANON)
leaf("canonical-accepted-at-verify", OK, "verify", ACCEPT, CANON)
for vid, d in (("reordered-keys", b'{"v":1,"leaf":"line"}'), ("whitespace", b'{"leaf": "line","v":1}'),
               ("escaped-e-acute", b'{"leaf":"line","v":1,"x":"\\u00e9"}'), ("minus-zero", b'{"leaf":"line","v":1,"x":-0}'),
               ("bom", b"\xef\xbb\xbf" + OK), ("trailing-newline", OK + b"\n")):
    leaf(f"non-canonical-{vid}-refused", d, "ingest", REFUSE, CANON)
leaf("canonical-e-acute-control", '{"leaf":"line","v":1,"x":"é"}'.encode(), "ingest", ACCEPT, CANON)
leaf("canonical-zero-control", b'{"leaf":"line","v":1,"x":0}', "ingest", ACCEPT, CANON)
for vid, d in (("float", b'{"leaf":"line","v":1,"x":1.0}'), ("exponent", b'{"leaf":"line","v":1,"x":1e2}'),
               ("nan", b'{"leaf":"line","v":1,"x":NaN}'), ("infinity", b'{"leaf":"line","v":1,"x":-Infinity}'),
               ("duplicate-key", b'{"leaf":"line","leaf":"line","v":1}'),
               ("lone-surrogate-escape", b'{"leaf":"line","v":1,"x":"\\ud800"}'),
               ("surrogate-bytes", b'{"leaf":"line","v":1,"x":"\xed\xa0\x80"}'),
               ("invalid-utf8", b'{"leaf":"line","v":1,"x":"\xff"}'),
               ("int-2-53", b'{"leaf":"line","v":1,"x":9007199254740992}'),
               ("int-minus-2-53", b'{"leaf":"line","v":1,"x":-9007199254740992}')):
    leaf(f"parse-{vid}-refused", d, "ingest", REFUSE, REF)
leaf("int-2-53-minus-1-accepted", b'{"leaf":"line","v":1,"x":9007199254740991}', "ingest", ACCEPT, REF)


def nest(depth: int) -> bytes:
    """A leaf object (depth 1) holding a list nest, so the whole document is ``depth`` deep."""
    inner = depth - 1
    return b'{"leaf":"line","v":1,"x":' + b"[" * inner + b"]" * inner + b"}"


leaf("depth-4-accepted", nest(4), "ingest", ACCEPT, DEPTH)
leaf("depth-32-accepted", nest(32), "ingest", ACCEPT, DEPTH)
leaf("depth-33-refused", nest(33), "ingest", REFUSE, DEPTH)
leaf("depth-5000-refused", nest(5000), "ingest", REFUSE, DEPTH)
leaf("brackets-in-strings-are-not-depth", b'{"leaf":"line","v":1,"x":"' + b"[" * 40 + b'"}', "ingest", ACCEPT, DEPTH)

leaf("unknown-leaf-refused-at-ingest", b'{"leaf":"nope","v":1}', "ingest", REFUSE, REG)
leaf("unknown-leaf-unverifiable-at-verify", b'{"leaf":"nope","v":1}', "verify", UNVERIFIABLE, REG)
leaf("unknown-v-refused-at-ingest", b'{"leaf":"line","v":2}', "ingest", REFUSE, REG)
leaf("unknown-v-unverifiable-at-verify", b'{"leaf":"line","v":2}', "verify", UNVERIFIABLE, REG)
leaf("registered-leaf-wrong-log-kind-refused", OK, "ingest", REFUSE, REG, log="fleet")
leaf("registered-leaf-wrong-log-kind-refused-at-verify", OK, "verify", REFUSE, REG, log="receipt")
leaf("receipt-checkpoint-on-team-accepted", b'{"leaf":"receipt_checkpoint","v":1}', "ingest", ACCEPT, REG)
for vid, d in (("not-an-object", b"[1]"), ("missing-v", b'{"leaf":"line"}'), ("v-true", b'{"leaf":"line","v":true}'),
               ("v-string", b'{"leaf":"line","v":"1"}'), ("v-negative", b'{"leaf":"line","v":-1}'),
               ("leaf-empty", b'{"leaf":"","v":1}'), ("leaf-not-string", b'{"leaf":1,"v":1}'), ("empty", b"")):
    leaf(f"frame-{vid}-refused", d, "ingest", REFUSE, REG)

# chain_hash over the entries tests/test_team_golden.py ships (values copied from that file, then recomputed here)
E1 = {"v": 1, "id": "ana-20261004120000-0a1b2c3d", "ts": "2026-10-04T12:00:00Z", "author": "ana",
      "agent": "claude-code", "type": "decision", "kind": "ruling", "paths": ["src/billing.py"], "owner": "client:Dana",
      "words": "Per-line rounding stays — the bank’s validator rejects mismatched totals.", "supersedes": [],
      "prev": ""}
C1 = ('{"agent":"claude-code","author":"ana","id":"ana-20261004120000-0a1b2c3d","kind":"ruling",'
      '"owner":"client:Dana","paths":["src/billing.py"],"prev":"","supersedes":[],"ts":"2026-10-04T12:00:00Z",'
      '"type":"decision","v":1,"words":"Per-line rounding stays — the bank’s validator rejects mismatched totals."}')
H1 = "64e621bb36fc63176a4b17966cb58a51921980f27559d0fc4308c2fff80b7e94"
assert hashlib.sha256(C1.encode()).hexdigest() == H1
E2 = {"v": 1, "id": "ana-20261004120100-0a1b2c3e", "ts": "2026-10-04T12:01:00Z", "author": "ana", "type": "ack",
      "refs": ["ana-20261004120000-0a1b2c3d"], "session": "s1", "paths": [], "supersedes": [], "prev": H1}
C2 = ('{"author":"ana","id":"ana-20261004120100-0a1b2c3e","paths":[],"prev":"' + H1 + '","refs":'
      '["ana-20261004120000-0a1b2c3d"],"session":"s1","supersedes":[],"ts":"2026-10-04T12:01:00Z","type":"ack","v":1}')
H2 = "1370dd29f84c9723e0536fc39dfac071fb4356b05db3717f13a2f1eef853417e"
assert hashlib.sha256((H1 + C2).encode()).hexdigest() == H2
V.append(vector("chain-hash-team-golden-1", "no_upstream", "chain_hash", {"prev": "", "entry": E1},
                {"verdict": ACCEPT, "hash": H1, "canonical": C1}, CHAIN, None))
V.append(vector("chain-hash-team-golden-2", "no_upstream", "chain_hash", {"prev": H1, "entry": E2},
                {"verdict": ACCEPT, "hash": H2, "canonical": C2}, CHAIN, None))
V.append(vector("chain-hash-ignores-hash-field", "no_upstream", "chain_hash", {"prev": H1, "entry": {**E2, "hash": H2}},
                {"verdict": ACCEPT, "hash": H2, "canonical": C2}, CHAIN, None))

write("F8", "canon", V, max_depth=32)
