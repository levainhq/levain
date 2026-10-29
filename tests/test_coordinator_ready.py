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

from levain.coordinator import ready, review
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
    # flow's fixture grows by syncing it: any count, every case named once
    assert data["schema"] == 1 and CASES and len({c["name"] for c in CASES}) == len(CASES)


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
        except BaseException as exc:  # noqa: BLE001 - a thread that died (a busy timeout) is named below
            results.append(exc)

    ts = [threading.Thread(target=mover, args=(to,)) for to in ("running", "waiting")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    assert not any(isinstance(r, BaseException) for r in results), results
    recs = ready.journal(store)
    # both moves landed, one after the other: the second's from is the first's to (never a lost update)
    assert [r["to_category"] for r in recs[1:]] in (["running", "waiting"], ["waiting", "running"])
    assert recs[2]["from_category"] == recs[1]["to_category"]


# ------------------------------------------------------------------------------------------------ fix round 1
def _pre_c13_store(directory, monkeypatch):
    """A coordinator store with the schema at origin/main (``git show origin/main:levain/coordinator/store.py``): its
    ``_statements`` is this one's less ``LIFECYCLE_SCHEMA``, so the store is made here with that one tuple empty."""
    directory.mkdir(mode=0o700)
    with monkeypatch.context() as m:
        m.setattr(ready, "LIFECYCLE_SCHEMA", ())
        old = CoordStore(directory)
        token = old.coord_take()
        review.file_item(old, "seat", {"text": "made before C13"}, dedupe_key="seat:pre")
    c = sqlite3.connect(directory / "dispatch.db")
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    c.close()
    assert not names & {"ready_items", "ready_journal"} and "review_items" in names
    return token


def test_a_store_made_before_c13_is_current_for_every_reader(tmp_path, monkeypatch):
    from levain.cockpit import coordinator_panels
    from levain.coordinator import morning

    d = tmp_path / "dispatch"
    token = _pre_c13_store(d, monkeypatch)
    store = CoordStore(d)
    store.open_readonly().close()                                    # not StoreAbsent
    assert [i.body["text"] for i in review.list_items(store)] == ["made before C13"]
    assert morning.card(store, queue_dir=tmp_path / "queue")["errors"] == []
    assert coordinator_panels._store_at(lambda: d) is not None
    out = ready.list(store, all=True)
    assert out["items"] == [] and out["unreadable"] == []            # an empty queue, not "unreadable"
    assert ready.journal(store) == [] and ready.journal(store, "levain:gate") == []
    c = _raw(store)
    assert "ready_items" not in {r[0] for r in c.execute("SELECT name FROM sqlite_master")}   # readers made nothing
    c.close()
    # the first ready write migrates it, in its own transaction
    _add(CoordStore(d), token, to="ready", tier="sonnet", done_line="green")
    other = CoordStore(d)                                            # a second opener sees it
    assert [i["id"] for i in ready.list(other)["items"]] == ["levain:gate"]
    assert [r["source"] for r in ready.journal(other)] == ["add"]
    assert [i.body["text"] for i in review.list_items(other)] == ["made before C13"]


def test_a_pre_c13_store_is_migrated_by_the_stores_own_opener_too(tmp_path, monkeypatch):
    d = tmp_path / "dispatch"
    _pre_c13_store(d, monkeypatch)
    review.file_item(CoordStore(d), "seat", {"text": "after"}, dedupe_key="seat:post")      # any read-write opener
    c = sqlite3.connect(d / "dispatch.db")
    assert {"ready_items", "ready_journal"} <= {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    c.close()


def test_one_lifecycle_table_without_the_other_is_unreadable(store, token):
    _add(store, token)
    c = _raw(store)
    c.execute("PRAGMA writable_schema = ON")
    c.execute("DELETE FROM sqlite_master WHERE tbl_name = 'ready_journal'")
    c.commit()
    c.close()
    assert ready.list(CoordStore(store.directory))["unreadable"] == ["coordinator"]


@pytest.mark.parametrize("text, named", [
    ("ship levain:gate now?", True), ("ship levain:gate.", True), ("levain:gate: blocked on keys", True),
    ("(levain:gate)", True), ("levain:gate, then more", True), ("levain:gate-", True),
    ("ship levain:gate-two now?", False), ("ship levain:gate:deploy after CI", False),
    ("ship levain:gate:2", False), ("read levain:gate.md", False), ("levain:gate.v2 only", False),
    ("levain:gate_x only", False), ("levain:gate+1", False), ("xlevain:gate", False), ("a.levain:gate", False),
    ("-levain:gate", False), ("_levain:gate", False), (":levain:gate", False),
])
def test_needs_phill_names_whole_ids_by_the_id_alphabet(text, named):
    by = {i["id"]: i for i in ready.derive_items(
        {"store": [_store_row("levain:gate")], "needs_phill": [{"id": "np-1", "repo": "levain", "what": text}]})}
    assert by["levain:gate"]["needs_phill"] is named
    assert by["levain:gate"]["status_category"] == ("needs-you" if named else "ready")


def test_a_dangling_link_is_unreadable_never_nothing_waiting(tmp_path):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    (d / "dispatch.db").symlink_to(d / "gone.db")
    out = ready.list(d)
    assert out["items"] == [] and out["unreadable"] == ["coordinator"]
    with pytest.raises(CoordError):
        ready.journal(d)


def test_a_missing_directory_or_file_is_no_store(tmp_path):
    assert ready.list(tmp_path / "nope")["unreadable"] == [] and ready.journal(tmp_path / "nope") == []
    d = tmp_path / "empty"
    d.mkdir(mode=0o700)
    assert ready.list(d)["unreadable"] == [] and ready.journal(d) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses any directory")
def test_an_untraversable_directory_is_unreadable(tmp_path):
    outer = tmp_path / "outer"
    d = outer / "dispatch"
    d.mkdir(parents=True, mode=0o700)
    CoordStore(d).coord_take()
    outer.chmod(0o000)
    try:
        assert ready.list(d)["unreadable"] == ["coordinator"]
    finally:
        outer.chmod(0o700)


def test_an_insecure_directory_is_named_unreadable_not_raised(store, token):
    _add(store, token, to="ready")
    store.directory.chmod(0o755)
    try:
        out = ready.list(CoordStore(store.directory))
    finally:
        store.directory.chmod(0o700)
    assert out["items"] == [] and out["unreadable"] == ["coordinator"]


def _journal_only_v2(store):
    """Direct SQL, never the API: journal v2 committed without its state row (the trigger allows one ahead)."""
    c = _raw(store)
    c.execute("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
              "to_category, who, why, source, applied_by, lease_token) VALUES ('levain:gate', 2, 99999, 0, 'i', 'a', "
              "'levain', 'ready', 'running', 'ready', 'running', 'sql', 'by hand', 'move', NULL, 1)")
    c.commit()
    c.close()


def test_a_journal_ahead_of_state_is_named_and_every_move_fails_loud(store, token):
    _add(store, token, to="ready")
    _add(store, token, "levain:fine", title="Fine", to="ready")
    _journal_only_v2(store)
    out = ready.list(CoordStore(store.directory), all=True)
    assert out["unreadable"] == ["coordinator:levain:gate (journal v2, state v1)"]
    assert {i["id"] for i in out["items"]} == {"levain:gate", "levain:fine"}         # shown at its state, and named
    for verb in (ready.move, ready.correct):
        with pytest.raises(ready.JournalAhead, match=r"levain:gate: its journal is at version 2 but its state is at "
                                                     r"version 1") as e:
            verb(store, token, "levain:gate", to="done", who="desk", why="landed.")
        assert (e.value.item_id, e.value.journal_ver, e.value.state_ver) == ("levain:gate", 2, 1)
    assert _counts(store) == (3, 2)                                              # nothing written, nothing healed
    ready.move(store, token, "levain:fine", to="running", who="desk", why="another item still moves.")


def test_a_state_row_with_a_wrong_shaped_depends_on_is_unreadable(store, token):
    _add(store, token, to="ready", tier="sonnet", done_line="green")
    c = _raw(store)
    c.execute("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
              "to_category, who, why, source, applied_by, lease_token) VALUES ('levain:held', 1, 99998, 0, 'i', 'a', "
              "'levain', NULL, 'ready', NULL, 'ready', 'sql', 'by hand', 'add', NULL, 1)")
    c.execute("INSERT INTO ready_items (id, ver, repo, title, status, category, tier, done_line, depends_on, rank, "
              "set_by, set_at) VALUES ('levain:held', 1, 'levain', 't', 'ready', 'ready', 'sonnet', 'g', "
              "'\"levain:blocker\"', 1, 'sql', 'x')")
    c.commit()
    c.close()
    out = ready.list(CoordStore(store.directory))
    assert out["items"] == [] and out["unreadable"] == ["coordinator"]          # never "independently ready"


def test_a_projection_dependency_and_a_cycle_are_refused(store, token):
    with pytest.raises(CoordError, match="projection id"):
        _add(store, token, depends_on=["seat:1028+15"])
    _add(store, token, "levain:a", title="A", depends_on=["levain:b"])
    _add(store, token, "levain:b", title="B", depends_on=["levain:c"])
    with pytest.raises(CoordError, match="cycle"):
        _add(store, token, "levain:c", title="C", depends_on=["levain:a"])
    _add(store, token, "levain:c", title="C")
    with pytest.raises(CoordError, match="cycle"):
        ready.move(store, token, "levain:c", to="backlog", who="desk", why="hold it.", depends_on=["levain:a"])
    ready.move(store, token, "levain:c", to="backlog", who="desk", why="hold it.", depends_on=["levain:x"])
    assert _counts(store) == (4, 4)


@pytest.mark.parametrize("bad", ["a\x0bb", "a\x0cb", "a\x1cb", "a\x85b", "a b", "a\x1b[31mb", "a\tb"])
def test_a_line_with_a_break_or_a_control_character_is_refused(store, token, bad):
    for field in ("why", "who", "title", "done_line"):
        with pytest.raises(CoordError, match=field):
            _add(store, token, **{field: bad})
    assert _counts(store) == (0, 0)


def test_a_title_is_written_no_longer_than_it_is_read(store, token):
    with pytest.raises(CoordError, match="title"):
        _add(store, token, title="t" * 121)
    _add(store, token, title="t" * 120, to="ready")
    assert ready.list(store)["items"][0]["title"] == "t" * 120


def test_a_caller_unreadable_string_is_one_entry(store):
    assert ready.list(store, sources={"unreadable": "band"})["unreadable"] == ["band"]
