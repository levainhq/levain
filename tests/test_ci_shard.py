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
tests/test::edge.py::test_colons

6 tests collected in 0.10s
tests/test_a.py:12: DeprecationWarning: noise
"""
EXPECTED = ["tests/sub/c_test.py", "tests/test d [x].py", "tests/test::edge.py", "tests/test_a.py", "tests/test_b.py"]


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


# ---- measured seconds -------------------------------------------------------------------------------------------


def _w(measured, per_test=0.5):
    return measured, per_test


def test_a_slow_file_with_few_tests_gets_a_shard_to_itself(tmp_path):
    (tmp_path / "many.py").write_text("def test_a(): pass\n" * 40)
    (tmp_path / "many2.py").write_text("def test_a(): pass\n" * 40)
    (tmp_path / "slow.py").write_text("def test_a(): pass\n")
    weights = _w({"slow.py": 100.0, "many.py": 10.0, "many2.py": 10.0})
    files = ["many.py", "many2.py", "slow.py"]
    by_count, _ = shard.partition(files, 2, lambda f: shard.weight(f, tmp_path, ({}, 1.0)))
    by_secs, loads = shard.partition(files, 2, lambda f: shard.weight(f, tmp_path, weights))
    assert ["slow.py"] not in by_count          # the count proxy packs the slow file with others' worth of weight
    assert ["slow.py"] in by_secs and sorted(loads) == [20.0, 100.0]


def test_an_unmeasured_file_is_not_free_and_scales_with_its_test_count(tmp_path):
    (tmp_path / "new.py").write_text("def test_a(): pass\nasync def test_b(): pass\n")
    weights = _w({"old.py": 3.0}, per_test=0.5)
    assert shard.weight("old.py", tmp_path, weights) == 3.0
    assert shard.weight("new.py", tmp_path, weights) == 1.0     # 2 tests x 0.5 s
    assert shard.weight("gone.py", tmp_path, weights) == 0.5    # not on disk: one test's worth
    assert shard.weight("old.py", tmp_path, _w({"old.py": 0.0})) == shard.MIN_SECONDS


def test_without_a_weights_file_the_weight_is_the_bare_test_count(tmp_path):
    (tmp_path / "t.py").write_text("def test_a(): pass\ndef test_b(): pass\n")
    assert shard.load_weights(tmp_path / "absent.json") == ({}, 1.0)
    assert shard.weight("t.py", tmp_path, ({}, 1.0)) == 2


@pytest.mark.parametrize("body", ["{", "{}", '{"files": [], "per_test": 1}', '{"files": {}, "per_test": 0}'])
def test_a_malformed_weights_file_fails_loudly(tmp_path, body):
    bad = tmp_path / "w.json"
    bad.write_text(body)
    with pytest.raises((ValueError, KeyError)):
        shard.load_weights(bad)


def test_the_committed_weights_load_and_name_repo_test_files():
    files, per_test = shard.load_weights()
    assert files and per_test > 0
    assert all(k.startswith("tests/") and k.endswith(".py") and v >= 0 for k, v in files.items())


def test_weighted_partition_is_deterministic_never_empty_and_covers_once():
    names = [f"f{i}.py" for i in range(30)]
    secs = {n: float((i * 7919) % 97 + 1) for i, n in enumerate(names)}
    weigh = lambda f: secs[f]  # noqa: E731
    first = shard.partition(names, 6, weigh)
    assert first == shard.partition(list(reversed(names)), 6, weigh)
    shard.check(first[0], names)
    assert max(first[1]) - min(first[1]) <= max(secs.values())


def test_measure_turns_progress_timestamps_into_per_file_seconds():
    nodes = "tests/a.py::t1\ntests/a.py::t2\ntests/b.py::t1\ntests/b.py::t2\n"
    # one shard, files a then b; the first chunk (a's two tests) took 2 s, the second (b's two) took 8 s
    log = "2026-11-04T02:00:00.0Z collecting\n2026-11-04T02:00:02.0Z ..    [ 50%]\n2026-11-04T02:00:10.0Z ..    [100%]\n"
    out = shard.measure(nodes, 1, [(log, 0)], run="9", sha="abc", root=Path("/nonexistent"))
    assert out["files"] == {"tests/a.py": 2.0, "tests/b.py": 8.0}
    assert out["measured_from"]["run"] == "9" and out["per_test"] == 2.5
    with pytest.raises(AssertionError, match="log shows"):
        shard.measure(nodes + "tests/c.py::t\n" * 50, 1, [(log, 0)], root=Path("/nonexistent"))


def test_the_weights_command_rejects_a_bad_invocation():
    assert _run("weights", "--nodes").returncode == 2
    assert _run("weights", "--bogus", "x").returncode == 2
