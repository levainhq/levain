"""The user-facing naming: "desk" is the one word for the seat where the operator meets Levain and for the mechanism behind it
(store, lease, launch, record, workspace, role); the old word for the mechanism is spelled only in levain/desk/legacy.py.
"At the desk" for being physically present is "at the laptop". This test holds the README, docs/, the main parser's help text
and the double- and single-quoted literals with a space in every .js/.html under levain/templates/web to that. Strings the
CLI prints and the band's text are not scanned."""
import argparse
import re
from pathlib import Path

from levain import cli

ROOT = Path(__file__).resolve().parent.parent

PHYSICAL = re.compile(r"\b(?:(?:at|from) (?:the|your)|wait for (?:the|your)) desk\b", re.I)


def test_readme_says_laptop_for_presence():
    text = (ROOT / "README.md").read_text()
    assert not PHYSICAL.findall(text), PHYSICAL.findall(text)


def test_docs_say_laptop_for_presence():
    paths = sorted((ROOT / "docs").rglob("*.md"))
    assert paths
    for path in paths:
        found = PHYSICAL.findall(path.read_text())
        assert not found, (path.name, found)


def _help_strings(parser: argparse.ArgumentParser, seen=None):
    seen = set() if seen is None else seen
    if id(parser) in seen:
        return
    seen.add(id(parser))
    for s in (parser.description, parser.epilog):
        if s:
            yield s
    for act in parser._actions:
        if act.help and act.help != argparse.SUPPRESS:
            yield act.help
        if isinstance(act, argparse._SubParsersAction):
            for choice in act._choices_actions:
                if choice.help:
                    yield choice.help
            for sub in act.choices.values():
                yield from _help_strings(sub, seen)


def test_cli_help_says_laptop_for_presence():
    texts = list(_help_strings(cli.build_parser()))
    assert len(texts) > 100
    for t in texts:
        assert not PHYSICAL.search(t), t


def _literals(line: str):
    return [a or b for a, b in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"|\'([^\'\\]*(?:\\.[^\'\\]*)*)\'', line)]


def test_cockpit_visible_strings_say_laptop_for_presence():
    web = ROOT / "levain" / "templates" / "web"
    bad = []
    for path in sorted(web.rglob("*")):
        if path.suffix not in (".js", ".html"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*(//|/\*|\*|<!--)", line):
                continue
            for lit in _literals(re.sub(r"\s//\s.*$", "", line)):   # a trailing comment is not a string
                if " " not in lit.strip():
                    continue
                if PHYSICAL.search(lit):
                    bad.append(f"{path.name}:{n}: {lit[:100]}")
    assert not bad, bad
