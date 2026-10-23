"""Split the pytest suite into N shards for CI, computed from the files on disk.

    python tests/ci/shard.py N K        print the test files of shard K (0-based), one per line
    python tests/ci/shard.py --check N  fail unless the N shards together hold every collected file exactly once
    python tests/ci/shard.py --weights N  print each shard's file count and weight

The file set is what pytest collects under `testpaths` with the default `python_files`
(`test_*.py`, recursive). The weight of a file is its count of `def test_` lines (async
included), at least 1. Assignment is greedy: heaviest file first, onto the lightest shard,
ties broken by shard index and file name, so the result is deterministic. A new test file
lands in a shard with no workflow edit.

Weight is a proxy for runtime. Parametrized tests and slow fixtures make it imperfect;
read the per-shard durations in CI before changing N.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def test_", re.MULTILINE)


def collected_files(tests_dir=TESTS):
    return sorted(
        p.relative_to(tests_dir.parent).as_posix() for p in tests_dir.rglob("test_*.py")
    )


def weight(rel_path, root=ROOT):
    text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
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
    """Raise AssertionError unless the shards hold every file exactly once."""
    flat = [f for s in shards for f in s]
    dup = sorted({f for f in flat if flat.count(f) > 1})
    missing = sorted(set(files) - set(flat))
    extra = sorted(set(flat) - set(files))
    assert not (dup or missing or extra), (
        f"shard partition is wrong: duplicated={dup} missing={missing} extra={extra}"
    )


def main(argv):
    if len(argv) == 3 and argv[1] in ("--check", "--weights"):
        n = int(argv[2])
        files = collected_files()
        assert files, "no test files collected"
        shards, loads = partition(files, n, weight)
        check(shards, files)
        if argv[1] == "--weights":
            for i, (s, w) in enumerate(zip(shards, loads)):
                print(f"shard {i}: {len(s)} files, weight {w}")
        else:
            print(f"ok: {len(files)} files in {n} shards, each exactly once")
        return 0
    if len(argv) == 3 and not argv[1].startswith("-"):
        n, k = int(argv[1]), int(argv[2])
        if not 0 <= k < n:
            print(f"K must be in 0..{n - 1}", file=sys.stderr)
            return 2
        files = collected_files()
        shards, _ = partition(files, n, weight)
        check(shards, files)
        print("\n".join(shards[k]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
