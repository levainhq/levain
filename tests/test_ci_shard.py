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


NODES2 = "tests/a.py::t1\ntests/a.py::t2\ntests/b.py::t1\ntests/b.py::t2\n"
NONE = ({}, 1.0)
GONE = Path("/nonexistent")


def _log(*rows):
    return "\n".join(rows) + "\n"


# the shape of a real job log (run 41757446489 shard 0): a 7-digit fractional stamp, then outcome chars and a percent
REAL = _log("2026-11-04T02:00:00.1000000Z shell: /usr/bin/bash -e {0}",
            "2026-11-04T02:00:02.1000000Z ..    [ 50%]",
            "2026-11-04T02:00:10.1000000Z ..    [100%]")


def test_measure_turns_progress_timestamps_into_per_file_seconds():
    out = shard.measure(NODES2, 1, [(REAL, 0)], NONE, run="9", sha="abc", root=GONE)
    assert out["files"] == {"tests/a.py": 2.0, "tests/b.py": 8.0}
    assert out["measured_from"]["run"] == "9" and out["per_test"] == 2.5
    with pytest.raises(ValueError, match="log shows"):
        shard.measure(NODES2 + "tests/c.py::t\n" * 50, 1, [(REAL, 0)], NONE, root=GONE)


def test_the_count_guard_is_a_real_raise_not_an_assert():
    import ast
    tree = ast.parse(_SHARD.read_text(encoding="utf-8"))
    measure = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "measure")
    assert not [n for n in ast.walk(measure) if isinstance(n, ast.Assert)]     # python -O elides asserts


def test_a_chunk_with_an_empty_rounded_range_keeps_its_seconds():
    nodes = "tests/a.py::t\n" * 50 + "tests/b.py::t\n" * 50
    log = _log("2026-11-04T02:00:00.0Z start", "2026-11-04T02:00:05.0Z " + "." * 50 + "    [ 50%]",
               "2026-11-04T02:00:07.0Z .    [ 51%]", "2026-11-04T02:00:10.0Z " + "." * 50 + "    [100%]")
    out = shard.measure(nodes, 1, [(log, 0)], NONE, root=GONE)["files"]
    assert round(sum(out.values()), 1) == 10.0, out


@pytest.mark.parametrize("body", ['{"files": {"a.py": NaN}, "per_test": 1}', '{"files": {"a.py": Infinity}, "per_test": 1}',
                                  '{"files": {"a.py": 1}, "per_test": NaN}', '{"files": {"a.py": 1}, "per_test": Infinity}',
                                  '{"files": {"a.py": "x"}, "per_test": 1}', '{"files": {"a.py": null}, "per_test": 1}',
                                  '{"files": {"a.py": 1}, "per_test": "x"}'])
def test_non_finite_or_non_numeric_weights_fail_naming_the_path(tmp_path, body):
    bad = tmp_path / "w.json"
    bad.write_text(body)
    with pytest.raises(ValueError, match="w.json"):
        shard.load_weights(bad)


def test_measure_rebuilds_the_plan_with_the_weights_the_run_used():
    # a (2 tests) and b (2 tests) split over 2 shards: which file lands on shard 0 depends on the weights the run had
    log0 = _log("2026-11-04T02:00:00.0Z start", "2026-11-04T02:00:03.0Z ..    [100%]")
    log1 = _log("2026-11-04T02:00:00.0Z start", "2026-11-04T02:00:07.0Z ..    [100%]")
    logs = [(log0, 0), (log1, 1)]
    bare = shard.measure(NODES2, 2, logs, NONE, root=GONE)["files"]
    assert bare == {"tests/a.py": 3.0, "tests/b.py": 7.0}
    flipped = shard.measure(NODES2, 2, logs, ({"tests/a.py": 1.0, "tests/b.py": 9.0}, 1.0), root=GONE)["files"]
    assert flipped == {"tests/a.py": 7.0, "tests/b.py": 3.0}     # b is heaviest -> shard 0 first


def test_measure_refuses_one_python_only_a_complete_exactly_once_log_set():
    log = _log("2026-11-04T02:00:00.0Z start", "2026-11-04T02:00:03.0Z ..    [100%]")
    for logs, why in (([], "no logs"), ([(log, 2), (log, 0)], r"\[2\] is not in 0"), ([(log, -1), (log, 0)], r"\[-1\]"),
                      ([(log, 0)], r"missing for \[1\]"), ([(log, 0), (log, 0), (log, 1)], r"\[0\] given more than once")):
        with pytest.raises(ValueError, match=why):
            shard.measure(NODES2, 2, logs, NONE, root=GONE)


def test_a_progress_interval_across_utc_midnight_is_kept_and_a_backwards_one_refuses():
    over = _log("2026-11-04T23:59:58.0Z start", "2026-11-04T23:59:59.0Z ..    [ 50%]", "2026-11-05T00:00:07.0Z ..    [100%]")
    assert shard.measure(NODES2, 1, [(over, 0)], NONE, root=GONE)["files"] == {"tests/a.py": 1.0, "tests/b.py": 8.0}
    back = _log("2026-11-05T00:00:01.0Z start", "2026-11-05T00:00:09.0Z ..    [ 50%]", "2026-11-04T23:59:59.0Z ..    [100%]")
    with pytest.raises(ValueError, match="backwards"):
        shard.measure(NODES2, 1, [(back, 0)], NONE, root=GONE)


def test_a_bare_continuation_line_counts_its_tests_and_the_first_chunk_needs_a_stamped_anchor():
    # a test printed mid-line: the continuation has no stamp; its tests join the next stamped line's chunk
    split = _log("2026-11-04T02:00:00.0Z start", "2026-11-04T02:00:02.0Z .    [ 25%]", ".", "2026-11-04T02:00:10.0Z ..    [100%]")
    assert shard.measure(NODES2, 1, [(split, 0)], NONE, root=GONE)["files"] == {"tests/a.py": 4.7, "tests/b.py": 5.3}
    # the first progress line is line 0, with nothing stamped before it: refuse (was lines[-1], a wrong anchor or a crash)
    first = _log("2026-11-04T02:00:02.0Z ..    [ 50%]", "2026-11-04T02:00:10.0Z ..    [100%]")
    with pytest.raises(ValueError, match="no timestamped line before"):
        shard.measure(NODES2, 1, [(first, 0)], NONE, root=GONE)
    # an unstamped line just before the first progress line: the anchor is the nearest stamped line above it
    gap = _log("2026-11-04T02:00:00.0Z start", "", "collected 4 items", REAL.splitlines()[1], REAL.splitlines()[2])
    assert shard.measure(NODES2, 1, [(gap, 0)], NONE, root=GONE)["files"] == {"tests/a.py": 2.1, "tests/b.py": 8.0}


def test_the_weights_command_end_to_end_and_its_refusals(tmp_path):
    nodes, log0 = tmp_path / "n.txt", tmp_path / "s0.log"
    nodes.write_text(NODES2)
    log0.write_text(REAL)
    base = ["weights", "--nodes", str(nodes), "--shards", "1", "--root", str(tmp_path), "--log", f"0:{log0}"]
    ok = _run(*base, "--plan-weights", "none")
    assert ok.returncode == 0 and json.loads(ok.stdout)["files"] == {"tests/a.py": 2.0, "tests/b.py": 8.0}
    for args, err in ((base, "--plan-weights is required"),
                      (base + ["--plan-weights", str(tmp_path / "absent.json")], "no such file"),
                      (["weights", "--nodes", str(nodes), "--shards", "1", "--plan-weights", "none"], "no logs"),
                      (base[:-2] + ["--log", "9:x", "--plan-weights", "none"], "OSError|No such file|not in 0")):
        r = _run(*args)
        assert r.returncode == 1 and "Traceback" not in r.stderr, (args, r.stderr)


def test_the_weights_command_rejects_a_bad_invocation():
    assert _run("weights", "--nodes").returncode == 2
    assert _run("weights", "--bogus", "x").returncode == 2
