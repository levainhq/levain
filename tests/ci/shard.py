"""Split the pytest suite into N shards for CI, from pytest's own collection.

    python tests/ci/shard.py shards --shards N
    pytest --collect-only -q > nodes.txt
    python tests/ci/shard.py pick --shards N --shard K < nodes.txt > files.txt

`shards` writes `shards=[0, .., N-1]` (the matrix list) to the file named by $GITHUB_OUTPUT, or to
stdout when it is unset. `pick` reads node ids on stdin, the output of `pytest --collect-only -q` run
in the SAME job and Python as the tests (so the repo's real config, under that interpreter, decides
what is collected), partitions the unique files into N shards, and prints shard K's files, one per
line. It exits non-zero when no node ids were read, when the shards do not hold the collected files
exactly once, when any shard is empty, or when K is not in 0..N-1. Every shard job plans the same
partition from its own collection, so the shards of one Python cover what that Python collects.

The weight of a file is its count of `def test_` lines (async included), at least 1; a file
that is not on disk weighs 1. Assignment is greedy: heaviest file first, onto the lightest
shard, ties broken by shard index and file name, so the result is deterministic. Weight is a
proxy for runtime: read the per-shard durations in CI before changing TEST_SHARDS.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def test_", re.MULTILINE)
# `pytest --collect-only -q` prints each collected item as `<path>.py::<name>[<params>]`. A node id is a line holding
# `.py::`; its file is everything up to the first `.py::`, so a path that itself holds `::` keeps it whole. Every other
# line (the summary, a warning "tests/x.py:3: Warning", "ERROR collecting tests/x.py") holds no `.py::` and is ignored;
# a collection error also fails the shard job's step (bash -e).


def files_from_node_ids(text):
    files = set()
    for line in text.splitlines():
        head, sep, _ = line.rstrip().partition(".py::")
        if sep and head:
            files.add(head + ".py")
    return sorted(files)


def weight(rel_path, root=ROOT):
    try:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    return max(1, len(_TEST_DEF.findall(text)))


def partition(files, n, weigh):
    if n < 1:
        raise ValueError("N must be at least 1")
    shards = [[] for _ in range(n)]
    loads = [0] * n
    for f in sorted(files, key=lambda f: (-weigh(f), f)):
        i = min(range(n), key=lambda i: (loads[i], i))
        shards[i].append(f)
        loads[i] += weigh(f)
    return [sorted(s) for s in shards], loads


def check(shards, files):
    """Raise AssertionError unless the shards hold every file exactly once and none is empty."""
    flat = [f for s in shards for f in s]
    dup = sorted({f for f in flat if flat.count(f) > 1})
    missing = sorted(set(files) - set(flat))
    extra = sorted(set(flat) - set(files))
    empty = [i for i, s in enumerate(shards) if not s]
    assert not (dup or missing or extra or empty), (
        f"shard plan is wrong: duplicated={dup} missing={missing} extra={extra} empty_shards={empty}"
    )


def plan(text, n, weigh=weight):
    files = files_from_node_ids(text)
    assert files, "no test node ids on stdin: refusing to plan"
    shards, _ = partition(files, n, weigh)
    check(shards, files)
    return shards


def _write_output(line, environ):
    target = environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    else:
        print(line)


def _count(value):
    n = int(value)
    if n < 1:
        raise ValueError("N must be at least 1")
    return n


def main(argv, stdin=None, environ=None):
    environ = os.environ if environ is None else environ
    if len(argv) == 4 and argv[1:3] == ["shards", "--shards"]:
        try:
            n = _count(argv[3])
        except ValueError as e:
            print(f"shard.py shards: {e}", file=sys.stderr)
            return 1
        _write_output("shards=" + json.dumps(list(range(n))), environ)
        return 0
    if len(argv) == 6 and argv[1] == "pick" and argv[2] == "--shards" and argv[4] == "--shard":
        try:
            n, k = _count(argv[3]), int(argv[5])
            if not 0 <= k < n:
                raise ValueError(f"shard {k} is not in 0..{n - 1}")
            shards = plan((stdin or sys.stdin).read(), n)
        except (AssertionError, ValueError) as e:
            print(f"shard.py pick: {e}", file=sys.stderr)
            return 1
        print("\n".join(shards[k]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
