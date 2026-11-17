"""tests/ci/LINK_INVARIANTS is the fixed list every stack link tip runs: it must stay real and exactly as pinned."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("link_invariants_under_test", REPO / "tests" / "ci" / "link_invariants.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

#: EVERY file the list holds, pinned by name: a link that drops one (or adds one) fails here, so the change is deliberate.
PINNED = {
    "tests/test_seam_classes_census.py", "tests/test_live_marker.py", "tests/test_ci_root_closure.py",
    "tests/test_slot_egress_cards.py", "tests/test_root_py39_floor.py", "tests/test_ci_invariant_marker.py",
    "tests/test_link_invariants_list.py",
}


def test_the_list_is_non_empty_unique_and_every_file_exists():
    paths = runner.listed()
    assert paths
    assert len(paths) == len(set(paths)), "duplicate entries"
    assert [p for p in paths if not (REPO / p).is_file()] == []


def test_the_list_is_exactly_the_pinned_set():
    got = set(runner.listed())
    assert got == PINNED, ("LINK_INVARIANTS changed: missing %s, added %s. Update PINNED in this file deliberately."
                           % (sorted(PINNED - got), sorted(got - PINNED)))


def test_an_empty_closure_is_refused_not_fallen_through(monkeypatch, capsys):
    def empty():
        raise RuntimeError("root_closure.py tests derived no files")
    monkeypatch.setattr(runner, "run_step", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "root_closure_files", empty)
    assert runner.main(["--all-root"]) == 1
    assert "root closure: FAIL (root_closure.py tests derived no files)" in capsys.readouterr().out


def test_print_executes_nothing_even_under_all_root(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("--print executed something")
    monkeypatch.setattr(runner, "run_step", boom)
    monkeypatch.setattr(runner, "root_closure_files", boom)
    assert runner.main(["--print", "--all-root"]) == 0
    assert "root closure: derived when run" in capsys.readouterr().out


def test_an_empty_derivation_raises(monkeypatch):
    class Done:
        returncode, stdout = 0, "\n"
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(RuntimeError):
        runner.root_closure_files()


def test_the_count_parser_keeps_errors_whole():
    assert [w for _, w in runner.COUNT.findall("2 errors in 1s")] == ["errors"]
    assert [w for _, w in runner.COUNT.findall("1 error, 3 passed, 4 skipped")] == ["error", "passed", "skipped"]


def test_the_runner_runs_the_guard_even_when_the_list_drops_it(monkeypatch, tmp_path):
    fake = tmp_path / "LINK_INVARIANTS"
    fake.write_text("tests/test_live_marker.py\n", encoding="utf-8")
    monkeypatch.setattr(runner, "LIST", fake)
    assert runner.listed() == ["tests/test_live_marker.py", runner.GUARD]
    fake.write_text("# nothing\n", encoding="utf-8")
    assert runner.listed() == [runner.GUARD]


def test_a_crash_before_any_count_prints_no_count_read(monkeypatch, capsys):
    class Crash:
        returncode, stdout = 2, "ImportError: boom\n"
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: Crash())
    assert runner.run_step("listed files", ["x"]) == 2
    assert "FAIL (no count read)" in capsys.readouterr().out
