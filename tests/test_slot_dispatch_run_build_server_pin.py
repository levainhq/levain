"""Pins the premise under the claims=None ruling: ``build_server`` is the ONLY constructor of ``RunServer``, and it passes a
real ``replica=``.

DESIGN_claimsnone.md (levain 45, key 1106-1, `project_memory/1106-1/`) ruled the deepseek MED on ``RunServer._rejudge``
(a live holder with ``child != None`` and ``claims=None`` is skipped, not ended) a KNOWN EDGE, because a holder gets
``claims=None`` only on a server built WITHOUT a replica, and such a server has no SYNC and so never re-judges. That
chain rests on two facts about the source, which its author recorded as "a grep result, not an invariant":

  1. every ``RunServer(...)`` call under ``levain/`` (the class body included) is inside the function ``build_server``;
  2. that call passes a ``replica=`` whose origin is a direct ``AuthorityReplica(...)`` call, assigned exactly once, as a
     plain top-level statement of ``build_server``, before the call: never ``None``, never a conditional, never a parameter.

This file turns both into tests. If one goes red, the ruling's premise has moved: read DESIGN_claimsnone.md section 2
(Shape A / Shape B) and route the claims=None finding back to a design decision. Do not just edit the pin.

Shapes the scanner covers (anywhere under ``levain/``, tests excluded because the scan reads only the package): a direct call
``RunServer(...)`` / ``mod.RunServer(...)``; ``from m import RunServer as X``; an assignment whose value is the bare name
``RunServer`` / ``m.RunServer`` (``ctor = RunServer``); a subclass ``class X(RunServer)``. A call is attributed to its
INNERMOST enclosing function, so a nested helper inside ``build_server`` that calls ``RunServer`` is a red on purpose (it
fails closed: move the call back, or re-read the design).

Out of scope, by decision, because proving Python name resolution is not this file's job: ``getattr``/``importlib``/
``functools.partial(RunServer, ...)``/``type(self)(...)`` and ``cls(...)`` inside the class. Those are not constructions a
caller of this package writes by accident.

The scanner is a pure function over a source string, so the red cases are synthetic sources and no production file is
touched; one test runs it over the real tree.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "levain"
SERVER_FILE = "levain/slot/services/dispatch_run.py"
CLASS, BUILDER, REPLICA_CLASS = "RunServer", "build_server", "AuthorityReplica"

WHY = ("DESIGN_claimsnone.md (levain 45, 1106-1) ruled the claims=None finding a KNOWN EDGE on the premise that "
       "`build_server` is the only constructor of RunServer and passes a real `replica=`; a RunServer without a replica "
       "has claims=None holders and no SYNC re-judge. That premise moved: re-open the design (Shape A / Shape B, section 2) "
       "instead of editing this pin.")

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_class_ref(node: ast.expr | None) -> bool:
    """The bare name ``RunServer`` or an attribute ending in ``.RunServer`` (not a call of it)."""
    return (isinstance(node, ast.Name) and node.id == CLASS) or (isinstance(node, ast.Attribute) and node.attr == CLASS)


def runserver_sites(source: str) -> tuple[list[tuple[str | None, ast.Call]], list[tuple[int, str]]]:
    """``([(innermost enclosing function name or None, call node)], [(lineno, message)])`` over the WHOLE of ``source``,
    the ``RunServer`` class body included: every ``RunServer(...)`` / ``mod.RunServer(...)`` call, plus the shapes that
    would hide a constructor from the call scan (an ``import RunServer as <other>``, an assignment whose value is the class
    itself, a subclass)."""
    tree = ast.parse(source)
    calls: list[tuple[str | None, ast.Call]] = []
    shapes: list[tuple[int, str]] = []

    def visit(node: ast.AST, fn: str | None) -> None:
        if isinstance(node, _FUNCS):
            fn = node.name                    # the INNERMOST function: a nested def's calls belong to the nested def
        if isinstance(node, ast.Call) and _callee(node.func) == CLASS:
            calls.append((fn, node))
        if isinstance(node, ast.ImportFrom):
            shapes.extend((node.lineno, f"RunServer imported as {a.asname!r}")
                          for a in node.names if a.name == CLASS and a.asname not in (None, CLASS))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and _is_class_ref(node.value):
            shapes.append((node.lineno, "RunServer bound to another name by assignment (an alias hides the call)"))
        if isinstance(node, ast.ClassDef) and any(_is_class_ref(b) for b in node.bases):
            shapes.append((node.lineno, f"class {node.name} subclasses RunServer (a subclass can drop `replica=`)"))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, None)
    return calls, shapes


def constructor_violations(sources: dict[str, str]) -> list[str]:
    """Pin 1 over ``{repo-relative path: source}``: every ``RunServer(`` call not inside ``build_server`` of ``SERVER_FILE``
    (the class body included), plus every alias or subclass shape. An empty list is green."""
    bad: list[str] = []
    for rel, text in sorted(sources.items()):
        calls, shapes = runserver_sites(text)
        bad += [f"{rel}:{ln}: {msg}" for ln, msg in shapes]
        for fn, call in calls:
            if not (rel == SERVER_FILE and fn == BUILDER):
                bad.append(f"{rel}:{call.lineno}: RunServer( called in {fn or 'module scope'}")
    return bad


def _own_nodes(func: ast.AST) -> Iterator[ast.AST]:
    """Every node of ``func``'s own scope: nested defs/lambdas/classes are yielded but not entered."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_FUNCS, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _origin_violations(builder: ast.FunctionDef, name: str, call: ast.Call) -> list[str]:
    """``name`` (what ``replica=`` is given) must be bound ONCE in ``builder``, by a plain top-level ``name = AuthorityReplica(...)``
    statement above ``call``. Anything else (None, a conditional, ``or``, another function, a parameter, a second binding, a
    binding under an ``if``/``try``/``with``) is the replica-optional mode the design names as the one reopening change."""
    a = builder.args
    params = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs, *filter(None, (a.vararg, a.kwarg)))}
    if name in params:
        return [f"{BUILDER}'s `replica={name}` is a parameter, not built here"]
    if any(isinstance(n, (ast.Nonlocal, ast.Global)) and name in n.names for n in ast.walk(builder)):
        return [f"{BUILDER}: `{name}` is declared nonlocal/global, so it can be rebound out of sight"]
    stores = [n for n in _own_nodes(builder) if isinstance(n, ast.Name) and n.id == name
              and isinstance(n.ctx, (ast.Store, ast.Del))]
    if len(stores) != 1:
        return [f"{BUILDER} binds `{name}` {len(stores)} times, expected exactly 1"]
    stmt = next((s for s in builder.body if isinstance(s, ast.Assign) and len(s.targets) == 1
                 and isinstance(s.targets[0], ast.Name) and s.targets[0].id == name), None)
    if stmt is None:
        return [f"{BUILDER}'s one binding of `{name}` is not a plain top-level `{name} = ...` statement"]
    if not (isinstance(stmt.value, ast.Call) and _callee(stmt.value.func) == REPLICA_CLASS):
        return [f"{BUILDER} binds `{name}` to `{ast.unparse(stmt.value)}`, not a direct {REPLICA_CLASS}(...) call"]
    if stmt.lineno >= call.lineno:
        return [f"{BUILDER} binds `{name}` at line {stmt.lineno}, not above the RunServer( call at line {call.lineno}"]
    return []


def replica_violations(source: str) -> list[str]:
    """Pin 2 over the source of ``SERVER_FILE``: the sites inside ``build_server`` must be exactly one call, passing one
    ``replica=`` keyword whose value is a direct ``AuthorityReplica(...)`` call or a name bound once to one (see
    :func:`_origin_violations`). An empty list is green."""
    sites = [call for fn, call in runserver_sites(source)[0] if fn == BUILDER]
    if len(sites) != 1:
        return [f"{BUILDER} holds {len(sites)} RunServer( calls, expected exactly 1"]
    kws = [k for k in sites[0].keywords if k.arg == "replica"]
    if len(kws) != 1:
        return [f"{BUILDER}'s RunServer( call has no single `replica=` keyword ({len(kws)} found)"]
    value = kws[0].value
    if isinstance(value, ast.Call) and _callee(value.func) == REPLICA_CLASS:
        return []
    if not isinstance(value, ast.Name):
        return [f"{BUILDER}'s RunServer( call passes replica={ast.unparse(value)}, expected a name or {REPLICA_CLASS}(...)"]
    builders = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == BUILDER]
    if len(builders) != 1:
        return [f"{BUILDER} is not exactly one module-level function ({len(builders)} found)"]
    return _origin_violations(builders[0], value.id, sites[0])


_GOOD = '''
class RunServer:
    def __init__(self, launcher, *, replica=None):
        assert isinstance(self, RunServer)   # a bare name is not a call
def build_server(host):
    replica = AuthorityReplica(host)
    return RunServer(launcher, verify=v, replica=replica, uploads=u)
'''


def test_the_scanner_is_green_on_the_shape_the_ruling_assumes() -> None:
    assert constructor_violations({SERVER_FILE: _GOOD}) == []
    assert replica_violations(_GOOD) == []


def test_an_inline_authority_replica_call_is_also_green() -> None:
    inline = _GOOD.replace("    replica = AuthorityReplica(host)\n", "").replace("replica=replica", "replica=AuthorityReplica(host)")
    assert inline != _GOOD
    assert replica_violations(inline) == []


@pytest.mark.parametrize("name, rel, source", [
    ("a second constructor in the same file", SERVER_FILE,
     _GOOD + "\ndef build_other(host):\n    return RunServer(launcher, verify=v)\n"),
    ("a constructor in the RunServer class body (a classmethod)", SERVER_FILE,
     _GOOD.replace("def build_server", "    @classmethod\n    def unreplicated(cls, l):\n        return RunServer(l, replica=None)\ndef build_server")),
    ("a second constructor in another module", "levain/other.py",
     "from levain.slot.services.dispatch_run import RunServer\ndef make():\n    return RunServer(l)\n"),
    ("a module-scope constructor", "levain/other.py", "import m\nSERVER = m.RunServer(l)\n"),
    ("a constructor in another file with the builder's name", "levain/other.py",
     "def build_server():\n    return RunServer(l, replica=r)\n"),
    ("an aliased import", "levain/other.py", "from levain.slot.services.dispatch_run import RunServer as RS\n"),
    ("a module-level alias assignment", "levain/other.py",
     "from levain.slot.services.dispatch_run import RunServer\nctor = RunServer\ndef make():\n    return ctor(l)\n"),
    ("a function-level alias assignment", "levain/other.py",
     "from levain.slot.services.dispatch_run import RunServer\ndef make():\n    ctor = RunServer\n    return ctor(l)\n"),
    ("an attribute alias assignment", "levain/other.py", "import m\nctor = m.RunServer\n"),
    ("an annotated alias assignment", "levain/other.py", "from m import RunServer\nctor: type = RunServer\n"),
    ("an alias assignment in the server file itself", SERVER_FILE, _GOOD + "\nctor = RunServer\n"),
    ("a subclass", "levain/other.py",
     "from levain.slot.services.dispatch_run import RunServer\nclass Alt(RunServer):\n    pass\n"),
    ("a subclass by attribute", "levain/other.py", "import m\nclass Alt(m.RunServer):\n    pass\n"),
    # Deliberate: only a call whose INNERMOST function is build_server counts, so a nested helper is red (fails closed).
    ("a nested helper inside build_server calling RunServer", SERVER_FILE,
     _GOOD.replace("    return RunServer(launcher, verify=v, replica=replica, uploads=u)",
                   "    def _mk():\n        return RunServer(launcher, verify=v, replica=replica, uploads=u)\n    return _mk()")),
])
def test_mutation_a_second_constructor_is_red(name: str, rel: str, source: str) -> None:
    """Mutation (a): any RunServer( call outside build_server (the class body and a nested helper included), and any alias
    assignment, aliased import or subclass that would hide one, fails pin 1."""
    assert source != _GOOD
    bad = constructor_violations({SERVER_FILE: _GOOD, rel: source} if rel != SERVER_FILE else {rel: source})
    assert bad, f"{name} was NOT caught. " + WHY


def test_mutation_replica_none_is_red() -> None:
    """Mutation (b): `replica=replica` -> `replica=None` fails pin 2."""
    mutated = _GOOD.replace("replica=replica", "replica=None")
    assert mutated != _GOOD
    assert replica_violations(mutated), "replica=None was NOT caught. " + WHY


@pytest.mark.parametrize("old, new", [
    ("replica=replica, ", ""),                       # the keyword dropped
    # a duplicate keyword is also not one good keyword. NOTE for the next reader: this source parses. CPython rejects a
    # repeated keyword at the compile stage, not in `ast.parse`, so the scanner sees both `replica` keywords (reviewers
    # have raised "invalid Python, errors out" here and it is refuted by running this test).
    ("RunServer(launcher,", "RunServer(launcher, replica=None,"),
])
def test_mutation_a_missing_or_doubled_replica_keyword_is_red(old: str, new: str) -> None:
    mutated = _GOOD.replace(old, new)
    assert mutated != _GOOD
    assert replica_violations(mutated), "a missing/doubled replica= was NOT caught. " + WHY


_BIND = "    replica = AuthorityReplica(host)\n"


@pytest.mark.parametrize("name, mutated", [
    ("replica = None", _GOOD.replace(_BIND, "    replica = None\n")),
    ("a conditional that can be None", _GOOD.replace(_BIND, "    replica = AuthorityReplica(host) if host else None\n")),
    ("an `or` that can be None", _GOOD.replace(_BIND, "    replica = AuthorityReplica(host) or None\n")),
    ("a second assignment", _GOOD.replace(_BIND, _BIND + "    replica = None\n")),
    ("another function's result", _GOOD.replace(_BIND, "    replica = other_fn(host)\n")),
    ("a binding under an `if`", _GOOD.replace(_BIND, "    if host:\n        replica = AuthorityReplica(host)\n")),
    ("a tuple-unpack binding", _GOOD.replace(_BIND, "    replica, extra = AuthorityReplica(host), 1\n")),
    ("a walrus binding", _GOOD.replace(_BIND, "    (replica := AuthorityReplica(host))\n")),
    ("a parameter", _GOOD.replace(_BIND, "").replace("def build_server(host)", "def build_server(host, replica)")),
    ("a nonlocal rebind", _GOOD.replace("    return RunServer", "    def _later():\n        nonlocal replica\n        replica = None\n    return RunServer")),
    ("a binding after the call", _GOOD.replace(_BIND, "").replace("    return RunServer(launcher, verify=v, replica=replica, uploads=u)",
                                                                 "    server = RunServer(launcher, verify=v, replica=replica, uploads=u)\n" + _BIND + "    return server")),
    ("an inline non-replica expression", _GOOD.replace(_BIND, "").replace("replica=replica", "replica=cfg.get('replica')")),
])
def test_mutation_replica_origin_is_red(name: str, mutated: str) -> None:
    """Mutation (c): `replica=` must be built here, once, by a direct AuthorityReplica( call. Each of these keeps a
    not-None-literal `replica=` keyword (the old check's whole test) and is still the replica-optional mode."""
    assert mutated != _GOOD
    assert replica_violations(mutated), f"{name} was NOT caught. " + WHY


def test_mutation_a_nested_helper_leaves_build_server_with_no_call() -> None:
    """The nested-helper case through pin 2: the call is the helper's, so build_server holds none (see the module docstring)."""
    nested = _GOOD.replace("    return RunServer(launcher, verify=v, replica=replica, uploads=u)",
                           "    def _mk():\n        return RunServer(launcher, verify=v, replica=replica, uploads=u)\n    return _mk()")
    assert nested != _GOOD
    assert replica_violations(nested) == [f"{BUILDER} holds 0 RunServer( calls, expected exactly 1"]


def test_the_real_tree_has_one_constructor_and_it_passes_a_replica() -> None:
    sources = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in sorted(PKG.rglob("*.py"))}
    assert SERVER_FILE in sources, f"{SERVER_FILE} is gone or renamed; the pin has no subject. " + WHY
    bad = constructor_violations(sources)
    assert not bad, "RunServer is constructed outside build_server: " + "; ".join(bad) + ". " + WHY
    bad = replica_violations(sources[SERVER_FILE])
    assert not bad, "; ".join(bad) + ". " + WHY
