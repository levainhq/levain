"""The per-model outcome ledger's data layer (F7 slice a): the store's triggers and idempotence, the label/source rule, and
the hierarchical rollup with its min_n floor and the answered/requested model split."""
from __future__ import annotations

import sqlite3
from fractions import Fraction

import pytest

from levain.dispatch import DispatchError
from levain.dispatch.outcomes import (LABELS, REVERTED_CAVEAT, SOURCES, OutcomeRow, OutcomeStore, cell_line,
                                      finest_reporting, rollup)

AT = "2026-10-19T12:00:00.000000Z"


def row(job="j1", run=0, label="adopted", *, requested="glm-5.2", answered="glm-5.2", route="R0", harness="codex 0.5",
        template="fix-lint", **kw):
    source = kw.pop("source", SOURCES.get(label, "human"))
    if label.startswith("verifier") and "verifier_kind" not in kw:
        kw["verifier_kind"] = "black-box"
    return OutcomeRow(job, run, requested, answered, route, harness, template, label, source, AT, **kw)


@pytest.fixture
def store(tmp_path):
    return OutcomeStore(tmp_path / "dispatch")


# --- the row ----------------------------------------------------------------------------------------------------

def test_every_label_has_one_source_and_the_other_is_refused():
    assert set(SOURCES) == set(LABELS) and len(LABELS) == 13
    for label, source in SOURCES.items():
        row(label=label)                                            # its own source is accepted
        other = "human" if source == "machine" else "machine"
        with pytest.raises(DispatchError, match="cannot book it"):
            row(label=label, source=other)


def test_the_human_labels_are_the_signatures_the_revert_and_the_signed_gate_answers():
    assert {l for l, s in SOURCES.items() if s == "human"} == {"authorised", "adopted", "reverted_14d",
                                                               "gated_allowed", "gated_denied"}


def test_a_verifier_label_needs_its_kind_and_no_other_label_takes_one():
    with pytest.raises(DispatchError, match="verifier_kind"):
        OutcomeRow("j", 0, "m", "m", "R0", "h", "t", "verifier_pass", "machine", AT)
    with pytest.raises(DispatchError, match="verifier_kind"):
        row(label="verifier_fail", verifier_kind="oracle")
    with pytest.raises(DispatchError, match="verifier label only"):
        row(label="adopted", verifier_kind="black-box")
    assert row(label="verifier_pass", verifier_kind="in-process").verifier_kind == "in-process"


@pytest.mark.parametrize("bad", [dict(job="a\nb"), dict(job=""), dict(run=-1), dict(run=True), dict(route=" R0"),
                                 dict(label="approved")])
def test_malformed_fields_are_refused(bad):
    with pytest.raises(DispatchError):
        row(**bad)


def test_an_unparsable_stamp_is_refused():
    with pytest.raises(DispatchError, match="timestamp"):
        OutcomeRow("j", 0, "m", "m", "R0", "h", "t", "adopted", "human", "yesterday")


# --- the store --------------------------------------------------------------------------------------------------

def test_append_and_read_back_in_order(store):
    assert store.rows() == []
    assert not store.directory.exists()                            # a read creates nothing
    a, b = row(run=0, label="verifier_pass"), row(run=1, label="adopted")
    assert store.append(a) and store.append(b)
    assert store.rows() == [a, b]
    assert oct(store.directory.stat().st_mode & 0o777) == "0o700"


def test_a_retry_is_idempotent_and_a_conflicting_retry_is_refused(store):
    assert store.append(row()) is True
    assert store.append(row()) is False                            # same job, run and label: no second row
    assert len(store.rows()) == 1
    with pytest.raises(DispatchError, match="another model_answered"):
        store.append(row(answered="other"))
    assert len(store.rows()) == 1


def test_a_second_label_on_a_run_must_agree_on_the_runs_facts(store):
    store.append(row(label="verifier_pass"))
    store.append(row(label="adopted"))                              # same facts: fine
    with pytest.raises(DispatchError, match="would split the run"):
        store.append(row(label="authorised", harness="codex 0.6"))
    assert len(store.rows()) == 2


def test_the_triggers_refuse_update_delete_and_replace(store):
    store.append(row())
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        for sql, msg in (("UPDATE outcomes SET route = 'R2'", "append-only"),
                         ("DELETE FROM outcomes", "append-only"),
                         ("INSERT OR REPLACE INTO outcomes (job_id, run_index, model_requested, model_answered, "
                          "route, harness, template_id, label, source, at) VALUES "
                          "('j1', 0, 'm', 'm', 'R0', 'h', 't', 'adopted', 'human', 'x')", "never replaced")):
            with pytest.raises(sqlite3.DatabaseError, match=msg):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):                 # the table itself refuses a bad label or source
            conn.execute("INSERT INTO outcomes (job_id, run_index, model_requested, model_answered, route, harness, "
                         "template_id, label, source, at) VALUES ('j2', 0, 'm', 'm', 'R0', 'h', 't', 'bogus', "
                         "'human', 'x')")
    finally:
        conn.close()
    assert len(store.rows()) == 1


def test_a_directory_others_can_reach_is_refused(tmp_path):
    d = tmp_path / "open"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(DispatchError, match="0700"):
        OutcomeStore(d).append(row())


def test_a_file_that_is_not_a_database_is_a_dispatch_error(store):
    """L4 F6: rows() and append() on a non-database file raised a bare sqlite3.DatabaseError."""
    store.directory.mkdir(mode=0o700)
    store.path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(DispatchError, match="is not an outcome store"):
        store.rows()
    with pytest.raises(DispatchError, match="is not an outcome store"):
        store.append(row())


def test_the_append_only_trigger_keeps_its_own_error_type(store):
    store.append(row())
    with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE outcomes SET label = 'adopted'")


def test_a_store_of_another_format_is_refused(store):
    store.directory.mkdir(mode=0o700)
    conn = sqlite3.connect(store.path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('format', 'something-else/9')")
    conn.commit()
    conn.close()
    with pytest.raises(DispatchError, match="format"):
        store.append(row())
    with pytest.raises(DispatchError, match="format"):
        store.rows()


# --- the rollup -------------------------------------------------------------------------------------------------

def runs(n, adopted, **kw):
    """n runs of one cell, the first ``adopted`` of them adopted, the rest verifier-failed."""
    out = []
    for i in range(n):
        out.append(row(job=f"{kw.get('tag', 'j')}{i}", run=0, label="adopted" if i < adopted else "verifier_fail",
                       **{k: v for k, v in kw.items() if k != "tag"}))
    return out


def test_the_rollup_is_hierarchical_and_counts_runs_not_labels():
    rows = runs(48, 41)
    rows += [row(job="j0", run=0, label="verifier_pass")]           # a second label on a counted run: still one run
    cells = rollup(rows, min_n=10)
    key = (("glm-5.2", "answered"), "R0", "codex 0.5", "fix-lint")
    assert set(cells) == {key[:1], key[:2], key[:3], key}
    cell = cells[key]
    assert (cell.n, cell.counts["adopted"], cell.rate("adopted")) == (48, 41, Fraction(41, 48))
    assert cell.rate("authorised") == 0                              # a label nobody carried: 0/N, not missing
    assert cells[key[:1]].n == 48


def test_a_cell_below_min_n_reports_its_n_and_no_rate():
    cells = rollup(runs(5, 4), min_n=10)
    key = (("glm-5.2", "answered"), "R0", "codex 0.5", "fix-lint")
    assert cells[key].n == 5 and cells[key].rate("adopted") is None and not cells[key].reporting
    assert "N=5, below min_n=10" in cell_line(cells[key])
    assert finest_reporting(cells, key) is None


def test_a_thin_cell_falls_back_to_the_coarser_line():
    rows = runs(30, 20, tag="a") + runs(3, 3, tag="b", template="rename")
    cells = rollup(rows, min_n=10)
    thin = (("glm-5.2", "answered"), "R0", "codex 0.5", "rename")
    assert cells[thin].n == 3
    fall = finest_reporting(cells, thin)
    assert fall.key == thin[:3] and fall.n == 33 and fall.rate("adopted") == Fraction(23, 33)


def test_a_model_the_gateway_never_saw_answer_is_kept_apart_from_an_answered_one():
    rows = (runs(12, 6, tag="a") + runs(12, 12, tag="b", answered="unknown"))
    cells = rollup(rows, min_n=10)
    answered, asked = (("glm-5.2", "answered"),), (("glm-5.2", "requested"),)
    assert cells[answered].n == 12 and cells[asked].n == 12
    assert cells[answered].rate("adopted") == Fraction(1, 2) and cells[asked].rate("adopted") == 1
    assert "requested; answering model unknown" in cell_line(cells[asked])


def test_route_harness_and_template_split_cells():
    rows = (runs(10, 5, tag="a") + runs(10, 9, tag="b", route="R1") + runs(10, 1, tag="c", harness="claude 2.1"))
    cells = rollup(rows, min_n=10)
    m = ("glm-5.2", "answered")
    assert cells[(m,)].n == 30
    assert cells[(m, "R0")].n == 20 and cells[(m, "R1")].n == 10
    assert cells[(m, "R0", "claude 2.1")].rate("adopted") == Fraction(1, 10)


def test_cell_line_formats_the_panel_text_and_the_survivor_caveat():
    cells = rollup(runs(48, 41), min_n=10)
    m = ("glm-5.2", "answered")
    assert cell_line(cells[(m, "R0", "codex 0.5", "fix-lint")]) == (
        "glm-5.2, R0, codex 0.5 `fix-lint`: 41/48 adopted (N=48)")
    assert cell_line(cells[(m,)]) == "glm-5.2: 41/48 adopted (N=48)"
    assert cell_line(cells[(m, "R0")], "adopted") == "glm-5.2, R0: 41/48 adopted (N=48)"
    rev = cell_line(cells[(m,)], "reverted_14d")
    assert rev.startswith("glm-5.2: 0/48 reverted 14d (N=48)") and rev.endswith(REVERTED_CAVEAT)
    assert REVERTED_CAVEAT not in cell_line(cells[(m,)], "adopted")
    with pytest.raises(DispatchError):
        cell_line(cells[(m,)], "nonsense")


def test_min_n_must_be_a_positive_integer():
    for bad in (0, -1, 2.5, True):
        with pytest.raises(DispatchError):
            rollup([], min_n=bad)
    assert rollup([], min_n=1) == {}


def test_a_rollup_over_a_real_store(store):
    for r in runs(4, 3):
        store.append(r)
    cells = rollup(store.rows(), min_n=4)
    assert cells[(("glm-5.2", "answered"),)].rate("adopted") == Fraction(3, 4)


def test_a_run_index_past_sqlites_integer_is_refused_at_construction(store):
    # codex: 2**63 escaped as a raw OverflowError from the bind, not a refusal
    with pytest.raises(DispatchError, match="run_index"):
        row(run=2 ** 63)
    top = row(run=2 ** 63 - 1)
    assert store.append(top) and store.rows() == [top]
