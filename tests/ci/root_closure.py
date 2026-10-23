"""The import closure of the root-run entry modules (``levain.slot.root_tree.ENTRY_MODULES``), derived at run time.

Root runs these under ``ROOT_PYTHON`` (``/usr/bin/python3``, 3.9.6 on macOS), which is older than the CI matrix, so the
``root-py39`` CI job byte-compiles this closure and runs the test files that exercise it under 3.9.

    python tests/ci/root_closure.py modules       # the import-time closure: what the entries execute when imported
    python tests/ci/root_closure.py modules-hop1  # plus one hop of function-local imports (informational)
    python tests/ci/root_closure.py modules-all   # every lazy import followed to a fixpoint (informational)
    python tests/ci/root_closure.py compile       # byte-compile the ``modules`` set (exit 1 on any failure)
    python tests/ci/root_closure.py tests         # the tests/test_*.py files that import a ``modules`` member

Function-local imports are not followed by default: they are where code the root helpers must not reach is kept behind
a call (``levain/slot/render.py`` imports ``levain.host_guard``, which needs 3.11 ``tomllib``, inside a function that
the operator's process runs, never the root helper). Following them pulls in the operator CLI and most of the package.

Stdlib only and 3.9-valid: it must run under the interpreter it checks. The walk is an AST walk over every import in a
module, function-local ones included (a lazy import still runs as root), and importing a submodule runs its parent
packages' ``__init__`` too, so those are members as well.
"""
import ast
import sys
import tempfile
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = "levain"


def _entry_modules() -> list:
    # Read the table from source: importing root_tree would need the 3.9-incompatible code this job exists to find.
    src = (ROOT / PKG / "slot" / "root_tree.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "ENTRY_MODULES":
            return sorted(ast.literal_eval(node.value).values())
    raise SystemExit("root_closure: ENTRY_MODULES not found in levain/slot/root_tree.py")


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


def _test_files(mods: list) -> list:
    """The test files that import a closure module and import NOTHING of ``levain`` outside the closure (a ``levain``
    package import aside): the files wholly about root-run code. A file that also imports operator-side code (the
    CLI, the cockpit, ``tomllib`` users) is 3.11+ by design and its 3.9 red says nothing about root, so it is left to
    the 3.12/3.13 matrix."""
    inside = set(mods)
    leaf = [m for m in mods if _path(m).name != "__init__.py"]  # a package import says nothing: every test has one
    out = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        names = set()
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"), filename=str(f))):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
                names.update(node.module + "." + a.name for a in node.names)
        names = {n for n in names if n == PKG or n.startswith(PKG + ".")}
        # ``from levain.slot import x`` names a module when levain/slot/x.py exists, else a function: keep modules only
        names = {n for n in names if _path(n) is not None}
        if names & set(leaf) and names <= inside:
            out.append(f.relative_to(ROOT).as_posix())
    return out


def main(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "modules"
    mods = closure(None if cmd == "modules-all" else 1 if cmd == "modules-hop1" else 0)
    if cmd in ("modules", "modules-hop1", "modules-all"):
        print("\n".join(mods))
    elif cmd == "compile":
        bad = 0
        with tempfile.TemporaryDirectory() as tmp:
            for i, mod in enumerate(mods):
                try:
                    py_compile.compile(str(_path(mod)), cfile=str(Path(tmp) / (str(i) + ".pyc")), doraise=True)
                except py_compile.PyCompileError as e:
                    bad += 1
                    print("FAIL " + mod + ": " + e.msg.strip().splitlines()[-1])
        print("compiled %d modules under %s, %d failed" % (len(mods), sys.version.split()[0], bad))
        return 1 if bad else 0
    elif cmd == "tests":
        print("\n".join(_test_files(mods)))
    else:
        raise SystemExit("usage: root_closure.py modules|modules-hop1|modules-all|compile|tests")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
