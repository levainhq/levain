"""The operator-facing name is "List": no user string in the TUI or the CLI help says "Tray"."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# code names that keep the word: the TRAY_* identifiers and the `tray` wire/panel id
_ALLOWED = re.compile(r"TRAY_\w+|`tray`")


def _user_strings(path):
    tree = ast.parse((ROOT / path).read_text())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def test_no_tray_in_user_strings():
    for path in ("levain/_tui_curses.py", "levain/tui.py", "levain/cli.py"):
        for n, text in _user_strings(path):
            if text == "tray":  # the panel kind id, a code name
                continue
            assert not re.search(r"\btray\b", _ALLOWED.sub("", text), re.I), f"{path}:{n}: {text!r}"
