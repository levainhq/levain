"""levain#119 (lane A): the operator is never "his / him / he" in the runtime text of levain/desk/launch.py and
levain/desk/cli.py. Every string Constant that is not a docstring is checked."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ("levain/desk/launch.py", "levain/desk/cli.py")
PAT = re.compile(r"\b(his|him|himself|he)\b", re.I)

#: (file, quoted phrase, reason): a literal that names someone other than the operator. Empty: lane A kept none.
KEPT: list[tuple[str, str, str]] = []


def _strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = getattr(n, "body", [])
            if (b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant)
                    and isinstance(b[0].value.value, str)):
                doc.add(id(b[0].value))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc:
            yield n.lineno, n.value


def test_no_gendered_operator_in_lane_a():
    bad = []
    for rel in FILES:
        for ln, s in _strings(ROOT / rel):
            if PAT.search(s) and not any(f == rel and q in s for f, q, _ in KEPT):
                bad.append(f"{rel}:{ln}: {s[:120]!r}")
    assert not bad, "gendered pronoun for the operator:\n" + "\n".join(bad)
