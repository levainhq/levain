"""`doctor` carries the ruled authorized-spend cap words and the sizing line (3c-2a, R4 DESIGN §1.1 and §10)."""

from __future__ import annotations

from pathlib import Path

from levain.doctor import SPEND_CAP_SIZING_LINE, SPEND_CAP_WORDS, spend_cap_note

_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def test_doctor_note_carries_both_strings_verbatim_and_no_badge():
    # The note checks nothing, so it must not look like a passing check: no OK / WARN / FAIL badge.
    from levain import doctor
    note = spend_cap_note()
    assert SPEND_CAP_WORDS in note
    assert SPEND_CAP_SIZING_LINE in note
    assert "refused 402 before they start" in note
    assert not any(b in note for b in (doctor._OK, doctor._WARN, doctor._FAIL))


def test_changelog_carries_the_same_strings():
    # The words must reach the changelog verbatim; which section holds them moves at every release, so the whole file is read.
    text = _CHANGELOG.read_text(encoding="utf-8")
    assert SPEND_CAP_WORDS in text
    assert SPEND_CAP_SIZING_LINE in text


def test_the_note_is_wired_into_run_doctor(tmp_path: Path, capsys):
    # L3 words r1 complement LOW 3: both tests above call `spend_cap_note()` directly, so deleting doctor's `print`
    # would survive them. The real entry point, on a scratch install: the note is printed exactly once, unbadged.
    from levain.doctor import run_doctor

    run_doctor(tmp_path)
    out = capsys.readouterr().out
    assert out.count(spend_cap_note()) == 1, out
