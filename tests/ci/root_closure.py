"""The import closure of the root-run entry modules (``levain.slot.root_tree.ENTRY_MODULES``), derived at run time.

Root runs these under ``ROOT_PYTHON`` (``/usr/bin/python3``, 3.9.6 on macOS), which is older than the CI matrix, so the
``root-py39`` CI job byte-compiles this closure and runs the test files that exercise it under 3.9.

    python tests/ci/root_closure.py modules       # the import-time closure: what the entries execute when imported
    python tests/ci/root_closure.py modules-hop1  # plus one hop of function-local imports (informational)
    python tests/ci/root_closure.py modules-all   # every lazy import followed to a fixpoint (informational)
    python tests/ci/root_closure.py compile       # byte-compile the ``modules`` set (exit 1 on any failure)
    python tests/ci/root_closure.py compile-hop1  # byte-compile (syntax only) the ``modules-hop1`` set
    python tests/ci/root_closure.py import-check  # ``python -c "import m"`` per ``modules`` member, in a subprocess
    python tests/ci/root_closure.py tests         # the tests/test_*.py files that import only ``modules`` members
    python tests/ci/root_closure.py h11-spec      # the pip requirement for the h11 floor ``root_tree.H11_MIN`` states
    python tests/ci/root_closure.py sites         # the dynamic-execution sites found, with the module(s) each runs

Function-local imports are not followed by default: they are where code the root helpers must not reach is kept behind
a call (``levain/slot/render.py`` imports ``levain.host_guard``, which needs 3.11 ``tomllib``, inside a function that
the operator's process runs, never the root helper). Following them pulls in the operator CLI and most of the package.

Dynamic roots are a tripwire, not an inference: an import graph cannot see ``runpy.run_module`` / ``wall.module_argv``
targets, so ``DYNAMIC_SITES`` classifies every such call site in ``levain/`` (found by a text scan of the same two
needles ``git grep -n -e 'run_module(' -e 'module_argv(' -- levain`` uses) and seeds the closure from it. A test
(``tests/test_ci_root_closure.py``) asserts the scan finds exactly the table's keys, so a new site fails until classified.

Stdlib only and 3.9-valid: it must run under the interpreter it checks. The walk is an AST walk over every import in a
module, function-local ones included (a lazy import still runs as root), and importing a submodule runs its parent
packages' ``__init__`` too, so those are members as well.
"""
import ast
import os
import re
import subprocess
import sys
import tempfile
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = "levain"
_WARNED: set = set()

#: Every dynamic-execution site in ``levain/``: ``"<path>::<stripped source line>"`` -> the module(s) it runs. A value is
#: a tuple of dotted names, or a token resolved from source: ``@entries`` (``root_tree.ENTRY_MODULES`` values),
#: ``@services`` (``levain.slot.services.<name>`` per ``units.SERVICES``), ``@front`` (``runtime_helper.FRONT_MODULE``).
#: An empty tuple is a site that runs whatever its caller passed (the executor itself); its callers are rows here.
DYNAMIC_SITES = {
    "levain/slot/root_tree.py::" 'f"runpy.run_module({ENTRY_MODULES[entry]!r}, run_name=\\"__main__\\", alter_sys=True)\\n"': "@entries",
    "levain/slot/service_boot.py::" 'runpy.run_module(f"levain.slot.services.{name}", run_name="__main__", alter_sys=True)': "@services",
    "levain/slot/wall.py::def module_argv(host: str, module: str, *args: str) -> Tuple[str, ...]:": (),
    "levain/slot/wall.py::" '"runpy.run_module(m, run_name=\'__main__\', alter_sys=True)")': (),
    "levain/slot/wall.py::" 'argv = module_argv(host, "levain.slot.forwarder", "--port", str(FORWARDER_PORT), "--unix", str(sock),': ("levain.slot.forwarder",),
    "levain/slot/wall.py::argv = module_argv(host, front_module or \"\", *argv)": "@front",
    "levain/slot/wall.py::" 'return module_argv(host, "levain.slot.wall", "--spec-fd", str(spec_fd))': ("levain.slot.wall",),
    "levain/slot/wall.py::" 'return module_argv("darwin", "levain.slot.wall", "--exec-fd", str(exec_fd), "--", *argv)': ("levain.slot.wall",),
}
_NEEDLES = ("run_module(", "module_argv(")


def dynamic_sites() -> list:
    """The sites a text scan of ``levain/*.py`` finds: ``"<path>::<stripped line>"``, sorted."""
    found = set()
    for path in sorted((ROOT / PKG).rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if any(n in line for n in _NEEDLES):
                found.add(rel + "::" + line.strip())
    return sorted(found)


def _table(rel: str, name: str):
    """A module-level literal assigned to ``name``, read from source: importing the module would need the
    3.9-incompatible code this job exists to find."""
    src = (ROOT / PKG / rel).read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == name:
            return node.value
    raise SystemExit("root_closure: %s not found in levain/%s" % (name, rel))


def _service_names() -> list:
    """The ``name`` of every ``Service(...)`` in ``levain.slot.units.SERVICES`` (first positional or ``name=``)."""
    names = []
    for call in _table("slot/units.py", "SERVICES").elts:
        kw = {k.arg: k.value for k in call.keywords}
        arg = call.args[0] if call.args else kw.get("name")
        names.append(ast.literal_eval(arg))
    return names


def _site_modules(spec) -> list:
    """The modules a ``DYNAMIC_SITES`` value names: a tuple as written, or a token resolved from source."""
    if spec == "@entries":
        return list(ast.literal_eval(_table("slot/root_tree.py", "ENTRY_MODULES")).values())
    if spec == "@services":
        return ["levain.slot.services." + n for n in _service_names()]
    if spec == "@front":
        return [ast.literal_eval(_table("slot/runtime_helper.py", "FRONT_MODULE"))]
    return list(spec)


def _entry_modules() -> list:
    """The roots: every module a dynamic-execution site runs (``DYNAMIC_SITES``: the entry scripts, ``service_boot``'s
    services, the wall's forwarder / stage / trampoline, the front). A module with no file is printed, not dropped."""
    roots = set()
    for spec in DYNAMIC_SITES.values():
        for mod in _site_modules(spec):
            if _path(mod) is None:
                if mod not in _WARNED:
                    _WARNED.add(mod)
                    print("root_closure: no module for %s: handled elsewhere or missing" % mod, file=sys.stderr)
            else:
                roots.add(mod)
    return sorted(roots)


def _path(mod: str):
    base = ROOT.joinpath(*mod.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _nodes(tree, lazy: bool):
    """Every node, or with ``lazy`` False only those that run at import time (not inside a function body)."""
    if lazy:
        yield from ast.walk(tree)
        return
    stack = [tree]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                stack.append(c)


def _imports(mod: str, path: Path, lazy: bool = True) -> set:
    is_pkg = path.name == "__init__.py"
    pkg = mod if is_pkg else mod.rpartition(".")[0]
    found = set()
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # A file this interpreter cannot parse is exactly what the job exists to report (``compile`` fails on it), but
        # its imports still belong to the closure: fall back to a line scan so the walk goes on past it.
        for m in re.finditer(r"^\s*(?:from\s+(levain[\w.]*)\s+import\s+\(?([^)\n]*)|import\s+(levain[\w.]*))", text, re.M):
            if m.group(3):
                found.add(m.group(3))
            else:
                found.add(m.group(1))
                found.update(m.group(1) + "." + n.strip().split(" as ")[0] for n in m.group(2).split(",") if n.strip())
        return found
    for node in _nodes(tree, lazy):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = pkg.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                base = base + "." + node.module if node.module else base
            else:
                base = node.module or ""
            names = [base] + [base + "." + a.name for a in node.names]
        else:
            continue
        for n in names:
            if n == PKG or n.startswith(PKG + "."):
                found.add(n)
    return found


def _eager(roots) -> set:
    seen, todo = set(), list(roots)
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        path = _path(mod)
        if path is None:  # a name imported from a module (``from m import func``), not a module
            continue
        seen.add(mod)
        parts = mod.split(".")
        todo.extend(".".join(parts[:i]) for i in range(1, len(parts)))  # parent packages run their __init__
        todo.extend(_imports(mod, path, lazy=False))
    return seen


def closure(hops=0) -> list:
    """Modules the entries import at import time, then ``hops`` rounds of: the function-local imports of what is
    already in, with their own import-time closure. ``hops=None`` follows every lazy import to a fixpoint (the whole
    AST closure, ~90% of the package, because the lazy edges chain into the operator CLI)."""
    seen = _eager(_entry_modules())
    n = 0
    while hops is None or n < hops:
        lazy = set()
        for mod in seen:
            lazy |= _imports(mod, _path(mod), lazy=True)
        grown = seen | _eager(lazy - seen)
        if grown == seen:
            break
        seen, n = grown, n + 1
    return sorted(seen)


def _levain_imports(tree) -> set:
    """The ``levain`` modules (files that exist) a parsed test file imports."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(node.module + "." + a.name for a in node.names)
    # ``from levain.slot import x`` names a module when levain/slot/x.py exists, else a function: keep modules only
    return {n for n in names if (n == PKG or n.startswith(PKG + ".")) and _path(n) is not None}


def _test_deps(path: Path, seen: set):
    """The ``levain`` modules a test file imports, plus those of every ``tests.*`` helper it imports, recursively;
    None when a file in that chain cannot be parsed by this interpreter (a 3.12-only test is not a root-run test)."""
    if path in seen:
        return set()
    seen.add(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return None
    out = _levain_imports(tree)
    helpers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            helpers.update(a.name for a in node.names if a.name.startswith("tests."))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module and (node.module == "tests" or node.module.startswith("tests.")):
            helpers.add(node.module)
            helpers.update(node.module + "." + a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 1:
            helpers.add("tests" + ("." + node.module if node.module else ""))
            helpers.update("tests." + (node.module + "." if node.module else "") + a.name for a in node.names)
    for h in sorted(helpers):
        hp = ROOT.joinpath(*h.split("."))
        hp = hp.with_suffix(".py") if hp.with_suffix(".py").is_file() else hp / "__init__.py"
        if not hp.is_file():
            continue  # ``from tests.x import name`` names a function, not a module
        sub = _test_deps(hp, seen)
        if sub is None:
            return None
        out |= sub
    return out


def _test_files(mods: list) -> list:
    """The test files that import a closure module and import NOTHING of ``levain`` outside the closure (a ``levain``
    package import aside), counting what the ``tests.*`` helpers they import (recursively) import too: the files
    wholly about root-run code. A file that also reaches operator-side code (the CLI, the cockpit, ``tomllib`` users),
    directly or through a helper, is 3.11+ by design and its 3.9 red says nothing about root, so it is left to the
    3.12/3.13 matrix. A file (or helper) this interpreter cannot parse is skipped for the same reason."""
    inside = set(mods)
    leaf = [m for m in mods if _path(m).name != "__init__.py"]  # a package import says nothing: every test has one
    out = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        names = _test_deps(f, set())
        if names is not None and names & set(leaf) and names <= inside:
            out.append(f.relative_to(ROOT).as_posix())
    return out


def _compile(mods: list, label: str) -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, mod in enumerate(mods):
            try:
                py_compile.compile(str(_path(mod)), cfile=str(Path(tmp) / (str(i) + ".pyc")), doraise=True)
            except py_compile.PyCompileError as e:
                bad += 1
                print("FAIL " + mod + ": " + e.msg.strip().splitlines()[-1])
    print("compiled %d %s modules under %s, %d failed" % (len(mods), label, sys.version.split()[0], bad))
    return 1 if bad else 0


def _import_check(mods: list) -> int:
    """``python -c "import m"`` for each module, one subprocess each: ``py_compile`` is syntax only and misses what
    fails at import (an unguarded ``X | None`` annotation, a 3.10+ stdlib name)."""
    bad = 0
    for mod in mods:
        r = subprocess.run([sys.executable, "-c", "import " + mod], cwd=str(ROOT), env=dict(os.environ, PYTHONPATH=str(ROOT)),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if r.returncode:
            bad += 1
            print("FAIL " + mod + ": " + (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1])
    print("imported %d import-time modules under %s, %d failed" % (len(mods), sys.version.split()[0], bad))
    return 1 if bad else 0


def _h11_spec() -> str:
    return "h11>=" + ".".join(str(n) for n in ast.literal_eval(_table("slot/root_tree.py", "H11_MIN")))


def main(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "modules"
    mods = closure(None if cmd == "modules-all" else 1 if cmd in ("modules-hop1", "compile-hop1") else 0)
    if cmd in ("modules", "modules-hop1", "modules-all"):
        print("\n".join(mods))
    elif cmd == "compile":
        return _compile(mods, "import-time")
    elif cmd == "compile-hop1":
        # Syntax only, never import: the import-time closure plus one hop of function-local imports (code a root
        # process can import at run time). Deeper hops chain into the operator CLI, which is 3.11+ by design.
        return _compile(mods, "hop1")
    elif cmd == "import-check":
        return _import_check(closure(0))
    elif cmd == "tests":
        files = _test_files(mods)
        sys.stdout.write("".join(f + "\n" for f in files))  # nothing at all when empty: no blank line for mapfile
    elif cmd == "h11-spec":
        print(_h11_spec())
    elif cmd == "sites":
        for key, spec in sorted(DYNAMIC_SITES.items()):
            print("%s -> %s" % (key, ", ".join(_site_modules(spec)) or "(caller's module)"))
    else:
        raise SystemExit("usage: root_closure.py modules|modules-hop1|modules-all|compile|compile-hop1|import-check|tests|h11-spec|sites")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
