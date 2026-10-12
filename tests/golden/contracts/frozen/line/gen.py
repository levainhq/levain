"""Generate frozen/line/vectors.json (F9: the device message, the (line, 1) leaf, the sidecar receipt). stdlib +
cryptography only.

The device message golden is assembled by hand from its three lengths (S1_DESIGN F9: 0x0000002c, 0x0000001a,
0x00000020). Leaves are built by ``_genlib`` from F8's canonical form, team/entry.py's seal formula and
PROTOCOL.sshsig, never by ``levain``; each expected verdict is written here from the design rule it pins.
"""
import hashlib
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import (ACCEPT, REFUSE, UNVERIFIABLE, b64, canon_json, derivation, device_message,  # noqa: E402
                     ed_key_blob, ed_sign, fp, line_leaf, profile, pub, seal_entry, seed, ssh_str, sshsig_blob,
                     sshsig_signed_data, vector, write)

ORIGIN = "levain-team/" + "a" * 32
OTHER_ORIGIN = "levain-team/" + "b" * 32
NOW = "2026-10-12T00:00:00Z"
MSG = derivation("S1_DESIGN F9 (rev 2 T11, T26; C2)",
                 "string(origin.encode('ascii')) || string(file.encode('utf-8')) || string(bytes.fromhex(chain_hash)); "
                 "SSH string = uint32 big-endian byte length || bytes; file is the exact JSON string, no Unicode "
                 "normalisation; chain_hash is 64 lowercase hex (uppercase refused)")
LEAF = profile("S1_DESIGN F9", "a (line, 1) leaf: exactly leaf, v, file, entry (SEALED), device_fp, device_sig (raw "
                               "SSHSIG, canonical base64); entry.hash = chain_hash(entry.prev, entry); device_sig "
                               "verifies over F9's message for the log's origin",
               rationale="levain's own leaf format; no external specification governs it")
EDGE = profile("S1_DESIGN F9 rev 10", "verify passes now = None: the future-ts branch is skipped at verify only",
               rationale="a C2SP checkpoint carries no time; a far-future ts admitted by the operator is a known edge")
REG = profile("S1_DESIGN F8 / F9", "an unknown (leaf, v) or scope is refused at ingest and UNVERIFIABLE at verify",
              rationale="an old levain never calls a newer leaf tampering")
HINT = profile("S1_DESIGN F9 / TS r5 F5", "the sidecar receipt is a hint: every field is derived by the client, and a "
                                          "receipt whose fields disagree with the derived ones is refused",
               rationale="a server-written field must never authorize")
V = []


def ssh_str_hex(b: bytes) -> str:
    return len(b).to_bytes(4, "big").hex() + b.hex()


def message(vid, origin, file, chain, verdict, by_hand=None):
    lv = {"verdict": verdict}
    if verdict == ACCEPT:
        lv["bytes_hex"] = by_hand or (ssh_str_hex(origin.encode("ascii")) + ssh_str_hex(file.encode("utf-8"))
                                      + ssh_str_hex(bytes.fromhex(chain)))
    V.append(vector(f"message-{vid}", "no_upstream", "device_message",
                    {"origin": origin, "file": file, "chain_hash": chain}, lv, MSG, None))


FILE = "ledger/dana/findings.jsonl"
CHAIN = "00" * 31 + "01"
BY_HAND = ("0000002c" + ORIGIN.encode("ascii").hex()              # 44 = len("levain-team/" + 32 hex)
           + "0000001a" + FILE.encode("utf-8").hex()               # 26 = len("ledger/dana/findings.jsonl")
           + "00000020" + "00" * 31 + "01")                        # 32 raw bytes of the chain hash
assert len(ORIGIN) == 0x2c and len(FILE) == 0x1a
message("fixed-golden", ORIGIN, FILE, CHAIN, ACCEPT, BY_HAND)
NFC = unicodedata.normalize("NFC", "ledger/dana/café.jsonl")
NFD = unicodedata.normalize("NFD", "ledger/dana/café.jsonl")
assert NFC != NFD
message("file-nfc", ORIGIN, NFC, CHAIN, ACCEPT)
message("file-nfd", ORIGIN, NFD, CHAIN, ACCEPT)
message("file-multibyte", ORIGIN, "ledger/dana/記録.jsonl", CHAIN, ACCEPT)
message("chain-hash-uppercase-refused", ORIGIN, FILE, "00" * 31 + "0A", REFUSE)
message("chain-hash-63-hex-refused", ORIGIN, FILE, "0" * 63, REFUSE)
message("origin-not-team-production-refused", "levain-team/" + "a" * 31, FILE, CHAIN, REFUSE)

# ---- leaves --------------------------------------------------------------------------------------------------
DANA = seed("line-device-dana")
OTHER_KEY = seed("line-device-other")
FINDING = {"v": 1, "id": "dana-20261010120000-0123abcd", "ts": "2026-10-10T12:00:00Z", "author": "dana",
           "type": "finding", "words": "the rounding is per line", "paths": ["src/billing.py"], "supersedes": []}
S1 = seal_entry(FINDING, "")
GOOD = line_leaf(S1, FILE, DANA, ORIGIN)


def leaf(vid, data: bytes, at, verdict, authority=LEAF, scope="human", now=NOW):
    V.append(vector(f"leaf-{vid}", "no_upstream", "line",
                    {"data_hex": data.hex(), "origin": ORIGIN, "scope": scope, "at": at,
                     "now": now if at == "ingest" else None}, {"verdict": verdict}, authority, None))


leaf("good-at-ingest", GOOD, "ingest", ACCEPT)
leaf("good-at-verify", GOOD, "verify", ACCEPT)
leaf("nfd-file-good", line_leaf(S1, NFD, DANA, ORIGIN), "ingest", ACCEPT)
leaf("signed-for-the-nfc-file-presented-as-nfd-refused", line_leaf(S1, NFD, DANA, ORIGIN, sign_file=NFC), "ingest",
     REFUSE)
leaf("signed-for-another-origin-refused", line_leaf(S1, FILE, DANA, ORIGIN, sign_origin=OTHER_ORIGIN), "verify", REFUSE)
leaf("signed-for-another-file-refused", line_leaf(S1, FILE, DANA, ORIGIN, sign_file="ledger/dana/other.jsonl"),
     "verify", REFUSE)
leaf("device-fp-not-the-signing-key-refused",
     line_leaf(S1, FILE, DANA, ORIGIN, device_fp=fp(ed_key_blob(pub(OTHER_KEY)))), "ingest", REFUSE)
edited = {**S1, "words": "the rounding is per invoice"}
leaf("entry-edited-after-sealing-refused", line_leaf(edited, FILE, DANA, ORIGIN), "verify", REFUSE)
leaf("entry-unsealed-refused", line_leaf({**FINDING, "hash": S1["hash"]}, FILE, DANA, ORIGIN), "ingest", REFUSE)
leaf("extra-key-n-refused", line_leaf(S1, FILE, DANA, ORIGIN, extra={"n": 1}), "ingest", REFUSE)
leaf("missing-device-fp-refused", line_leaf(S1, FILE, DANA, ORIGIN, drop=("device_fp",)), "ingest", REFUSE)
leaf("file-not-a-string-refused", line_leaf(S1, FILE, DANA, ORIGIN, extra={"file": ["x"]}), "ingest", REFUSE)
good_obj = __import__("json").loads(GOOD)
blob_b64 = good_obj["device_sig"]
armored = "-----BEGIN SSH SIGNATURE-----\n" + blob_b64 + "\n-----END SSH SIGNATURE-----\n"
leaf("device-sig-armored-refused", canon_json({**good_obj, "device_sig": armored}).encode(), "ingest", REFUSE)
raw = __import__("base64").b64decode(blob_b64)
assert len(raw) % 3 == 1, len(raw)     # two '=' pads: the last symbol carries 4 pad bits
noncanon = blob_b64[:-3] + chr(ord(blob_b64[-3]) + 1) + "=="
assert __import__("base64").b64decode(noncanon) == raw, "the mutation must only touch pad bits"
leaf("device-sig-non-canonical-base64-refused", canon_json({**good_obj, "device_sig": noncanon}).encode(), "ingest",
     REFUSE)
leaf("device-sig-trailing-byte-refused", canon_json({**good_obj, "device_sig": b64(raw + b"\x00")}).encode(),
     "verify", REFUSE)
noncanon_leaf = GOOD.replace(b'{"device_fp"', b'{ "device_fp"')
leaf("leaf-bytes-not-canonical-refused", noncanon_leaf, "ingest", REFUSE)

FUTURE = seal_entry({**FINDING, "id": "dana-20991231120000-0123abcd", "ts": "2099-12-31T12:00:00Z"}, "")
FUT = line_leaf(FUTURE, FILE, DANA, ORIGIN)
leaf("future-ts-refused-at-ingest", FUT, "ingest", REFUSE)
leaf("future-ts-accepted-at-verify-known-edge", FUT, "verify", ACCEPT, authority=EDGE)

leaf("unknown-scope-refused-at-ingest", GOOD, "ingest", REFUSE, authority=REG, scope="auditor")
leaf("unknown-scope-unverifiable-at-verify", GOOD, "verify", UNVERIFIABLE, authority=REG, scope="auditor")
v2 = canon_json({**good_obj, "v": 2}).encode()
leaf("line-v2-refused-at-ingest", v2, "ingest", REFUSE, authority=REG)
leaf("line-v2-unverifiable-at-verify", v2, "verify", UNVERIFIABLE, authority=REG)
leaf("receipt-scope-writes-no-line-refused", GOOD, "ingest", REFUSE, scope="receipt")

# ---- rev 11 Q9: the registry's context-free validator (canon.classify_leaf -> check_line_fields) runs F12's
# profile check without the signature, so a sha256 SSHSIG is refused there by the hash-algorithm rule ALONE (at
# judge_line, sshsig.signed_data refuses the same blob a second time). This isolates that one rule.
SHA256_RULE = profile("S1_DESIGN F12", "the hash algorithm is sha512 (the profile check, before any signature)",
                      quote='The supported hash algorithms are "sha256" and "sha512".')
sig256 = ed_sign(DANA, sshsig_signed_data(b"levain-team-entry", device_message(ORIGIN, FILE, S1["hash"]),
                                          alg=b"sha256"))
blob256 = sshsig_blob(ed_key_blob(pub(DANA)), b"levain-team-entry", ssh_str(b"ssh-ed25519") + ssh_str(sig256),
                      alg=b"sha256")


def classify(vid, data: bytes, verdict, authority):
    V.append(vector(f"classify-{vid}", "no_upstream", "classify", {"data_hex": data.hex(), "log_kind": "team",
                                                                    "at": "ingest"}, {"verdict": verdict}, authority,
                    None))


classify("good-control", GOOD, ACCEPT, LEAF)
classify("device-sig-sha256-refused-by-the-profile-alone", canon_json({**good_obj, "device_sig": b64(blob256)}).encode(),
         REFUSE, SHA256_RULE)

# ---- the sidecar receipt (TS §1.4), a hint --------------------------------------------------------------------
DEVICE_HINT = hashlib.sha256(ed_key_blob(pub(DANA))).hexdigest()[:16]
DERIVE = {"n": 12, "leaf_index": 41, "handle": "dana", "scope": "human", "team_version": 3}
RECEIPT = {"v": 1, "type": "device", "file": FILE, "n": 12, "hash": S1["hash"], "handle": "dana",
           "device": DEVICE_HINT, "scope": "human", "device_sig": blob_b64, "leaf": 41, "team_version": 3,
           "accepted": "2026-11-02T14:03:11Z"}


def receipt(vid, rec, verdict):
    V.append(vector(f"receipt-{vid}", "no_upstream", "receipt",
                    {"leaf_hex": GOOD.hex(), "derive": DERIVE, "receipt": rec}, {"verdict": verdict}, HINT, None))


receipt("matches-the-derived-fields", RECEIPT, ACCEPT)
receipt("team-version-disagrees-refused", {**RECEIPT, "team_version": 2}, REFUSE)
receipt("team-version-as-string-refused", {**RECEIPT, "team_version": "3"}, REFUSE)
receipt("n-disagrees-refused", {**RECEIPT, "n": 13}, REFUSE)
receipt("leaf-index-disagrees-refused", {**RECEIPT, "leaf": 40}, REFUSE)
receipt("scope-disagrees-refused", {**RECEIPT, "scope": "member"}, REFUSE)
receipt("handle-disagrees-refused", {**RECEIPT, "handle": "erin"}, REFUSE)
receipt("device-hint-disagrees-refused", {**RECEIPT, "device": "0" * 16}, REFUSE)
receipt("hash-disagrees-refused", {**RECEIPT, "hash": "0" * 64}, REFUSE)
receipt("device-sig-disagrees-refused", {**RECEIPT, "device_sig": b64(raw + b"\x00")}, REFUSE)
receipt("extra-field-refused", {**RECEIPT, "authority": "owner"}, REFUSE)
receipt("accepted-malformed-refused", {**RECEIPT, "accepted": "2026-11-02 14:03"}, REFUSE)
receipt("n-true-refused", {**RECEIPT, "n": True}, REFUSE)
# rev 11 (T03): the sidecar's accepted check shares TS_RE, ASCII digits only
receipt("accepted-fullwidth-digits-refused",
        {**RECEIPT, "accepted": "2026-11-02T14:03:11Z".translate(str.maketrans("0123456789", "０１２３４５６７８９"))},
        REFUSE)

write("F9", "line", V, origin=ORIGIN, now=NOW)
