"""scripts/rename_intention.py: the pure rewrite, the per-file transform (stored-token rules, the anneal seam, the
refusals), and the tree run (dry-run by default, idempotent, collisions refuse), each on a tiny throwaway git repo.

This file is exempt from the codemod (it names the old word on purpose)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("rename_intention", ROOT / "scripts" / "rename_intention.py")
ri = importlib.util.module_from_spec(_spec)
sys.modules["rename_intention"] = ri
_spec.loader.exec_module(ri)


# --- helpers ----------------------------------------------------------------------------------------------------

def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                           *args], check=True, capture_output=True, text=True).stdout


def _repo(root: Path, files: dict[str, str]) -> Path:
    _write_tree(root, files)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}


def _tx(rel: str, text: str, rules=None) -> "ri.FileOut":
    return ri.transform_file(rel, text, ri.STORED_TOKEN_RULES if rules is None else rules)


# --- R2: the word rewrite ---------------------------------------------------------------------------------------

def test_the_word_keeps_its_case_plurals_ids_and_identifiers():
    got = ri.rewrite_string("Spore spore SPORE spores Spores SPORES SporeStore spore_id _spore_fire buildSporeVerbs __SPORES__ spore-1680")
    assert got == ("Intention intention INTENTION intentions Intentions INTENTIONS IntentionStore intention_id _intention_fire "
                   "buildIntentionVerbs __INTENTIONS__ intention-1680")


def test_an_article_before_the_word_follows_it():
    assert ri.rewrite_string("a spore, A spore, a Spore and a SporeStore, data spore") == \
        "an intention, An intention, an Intention and an IntentionStore, data intention"


def test_a_longer_word_or_an_odd_case_is_not_the_word():
    keep = "sporeless unspore sporeadic"
    assert ri.rewrite_string(keep) == keep
    out = _tx("a.md", "the sPore and a sporeless one\n")
    assert out.text == "the sPore and a sporeless one\n" and out.left == ["a.md:1: sPore"]


def test_the_new_path_is_derived_from_the_same_rewrite():
    assert ri.rewrite_path("levain/spores.py") == "levain/intentions.py"
    assert ri.rewrite_path("tests/test_spore866_install_path_escaping.py") == "tests/test_intention866_install_path_escaping.py"
    assert ri.rewrite_path("levain/templates/seed/spore_instructions.md") == "levain/templates/seed/intention_instructions.md"
    assert ri.rewrite_path("levain/cli.py") == "levain/cli.py"


def test_the_intent_concept_is_never_touched():
    src = "class RootIntent: pass\nintent_provenance = 1\nx = IntentionStale()\n# intentionally a spore\n"
    out = _tx("levain/x.py", src)
    assert out.text == "class RootIntent: pass\nintent_provenance = 1\nx = IntentionStale()\n# intentionally an intention\n"


def test_a_token_holding_both_concepts_is_refused_and_left():
    out = _tx("levain/x.py", "spore_intent = 1\nintention_spore = 2\nok_spore = 3\n")
    assert out.text == "spore_intent = 1\nintention_spore = 2\nok_intention = 3\n"
    assert len([r for r in out.refusals if "holds both" in r]) == 2


# --- R3: the never-touched set ------------------------------------------------------------------------------------

@pytest.mark.parametrize("rel,expect", [
    ("project_memory/x.md", True), ("state/log.jsonl", True), ("tests/fixtures/spores_frozen.json", True),
    ("tests/golden/a.json", True), ("docs/cockpit/history/old.md", True), ("img/logo.png", True), ("x/store.db", True),
    ("scripts/rename_intention.py", True), ("levain/intention_legacy.py", True),
    ("levain/spores.py", False), ("README.md", False), ("docs/contracts.md", False),
])
def test_the_never_touched_table(rel, expect):
    assert (ri.never_touched(rel) is not None) is expect


# --- R4: stored and wire tokens -------------------------------------------------------------------------------------

STORED_SRC = (
    '"""The spore store."""\n'
    'KIND = "spore_touch"\n'
    'PREFIX = "spore:"\n'
    'KEY = "spores"\n'
    'FILE = "memory.spores.json"\n'
    'MSG = "a spore is one open loop"\n'
)


def test_a_stored_looking_literal_no_rule_covers_is_listed_as_needs_rule():
    out = _tx("levain/x.py", STORED_SRC, rules=[])
    kinds = {(n.kind, n.excerpt) for n in out.needs}
    assert kinds == {("wire-token", '"spore_touch"'), ("wire-token", '"spore:"'), ("bare-word", '"spores"'),
                     ("store-file", "memory.spores.json")}
    assert all(n.suggest for n in out.needs)
    assert 'MSG = "an intention is one open loop"' in out.text       # prose is rewritten, not listed


def test_a_docstring_and_a_comment_are_prose_even_with_a_wire_looking_word():
    out = _tx("levain/x.py", '"""the spore_touch verb"""\n# spore_touch\nX = 1\n', rules=[])
    assert not out.needs and out.text == '"""the intention_touch verb"""\n# intention_touch\nX = 1\n'


def test_a_name_in_dunder_all_follows_its_definition_and_is_not_listed():
    src = '__all__ = [\n    "guarded_spore_store",\n    "SporeThing",\n]\n__all__ += ["spore_x"]\nKIND = "spore_x"\n'
    out = _tx("levain/x.py", src, rules=[])
    assert out.text.startswith('__all__ = [\n    "guarded_intention_store",\n    "IntentionThing",\n]\n__all__ += ["intention_x"]\n')
    assert [n.excerpt for n in out.needs] == ['"spore_x"']


def test_a_keep_rule_keeps_the_bytes_and_a_rewrite_rule_approves_the_rewrite():
    rules = [ri.Rule(r"spore_touch", "keep-as-legacy-constant", "a signed verb id"),
             ri.Rule(r"^\"spores\"$", "rewrite", "a label"),
             ri.Rule(r"spore:", "keep-as-legacy-constant", "a row prefix"),
             ri.Rule(r"memory\.spores\.json", "keep-as-legacy-constant", "the store file name")]
    out = _tx("levain/x.py", STORED_SRC, rules=rules)
    assert not out.needs
    assert 'KIND = "spore_touch"' in out.text and 'PREFIX = "spore:"' in out.text
    assert 'KEY = "intentions"' in out.text and 'FILE = "memory.spores.json"' in out.text
    assert sorted(a for _, a, _, _ in out.rule_hits) == ["keep", "keep", "keep", "rewrite"]


def test_a_refuse_rule_stops_the_run_and_lists_the_site():
    out = _tx("levain/x.py", 'X = "spore_touch"\n', rules=[ri.Rule(r"spore_touch", "refuse", "decide by hand")])
    assert any("rule 0 refuses" in r and "decide by hand" in r for r in out.refusals)


def test_a_constant_replaces_a_whole_plain_literal_and_the_import_is_added():
    rules = [ri.Rule(r"spore_touch", "keep-as-legacy-constant", "a signed verb id", const="VERB_TOUCH")]
    src = '"""doc"""\nfrom __future__ import annotations\n\nimport os\n\nV = "spore_touch"\nT = ("spore_touch", 1)\n'
    out = _tx("levain/x.py", src, rules=rules)
    assert out.text == ('"""doc"""\nfrom __future__ import annotations\n\nimport os\nfrom levain.intention_legacy import VERB_TOUCH\n'
                        '\nV = VERB_TOUCH\nT = (VERB_TOUCH, 1)\n')
    assert out.legacy == {"VERB_TOUCH": "spore_touch"}


def test_a_constant_never_stands_in_a_match_pattern_a_literal_type_an_f_string_or_a_concatenation():
    rules = [ri.Rule(r"spore_\w+", "keep-as-legacy-constant", "verbs", const="C")]
    src = ('from typing import Literal\n'
           'def f(k: Literal["spore_a"], x):\n'
           '    match x:\n'
           '        case "spore_b":\n'
           '            pass\n'
           '    return f"spore_c {x}", "spore_d" "e"\n')
    out = _tx("levain/x.py", src, rules=rules)
    assert out.text == src and not out.legacy and not out.needs


def test_two_values_for_one_constant_refuse():
    rules = [ri.Rule(r"spore_\w+", "keep-as-legacy-constant", "verbs", const="C")]
    out = _tx("levain/x.py", 'A = "spore_a"\nB = "spore_b"\n', rules=rules)
    assert any("would hold two values" in r for r in out.refusals)


def test_a_replacement_that_keeps_the_word_is_protected_from_a_second_pass_and_takes_no_group_reference():
    rule = ri.Rule(r"X = (\d)", "rewrite", "reads both", replacement="X = old_spore_ok", always=True, paths=("*.py",))
    once = _tx("a.py", "X = 1\n", [rule]).text
    assert once == "X = old_spore_ok\n" and _tx("a.py", once, [rule]).text == once
    with pytest.raises(ValueError):
        ri.Rule(r"(a)", "rewrite", "x", replacement=r"\1 spore", always=True, paths=("*.py",))


def test_an_always_rule_needs_paths_and_renames_a_token_that_holds_no_spore_only_there():
    with pytest.raises(ValueError):
        ri.Rule(r"seed", "rewrite", "x", replacement="someday", always=True)
    rules = [ri.Rule(r'"seed"', "rewrite", "the stored disposition", paths=("levain/spores.py", "levain/tray*.py"),
                     replacement='"someday"', always=True)]
    assert _tx("levain/spores.py", 'D = "seed"\nTEXT = "a seed doc"\n', rules).text == 'D = "someday"\nTEXT = "a seed doc"\n'
    assert _tx("levain/other.py", 'D = "seed"\n', rules).text == 'D = "seed"\n'
    assert _tx("levain/tray_x.py", 'D = "seed"\n', rules).text == 'D = "someday"\n'


def test_the_id_resolver_reads_both_prefixes_by_default():
    src = 'import re\nINTENTION_ID = re.compile(r"spore-[0-9A-Za-z_-]{1,56}")\n'
    out = _tx("levain/consent/questions.py", src)
    assert 'INTENTION_ID = re.compile(r"(?:intention|spore)-[0-9A-Za-z_-]{1,56}")' in out.text
    assert _tx("levain/consent/questions.py", out.text).text == out.text


# --- R5: the anneal seam -------------------------------------------------------------------------------------------

def test_the_anneal_import_keeps_its_module_path_and_takes_the_new_names():
    src = ("from anneal_memory.spores import ApplyRefused, SporeError, SporeStore as S, normalize_spore_field, spore_version\n"
           "class G(S):\n    pass\n"
           "def f():\n    from anneal_memory.spores import (\n        SporeApply,\n        SporeOp,\n        SporeType,\n    )\n")
    out = _tx("levain/x.py", src)
    assert out.text.splitlines()[0] == ("from anneal_memory.spores import ApplyRefused, IntentionError, IntentionStore as S, "
                                         "normalize_intention_field, intention_version")
    assert "        IntentionApply,\n        IntentionOp,\n        IntentionType,\n" in out.text
    assert out.seam_imports == 7 and not out.refusals and not out.needs


def test_an_anneal_name_with_no_alias_refuses():
    out = _tx("levain/x.py", "from anneal_memory.spores import SporeFrobnicate\n")
    assert any("SporeFrobnicate has no mapped alias" in r for r in out.refusals)


def test_a_store_construction_passes_the_id_prefix_and_a_second_pass_changes_nothing():
    src = ("from anneal_memory.spores import SporeStore\n"
           "a = SporeStore(path)\n"
           "b = SporeStore(path, lock=True,)\n"
           "c = SporeStore()\n"
           "d = SporeStore(path, id_prefix='x')\n"
           "e = sp.SporeStore(path).get(1)\n"
           "f = GuardedSporeStore(path)\n"
           "g = _GUARDED(path)\n"
           "h = SporeStore(\n    path,\n)\n")
    out = _tx("levain/x.py", src)
    lines = out.text.splitlines()
    assert lines[1] == 'a = IntentionStore(path, id_prefix="intention")'
    assert lines[2] == 'b = IntentionStore(path, lock=True, id_prefix="intention",)'
    assert lines[3] == 'c = IntentionStore(id_prefix="intention")'
    assert lines[4] == "d = IntentionStore(path, id_prefix='x')"
    assert lines[5] == 'e = sp.IntentionStore(path, id_prefix="intention").get(1)'
    assert lines[6] == 'f = GuardedIntentionStore(path, id_prefix="intention")'
    assert lines[7] == 'g = _GUARDED(path, id_prefix="intention")'
    assert out.seam_calls == 7
    assert _tx("levain/x.py", out.text).text == out.text


def test_other_anneal_module_uses_are_listed_and_the_module_path_is_kept_in_prose():
    src = ("import anneal_memory.spores\n"
           "from anneal_memory import spores\n"
           'T = "anneal_memory.spores.SporeStore"\n'
           "# see anneal_memory.spores for the spore store\n")
    out = _tx("levain/x.py", src, rules=[])
    assert sorted(n.kind for n in out.needs) == ["anneal-module", "anneal-module-import", "anneal-module-import"]
    assert out.text.endswith("# see anneal_memory.spores for the intention store\n")
    assert 'T = "anneal_memory.spores.IntentionStore"' in out.text


# --- R8: the guards -------------------------------------------------------------------------------------------------

def test_a_file_that_does_not_parse_is_refused_untouched():
    out = _tx("levain/x.py", "def f(:\n    spore = 1\n")
    assert out.text == "def f(:\n    spore = 1\n" and any("does not parse before" in r for r in out.refusals)


def test_a_rewrite_that_breaks_the_syntax_is_refused():
    rules = [ri.Rule(r"X = 1", "rewrite", "broken on purpose", replacement="X = (", always=True, paths=("levain/*",))]
    out = _tx("levain/x.py", "X = 1\nspore = 2\n", rules=rules)
    assert any("does not parse after" in r for r in out.refusals)


def test_a_rewrite_that_breaks_json_is_refused():
    rules = [ri.Rule(r'"k"', "rewrite", "broken on purpose", replacement='"k', always=True, paths=("*.json",))]
    out = _tx("a.json", '{"k": 1, "spore": 2}\n', rules=rules)
    assert any("no longer valid JSON" in r for r in out.refusals)


# --- the tree run -----------------------------------------------------------------------------------------------

def _tree() -> dict[str, str]:
    return {
        "levain/__init__.py": "",
        "levain/spores.py": ('"""The spore vocabulary."""\nfrom __future__ import annotations\n\n'
                             'from anneal_memory.spores import SporeStore\n\n'
                             'def open_spores(path):\n    """Open a spore store; a spore is one loop."""\n    return SporeStore(path)\n'),
        "levain/consumer.py": "from levain.spores import open_spores\n\nclass RootIntent:\n    pass\n\nX = open_spores(1)  # spore-751 cites it\n",
        "levain/templates/seed/spore_instructions.md": "# Your Open Loops — Spores\n\nA spore is one open loop.\n",
        "levain/templates/web/v.js": "// the spore view\nvar a = 1;\n",
        "tests/test_spores.py": "from levain.spores import open_spores\n\ndef test_a_spore():\n    assert open_spores\n",
        "tests/fixtures/spores_frozen.json": '{"spores": []}\n',
        "project_memory/notes.md": "spore-751 was ruled\n",
        "state/log.jsonl": '{"id": "spore-1"}\n',
        "docs/cockpit/history/old.md": "a spore\n",
        "README.md": "# R\n\nThe spore store lives in the spore list.\n",
        "CHANGELOG.md": ("# Changelog\n\n## [Unreleased]\n\n### Added\n- a new spore verb.\n\n"
                         "## [0.1]\n\n### Added\n- the first spore.\n"),
    }


def test_a_dry_run_changes_nothing_and_says_so(tmp_path, capsys):
    _repo(tmp_path, _tree())
    before = _snapshot(tmp_path)
    assert ri.main(["--root", str(tmp_path)]) == 0
    assert _snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "R1 moves: 3 files" in out
    assert "levain/spores.py -> levain/intentions.py" in out


def test_the_run_renames_then_a_second_run_changes_nothing(tmp_path, capsys):
    _repo(tmp_path, _tree())
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 0
    snap = _snapshot(tmp_path)
    # R1: paths moved with git, content carried
    assert "levain/spores.py" not in snap and "levain/intentions.py" in snap
    assert "tests/test_intentions.py" in snap and "levain/templates/seed/intention_instructions.md" in snap
    assert len([ln for ln in _git(tmp_path, "status", "--porcelain").splitlines() if ln.startswith("R")]) == 3
    # R2 + R5 on code
    mod = snap["levain/intentions.py"]
    assert '"""The intention vocabulary."""' in mod and "def open_intentions(path):" in mod
    assert "from anneal_memory.spores import IntentionStore" in mod
    assert 'return IntentionStore(path, id_prefix="intention")' in mod
    assert "a intention" not in mod and "Open a intention" not in mod and "Open an intention store; an intention is one loop." in mod
    assert snap["levain/consumer.py"] == ("from levain.intentions import open_intentions\n\nclass RootIntent:\n    pass\n\n"
                                          "X = open_intentions(1)  # intention-751 cites it\n")
    assert snap["levain/templates/seed/intention_instructions.md"] == "# Your Open Loops — Intentions\n\nAn intention is one open loop.\n"
    assert snap["levain/templates/web/v.js"] == "// the intention view\nvar a = 1;\n"
    assert snap["README.md"] == "# R\n\nThe intention store lives in the intention list.\n"
    # R3: history, fixtures, logs and past changelog entries keep the word
    assert snap["tests/fixtures/spores_frozen.json"] == '{"spores": []}\n'
    assert snap["project_memory/notes.md"] == "spore-751 was ruled\n"
    assert snap["state/log.jsonl"] == '{"id": "spore-1"}\n'
    assert snap["docs/cockpit/history/old.md"] == "a spore\n"
    assert "- a new intention verb." in snap["CHANGELOG.md"] and "- the first spore." in snap["CHANGELOG.md"]
    capsys.readouterr()
    # idempotent
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 0
    assert _snapshot(tmp_path) == snap
    assert "R1 moves: 0 files" in capsys.readouterr().out


def test_the_apply_leaves_no_spore_in_a_clean_tree(tmp_path):
    _repo(tmp_path, {"levain/a.py": "spore = 1\n", "docs/x.md": "A spore.\n", "README.md": "spores\n"})
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 0
    assert "spore" not in "".join(_snapshot(tmp_path).values()).lower()


def test_a_marked_file_is_history_and_keeps_the_word(tmp_path):
    files = {"docs/old.md": f"<!-- {ri.MARKER} -->\na spore\n", "docs/new.md": "a spore\n"}
    _repo(tmp_path, files)
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 0
    snap = _snapshot(tmp_path)
    assert snap["docs/old.md"] == files["docs/old.md"] and snap["docs/new.md"] == "an intention\n"


def test_crlf_line_endings_survive(tmp_path):
    _repo(tmp_path, {"docs/x.md": "a spore\r\nthe spores\r\n"})
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 0
    assert (tmp_path / "docs/x.md").read_bytes() == b"an intention\r\nthe intentions\r\n"


# --- the refusals on a tree -----------------------------------------------------------------------------------------

def test_an_identifier_that_already_exists_in_the_tree_refuses_and_lists_both(tmp_path, capsys):
    _repo(tmp_path, {"levain/a.py": "def _spore_fire():\n    pass\n", "levain/b.py": "def _intention_fire():\n    pass\n",
                     "levain/c.py": "x = spore_ok\n"})
    before = _snapshot(tmp_path)
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 2
    assert _snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "REFUSED" in out and "_spore_fire -> _intention_fire" in out and "### collision (1)" in out
    assert "spore_ok" not in out.split("## R8 refusals")[1].split("## NEEDS-RULE")[0]


def test_a_bare_word_is_not_a_collision(tmp_path):
    _repo(tmp_path, {"docs/a.md": "an intention and a spore\n"})
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 0


def test_a_git_mv_collision_refuses_the_whole_run(tmp_path, capsys):
    _repo(tmp_path, {"tests/test_spore_x.py": "A = 1\n", "tests/test_intention_x.py": "B = 1\n", "levain/spores.py": "C = 1\n"})
    before = _snapshot(tmp_path)
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 2
    assert _snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "tests/test_spore_x.py -> tests/test_intention_x.py (the target exists)" in out


def test_a_module_that_would_shadow_a_package_refuses(tmp_path, capsys):
    _repo(tmp_path, {"levain/spores.py": "A = 1\n", "levain/intentions/__init__.py": ""})
    assert ri.main(["--root", str(tmp_path)]) == 2
    assert "a package of that name exists" in capsys.readouterr().out


def test_a_move_into_a_path_outside_t1_refuses(tmp_path, capsys):
    _repo(tmp_path, {"scripts/rename_spore.py": "A = 1\n"})
    assert ri.main(["--root", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "### path outside T1 (1)" in out and "scripts/rename_spore.py -> scripts/rename_intention.py" in out


def test_a_needs_rule_item_blocks_apply_until_a_rule_covers_it(tmp_path, capsys):
    _repo(tmp_path, {"levain/a.py": 'KIND = "spore_touch"\n'})
    before = _snapshot(tmp_path)
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 2
    assert _snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "NEEDS-RULE (1)" in out and "--apply refuses until every item" in out and "levain/a.py:1 [wire-token]" in out
    # the same tree with a covering rule applies
    rules = [ri.Rule(r"spore_touch", "keep-as-legacy-constant", "a signed verb id", const="VERB_TOUCH")]
    plan = ri.build_plan(tmp_path, rules)
    assert not plan.blocked
    ri.apply_plan(plan)
    assert (tmp_path / "levain/a.py").read_text() == 'from levain.intention_legacy import VERB_TOUCH\nKIND = VERB_TOUCH\n'
    legacy = (tmp_path / ri.LEGACY_PATH).read_text()
    assert "VERB_TOUCH = 'spore_touch'   # a signed verb id" in legacy
    # a rerun on the applied tree: the legacy module is exempt, nothing is left to do
    assert ri.build_plan(tmp_path, rules).empty


def test_a_legacy_module_without_the_constant_refuses(tmp_path):
    files = {"levain/a.py": 'KIND = "spore_touch"\n', ri.LEGACY_PATH: "OTHER = 'x'\n"}
    _repo(tmp_path, files)
    rules = [ri.Rule(r"spore_touch", "keep-as-legacy-constant", "verb", const="VERB_TOUCH")]
    plan = ri.build_plan(tmp_path, rules)
    assert any(cat == "legacy constant" for cat, _ in plan.refusals)


# --- apply guards -----------------------------------------------------------------------------------------------

def test_apply_on_a_dirty_worktree_refuses_but_a_dry_run_does_not(tmp_path, capsys):
    _repo(tmp_path, {"docs/a.md": "a spore\n"})
    (tmp_path / "scratch.txt").write_text("dirty\n")
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 3
    assert (tmp_path / "docs/a.md").read_text() == "a spore\n"
    assert ri.main(["--root", str(tmp_path)]) == 0
    capsys.readouterr()


def test_apply_on_a_dirty_worktree_with_nothing_to_do_is_fine(tmp_path):
    _repo(tmp_path, {"docs/a.md": "an intention\n"})
    (tmp_path / "scratch.txt").write_text("dirty\n")
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 0


def test_a_plain_directory_dry_runs_but_does_not_apply(tmp_path, capsys):
    _write_tree(tmp_path, {"docs/a.md": "a spore\n"})
    assert ri.main(["--root", str(tmp_path)]) == 0
    assert ri.main(["--root", str(tmp_path), "--apply"]) == 4
    assert (tmp_path / "docs/a.md").read_text() == "a spore\n"
    capsys.readouterr()


# --- the report -----------------------------------------------------------------------------------------------------

def test_the_report_lists_counts_moves_refusals_needs_rule_and_the_seam(tmp_path):
    files = dict(_tree())
    files["levain/wire.py"] = 'KIND = "spore_touch"\n'
    files["levain/a.py"] = "def _spore_fire():\n    pass\n"
    files["levain/b.py"] = "def _intention_fire():\n    pass\n"
    _repo(tmp_path, files)
    report = tmp_path.parent / (tmp_path.name + "_report.md")
    assert ri.main(["--root", str(tmp_path), "--report", str(report)]) == 2
    text = report.read_text(encoding="utf-8")
    for needle in ("## scope", "T1 files (rewritten set):", "never touched, ", "## per rule", "R1 moves: 3 files",
                   "R2 word rewrites:", "R5 anneal seam: 1 imported names, 1 store constructions", "R8 refusals: 1; NEEDS-RULE: 1",
                   "## R1 paths moved (3)", "## R8 refusals (1)", "### collision (1)", "## NEEDS-RULE (1)", "by kind: wire-token 1",
                   "suggest pattern:", "## R5 anneal seam lines (2)", "REFUSED"):
        assert needle in text, needle
    assert "- rule 0" not in text      # the default INTENTION_ID rule did not fire in this tree
