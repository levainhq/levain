"""C15 S0: the class ruling covers every table of a maximal coordinator database, and no schema name is defined twice.

The maximal database is built as the census built it (``project_memory/1030-62-c15/CENSUS.md``): ``EnvelopeStore``
(which makes the base, lease, draft, review, first-reader, lifecycle, operator-view and seam tables and the dispatch
store's own), then ``ChatStore`` (the chat tables, with the turn lease and approvals folded in), then ``EgressRecords``
(``chat_egress_events``, not folded into ``CHAT_SCHEMA`` on this base), all on ONE ``dispatch.db``.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from levain.chat_egress.records import EGRESS_SCHEMA, EgressRecords
from levain.coordinator import seam_classes as sc
from levain.coordinator.chat import CHAT_SCHEMA, ChatStore
from levain.coordinator.envelopes import ENVELOPE_SCHEMA, EnvelopeStore
from levain.coordinator.first_reader import FIRST_READER_SCHEMA
from levain.coordinator.lease import LAUNCH_KINDS, LEASE_KINDS, LEASE_SCHEMA
from levain.coordinator.ready import LIFECYCLE_SCHEMA
from levain.coordinator.review import REVIEW_SCHEMA
from levain.coordinator.seam_handoffs import SEAM_HANDOFF_SCHEMA
from levain.coordinator.store import GSEQ_TABLES, SCHEMA, SEAM_SCHEMA
from levain.coordinator.teamdrafts import DRAFT_SCHEMA
from levain.coordinator.work_delta import OPERATOR_VIEWS_SCHEMA
from levain.dispatch.jobs import _SCHEMA as JOB_SCHEMA


@pytest.fixture(scope="module")
def maximal(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("seam-census")
    base.chmod(0o700)
    store = EnvelopeStore(base / "d")
    store._connect().close()
    ChatStore(store)._connect().close()
    EgressRecords(store)._connect().close()
    return store.path


def _tables(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    finally:
        conn.close()


def _columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_the_classes_do_not_overlap() -> None:
    counted, infra, exempt = set(sc.COUNTED), set(sc.INFRASTRUCTURE), set(sc.EXEMPT)
    assert not counted & infra and not counted & exempt and not infra & exempt
    assert all(isinstance(why, str) and why.strip() for why in sc.EXEMPT.values())     # an exemption carries its reason


def test_every_table_of_the_maximal_database_is_in_exactly_one_class(maximal: Path) -> None:
    tables = _tables(maximal)
    assert "sqlite_sequence" in tables and sc.EXEMPT["sqlite_sequence"] == "SQLite's own"
    assert len(tables) == 57 and len(set(tables)) == 57           # 54 of the census, SQLite's own, S0's bounds table and S1's handoffs
    # `_r4` is the transient name of the review events' rebuild: gone by commit, so a finished store holds none
    assert not [t for t in tables if t.endswith("_r4")]
    known = sc.COUNTED | sc.INFRASTRUCTURE | set(sc.EXEMPT)
    assert sorted(set(tables) - known) == [], "a table with no class: add it to seam_classes with S0_CLASSES.md's ruling"
    assert sorted(known - set(tables)) == [], "a class entry for a table the maximal database does not hold"
    for t in tables:
        assert [t in sc.COUNTED, t in sc.INFRASTRUCTURE, t in sc.EXEMPT].count(True) == 1


def test_every_counted_table_has_a_gseq_column(maximal: Path) -> None:
    missing = [t for t in sorted(sc.COUNTED) if "gseq" not in _columns(maximal, t)]
    assert missing == []
    # the ten the migration reaches are in the counted class, and each took its column from `_add_gseq`
    assert set(GSEQ_TABLES) <= sc.COUNTED
    for t in GSEQ_TABLES:
        assert "gseq" in _columns(maximal, t)


def test_the_migrated_tables_are_the_counted_tables_that_lacked_a_gseq() -> None:
    # S0_CLASSES.md: drops, threads, thread_revisions, thread_events, review_items, review_events, first_reads,
    # team_drafts, team_draft_events gain the column; replies already has it (nullable)
    assert set(GSEQ_TABLES) == {"replies", "drops", "threads", "thread_revisions", "thread_events", "review_items",
                                "review_events", "first_reads", "team_drafts", "team_draft_events"}


def test_the_kind_classes_partition_the_lease_and_launch_kinds(maximal: Path) -> None:
    assert set(sc.KIND_CLASSES) == {"lease_events", "launch_events"}
    for table, kinds in (("lease_events", LEASE_KINDS), ("launch_events", LAUNCH_KINDS)):
        classes = sc.KIND_CLASSES[table]
        assert set(classes) == {"COUNTED", "INFRASTRUCTURE"}
        both = [*classes["COUNTED"], *classes["INFRASTRUCTURE"]]
        assert sorted(both) == sorted(kinds), f"{table}: the kinds are not partitioned exactly once"
        assert len(both) == len(set(both))
        conn = sqlite3.connect(maximal)
        try:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        finally:
            conn.close()
        assert all(f"'{k}'" in sql for k in kinds)               # the table's own CHECK names the same kinds
    assert sc.KIND_CLASSES["lease_events"]["COUNTED"] == ("takeover", "lapse")
    assert sc.KIND_CLASSES["launch_events"]["COUNTED"] == ("failed", "expired", "ended")
    assert {"lease_events", "launch_events"} <= sc.COUNTED


def test_a_fresh_store_bounds_every_migrated_table_at_zero(maximal: Path) -> None:
    conn = sqlite3.connect(maximal)
    try:
        assert dict(conn.execute("SELECT table_name, baseline_rowid FROM gseq_migration_bounds")) == {
            t: 0 for t in GSEQ_TABLES}
        assert [sc.unsequenced(conn, t) for t in GSEQ_TABLES] == [0] * len(GSEQ_TABLES)
        with pytest.raises(sc.SeamClassError):
            sc.unsequenced(conn, "chat_messages")                # never migrated: no bound row
    finally:
        conn.close()


# --- no schema name is defined twice ---------------------------------------------------------------------------------

_DDL = re.compile(r"CREATE\s+(?:UNIQUE\s+)?(TABLE|TRIGGER|INDEX|VIEW)\s+(?:IF NOT EXISTS\s+)?(\w+)", re.I)

#: the statement lists applied to ``dispatch.db`` (the file the census built); every other store is its own file and is
#: not compared. ``TURN_LEASE_SCHEMA`` and ``APPROVAL_SCHEMA`` are spliced into ``CHAT_SCHEMA`` (chat.py), so they are
#: read through it, not as lists of their own
_DISPATCH_DB_LISTS = {
    "dispatch.jobs._SCHEMA": JOB_SCHEMA, "store.SCHEMA": SCHEMA, "lease.LEASE_SCHEMA": LEASE_SCHEMA,
    "teamdrafts.DRAFT_SCHEMA": DRAFT_SCHEMA, "review.REVIEW_SCHEMA": REVIEW_SCHEMA,
    "first_reader.FIRST_READER_SCHEMA": FIRST_READER_SCHEMA, "ready.LIFECYCLE_SCHEMA": LIFECYCLE_SCHEMA,
    "work_delta.OPERATOR_VIEWS_SCHEMA": OPERATOR_VIEWS_SCHEMA, "store.SEAM_SCHEMA": SEAM_SCHEMA,
    "seam_handoffs.SEAM_HANDOFF_SCHEMA": SEAM_HANDOFF_SCHEMA,
    "envelopes.ENVELOPE_SCHEMA": ENVELOPE_SCHEMA, "chat.CHAT_SCHEMA": CHAT_SCHEMA,
    "chat_egress.EGRESS_SCHEMA": EGRESS_SCHEMA,
}


def _declared(statements: tuple[str, ...]) -> list[tuple[str, str]]:
    out = []
    for s in statements:
        m = _DDL.match(s.strip())
        if m is not None:
            out.append((m.group(1).upper(), m.group(2)))
    return out


def test_the_name_scan_reads_every_statement_and_catches_a_seeded_duplicate() -> None:
    # the lists as measured: every statement names an object (247 in all), so the scan below skips nothing
    assert {k: (len(v), len(_declared(v))) for k, v in _DISPATCH_DB_LISTS.items()} == {
        "dispatch.jobs._SCHEMA": (57, 57), "store.SCHEMA": (25, 25), "lease.LEASE_SCHEMA": (29, 29),
        "teamdrafts.DRAFT_SCHEMA": (9, 9), "review.REVIEW_SCHEMA": (15, 15),
        "first_reader.FIRST_READER_SCHEMA": (16, 16), "ready.LIFECYCLE_SCHEMA": (10, 10),
        "work_delta.OPERATOR_VIEWS_SCHEMA": (4, 4), "store.SEAM_SCHEMA": (4, 4),
        "seam_handoffs.SEAM_HANDOFF_SCHEMA": (8, 8),
        "envelopes.ENVELOPE_SCHEMA": (12, 12), "chat.CHAT_SCHEMA": (52, 52), "chat_egress.EGRESS_SCHEMA": (6, 6)}
    # IF NOT EXISTS hides a second declaration from the database: the scan must see it in the source
    assert _declared(("CREATE TABLE IF NOT EXISTS replies (a)", "CREATE UNIQUE INDEX IF NOT EXISTS i ON t (a)",
                      "CREATE TRIGGER x BEFORE INSERT ON t BEGIN SELECT 1; END")) == [
        ("TABLE", "replies"), ("INDEX", "i"), ("TRIGGER", "x")]
    assert _duplicates({**_DISPATCH_DB_LISTS, "seeded": ("CREATE TABLE IF NOT EXISTS replies (a)",)}) == {
        ("TABLE", "replies"): ["store.SCHEMA", "seeded"]}


def _duplicates(lists: dict[str, tuple[str, ...]]) -> dict[tuple[str, str], list[str]]:
    seen: dict[tuple[str, str], list[str]] = {}
    for label, statements in lists.items():
        for key in _declared(statements):
            seen.setdefault(key, []).append(label)
    # what `_add_gseq` makes by name, which `IF NOT EXISTS` would hide if a list declared the same name
    for table in GSEQ_TABLES:
        seen.setdefault(("INDEX", f"{table}_gseq"), []).append("store._add_gseq")
        seen.setdefault(("TRIGGER", f"{table}_gseq_no_replace"), []).append("store._add_gseq")
    return {k: v for k, v in seen.items() if len(v) > 1 and not k[1].endswith("_r4")}


def test_no_table_or_trigger_name_is_declared_twice_in_the_dispatch_db_lists() -> None:
    assert _duplicates(_DISPATCH_DB_LISTS) == {}, "a name declared in more than one statement list of dispatch.db"
    # a table, an index and a view share SQLite's one namespace: no kind may reuse another kind's name either
    shared = [n for lst in _DISPATCH_DB_LISTS.values() for kind, n in _declared(lst) if kind != "TRIGGER"]
    shared += [f"{t}_gseq" for t in GSEQ_TABLES]
    assert len(shared) == len(set(shared))


def test_the_declared_names_are_the_names_the_database_holds(maximal: Path) -> None:
    conn = sqlite3.connect(maximal)
    try:
        held = {(r[0].upper(), r[1]) for r in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()
    declared: set[tuple[str, str]] = set()
    for statements in _DISPATCH_DB_LISTS.values():
        declared |= set(_declared(statements))
    # everything the schema lists declare is in the file; the file holds only more of the migrations' own names
    # (`_add_gseq`'s index and trigger per table, R4's one-signature index) and what the stores make by other means
    assert sorted(declared - held) == []


_COORD_SRC = Path(__file__).resolve().parent.parent / "levain" / "coordinator"


def _insert_sites_without_gseq(text: str) -> list[tuple[str, str]]:
    """(table, column list) of every ``INSERT INTO <gseq table> (...)`` in ``text`` whose column list lacks ``gseq``;
    the list runs to its closing parenthesis, across the string-literal line breaks a long statement is written in."""
    pattern = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(" + "|".join(GSEQ_TABLES) + r")\s*\(([^)]*)\)")
    return [(m.group(1), " ".join(m.group(2).split())) for m in pattern.finditer(text)
            if not re.search(r"\bgseq\b", m.group(2))]


def test_every_insert_into_a_gseq_table_names_gseq() -> None:
    sources = {str(p.relative_to(_COORD_SRC)): p.read_text() for p in sorted(_COORD_SRC.rglob("*.py"))}
    found = {t: 0 for t in GSEQ_TABLES}
    for text in sources.values():                                  # the scan is not vacuous: every table has a site
        for t in GSEQ_TABLES:
            found[t] += len(re.findall(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+" + t + r"\s*\(", text))
    assert all(found.values()), found
    holes = {name: _insert_sites_without_gseq(text) for name, text in sources.items()}
    assert {n: h for n, h in holes.items() if h} == {}

    # the scanner reports a site that omits the column: one INSERT of a scratch copy loses `, gseq`
    victim = "teamdrafts.py"
    assert ", gseq)" in sources[victim]
    broken = sources[victim].replace(", gseq)", ")", 1)
    reported = _insert_sites_without_gseq(broken)
    assert len(reported) == 1 and reported[0][0] in GSEQ_TABLES and "gseq" not in reported[0][1]


def test_no_insert_into_a_gseq_table_hides_its_column_list_or_lives_outside_the_coordinator() -> None:
    pkg = _COORD_SRC.parent                                      # the whole levain package, not only coordinator/
    bare = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?:" + "|".join(GSEQ_TABLES) + r")\s*(?!\s*\()(?=VALUES|SELECT|\s)", re.I)
    outside = []
    for path in sorted(pkg.rglob("*.py")):
        text = path.read_text()
        if re.search(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?:" + "|".join(GSEQ_TABLES) + r")\b", text):
            if _COORD_SRC not in path.parents:
                outside.append(path.name)
            elif bare.search(text):
                outside.append(f"{path.name}: positional insert")
    assert outside == []


def test_a_locked_bounds_read_is_not_reported_as_never_migrated(tmp_path) -> None:
    import sqlite3
    from levain.coordinator import seam_classes

    class _Locked:
        def execute(self, *_a):
            raise sqlite3.OperationalError("database schema is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        seam_classes.unsequenced(_Locked(), "drops")            # type: ignore[arg-type]
