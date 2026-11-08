"""README currency pin: every `levain ...` command in a code span or code block resolves in the real parser, every flag
named beside it exists on that command (or one above it), and every `tests/<file>::<test>` locator names a test that is
in that file. The parser is `levain.cli.build_parser`, not a copy.

What it does not check: positional values (`consent away on`, `team record decision`), placeholders, and prose. A sub-verb
the README names as GONE is listed in GONE and must stay absent from the parser, so a verb that comes back turns the
sentence stale and this test red."""
import argparse
import re
import shlex
from pathlib import Path

from levain.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
# README sentences that say a command is gone or not in this build: (path to the node, the missing word).
GONE = {(("ping",), "revoke"), (("ping",), "pair"), (("cockpit", "ledger"), "re-genesis")}


def _subs(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def _flags(parser):
    return {s for a in parser._actions for s in a.option_strings}


def _spans(text):
    """Every command line the README shows: inline code spans that begin with `levain `, and fenced-block lines."""
    out = [m.group(1) for m in re.finditer(r"`(levain [^`\n]*)`", text)]
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
        out += [re.sub(r"\s+#.*$", "", ln).strip() for ln in block.splitlines() if re.match(r"\s*levain ", ln)]
    return out


def _problems(text):
    root = build_parser()
    bad = []
    for sp in _spans(text):
        try:
            toks = shlex.split(sp)
        except ValueError:
            toks = sp.split()
        toks = toks[1:]
        node, path, i = root, [], 0
        while i < len(toks) and toks[i] in _subs(node):
            node = _subs(node)[toks[i]]
            path.append(toks[i])
            i += 1
        chain = [root]
        n = root
        for w in path:
            n = _subs(n)[w]
            chain.append(n)
        if i < len(toks) and _subs(node) and re.fullmatch(r"[a-z][a-z-]*", toks[i]):
            if (tuple(path), toks[i]) not in GONE:
                bad.append(f"verb {' '.join(path + [toks[i]])!r} is not in the parser: {sp}")
        for t in toks[i:]:
            m = re.match(r"(--[a-z][a-z0-9-]*)", t)
            if m and not any(m.group(1) in _flags(c) for c in chain):
                bad.append(f"flag {m.group(1)} is not on `levain {' '.join(path)}`: {sp}")
    for (path, word) in GONE:
        n = root
        for w in path:
            n = _subs(n)[w]
        if word in _subs(n):
            bad.append(f"README says `{' '.join(path)} {word}` is gone but the parser has it")
    return bad


def _locator_problems(text):
    bad = []
    for fm in re.finditer(r"`(tests/[a-z0-9_/]+\.py)::(test_[a-z0-9_]+)`", text):
        f, t = fm.group(1), fm.group(2)
        src = (ROOT / f).read_text(encoding="utf-8") if (ROOT / f).is_file() else ""
        if not re.search(rf"^\s*(async )?def {t}\b", src, re.M):
            bad.append(f"{f}::{t} is not a test in that file")
    return bad


def test_every_levain_command_and_flag_in_the_readme_resolves():
    assert not _problems(README), "\n".join(_problems(README))


def test_every_test_locator_in_the_readme_names_a_test_in_its_file():
    assert re.search(r"`tests/[a-z0-9_/]+\.py::test_", README), "the README names no locator: the pin reads nothing"
    assert not _locator_problems(README), "\n".join(_locator_problems(README))


def test_the_command_pin_goes_red_on_a_bogus_flag_a_bogus_verb_and_a_returned_verb():
    """Mutation proof: the same check on doctored copies must fail (a probe that cannot fail is not evidence)."""
    assert _problems(README + "\n`levain doctor --no-such-flag`\n")
    assert _problems(README + "\n`levain consent no-such-verb`\n")
    assert _problems(README + "\n`levain cockpit pair --no-such-flag`\n")
    assert _problems(README + "\n```\nlevain serve --bogus\n```\n")
    # a verb the README calls gone stays gone: it must be in GONE to be tolerated, and absent from the parser
    assert not _problems(README + "\n`levain ping revoke`\n")
    assert _problems(README + "\n`levain ping revoke-everything`\n")


def test_the_locator_pin_goes_red_on_a_wrong_file_and_a_wrong_test():
    assert _locator_problems(README + "\n`tests/test_readme_commands.py::test_nope`\n")
    assert _locator_problems(README + "\n`tests/test_room_phone.py::test_every_listed_verb_is_named_in_all_commands`\n")
    assert not _locator_problems(
        README + "\n`tests/test_readme_commands.py::test_every_listed_verb_is_named_in_all_commands`\n")
