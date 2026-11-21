"""tests/ci/invariant_tests.txt: the `invariant` marker covers exactly the listed whole-tree tests, and the listed
ones exist (a rename must not silently drop a test out of the `invariant` tier)."""
import re
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


def _run_collect(*args):
    """One `--collect-only` run, its exit code checked: a collection error after the ids were printed is a failure."""
    run = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
                         cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, (run.returncode, run.stdout[-1500:], run.stderr[-1500:])
    return run


def _collect(*args):
    out = _run_collect(*args).stdout
    return {ln.split("[", 1)[0] for ln in out.splitlines() if "::" in ln}


def test_the_marker_selects_exactly_the_listed_tests_and_the_named_failures_are_in():
    listed = {ln.strip() for ln in (ROOT / "tests" / "ci" / "invariant_tests.txt").read_text().splitlines()
              if ln.strip() and not ln.startswith("#")}
    assert set(NAMED_FAILURES) <= listed
    # one collection pass over just the listed files keeps this fast; a listed id that no longer exists is missing here
    selected = _collect("-m", "invariant", *sorted({i.split("::", 1)[0] for i in listed}))
    assert selected == listed, (sorted(listed - selected), sorted(selected - listed))


def test_no_header_claims_a_pre_push_hook_runs_the_tier():
    for rel in ("tests/ci/invariant_tests.txt", "pyproject.toml"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "scripts/hooks/pre-push" not in text, rel
        assert "is the pre-push tier" not in text, rel


def _listed():
    return {ln.strip() for ln in (ROOT / "tests" / "ci" / "invariant_tests.txt").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")}


def test_the_bare_invariant_run_imports_only_the_listed_modules_and_builds_only_the_listed_tests():
    """The conductor's pin runs `pytest -m invariant --collect-only` with no files. It must select the set the
    explicit-files run does, import the modules that hold the listed tests and no others, and build no test it then
    deselects (a `N/M tests collected` summary means unlisted tests were built). Without the hooks in
    tests/conftest.py this test fails on the module set and on the summary, not only on the time."""
    listed = _listed()
    run = _run_collect("-m", "invariant", "-p", "tests._collect_probe")
    ids = {ln.split("[", 1)[0] for ln in run.stdout.splitlines() if "::" in ln}
    assert ids == listed, (sorted(listed - ids), sorted(ids - listed))
    imported = {ln.split(" ", 1)[1] for ln in run.stderr.splitlines() if ln.startswith("COLLECTING ")}
    assert imported == {i.split("::", 1)[0] for i in listed}, sorted(imported ^ {i.split("::", 1)[0] for i in listed})[:10]
    assert re.search(r"^\d+ tests? collected", run.stdout, re.M), run.stdout[-300:]


def test_no_listed_id_is_written_with_a_parametrize_bracket():
    """A listed id names its function bare (the marker compares the base id, and the bare invariant run builds a
    function only when `file::function` is listed)."""
    assert [i for i in _listed() if "[" in i] == []


class _Cfg:
    """The three things the tier's rule asks a pytest config."""
    def __init__(self, markexpr="invariant", args=("tests",), patterns=("test_*.py", "*_test.py")):
        self._m, self.args, self._p = markexpr, list(args), list(patterns)

    def getoption(self, name, default=None):
        return self._m if name == "markexpr" else default

    def getini(self, name):
        return self._p


def test_the_invariant_tier_skips_only_test_modules_that_hold_no_listed_test():
    from tests.conftest import invariant_modules, skipped_by_the_invariant_tier as skip
    held = sorted(invariant_modules())[0]
    assert not skip(ROOT / held, _Cfg())                           # a module with a listed test is collected
    other = next(p for p in sorted((ROOT / "tests").glob("test_*.py")) if p.relative_to(ROOT).as_posix() not in invariant_modules())
    assert skip(other, _Cfg())                                     # a module with none is not imported
    assert skip(other.with_name("zz_probe_test.py"), _Cfg())       # pytest's `*_test.py` pattern counts as a test module
    for cfg in (_Cfg("not invariant"), _Cfg(""), _Cfg("invariant or slow"),
                _Cfg(args=("tests/test_launch.py::test_x",))):     # another expression, or a node id named: collect as before
        assert not skip(other, cfg)
    assert not skip(ROOT / "tests" / "conftest.py", _Cfg())        # only test modules are ever skipped
