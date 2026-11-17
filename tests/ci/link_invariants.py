"""Run the fixed link invariants: the one command every link tip of a stacked landing runs, unchanged.

HOW A STACK IS CUT (no repo doc owns this, so it lives here): at EVERY link tip, before the link is pushed or verified,
run

    python3 tests/ci/link_invariants.py

with no arguments and no subset. The cutter does not choose which invariants apply: the list is tests/ci/LINK_INVARIANTS
(checked in, one test path per line), and the runner runs, in order, ``pytest <the listed files>`` and then
``pytest -m invariant -q`` (the whole-tree marker tier). Every land-CI red of this class came from the one invariant a
hand-picked subset left out. Exit status is non-zero on any failure; one summary line per step prints the counts
(skipped is always printed: a skip-gutted invariant is visible, not failing).

    --print      list what would run and run nothing (not even the closure derivation)
    --all-root   ALSO run the full root closure once (the root-py39 job's test files, ``root_closure.py tests``, about two
                 hundred files, slow): for the TAIL tip of the stack only, not every link. The closure is derived last; an
                 empty or failed derivation fails the run, it never falls through to pytest's testpaths.

Stdlib only, Python 3.9 clean, runs pytest under the interpreter that runs this file.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIST = ROOT / "tests" / "ci" / "LINK_INVARIANTS"
COUNT = re.compile(r"(\d+) (passed|failed|skipped|errors|error|xfailed|xpassed|deselected)\b")
TIMEOUT = 1800
CLOSURE_TIMEOUT = 3600


def listed():
    """The test paths of LINK_INVARIANTS, in file order (comments and blank lines dropped)."""
    out = []
    for line in LIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def root_closure_files():
    """The root closure's test files; an empty or failed derivation raises, never returns [] (pytest with no files
    falls back to testpaths and runs the whole tree)."""
    try:
        res = subprocess.run([sys.executable, str(ROOT / "tests" / "ci" / "root_closure.py"), "tests"],
                             cwd=str(ROOT), stdout=subprocess.PIPE, universal_newlines=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("root_closure.py tests timed out")
    if res.returncode != 0:
        raise RuntimeError("root_closure.py tests failed (exit %d)" % res.returncode)
    files = [x for x in res.stdout.split() if x]
    if not files:
        raise RuntimeError("root_closure.py tests derived no files")
    return files


def run_step(label, tail, timeout=TIMEOUT):
    cmd = [sys.executable, "-m", "pytest"] + (["-q"] if label == "listed files" else []) + tail
    try:
        res = subprocess.run(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("%s: FAIL (timed out after %ds)" % (label, timeout))
        return 1
    sys.stdout.write(res.stdout if res.returncode else "")
    counts = {}
    for n, word in COUNT.findall(res.stdout.splitlines()[-1] if res.stdout.strip() else ""):
        counts[word] = int(n)
    counts.setdefault("skipped", 0)
    shown = " ".join("%d %s" % (n, w) for w, n in counts.items())
    print("%s: %s (%s)" % (label, "PASS" if res.returncode == 0 else "FAIL", shown))
    return res.returncode


def main(argv):
    args = set(argv)
    if args - {"--print", "--all-root"}:
        print("usage: link_invariants.py [--print] [--all-root]", file=sys.stderr)
        return 2
    all_root = "--all-root" in args
    plan = [("listed files", listed()), ("-m invariant", ["-m", "invariant", "-q"])]
    if "--print" in args:
        for label, tail in plan:
            print("%s: pytest %s" % (label, " ".join(tail)))
        if all_root:
            print("root closure: derived when run")
        return 0
    rc = 0
    for label, tail in plan:
        rc = run_step(label, tail) or rc
    if all_root:  # last and lazily: a derivation crash must not skip the steps above
        try:
            files = root_closure_files()
        except RuntimeError as e:
            print("root closure: FAIL (%s)" % e)
            return 1
        rc = run_step("root closure", ["--noconftest", "-q", "-p", "no:cacheprovider"] + files, CLOSURE_TIMEOUT) or rc
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
