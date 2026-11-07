"""The user-facing naming: "desk" is the word for the seat where the operator meets Levain, "coordinator" is the
mechanism's name (store, lease, launch, record, workspace, role). "At the desk" for being physically present is
"at the laptop". The table that classed each phrase is project_memory/1107-26-naming/NAMING_TABLE.md; this test holds
the README, docs/, the main parser's help text and the cockpit's visible strings to it."""
import argparse
import re
from pathlib import Path

from levain import cli

ROOT = Path(__file__).resolve().parent.parent

# the phrases that name the mechanism; "coordinator" may stay inside these
MECHANISM = [
    r"coordinator launch", r"coordinator role", r"coordinator lease", r"coordinator store", r"coordinator record",
    r"coordinator workspace", r"coordinator queue", r"coordinator agent", r"coordinator directory",
    r"coordinator verb", r"--by coordinator", r"`coordinator:`", r"dead coordinator", r"holds a live lease",
    r"levain/coordinator/", r"tests/test_coordinator", r"levain coord", r"coordinator action",
    r"coordinator setting", r"coordinator install", r"coordinator review", r"coordinator-wake",
    # the session's lifecycle (launch, lease, the setting that names which one a drop starts)
    r"coordinator session", r"coordinator now", r"claude coordinator", r"coordinator a drop launches",
    r"pending coordinator", r"coordinator's sessions", r"drops with no coordinator", r"coordinator also loads",
    r"coordinator runs in", r"coordinator whose", r"no live coordinator", r"coordinator live", r"coordinator \(needs",
    r"a coordinator thread", r"next coordinator", r"fresh coordinator", r"live coordinator lease",
    r"<levain home>/coordinator", r"coordinator runs", r"Codex coordinator", r"writing as coordinator",
    r"coordinator's own mode",
]
PHYSICAL = re.compile(r"\b(?:at|from) (?:the|your) desk\b", re.I)


def _stripped(text: str) -> str:
    for pat in MECHANISM:
        text = re.sub(pat, "", text, flags=re.I)
    return text


def _seat_hits(text: str):
    return [m.group(0) for m in re.finditer(r"\bcoordinator(?:'s|s)?\b", _stripped(text), flags=re.I)]


def test_readme_says_desk_for_the_seat_and_laptop_for_presence():
    text = (ROOT / "README.md").read_text()
    assert not PHYSICAL.findall(text), PHYSICAL.findall(text)
    assert not _seat_hits(text), _seat_hits(text)


def test_docs_say_laptop_for_presence():
    for path in sorted((ROOT / "docs").rglob("*.md")):
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


def test_cli_help_says_desk_for_the_seat_and_laptop_for_presence():
    texts = list(_help_strings(cli.build_parser()))
    assert len(texts) > 100
    for t in texts:
        assert not PHYSICAL.search(t), t
        assert not _seat_hits(t), (_seat_hits(t), t[:120])


def test_cockpit_visible_strings_say_desk_for_the_seat():
    web = ROOT / "levain" / "templates" / "web"
    bad = []
    for path in sorted(web.iterdir()):
        if path.suffix not in (".js", ".html"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*(//|/\*|\*|<!--)", line):
                continue
            for lit in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', line):
                if " " not in lit.strip():
                    continue
                if PHYSICAL.search(lit) or _seat_hits(lit):
                    bad.append(f"{path.name}:{n}: {lit[:100]}")
    assert not bad, bad
