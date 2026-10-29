"""Q17-ALL S2: the shorthand parser's golden table (pure: no store)."""
from __future__ import annotations

import pytest

from levain.band_shorthand import ItemInfo, View, parse

INFO = ItemInfo("information", 3, "b")
AUTH = ItemInfo("authority", 2, "a")
ITEMS = {1: INFO, 2: INFO, 3: INFO, 4: INFO, 5: AUTH, 6: ItemInfo("information", 3, None)}
VIEW = View(seen_through=6, open_list=(1, 2, 3, 4, 5, 6), store_id="qs-1")


def run(text, view=VIEW, store="qs-1", items=ITEMS):
    return parse(text, view, items, store)


def acts(p):
    return [(a.n, a.pick, a.text, a.note, a.via_rec) for a in p.actions]


GOLDEN = [
    ("1a", [(1, "a", None, None, False)]),
    ("1A", [(1, "a", None, None, False)]),
    ("1 a", [(1, "a", None, None, False)]),
    ("1(a)", [(1, "a", None, None, False)]),
    ("1) c", [(1, "c", None, None, False)]),
    ("2: rec", [(2, "b", None, None, True)]),
    ("2 rec", [(2, "b", None, None, True)]),
    ("2 your rec", [(2, "b", None, None, True)]),
    ("2 you rec", [(2, "b", None, None, True)]),
    ("2 r", [(2, "b", None, None, True)]),
    ("1a, 2: rec", [(1, "a", None, None, False), (2, "b", None, None, True)]),
    ("1a 2c", [(1, "a", None, None, False), (2, "c", None, None, False)]),
    ("1a; 4c", [(1, "a", None, None, False), (4, "c", None, None, False)]),
    ("3 b because the API is down", [(3, "b", None, "the API is down", False)]),
    ("3 b but only maybe", [(3, "b", None, "only maybe", False)]),
    ("3 b - verbatim: X, Y; Z", [(3, "b", None, "verbatim: X, Y; Z", False)]),
    ("3 b — em dash note", [(3, "b", None, "em dash note", False)]),
    ("3: no longer needed", [(3, None, "no longer needed", None, False)]),
    ("3: a thing, with commas; and more", [(3, None, "a thing, with commas; and more", None, False)]),
    ("1a, 1a", [(1, "a", None, None, False)]),                       # the same pick twice: once
    ("2b, 2 rec", [(2, "b", None, None, False)]),                    # b and rec->b are the same pick
]


@pytest.mark.parametrize("text,want", GOLDEN)
def test_golden_records(text, want):
    p = run(text)
    assert p.matched and not p.refused, p
    assert acts(p) == want


def test_sweep_takes_only_seen_open_information_items_nobody_named():
    p = run("1a, all recs")
    assert [(a.n, a.pick, a.via_rec) for a in p.actions] == [(1, "a", False), (2, "b", True), (3, "b", True),
                                                            (4, "b", True)]
    # 5 is authority: left alone, said; 6 has no rec: said
    assert sorted(r.n for r in p.refused) == [5, 6]
    assert "signature" in next(r.reason for r in p.refused if r.n == 5)


@pytest.mark.parametrize("word", ["all recs", "rest rec", "others rec", "all others recs", "ALL RECS"])
def test_sweep_spellings(word):
    assert {a.n for a in run(word).actions} == {1, 2, 3, 4}


def test_sweep_never_takes_an_item_filed_after_the_view():
    view = View(seen_through=2, open_list=(1, 2), store_id="qs-1")
    p = run("all recs", view)
    assert [a.n for a in p.actions] == [1, 2]


def test_sweep_note_goes_on_each_swept_item():
    p = run("all recs because fine")
    assert {a.note for a in p.actions} == {"fine"}


def test_conflict_refuses_that_item_and_the_rest_proceed():
    p = run("3a, 3b, 1c")
    assert [a.n for a in p.actions] == [1]
    assert [(r.n, r.reason) for r in p.refused] == [(3, "conflict: 3a and 3b")]


def test_pick_against_text_is_a_conflict():
    p = run("3a\n3: no")
    assert not p.actions and p.refused[0].reason.startswith("conflict")


def test_number_above_the_views_high_water_mark_is_refused():
    p = run("1a, 7b")
    assert [a.n for a in p.actions] == [1]
    assert p.refused[0].n == 7 and "filed after the band you saw" in p.refused[0].reason


def test_a_number_not_open_in_the_view_is_refused():
    view = View(seen_through=6, open_list=(1, 2), store_id="qs-1")
    p = run("3a", view)
    assert not p.actions and "not open" in p.refused[0].reason


def test_store_id_mismatch_refuses_everything():
    p = run("1a, all recs", store="qs-OTHER")
    assert p.matched and not p.actions and len(p.refused) == 1 and "another question store" in p.refused[0].reason


def test_no_view_refuses_everything():
    p = run("1a", view=None)
    assert p.matched and not p.actions and "no band was shown" in p.refused[0].reason


def test_authority_item_records_nothing():
    p = run("5a")
    assert not p.actions and p.refused[0].reason == "5 needs your signature; nothing recorded"


def test_letter_past_the_options_is_refused_for_that_item():
    p = run("1f, 2a")
    assert [a.n for a in p.actions] == [2] and "no option f" in p.refused[0].reason


def test_rec_on_an_item_with_no_rec_is_refused():
    p = run("6 rec")
    assert not p.actions and "no rec" in p.refused[0].reason


@pytest.mark.parametrize("text", [
    "(a) all recs",                  # a pick with no number is never guessed onto an item
    "1a foo",                        # trailing words that are not a note
    "1ab",
    "1a2b",
    "1 because",
    "0a",
    "1a, nonsense, 2b",              # one bad entry spoils the whole line
    "1 b because",                   # a note with no words
    "2:",
    "all recs please",
    "hello there",
    "",
    "   ",
    "12345678a",
])
def test_a_line_that_does_not_fully_parse_records_nothing(text):
    p = run(text)
    assert not p.matched and not p.actions and not p.refused
    assert p.rest == text


def test_hint_only_for_a_line_that_starts_like_an_answer():
    assert run("1a foo").hint and "nothing was recorded" in run("1a foo").hint
    assert run("hello there").hint is None
    assert "number in front" in run("(a) all recs").hint
    assert run("(a) all recs").hint.startswith("Levain did not read this as a band answer")


def test_a_leading_block_then_the_rest_goes_to_the_agent_untouched():
    p = run("1a\n2b\nnow please fix the bug\nand more")
    assert [a.n for a in p.actions] == [1, 2]
    assert p.rest == "now please fix the bug\nand more"


def test_a_message_whose_first_line_is_prose_is_not_parsed_even_if_later_lines_would():
    p = run("hello\n1a")
    assert not p.matched and not p.actions and p.rest == "hello\n1a"


def test_note_attaches_to_the_entry_before_it_only():
    p = run("1a, 2 b because two")
    assert acts(p) == [(1, "a", None, None, False), (2, "b", None, "two", False)]


def test_repeat_with_a_note_keeps_the_note():
    p = run("1a\n1a because why")
    assert acts(p) == [(1, "a", None, "why", False)]


# --- fix round 1 ----------------------------------------------------------------------------------------------------
def _colon_line(count):
    return ", ".join(f"{i}: a" for i in range(1, count + 1))


def test_the_lexer_is_linear_a_5000_entry_colon_form_line_takes_under_a_second():
    """L3 r1 HIGH (complement): each `NUM: pick` re-lexed its tail, doubling the work per entry (the hook hung)."""
    import time

    from levain.band_shorthand import _lex

    line = _colon_line(5000)
    t = time.monotonic()
    lexed = _lex(line)
    assert time.monotonic() - t < 1.0
    assert lexed.error is None and len(lexed.entries) == 5000 and lexed.entries[-1].n == 5000
    t = time.monotonic()
    p = parse(line, View(5000, tuple(range(1, 5001)), "qs-1"), {n: INFO for n in range(1, 5001)}, "qs-1")
    assert time.monotonic() - t < 1.0
    assert not p.matched and not p.actions and "too long" in p.hint     # refused for length before any lexing


def test_a_long_colon_form_line_under_the_cap_still_parses():
    p = parse(_colon_line(30), View(30, tuple(range(1, 31)), "qs-1"), {n: INFO for n in range(1, 31)}, "qs-1")
    assert p.matched and [a.n for a in p.actions] == list(range(1, 31)) and {a.pick for a in p.actions} == {"a"}


def test_free_text_after_colon_picks_takes_only_the_last_ambiguous_entry():
    # 1 and 2 are picks (their tails lex cleanly); 3 is free text (its tail "thing, more" does not)
    assert acts(run("1: a, 2: b, 3: a thing, more")) == [(1, "a", None, None, False), (2, "b", None, None, False),
                                                          (3, None, "a thing, more", None, False)]


def test_a_blank_line_ends_the_answer_block_and_is_kept_in_rest():
    """L3 r1 MED (codex): `1a\\n\\n2b` recorded both."""
    p = run("1a\n\n2b")
    assert [a.n for a in p.actions] == [1] and p.rest == "\n2b"
    assert run("1a\n").rest == ""                                          # a trailing newline is nothing after


def test_the_sweep_says_so_for_an_open_item_it_cannot_read():
    """L3 r1 LOW (complement): an unreadable open item was skipped by "all recs" without a word."""
    items = {n: i for n, i in ITEMS.items() if n != 4}
    p = run("all recs", items=items)
    assert [a.n for a in p.actions] == [1, 2, 3]
    assert any(r.n == 4 and "could not be read" in r.reason for r in p.refused)
