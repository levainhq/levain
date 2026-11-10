"""scripts/rename_desk.py: the pure functions (word substitution, literal classification, stored-token interpolation),
the tree run (idempotent, dry-run by default, collisions refuse), the two files it writes (the import shim and the
legacy module) and, once the tree is renamed, the alias and the legacy schema.

This file is exempt from the codemod (it names the old word on purpose)."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("rename_desk", ROOT / "scripts" / "rename_desk.py")
rd = importlib.util.module_from_spec(_spec)
sys.modules["rename_desk"] = rd
_spec.loader.exec_module(rd)

PY = sys.executable


def _legacy_ns() -> dict:
    ns: dict = {}
    exec(compile(rd.LEGACY_SOURCE, "legacy.py", "exec"), ns)
    return ns


# --- the word substitution -------------------------------------------------------------------------------------

def test_the_word_keeps_its_case_and_leaves_the_abbreviations():
    got = rd.rename_words("Coordinator coordinator COORDINATOR CoordinatorVerbs is_coordinator_session coordinators")
    assert got == "Desk desk DESK DeskVerbs is_desk_session desks"
    keep = "CoordError CoordStore coord_settings _coord_dir coordinate coordinates coordroom"
    assert rd.rename_words(keep) == keep


def test_a_project_memory_directory_name_is_not_a_role_name():
    assert rd.rename_words("see 1020-coordinator/C0_DESIGN.md") == "see 1020-coordinator/C0_DESIGN.md"


def test_the_cli_phrase_and_the_module_names_follow():
    assert rd.rename_words("run `levain coord status` or levain coordinator") == "run `levain desk status` or levain desk"
    assert rd.rename_words("levain coord-x levain coordx") == "levain coord-x levain coordx"
    assert rd.rename_words("from levain.cockpit.coord_effects import X") == "from levain.cockpit.desk_effects import X"


def test_prose_that_names_a_stored_thing_keeps_its_spelling():
    got = rd.rename_prose("the ``coordinator_reply`` kind, coordinator_drop_effect, the coordinator-wake fact, a coordinator")
    assert got == "the ``coordinator_reply`` kind, desk_drop_effect, the coordinator-wake fact, a desk"


def test_articles():
    assert rd.rename_words("a coordinator, the coordinator's store, A Coordinator") == "a desk, the desk's store, A Desk"
    assert rd.fix_articles("an desk and An Desk and a desk") == "a desk and A Desk and a desk"


def test_a_substitution_that_collides_with_an_existing_desk_is_listed():
    out = rd.rename_words("the desk coordinator and desk.coordinator()")
    g = rd.find_glitches(out, "x.py")
    assert len(g) == 1 and "the desk desk" in g[0]
    assert rd.find_glitches("the desk is here", "x.py") == []


# --- classifying a literal ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("body,expect", [
    ("coordinator", ("whole", "ROLE_DESK")),
    ("coordinator_reply", ("whole", "KIND_DESK_REPLY")),
    ("coordinator-wake", ("whole", "FACT_DESK_WAKE")),
    ("levain-coordinator-", ("whole", "SESSION_PREFIX_DESK")),
    ("WHERE role = 'coordinator' LIMIT 1", ("interp", None)),
    ("by TEXT NOT NULL CHECK (by IN ('operator', 'coordinator'))", ("interp", None)),
    ("'coordinator')", ("interp", None)),
    ("BEGIN SELECT RAISE(ABORT, 'a coordinator setting is append-only'); END", ("interp", None)),
    ('{"role": "coordinator"}', ("interp", None)),
    ("levain-(coordinator|worker)-([0-9a-f]{32})", ("interp", None)),
    ("the coordinator holds the lease", ("prose", None)),
    ("check where the coordinator is", ("prose", None)),                 # lower-case "check where" is not SQL
    ("the role 'coordinator' is a word here", ("prose", None)),          # quoted, but not in a SQL position
])
def test_classify_literal(body, expect):
    kind, why = rd.classify_literal(body, quote="'" if body.startswith("{") else '"')
    assert kind == expect[0]
    if expect[1]:
        assert why == expect[1]


def test_a_docstring_is_prose_even_with_sql_in_it():
    assert rd.classify_literal("WHERE role = 'coordinator'", docstring=True)[0] == "prose"


# --- the Python transform: what it writes must equal the old text -----------------------------------------------

def _eval_assigned(src: str, name: str):
    ns = _legacy_ns()
    # the transform imports from levain.desk.legacy: serve that import from the legacy namespace
    import types
    mod = types.ModuleType("levain.desk.legacy")
    mod.__dict__.update(ns)
    sys.modules.setdefault("levain", types.ModuleType("levain"))
    sys.modules.setdefault("levain.desk", types.ModuleType("levain.desk"))
    sys.modules["levain.desk.legacy"] = mod
    out: dict = {}
    exec(compile(src, "t.py", "exec"), out)
    return out[name]


def test_schema_text_is_byte_identical_after_the_transform():
    src = (
        'from __future__ import annotations\n'
        'import os\n'
        '\n'
        'SCHEMA = (\n'
        '    "CREATE TABLE t (by TEXT CHECK (by IN (\'operator\', \'coordinator\')), n INTEGER DEFAULT {1}), "\n'
        '    "CHECK ((a = \'coordinator\') = (b = \'coordinator\')), "\n'
        '    "BEGIN SELECT RAISE(ABORT, \'the coordinator mode is the coordinator\'\'s\'); END",\n'
        ')\n'
        'ROLE = "coordinator"\n'
        'KIND = \'coordinator_reply\'\n'
        'PAT = r"levain-(coordinator|worker)-([0-9a-f]{32})"\n'
        'FS = f"WHEN NEW.kind = \'coordinator_reply\' AND x = {os.sep!r} {{y}}"\n'
    )
    res = rd.transform_python("levain/coordinator/x.py", src)
    assert "from levain.desk.legacy import" in res.text
    assert "coordinator" not in res.text.replace("levain.coordinator", "")        # nothing left as code
    # evaluate the OLD source and the NEW one: every assigned value is identical
    for name in ("SCHEMA", "ROLE", "KIND", "PAT", "FS"):
        old = {}
        exec(compile(src, "o.py", "exec"), old)
        assert _eval_assigned(res.text, name) == old[name], name
    assert {r for _, _, r in res.stored}                                           # and every literal is listed


def test_a_prose_literal_is_renamed_and_a_docstring_too():
    src = '"""The coordinator."""\nMSG = "the coordinator holds it"\nHELP = "levain coord status"\n'
    res = rd.transform_python("m.py", src)
    assert res.text == '"""The desk."""\nMSG = "the desk holds it"\nHELP = "levain desk status"\n'
    assert res.stored == [] and not res.consts


def test_a_stored_token_in_a_formatted_literal_is_left_for_a_hand():
    src = 'X = "WHERE role = \'coordinator\' {0}".format(1)\n'
    res = rd.transform_python("m.py", src)
    assert res.manual and "format" in res.manual[0][1]


def test_the_import_goes_after_the_import_block_and_a_form_feed_does_not_shift_it():
    src = '"""doc"""\nfrom __future__ import annotations\n\nimport os\n\x0c\nROLE = "coordinator"\n'
    res = rd.transform_python("m.py", src)
    lines = res.text.split("\n")
    assert lines.index("from levain.desk.legacy import ROLE_DESK") == lines.index("import os") + 1
    assert "ROLE = ROLE_DESK" in res.text


# --- the resolver and the shim ------------------------------------------------------------------------------------

def test_desk_home_resolves_new_then_old_then_new(tmp_path):
    ns = _legacy_ns()
    f = ns["desk_home"]
    assert f(tmp_path) == tmp_path / "desk" and not (tmp_path / "desk").exists()           # nothing created
    (tmp_path / "coordinator").mkdir()
    assert f(tmp_path) == tmp_path / "coordinator"
    (tmp_path / "desk").mkdir()
    assert f(tmp_path) == tmp_path / "desk"
    other = tmp_path / "fresh"
    other.mkdir()
    assert f(other, create=True) == other / "desk" and (other / "desk").is_dir()


def test_the_shim_resolves_the_old_import_path_to_the_same_objects(tmp_path):
    pkg = tmp_path / "levain"
    (pkg / "desk").mkdir(parents=True)
    (pkg / "coordinator").mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "desk" / "__init__.py").write_text("")
    (pkg / "desk" / "room_host.py").write_text("class HostCommands:\n    pass\n")
    (pkg / "coordinator" / "__init__.py").write_text(rd.SHIM_SOURCE)
    code = ("import levain.coordinator\n"
            "from levain.coordinator.room_host import HostCommands\n"
            "import levain.desk.room_host as r\n"
            "import sys\n"
            "assert HostCommands is r.HostCommands\n"
            "assert sys.modules['levain.coordinator.room_host'] is r\n"
            "try:\n    import levain.coordinator.nothing_here\nexcept ImportError:\n    print('ok')\n")
    out = subprocess.run([PY, "-c", code], cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)},
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


# --- the tree run -----------------------------------------------------------------------------------------------

def _tree(root: Path) -> None:
    files = {
        "levain/__init__.py": "",
        "levain/coordinator/__init__.py": '"""The coordinator package."""\n',
        "levain/coordinator/store.py": ('"""The coordinator store."""\nfrom __future__ import annotations\n\n'
                                        'ROLE = "coordinator"\nSQL = "WHERE role = \'coordinator\'"\n\n'
                                        'def is_coordinator_session():\n    return "the coordinator"\n'),
        "levain/cockpit/coordinator_panels.py": "from levain.coordinator.store import ROLE\n",
        "levain/cockpit/coord_effects.py": "X = 1\n",
        "levain/contracts/namespaces.py": 'OWNER = "levain/coordinator/envelope.py"\n',
        "levain/templates/web/v.js": 'var a = "coordinator"; // the coordinator view\n',
        "tests/test_coordinator_store.py": "from levain.coordinator.store import ROLE\n",
        "tests/test_cockpit_coord_drop.py": "from levain.cockpit.coord_effects import X\n",
        "tests/golden/contracts/frozen/namespaces.json": '{"owner": "levain/coordinator/envelope.py"}\n',
        "tests/fixtures/old.txt": "coordinator stays\n",
        "README.md": "# R\n\nThe desk is the session that holds the coordinator role (`levain coord status`).\n",
        "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n### Changed\n- an older entry about the coordinator.\n\n## [0.1]\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def test_a_dry_run_changes_nothing(tmp_path, capsys):
    _tree(tmp_path)
    before = _snapshot(tmp_path)
    assert rd.main(["--root", str(tmp_path)]) == 0
    assert _snapshot(tmp_path) == before
    assert "DRY RUN" in capsys.readouterr().out


def test_the_run_renames_then_a_second_run_changes_nothing(tmp_path, capsys):
    _tree(tmp_path)
    assert rd.main(["--root", str(tmp_path), "--apply"]) == 0
    snap = _snapshot(tmp_path)
    # R1 paths
    assert "levain/desk/store.py" in snap and "levain/cockpit/desk_panels.py" in snap
    assert "levain/cockpit/desk_effects.py" in snap and "tests/test_desk_store.py" in snap
    assert "tests/test_cockpit_desk_drop.py" in snap
    # R2 words and R4 stored tokens
    store = snap["levain/desk/store.py"]
    assert "ROLE = ROLE_DESK" in store and "from levain.desk.legacy import" in store and "is_desk_session" in store
    assert 'return "the desk"' in store and "{SQL_ROLE_DESK}" in store
    assert snap["levain/cockpit/desk_panels.py"] == "from levain.desk.store import ROLE\n"
    assert snap["tests/test_cockpit_desk_drop.py"] == "from levain.cockpit.desk_effects import X\n"
    # R8, exempt files, the README sentence, the CHANGELOG
    assert snap["levain/contracts/namespaces.py"] == 'OWNER = "levain/desk/envelope.py"\n'
    assert snap["tests/golden/contracts/frozen/namespaces.json"] == '{"owner": "levain/desk/envelope.py"}\n'
    assert snap["tests/fixtures/old.txt"] == "coordinator stays\n"
    assert "holds the lease on your drops (`levain desk status`)" in snap["README.md"]
    assert "an older entry about the coordinator." in snap["CHANGELOG.md"] and rd.CHANGELOG_SENTINEL in snap["CHANGELOG.md"]
    # JS keeps its wire literal and says so; prose in it is renamed
    assert 'var a = "coordinator"; // the desk view' in snap["levain/templates/web/v.js"]
    # the two files the codemod writes
    assert "def desk_home" in snap["levain/desk/legacy.py"] and "_AliasFinder" in snap["levain/coordinator/__init__.py"]
    # the DONE check: only legacy.py, the shim and the JS wire literal remain
    assert rd.done_check(tmp_path) == ["levain/templates/web/v.js"]
    capsys.readouterr()
    # idempotent
    assert rd.main(["--root", str(tmp_path), "--apply"]) == 0
    assert _snapshot(tmp_path) == snap
    assert "R1 moves: 0 files" in capsys.readouterr().out


def test_a_name_collision_refuses_and_lists_it(tmp_path, capsys):
    _tree(tmp_path)
    (tmp_path / "tests" / "test_desk_store.py").write_text("# already here\n")
    before = _snapshot(tmp_path)
    assert rd.main(["--root", str(tmp_path), "--apply"]) == 2
    assert _snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "REFUSED" in out and "tests/test_coordinator_store.py -> tests/test_desk_store.py" in out


# --- after the rename: the alias and the legacy store -----------------------------------------------------------

needs_desk = pytest.mark.skipif(importlib.util.find_spec("levain.desk") is None, reason="the tree is not renamed yet")


@needs_desk
def test_the_alias_runs_and_the_canonical_verb_lists_its_verbs():
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    desk = subprocess.run([PY, "-m", "levain", "desk", "--help"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert desk.returncode == 0 and "status" in desk.stdout and "release" in desk.stdout
    for alias in ("coord", "coordinator"):
        r = subprocess.run([PY, "-m", "levain", alias, "--help"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and "alias" in r.stdout
    top = subprocess.run([PY, "-m", "levain", "--help"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120).stdout
    assert not re.search(r"\bdesk \(coord", top)                                    # the aliases are not in the verb list


@needs_desk
def test_the_new_code_writes_the_pre_rename_schema(tmp_path):
    """A fresh store opened by the renamed code has the schema text of the pre-rename fixture, byte for byte (the
    fixture is scripts/make_desk_legacy_fixture.py's output, committed before the rename)."""
    import sqlite3
    fixture = ROOT / "tests" / "fixtures" / "desk_legacy_store" / "schema.sql.txt"
    if not fixture.exists():
        pytest.skip("the pre-rename schema fixture is not committed")
    store = tmp_path / "fresh"
    store.mkdir(mode=0o700)
    r = subprocess.run([PY, "-m", "levain", "desk", "take", "--queue-dir", str(store)], cwd=ROOT,
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(f"file:{store / 'dispatch.db'}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    finally:
        conn.close()
    have = "\n".join(f"{t} {n}\n{sql};" for t, n, sql in sorted(rows)) + "\n"
    assert have == fixture.read_text(encoding="utf-8")
