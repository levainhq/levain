"""Generate frozen/scope/vectors.json (F9: the team-log scope matrix, entry-id uniqueness, (file, hash) idempotency).
stdlib + cryptography only.

``may_write`` vectors judge one validated entry against a snapshot (``accept`` = ALLOW). ``line_log`` vectors walk
signed leaves in log order at ingest or verify; the snapshot grows by ACCEPTED leaves only. Every expected verdict is
written here from TS l.410-412 and S1_DESIGN F9 (rev 2 T17; rev 3 U12; rev 4 V03; rev 5 W03; rev 6 X01, X02; rev 7
Y01; rev 8 Z01/Z02), never computed by levain.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, UNVERIFIABLE, canon_json, line_leaf, profile, seal_entry, seed, vector, write  # noqa: E402

ORIGIN = "levain-team/" + "c" * 32
NOW = "2026-10-12T00:00:00Z"
TS = "TS (team_server_managed_DESIGN_1006) l.410-412"
MATRIX = profile(f"S1_DESIGN F9 / {TS}",
                 "human writes finding, question, tension, ack, retire; member writes only finding, question, tension "
                 "and may not carry a non-empty supersedes or write ack; receipt writes only receipt_checkpoint leaves; "
                 "decision, constraint and kind: ruling are refused for every scope until P2; supersedes is default-deny: "
                 "every target present in the snapshot exactly once and not a ruling",
                 quote="A member-scoped device ... may write only `finding`, `question`, `tension`, and may not carry "
                       "`supersedes` or write `ack`")
UNIQUE = profile("S1_DESIGN F9 rev 6 X01 / rev 7", "an entry.id already held by an ACCEPTED leaf is refused at ingest; at "
                 "verify the second accepted occurrence is refused, the first in log order wins; a refused leaf holds no id",
                 rationale="the duplicate-ID state is made unreachable at ingest (Matrix MSC1659 met this class)")
IDEM = profile("S1_DESIGN F9 / TS l.326", "a second leaf with an accepted leaf's (file, entry.hash) is refused at "
               "ingest and is an operator fault at verify", rationale="TS's POST /lines is idempotent on (file, hash)")
SCHEMA = profile("S1_DESIGN F9 rev 7 Y01 / rev 8 Z01, Z02", "every leaf's entry passes validate_entry_v1, the frozen "
                 "entry schema of levain 31dea1f", rationale="levain's own entry schema; no external specification")
KNOWN = profile("S1_DESIGN F9 rev 11 / levain team/entry.py validate @ 31dea1f (known)",
                "validate_entry_v1 runs at ingest and verify with known = the snapshot's entries by unique id: every "
                "refs and supersedes target must be held, an ack or a retire is never superseded",
                rationale="the frozen schema's snapshot rules are T's writer rules; the snapshot is the team log's "
                          "verified prefix")
CHAIN = profile("S1_DESIGN F9 rev 11", "the snapshot keeps each file's head hash; a leaf is accepted only if "
                "entry.prev equals that head (\"\" only while the file has no accepted entry); a second prev=\"\" "
                "for a file, or any prev that is not the head, is refused",
                rationale="team/entry.py chains each file by prev; two accepted children of one parent would be a "
                          "permanent fork in the sealed per-file chain")
CASCADE = profile("S1_DESIGN F8/F9 rev 11", "verification of a log is a replay: once a leaf is UNVERIFIABLE (an "
                  "unknown (leaf, v) or scope), every later leaf in that log is UNVERIFIABLE, naming the first; "
                  "ingest is unchanged (unknown is REFUSE there)",
                  rationale="an unverifiable leaf could have changed replay state (an id, a head, a target); the rule "
                            "is positional, not a guess about dependence")
REG = profile("S1_DESIGN F9", "an unknown scope is refused at ingest and UNVERIFIABLE at verify",
              rationale="scope comes from the key directory; an old levain never calls a newer scope tampering")
V = []


def ent(author, n, typ, **kw):
    e = {"v": 1, "id": f"{author}-202610101200{n:02d}-0000{n:04x}", "ts": f"2026-10-10T12:{n // 60:02d}:{n % 60:02d}Z",
         "author": author, "type": typ, "paths": [], "supersedes": []}
    e.update(kw)
    return e


T = ent("dana", 1, "finding", words="target finding")            # a finding the snapshot holds
R = ent("dana", 2, "decision", kind="ruling", owner="lead", words="w")    # a ruling (a snapshot may hold one only as a
#                                                                       reader's defence in depth: F9 keeps them out)
CELL = {"finding": ent("dana", 10, "finding"), "question": ent("dana", 11, "question"),
        "tension": ent("dana", 12, "tension"), "ack": ent("dana", 13, "ack", refs=[T["id"]]),
        "retire": ent("dana", 14, "retire", supersedes=[T["id"]]),
        "decision": ent("dana", 15, "decision", kind="practice"),
        "constraint": ent("dana", 16, "constraint", kind="practice")}
ALLOWED = {"human": {"finding", "question", "tension", "ack", "retire"}, "member": {"finding", "question", "tension"},
           "receipt": set()}


def may(vid, scope, entry, snapshot, verdict):
    V.append(vector(f"may-write-{vid}", "no_upstream", "may_write",
                    {"scope": scope, "entry": entry, "snapshot": snapshot},
                    {"verdict": verdict}, MATRIX, None))


for scope in ("human", "member", "receipt"):
    for typ, e in CELL.items():
        may(f"{scope}-{typ}", scope, e, [T], ACCEPT if typ in ALLOWED[scope] else REFUSE)

may("member-finding-supersedes-a-target-refused", "member", ent("mo", 20, "finding", supersedes=[T["id"]]), [T], REFUSE)
may("member-finding-supersedes-empty-list-allowed", "member", ent("mo", 21, "finding", supersedes=[]), [T], ACCEPT)
ruling_finding = ent("dana", 22, "finding", kind="ruling", owner="lead", words="w")
for scope in ("human", "member", "receipt"):
    may(f"{scope}-finding-kind-ruling-refused", scope, ruling_finding, [T], REFUSE)
may("human-retire-of-a-ruling-refused", "human", ent("dana", 23, "retire", supersedes=[R["id"]]), [T, R], REFUSE)
may("human-retire-of-a-finding-allowed", "human", ent("dana", 24, "retire", supersedes=[T["id"]]), [T], ACCEPT)
may("human-retire-target-not-in-snapshot-refused", "human", ent("dana", 25, "retire", supersedes=["dana-x"]), [T], REFUSE)
may("human-retire-one-allowed-one-ruling-refused", "human", ent("dana", 26, "retire", supersedes=[T["id"], R["id"]]),
    [T, R], REFUSE)
may("human-retire-a-a-allowed", "human", ent("dana", 27, "retire", supersedes=[T["id"], T["id"]]), [T], ACCEPT)
may("human-finding-supersedes-a-finding-allowed", "human", ent("dana", 28, "finding", supersedes=[T["id"]]), [T], ACCEPT)
may("human-retire-target-twice-in-snapshot-refused", "human", ent("dana", 29, "retire", supersedes=[T["id"]]), [T, T],
    REFUSE)
may("human-retire-empty-supersedes-refused", "human", ent("dana", 30, "retire", supersedes=[]), [T], REFUSE)

# ---- signed leaves in log order -------------------------------------------------------------------------------
DANA, MO, ERIN = seed("scope-device-dana"), seed("scope-device-mo"), seed("scope-device-erin")
F1, F2 = "ledger/dana/findings.jsonl", "ledger/dana/other.jsonl"


def log(vid, at, leaves, verdicts, authority, operator_fault=None, first_unverifiable=None):
    lv = {"verdict": verdicts[-1], "verdicts": verdicts}
    if operator_fault is not None:
        lv["operator_fault"] = operator_fault
    if first_unverifiable is not None:
        lv["first_unverifiable"] = first_unverifiable
    V.append(vector(f"log-{vid}", "no_upstream", "line_log",
                    {"origin": ORIGIN, "at": at, "now": NOW if at == "ingest" else None,
                     "leaves": [{"data_hex": d.hex(), "scope": s} for d, s in leaves]}, lv, authority, None))


A = seal_entry(ent("dana", 40, "finding", words="first"), "")
A_LEAF = line_leaf(A, F1, DANA, ORIGIN)
COPY = seal_entry({**A, "words": "a different body, the same id"}, "")          # same id, another file and device
COPY_LEAF = line_leaf(COPY, F2, ERIN, ORIGIN)
for at in ("ingest", "verify"):
    log(f"second-leaf-copying-an-accepted-id-refused-at-{at}", at, [(A_LEAF, "human"), (COPY_LEAF, "human")],
        [ACCEPT, REFUSE], UNIQUE)
BAD_SIG = line_leaf(A, F1, DANA, ORIGIN, sign_file="ledger/dana/elsewhere.jsonl")
log("a-refused-leaf-holds-no-id", "verify", [(BAD_SIG, "human"), (A_LEAF, "human")], [REFUSE, ACCEPT], UNIQUE)
log("repeated-file-hash-refused-at-ingest", "ingest", [(A_LEAF, "human"), (A_LEAF, "human")], [ACCEPT, REFUSE], IDEM,
    operator_fault=[False, True])
log("repeated-file-hash-operator-fault-at-verify", "verify", [(A_LEAF, "human"), (A_LEAF, "human")], [ACCEPT, REFUSE],
    IDEM, operator_fault=[False, True])

for vid, e in (("supersedes-empty-string", ent("mo", 50, "finding", supersedes="")),
               ("supersedes-object", ent("mo", 51, "finding", supersedes={})),
               ("id-not-prefixed-by-author", {**ent("mo", 52, "finding"), "id": "erin-20261010120052-00000034"}),
               ("malformed-ts", {**ent("mo", 53, "finding"), "ts": "2026-10-10T12:00Z"})):
    log(f"member-finding-{vid}-refused", "ingest", [(line_leaf(seal_entry(e, ""), F1, MO, ORIGIN), "member")],
        [REFUSE], SCHEMA)

M_TARGET = seal_entry(ent("dana", 60, "finding", words="target"), "")
M_SUP = seal_entry(ent("mo", 61, "finding", supersedes=[M_TARGET["id"]]), "")
log("member-finding-carrying-supersedes-refused", "ingest",
    [(line_leaf(M_TARGET, F1, DANA, ORIGIN), "human"), (line_leaf(M_SUP, "ledger/mo/a.jsonl", MO, ORIGIN), "member")],
    [ACCEPT, REFUSE], MATRIX)
M_OK = seal_entry(ent("mo", 62, "finding", supersedes=[]), "")
log("member-finding-empty-supersedes-allowed", "verify", [(line_leaf(M_OK, "ledger/mo/a.jsonl", MO, ORIGIN), "member")],
    [ACCEPT], MATRIX)
H_RETIRE = seal_entry(ent("dana", 63, "retire", supersedes=[M_TARGET["id"]]), M_TARGET["hash"])
log("human-retire-of-an-accepted-finding-allowed", "verify",
    [(line_leaf(M_TARGET, F1, DANA, ORIGIN), "human"), (line_leaf(H_RETIRE, F1, DANA, ORIGIN), "human")],
    [ACCEPT, ACCEPT], MATRIX)
DANGLING = seal_entry(ent("dana", 64, "retire", supersedes=["dana-20261009000000-deadbeef"]), "")
log("human-retire-dangling-target-refused", "ingest", [(line_leaf(DANGLING, F1, DANA, ORIGIN), "human")], [REFUSE],
    MATRIX)
# ---- rev 11: validate_entry_v1 runs with `known` built from the snapshot, through the leaf path -----------------
K_FIND = seal_entry(ent("dana", 70, "finding", words="a finding"), "")
K_FIND_LEAF = line_leaf(K_FIND, F1, DANA, ORIGIN)
K_ACK = seal_entry(ent("dana", 71, "ack", refs=[K_FIND["id"]]), K_FIND["hash"])
K_ACK_LEAF = line_leaf(K_ACK, F1, DANA, ORIGIN)
K_RET = seal_entry(ent("dana", 72, "retire", supersedes=[K_FIND["id"]]), K_FIND["hash"])
K_RET_LEAF = line_leaf(K_RET, F1, DANA, ORIGIN)
GHOST = "dana-20261009000000-0badc0de"
for at in ("ingest", "verify"):
    log(f"human-ack-of-an-accepted-finding-allowed-at-{at}", at, [(K_FIND_LEAF, "human"), (K_ACK_LEAF, "human")],
        [ACCEPT, ACCEPT], KNOWN)
    log(f"human-ack-dangling-ref-refused-at-{at}", at,
        [(line_leaf(seal_entry(ent("dana", 73, "ack", refs=[GHOST]), ""), F1, DANA, ORIGIN), "human")], [REFUSE], KNOWN)
    log(f"member-finding-dangling-ref-refused-at-{at}", at,
        [(line_leaf(seal_entry(ent("mo", 74, "finding", refs=[GHOST]), ""), "ledger/mo/a.jsonl", MO, ORIGIN), "member")],
        [REFUSE], KNOWN)
    log(f"human-finding-superseding-an-accepted-ack-refused-at-{at}", at,
        [(K_FIND_LEAF, "human"), (K_ACK_LEAF, "human"),
         (line_leaf(seal_entry(ent("dana", 75, "finding", supersedes=[K_ACK["id"]]), K_ACK["hash"]), F1, DANA, ORIGIN),
          "human")],
        [ACCEPT, ACCEPT, REFUSE], KNOWN)
    log(f"human-retire-of-an-accepted-retire-refused-at-{at}", at,
        [(K_FIND_LEAF, "human"), (K_RET_LEAF, "human"),
         (line_leaf(seal_entry(ent("dana", 76, "retire", supersedes=[K_RET["id"]]), K_RET["hash"]), F1, DANA, ORIGIN),
          "human")],
        [ACCEPT, ACCEPT, REFUSE], KNOWN)

# ---- rev 11: per-file chain continuity: entry.prev is the file's head ("" only while the file has none) --------
C_G = seal_entry(ent("dana", 80, "finding", words="genesis"), "")
C_G_LEAF = line_leaf(C_G, F1, DANA, ORIGIN)
C_L1 = seal_entry(ent("dana", 81, "finding", words="second link"), C_G["hash"])
C_L1_LEAF = line_leaf(C_L1, F1, DANA, ORIGIN)
C_FORK0 = line_leaf(seal_entry(ent("dana", 82, "finding", words="a second genesis"), ""), F1, DANA, ORIGIN)
C_FORK1 = line_leaf(seal_entry(ent("dana", 83, "finding", words="a second child of genesis"), C_G["hash"]), F1, DANA,
                    ORIGIN)
C_OTHER = seal_entry(ent("dana", 84, "finding", words="the other file's genesis"), "")
C_OTHER_LEAF = line_leaf(C_OTHER, F2, DANA, ORIGIN)
C_CROSS = line_leaf(seal_entry(ent("dana", 85, "finding", words="prev names the other file's head"), C_OTHER["hash"]),
                    F1, DANA, ORIGIN)
C_ORPHAN = line_leaf(seal_entry(ent("dana", 86, "finding", words="prev in an empty file"), C_G["hash"]), F2, DANA,
                     ORIGIN)
for at in ("ingest", "verify"):
    log(f"chain-genesis-accepted-at-{at}", at, [(C_G_LEAF, "human")], [ACCEPT], CHAIN)
    log(f"chain-second-link-accepted-at-{at}", at, [(C_G_LEAF, "human"), (C_L1_LEAF, "human")], [ACCEPT, ACCEPT], CHAIN)
    log(f"chain-fork-at-genesis-refused-at-{at}", at, [(C_G_LEAF, "human"), (C_FORK0, "human")], [ACCEPT, REFUSE],
        CHAIN)
    log(f"chain-fork-mid-chain-refused-at-{at}", at,
        [(C_G_LEAF, "human"), (C_L1_LEAF, "human"), (C_FORK1, "human")], [ACCEPT, ACCEPT, REFUSE], CHAIN)
    log(f"chain-prev-names-a-non-head-hash-refused-at-{at}", at,
        [(C_G_LEAF, "human"), (C_OTHER_LEAF, "human"), (C_CROSS, "human")], [ACCEPT, ACCEPT, REFUSE], CHAIN)
    log(f"chain-non-empty-prev-in-an-empty-file-refused-at-{at}", at, [(C_G_LEAF, "human"), (C_ORPHAN, "human")],
        [ACCEPT, REFUSE], CHAIN)

# ---- rev 11: at verify an UNVERIFIABLE leaf poisons every later leaf in the log (positional, never a guess) -----
U_X = seal_entry(ent("dana", 90, "finding", words="held by an unknown (line, 2) leaf"), "")
U_V2 = canon_json({**__import__("json").loads(line_leaf(U_X, F1, DANA, ORIGIN)), "v": 2}).encode("utf-8")
U_SAME_ID = line_leaf(seal_entry({**U_X, "words": "the same id, a (line, 1) leaf"}, ""), F2, DANA, ORIGIN)
U_INDEP = line_leaf(seal_entry(ent("mo", 91, "finding", words="independent"), ""), "ledger/mo/u.jsonl", MO, ORIGIN)
U_BAD = line_leaf(seal_entry(ent("mo", 92, "finding"), ""), "ledger/mo/v.jsonl", MO, ORIGIN, sign_file="x.jsonl")
log("unknown-leaf-then-a-leaf-reusing-its-id-unverifiable-at-verify", "verify",
    [(U_V2, "human"), (U_SAME_ID, "human")], [UNVERIFIABLE, UNVERIFIABLE], CASCADE, first_unverifiable=0)
log("unknown-leaf-then-an-independent-leaf-unverifiable-at-verify", "verify",
    [(U_V2, "human"), (U_INDEP, "member")], [UNVERIFIABLE, UNVERIFIABLE], CASCADE, first_unverifiable=0)
log("unknown-scope-then-an-independent-leaf-unverifiable-at-verify", "verify",
    [(A_LEAF, "auditor"), (U_INDEP, "member")], [UNVERIFIABLE, UNVERIFIABLE], CASCADE, first_unverifiable=0)
log("accepted-then-unknown-then-a-bad-leaf-unverifiable-at-verify", "verify",
    [(U_INDEP, "member"), (U_V2, "human"), (U_BAD, "member"), (U_SAME_ID, "human")],
    [ACCEPT, UNVERIFIABLE, UNVERIFIABLE, UNVERIFIABLE], CASCADE, first_unverifiable=1)
log("unknown-leaf-then-a-leaf-reusing-its-id-at-ingest-unchanged", "ingest",
    [(U_V2, "human"), (U_SAME_ID, "human")], [REFUSE, ACCEPT], CASCADE)

log("unknown-scope-refused-at-ingest", "ingest", [(A_LEAF, "auditor")], [REFUSE], REG)
log("unknown-scope-unverifiable-at-verify", "verify", [(A_LEAF, "auditor")], [UNVERIFIABLE], REG)

write("F9 scope", "scope", V, origin=ORIGIN, now=NOW)
