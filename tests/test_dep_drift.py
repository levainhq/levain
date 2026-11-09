"""scripts/dep_drift.py: parsing a uv.lock and diffing two of them, the part the nightly issue body is made from."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("dep_drift", Path(__file__).resolve().parents[1] / "scripts" / "dep_drift.py")
dep_drift = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dep_drift)

OLD = '''
version = 1
[[package]]
name = "agent-client-protocol"
version = "0.12.1"
[[package]]
name = "levain"
source = { editable = "." }
[[package]]
name = "colorama"
version = "0.4.6"
[[package]]
name = "gone"
version = "1.0"
'''
NEW = '''
version = 1
[[package]]
name = "agent-client-protocol"
version = "1.0.0"
[[package]]
name = "levain"
source = { editable = "." }
[[package]]
name = "colorama"
version = "0.4.6"
[[package]]
name = "fresh"
version = "2.0"
'''


def test_the_project_itself_has_no_version_and_is_not_listed():
    assert "levain" not in dep_drift.lock_versions(OLD)


def test_diff_names_every_changed_package_old_to_new():
    rows = dep_drift.diff_versions(dep_drift.lock_versions(OLD), dep_drift.lock_versions(NEW))
    assert [dep_drift.format_change(r) for r in rows] == [
        "agent-client-protocol 0.12.1 -> 1.0.0",
        "fresh (new) -> 2.0",
        "gone 1.0 -> (removed)",
    ]


def test_an_unchanged_lock_diffs_empty():
    assert dep_drift.diff_versions(dep_drift.lock_versions(OLD), dep_drift.lock_versions(OLD)) == []


def test_a_package_with_two_marker_versions_compares_as_a_set():
    two = '[[package]]\nname = "x"\nversion = "2"\n[[package]]\nname = "x"\nversion = "1"\n'
    assert dep_drift.lock_versions(two) == {"x": ("1", "2")}
    one = '[[package]]\nname = "x"\nversion = "1"\n'
    rows = dep_drift.diff_versions(dep_drift.lock_versions(one), dep_drift.lock_versions(two))
    assert [dep_drift.format_change(r) for r in rows] == ["x 1 -> 1,2"]


def test_the_result_line_is_one_json_line_after_the_marker():
    import json
    rows = [{"name": "a", "old": "1", "new": "2"}]
    line = dep_drift.result_line("red", "suite", rows, pytest_exit=1)
    assert line.startswith("DEP_DRIFT ") and "\n" not in line
    assert json.loads(line.split(" ", 1)[1]) == {"result": "red", "stage": "suite", "changed": ["a 1 -> 2"], "pytest_exit": 1}
