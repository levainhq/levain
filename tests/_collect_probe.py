"""A pytest plugin for tests/test_ci_invariant_marker.py (`-p tests._collect_probe`): it prints one line per test module
pytest starts to collect, so that test can say which modules the invariant tier really imported."""
import sys


def pytest_collectstart(collector):
    if collector.__class__.__name__ == "Module":
        sys.stderr.write(f"COLLECTING {collector.nodeid}\n")
