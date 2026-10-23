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


def test_changelog_unreleased_carries_the_same_strings():
    text = _CHANGELOG.read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert SPEND_CAP_WORDS in unreleased
    assert SPEND_CAP_SIZING_LINE in unreleased
