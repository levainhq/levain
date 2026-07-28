"""End-to-end `levain init --answers` / `--answers-template` (K2).

The capability: create an entity with NO human at a terminal. It did not exist —
and the first scripted attempt did not fail, it HUNG on an undiscoverable
`Skip this section? [y/N]` gate after silently checkpointing four answers. So the
tests that matter here are the ones that pin NON-INTERACTIVITY as a structural
property, not a hopeful one: nothing may read stdin, and a validation gap must
surface as a loud failure rather than a block.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.answers import AnswersError
from levain.install import _refuse_input, run_answers_template, run_init


def _template(capsys) -> dict[str, str]:
    assert run_answers_template() == 0
    out = capsys.readouterr()
    return json.loads(out.out)


def _filled(capsys) -> dict[str, str]:
    """A complete, plausible answer set for the SHIPPED base interview."""
    a = _template(capsys)
    prose = {
        "COGNITION": "Plays first.\nGrinding breaks it.",
        "WORK": "Levain.",
        "COMMUNICATION": "Direct. Dense.",
        "BOUNDARIES": "Work and personal stay separate.",
    }
    for slot in a:
        a[slot] = prose.get(slot, "")
    a.update({
        "OPERATOR_NAME": "Chris",
        "LOCATION": "Ohio",
        "ROLES": "- Builder",
        "INTERESTS": "Baking",
        "ENTITY_NAME": "Ada",
        "SUBSTRATE": "an open model via Ollama",
        "JOB": "Shipping v2",
    })
    return a


def test_template_is_valid_json_and_writes_nothing(tmp_path: Path, capsys):
    # Discovering what an install WILL ask must never itself alter anything.
    before = set(tmp_path.iterdir())
    data = _template(capsys)
    assert "OPERATOR_NAME" in data and "ENTITY_NAME" in data
    assert set(data.values()) == {""}
    assert set(tmp_path.iterdir()) == before


def test_template_guide_goes_to_stderr_so_the_redirect_yields_valid_json(capsys):
    run_answers_template()
    out = capsys.readouterr()
    json.loads(out.out)                 # stdout alone parses
    assert "BY SLOT NAME" in out.err    # the guide is on stderr


def test_non_interactive_install_creates_a_real_entity(tmp_path: Path, capsys, monkeypatch):
    # THE CAPABILITY. `input` is replaced with a hard failure for the whole run:
    # if any prompt is reached, this test errors instead of hanging.
    def _boom(*_a, **_k):
        raise AssertionError("non-interactive init read stdin")

    monkeypatch.setattr("builtins.input", _boom)

    answers = _filled(capsys)
    af = tmp_path / "answers.json"
    af.write_text(json.dumps(answers), encoding="utf-8")

    install = tmp_path / "entity"
    assert run_init(install, "openhands", force=False, answers_file=af) == 0

    origin = (install / "seed" / "origin.md").read_text(encoding="utf-8")
    world = (install / "seed" / "world.md").read_text(encoding="utf-8")
    assert "Ada" in origin and "Shipping v2" in origin
    assert "Chris" in world and "Ohio" in world
    assert "{{" not in origin.replace("`{{", "")  # nothing left unfilled

    # The answers are RECORDED — which is what makes doctor's content check possible.
    assert json.loads((install / ".levain" / "answers.json").read_text()) == answers


def test_the_installed_entity_passes_its_own_doctor_content_checks(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("prompted")))
    af = tmp_path / "answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    install = tmp_path / "entity"
    assert run_init(install, "openhands", force=False, answers_file=af) == 0

    from levain.doctor import _check_recorded_answers, _check_seed_content

    assert all(r.ok for r in _check_seed_content(install))
    assert all(r.ok for r in _check_recorded_answers(install))


def test_answers_requires_an_adapter_rather_than_prompting(tmp_path: Path, capsys):
    af = tmp_path / "answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    # adapter=None would open the interactive adapter menu; refuse instead.
    assert run_init(tmp_path / "e", None, force=False, answers_file=af) == 1
    assert "--answers requires --adapter" in capsys.readouterr().out


def test_incomplete_answers_fail_before_anything_is_installed(tmp_path: Path, capsys):
    answers = _filled(capsys)
    del answers["ENTITY_NAME"]
    af = tmp_path / "answers.json"
    af.write_text(json.dumps(answers), encoding="utf-8")

    install = tmp_path / "entity"
    assert run_init(install, "openhands", force=False, answers_file=af) == 1
    out = capsys.readouterr().out
    assert "ENTITY_NAME" in out and "missing" in out
    # Never blank-filled: no seed was written.
    assert not (install / "seed").exists()


def test_malformed_file_fails_before_the_install_dir_is_made(tmp_path: Path):
    af = tmp_path / "answers.json"
    af.write_text("{not json", encoding="utf-8")
    install = tmp_path / "entity"
    assert run_init(install, "openhands", force=False, answers_file=af) == 1
    assert not install.exists()


def test_refuse_input_raises_instead_of_blocking():
    # The structural guarantee behind non-interactivity: the engine is handed an
    # input_fn that CANNOT block. A hang is the one failure mode a provisioning
    # path must never have, because it produces no output to diagnose.
    with pytest.raises(AnswersError) as e:
        _refuse_input("  Skip this section? [y/N] ")
    assert "Skip this section?" in str(e.value)
    assert "--answers-template" in str(e.value)
