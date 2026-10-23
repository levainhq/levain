"""Split the pytest suite into N shards for CI, from pytest's own collection.

    pytest --collect-only -q | python tests/ci/shard.py plan --shards N
    SHARD_FILES='["tests/test_a.py", ...]' python tests/ci/shard.py files

`plan` reads node ids on stdin (the output of `pytest --collect-only -q`, so the repo's real
config decides what is collected), takes the unique file paths, partitions them into N shards
and writes two outputs: `shards` (JSON list [0..N-1]) and `files` (JSON list of lists, one per
shard). Outputs go to the file named by $GITHUB_OUTPUT, or to stdout when it is unset. It exits
non-zero when no node ids were read, when the shards do not hold the collected files exactly
once, or when any shard is empty (N larger than the file count).

`files` prints the paths in the JSON list in $SHARD_FILES, one per line, for `mapfile -t`.
It exits non-zero on anything but a non-empty list of strings.

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
# A node id is `<path>.py` or `<path>.py::<name>[<params>]`; the path holds no colon. Summary and
# warning lines ("12 tests collected in 1s", "tests/x.py:3: Warning") do not match.
# pytest's own status words ("ERROR collecting tests/x.py", "FAILED ...") lead with an uppercase word and a space.
_NODE = re.compile(r"^(?![A-Z]+ )([^\s:][^:]*\.py)(?:::.*)?$")


def files_from_node_ids(text):
    return sorted({m.group(1) for line in text.splitlines() if (m := _NODE.match(line.rstrip()))})


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


def _emit(shards):
    lines = [
        "shards=" + json.dumps(list(range(len(shards)))),
        "files=" + json.dumps(shards, separators=(",", ":")),
    ]
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def main(argv, stdin=None, environ=None):
    environ = os.environ if environ is None else environ
    if len(argv) == 4 and argv[1] == "plan" and argv[2] == "--shards":
        try:
            shards = plan((stdin or sys.stdin).read(), int(argv[3]))
        except (AssertionError, ValueError) as e:
            print(f"shard.py plan: {e}", file=sys.stderr)
            return 1
        _emit(shards)
        return 0
    if len(argv) == 2 and argv[1] == "files":
        try:
            paths = json.loads(environ.get("SHARD_FILES", ""))
        except json.JSONDecodeError as e:
            print(f"shard.py files: SHARD_FILES is not JSON: {e}", file=sys.stderr)
            return 1
        if not (isinstance(paths, list) and paths and all(isinstance(p, str) and p for p in paths)):
            print("shard.py files: SHARD_FILES must be a non-empty JSON list of paths", file=sys.stderr)
            return 1
        print("\n".join(paths))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
