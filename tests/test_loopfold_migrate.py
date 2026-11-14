"""The loop fold's migration (board item levain#4; Phill np-10262267 (a), Q2 np-ee498a62 (a)): `levain update` rewrites every OPEN
row whose disposition is absent, empty or the literal `loop` to `someday`, through ``levain.intention_migrate``.

Every store below is built the OLD way (anneal's plain ``IntentionStore``, which writes no disposition for a loop and accepts the
literal), the way a release before the fold left it. The real update path (``run_update``) is exercised at the end."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.dashboard import AnnealPaths
from levain.intention_migrate import BACKUP_SUBDIR, LEDGER_PREFIX

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
PAST, FUTURE = "2020-01-01", "2031-01-01"        # far from NOW either side: no timezone can move them across it


def _migrate(paths, **kw):
    from levain.intention_migrate import migrate_loop_to_someday   # imported here so a missing module fails each test, not the file

    return migrate_loop_to_someday(paths, now=NOW, **kw)


def _store(paths):
    from anneal_memory.spores import IntentionStore

    return IntentionStore(paths.intentions_json, id_prefix="intention")        # the plain store: no write guard


def _hand_set(paths: AnnealPaths, rid: str, **fields) -> None:
    """Write fields into one open row as a hand-edited store would hold them (anneal clears a "" itself, and drops a None)."""
    doc = json.loads(paths.intentions_json.read_text(encoding="utf-8"))
    row = next(r for r in doc["intentions"] if r["id"] == rid)
    row.update(fields)
    paths.intentions_json.write_text(json.dumps(doc), encoding="utf-8")


def _old_store(tmp_path: Path) -> tuple[AnnealPaths, dict[str, str]]:
    """What a release before the fold left: every kind of loop row, a someday, an agenda, a retired seed and note, an unknown
    value, a resolved loop, and a resolved seed."""
    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    s = _store(paths)
    ids = {
        "plain": s.add(type="task", text="a plain loop")["id"],
        "parked": s.add(type="task", text="a parked loop", tier="parked")["id"],
        "past": s.add(type="task", text="a dormant loop", next=PAST, tier="hot")["id"],
        "future": s.add(type="task", text="a loop that wakes later", next=FUTURE)["id"],
        "timed_past": s.add(type="task", text="a timed loop", next="2020-01-01T09:30")["id"],
        "literal": s.add(type="task", text="the literal loop", disposition="loop")["id"],
        "empty": s.add(type="task", text="a stored empty string")["id"],
        "null": s.add(type="task", text="a stored null")["id"],
        "someday": s.add(type="thought", text="already a someday", disposition="someday")["id"],
        "agenda": s.add(type="task", text="an agenda", disposition="agenda", target="band", next=FUTURE)["id"],
        "seed": s.add(type="thought", text="a retired seed", disposition="seed")["id"],
        "note": s.add(type="thought", text="a legacy note", disposition="note")["id"],
        "unknown": s.add(type="task", text="a typo", disposition="GARBAGE")["id"],
        "resolved": s.add(type="task", text="a resolved loop")["id"],
        "resolved_literal": s.add(type="task", text="a resolved literal loop", disposition="loop")["id"],
    }
    s.descend(ids["resolved"], kind="done")
    s.descend(ids["resolved_literal"], kind="done")
    _hand_set(paths, ids["empty"], disposition="")
    _hand_set(paths, ids["null"], disposition=None)
    return paths, ids


def _doc(paths: AnnealPaths) -> dict:
    return json.loads(paths.intentions_json.read_text(encoding="utf-8"))


def _open(paths: AnnealPaths, rid: str) -> dict:
    return next(r for r in _doc(paths)["intentions"] if r["id"] == rid)


def _ledgers(tmp_path: Path) -> list[Path]:
    d = tmp_path / ".levain" / Path(*BACKUP_SUBDIR)
    return sorted(d.glob(f"{LEDGER_PREFIX}*.json")) if d.is_dir() else []


def _backups(tmp_path: Path) -> list[Path]:
    d = tmp_path / ".levain" / Path(*BACKUP_SUBDIR)
    return sorted(p for p in d.iterdir() if not p.name.startswith(LEDGER_PREFIX)) if d.is_dir() else []


LOOP_ROWS = ("plain", "parked", "past", "future", "timed_past", "literal", "empty", "null")


# --- the rewrite ---------------------------------------------------------------------------------------------------

def test_absent_empty_null_and_literal_loop_rows_become_someday_and_nothing_else_moves(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    before = {rid: dict(_open(paths, rid)) for rid in ids.values() if rid in {r["id"] for r in _doc(paths)["intentions"]}}
    resolved_before = _doc(paths)["resolved"]

    report = _migrate(paths)

    assert sorted(report.migrated) == sorted(ids[k] for k in LOOP_ROWS) and report.count == len(LOOP_ROWS)
    assert report.skipped == [] and report.refused is None and not report.incomplete and not report.failed
    for key in LOOP_ROWS:
        after = _open(paths, ids[key])
        assert after["disposition"] == "someday", key
        # ONLY the disposition changed: the tier (parked is kept), the next (a past one stays: Q2), the text, the type, the keys
        assert {k: v for k, v in after.items() if k != "disposition"} == \
            {k: v for k, v in before[ids[key]].items() if k != "disposition"}, key
    assert _open(paths, ids["parked"])["tier"] == "parked"
    assert _open(paths, ids["past"])["next"] == PAST and _open(paths, ids["timed_past"])["next"] == "2020-01-01T09:30"
    assert _open(paths, ids["future"])["next"] == FUTURE
    # every other open row is byte-for-byte what it was
    for key in ("someday", "agenda", "seed", "note", "unknown"):
        assert _open(paths, ids[key]) == before[ids[key]], key
    assert _doc(paths)["resolved"] == resolved_before                  # a resolved loop keeps its history, key-absent or literal
    assert report.backup is not None and report.backup.parent == tmp_path / ".levain" / "backups" / "intentions"


def test_a_row_whose_disposition_this_release_does_not_know_is_untouched_and_reported(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    before = _open(paths, ids["unknown"])

    report = _migrate(paths)

    assert _open(paths, ids["unknown"]) == before and _open(paths, ids["unknown"])["disposition"] == "GARBAGE"
    assert report.unknown == [(ids["unknown"], "GARBAGE")]
    text = "\n".join(report.lines())
    assert ids["unknown"] in text and "'GARBAGE'" in text and "left as they are" in text and "reads as that name" in text


@pytest.mark.parametrize("value", ["Loop", " loop", "loops", 7, ["loop"]])
def test_only_the_exact_names_are_rewritten_a_near_miss_is_unknown(tmp_path: Path, value) -> None:
    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    rid = _store(paths).add(type="task", text="x")["id"]
    _hand_set(paths, rid, disposition=value)
    before = paths.intentions_json.read_bytes()
    report = _migrate(paths)
    assert report.count == 0 and report.backup is None and report.unknown == [(rid, value)]
    assert paths.intentions_json.read_bytes() == before


def test_the_past_dated_rows_are_named_in_the_report_and_keep_their_next(tmp_path: Path) -> None:
    """Q2 (np-ee498a62 (a)): a past `next` stays, so the row shows as Overdue. The report names it, the future one it does not."""
    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    named = {rid for rid, _nxt, _note in report.past_dated}
    assert named == {ids["past"], ids["timed_past"]}
    text = "\n".join(report.lines())
    assert f"{ids['past']}: its next {PAST} has passed and it is kept" in text and "shows as Overdue on the List" in text
    assert ids["future"] not in "".join(ln for ln in report.lines() if "has passed" in ln)


def test_q2_is_one_function_and_option_a_adds_nothing_to_the_write() -> None:
    """The branch the lane names: no extra ``update`` fields (the row's `next` is not touched), and the words for a past-dated row.
    Flipping to option (b) is a change to this function only."""
    from levain.intention_migrate import q2_past_next_policy

    now = datetime(2026, 11, 13, 12, 0)
    assert q2_past_next_policy({"next": PAST}, now) == ({}, "shows as Overdue on the List")
    assert q2_past_next_policy({"next": "2026-11-12"}, now)[1] is not None                # yesterday has passed
    assert q2_past_next_policy({"next": "2026-11-13"}, now) == ({}, None)                 # today has not (the List: before today)
    assert q2_past_next_policy({"next": "2026-11-13T11:59"}, now)[1] is not None          # a timed one, once its minute is reached
    assert q2_past_next_policy({"next": "2026-11-13T12:00"}, now)[1] is not None
    assert q2_past_next_policy({"next": "2026-11-13T12:01"}, now) == ({}, None)
    for nxt in (FUTURE, None, "", "soon", "2026-13-45", 5):
        assert q2_past_next_policy({"next": nxt}, now) == ({}, None), nxt
    assert q2_past_next_policy({}, now) == ({}, None)


def test_flipping_q2_to_option_b_is_the_one_function(tmp_path: Path, monkeypatch) -> None:
    """The proof the flip is one function: replace it with option (b)'s body (clear the past `next`, keep its words in a note)
    and the rewrite does that, with nothing else changed."""
    import levain.intention_migrate as mig

    def option_b(row, now):
        if not mig._has_passed(row.get("next"), now):
            return {}, None
        return {"next": None, "add_note": f"was due {row['next']}"}, "moves its date into a note and shows under Someday"

    monkeypatch.setattr(mig, "q2_past_next_policy", option_b)
    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    assert report.migrated and not report.skipped
    assert _open(paths, ids["past"])["next"] is None and _open(paths, ids["past"])["disposition"] == "someday"
    assert _open(paths, ids["future"])["next"] == FUTURE                                  # a future one is not past-dated
    assert "moves its date into a note" in "\n".join(report.lines())


# --- idempotence, the backup, the ledger ---------------------------------------------------------------------------

def test_a_second_run_writes_nothing_and_takes_no_backup(tmp_path: Path) -> None:
    paths, _ids = _old_store(tmp_path)
    assert _migrate(paths).count == len(LOOP_ROWS)
    after = paths.intentions_json.read_bytes()
    files = sorted(p.name for p in (tmp_path / ".levain" / "backups" / "intentions").iterdir())

    second = _migrate(paths)

    assert second.count == 0 and second.backup is None and second.refused is None and not second.failed
    assert second.ledger is None and second.migrated == []
    assert paths.intentions_json.read_bytes() == after
    assert sorted(p.name for p in (tmp_path / ".levain" / "backups" / "intentions").iterdir()) == files    # no copy, no ledger
    assert not any("rewritten" in ln for ln in second.lines())          # only the unknown row is named again


def test_the_backup_is_the_pre_migration_file_byte_for_byte_and_is_taken_once_per_run(tmp_path: Path) -> None:
    paths, _ids = _old_store(tmp_path)
    before = paths.intentions_json.read_bytes()
    report = _migrate(paths)
    assert report.backup is not None and report.backup.read_bytes() == before
    assert report.backup.name.startswith("memory.spores.json.20261113T120000Z")
    assert len(_backups(tmp_path)) == 1


def test_the_ledger_lists_exactly_the_migrated_ids_and_what_each_held(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    (ledger,) = _ledgers(tmp_path)
    assert report.ledger == ledger and ledger.name == "loopfold-20261113T120000Z.json"
    doc = json.loads(ledger.read_text(encoding="utf-8"))
    assert doc["migrated"] == report.migrated and sorted(doc["migrated"]) == sorted(ids[k] for k in LOOP_ROWS)
    assert doc["backup"] == str(report.backup) and doc["at"].startswith("2026-11-13T12:00:00")
    assert doc["prior"][ids["plain"]] is None and doc["prior"][ids["literal"]] == "loop" and doc["prior"][ids["empty"]] == ""
    assert str(ledger) in "\n".join(report.lines())


def test_a_store_with_no_open_loop_is_left_untouched_and_unbacked(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    _store(paths).add(type="thought", text="a someday", disposition="someday")
    before = paths.intentions_json.read_bytes()
    report = _migrate(paths)
    assert report.count == 0 and report.backup is None and report.lines() == []
    assert paths.intentions_json.read_bytes() == before and _backups(tmp_path) == []       # no copy; only the completed ledger is written


def test_no_store_at_all_is_not_an_error(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    report = _migrate(AnnealPaths.from_db(tmp_path / ".levain" / "memory.db"))
    assert report.count == 0 and report.refused is None and report.backup is None and report.lines() == []


def test_an_unwritable_backup_refuses_and_the_store_is_untouched(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    (tmp_path / ".levain" / "backups").write_text("a file where the backup directory must go")
    before = paths.intentions_json.read_bytes()
    report = _migrate(paths)
    assert report.count == 0 and report.backup is None and report.failed
    assert report.refused and "backup" in report.refused and "nothing was rewritten" in report.refused
    assert paths.intentions_json.read_bytes() == before and "disposition" not in _open(paths, ids["plain"])


def test_a_corrupt_store_refuses_and_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    paths.intentions_json.write_text("{not json")
    report = _migrate(paths)
    assert report.refused and report.count == 0 and report.failed
    assert paths.intentions_json.read_text() == "{not json" and not (tmp_path / ".levain" / "backups").exists()


def test_an_anneal_below_the_pin_refuses_before_any_copy(tmp_path: Path, monkeypatch) -> None:
    import levain.intentions as lint

    paths, _ids = _old_store(tmp_path)
    before = paths.intentions_json.read_bytes()

    def refuse():
        raise RuntimeError("levain needs anneal-memory>=2.0.0,<2.1")

    monkeypatch.setattr(lint, "_require_schema2_anneal", refuse)
    report = _migrate(paths)
    assert report.refused and "anneal-memory>=2.0.0,<2.1" in report.refused and report.backup is None
    assert paths.intentions_json.read_bytes() == before and not (tmp_path / ".levain" / "backups").exists()


# --- a row that moved, a store fault -------------------------------------------------------------------------------

def test_a_row_that_moved_mid_run_is_skipped_and_named_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    """The compare-and-set: the rows are read first, then each is updated under the store lock expecting the value read."""
    import levain.intention_migrate as mig

    paths, ids = _old_store(tmp_path)
    plain = _store(paths)
    real = mig._classify_open_loops
    calls = {"n": 0}

    def stale_read(path, **kw):
        found = real(path, **kw)
        calls["n"] += 1
        if calls["n"] == 1:                                           # between the read and the writes
            plain.update(ids["plain"], disposition="agenda")          # a writer re-routes one
            plain.descend(ids["parked"], kind="done")                 # another resolves one
            plain.update(ids["literal"], disposition="someday")       # a third already made one a someday
        return found

    monkeypatch.setattr(mig, "_classify_open_loops", stale_read)
    report = _migrate(paths)

    assert sorted(r for r, _why in report.skipped) == sorted([ids["plain"], ids["parked"], ids["literal"]])
    assert _open(paths, ids["plain"])["disposition"] == "agenda"                 # its writer's value stands
    assert ids["parked"] not in {r["id"] for r in _doc(paths)["intentions"]}      # resolved, left resolved
    assert sorted(report.migrated) == sorted(ids[k] for k in LOOP_ROWS if k not in ("plain", "parked", "literal"))
    assert any(ids["plain"] in ln and "left as it is" in ln for ln in report.lines())
    assert not report.incomplete and not report.failed        # a row that moved is its writer's: nothing is left behind
    assert sorted(json.loads(report.ledger.read_text())["migrated"]) == sorted(report.migrated)     # the ledger holds only what moved


def _fail_updates_of(monkeypatch, fail_ids, exc: BaseException) -> None:
    import levain.intentions as li

    real = li.guarded_intention_store

    def build(path):
        store = real(path)
        orig = store.update

        def update(rid, **kw):
            if rid in fail_ids:
                raise exc
            return orig(rid, **kw)

        store.update = update
        return store

    monkeypatch.setattr(li, "guarded_intention_store", build)


@pytest.mark.parametrize("exc", [OSError(5, "I/O error"), ValueError("a field the store rejects")])
def test_a_row_the_store_could_not_update_is_reported_and_leaves_the_run_incomplete(tmp_path: Path, monkeypatch, exc) -> None:
    paths, ids = _old_store(tmp_path)
    _fail_updates_of(monkeypatch, {ids["plain"]}, exc)
    report = _migrate(paths)
    assert [r for r, _why in report.skipped] == [ids["plain"]] and report.unmigrated == [ids["plain"]]
    assert report.incomplete and report.failed and report.refused is None
    assert "disposition" not in _open(paths, ids["plain"])                      # still there, for the next `levain update`
    assert ids["plain"] not in report.migrated and ids["plain"] not in json.loads(report.ledger.read_text())["migrated"]


# --- the revert from the ledger ------------------------------------------------------------------------------------

def test_the_revert_from_the_ledger_restores_key_absent_rows(tmp_path: Path) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids = _old_store(tmp_path)
    original = {k: dict(_open(paths, ids[k])) for k in LOOP_ROWS}
    report = _migrate(paths)
    assert all(_open(paths, ids[k])["disposition"] == "someday" for k in LOOP_ROWS)

    back = revert_loop_migration(paths, report.ledger)

    assert back.refused is None and back.skipped == [] and sorted(back.restored) == sorted(report.migrated)
    for key in LOOP_ROWS:
        row = _open(paths, ids[key])
        assert "disposition" not in row, key                                       # key-absent again, and it reads as it did
        if original[key].get("disposition") in (None, ""):     # the rows that held no disposition are exactly as they were
            assert {k: v for k, v in row.items()} == {k: v for k, v in original[key].items() if k != "disposition"}, key
    assert _open(paths, ids["someday"])["disposition"] == "someday"                # a row the migration never touched is not reverted
    assert _open(paths, ids["unknown"])["disposition"] == "GARBAGE"


def test_the_revert_leaves_a_row_that_moved_since_to_its_writer(tmp_path: Path) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    _store(paths).update(ids["plain"], disposition="agenda")                 # re-routed after the rewrite
    back = revert_loop_migration(paths, report.ledger)
    assert [r for r, _why in back.skipped] == [ids["plain"]] and ids["plain"] not in back.restored
    assert _open(paths, ids["plain"])["disposition"] == "agenda"
    assert "disposition" not in _open(paths, ids["parked"])


def test_the_revert_of_a_missing_ledger_refuses(tmp_path: Path) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, _ids = _old_store(tmp_path)
    back = revert_loop_migration(paths, tmp_path / "nope.json")
    assert back.refused and back.restored == []


# --- the dry run ---------------------------------------------------------------------------------------------------

def test_the_plan_names_every_past_dated_row_and_writes_nothing(tmp_path: Path) -> None:
    from levain.intention_migrate import plan_loop_migration

    paths, ids = _old_store(tmp_path)
    before = paths.intentions_json.read_bytes()
    plan = plan_loop_migration(paths, now=NOW)
    assert paths.intentions_json.read_bytes() == before and not (tmp_path / ".levain" / "backups").exists()
    assert sorted(plan.ids) == sorted(ids[k] for k in LOOP_ROWS)
    assert {rid for rid, _n, _note in plan.past_dated} == {ids["past"], ids["timed_past"]}
    text = "\n".join(plan.rewrite_lines())
    assert f"{len(LOOP_ROWS)} open loop record(s) would be rewritten to someday" in text
    assert f"{ids['past']} (next {PAST}: shows as Overdue on the List)" in text and ids["future"] + " (next" not in text
    assert ids["unknown"] in plan.unknown_line()


@pytest.mark.parametrize("how", ["malformed_json", "not_a_json_object", "a_directory"])
def test_the_plan_of_a_store_it_cannot_read_raises_rather_than_say_nothing_to_do(tmp_path: Path, how: str) -> None:
    from levain.intention_migrate import plan_loop_migration

    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    if how == "malformed_json":
        paths.intentions_json.write_text("{not json")
    elif how == "not_a_json_object":
        paths.intentions_json.write_text("[1, 2]")
    else:
        paths.intentions_json.mkdir()
    with pytest.raises((OSError, ValueError)):
        plan_loop_migration(paths)


# --- through the real `levain update` ------------------------------------------------------------------------------

from tests.test_subfolder_hooks import cc_local_layout  # noqa: E402,F401  (fixture)


@pytest.fixture
def install(tmp_path, capsys, monkeypatch) -> Path:
    from levain.install import run_init

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    from tests.test_init_answers import _filled

    af = tmp_path / "answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    target = tmp_path / "entity"
    assert run_init(target, "claude-code", force=False, answers_file=af) == 0
    capsys.readouterr()
    return target


def _iplant(install: Path) -> dict[str, str]:
    paths = AnnealPaths.from_db(install / ".levain" / "memory.db")
    s = _store(paths)
    return {"plain": s.add(type="task", text="a plain loop")["id"],
            "past": s.add(type="task", text="a dormant loop", next=PAST)["id"],
            "parked": s.add(type="task", text="a parked loop", tier="parked")["id"],
            "unknown": s.add(type="task", text="a typo", disposition="GARBAGE")["id"]}


def _ipaths(install: Path) -> AnnealPaths:
    return AnnealPaths.from_db(install / ".levain" / "memory.db")


@pytest.mark.usefixtures("cc_local_layout")
def test_update_rewrites_open_loops_with_a_backup_a_ledger_and_the_report(install) -> None:
    from levain.update import run_update

    ids = _iplant(install)
    before = _ipaths(install).intentions_json.read_bytes()
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    doc = json.loads(_ipaths(install).intentions_json.read_text(encoding="utf-8"))
    got = {r["id"]: r for r in doc["intentions"]}
    assert [got[ids[k]]["disposition"] for k in ("plain", "past", "parked")] == ["someday"] * 3
    assert got[ids["unknown"]]["disposition"] == "GARBAGE" and got[ids["past"]]["next"] == PAST and got[ids["parked"]]["tier"] == "parked"
    d = install / ".levain" / Path(*BACKUP_SUBDIR)
    backups = [p for p in d.iterdir() if not p.name.startswith(LEDGER_PREFIX)]
    assert len(backups) == 1 and backups[0].read_bytes() == before and len(list(d.glob(LEDGER_PREFIX + "*.json"))) == 1
    text = "\n".join(lines)
    assert "3 open loop record(s) rewritten to someday" in text and str(backups[0]) in text
    assert f"{ids['past']}: its next {PAST} has passed" in text and "GARBAGE" in text


@pytest.mark.usefixtures("cc_local_layout")
def test_a_second_update_writes_nothing_and_takes_no_second_copy(install) -> None:
    from levain.update import run_update

    _iplant(install)
    assert run_update(install, no_pip=True, emit=lambda s: None) == 0
    store_bytes = _ipaths(install).intentions_json.read_bytes()
    d = install / ".levain" / Path(*BACKUP_SUBDIR)
    names = sorted(p.name for p in d.iterdir())
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    assert _ipaths(install).intentions_json.read_bytes() == store_bytes and sorted(p.name for p in d.iterdir()) == names
    assert not any("rewritten to someday" in ln for ln in lines)


@pytest.mark.usefixtures("cc_local_layout")
def test_the_dry_run_prints_the_would_rewrite_line_names_the_past_dated_rows_and_writes_nothing(install) -> None:
    from levain.update import run_update

    ids = _iplant(install)
    before = _ipaths(install).intentions_json.read_bytes()
    lines: list[str] = []
    rc = run_update(install, dry_run=True, no_pip=True, emit=lines.append)
    assert rc == 1                                                          # a plan with something to do is not "nothing to reconcile"
    assert _ipaths(install).intentions_json.read_bytes() == before
    assert not (install / ".levain" / "backups").exists()
    text = "\n".join(lines)
    assert "3 open loop record(s) would be rewritten to someday" in text
    assert f"{ids['past']} (next {PAST}: shows as Overdue on the List)" in text
    assert "nothing to reconcile" not in text and "GARBAGE" in text


@pytest.mark.usefixtures("cc_local_layout")
def test_held_hooks_leave_the_loop_rewrite_for_the_next_update(install, monkeypatch) -> None:
    import levain.update as upd

    _iplant(install)
    before = _ipaths(install).intentions_json.read_bytes()
    real = upd.refresh_adapter

    def held(install_, **kw):
        out = real(install_, **kw)
        out.review.append("activation/hooks/_levain_hook.py")
        return out

    monkeypatch.setattr(upd, "refresh_adapter", held)
    lines: list[str] = []
    assert upd.run_update(install, no_pip=True, emit=lines.append) == 1
    assert _ipaths(install).intentions_json.read_bytes() == before and not (install / ".levain" / "backups").exists()
    text = "\n".join(lines)
    assert "3 open loop record(s) are left for the next `levain update`" in text and "rewritten to someday (" not in text


@pytest.mark.usefixtures("cc_local_layout")
def test_a_refused_backup_surfaces_and_the_update_exits_non_zero(install) -> None:
    from levain.update import _SEEDS_REFUSED_TAIL, run_update

    _iplant(install)
    before = _ipaths(install).intentions_json.read_bytes()
    (install / ".levain" / BACKUP_SUBDIR[0]).write_text("a file where the backups directory must be", encoding="utf-8")
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 1
    assert _ipaths(install).intentions_json.read_bytes() == before
    text = "\n".join(lines)
    assert "loop -> someday rewrite was not done" in text and "backup could not be written" in text and _SEEDS_REFUSED_TAIL in text


@pytest.mark.usefixtures("cc_local_layout")
def test_the_seed_rewrite_and_the_loop_rewrite_both_run_in_one_update(install) -> None:
    from levain.update import run_update

    paths = _ipaths(install)
    s = _store(paths)
    seed = s.add(type="thought", text="an old seed", disposition="seed")["id"]
    plain = s.add(type="task", text="a plain loop")["id"]
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    got = {r["id"]: r["disposition"] for r in json.loads(paths.intentions_json.read_text())["intentions"]}
    assert got[seed] == "someday" and got[plain] == "someday"
    text = "\n".join(lines)
    assert "1 open seed record(s) rewritten to someday" in text and "1 open loop record(s) rewritten to someday" in text
    d = install / ".levain" / Path(*BACKUP_SUBDIR)
    assert len([p for p in d.iterdir() if not p.name.startswith(LEDGER_PREFIX)]) == 2      # one copy per rewrite


# --- after the first completed run, a key-absent row is no longer a loop (L3 r1: every update re-selected the entity's own adds) ---

def _snapshot_dir(tmp_path: Path) -> list[str]:
    d = tmp_path / ".levain" / Path(*BACKUP_SUBDIR)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_a_key_absent_row_planted_after_a_completed_run_is_left_alone_and_costs_no_backup(tmp_path: Path) -> None:
    from levain.intention_migrate import plan_loop_migration

    paths, ids = _old_store(tmp_path)
    first = _migrate(paths)
    assert first.migrated and first.ledger is not None and not first.incomplete       # the first run rewrites the key-absent legacy loops
    assert json.loads(first.ledger.read_text(encoding="utf-8"))["unmigrated"] == 0
    late = _store(paths).add(type="task", text="the entity's own add after the fold")["id"]     # key-absent, as anneal writes it
    assert "disposition" not in _open(paths, late)
    names, before = _snapshot_dir(tmp_path), paths.intentions_json.read_bytes()

    plan = plan_loop_migration(paths, now=NOW)
    again = _migrate(paths)

    assert plan.ids == [] and plan.rewrite_lines() == []                              # the dry run reaches "nothing to reconcile"
    assert again.migrated == [] and again.backup is None and again.ledger is None and not again.failed
    assert _snapshot_dir(tmp_path) == names and paths.intentions_json.read_bytes() == before
    assert "disposition" not in _open(paths, late)


def test_a_literal_loop_or_an_empty_key_planted_by_an_old_client_is_still_rewritten_after_a_completed_run(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    assert _migrate(paths).ledger is not None
    old_literal = _store(paths).add(type="task", text="an old client's loop", disposition="loop")["id"]
    old_empty = _store(paths).add(type="task", text="an old client's empty")["id"]
    absent = _store(paths).add(type="task", text="the entity's add")["id"]
    _hand_set(paths, old_empty, disposition="")

    again = _migrate(paths)

    assert sorted(again.migrated) == sorted([old_literal, old_empty]) and again.backup is not None
    assert _open(paths, old_literal)["disposition"] == "someday" and _open(paths, old_empty)["disposition"] == "someday"
    assert "disposition" not in _open(paths, absent)


def test_an_incomplete_first_run_does_not_close_the_key_absent_window(tmp_path: Path, monkeypatch) -> None:
    paths, ids = _old_store(tmp_path)
    _fail_updates_of(monkeypatch, {ids["plain"]}, OSError("disk"))
    first = _migrate(paths)
    assert first.incomplete and json.loads(first.ledger.read_text(encoding="utf-8"))["unmigrated"] == 1
    monkeypatch.undo()
    second = _migrate(paths)                                                          # no completed ledger yet: the absent row is still a loop
    assert second.migrated == [ids["plain"]]


@pytest.mark.usefixtures("cc_local_layout")
def test_update_on_an_active_install_takes_no_backup_for_the_entitys_own_adds(install) -> None:
    from levain.update import run_update

    _iplant(install)
    assert run_update(install, no_pip=True, emit=lambda s: None) == 0
    _store(_ipaths(install)).add(type="task", text="the entity's own add")
    d = install / ".levain" / Path(*BACKUP_SUBDIR)
    names, before = sorted(p.name for p in d.iterdir()), _ipaths(install).intentions_json.read_bytes()
    lines: list[str] = []
    assert run_update(install, dry_run=True, no_pip=True, emit=lines.append) == 0
    assert "nothing to reconcile" in "\n".join(lines)
    assert run_update(install, no_pip=True, emit=lambda s: None) == 0
    assert sorted(p.name for p in d.iterdir()) == names and _ipaths(install).intentions_json.read_bytes() == before


# --- the revert validates its ledger and its store (L3 r1) ---

def _ledger_doc(ledger: Path) -> dict:
    return json.loads(ledger.read_text(encoding="utf-8"))


def _rewrite_ledger(ledger: Path, **changes) -> None:
    doc = _ledger_doc(ledger)
    doc.update(changes)
    ledger.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("migrated", ["intention-001", {"intention-001": 1}, [1, 2], None])
def test_the_revert_refuses_a_ledger_whose_migrated_is_not_a_list_of_ids(tmp_path: Path, migrated) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    _rewrite_ledger(report.ledger, migrated=migrated)
    before = paths.intentions_json.read_bytes()
    back = revert_loop_migration(paths, report.ledger)
    assert back.refused and back.restored == [] and back.skipped == [] and paths.intentions_json.read_bytes() == before


def test_the_revert_refuses_a_ledger_whose_prior_disagrees_and_one_that_is_not_an_object(tmp_path: Path) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    before = paths.intentions_json.read_bytes()
    _rewrite_ledger(report.ledger, prior={"intention-001": None})
    assert revert_loop_migration(paths, report.ledger).refused
    report.ledger.write_text("[1, 2]", encoding="utf-8")
    assert revert_loop_migration(paths, report.ledger).refused
    assert paths.intentions_json.read_bytes() == before


def test_the_revert_refuses_a_ledger_written_for_another_store(tmp_path: Path) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    other_root = tmp_path / "other"
    (other_root / ".levain").mkdir(parents=True)
    other = AnnealPaths.from_db(other_root / ".levain" / "memory.db")
    kept = _store(other).add(type="task", text="B's own row", disposition="someday")["id"]       # both stores hold an `intention-001`
    before = other.intentions_json.read_bytes()
    back = revert_loop_migration(other, report.ledger)
    assert back.refused and "another store" in back.refused and back.restored == []
    assert other.intentions_json.read_bytes() == before and _open(other, kept)["disposition"] == "someday"
    assert revert_loop_migration(paths, report.ledger).restored                               # its own store still reverts


def test_the_revert_of_a_corrupt_store_refuses_instead_of_raising(tmp_path: Path) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    paths.intentions_json.write_text("{not json", encoding="utf-8")
    back = revert_loop_migration(paths, report.ledger)
    assert back.refused and "cannot be read" in back.refused and back.restored == []
    assert paths.intentions_json.read_text(encoding="utf-8") == "{not json"


# --- the Q2 policy never takes the run down (L3 r1) ---

def test_a_timezone_aware_next_is_not_a_past_date_and_does_not_break_the_run(tmp_path: Path) -> None:
    from levain.intention_migrate import q2_past_next_policy

    paths, ids = _old_store(tmp_path)
    _hand_set(paths, ids["plain"], next="2026-11-13T12:00+05:00")
    assert q2_past_next_policy({"next": "2026-11-13T12:00+05:00"}, datetime(2031, 1, 1)) == ({}, None)
    report = _migrate(paths)
    assert not report.skipped and ids["plain"] in report.migrated and _open(paths, ids["plain"])["disposition"] == "someday"


def test_the_policy_reads_an_aware_clock_on_the_local_basis() -> None:
    from levain.intention_migrate import q2_past_next_policy

    aware = datetime(2031, 1, 1, tzinfo=timezone.utc)
    assert q2_past_next_policy({"next": "2020-01-01T09:30"}, aware) == ({}, "shows as Overdue on the List")
    assert q2_past_next_policy({"next": "2020-01-01"}, aware)[1] == "shows as Overdue on the List"
    assert q2_past_next_policy({"next": "2040-01-01"}, aware) == ({}, None)


def test_a_row_the_policy_raises_on_is_skipped_and_named_and_the_ledger_still_lists_the_rest(tmp_path: Path, monkeypatch) -> None:
    import levain.intention_migrate as im

    paths, ids = _old_store(tmp_path)
    real = im.q2_past_next_policy

    def policy(row, now):
        if row["id"] == ids["plain"]:
            raise TypeError("a bad row")
        return real(row, now)

    monkeypatch.setattr(im, "q2_past_next_policy", policy)
    report = _migrate(paths)
    assert [r for r, _why in report.skipped] == [ids["plain"]] and "TypeError" in report.skipped[0][1]
    assert ids["plain"] in report.unmigrated and report.incomplete
    assert sorted(report.migrated) == sorted(ids[k] for k in LOOP_ROWS if k != "plain")
    assert report.ledger is not None and sorted(_ledger_doc(report.ledger)["migrated"]) == sorted(report.migrated)
