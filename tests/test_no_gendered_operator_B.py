"""levain#119: the operator is never "his / him / he" in Levain's own runtime text (lane B files).

Parses each listed source with ``ast``, takes every string Constant that is not a docstring, and asserts none
matches \\b(his|him|he)\\b except the KEPT allowlist below (each a (file, quoted phrase) pair with its reason).
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAT = re.compile(r"\b(his|him|himself|he)\b", re.I)

FILES = [
    "levain/cockpit/authority.py",
    "levain/consent/adapters/claude_code.py",
    "levain/consent/publish.py",
    "levain/consent/push_gate.py",
    "levain/desk/arming_gate.py",
    "levain/desk/dream_status.py",
    "levain/desk/effects.py",
    "levain/desk/events.py",
    "levain/desk/first_reader.py",
    "levain/desk/morning.py",
    "levain/desk/review.py",
    "levain/desk/room.py",
    "levain/desk/room_mcp.py",
    "levain/desk/seam_plan.py",
    "levain/desk/standing_orders.py",
    "levain/desk_click.py",
    "levain/dispatch/calibration.py",
    "levain/dream/agent.py",
    "levain/dream/card.py",
    "levain/dream/cli.py",
    "levain/dream/recover.py",
    "levain/slot/adopt_take.py",
    "levain/slot/adopt_view.py",
    "levain/webpush/intent.py",
    "levain/webpush/open_ping.py",
    "levain/webpush/presence.py",
]

# (file, quoted phrase): why it is kept.
KEPT = [
    # levain/keep_migrate.py is not in FILES: its literals are rewritten by the levain#88 T11 code link, which lands that file;
    # add it here, with its stopword list as a KEPT entry, once that link is on main.
]

# (file, the whole literal): a stored dict key or field name, read by other code and by old data. It is a compatibility schema key: it may appear in serialized output (dream rollback prints the card), it is not a sentence said to anyone.
KEPT_KEYS = [
    ("levain/desk/review.py", "his", "an item body field name ('his' on an agent card) that item_text reads"),
    ("levain/dream/card.py", "his", "the card body's 'his' list key, stored in cards and read by the desk"),
    ("levain/dream/cli.py", "his", "reads the card's stored 'his' list key"),
    ("levain/dream/recover.py", "his", "writes the card's stored 'his' list key"),
]


def _strings(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = getattr(n, "body", [])
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) \
                    and isinstance(b[0].value.value, str):
                doc.add(id(b[0].value))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc:
            yield n.lineno, n.value


@pytest.mark.parametrize("rel", FILES)
def test_no_gendered_operator(rel):
    bad = []
    for ln, s in _strings(rel):
        if not PAT.search(s):
            continue
        if any(f == rel and phrase in s for f, phrase, _why in KEPT):
            continue
        if any(f == rel and key == s for f, key, _why in KEPT_KEYS):
            continue
        bad.append(f"{rel}:{ln}: {s[:120]!r}")
    assert not bad, "gendered pronoun in runtime text:\n" + "\n".join(bad)
