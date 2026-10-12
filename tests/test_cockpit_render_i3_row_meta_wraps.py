"""1012+100 item 3: at 390 px the Now card's row meta ("tray · Overdue · 40d overdue · 42d old · due 2026-09-02")
overflowed the card edge. Measured live on the served page (meta right edge 378 against the card's 363): the meta
span was `flex: 0 0 auto` with no wrap, so a long meta line could never be narrower than its text.

There is no layout engine in a unit test, so this asserts the rule that makes the meta shrinkable and breakable:
it may shrink (not `flex: 0 0 auto`), has `min-width: 0` and `max-width: 100%`, and breaks inside a long token."""

from __future__ import annotations

import re

from tests._ke_dom import WEB


def _rule(css: str, selector: str) -> str:
    m = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


def test_the_row_meta_can_shrink_and_wrap_inside_its_card() -> None:
    body = _rule((WEB / "dashboard.css").read_text(encoding="utf-8"), ".ext-meta")
    assert "flex: 0 0 auto" not in body and "flex:0 0 auto" not in body
    assert re.search(r"flex:\s*0 1 auto", body)
    assert re.search(r"min-width:\s*0", body) and re.search(r"max-width:\s*100%", body)
    assert re.search(r"overflow-wrap:\s*anywhere", body)
