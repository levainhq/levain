"""The root-py39 job's closure (tests/ci/root_closure.py): its dynamic-execution table is a tripwire."""
import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "ci" / "root_closure.py"
_spec = importlib.util.spec_from_file_location("root_closure_under_test", _PATH)
rc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rc
_spec.loader.exec_module(rc)


def test_every_dynamic_execution_site_is_classified():
    """A new ``run_module(`` / ``module_argv(`` site in levain/ fails here until it is a row of DYNAMIC_SITES, so a
    module root runs by name cannot be missed by the import-graph walk."""
    found, table = rc.dynamic_sites(), sorted(rc.DYNAMIC_SITES)
    assert found == table, {"unclassified": sorted(set(found) - set(table)), "stale": sorted(set(table) - set(found))}


def test_two_identical_site_lines_in_one_file_are_two_sites(tmp_path, monkeypatch):
    (tmp_path / "levain").mkdir()
    (tmp_path / "levain" / "a.py").write_text("x = run_module(m)\ny = 1\nx = run_module(m)\n")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    assert rc.dynamic_sites() == ["levain/a.py:1::x = run_module(m)", "levain/a.py:3::x = run_module(m)"]


def test_roots_include_the_dynamically_run_modules():
    roots = set(rc._entry_modules())
    assert {"levain.slot.forwarder", "levain.slot.wall", "levain.broker.stub"} <= roots


def test_a_test_with_an_unparseable_helper_is_not_selected(tmp_path, monkeypatch, capsys):
    (tmp_path / "tests").mkdir()
    (tmp_path / "levain" / "slot").mkdir(parents=True)
    (tmp_path / "levain" / "__init__.py").write_text("")
    (tmp_path / "levain" / "slot" / "__init__.py").write_text("")
    (tmp_path / "levain" / "slot" / "forwarder.py").write_text("")
    (tmp_path / "tests" / "helper.py").write_text("def f(:\n")
    (tmp_path / "tests" / "test_a.py").write_text("from tests import helper\nfrom levain.slot import forwarder\n")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    assert rc._test_deps(tmp_path / "tests" / "test_a.py", set()) is None
    assert rc._test_files(["levain.slot.forwarder"]) == []
    assert "skipped (3.10+ syntax): tests/test_a.py" in capsys.readouterr().err
