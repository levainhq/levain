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
    assert rc.dynamic_sites() == ["levain/a.py::x = run_module(m)", "levain/a.py::x = run_module(m)::#2"]


def _site_tree(tmp_path, monkeypatch, body):
    (tmp_path / "levain").mkdir(exist_ok=True)
    (tmp_path / "levain" / "a.py").write_text(body)
    monkeypatch.setattr(rc, "ROOT", tmp_path)


def test_a_line_inserted_above_a_site_keeps_its_row(tmp_path, monkeypatch):
    """Rows are keyed by content, not line number: an edit above a cited site (the CI churn of 10-23) changes nothing."""
    _site_tree(tmp_path, monkeypatch, "x = run_module(m)\ny = run_module(n)\n")
    table = rc.dynamic_sites()
    _site_tree(tmp_path, monkeypatch, "\n\nx = run_module(m)\n\ny = run_module(n)\n")
    assert rc.dynamic_sites() == table


def test_a_changed_site_line_fails_its_row(tmp_path, monkeypatch):
    """A row whose content no longer exists is stale and a changed site is unclassified: it must be re-classified."""
    _site_tree(tmp_path, monkeypatch, "x = run_module(m)\n")
    table = rc.dynamic_sites()
    _site_tree(tmp_path, monkeypatch, "x = run_module(other)\n")
    found = rc.dynamic_sites()
    assert set(table) - set(found) == {"levain/a.py::x = run_module(m)"}
    assert set(found) - set(table) == {"levain/a.py::x = run_module(other)"}


def test_a_module_name_differing_only_in_case_is_not_a_module(tmp_path, monkeypatch):
    """macOS's filesystem is case-insensitive: ``is_file`` alone finds binding.py for ``Binding``, Linux's does not."""
    (tmp_path / "levain" / "sub").mkdir(parents=True)
    (tmp_path / "levain" / "__init__.py").write_text("")
    (tmp_path / "levain" / "binding.py").write_text("")
    (tmp_path / "levain" / "sub" / "__init__.py").write_text("")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    assert rc._path("levain.binding") == tmp_path / "levain" / "binding.py"
    assert rc._path("levain.sub") == tmp_path / "levain" / "sub" / "__init__.py"
    assert rc._path("levain.Binding") is None
    assert rc._path("Levain.binding") is None
    assert rc._path("levain.SUB") is None


def test_roots_include_the_dynamically_run_modules():
    roots = set(rc._entry_modules())
    assert {"levain.slot.forwarder", "levain.slot.wall", "levain.broker.stub"} <= roots


def test_root_verifies_a_route_map_without_the_authority_store_or_the_autonomic_package():
    """S1 3c-4: CONTROL and keys verify a route map's signature with ``levain.authority_sig``; the authority store
    (its replay reaches the cockpit) and ``levain.autonomic`` stay out of what root imports."""
    mods = set(rc.closure())
    assert {"levain.slot.routemaps", "levain.authority_sig", "levain.trust_anchor"} <= mods
    assert "levain.authority" not in mods and not {m for m in mods if m.startswith("levain.autonomic")}


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


def test_every_function_local_import_in_the_closure_is_classified():
    """A new function-local ``levain`` import in a closure module fails here until it is a row of LAZY_SITES, ``"root"``
    or ``"operator"``, so a lazy import a root process can run cannot be missed by the import-graph walk."""
    found, table = sorted(rc.lazy_sites(rc.closure())), sorted(rc.LAZY_SITES)
    assert found == table, {"unclassified": sorted(set(found) - set(table)), "stale": sorted(set(table) - set(found))}
    assert set(rc.LAZY_SITES.values()) == {"root", "operator"}


def _lazy_tree(tmp_path, monkeypatch, body):
    (tmp_path / "levain" / "slot").mkdir(parents=True)
    for d in ("levain", "levain/slot"):
        (tmp_path / d / "__init__.py").write_text("")
    (tmp_path / "levain" / "slot" / "forwarder.py").write_text(body)
    (tmp_path / "levain" / "lazy_a.py").write_text("")
    (tmp_path / "levain" / "lazy_b.py").write_text("")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "DYNAMIC_SITES", {})
    monkeypatch.setattr(rc, "_entry_modules", lambda: ["levain.slot.forwarder"])


def test_a_new_lazy_import_is_unclassified_until_the_table_names_it(tmp_path, monkeypatch):
    _lazy_tree(tmp_path, monkeypatch, "def f():\n    from levain import lazy_a\n")
    monkeypatch.setattr(rc, "LAZY_SITES", {})
    found = rc.lazy_sites(rc.closure())
    key = "levain/slot/forwarder.py::from levain import lazy_a"
    assert sorted(found) == [key]
    assert sorted(set(found) - set(rc.LAZY_SITES)) == [key]  # the test above would fail on this tree


def test_lazy_sites_are_keyed_by_content_with_an_ordinal_for_a_repeat(tmp_path, monkeypatch):
    body = "def f():\n    from levain import lazy_a\n\n\ndef g():\n    from levain import lazy_a\n"
    _lazy_tree(tmp_path, monkeypatch, body)
    keys = sorted(rc.lazy_sites(["levain.slot.forwarder"]))
    assert keys == ["levain/slot/forwarder.py::from levain import lazy_a",
                    "levain/slot/forwarder.py::from levain import lazy_a::#2"]
    (tmp_path / "levain" / "slot" / "forwarder.py").write_text("\n\n" + body)  # an edit above both sites
    assert sorted(rc.lazy_sites(["levain.slot.forwarder"])) == keys


def test_root_lazy_targets_join_the_closure_and_operator_ones_do_not(tmp_path, monkeypatch):
    _lazy_tree(tmp_path, monkeypatch, "def f():\n    from levain import lazy_a\n\n\ndef g():\n    import levain.lazy_b\n")
    monkeypatch.setattr(rc, "LAZY_SITES", {"levain/slot/forwarder.py::from levain import lazy_a": "root",
                                           "levain/slot/forwarder.py::import levain.lazy_b": "operator"})
    mods = rc.closure()
    assert "levain.lazy_a" in mods and "levain.lazy_b" not in mods


def test_a_module_level_import_is_not_a_lazy_site(tmp_path, monkeypatch):
    _lazy_tree(tmp_path, monkeypatch, "from levain import lazy_a\ntry:\n    import levain.lazy_b\nexcept ImportError:\n    pass\n")
    assert rc.lazy_sites(["levain.slot.forwarder"]) == {}


def test_importlib_call_sites_are_dynamic_sites(tmp_path, monkeypatch):
    (tmp_path / "levain").mkdir()
    (tmp_path / "levain" / "a.py").write_text('import importlib\nx = importlib.import_module("levain.x")\ny = __import__("levain.y")\n')
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "DYNAMIC_SITES", {})
    assert rc.dynamic_sites() == ['levain/a.py::x = importlib.import_module("levain.x")',
                                  'levain/a.py::y = __import__("levain.y")']  # unclassified until a row names them
