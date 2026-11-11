"""`levain intentions keep-migrate` (parse + dispatch) and the doctor's keep row."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain import doctor, keep_migrate as km
from levain.cli import build_parser, main


@pytest.fixture()
def install(tmp_path: Path) -> Path:
    from anneal_memory import Store
    from anneal_memory.spores import IntentionStore

    lv = tmp_path / ".levain"
    lv.mkdir()
    with Store(str(lv / "memory.db")):
        pass
    s = IntentionStore(lv / "memory.spores.json", id_prefix="intention")
    s.add(type="thought", text="gotcha: never run `foo --all` on the prod box", disposition="note")
    s.add(type="task", text="pick up the Liz thread", disposition="handoff")
    s.add(type="thought", text="a live seed", disposition="seed")
    return tmp_path


def _open_texts(install: Path) -> list[str]:
    from anneal_memory.spores import IntentionStore

    return sorted(r["text"] for r in IntentionStore(install / ".levain" / "memory.spores.json", id_prefix="intention").list_open())


def _plans(install: Path) -> list[Path]:
    return sorted((install / ".levain" / "migrations").glob("keep-*.json"))


def test_parse_shapes():
    p = build_parser()
    a = p.parse_args(["intentions", "keep-migrate"])
    assert a.apply is None and a.revert is None and a.handoff_to is None
    a = p.parse_args(["intentions", "keep-migrate", "--handoff-to", "agenda", "--agenda-date", "2026-12-01"])
    assert (a.handoff_to, a.agenda_date) == ("agenda", "2026-12-01")
    assert p.parse_args(["intentions", "keep-migrate", "--apply", "x.json"]).apply == Path("x.json")
    assert p.parse_args(["intentions", "keep-migrate", "--revert", "x.json"]).revert == Path("x.json")


def test_apply_with_revert_is_refused_by_the_parser(capsys):
    with pytest.raises(SystemExit) as e:
        main(["intentions", "keep-migrate", "--apply", "a.json", "--revert", "b.json"])
    assert e.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_bad_handoff_to_is_refused_by_the_parser():
    with pytest.raises(SystemExit) as e:
        main(["intentions", "keep-migrate", "--handoff-to", "bogus"])
    assert e.value.code == 2


def test_intentions_alone_needs_a_verb():
    with pytest.raises(SystemExit) as e:
        main(["intentions"])
    assert e.value.code == 2


def test_dry_run_prints_a_plan_and_changes_nothing(install: Path, capsys):
    before = _open_texts(install)
    intentions_bytes = (install / ".levain" / "memory.spores.json").read_bytes()
    assert main(["intentions", "keep-migrate", "--path", str(install)]) == 0
    out = capsys.readouterr().out
    assert "Nothing was changed" in out and "2 items: 1 notes, 1 handoffs" in out
    assert len(_plans(install)) == 1
    assert (install / ".levain" / "memory.spores.json").read_bytes() == intentions_bytes
    assert _open_texts(install) == before


def test_apply_then_revert_round_trips(install: Path, capsys):
    before = _open_texts(install)
    assert main(["intentions", "keep-migrate", "--path", str(install)]) == 0
    capsys.readouterr()
    plan = _plans(install)[0]
    assert main(["intentions", "keep-migrate", "--path", str(install), "--apply", str(plan)]) == 0
    assert "applied: 2 items" in capsys.readouterr().out
    assert not any(r["disposition"] in ("note", "handoff") for r in _open_rows(install))
    assert main(["intentions", "keep-migrate", "--path", str(install), "--revert", str(plan)]) == 0
    assert "reverted: 2 items" in capsys.readouterr().out
    assert [r["disposition"] for r in _open_rows(install) if r["disposition"] in ("note", "handoff")].count("note") == 1
    assert "pick up the Liz thread" in _open_texts(install)


def _open_rows(install: Path) -> list[dict]:
    from anneal_memory.spores import IntentionStore

    return list(IntentionStore(install / ".levain" / "memory.spores.json", id_prefix="intention").list_open())


def test_apply_and_revert_refuse_dry_run_options(install: Path, capsys):
    for mode in ("--apply", "--revert"):
        assert main(["intentions", "keep-migrate", "--path", str(install), mode, "p.json",
                     "--handoff-to", "someday"]) == 1
        assert "cannot be combined" in capsys.readouterr().err


def test_agenda_without_a_date_is_a_clean_error(install: Path, capsys):
    assert main(["intentions", "keep-migrate", "--path", str(install), "--handoff-to", "agenda"]) == 1
    assert "agenda_date" in capsys.readouterr().err
    assert not _plans(install)


def test_apply_of_a_missing_plan_is_a_clean_error(install: Path, capsys):
    assert main(["intentions", "keep-migrate", "--path", str(install), "--apply", str(install / "nope.json")]) == 1
    assert "cannot read plan" in capsys.readouterr().err


def test_the_verb_is_the_operators_not_the_desks():
    from levain.desk import door

    leaves = door.declared_leaves(build_parser())
    assert leaves[("intentions", "keep-migrate")] is False


def test_doctor_row_says_the_exact_line(install: Path, capsys):
    rows = doctor._check_keep_migration(install)
    assert len(rows) == 1 and rows[0].warn and rows[0].ok
    doctor._emit(rows[0])
    assert "keep: 1 notes / 1 handoffs await migration" in capsys.readouterr().out


def test_doctor_row_is_silent_when_nothing_waits(install: Path, capsys):
    plan = km.plan(install / ".levain" / "memory.spores.json", migrations_dir=install / "m",
                   episodic_db=install / ".levain" / "memory.db")
    km.apply(plan, episodic_db=install / ".levain" / "memory.db")
    assert doctor._check_keep_migration(install) == []


def test_doctor_row_is_silent_without_a_intention_store(tmp_path: Path):
    assert doctor._check_keep_migration(tmp_path) == []


def test_doctor_row_reports_an_unreadable_store_as_a_warning(tmp_path: Path):
    (tmp_path / ".levain").mkdir()
    (tmp_path / ".levain" / "memory.spores.json").write_text("{not json")
    rows = doctor._check_keep_migration(tmp_path)
    assert len(rows) == 1 and rows[0].ok and rows[0].warn and "could not be read" in rows[0].detail


def _clone_install(install: Path, name: str) -> Path:
    from anneal_memory import Store
    from anneal_memory.spores import IntentionStore

    other = install.parent / name
    lv = other / ".levain"
    lv.mkdir(parents=True)
    with Store(str(lv / "memory.db")):
        pass
    IntentionStore(lv / "memory.spores.json", id_prefix="intention").add(type="thought", text="gotcha: other box note", disposition="note")
    return other


def test_apply_and_revert_refuse_a_plan_made_for_another_install(install: Path, capsys):
    assert main(["intentions", "keep-migrate", "--path", str(install)]) == 0
    plan = _plans(install)[0]
    other = _clone_install(install, "install_b")
    a_intentions = (install / ".levain" / "memory.spores.json").read_bytes()
    b_intentions = (other / ".levain" / "memory.spores.json").read_bytes()
    capsys.readouterr()
    for verb in ("--apply", "--revert"):
        assert main(["intentions", "keep-migrate", verb, str(plan), "--path", str(other)]) == 1
        err = capsys.readouterr().err
        assert "another install" in err and str(install) in err and str(other) in err
    assert (install / ".levain" / "memory.spores.json").read_bytes() == a_intentions
    assert (other / ".levain" / "memory.spores.json").read_bytes() == b_intentions
    # the matching install still applies
    assert main(["intentions", "keep-migrate", "--apply", str(plan), "--path", str(install)]) == 0
