"""Every stylesheet the cockpit serves closes each comment it opens. An unclosed comment swallows every rule after it, and a
page test that never reads computed style cannot see that."""

from __future__ import annotations

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
CSS = sorted(WEB.glob("*.css"))


def comment_problems(text: str) -> list[str]:
    """Walk the sheet comment by comment: an opener with no closer, a closer with no opener, or an opener inside a comment."""
    problems: list[str] = []
    i, n = 0, len(text)
    while i < n:
        a = text.find("/*", i)
        z = text.find("*/", i)
        if a == -1 and z == -1:
            break
        if z != -1 and (a == -1 or z < a):
            problems.append(f"closer with no opener at offset {z}")
            i = z + 2
            continue
        end = text.find("*/", a + 2)
        if end == -1:
            problems.append(f"comment opened at offset {a} is never closed")
            break
        if "/*" in text[a + 2:end]:
            problems.append(f"comment at offset {a} contains a nested opener")
        i = end + 2
    return problems


@pytest.mark.parametrize("path", CSS, ids=[p.name for p in CSS])
def test_every_comment_is_closed_and_none_nests(path: Path) -> None:
    text = path.read_text()
    assert text.count("/*") == text.count("*/"), f"{path.name}: opener and closer counts differ"
    assert comment_problems(text) == []


def test_the_check_fails_on_the_broken_sheet() -> None:
    text = (WEB / "dashboard.css").read_text()
    broken = text.replace("simply not drawn. */", "simply not drawn. *", 1)
    assert broken != text
    assert broken.count("/*") != broken.count("*/")
    assert comment_problems(broken)
