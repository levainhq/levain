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

The weight of a file is its measured seconds when tests/ci/shard_weights.json holds it, else its count of
`def test_` lines (async included, at least 1; a file not on disk counts 1) times the file's `per_test` seconds, so a
file that was never measured is not free. Without a weights file every weight is the bare count. Assignment is greedy:
heaviest file first, onto the lightest shard, ties broken by shard index and file name, so the result is
deterministic. A malformed weights file fails the plan (a quiet fallback would hide a broken refresh).

Refresh the weights (measured seconds, not guesses): pytest's `-q` progress lines carry a CI timestamp per chunk of
tests, and the node ids in collection order say which file each chunk's tests belong to. From a successful `test`
run on main:
    gh run list --repo levainhq/levain --workflow test --branch main --status success --limit 3
    gh api repos/levainhq/levain/actions/jobs/<pytest job id>/logs > shard<K>.log     (every shard job of one Python)
    python tests/ci/shard.py weights --nodes nodes.txt --shards N --run <run id> --sha <sha> --root <checkout of that sha> \
        --log shard0.log:0 --log shard1.log:1 ...  > tests/ci/shard_weights.json
`nodes.txt` is `pytest --collect-only -q` at that sha, and `N` the TEST_SHARDS the run used (the log:K pairing needs
that run's own plan, which is planned from the files on disk, so `--root` is a checkout of that sha; a run that
already used a weights file needs its plan reproduced, so refresh from a run made before the file changed or note the
limit). Pass several logs of one shard (other Pythons) and
their seconds are averaged. Seconds are smeared over each chunk's tests, so the figure is a balance proxy, not a
profile. Read the per-shard durations in CI before changing TEST_SHARDS.
"""
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEIGHTS = Path(__file__).resolve().parent / "shard_weights.json"
MIN_SECONDS = 0.1
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


def load_weights(path=WEIGHTS):
    """(seconds by file, seconds per un-measured test). No file -> ({}, 1.0): the bare test count."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, 1.0
    data = json.loads(text)
    files, per_test = data["files"], float(data["per_test"])
    if not isinstance(files, dict) or per_test <= 0:
        raise ValueError(f"{path}: needs a files object and a positive per_test")
    return {str(k): float(v) for k, v in files.items()}, per_test


def weight(rel_path, root=ROOT, weights=None):
    measured, per_test = load_weights() if weights is None else weights
    if rel_path in measured:
        return max(MIN_SECONDS, measured[rel_path])
    try:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return per_test
    return max(1, len(_TEST_DEF.findall(text))) * per_test


def partition(files, n, weigh):
    if n < 1:
        raise ValueError("N must be at least 1")
    shards = [[] for _ in range(n)]
    loads = [0.0] * n
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


def plan(text, n, weigh=None):
    if weigh is None:
        weights = load_weights()
        weigh = lambda f: weight(f, ROOT, weights)  # noqa: E731
    files = files_from_node_ids(text)
    assert files, "no test node ids on stdin: refusing to plan"
    shards, _ = partition(files, n, weigh)
    check(shards, files)
    return shards


# One `pytest -q` progress line in a GitHub job log: `<timestamp>Z <outcome chars>[   [ 12%]]`. A test that prints splits
# the line, so a bare run of outcome chars counts too.
_PROGRESS = re.compile(r"^(\S+)Z ([.sFExX]+)(?:\s+\[\s*\d+%\])?$")


def _seconds(stamp):
    hh, mm, ss = stamp.split("T")[1].split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def measure(nodes_text, n, logs, run="", sha="", root=ROOT, weights=None):
    """Per-file seconds from CI job logs. `logs` is [(log text, shard index)]. The plan must be the one the run used:
    `root` is a checkout of the run's sha and `weights` the weights it had (None: none, the bare test count)."""
    count = defaultdict(int)
    for line in nodes_text.splitlines():
        head, sep, _ = line.rstrip().partition(".py::")
        if sep and head:
            count[head + ".py"] += 1
    weights = ({}, 1.0) if weights is None else weights
    files_all = files_from_node_ids(nodes_text)
    shards, _ = partition(files_all, n, lambda f: weight(f, root, weights))
    seconds = defaultdict(list)
    for text, k in logs:
        seq = [f for f in shards[k] for _ in range(count[f])]
        lines, chunks, prev = text.splitlines(), [], None
        for i, line in enumerate(lines):
            m = _PROGRESS.match(line.rstrip())
            if not m:
                continue
            if prev is None:
                prev = _seconds(lines[i - 1].split("Z ")[0])
            now = _seconds(m.group(1))
            chunks.append((len(m.group(2)), max(0.0, now - prev)))
            prev = now
        done = sum(c for c, _ in chunks)
        assert done and abs(done - len(seq)) <= 0.02 * len(seq), f"shard {k}: log shows {done} tests, plan has {len(seq)}"
        got, scale, cum = defaultdict(float), len(seq) / done, 0
        for c, dt in chunks:
            a, b = round(cum * scale), round((cum + c) * scale)
            cum += c
            for f in seq[a:b]:
                got[f] += dt / max(1, b - a)
        for f, v in got.items():
            seconds[f].append(v)
    files = {f: round(sum(v) / len(v), 1) for f, v in sorted(seconds.items())}
    tests = sum(count[f] for f in files)
    return {
        "measured_from": {"run": run, "sha": sha, "shards": n, "logs": len(logs)},
        "per_test": round(sum(files.values()) / tests, 3),
        "files": files,
    }


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


def _weights_cli(args):
    opts, logs = {}, []
    it = iter(args)
    for flag in it:
        value = next(it, None)
        if value is None or flag not in ("--nodes", "--shards", "--run", "--sha", "--log", "--root"):
            print("shard.py weights: --nodes F --shards N --run ID --sha SHA --log FILE:K ...", file=sys.stderr)
            return 2
        if flag == "--log":
            path, _, k = value.rpartition(":")
            logs.append((Path(path).read_text(encoding="utf-8"), int(k)))
        else:
            opts[flag] = value
    try:
        out = measure(Path(opts["--nodes"]).read_text(encoding="utf-8"), _count(opts["--shards"]), logs,
                      opts.get("--run", ""), opts.get("--sha", ""), Path(opts.get("--root", ROOT)))
    except (KeyError, AssertionError, ValueError) as e:
        print(f"shard.py weights: {e!r}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1))
    return 0


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
    if len(argv) >= 2 and argv[1] == "weights":
        return _weights_cli(argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
