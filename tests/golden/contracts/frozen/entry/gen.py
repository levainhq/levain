"""Generate frozen/entry/vectors.json: the FROZEN (line, 1) entry schema's recorded verdicts (S1_DESIGN F9, rev 10 N1/N2).

The oracle is ``levain/team/entry.py`` ``validate`` AS IT STANDS AT levain ``31dea1f``, read with ``git show`` and
executed from that source text with its wall clock replaced by the vector's fixed ``now`` (31dea1f reads
``datetime.now``; the frozen copy takes ``now`` from its caller). Nothing here imports ``levain``: the copy in
``levain/contracts/leaves/entry_v1.py`` is what the test checks against these recorded verdicts. At the freeze, and
only then, the recorded verdicts come from 31dea1f; regenerating after the freeze must reproduce them byte for byte.

A refusal records its ``branch``: the 1-based index of the ``raise EntryError`` site that fired, counted in source
order inside ``validate`` (35 sites at 31dea1f), and its message. The vectors cover every site, every
``_MAX_LEN`` / ``_MAX_ITEMS`` / ``_MAX_PATH`` bound at exactly its limit (boundary accepts), and every branch of the
copy (the test measures it). stdlib only.
"""
import copy
import json
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _genlib import ACCEPT, REFUSE, profile, vector, write  # noqa: E402

AT = "31dea1f"
SRC = subprocess.run(["git", "show", f"{AT}:levain/team/entry.py"], capture_output=True, text=True, check=True,
                     cwd=Path(__file__).resolve().parent).stdout
FN = "<levain team/entry.py @ 31dea1f>"
NS: dict = {"__name__": "entry_31dea1f"}
exec(compile(SRC, FN, "exec"), NS)   # noqa: S102 - the design's named oracle, from a fixed commit's source
LINES = SRC.splitlines()
START = next(i for i, ln in enumerate(LINES, 1) if ln.startswith("def validate("))
END = next(i for i, ln in enumerate(LINES, 1) if i > START and ln.startswith("def "))
SITES = [i for i in range(START, END) if "raise EntryError" in LINES[i - 1]]
assert len(SITES) == 35, len(SITES)

NOW = "2026-10-12T00:00:00Z"
AUTH = profile("S1_DESIGN F9 (rev 7 Y01, rev 8 Z01, rev 9 M-1, rev 10 N1/N2) / levain team/entry.py validate @ 31dea1f",
               "(line, 1) entries pass the FROZEN entry schema: entry.validate as it stands at 31dea1f, with the "
               "future-ts branch reading the caller's now; the verdict (accept, or refuse plus the refusing raise "
               "site) is the one recorded here at the freeze",
               rationale="no external specification governs levain's entry schema; a later T schema change is a "
                         "new (line, v), never a moved (line, 1) verdict")
NONE_AUTH = profile("S1_DESIGN F9 rev 10", "now = None (verify) skips exactly the future-ts branch and no other",
                    rationale="a C2SP checkpoint carries no time, so a verifier cannot know when a leaf was ingested")


def fixed_clock(iso: str):
    t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return t
    return Fixed


def oracle(entry: dict, known, scan: bool, now: str) -> dict:
    NS["datetime"] = fixed_clock(now)
    try:
        NS["validate"](copy.deepcopy(entry), copy.deepcopy(known), scan=scan)
    except NS["EntryError"] as e:
        line = [f.lineno for f in traceback.extract_tb(e.__traceback__) if f.filename == FN][-1]
        return {"verdict": REFUSE, "branch": SITES.index(line) + 1, "message": str(e)}
    return {"verdict": ACCEPT}


V = []


def add(vid, entry, *, known=None, scan=False, now=NOW, expect_branch=None):
    want = oracle(entry, known, scan, now)
    if expect_branch is None:
        assert want["verdict"] == ACCEPT, (vid, want)
    else:
        assert want.get("branch") == expect_branch, (vid, want)
    V.append(vector(vid, "no_upstream", "validate_entry_v1",
                    {"entry": entry, "known": known, "scan": scan, "now": now}, want, AUTH, None))


def e(**kw):
    base = {"v": 1, "id": "dana-20261010120000-0123abcd", "ts": "2026-10-10T12:00:00Z", "author": "dana",
            "type": "finding", "paths": [], "supersedes": []}
    for k, x in kw.items():
        if x is DEL:
            base.pop(k)
        else:
            base[k] = x
    return base


DEL = object()
TID = "dana-20261009120000-0000aaaa"     # a target id held in `known`
RULING = {"v": 1, "id": TID, "ts": "2026-10-09T12:00:00Z", "author": "dana", "type": "decision", "kind": "ruling",
          "owner": "lead", "words": "w", "paths": [], "supersedes": []}

# ---- accepts: the shipped golden entries (tests/test_team_golden.py, raw and sealed) --------------------------
E1 = {"v": 1, "id": "ana-20261004120000-0a1b2c3d", "ts": "2026-10-04T12:00:00Z", "author": "ana",
      "agent": "claude-code", "type": "decision", "kind": "ruling", "paths": ["src/billing.py"], "owner": "client:Dana",
      "words": "Per-line rounding stays — the bank’s validator rejects mismatched totals.", "supersedes": []}
H1 = "64e621bb36fc63176a4b17966cb58a51921980f27559d0fc4308c2fff80b7e94"
E2 = {"v": 1, "id": "ana-20261004120100-0a1b2c3e", "ts": "2026-10-04T12:01:00Z", "author": "ana", "type": "ack",
      "refs": ["ana-20261004120000-0a1b2c3d"], "session": "s1", "paths": [], "supersedes": []}
H2 = "1370dd29f84c9723e0536fc39dfac071fb4356b05db3717f13a2f1eef853417e"
add("team-golden-entry-1", E1, scan=True)
add("team-golden-entry-1-sealed", {**E1, "prev": "", "hash": H1}, scan=True)
add("team-golden-entry-2", E2, scan=True)
add("team-golden-entry-2-sealed", {**E2, "prev": H1, "hash": H2}, scan=True)

# ---- accepts that reach branches ---------------------------------------------------------------------------
add("finding-minimal", e())
add("finding-scanned-clean", e(words="plain words, no secret"), scan=True)
add("secret-looking-text-unscanned", e(words="token = abcd1234efgh"), scan=False)
add("question-with-agent-session-owner", e(type="question", agent="codex", session="s-1", owner="lead"))
add("tension-practice-kind", e(type="tension", kind="practice"))
add("decision-practice", e(type="decision", kind="practice"))
add("constraint-ruling-with-mode", e(type="constraint", kind="ruling", mode="block", owner="lead", words="w"))
add("ruling-finding-mode-ask-once", e(kind="ruling", mode="ask-once", owner="client:x", words="w"))
add("ack-with-refs", e(type="ack", refs=["x-1"]))
add("retire-with-supersedes", e(type="retire", supersedes=["x-1"]))
add("retire-supersedes-a-a", e(type="retire", supersedes=[TID, TID]))
add("paths-globs", e(paths=["src/**/x.py", "**", "a/**b/c", "docs/*.md"]))
add("path-six-starred-segments", e(paths=["/".join(["a**"] * 6)]))
add("known-finding-target", e(supersedes=[TID], refs=[TID]),
    known={TID: {"type": "finding"}})
add("known-ruling-target-by-ruling", e(type="question", kind="ruling", owner="lead", words="new words", supersedes=[TID]),
    known={TID: RULING})
add("known-ruling-target-by-retire", e(type="retire", words="the decider's words", supersedes=[TID]),
    known={TID: RULING})
add("known-empty-no-links", e(), known={})
add("future-ts-within-a-day", e(ts="2026-10-13T00:00:00Z"))

# ---- boundary accepts: every bounded field at exactly its limit (rev 10 N1) ------------------------------------
add("boundary-owner-200", e(owner="o" * 200))
add("boundary-words-4000", e(words="w" * 4000))
add("boundary-reason-4000", e(reason="r" * 4000))
add("boundary-summary-2000", e(summary="s" * 2000))
add("boundary-recheck-1000", e(recheck="c" * 1000))
add("boundary-paths-100-items", e(paths=[f"p{i}" for i in range(100)]))
add("boundary-supersedes-100-items", e(supersedes=[f"t{i}" for i in range(100)]))
add("boundary-refs-100-items", e(refs=[f"r{i}" for i in range(100)]))
add("boundary-path-300-chars", e(paths=["p" * 300]))
add("boundary-agent-128-chars", e(agent="a" * 128))
add("boundary-session-128-chars", e(session="s" * 128))
add("boundary-author-handle-64-chars", e(author="h" * 64, id="h" * 64 + "-20261010120000-0123abcd"))

# ---- one refusal per raise site, in source order -------------------------------------------------------------
add("r01-unknown-field", e(n=1), expect_branch=1)
add("r02-schema-version", e(v=2), expect_branch=2)
add("r03-agent-not-string", e(agent=5), expect_branch=3)
add("r04-missing-ts", e(ts=DEL), expect_branch=4)
add("r05-id-shape", e(id="not-an-id"), expect_branch=5)
add("r06-id-not-author-handle", e(author="erin"), expect_branch=6)
add("r07-ts-shape", e(ts="2026-10-10 12:00:00"), expect_branch=7)
add("r08-ts-not-a-real-date", e(ts="2026-02-30T00:00:00Z"), expect_branch=8)
add("r09-ts-future", e(ts="2099-01-01T00:00:00Z"), expect_branch=9)
add("r10-agent-not-plain", e(agent="a b"), expect_branch=10)
add("r11-owner-201", e(owner="o" * 201), expect_branch=11)
add("r12-owner-not-printable", e(owner="a\nb"), expect_branch=12)
add("r13-type-unknown", e(type="note"), expect_branch=13)
add("r14-decision-without-kind", e(type="decision"), expect_branch=14)
add("r15-kind-unknown", e(kind="maybe"), expect_branch=15)
add("r16-mode-unknown", e(kind="ruling", mode="loud", owner="lead", words="w"), expect_branch=16)
add("r17-mode-on-practice", e(kind="practice", mode="block"), expect_branch=17)
add("r18-ruling-without-owner", e(kind="ruling", words="w"), expect_branch=18)
add("r19-ruling-without-words", e(kind="ruling", owner="lead"), expect_branch=19)
add("r20-supersedes-not-a-list", e(supersedes=""), expect_branch=20)
add("r21-paths-101-items", e(paths=[f"p{i}" for i in range(101)]), expect_branch=21)
add("r22-path-301-chars", e(paths=["p" * 301]), expect_branch=22)
add("r23-seven-starred-segments", e(paths=["/".join(["a**"] * 7)]), expect_branch=23)
add("r24-segment-with-two-stars", e(paths=["a/**x**/b"]), expect_branch=24)
add("r25-retire-without-supersedes", e(type="retire"), expect_branch=25)
add("r26-ack-without-refs", e(type="ack"), expect_branch=26)
add("r27-ack-with-supersedes", e(type="ack", refs=["x-1"], supersedes=["x-2"]), expect_branch=27)
add("r28-path-absolute", e(paths=["/etc/passwd"]), expect_branch=28)
add("r29-supersedes-itself", e(supersedes=["dana-20261010120000-0123abcd"]), expect_branch=29)
add("r30-unknown-link", e(supersedes=["x-1"]), known={}, expect_branch=30)
add("r31-supersedes-an-ack", e(supersedes=[TID]), known={TID: {"type": "ack"}}, expect_branch=31)
add("r32-supersedes-a-retire", e(supersedes=[TID]), known={TID: {"type": "retire"}}, expect_branch=32)
add("r33-ruling-target-without-words", e(type="retire", supersedes=[TID]), known={TID: RULING}, expect_branch=33)
add("r34-ruling-target-by-a-practice", e(kind="practice", words="w", supersedes=[TID]), known={TID: RULING},
    expect_branch=34)
add("r35-secret", e(words="api_key=sk1234567890abcd"), scan=True, expect_branch=35)

# ---- further refusals that reach the remaining branches -------------------------------------------------------
add("r02-schema-version-missing", e(v=DEL), expect_branch=2)
add("r04-missing-id-empty", e(id=""), expect_branch=4)
add("r10-session-not-plain", e(session=""), expect_branch=10)
add("r11-recheck-1001", e(recheck="c" * 1001), expect_branch=11)
add("r18-ruling-owner-blank", e(kind="ruling", owner="   ", words="w"), expect_branch=18)
add("r19-ruling-words-blank", e(kind="ruling", owner="lead", words=" "), expect_branch=19)
add("r20-supersedes-an-object", e(supersedes={}), expect_branch=20)
add("r20-paths-holds-a-number", e(paths=[1]), expect_branch=20)
add("r20-refs-holds-a-blank", e(refs=["  "]), expect_branch=20)
add("r21-refs-101-items", e(refs=[f"r{i}" for i in range(101)]), expect_branch=21)
add("r28-path-backslash", e(paths=["a\\b"]), expect_branch=28)
add("r28-path-dotdot", e(paths=["a/../b"]), expect_branch=28)
add("r30-unknown-ref", e(refs=["x-9"]), known={TID: {"type": "finding"}}, expect_branch=30)
add("r35-secret-in-summary", e(summary="-----BEGIN RSA PRIVATE KEY-----"), scan=True, expect_branch=35)

# ---- the F9 scope goldens' schema-invalid member findings (Y01 / Z02), recorded here too -----------------------
add("f9-member-finding-supersedes-empty-string", e(supersedes=""), expect_branch=20)
add("f9-member-finding-supersedes-object", e(supersedes={}), expect_branch=20)
add("f9-member-finding-id-not-author-prefixed", e(id="erin-20261010120000-0123abcd"), expect_branch=6)
add("f9-member-finding-malformed-ts", e(ts="2026-10-10T12:00Z"), expect_branch=7)

# ---- now = None skips exactly the future-ts branch (rev 10): 31dea1f with the fixed clock refuses at branch 9 --
FUT = e(ts="2099-01-01T00:00:00Z")
assert oracle(FUT, None, False, NOW)["branch"] == 9
V.append(vector("now-none-skips-the-future-ts-branch", "no_upstream", "validate_entry_v1",
                {"entry": FUT, "known": None, "scan": False, "now": None}, {"verdict": ACCEPT}, NONE_AUTH, None))
FUT_BAD = e(ts="2099-01-01T00:00:00Z", type="note")
assert oracle(FUT_BAD, None, False, NOW)["branch"] == 9
want = oracle(FUT_BAD, None, False, "2099-01-02T00:00:00Z")   # a clock at which the future branch cannot fire
assert want["branch"] == 13
V.append(vector("now-none-still-refuses-every-other-branch", "no_upstream", "validate_entry_v1",
                {"entry": FUT_BAD, "known": None, "scan": False, "now": None}, want, NONE_AUTH, None))

branches = {v["expect"]["levain"].get("branch") for v in V} - {None}
assert branches == set(range(1, 36)), sorted(set(range(1, 36)) - branches)
json.dumps(V)
write("F9 entry schema", "entry", V, oracle=f"levain team/entry.py validate @ {AT}", raise_sites=len(SITES), now=NOW)
