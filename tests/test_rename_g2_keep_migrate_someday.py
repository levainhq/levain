"""G2: keep-migrate plants `someday` for every new plan (the guarded store refuses a NEW `seed`, H8), and reads an OLD
plan's stored destination `seed` as `someday` when it applies (a plan file is history: it is never rewritten)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain import keep_migrate as km
from levain.cli import main
from tests.test_keep_migrate import Env
from tests.test_rename_h10_refusals import FIXTURES, old_install  # noqa: F401  (fixture)


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    e.seed_legacy()
    return e


def _dest_dispositions(plan_path: Path) -> set[str]:
    doc = json.loads(plan_path.read_text())
    return {i["destination"]["disposition"] for i in doc["items"] if i["destination"]["kind"] == km.DEST_INTENTION}


def test_a_new_plan_names_someday_never_seed(env):
    out = km.plan(env.intentions, migrations_dir=env.mig, episodic_db=env.db)
    assert _dest_dispositions(out) == {"someday"}
    assert json.loads(out.read_text())["options"]["handoff_to"] == "someday"
    assert "seed" not in {i["destination"].get("disposition") for i in json.loads(out.read_text())["items"]}


def test_handoff_to_agenda_still_plans_dated_agendas(env):
    out = km.plan(env.intentions, migrations_dir=env.mig, episodic_db=env.db, handoff_to="agenda", agenda_date="2030-02-02")
    assert _dest_dispositions(out) == {"someday", "agenda"}


def test_the_retired_handoff_to_seed_is_refused_with_a_pointer(env):
    with pytest.raises(km.KeepMigrateError, match="someday"):
        km.plan(env.intentions, migrations_dir=env.mig, episodic_db=env.db, handoff_to="seed")


def test_an_override_naming_seed_is_refused_with_a_pointer(env):
    handoff = next(r for r in env.plain().list_open() if r.get("disposition") == "handoff")
    with pytest.raises(km.KeepMigrateError, match="someday"):
        km.plan(env.intentions, migrations_dir=env.mig, episodic_db=env.db,
                overrides={handoff["id"]: {"kind": km.DEST_INTENTION, "disposition": "seed", "next": None}})


def test_the_cli_refuses_handoff_to_seed_with_a_pointer(tmp_path, capsys):
    lv = tmp_path / ".levain"
    lv.mkdir()
    with pytest.raises(SystemExit) as ei:
        main(["intentions", "keep-migrate", "--path", str(tmp_path), "--handoff-to", "seed"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "someday" in err and "seed" in err


def test_an_old_plans_stored_seed_destination_applies_as_someday_and_the_plan_stays_history(old_install, capsys):
    root, plan = old_install
    stored = json.loads(plan.read_text())
    assert "seed" in {i["destination"].get("disposition") for i in stored["items"]}      # the fixture is an old plan
    assert main(["intentions", "keep-migrate", "--path", str(root), "--apply", str(plan)]) == 0
    capsys.readouterr()
    from anneal_memory.spores import IntentionStore

    rows = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").list_open()
    assert sorted(r["disposition"] for r in rows) == ["someday", "someday"]
    after = json.loads(plan.read_text())
    assert {i["destination"].get("disposition") for i in after["items"]} == {i["destination"].get("disposition") for i in stored["items"]}
    assert main(["intentions", "keep-migrate", "--path", str(root), "--revert", str(plan)]) == 0
