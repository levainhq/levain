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
    found, table = set(rc.dynamic_sites()), set(rc.DYNAMIC_SITES)
    assert found == table, {"unclassified": sorted(found - table), "stale": sorted(table - found)}


def test_roots_include_the_dynamically_run_modules():
    roots = set(rc._entry_modules())
    assert {"levain.slot.forwarder", "levain.slot.wall", "levain.broker.stub"} <= roots


def test_a_test_with_an_unparseable_helper_is_not_selected(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helper.py").write_text("def f(:\n")
    (tmp_path / "tests" / "test_a.py").write_text("from tests import helper\nfrom levain.slot import forwarder\n")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    assert rc._test_deps(tmp_path / "tests" / "test_a.py", set()) is None
