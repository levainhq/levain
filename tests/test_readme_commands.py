"""README "All commands": the section names every top-level verb the parser lists, and only verbs that exist.

The verb list is read from the real argparse parser (`levain.cli.build_parser`), not from a copy. A verb is HIDDEN when
it is registered without a `help=` line (argparse then leaves it out of `levain --help`); hidden verbs are not required
here, and a hidden verb in the table is also a failure (the table lists what `levain --help` lists)."""
import argparse
import re
from pathlib import Path

from levain.cli import build_parser

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
HEADING = "## All commands"
ROW = re.compile(r"^\| `levain ([a-z][a-z0-9-]*)` \| (.+?) \|$", re.M)


def _parser_verbs() -> tuple[set[str], set[str]]:
    """(listed, hidden) top-level verbs of the real parser."""
    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    listed = {a.dest for a in sub._choices_actions}
    return listed, set(sub.choices) - listed


def _rows(text: str) -> dict[str, str]:
    assert text.count(HEADING + "\n") == 1, f"the README needs exactly one {HEADING!r} heading"
    body = text.split(HEADING + "\n", 1)[1]
    section = re.split(r"^#{1,2} ", body, maxsplit=1, flags=re.M)[0]
    return {m.group(1): m.group(2) for m in ROW.finditer(section)}


def _missing(text: str) -> list[str]:
    """Listed verbs the section does not name."""
    return sorted(_parser_verbs()[0] - set(_rows(text)))


def _extra(text: str) -> list[str]:
    """Section rows that name no listed verb (gone, renamed, or hidden)."""
    return sorted(set(_rows(text)) - _parser_verbs()[0])


def test_every_listed_verb_is_named_in_all_commands():
    assert not _missing(README), f"`levain --help` lists these but {HEADING!r} does not: {_missing(README)}"


def test_every_row_in_all_commands_is_a_listed_verb():
    assert not _extra(README), f"rows naming no listed verb: {_extra(README)}"


def test_every_row_has_a_description():
    assert not [v for v, d in _rows(README).items() if not d.strip()]


def test_the_two_checks_go_red_on_a_deleted_row_and_on_a_bogus_row():
    """Mutation proof, kept as a test: the same checks on a doctored copy of the README must fail."""
    rows = list(ROW.finditer(README))
    assert rows, "no rows to doctor"
    first = rows[0]
    without = README[: first.start()] + README[first.end() + 1:]
    assert _missing(without) == [first.group(1)]
    bogus = README[: first.start()] + "| `levain no-such-verb` | invented |\n" + README[first.start():]
    assert _extra(bogus) == ["no-such-verb"]
    hidden = sorted(_parser_verbs()[1])
    if hidden:  # a hidden verb pasted into the table is a failure too
        h = README[: first.start()] + f"| `levain {hidden[0]}` | hidden |\n" + README[first.start():]
        assert _extra(h) == [hidden[0]]
