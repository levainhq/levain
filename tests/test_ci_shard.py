"""tests/ci/shard.py: the CI shard partition covers every test file once and can fail."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SHARD = Path(__file__).resolve().parent / "ci" / "shard.py"
_spec = importlib.util.spec_from_file_location("ci_shard", _SHARD)
shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shard)


def _fake_weight(name):
    return {"a": 9, "b": 5, "c": 4, "d": 3, "e": 1}[name]


FILES = ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 7])
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


def test_real_suite_partitions_and_includes_this_file():
    files = shard.collected_files()
    assert "tests/test_ci_shard.py" in files
    shards, _ = shard.partition(files, 4, shard.weight)
    shard.check(shards, files)


def test_cli_prints_a_shard_and_check_passes():
    py = sys.executable
    out = subprocess.run([py, str(_SHARD), "4", "0"], capture_output=True, text=True, check=True)
    assert out.stdout.split()
    ok = subprocess.run([py, str(_SHARD), "--check", "4"], capture_output=True, text=True)
    assert ok.returncode == 0
    bad = subprocess.run([py, str(_SHARD), "4", "4"], capture_output=True, text=True)
    assert bad.returncode == 2
