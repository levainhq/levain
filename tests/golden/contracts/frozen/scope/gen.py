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
from _genlib import ACCEPT, REFUSE, UNVERIFIABLE, line_leaf, profile, seal_entry, seed, vector, write  # noqa: E402

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


def log(vid, at, leaves, verdicts, authority, operator_fault=None):
    lv = {"verdict": verdicts[-1], "verdicts": verdicts}
    if operator_fault is not None:
        lv["operator_fault"] = operator_fault
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
log("unknown-scope-refused-at-ingest", "ingest", [(A_LEAF, "auditor")], [REFUSE], REG)
log("unknown-scope-unverifiable-at-verify", "verify", [(A_LEAF, "auditor")], [UNVERIFIABLE], REG)

write("F9 scope", "scope", V, origin=ORIGIN, now=NOW)
