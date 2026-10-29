"""C13: the ready queue's pure reader against flow's shared conformance fixture, and the coordinator store's ONE writer
of lifecycle state + its append-only transition journal (READY_SCHEMA v1). Synthetic item names; a scratch store."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from levain.coordinator import ready
from levain.coordinator.lease import LeaseRefused
from levain.coordinator.store import CoordError, CoordStore

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ready_schema" / "ready_v1.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


# ------------------------------------------------------------------------------------------------ conformance
@pytest.mark.parametrize("case", CASES, ids=[c["name"][:60] for c in CASES])
def test_fixture_case(case):
    out = ready.derive(case["input"], generated="2026-10-28T21:00:00-04:00")
    assert out["schema"] == 1 and out["unreadable"] == []
    got = out["items"]
    assert [i["id"] for i in got] == [e["id"] for e in case["expect"]]
    for item, want in zip(got, case["expect"]):
        assert {k: item[k] for k in want} == want, item["id"]


def test_fixture_is_flows_schema_1():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == 1 and len(CASES) == 5


def test_every_item_carries_exactly_the_schema_fields():
    fields = {"id", "repo", "title", "done_line", "tier", "depends_on", "status", "status_category",
              "declared_category", "independent", "held_reason", "needs_phill", "well_formed", "rank", "provenance"}
    for case in CASES:
        for it in ready.derive_items(case["input"]):
            extra = set(it) - fields
            assert extra <= {"unknown_category"}, extra
            assert fields <= set(it)
            assert set(it["provenance"]) == {"source", "ref", "set_by", "set_at"}


def test_unknown_category_is_reported():
    (case,) = [c for c in CASES if "unknown category" in c["name"]]
    by = {i["id"]: i for i in ready.derive_items(case["input"])}
    assert by["spore-1005"]["unknown_category"] == "frozen"
    assert by["spore-1006"]["unknown_category"] is None


def test_derive_is_pure_and_does_not_mutate_its_input():
    src = json.loads(json.dumps(CASES[0]["input"]))
    before = json.dumps(src, sort_keys=True)
    assert ready.derive_items(src) == ready.derive_items(src)
    assert json.dumps(src, sort_keys=True) == before


def _store_row(id, category="ready", *, rank=1, depends_on=(), repo="levain", tier="sonnet", done_line="green"):
    return {"id": id, "repo": repo, "title": id, "category": category, "status": category, "tier": tier,
            "done_line": done_line, "depends_on": [*depends_on], "rank": rank, "set_by": "desk", "set_at": "x"}


def test_unknown_dependency_holds_the_item():
    items = ready.derive_items({"store": [_store_row("levain:a", depends_on=["levain:nowhere"])]})
    (a,) = items
    assert (a["status_category"], a["declared_category"]) == ("backlog", "ready")
    assert a["held_reason"] == "depends on levain:nowhere" and a["independent"] is False


def test_done_dependency_releases_and_first_unmet_is_named():
    rows = [_store_row("levain:a", "done"), _store_row("levain:b", "backlog"),
            _store_row("levain:c", depends_on=["levain:a", "levain:b", "levain:zz"])]
    c = {i["id"]: i for i in ready.derive_items({"store": rows})}["levain:c"]
    assert c["held_reason"] == "depends on levain:b"
    rows[1] = _store_row("levain:b", "done")
    rows[2] = _store_row("levain:c", depends_on=["levain:a", "levain:b"])
    c = {i["id"]: i for i in ready.derive_items({"store": rows})}["levain:c"]
    assert c["status_category"] == "ready" and c["independent"] is True


def test_rank_ties_break_by_id():
    rows = [_store_row("levain:zeta", rank=5), _store_row("levain:alpha", rank=5), _store_row("levain:mid", rank=5),
            _store_row("levain:first", rank=1), _store_row("flow-x", rank=9, repo="flow")]
    ids = [i["id"] for i in ready.derive_items({"store": rows})]
    assert ids == ["flow-x", "levain:first", "levain:alpha", "levain:mid", "levain:zeta"]


def test_needs_phill_naming_matches_whole_ids_only():
    rows = [_store_row("levain:gate"), _store_row("levain:gate-two", rank=2)]
    by = {i["id"]: i for i in ready.derive_items(
        {"store": rows, "needs_phill": [{"id": "np-1", "repo": "levain", "what": "ship levain:gate-two now?"}]})}
    assert by["levain:gate-two"]["status_category"] == "needs-you"
    assert by["levain:gate"]["status_category"] == "ready" and by["levain:gate"]["needs_phill"] is False


def test_store_item_shadows_a_plan_row_of_the_same_id():
    plan = [{"repo": "levain", "n": 1, "text": "**Land the gate** design first."}]
    items = ready.derive_items({"store": [_store_row("levain:land-the-gate", "running")], "plan": plan})
    (it,) = items
    assert it["status_category"] == "running" and it["provenance"]["source"] == "plan"
    assert it["provenance"]["set_by"] == "desk"


def test_ready_counts_count_only_well_formed_ready():
    rows = [_store_row("levain:a"), _store_row("levain:b", rank=2, tier=None), _store_row("levain:c", "backlog"),
            _store_row("levain:d", rank=4)]
    assert ready.ready_counts(ready.derive_items({"store": rows})) == {"levain": 2}
    assert ready.HEALTHY_AT == 3


def test_handoff_pointer_is_pure():
    s = {"id": "spore-9", "status": "open", "disposition": "handoff", "pointer": "projects/flow/x.md", "text": "t"}
    assert ready.derive_items({"spores": [s]})[0]["repo"] == "projects"
    assert ready.derive_items({"spores": [s], "repos": ["levain"]})[0]["repo"] == "flow"
    s2 = dict(s, pointer="/h/Briefcase/.levain-1028-15/x.md")
    assert ready.derive_items({"spores": [s2], "home": "/h"})[0]["repo"] == "levain"
    s3 = dict(s, pointer="/elsewhere/x.md")
    assert ready.derive_items({"spores": [s3], "home": "/h"}) == []


# ------------------------------------------------------------------------------------------------ the writer
@pytest.fixture
def store(tmp_path):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    return CoordStore(d)


@pytest.fixture
def token(store):
    return store.coord_take()


def _raw(store):
    return sqlite3.connect(store.path)


def _counts(store):
    c = _raw(store)
    try:
        return (c.execute("SELECT COUNT(*) FROM ready_journal").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM ready_items").fetchone()[0])
    finally:
        c.close()


def _add(store, token, id="levain:gate", **kw):
    kw.setdefault("repo", "levain")
    kw.setdefault("title", "Land the gate")
    kw.setdefault("who", "desk")
    kw.setdefault("why", "the gate is the next thing to land.")
    return ready.add(store, token, id, **kw)


def test_add_move_correct_each_write_one_record(store, token):
    r1 = _add(store, token, to="ready", tier="opus", done_line="main is green")
    assert (r1["source"], r1["from_category"], r1["to_category"], r1["applied_by"]) == ("add", None, "ready", None)
    assert set(r1) == set(ready._JOURNAL_FIELDS) and isinstance(r1["ts"], float)
    assert _counts(store) == (1, 1)
    r2 = ready.move(store, token, "levain:gate", to="running", who="1028+15 levain-seat", why="the seat took it.")
    assert (r2["from_category"], r2["to_category"], r2["from_status"], r2["to_status"]) == \
        ("ready", "running", "ready", "running")
    r3 = ready.correct(store, token, "levain:gate", to="ready", who="desk", why="the seat never started it.")
    assert r3["source"] == "correction" and r3["from_category"] == "running"
    assert _counts(store) == (3, 3)
    recs = ready.journal(store)
    assert [r["source"] for r in recs] == ["add", "move", "correction"]
    assert recs == ready.journal(store, "levain:gate") and ready.journal(store, "levain:other") == []


def test_list_reads_the_store_and_filters_ready(store, token):
    _add(store, token, to="ready", tier="sonnet", done_line="green")
    _add(store, token, "levain:held", title="Held", to="ready", depends_on=["levain:gate"])
    _add(store, token, "levain:later", title="Later", to="backlog")
    out = ready.list(store, generated="g")
    assert out["schema"] == 1 and out["unreadable"] == [] and out["generated"] == "g"
    assert [i["id"] for i in out["items"]] == ["levain:gate"]
    gate = out["items"][0]
    assert gate["well_formed"] is True and gate["provenance"]["set_by"] == "desk"
    every = ready.list(store, all=True)["items"]
    assert [i["id"] for i in every] == ["levain:held", "levain:later", "levain:gate"]
    assert every[0]["held_reason"] == "depends on levain:gate"
    assert ready.list(store, all=True, repo="flow")["items"] == []
    ready.move(store, token, "levain:gate", to="done", who="desk", why="landed on main.")
    assert [i["id"] for i in ready.list(store)["items"]] == ["levain:held"]
    assert ready.list is ready.list_ready


def test_list_merges_caller_projections(store, token):
    _add(store, token, to="ready", tier="sonnet", done_line="green")
    out = ready.list(store, all=True, sources={"needs_phill": [{"id": "np-abc", "repo": "levain",
                                                                 "what": "may levain:gate land friday?"}],
                                                "seats": [{"token": "1028+15", "repo": "levain"}]})
    by = {i["id"]: i for i in out["items"]}
    assert by["levain:gate"]["status_category"] == "needs-you" and by["levain:gate"]["declared_category"] == "ready"
    assert by["seat:1028+15"]["status_category"] == "running"


def test_reads_need_no_coordinator_and_no_lock(store, token):
    _add(store, token, to="ready", tier="sonnet", done_line="green")
    store.coord_release(None, by="operator")             # no coordinator holds the lease any more
    assert store.coord_status()["lease"] is None
    blocker = _raw(store)
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")                   # a writer holds the write lock, mid-transaction
    try:
        assert [i["id"] for i in ready.list(CoordStore(store.directory))["items"]] == ["levain:gate"]
        assert len(ready.journal(store.directory)) == 1
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    with pytest.raises(LeaseRefused):                    # but a write does need one
        ready.move(store, token, "levain:gate", to="running", who="desk", why="go.")


def test_list_with_no_store_is_empty_and_unreadable_store_is_named(tmp_path):
    d = tmp_path / "none"
    out = ready.list(d)
    assert out["items"] == [] and out["unreadable"] == []
    d.mkdir(mode=0o700)
    (d / "dispatch.db").write_bytes(b"not a database at all, sixteen+ bytes")
    out = ready.list(d)
    assert out["items"] == [] and out["unreadable"] == ["coordinator"]


@pytest.mark.parametrize("why", ["", "   ", None, "two\nlines"])
def test_empty_or_multiline_why_is_refused(store, token, why):
    with pytest.raises(CoordError, match="why"):
        _add(store, token, why=why)
    assert _counts(store) == (0, 0)


def test_empty_who_is_refused(store, token):
    with pytest.raises(CoordError, match="who"):
        _add(store, token, who=" ")


def test_no_coordinator_identity_is_refused(store, token):
    with pytest.raises(CoordError, match="coordinator's identity"):
        _add(store, None)
    with pytest.raises(CoordError, match="lease token"):
        _add(store, "1")
    with pytest.raises(LeaseRefused):                    # a token that is not the live lease's: the fence refuses
        _add(store, token + 1000)
    assert _counts(store) == (0, 0)


@pytest.mark.parametrize("who", ["1028+15 levain-seat", "desk", "phill"])
def test_move_into_needs_you_is_refused_for_every_actor(store, token, who):
    _add(store, token, to="ready")
    with pytest.raises(CoordError, match="projection of the band"):
        ready.move(store, token, "levain:gate", to="needs-you", who=who, why="blocked on the operator.")
    with pytest.raises(CoordError, match="projection of the band"):
        ready.correct(store, token, "levain:gate", to="needs-you", who=who, why="blocked on the operator.")
    with pytest.raises(CoordError, match="projection of the band"):
        _add(store, token, "levain:other", to="needs-you", who=who)
    assert _counts(store) == (1, 1)


def test_needs_you_is_refused_by_the_table_too(store, token):
    _add(store, token, to="ready")
    c = _raw(store)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, "
                  "from_category, to_category, who, why, source, applied_by, lease_token) VALUES ('levain:gate', 2, "
                  "99999, 0, 'i', 'a', 'levain', 'ready', 'needs-you', 'ready', 'needs-you', 'x', 'y', 'move', NULL, 1)")
    c.close()


@pytest.mark.parametrize("to", ["frozen", "Ready", "", None])
def test_unknown_category_is_refused(store, token, to):
    with pytest.raises(CoordError, match="unknown category"):
        _add(store, token, to=to)


@pytest.mark.parametrize("bad", ["seat:1028+15", "ci:levain-02da", "np-aaaa1111", "", "has space", "x" * 201])
def test_projection_and_malformed_ids_are_never_writable(store, token, bad):
    with pytest.raises(CoordError):
        _add(store, token, bad)


def test_add_refuses_an_existing_id_and_move_refuses_a_missing_one(store, token):
    _add(store, token)
    with pytest.raises(CoordError, match="already exists"):
        _add(store, token)
    with pytest.raises(CoordError, match="add it first"):
        ready.move(store, token, "levain:nope", to="ready", who="desk", why="go.")
    assert _counts(store) == (1, 1)


def test_a_write_that_changes_nothing_is_refused(store, token):
    _add(store, token, to="ready")
    with pytest.raises(CoordError, match="nothing to write"):
        ready.move(store, token, "levain:gate", to="ready", who="desk", why="again.")
    r = ready.move(store, token, "levain:gate", to="ready", who="desk", why="bump it up.", rank=0)
    assert r["from_category"] == r["to_category"] == "ready"


def test_field_checks(store, token):
    with pytest.raises(CoordError, match="tier"):
        _add(store, token, tier="gpt")
    with pytest.raises(CoordError, match="itself"):
        _add(store, token, depends_on=["levain:gate"])
    with pytest.raises(CoordError, match="list"):
        _add(store, token, depends_on="levain:x")
    with pytest.raises(CoordError, match="repo"):
        _add(store, token, repo="")
    with pytest.raises(CoordError, match="rank"):
        _add(store, token, rank=-1)
    with pytest.raises(CoordError, match="title"):
        _add(store, token, title="")


def test_default_rank_is_next_in_repo(store, token):
    _add(store, token, "levain:a")
    _add(store, token, "levain:b", rank=7)
    _add(store, token, "levain:c")
    _add(store, token, "flow-a", repo="flow")
    rows = {r["id"]: r["rank"] for r in ready._read(store, ready._current)}
    assert rows == {"levain:a": 1, "levain:b": 7, "levain:c": 8, "flow-a": 1}


def test_replay_reproduces_declared_category(store, token):
    _add(store, token, "levain:a", to="ready")
    _add(store, token, "levain:b")
    ready.move(store, token, "levain:a", to="running", who="seat", why="took it.")
    ready.move(store, token, "levain:b", to="ready", who="desk", why="cleared.")
    ready.correct(store, token, "levain:a", to="waiting", who="desk", why="it is in CI, not running.")
    declared = {i["id"]: i["declared_category"] for i in ready.list(store, all=True)["items"]}
    assert ready.replay(ready.journal(store)) == declared == {"levain:a": "waiting", "levain:b": "ready"}


# ------------------------------------------------------------------------------------------------ adversarial
def test_failing_second_insert_rolls_back_the_journal_record(store, token, monkeypatch):
    _add(store, token, to="ready")

    def boom(*a, **kw):
        raise sqlite3.OperationalError("disk gone between statements")

    monkeypatch.setattr(ready, "_insert_state", boom)
    with pytest.raises(sqlite3.OperationalError):
        ready.move(store, token, "levain:gate", to="running", who="desk", why="go.")
    assert _counts(store) == (1, 1)
    monkeypatch.undo()
    assert ready.move(store, token, "levain:gate", to="running", who="desk", why="go.")["from_category"] == "ready"


def test_failing_state_insert_at_the_database_rolls_back_too(store, token):
    _add(store, token, to="ready")
    c = _raw(store)
    c.execute("CREATE TRIGGER t_fail BEFORE INSERT ON ready_items BEGIN SELECT RAISE(ABORT, 'boom'); END")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        ready.move(store, token, "levain:gate", to="running", who="desk", why="go.")
    assert _counts(store) == (1, 1)
    c.execute("DROP TRIGGER t_fail")
    c.commit()
    c.close()


_CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from levain.coordinator import ready
from levain.coordinator.store import CoordStore
real = ready._insert_journal
def journal_then_die(*a, **kw):
    real(*a, **kw)
    os._exit(9)          # killed between the journal insert and the state insert, mid-transaction
ready._insert_journal = journal_then_die
ready.move(CoordStore(sys.argv[2]), int(sys.argv[3]), "levain:gate", to="running", who="desk", why="go.")
"""


def test_a_killed_writer_leaves_neither_row(store, token):
    _add(store, token, to="ready")
    cp = subprocess.run([sys.executable, "-c", _CHILD, str(REPO), str(store.directory), str(token)],
                        capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(REPO)})
    assert cp.returncode == 9, cp.stderr
    assert _counts(store) == (1, 1)
    assert ready.replay(ready.journal(store)) == {"levain:gate": "ready"}
    assert ready.move(store, token, "levain:gate", to="running", who="desk", why="go.")["from_category"] == "ready"


def test_a_state_row_without_its_journal_record_is_refused_by_the_table(store, token):
    _add(store, token, to="ready")
    c = _raw(store)
    with pytest.raises(sqlite3.IntegrityError, match="only with its journal record"):
        c.execute("INSERT INTO ready_items (id, ver, repo, title, status, category, tier, done_line, depends_on, "
                  "rank, set_by, set_at) VALUES ('levain:gate', 2, 'levain', 't', 'done', 'done', NULL, NULL, '[]', 1, "
                  "'desk', 'x')")
    c.close()


def test_a_journal_record_that_is_not_the_next_transition_is_refused(store, token):
    _add(store, token, to="ready")
    c = _raw(store)
    base = ("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
            "to_category, who, why, source, applied_by, lease_token) VALUES ('levain:gate', ?, ?, 0, 'i', 'a', ?, ?, "
            "'done', ?, 'done', 'desk', 'why', 'move', NULL, 1)")
    for ver, repo, frm in ((3, "levain", "ready"), (2, "levain", "backlog"), (2, "flow", "ready")):
        with pytest.raises(sqlite3.IntegrityError, match="next transition"):
            c.execute(base, (ver, 90000 + ver, repo, frm, frm))
    c.close()


def test_journal_is_append_only_by_trigger(store, token):
    _add(store, token, to="ready")
    c = _raw(store)
    for stmt in ("UPDATE ready_journal SET why = 'rewritten'", "DELETE FROM ready_journal",
                 "UPDATE ready_items SET category = 'done'", "DELETE FROM ready_items"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(stmt)
    # a replacing INSERT (which deletes without firing the delete trigger) is refused too (either trigger may fire first)
    with pytest.raises(sqlite3.IntegrityError, match="never replaced|next transition"):
        c.execute("INSERT OR REPLACE INTO ready_journal SELECT * FROM ready_journal")
    with pytest.raises(sqlite3.IntegrityError, match="never replaced|journal record"):
        c.execute("INSERT OR REPLACE INTO ready_items SELECT * FROM ready_items")
    c.close()
    assert _counts(store) == (1, 1)


def test_two_concurrent_writers_serialize(store, token):
    errors: list = []
    n = 12

    def writer(prefix: str) -> None:
        s = CoordStore(store.directory)          # its own store object, its own connections
        try:
            for i in range(n):
                ready.add(s, token, f"levain:{prefix}-{i}", repo="levain", title=f"{prefix} {i}", who=prefix,
                          why="parallel add.", to="ready")
                ready.move(s, token, f"levain:{prefix}-{i}", to="running", who=prefix, why="parallel move.")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    assert errors == []
    assert _counts(store) == (4 * n, 4 * n)
    c = _raw(store)
    ranks = [r[0] for r in c.execute("SELECT rank FROM ready_items WHERE ver = 1 ORDER BY rank")]
    gseqs = [r[0] for r in c.execute("SELECT gseq FROM ready_journal")]
    c.close()
    assert ranks == [*range(1, 2 * n + 1)]          # each default rank read and written under one lock
    assert len(set(gseqs)) == len(gseqs)


def test_two_writers_racing_one_item_both_journal_in_order(store, token):
    _add(store, token, to="ready")
    barrier = threading.Barrier(2)
    results: list = []

    def mover(to: str) -> None:
        barrier.wait()
        try:
            results.append(ready.move(CoordStore(store.directory), token, "levain:gate", to=to, who=to,
                                      why="racing."))
        except CoordError as exc:
            results.append(exc)

    ts = [threading.Thread(target=mover, args=(to,)) for to in ("running", "waiting")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    recs = ready.journal(store)
    # both moves landed, one after the other: the second's from is the first's to (never a lost update)
    assert [r["to_category"] for r in recs[1:]] in (["running", "waiting"], ["waiting", "running"])
    assert recs[2]["from_category"] == recs[1]["to_category"]
