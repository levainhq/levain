"""F9: the team-log scope matrix, entry-id uniqueness and (file, hash) idempotency, against frozen/scope/."""
from datetime import datetime, timezone

import pytest

from levain.contracts.leaves import team as T
from tests._contracts_vectors import check_shape, h, ids, load

VECTORS = load("scope")


class _Held:
    """A snapshot from plain entries (may_write vectors carry entries, not signed leaves)."""

    def __init__(self, entries):
        self.snap = T.Snapshot()
        for e in entries:
            self.snap._by_id.setdefault(e["id"], []).append(e)


@pytest.mark.parametrize("v", VECTORS, ids=ids(VECTORS))
def test_vector(v):
    check_shape(v)
    i, want = v["input"], v["expect"]["levain"]
    if v["op"] == "may_write":
        got = T.may_write_team_log(i["scope"], i["entry"], _Held(i["snapshot"]).snap)
        assert got == {"accept": T.ALLOW, "refuse": T.REFUSE}[want["verdict"]]
    elif v["op"] == "line_log":
        now = (datetime.strptime(i["now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
               if i["at"] == "ingest" else None)
        snap, verdicts, faults = T.Snapshot(), [], []
        for leaf in i["leaves"]:
            verdict, ok, reason = T.judge_line(h(leaf["data_hex"]), origin=i["origin"], scope=leaf["scope"],
                                               snapshot=snap, at=i["at"], now=now)
            if ok is not None:
                snap.add(ok)
            verdicts.append(verdict)
            faults.append(reason.startswith("operator fault"))
        assert verdicts == want["verdicts"]
        if "operator_fault" in want:
            assert faults == want["operator_fault"]
        if i["at"] == "verify":
            assert [x for x, _ in T.verify_team_lines([(h(x["data_hex"]), x["scope"]) for x in i["leaves"]],
                                                      i["origin"])] == want["verdicts"]
    else:
        raise AssertionError(v["op"])


def test_every_scope_and_entry_type_cell_is_pinned():
    cells = {v["id"] for v in VECTORS if v["op"] == "may_write"}
    for scope in T.SCOPES:
        for typ in ("decision", "constraint", "finding", "question", "tension", "ack", "retire"):
            assert f"may-write-{scope}-{typ}" in cells


def test_an_unknown_scope_raises_in_the_matrix():
    with pytest.raises(T.UnknownScopeError):
        T.may_write_team_log("auditor", {"type": "finding", "supersedes": []}, T.Snapshot())


@pytest.mark.parametrize("entry", [{"type": "finding", "supersedes": "x"}, {"type": "finding", "supersedes": [1]}])
def test_the_matrix_is_total_on_unvalidated_input(entry):
    assert T.may_write_team_log("human", entry, T.Snapshot()) == T.REFUSE
