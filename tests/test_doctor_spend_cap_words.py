"""`doctor` carries the ruled authorized-spend cap words and the sizing line (3c-2a, R4 DESIGN §1.1 and §10)."""

from __future__ import annotations

from pathlib import Path

from levain.doctor import SPEND_CAP_SIZING_LINE, SPEND_CAP_WORDS, _check_spend_cap_words

_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def test_doctor_row_carries_both_strings_verbatim_as_info():
    (row,) = _check_spend_cap_words()
    assert row.ok and not row.warn and row.hint is None
    assert SPEND_CAP_WORDS in row.detail
    assert SPEND_CAP_SIZING_LINE in row.detail
    assert "refused 402 before they start" in row.detail


def test_changelog_unreleased_carries_the_same_strings():
    text = _CHANGELOG.read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert SPEND_CAP_WORDS in unreleased
    assert SPEND_CAP_SIZING_LINE in unreleased
