"""tests/ci/invariant_tests.txt: the `invariant` marker covers exactly the listed whole-tree tests, and the listed
ones exist (a rename must not silently drop a test out of the pre-push tier)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Whole-tree tests that failed in the 40 `test` runs before the marker was cut (the plan's second membership source).
NAMED_FAILURES = [
    "tests/test_launch.py::test_every_child_levain_starts_is_given_an_explicit_environment",
    "tests/test_slot_sudo_abs.py::test_no_file_in_levain_spells_sudo_itself_outside_the_home",
    "tests/test_seam_classes_census.py::test_every_table_of_the_maximal_database_is_in_exactly_one_class",
    "tests/test_readme_commands.py::test_every_listed_verb_is_named_in_all_commands",
    "tests/test_authority_agent_signing.py::test_no_caller_of_the_agent_link_prepare_outside_signing_py",
]


def _collect(*args):
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
                         cwd=ROOT, capture_output=True, text=True, timeout=600).stdout
    return {ln.split("[", 1)[0] for ln in out.splitlines() if "::" in ln}


def test_the_marker_selects_exactly_the_listed_tests_and_the_named_failures_are_in():
    listed = {ln.strip() for ln in (ROOT / "tests" / "ci" / "invariant_tests.txt").read_text().splitlines()
              if ln.strip() and not ln.startswith("#")}
    assert set(NAMED_FAILURES) <= listed
    # one collection pass over just the listed files keeps this fast; a listed id that no longer exists is missing here
    selected = _collect("-m", "invariant", *sorted({i.split("::", 1)[0] for i in listed}))
    assert selected == listed, (sorted(listed - selected), sorted(selected - listed))
