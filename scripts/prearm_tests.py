#!/usr/bin/env python3
"""Pre-arm affected-tests filter: run it BEFORE arming a landing link.

It picks the test files that import or name the files changed in the link (a deterministic file-map built
with `ast` at run time, no model, no data file) and runs only those: serial, under `nice`, no xdist (the box is shared).

Three limits, stated up front:
  (a) The map is import-based. A change reached only through a data file, a subprocess or a dynamic import selects
      nothing: the script then says "no test imports this file", and a levain/ .py file it cannot map is printed as
      UNMAPPED. It never silently passes. Re-exports are followed to depth 2 only (a changed levain/a.py also selects
      tests that import a levain module that imports a).
  (b) One real link before its arm is the discharge: this script has no proof of its own until a link has used it.
  (c) It does NOT replace the full suite. THE LANDING SHA PASSES THE FULL SUITE; this is a cheap early filter.

Selection rules: a changed test file selects itself; a changed levain/ module selects the tests importing it (or naming
its path or dotted name in a string constant); a changed tests/ helper selects the tests importing it; a changed
non-.py file selects the tests naming its repo path. A changed tests/conftest.py or pyproject.toml affects every test:
exit 2 and run nothing. A conftest in a subdirectory selects the tests under that directory.
Exit: pytest's code; 2 = run the full suite instead; 3 = nothing selected (a non-run, never a pass).
"""
import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAIL = "PREARM ONLY: this is a filter, the full suite still runs on the landing SHA."


def git(*args):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return [x for x in r.stdout.splitlines() if x]


def changed_files(base):
    out = git("diff", "--name-only", f"{base}...HEAD") + git("diff", "--name-only", "HEAD") + git(
        "ls-files", "--others", "--exclude-standard")
    return sorted({f for f in out if (ROOT / f).exists() or f.endswith((".py", ".json", ".yml"))})


def dotted(rel):
    p = Path(rel).with_suffix("")
    parts = list(p.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def scan(path):
    """Return (imported dotted names, string constants) of one .py file."""
    rel = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set(), []
    pkg = dotted(rel).split(".")
    if path.name != "__init__.py":
        pkg = pkg[:-1]
    names, strings = set(), []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add(a.name)
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                head = pkg[: len(pkg) - (n.level - 1)] if n.level > 1 else pkg
                base = ".".join(head + ([n.module] if n.module else []))
            names.add(base)
            for a in n.names:
                names.add(f"{base}.{a.name}")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            strings.append(n.value)
    return names, strings


def aliases(rel):
    d = dotted(rel)
    return {d, d.removeprefix("tests.")} if rel.startswith("tests/") else {d}


def named_in(strings, rel):
    pat = re.compile(r"(?<![\w.])" + re.escape(dotted(rel)) + r"(?!\w)")
    return any(rel in s or (rel.endswith(".py") and pat.search(s)) for s in strings)


def main():
    ap = argparse.ArgumentParser(description=(
        "Choose and run the tests that import or name the files changed in a landing link (file-map, serial, nice, "
        "no xdist). A FILTER only: the full suite still runs on the landing SHA and this never replaces it."))
    ap.add_argument("--base", default="origin/main", help="diff base (default origin/main)")
    ap.add_argument("--files", nargs="+", help="repo-relative changed files; overrides git entirely")
    ap.add_argument("--list", action="store_true", help="print the selection and stop")
    ap.add_argument("--invariant", action="store_true", help="also run `-m invariant` (CI stage0 runs them; off by default)")
    ap.add_argument("--max-files", type=int, default=60, help="above this many chosen files exit 2: run the full suite")
    ap.add_argument("--basetemp", help="pytest basetemp parent (default: a fresh temp dir, removed after)")
    a = ap.parse_args()

    changed = sorted(set(a.files)) if a.files else changed_files(a.base)
    print(f"changed files ({len(changed)}):")
    for f in changed:
        print(f"  {f}")
    if "pyproject.toml" in changed or "tests/conftest.py" in changed:
        print("this change affects every test: run the full suite (pyproject.toml / tests/conftest.py changed)")
        return 2

    tests = sorted(p for p in (ROOT / "tests").rglob("*.py") if p.name.startswith("test_") and "__pycache__" not in p.parts)
    info = {p.relative_to(ROOT).as_posix(): scan(p) for p in tests}
    lev = {p.relative_to(ROOT).as_posix(): scan(p) for p in (ROOT / "levain").rglob("*.py")}
    chosen, notes = {}, []

    def pick(t, why):
        chosen.setdefault(t, []).append(why)

    for f in changed:
        hit = False
        if f in info:
            pick(f, "changed itself")
            hit = True
        elif f.startswith("tests/") and f.endswith("conftest.py"):
            d = f.rsplit("/", 1)[0] + "/"
            for t in info:
                if t.startswith(d):
                    pick(t, f"under changed {f}")
                    hit = True
        if f.endswith(".py") and f not in info and not f.endswith("conftest.py"):
            want = aliases(f)
            reach = {m: f"imports {m}" for m in want}  # module name -> reason text
            if f.startswith("levain/"):  # depth 2: levain modules importing the changed one
                for lf, (names, _) in lev.items():
                    if lf != f and want & names:
                        reach[dotted(lf)] = f"imports {dotted(lf)} (re-exports {dotted(f)}, depth 2)"
            for t, (names, strings) in info.items():
                for m, why in reach.items():
                    if m in names:
                        pick(t, why)
                        hit = True
                if named_in(strings, f):
                    pick(t, f"names path {f}")
                    hit = True
        elif not f.endswith("conftest.py") and f not in info:
            for t, (_, strings) in info.items():
                if named_in(strings, f):
                    pick(t, f"names path {f}")
                    hit = True
        if not hit:
            if f.startswith("levain/") and f.endswith(".py"):
                notes.append(f"UNMAPPED: {f} (a levain module no test imports or names; run its tests by hand)")
            else:
                notes.append(f"no test imports this file: {f}")

    print(f"chosen test files ({len(chosen)}):")
    for t in sorted(chosen):
        print(f"  {t}  [{'; '.join(dict.fromkeys(chosen[t]))}]")
    for n in notes:
        print(n)
    print("note: re-export walk is capped at depth 2; the map is import-based (data files, subprocesses, dynamic imports are invisible)")
    if len(chosen) > a.max_files:
        print(f"{len(chosen)} files chosen, over --max-files {a.max_files}: run the full suite instead of a partial one")
        return 2
    if a.list:
        return 0
    if not chosen:
        print("nothing selected: this is NOT a pass. " + TAIL)
        return 3

    addopts = re.search(r'addopts\s*=\s*"([^"]*)"', (ROOT / "pyproject.toml").read_text())
    xdist = ["-p", "no:xdist"] if addopts and re.search(r"(^|\s)(-n|--numprocesses|--dist)", addopts.group(1)) else []
    tmp = tempfile.mkdtemp(prefix="prearm-", dir=a.basetemp)
    runs = [sorted(chosen)] + ([["-m", "invariant", "tests"]] if a.invariant else [])
    rc, counts = 0, {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    try:
        for i, targets in enumerate(runs):
            cmd = ["nice", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *xdist,
                   f"--basetemp={tmp}/r{i}", *targets]
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            last = ""
            for line in p.stdout:
                print(line, end="")
                if re.search(r"\d+ (passed|failed|skipped|error)", line):
                    last = line
            p.wait()
            rc = rc or p.returncode
            for n, w in re.findall(r"(\d+) (passed|failed|skipped|error)", last):
                counts[w] += int(n)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    c = counts
    skip = f" (skipped tests did not run, e.g. needs_browser without Chrome: not a pass)" if c["skipped"] else ""
    print(f"{TAIL} passed={c['passed']} failed={c['failed']} errors={c['error']} skipped={c['skipped']}{skip}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
