"""`levain adopt-answers` (spore-423): a hand-edited seed becomes the record.

Reproduced through the CLI first: an operator's edit to LOCATION in seed/world.md was
silently reverted by `levain init --force --answers .levain/answers.json`, with no
backup of the seed, because the record still held the old answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.install import read_answers, run_adopt_answers, run_init


@pytest.fixture
def install(tmp_path, capsys):
    from tests.test_init_answers import _filled

    answers = _filled(capsys)
    af = tmp_path / "answers.json"
    af.write_text(json.dumps(answers), encoding="utf-8")
    inst = tmp_path / "entity"
    assert run_init(inst, "openhands", force=False, answers_file=af) == 0
    capsys.readouterr()
    return inst


def _adopt(install: Path, **kw) -> tuple[int, str]:
    lines: list[str] = []
    return run_adopt_answers(install, emit=lines.append, **kw), "\n".join(lines)


def _edit(install: Path, name: str, old: str, new: str) -> None:
    p = install / "seed" / name
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_an_unedited_install_has_nothing_to_adopt(install):
    before = (install / ".levain" / "answers.json").read_bytes()
    code, out = _adopt(install)
    assert code == 0 and "nothing to adopt" in out
    assert (install / ".levain" / "answers.json").read_bytes() == before


def test_an_edit_becomes_the_record_and_survives_a_re_onboard(install, tmp_path, capsys):
    _edit(install, "world.md", "Riverton", "Lakewood")
    _edit(install, "origin.md", "Shipping v2", "Shipping v3")
    code, out = _adopt(install)
    assert code == 0 and "LOCATION" in out and "JOB" in out
    record = read_answers(install)
    assert (record["LOCATION"], record["JOB"]) == ("Lakewood", "Shipping v3")
    # The path that used to revert the edit: re-onboard from the record.
    af = tmp_path / "record.json"
    af.write_text(json.dumps(record), encoding="utf-8")
    assert run_init(install, "openhands", force=True, answers_file=af) == 0
    assert "Lakewood" in (install / "seed" / "world.md").read_text(encoding="utf-8")
    assert "Shipping v3" in (install / "seed" / "origin.md").read_text(encoding="utf-8")


def test_a_dry_run_writes_nothing(install):
    before = (install / ".levain" / "answers.json").read_bytes()
    _edit(install, "world.md", "Riverton", "Lakewood")
    code, out = _adopt(install, dry_run=True)
    assert code == 0 and "Would adopt" in out and "Lakewood" in out
    assert (install / ".levain" / "answers.json").read_bytes() == before


def test_an_edit_outside_the_fields_is_refused_and_named(install):
    before = (install / ".levain" / "answers.json").read_bytes()
    _edit(install, "world.md", "Riverton", "Lakewood")
    _edit(install, "world.md", "## ", "## Renamed ")
    code, out = _adopt(install)
    assert code == 1 and "outside the interview's fields" in out and "Renamed" in out
    assert (install / ".levain" / "answers.json").read_bytes() == before  # all or nothing


def test_a_blanked_identity_is_refused_as_an_invalid_record(install):
    before = (install / ".levain" / "answers.json").read_bytes()
    text = (install / "seed" / "origin.md").read_text(encoding="utf-8")
    (install / "seed" / "origin.md").write_text(text.replace("Ada", ""), encoding="utf-8")
    code, out = _adopt(install)
    assert code == 1 and "not a valid record" in out
    assert (install / ".levain" / "answers.json").read_bytes() == before


def test_a_missing_seed_is_refused(install):
    (install / "seed" / "world.md").unlink()
    code, out = _adopt(install)
    assert code == 1 and "seed/world.md could not be read" in out


def test_it_is_refused_while_another_process_writes_the_install(install):
    import fcntl
    import os

    from levain.install import INSTALL_LOCK_REL

    _edit(install, "world.md", "Riverton", "Lakewood")
    fd = os.open(install.joinpath(*INSTALL_LOCK_REL), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code, out = _adopt(install)
    finally:
        os.close(fd)
    assert code == 1 and "another levain process" in out
    assert read_answers(install)["LOCATION"] == "Riverton"


def test_the_cli_wires_it(install, capsys):
    from levain.cli import main

    _edit(install, "world.md", "Riverton", "Lakewood")
    assert main(["adopt-answers", "--path", str(install), "--dry-run"]) == 0
    assert "Would adopt" in capsys.readouterr().out
    assert main(["adopt-answers", "--path", str(install)]) == 0
    assert read_answers(install)["LOCATION"] == "Lakewood"
