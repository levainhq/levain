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
    assert rc.dynamic_sites() == ["levain/a.py::<module>::x = run_module(m)", "levain/a.py::<module>::x = run_module(m)::#2"]


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
    assert set(table) - set(found) == {"levain/a.py::<module>::x = run_module(m)"}
    assert set(found) - set(table) == {"levain/a.py::<module>::x = run_module(other)"}


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
    assert {"levain.slot.forwarder", "levain.slot.wall", "levain.broker.front"} <= roots


QBROKER = "levain.slot.services.qbroker"
DISPATCH = "levain.slot.services.dispatch"


def test_root_verifies_a_route_map_without_the_authority_store_or_the_autonomic_package():
    """S1 3c-4: CONTROL and keys verify a route map's signature with ``levain.authority_sig``; the authority store
    (its replay reaches the cockpit) and ``levain.autonomic`` stay out of what root imports. The queue broker's
    service runs as ``_levain_qbroker``, never root, and verifies signed grants and counts limits with both, so it is
    a root left out here, and the dispatch service is the other (it runs as ``_levain_dispatch`` and any other euid,
    root included, exits at once, and its job store replays the authority); every other root (the helpers, the reaper, the wall's, CONTROL's and keys' services
    once they exist) is held to it."""
    roots = rc._entry_modules()
    assert QBROKER in roots and DISPATCH in roots  # each exclusion names a real root, so it cannot silently exclude nothing
    # the reason each exclusion stands: its unit runs under its own service account, never root (fails if one moves to root)
    from levain.slot.units import SERVICES
    users = {s.name: s.user for s in SERVICES}
    assert users["qbroker"] == "_levain_qbroker" and users["dispatch"] == "_levain_dispatch"
    mods = set(rc.closure([m for m in roots if m not in (QBROKER, DISPATCH)]))
    assert {"levain.slot.routemaps", "levain.authority_sig", "levain.trust_anchor"} <= mods
    assert "levain.authority" not in mods and not {m for m in mods if m.startswith("levain.autonomic")}


def test_the_queue_broker_imports_neither_the_agent_hooks_nor_the_dispatch_cli():
    """The queue broker's closure holds what its paths run, not the agent hooks or the operator's dispatch CLI: the
    rules it shares with the hooks live in the leaf ``levain.consent.adapters.rules``, the contract bound in
    ``levain.dispatch.contract``."""
    mods = set(rc.closure([QBROKER]))
    assert {"levain.consent.adapters.rules", "levain.dispatch.contract", "levain.authority"} <= mods
    assert not mods & {"levain.consent.adapters.claude_code", "levain.consent.adapters.codex", "levain.consent.away",
                       "levain.dispatch.cli"}


def test_every_excluded_test_file_exists_is_selected_without_the_table_and_holds_its_evidence():
    """An entry of NOT_ROOT_TESTS cannot rot: its file exists, the import rule alone would still select it (so the
    entry still excludes something), and both evidence strings of its reason are where it says."""
    mods = rc.closure()
    unfiltered = set(rc._test_files(mods, excluded={}))
    filtered = set(rc._test_files(mods))
    assert unfiltered - filtered == set(rc.NOT_ROOT_TESTS)
    root = Path(__file__).resolve().parents[1]
    for name, (reason, marker, (other, needle)) in rc.NOT_ROOT_TESTS.items():
        assert reason and (root / name).is_file(), name
        assert marker in (root / name).read_text(encoding="utf-8"), (name, marker)
        assert needle in (root / other).read_text(encoding="utf-8"), (name, other, needle)


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
    key = "levain/slot/forwarder.py::f::from levain import lazy_a"
    assert sorted(found) == [key]
    assert sorted(set(found) - set(rc.LAZY_SITES)) == [key]  # the test above would fail on this tree


def test_lazy_sites_are_keyed_by_content_with_an_ordinal_for_a_repeat(tmp_path, monkeypatch):
    """The ordinal counts a repeat within one function; the same line in another function is a different key."""
    body = ("def f(x):\n    if x:\n        from levain import lazy_a\n    else:\n        from levain import lazy_a\n\n\n"
            "class C:\n    def g(self):\n        from levain import lazy_a\n")
    _lazy_tree(tmp_path, monkeypatch, body)
    keys = sorted(rc.lazy_sites(["levain.slot.forwarder"]))
    assert keys == ["levain/slot/forwarder.py::C.g::from levain import lazy_a",
                    "levain/slot/forwarder.py::f::from levain import lazy_a",
                    "levain/slot/forwarder.py::f::from levain import lazy_a::#2"]
    (tmp_path / "levain" / "slot" / "forwarder.py").write_text("\n\n" + body)  # an edit above both sites
    assert sorted(rc.lazy_sites(["levain.slot.forwarder"])) == keys


def test_a_lazy_import_moved_to_another_function_fails_its_row(tmp_path, monkeypatch):
    """A row is the site's enclosing function as well as its text: an unchanged import moved from an operator-only
    function into a root-callable one must be re-classified, while an edit above it changes nothing."""
    body = "def f():\n    from levain import lazy_a\n\n\ndef g():\n    pass\n"
    _lazy_tree(tmp_path, monkeypatch, body)
    table = sorted(rc.lazy_sites(["levain.slot.forwarder"]))
    forwarder = tmp_path / "levain" / "slot" / "forwarder.py"
    forwarder.write_text("\n\n" + body)
    assert sorted(rc.lazy_sites(["levain.slot.forwarder"])) == table
    forwarder.write_text("def f():\n    pass\n\n\ndef g():\n    from levain import lazy_a\n")
    found = sorted(rc.lazy_sites(["levain.slot.forwarder"]))
    assert set(table) - set(found) and set(found) - set(table)  # stale and unclassified: the classification test fails


def test_a_dynamic_site_moved_to_another_function_fails_its_row(tmp_path, monkeypatch):
    body = "def f():\n    x = run_module(m)\n\n\nclass C:\n    def g(self):\n        pass\n"
    _site_tree(tmp_path, monkeypatch, body)
    table = rc.dynamic_sites()
    _site_tree(tmp_path, monkeypatch, "\n" + body)
    assert rc.dynamic_sites() == table
    _site_tree(tmp_path, monkeypatch, "def f():\n    pass\n\n\nclass C:\n    def g(self):\n        x = run_module(m)\n")
    found = rc.dynamic_sites()
    assert set(table) - set(found) and set(found) - set(table)


def test_root_lazy_targets_join_the_closure_and_operator_ones_do_not(tmp_path, monkeypatch):
    _lazy_tree(tmp_path, monkeypatch, "def f():\n    from levain import lazy_a\n\n\ndef g():\n    import levain.lazy_b\n")
    monkeypatch.setattr(rc, "LAZY_SITES", {"levain/slot/forwarder.py::f::from levain import lazy_a": "root",
                                           "levain/slot/forwarder.py::g::import levain.lazy_b": "operator"})
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
    assert rc.dynamic_sites() == ['levain/a.py::<module>::x = importlib.import_module("levain.x")',
                                  'levain/a.py::<module>::y = __import__("levain.y")']  # unclassified until a row names them
