"""The snapshot contract doc points at code; every pointer must still land where the doc says it does.

docs/cockpit_snapshot_contract.md backticks only code symbols and repo paths (its own convention paragraph says so). Each
backticked symbol must be DEFINED in the module whose path is named in the same paragraph or list item of the doc
(snapshots.py when none is named): a class, a function or a module-level assignment in that module's own source, so an
imported module or an imported name (`os`, `Any`, `Path`), a dunder, and an attribute a class only inherits all fail.
``Class.method`` must be a function in that class's own ``__dict__``. A path must be an existing file. That is all that
is checked: a symbol resolves by the module its own unit names, not by any other module.

The phrase list below catches the exact phrases review proved false coming back, not a paraphrase of them; a true name
inside a false sentence is not catchable by any test.
"""
from __future__ import annotations

import ast
import inspect
import re
import types
from pathlib import Path

from levain.cockpit import derive, engine, snapshot_host, snapshots

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "cockpit_snapshot_contract.md"
MODULES = {
    "levain/cockpit/snapshots.py": snapshots,
    "levain/cockpit/snapshot_host.py": snapshot_host,
    "levain/cockpit/engine.py": engine,
    "levain/cockpit/derive.py": derive,
}
DEFAULT_PATH = "levain/cockpit/snapshots.py"
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")

# Sentences the doc once held that the code contradicts (L1/L2 reviews of the first cut): the doc states none of them.
DELETED_FALSE_CLAIMS = (
    ("last good content", "the reader serves an error with no body; only the stored row keeps the body"),
    ("exactly when gen moves", "an error-text change and a stale-to-current flip move neither gen nor publish_seq"),
    ("unique only within one file lifetime", "a dropped then republished panel starts at gen 1 again in the same file"),
    ("never decreases", "write_gen drops after drop then publish, and an in-place rebuild resets publish_seq"),
    ("with a matching meta schema", "adoption of a foreign file applies no such guard"),
    ("never errors", "an unreadable store, a panel with no row and an error row all read as an error"),
    ("exactly as it found it", "a read-only preflight can still create sqlite's shared-memory file"),
    ("costs the file nothing", "the same refusal claim reworded: a read-only preflight can still create sqlite's -shm file"),
)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _toplevel_names(mod: types.ModuleType) -> set[str]:
    """Names the module's own source defines at top level: a class, a function, or an assignment (not an import)."""
    names: set[str] = set()
    for node in ast.parse(Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _defined_in(mod: types.ModuleType, token: str) -> bool:
    head, _, tail = token.partition(".")
    if _is_dunder(head) or (tail and _is_dunder(tail)):
        return False
    if head not in _toplevel_names(mod) or head not in vars(mod):
        return False
    obj = vars(mod)[head]
    if inspect.isclass(obj) or inspect.isfunction(obj):
        if obj.__module__ != mod.__name__:
            return False
    if not tail:
        return True
    if not inspect.isclass(obj):
        return False
    own = vars(obj).get(tail)
    return isinstance(own, (types.FunctionType, staticmethod, classmethod))


def _units(text: str) -> list[str]:
    """The doc split into paragraphs and list items; a heading is its own unit."""
    units: list[list[str]] = [[]]
    for line in text.splitlines():
        if not line.strip():
            units.append([])
        elif line.startswith(("- ", "#")):
            units.append([line])
        else:
            units[-1].append(line)
    return ["\n".join(u) for u in units if u]


def unresolved(text: str, root: Path = ROOT) -> list[str]:
    """Every backticked token of `text` that is neither a symbol defined in the module its unit names, nor an existing file."""
    bad = []
    for unit in _units(text):
        tokens = re.findall(r"`([^`\n]+)`", unit)
        paths = [t for t in tokens if t in MODULES] or [DEFAULT_PATH]
        for token in tokens:
            if "/" in token:
                if not (root / token).is_file():
                    bad.append(token)
            elif not (IDENT.fullmatch(token) and any(_defined_in(MODULES[p], token) for p in paths)):
                bad.append(token)
    return bad


def test_every_symbol_and_path_the_doc_names_exists_where_it_says():
    assert unresolved(DOC.read_text(encoding="utf-8")) == []


def test_the_doc_still_points_at_the_reader_code_for_the_field_rules():
    names = set(re.findall(r"`([^`\n]+)`", DOC.read_text(encoding="utf-8")))
    for symbol in ("BODY_FIELDS", "decode_snap", "PSEUDO_PREFIX", "DISCOVER_ID", "ENTITY_ID",
                   "Cockpit._fresh_of", "Cockpit._worker_health", "Cockpit._attached_snap", "_ChangeProbe.change_seq",
                   "Dir", "GitRefs", "LOCK_HOLD_MAX_S", "DERIVE_TIMEOUT_S"):
        assert symbol in names, f"the doc no longer points at {symbol}"


def test_the_doc_states_none_of_the_claims_the_reviewers_proved_false():
    text = " ".join(DOC.read_text(encoding="utf-8").split()).lower()
    for phrase, why in DELETED_FALSE_CLAIMS:
        assert phrase not in text, f"the doc says {phrase!r} again, which is false: {why} (L1/L2 reviews of the first cut)"


def _scratch_tree(tmp_path: Path) -> Path:
    for rel in (*MODULES, "docs/real.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    return tmp_path


def test_the_check_goes_red_on_a_pointer_that_rotted(tmp_path):
    root = _scratch_tree(tmp_path)
    scratch = (
        "Names `BODY_FIELDS` and `Cockpit._fresh_of` (live, in `levain/cockpit/snapshot_host.py` and "
        "`levain/cockpit/engine.py`), `docs/real.md` (a file that exists),\n"
        "then `BODY_FIELDS_GONE`, `Cockpit._no_such_method`, `NoSuchClass.attr`, `docs/gone.md`, `not a symbol`.\n"
    )
    assert unresolved(scratch, root=root) == [
        "BODY_FIELDS_GONE", "Cockpit._no_such_method", "NoSuchClass.attr", "docs/gone.md", "not a symbol",
    ]
    assert unresolved("Only `BODY_FIELDS` and `decode_snap` in `levain/cockpit/snapshot_host.py`.", root=root) == []


def test_the_check_goes_red_on_a_name_that_is_imported_a_dunder_or_inherited(tmp_path):
    root = _scratch_tree(tmp_path)
    # derive.py imports Any and Path and the names encode_body and KNOWN_STATUSES; snapshots.py imports os
    assert unresolved("In `levain/cockpit/derive.py`: `Any`, `Path`, `encode_body`, `KNOWN_STATUSES`.", root=root) == [
        "Any", "Path", "encode_body", "KNOWN_STATUSES",
    ]
    assert unresolved("`os`, `sqlite3` and `time` are modules.", root=root) == ["os", "sqlite3", "time"]
    # snapshot_host.py imports DB_FILENAME; snapshots.py defines it
    assert unresolved("In `levain/cockpit/snapshot_host.py`: `DB_FILENAME`.", root=root) == ["DB_FILENAME"]
    assert unresolved("`DB_FILENAME`, `SnapshotWriter.publish`, `ContractMismatch`.", root=root) == []
    # a dunder, and a method the class only inherits from its base
    assert unresolved("`SnapshotWriter.__init__`, `SnapshotWriter.__doc__`, `__name__`.", root=root) == [
        "SnapshotWriter.__init__", "SnapshotWriter.__doc__", "__name__",
    ]
    assert unresolved("`ContractMismatch.with_traceback`, `WriterLocked.args`.", root=root) == [
        "ContractMismatch.with_traceback", "WriterLocked.args",
    ]


def test_the_check_goes_red_on_a_name_that_lives_in_another_module_than_the_one_named(tmp_path):
    root = _scratch_tree(tmp_path)
    assert unresolved("In `levain/cockpit/snapshots.py`: `Cockpit._fresh_of`.", root=root) == ["Cockpit._fresh_of"]
    assert unresolved("In `levain/cockpit/engine.py`: `Cockpit._fresh_of`.", root=root) == []
    # a class attribute that is not a function is not a method
    assert unresolved("`SnapshotWriter.publish` and `PanelSnap.gen`.", root=root) == ["PanelSnap.gen"]
