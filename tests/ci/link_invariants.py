"""Run the fixed link invariants: the one command every link tip of a stacked landing runs, unchanged.

HOW A STACK IS CUT (no repo doc owns this, so it lives here): at EVERY link tip, before the link is pushed or verified,
run

    python3 tests/ci/link_invariants.py

with no arguments and no subset. The cutter does not choose which invariants apply: the list is tests/ci/LINK_INVARIANTS
(checked in, one test path per line), and the runner runs, in order, ``pytest <the listed files>`` and then
``pytest -m invariant -q`` (the whole-tree marker tier). Every land-CI red of this class came from the one invariant a
hand-picked subset left out. Exit status is non-zero on any failure; one summary line per step prints the counts.

    --print      list what would run and run nothing
    --all-root   ALSO run the full root closure once (the root-py39 job's test files, ``root_closure.py tests``, about two
                 hundred files, slow): for the TAIL tip of the stack only, not every link

Stdlib only, Python 3.9 clean, runs pytest under the interpreter that runs this file.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIST = ROOT / "tests" / "ci" / "LINK_INVARIANTS"
COUNT = re.compile(r"(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed|deselected)")


def listed():
    """The test paths of LINK_INVARIANTS, in file order (comments and blank lines dropped)."""
    out = []
    for line in LIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def root_closure_files():
    res = subprocess.run([sys.executable, str(ROOT / "tests" / "ci" / "root_closure.py"), "tests"],
                         cwd=str(ROOT), stdout=subprocess.PIPE, universal_newlines=True)
    if res.returncode != 0:
        raise SystemExit("root_closure.py tests failed (exit %d)" % res.returncode)
    return [x for x in res.stdout.split() if x]


def steps(all_root=False):
    """(label, pytest argv tail) per step, in run order."""
    out = [("listed files", listed()), ("-m invariant", ["-m", "invariant", "-q"])]
    if all_root:
        out.append(("root closure", ["--noconftest", "-q", "-p", "no:cacheprovider"] + root_closure_files()))
    return out


def run_step(label, tail):
    cmd = [sys.executable, "-m", "pytest", "-q"] + tail if label == "listed files" else [sys.executable, "-m", "pytest"] + tail
    res = subprocess.run(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    sys.stdout.write(res.stdout if res.returncode else "")
    counts = {}
    for n, word in COUNT.findall(res.stdout.splitlines()[-1] if res.stdout.strip() else ""):
        counts[word] = int(n)
    shown = " ".join("%d %s" % (n, w) for w, n in counts.items()) or "no count read"
    print("%s: %s (%s)" % (label, "PASS" if res.returncode == 0 else "FAIL", shown))
    return res.returncode


def main(argv):
    args = set(argv)
    if args - {"--print", "--all-root"}:
        print("usage: link_invariants.py [--print] [--all-root]", file=sys.stderr)
        return 2
    plan = steps("--all-root" in args)
    if "--print" in args:
        for label, tail in plan:
            print("%s: pytest %s" % (label, " ".join(tail)))
        return 0
    rc = 0
    for label, tail in plan:
        rc = run_step(label, tail) or rc
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
