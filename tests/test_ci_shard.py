"""tests/ci/shard.py: the CI shard plan covers every collected file once and can fail."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SHARD = Path(__file__).resolve().parent / "ci" / "shard.py"
_spec = importlib.util.spec_from_file_location("ci_shard", _SHARD)
shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shard)

NODE_IDS = """\
tests/test_a.py::test_x[1]
tests/test_a.py::test_x[2]
tests/test_a.py::test_y
tests/test_b.py::TestK::test_z
tests/sub/c_test.py::test_w
tests/test d [x].py::test_spaced

6 tests collected in 0.10s
tests/test_a.py:12: DeprecationWarning: noise
"""
EXPECTED = ["tests/sub/c_test.py", "tests/test d [x].py", "tests/test_a.py", "tests/test_b.py"]


def _run(*args, stdin="", env=None):
    return subprocess.run(
        [sys.executable, str(_SHARD), *args], input=stdin, capture_output=True, text=True, env=env
    )


def _fake_weight(name):
    return {"a": 9, "b": 5, "c": 4, "d": 3, "e": 1}[name]


FILES = ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_partition_covers_every_file_exactly_once(n):
    shards, _ = shard.partition(FILES, n, _fake_weight)
    assert len(shards) == n
    shard.check(shards, FILES)
    assert sorted(f for s in shards for f in s) == sorted(FILES)


def test_partition_is_deterministic_and_balanced():
    first = shard.partition(FILES, 3, _fake_weight)
    assert first == shard.partition(list(reversed(FILES)), 3, _fake_weight)
    assert max(first[1]) - min(first[1]) <= max(map(_fake_weight, FILES))


def test_check_fails_on_a_missing_file():
    shards, _ = shard.partition(FILES, 3, _fake_weight)
    shards[0] = [f for f in shards[0] if f != "a"] if "a" in shards[0] else shards[0][:-1]
    with pytest.raises(AssertionError, match="missing"):
        shard.check(shards, FILES)


def test_check_fails_on_a_duplicated_file():
    shards, _ = shard.partition(FILES, 3, _fake_weight)
    shards[1] = shards[1] + [shards[0][0]]
    with pytest.raises(AssertionError, match="duplicated"):
        shard.check(shards, FILES)


def test_node_ids_reduce_to_unique_files_including_star_test_and_parametrized():
    assert shard.files_from_node_ids(NODE_IDS) == EXPECTED


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_plan_covers_every_file_exactly_once(n):
    shards = shard.plan(NODE_IDS, n)
    assert sorted(f for s in shards for f in s) == EXPECTED


def test_shards_cli_writes_the_matrix_list(tmp_path):
    out = tmp_path / "gh_output"
    env = {**os.environ, "GITHUB_OUTPUT": str(out)}
    r = _run("shards", "--shards", "3", env=env)
    assert r.returncode == 0, r.stderr
    assert out.read_text() == "shards=[0, 1, 2]\n"


def test_pick_shards_together_cover_every_file_once():
    picked = []
    for k in range(3):
        r = _run("pick", "--shards", "3", "--shard", str(k), stdin=NODE_IDS)
        assert r.returncode == 0, r.stderr
        picked += r.stdout.splitlines()
    assert sorted(picked) == EXPECTED


def test_pick_prints_paths_with_spaces_and_brackets_literally():
    r = _run("pick", "--shards", "1", "--shard", "0", stdin=NODE_IDS)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == EXPECTED


def test_pick_fails_when_a_shard_would_be_empty():
    n = len(EXPECTED) + 1
    r = _run("pick", "--shards", str(n), "--shard", "0", stdin=NODE_IDS)
    assert r.returncode != 0
    assert "empty_shards" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize("text", ["", "\n", "no tests ran in 0.01s\n", "ERROR collecting tests/x.py\n"])
def test_pick_fails_on_input_without_node_ids(text):
    r = _run("pick", "--shards", "2", "--shard", "0", stdin=text)
    assert r.returncode != 0
    assert "no test node ids" in r.stderr


@pytest.mark.parametrize("args", [("shards", "--shards", "0"), ("shards", "--shards", "x"),
                                  ("pick", "--shards", "0", "--shard", "0"), ("pick", "--shards", "2", "--shard", "2"),
                                  ("pick", "--shards", "2", "--shard", "-1"), ("pick", "--shards", "2", "--shard", "x")])
def test_nonsense_counts_and_indexes_fail(args):
    assert _run(*args, stdin=NODE_IDS).returncode == 1


def test_unknown_usage_exits_2():
    assert _run("4", "0").returncode == 2
    assert _run("--check", "4").returncode == 2
    assert _run("plan", "--shards", "2").returncode == 2
    assert _run("files").returncode == 2
