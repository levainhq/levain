"""The short pairing code's folding rule: the rule the cockpit's short code is typed through (``levain.cockpit.shortcode``). The
phone page that carried the typed box is deleted (OS-6); the cockpit's mint, redeem and lock are pinned in
``tests/test_cockpit_w1b_server.py``."""
from __future__ import annotations

from levain.cockpit.shortcode import normalize_short


def test_normalize_folds_case_hyphens_spaces_and_look_alikes():
    assert normalize_short("abcd-efgh") == "ABCDEFGH"
    assert normalize_short("  AB CD\tEF-GH \n") == "ABCDEFGH"
    assert normalize_short("abcd–efgh") == "ABCDEFGH"            # a keyboard's en dash for the hyphen
    assert normalize_short("OIL0-1234") == "01101234"                  # O reads 0, I and L read 1
    assert normalize_short("oil0-1234") == "01101234"
    for bad in ("", "   ", "-", "ABCD-EFG", "ABCD-EFGHJ", "ABCD-EFGU", "ABCD-EF!H", "ABCD-EFGÉ", "ABCD-ＥFGH",
                "x" * 65, None, 12345678, b"ABCDEFGH"):
        assert normalize_short(bad) is None, bad
