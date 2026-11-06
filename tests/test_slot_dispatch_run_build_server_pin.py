"""Pins the premise under the claims=None ruling: ``build_server`` is the ONLY constructor of ``RunServer``, and it passes a
real ``replica=``.

DESIGN_claimsnone.md (levain 45, key 1106-1, `project_memory/1106-1/`) ruled the deepseek MED on ``RunServer._rejudge``
(a live holder with ``child != None`` and ``claims=None`` is skipped, not ended) a KNOWN EDGE, because a holder gets
``claims=None`` only on a server built WITHOUT a replica, and such a server has no SYNC and so never re-judges. That
chain rests on two facts about the source, which its author recorded as "a grep result, not an invariant":

  1. outside the class definition, the one ``RunServer(...)`` call under ``levain/`` is inside the function ``build_server``;
  2. that call passes a ``replica=`` keyword whose value is not the literal ``None``.

This file turns both into tests. If one goes red, the ruling's premise has moved: read DESIGN_claimsnone.md section 2
(Shape A / Shape B) and route the claims=None finding back to a design decision. Do not just edit the pin.

The scanner is a pure function over a source string, so the red cases are synthetic sources and no production file is
touched; one test runs it over the real tree.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "levain"
SERVER_FILE = "levain/slot/services/dispatch_run.py"
CLASS, BUILDER = "RunServer", "build_server"

WHY = ("DESIGN_claimsnone.md (levain 45, 1106-1) ruled the claims=None finding a KNOWN EDGE on the premise that "
       "`build_server` is the only constructor of RunServer and passes a real `replica=`; a RunServer without a replica "
       "has claims=None holders and no SYNC re-judge. That premise moved: re-open the design (Shape A / Shape B, section 2) "
       "instead of editing this pin.")


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def runserver_sites(source: str) -> tuple[list[tuple[str | None, ast.Call]], list[str]]:
    """``([(innermost enclosing function name or None, call node)], [aliased imports])`` for every ``RunServer(...)`` call
    in ``source`` outside the ``RunServer`` class body (``RunServer(...)`` or ``mod.RunServer(...)``), and every
    ``import RunServer as <other>`` (an alias would hide a constructor from the scan)."""
    tree = ast.parse(source)
    calls: list[tuple[str | None, ast.Call]] = []
    aliases: list[str] = []

    def visit(node: ast.AST, fn: str | None) -> None:
        if isinstance(node, ast.ClassDef) and node.name == CLASS:
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Call) and _callee(node.func) == CLASS:
            calls.append((fn, node))
        if isinstance(node, ast.ImportFrom):
            aliases.extend(a.asname for a in node.names if a.name == CLASS and a.asname not in (None, CLASS))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, None)
    return calls, aliases


def constructor_violations(sources: dict[str, str]) -> list[str]:
    """Pin 1 over ``{repo-relative path: source}``: every ``RunServer(`` call (or aliased import) not inside ``build_server``
    of ``SERVER_FILE``. An empty list is green."""
    bad: list[str] = []
    for rel, text in sorted(sources.items()):
        calls, aliases = runserver_sites(text)
        bad += [f"{rel}: RunServer imported as {a!r}" for a in aliases]
        for fn, call in calls:
            if not (rel == SERVER_FILE and fn == BUILDER):
                bad.append(f"{rel}:{call.lineno}: RunServer( called in {fn or 'module scope'}")
    return bad


def replica_violations(source: str) -> list[str]:
    """Pin 2 over the source of ``SERVER_FILE``: the sites inside ``build_server`` must be exactly one call, passing a
    ``replica=`` keyword whose value is not the literal ``None``. An empty list is green."""
    sites = [call for fn, call in runserver_sites(source)[0] if fn == BUILDER]
    if len(sites) != 1:
        return [f"{BUILDER} holds {len(sites)} RunServer( calls, expected exactly 1"]
    kws = [k for k in sites[0].keywords if k.arg == "replica"]
    if len(kws) != 1:
        return [f"{BUILDER}'s RunServer( call has no single `replica=` keyword ({len(kws)} found)"]
    value = kws[0].value
    if isinstance(value, ast.Constant) and value.value is None:
        return [f"{BUILDER}'s RunServer( call passes replica=None"]
    return []


_GOOD = '''
class RunServer:
    def __init__(self, launcher, *, replica=None):
        self.twin = RunServer          # a bare name is not a call
def build_server(host):
    replica = AuthorityReplica(host)
    return RunServer(launcher, verify=v, replica=replica, uploads=u)
'''


def test_the_scanner_is_green_on_the_shape_the_ruling_assumes() -> None:
    assert constructor_violations({SERVER_FILE: _GOOD}) == []
    assert replica_violations(_GOOD) == []


@pytest.mark.parametrize("name, rel, source", [
    ("a second constructor in the same file", SERVER_FILE,
     _GOOD + "\ndef build_other(host):\n    return RunServer(launcher, verify=v)\n"),
    ("a second constructor in another module", "levain/other.py",
     "from levain.slot.services.dispatch_run import RunServer\ndef make():\n    return RunServer(l)\n"),
    ("a module-scope constructor", "levain/other.py", "import m\nSERVER = m.RunServer(l)\n"),
    ("a constructor in another file with the builder's name", "levain/other.py",
     "def build_server():\n    return RunServer(l, replica=r)\n"),
    ("an aliased import", "levain/other.py", "from levain.slot.services.dispatch_run import RunServer as RS\n"),
])
def test_mutation_a_second_constructor_is_red(name: str, rel: str, source: str) -> None:
    """Mutation (a): any RunServer( call outside build_server, or an alias that would hide one, fails pin 1."""
    bad = constructor_violations({SERVER_FILE: _GOOD, rel: source} if rel != SERVER_FILE else {rel: source})
    assert bad, f"{name} was NOT caught. " + WHY


def test_mutation_replica_none_is_red() -> None:
    """Mutation (b): `replica=replica` -> `replica=None` fails pin 2."""
    mutated = _GOOD.replace("replica=replica", "replica=None")
    assert mutated != _GOOD
    assert replica_violations(mutated), "replica=None was NOT caught. " + WHY


@pytest.mark.parametrize("old, new", [
    ("replica=replica, ", ""),                       # the keyword dropped
    ("RunServer(launcher,", "RunServer(launcher, replica=None,"),   # a duplicate keyword is also not one good keyword
])
def test_mutation_a_missing_or_doubled_replica_keyword_is_red(old: str, new: str) -> None:
    mutated = _GOOD.replace(old, new)
    assert mutated != _GOOD
    assert replica_violations(mutated), "a missing/doubled replica= was NOT caught. " + WHY


def test_the_real_tree_has_one_constructor_and_it_passes_a_replica() -> None:
    sources = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in sorted(PKG.rglob("*.py"))}
    assert SERVER_FILE in sources, f"{SERVER_FILE} is gone or renamed; the pin has no subject. " + WHY
    bad = constructor_violations(sources)
    assert not bad, "RunServer is constructed outside build_server: " + "; ".join(bad) + ". " + WHY
    bad = replica_violations(sources[SERVER_FILE])
    assert not bad, "; ".join(bad) + ". " + WHY
