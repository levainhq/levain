"""The rename's install-side behaviour, pinned against what the OLD release actually wrote (H6, H7, H9).

The tests that matter here cannot be written from the new code alone: an old install is the thing the rename has to
survive. Each builds a REAL install by running the OLD release's own `levain init` (the release tree is extracted from git,
never imitated), then lets this tree's code act on it.

  H6  the seed doc moved (`seed/spore_instructions.md` -> `seed/intention_instructions.md`): `levain update` on an old
      install installs the new one without holding the carrier, retires the old copy only when it is provably levain's
      own unedited one, and says so when it is not.
  H7  `seed` is two words. The install's `seed/` directory and everything else that is not the disposition word stays
      byte-identical to what the old release writes.
  H9  the three hook files of an adapter call each other BY NAME, so they move together or not at all; an old hook next to
      this package is exercised for real.

They skip when git cannot supply the old release (a shallow clone without it): the skip is the only reason they would not run.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import levain.install as install_mod
from levain.install import (
    ADAPTER_RECEIPT_REL,
    PENDING_REL,
    activation_receipt_path,
    refresh_adapter,
)
from levain.intention_legacy import OLD_SEED_DOC

REPO = Path(__file__).resolve().parents[1]
OLD_REV = "a8617e55c"  # the last release before the rename codemod
NEW_DOC = "seed/intention_instructions.md"
HOOK_FILES = ("_levain_hook.py", "session_start.py", "user_prompt_submit.py")
COUPLED = tuple(f"hooks/{n}" for n in HOOK_FILES)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# the old release, extracted from git, and real installs made by it and by this tree
# ---------------------------------------------------------------------------------------------------------------------

def _env(tree: Path, home: Path, codex_home: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(PYTHONPATH=str(tree), HOME=str(home), CODEX_HOME=str(codex_home), LEVAIN_HOME=str(home / ".levain"),
               **extra)
    return env


def _levain(tree: Path, home: Path, *argv: str, **extra: str) -> subprocess.CompletedProcess:
    """`python -m levain <argv>` running THAT tree's code (cwd and PYTHONPATH both pinned to it)."""
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, "-m", "levain", *argv], cwd=tree, capture_output=True, text=True,
                          env=_env(tree, home, home / "codex-home", **extra), timeout=300)


@pytest.fixture(scope="module")
def old_tree(tmp_path_factory) -> Path:
    probe = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{OLD_REV}^{{commit}}"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"git has no {OLD_REV}, so the old release cannot be extracted")
    root = tmp_path_factory.mktemp("old_release")
    blob = subprocess.run(["git", "-C", str(REPO), "archive", OLD_REV, "levain", "pyproject.toml"],
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(root, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(root)
    return root


@pytest.fixture(scope="module")
def answers_file(old_tree, tmp_path_factory) -> Path:
    r = _levain(old_tree, tmp_path_factory.mktemp("tpl_home"), "init", "--answers-template")
    assert r.returncode == 0, r.stderr
    answers = {slot: "" for slot in json.loads(r.stdout)}
    answers.update({"COGNITION": "Plays first.", "WORK": "Levain.", "COMMUNICATION": "Direct.",
                    "BOUNDARIES": "Separate.", "OPERATOR_NAME": "Chris", "LOCATION": "Riverton",
                    "ROLES": "- Builder", "INTERESTS": "Baking", "ENTITY_NAME": "Ada",
                    "SUBSTRATE": "an open model via Ollama", "JOB": "Shipping v2"})
    path = tmp_path_factory.mktemp("answers") / "answers.json"
    path.write_text(json.dumps(answers), encoding="utf-8")
    return path


def _init(tree: Path, install: Path, answers: Path, home: Path, adapter: str = "claude-code") -> str:
    install.parent.mkdir(parents=True, exist_ok=True)
    r = _levain(tree, home, "init", "--path", str(install), "--adapter", adapter, "--answers", str(answers))
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    return r.stdout


@pytest.fixture
def old_install(old_tree, answers_file, tmp_path, monkeypatch) -> Path:
    """An install made by the OLD release, with this process pointed at a scratch home so nothing touches the real one."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home" / "codex-home"))
    install = tmp_path / "entity"
    _init(old_tree, install, answers_file, tmp_path / "home")
    return install.resolve()


def _update(install: Path, *, apply: bool = True) -> tuple[install_mod.AdapterRefresh, str]:
    lines: list[str] = []
    out = refresh_adapter(install, apply=apply, emit=lines.append)
    return out, "\n".join(lines)


def _receipt(install: Path) -> dict[str, str]:
    return json.loads(install.joinpath(*ADAPTER_RECEIPT_REL).read_text())["files"]


def _write_receipt(install: Path, files: dict[str, str]) -> None:
    install.joinpath(*ADAPTER_RECEIPT_REL).write_text(json.dumps({"schema": 1, "files": files}), encoding="utf-8")


def _backups(install: Path) -> list[bytes]:
    where = install / ".levain" / "backups" / "seed"
    return sorted(p.read_bytes() for p in where.iterdir()) if where.is_dir() else []


def _new_doc_bytes() -> bytes:
    return (REPO / "levain" / "templates" / "seed" / "intention_instructions.md").read_bytes()


# ---------------------------------------------------------------------------------------------------------------------
# H6: the seed doc
# ---------------------------------------------------------------------------------------------------------------------

def test_the_old_release_really_installed_the_old_doc_and_pointed_the_carrier_at_it(old_install):
    # the fixture's premise, asserted: without it every H6 test below would pass against nothing
    assert (old_install / OLD_SEED_DOC).is_file()
    assert not (old_install / NEW_DOC).exists()
    assert OLD_SEED_DOC in _receipt(old_install)
    assert f"`{OLD_SEED_DOC}`" in (old_install / "CLAUDE.md").read_text()


def test_the_released_hashes_of_the_old_doc_are_kept_under_the_old_path():
    # the three hashes every release through 0.5.4 shipped for it, keyed by the OLD path (a pre-receipt install is
    # recognised unedited by them)
    assert install_mod._RELEASED_BASE_SEED_SHA256[OLD_SEED_DOC] == frozenset({
        "e23c7a439c852ebbc872bb0a1b14e4ca24f1a01485e34059c9b12c1baab0b670",
        "4b66417ef61a4e7c0fc3754d944967af94d819e93883bee6f263b8199a94a162",
        "01e246c4df5ab23c6d83e5151b72e034e340be90b4421322e5d7699350b4dd56",
    })
    assert NEW_DOC not in install_mod._RELEASED_BASE_SEED_SHA256


def test_an_unedited_old_doc_is_replaced_by_the_new_one_and_does_not_hold_the_carrier(old_install):
    old_doc = (old_install / OLD_SEED_DOC).read_bytes()
    out, text = _update(old_install)
    assert "not refreshed, because it would load seed file(s) not installed yet" not in text
    assert (old_install / NEW_DOC).read_bytes() == _new_doc_bytes()
    carrier = (old_install / "CLAUDE.md").read_text()
    assert f"`{NEW_DOC}`" in carrier and "spore_instructions" not in carrier
    # the unedited old copy is retired, its bytes kept
    assert not (old_install / OLD_SEED_DOC).exists()
    assert _backups(old_install) == [old_doc]
    receipt = _receipt(old_install)
    assert receipt[NEW_DOC] == _sha(_new_doc_bytes()) and OLD_SEED_DOC not in receipt
    assert out.review == [], out.review
    # settled: a second update is silent
    out2, text2 = _update(old_install)
    assert out2.review == [] and out2.refreshed == [] and text2 == ""


def test_an_edited_old_doc_is_left_in_place_and_update_says_so(old_install):
    doc = old_install / OLD_SEED_DOC
    doc.write_text(doc.read_text() + "\nMY OWN RULE: never plant on a Friday.\n", encoding="utf-8")
    edited = doc.read_bytes()
    out, text = _update(old_install)
    assert doc.read_bytes() == edited
    assert (old_install / NEW_DOC).read_bytes() == _new_doc_bytes()
    assert "spore_instructions.md" in text and "intention_instructions.md" in text and "carry your edits" in text
    # the carrier is NOT held by the stray old file, and nothing is demanded of the operator
    assert "not refreshed, because" not in text
    assert "`seed/spore_instructions.md`" not in (old_install / "CLAUDE.md").read_text()
    assert out.review == [], out.review
    assert _backups(old_install) == []


def test_an_old_doc_the_carrier_still_names_is_kept(old_install):
    # an operator-edited carrier is staged, not replaced, so it still points at the old doc: removing that doc now would
    # leave the agent a pointer to nothing
    carrier = old_install / "CLAUDE.md"
    carrier.write_text(carrier.read_text() + "\nMY OWN CARRIER LINE.\n", encoding="utf-8")
    out, text = _update(old_install)
    assert "CLAUDE.md" in out.review
    assert (old_install / OLD_SEED_DOC).is_file()
    assert "still names" in text or "still points" in text


def test_a_pre_receipt_install_recognises_the_old_doc_by_its_released_hash(old_install, monkeypatch):
    doc = old_install / OLD_SEED_DOC
    old_doc = doc.read_bytes()
    _write_receipt(old_install, {k: v for k, v in _receipt(old_install).items() if not k.startswith("seed/")})
    # no record and a hash no release list holds: indistinguishable from an edit, so it is kept (the conservative side)
    out, text = _update(old_install)
    assert doc.read_bytes() == old_doc
    assert (old_install / NEW_DOC).read_bytes() == _new_doc_bytes()
    # now the released list holds it: it is levain's own unedited copy and goes, bytes kept
    monkeypatch.setitem(install_mod._RELEASED_BASE_SEED_SHA256, OLD_SEED_DOC,
                        install_mod._RELEASED_BASE_SEED_SHA256[OLD_SEED_DOC] | {_sha(old_doc)})
    out, text = _update(old_install)
    assert not doc.exists() and _backups(old_install) == [old_doc]


def test_a_dry_run_writes_nothing(old_install):
    def snapshot() -> dict[str, bytes]:
        return {str(p.relative_to(old_install)): p.read_bytes() for p in sorted(old_install.rglob("*"))
                if p.is_file() and ".levain/memory" not in str(p) and "baton" not in p.name}

    before = snapshot()
    out, text = _update(old_install, apply=False)
    assert snapshot() == before
    assert "spore_instructions.md" in text  # the plan says what it would do


def test_update_through_the_real_cli_installs_the_new_doc_without_holding_the_carrier(old_install, tmp_path):
    r = _levain(REPO, tmp_path / "home", "update", "--path", str(old_install), "--no-pip")
    both = r.stdout + r.stderr
    assert "would load seed file(s) not installed yet" not in both, both
    assert (old_install / NEW_DOC).is_file() and not (old_install / OLD_SEED_DOC).exists()
    assert f"`{NEW_DOC}`" in (old_install / "CLAUDE.md").read_text()


# ---------------------------------------------------------------------------------------------------------------------
# H7: seed is two words; nothing but the disposition word and the renamed files differs from the old release
# ---------------------------------------------------------------------------------------------------------------------

# Every file an install holds outside `.levain/` that this release writes differently, and why: the carrier names the
# renamed seed doc, the three hooks and seed/memory.md carry the disposition word and the renamed functions.
RENAMED_FILES = {
    "CLAUDE.md", "seed/memory.md",
    "activation/hooks/_levain_hook.py", "activation/hooks/session_start.py", "activation/hooks/user_prompt_submit.py",
}
DOC_RENAME = {"seed/spore_instructions.md": "seed/intention_instructions.md"}
# A changed line must be about the rename: a changed line without one of these words is an unrelated change (a `seed`
# directory or a sampling parameter swept up by a quoted-word rewrite would be exactly that).
RENAME_WORDS = re.compile(r"spore|intention|someday|germinat|_NON_COGNITION_DISPOSITIONS", re.IGNORECASE)
# The seed document names the retired disposition as a word of prose (the old copy says `seed` where the new one says
# `someday`), so its changed lines may carry the old word too. Only that document: in a hook or the carrier a bare
# `seed` is the install's directory, not a disposition.
RETIRED_DISPOSITION_IN_DOC = re.compile(r"\bseed\b", re.IGNORECASE)


def _tree(install: Path) -> dict[str, bytes]:
    """Every file outside `.levain/`, its text with this install's root and scratch home normalised away."""
    out: dict[str, bytes] = {}
    for p in sorted(install.rglob("*")):
        rel = p.relative_to(install)
        if p.is_file() and rel.parts[0] != ".levain" and "__pycache__" not in rel.parts:
            data = p.read_bytes()
            for needle in {str(install), str(install.resolve())}:
                data = data.replace(needle.encode(), b"<ROOT>")
            out[str(rel)] = data
    return out


@pytest.fixture
def both_installs(old_tree, answers_file, tmp_path):
    old, new = tmp_path / "old" / "entity", tmp_path / "new" / "entity"
    out_old = _init(old_tree, old, answers_file, tmp_path / "home-old")
    out_new = _init(REPO, new, answers_file, tmp_path / "home-new")

    def norm(text: str, root: Path, home: str) -> str:
        return text.replace(str(root.resolve()), "<ROOT>").replace(str(root), "<ROOT>").replace(
            str((tmp_path / home).resolve()), "<HOME>").replace(str(tmp_path / home), "<HOME>")

    return (old, new, norm(out_old, old, "home-old"), norm(out_new, new, "home-new"))


def _changed_lines(a: str, b: str) -> list[str]:
    lines_a, lines_b = a.split("\n"), b.split("\n")
    changed: list[str] = []
    # autojunk off: blank lines are the most common line, and the default heuristic stops anchoring on them
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, lines_a, lines_b, autojunk=False).get_opcodes():
        if tag != "equal":
            changed += lines_a[i1:i2] + lines_b[j1:j2]
    return changed


def _removed_lines(a: str, b: str) -> list[str]:
    """The lines the old text `a` had that the new text `b` no longer has (the old half of every changed block)."""
    lines_a, lines_b = a.split("\n"), b.split("\n")
    gone: list[str] = []
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, lines_a, lines_b, autojunk=False).get_opcodes():
        if tag != "equal":
            gone += lines_a[i1:i2]
    return gone


def test_the_install_tree_differs_only_in_the_renamed_files(both_installs):
    old, new, _o, _n = both_installs
    t_old, t_new = _tree(old), _tree(new)
    t_old = {DOC_RENAME.get(k, k): v for k, v in t_old.items()}
    assert t_old.keys() == t_new.keys()
    differing = {k for k in t_old if t_old[k] != t_new[k]}
    assert differing == RENAMED_FILES | set(DOC_RENAME.values()), sorted(differing)


def test_nothing_that_is_not_the_disposition_word_changed_in_those_files(both_installs):
    old, new, _o, _n = both_installs
    t_old = {DOC_RENAME.get(k, k): v for k, v in _tree(old).items()}
    t_new = _tree(new)
    for rel in sorted(RENAMED_FILES | set(DOC_RENAME.values())):
        a, b = t_old[rel].decode(), t_new[rel].decode()
        # The germinate-trigger helper gained a feature after the old release (row 178, the split: an agenda that waits on an
        # event), which is ADDED code with no rename word in it. For that one file the rename guard is therefore on what the
        # old release wrote: every line the old copy had and the new one lost must be about the rename (or be the
        # `datetime` import the feature widened). Every other file is held to the full rule.
        if rel == "activation/hooks/_levain_hook.py":
            for line in _removed_lines(a, b):
                assert RENAME_WORDS.search(line) or line == "from datetime import datetime, timezone", \
                    f"{rel}: an old line lost that is not about the rename: {line!r}"
        else:
            for line in _changed_lines(a, b):
                about_the_rename = RENAME_WORDS.search(line) or (
                    rel in DOC_RENAME.values() and RETIRED_DISPOSITION_IN_DOC.search(line))
                assert about_the_rename, f"{rel}: a changed line that is not about the rename: {line!r}"
        # the install's `seed/` directory is not a disposition: every path through it survives
        assert a.count("seed/") == b.count("seed/"), rel


def test_the_install_directory_and_the_pack_subtrees_are_unchanged(both_installs):
    old, new, _o, _n = both_installs
    for tag, root in (("old", old), ("new", new)):
        assert (root / "seed").is_dir() and (root / "activation").is_dir()
    for name in HOOK_FILES[:1]:
        a = (old / "activation" / "hooks" / name).read_text().splitlines()
        b = (new / "activation" / "hooks" / name).read_text().splitlines()
        assert [ln for ln in a if "_PACK_SUBTREES" in ln] == [ln for ln in b if "_PACK_SUBTREES" in ln]
        assert any("_PACK_SUBTREES" in ln for ln in b)


def test_init_prints_the_same_thing_but_for_the_renamed_doc(both_installs):
    _old, _new, out_old, out_new = both_installs
    changed = _changed_lines(out_old, out_new)
    assert changed, "init printed nothing different: the seed doc line should be the one difference"
    assert all("instructions.md" in ln for ln in changed), "\n".join(changed)
    assert out_old.count("seed") == out_new.count("seed")


def test_the_sampling_parameter_and_the_firing_seed_directory_are_untouched(old_tree):
    # these hold the word `seed` for other reasons; a quoted-word rewrite is what would have reached them
    pairs = [("levain/firing/seed.py", re.compile(r"^SEED_SUBDIR = ")),
             ("levain/broker/codec.py", re.compile(r'"logprobs".*"seed"')),
             ("levain/manifest.py", re.compile(r"^_PACK_SUBTREES = "))]
    for rel, pattern in pairs:
        old_lines = [ln for ln in (old_tree / rel).read_text().splitlines() if pattern.search(ln)]
        new_lines = [ln for ln in (REPO / rel).read_text().splitlines() if pattern.search(ln)]
        assert old_lines and old_lines == new_lines, rel


# ---------------------------------------------------------------------------------------------------------------------
# H9: the hooks. A stub anneal and a stub `levain` make the old/new matrix deterministic; the hook scripts are the real ones.
# ---------------------------------------------------------------------------------------------------------------------

_ITEMS = [
    {"id": "intention-1", "text": "rotate the staging certificate before launch", "type": "task",
     "germination": "dormant", "disposition": None, "next": None},
    {"id": "intention-2", "text": "phone the vet about the cat", "type": "task", "germination": "growing",
     "disposition": "agenda", "next": "2020-01-01"},
]
_ANNEAL_STUB = f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
if args[:1] == ["--db"]:
    args = args[2:]
if args[:2] in (["intention", "list"], ["spore", "list"]) and "--json" in args:
    print(json.dumps({_ITEMS!r}))
    sys.exit(0)
sys.exit(1)
"""
# what the hook's spawn would run: records its argv, and refuses the retired noun the way the CLI does (exit 2, at once)
_LEVAIN_STUB_MAIN = """import os, sys
with open(os.environ["LEVAIN_STUB_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == [os.environ["LEVAIN_STUB_REFUSES"]]:
    sys.stderr.write("levain spores is retired: use `levain intentions ...`\\n")
    sys.exit(2)
sys.exit(0)
"""
_DORMANT = "[open loops — due or gone quiet]"
_COLLISION = "[open loops — relevant to what you're doing]"


def _additional_context(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            return json.loads(line)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError, TypeError):
            continue
    return ""


def _probe(install: Path, scratch: Path, refuses: str = "spores") -> dict[str, str]:
    """Run the install's two entry-point hooks, as they stand on disk, against a stub anneal and a stub `levain` that
    refuses the noun ``refuses`` the way the CLI does. Works on a COPY so the install under test is never written."""
    work = scratch / f"probe-{len(list(scratch.glob('probe-*')))}"
    shutil.copytree(install, work, symlinks=True, ignore=shutil.ignore_patterns(".levain"))
    (work / ".levain").mkdir()
    stub = scratch / "anneal_stub.py"
    stub.write_text(_ANNEAL_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    pkg = scratch / "stubpkg" / "levain"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(_LEVAIN_STUB_MAIN, encoding="utf-8")
    log = work / "levain-spawns.log"
    for name in HOOK_FILES:  # the install fills this placeholder with the real anneal; the stub stands in for it
        path = work / "activation" / "hooks" / name
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r'^_INSTALL_ANNEAL_BIN = ".*"$', f'_INSTALL_ANNEAL_BIN = "{stub}"', text, flags=re.M),
                        encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(scratch / "stubpkg"), LEVAIN_STUB_LOG=str(log),
               LEVAIN_STUB_REFUSES=refuses, HOME=str(scratch / "h"), LEVAIN_HOME=str(scratch / "h" / ".levain"))
    env.pop("LEVAIN_HOOK_SUPPRESS", None)

    def run(script: str, payload: dict) -> str:
        r = subprocess.run([sys.executable, str(work / "activation" / "hooks" / script)], input=json.dumps(payload),
                           cwd=work, env=env, capture_output=True, text=True, timeout=60)
        return _additional_context(r.stdout)

    start = run("session_start.py", {"session_id": "s1", "source": "startup", "cwd": str(work)})
    prompt = run("user_prompt_submit.py", {"session_id": "s1", "cwd": str(work),
                                           "prompt": "please rotate the staging certificate today"})
    return {"start": start, "prompt": prompt, "spawned": log.read_text() if log.is_file() else ""}


@pytest.fixture
def scratch(tmp_path) -> Path:
    path = tmp_path / "scratch"
    path.mkdir()
    return path


@pytest.fixture
def old_and_new_installs(old_tree, answers_file, tmp_path):
    old, new = tmp_path / "gen-old" / "entity", tmp_path / "gen-new" / "entity"
    _init(old_tree, old, answers_file, tmp_path / "home-old")
    _init(REPO, new, answers_file, tmp_path / "home-new")
    return old, new


def test_an_old_hook_set_and_a_new_one_each_work_whole_and_a_mixed_set_goes_dark(old_and_new_installs, scratch, tmp_path):
    """The matrix: old/new for each of the three hook files of a real install (eight installs). A set is coherent when
    all three are one generation, and then both entry points show the surfaces; a mixed set raises an AttributeError the
    hooks swallow, so the surface that crosses the generation goes dark with nothing said. This is why the three move in
    one step (see the `levain update` tests below), and what an old hook looks like next to this package."""
    old, new = old_and_new_installs
    for gens in itertools.product(("old", "new"), repeat=3):
        install = tmp_path / ("combo-" + "-".join(gens)) / "entity"
        shutil.copytree(new, install, symlinks=True)
        for name, gen in zip(HOOK_FILES, gens):
            if gen == "old":
                shutil.copy2(old / "activation" / "hooks" / name, install / "activation" / "hooks" / name)
        got = _probe(install, scratch)
        hook_gen, start_gen, prompt_gen = gens
        assert (_DORMANT in got["start"]) == (start_gen == hook_gen), (gens, got["start"])
        assert (_COLLISION in got["prompt"]) == (prompt_gen == hook_gen), (gens, got["prompt"])
        # the agenda surface calls only names the rename did not touch (due_agendas, open_agendas, start_germinate), so it
        # survives a mixed set: what it spawns is the noun of the generation of `_levain_hook.py`, and what it prints on a
        # refusal is the noun of the generation of `session_start.py`
        spawn_noun = "spores" if hook_gen == "old" else "intentions"
        print_noun = "spores" if start_gen == "old" else "intentions"
        assert f"{spawn_noun} germinate" in got["spawned"], (gens, got["spawned"])
        if hook_gen == "old":
            assert f"did not start: run `levain {print_noun} germinate`" in got["start"], (gens, got["start"])
        else:
            assert "germination started" in got["start"], (gens, got["start"])


def test_an_old_hooks_germinate_spawn_is_refused_and_the_hook_says_so(old_and_new_installs, scratch):
    """In the OLD hook code a refused spawn (the child exits non-zero within its 0.4 s grace) is printed, not swallowed:
    `[germinate] N agenda came due but germination did not start: run \\`levain spores germinate\\``. It names the retired
    command, which refuses again with its pointer: the loop closes on the pointer, and `levain update` (which refreshes
    the hooks) is the way out. The new hook names the new command."""
    old, new = old_and_new_installs
    got_old = _probe(old, scratch)
    assert got_old["spawned"].startswith("spores germinate")
    assert "came due but germination did not start: run `levain spores germinate`" in got_old["start"]
    got_new = _probe(new, scratch)
    assert got_new["spawned"].startswith("intentions germinate") and "spores" not in got_new["spawned"]
    assert "germination started" in got_new["start"] and "did not start" not in got_new["start"]


def test_the_new_hooks_failed_spawn_names_the_new_command(old_and_new_installs, scratch):
    _old, new = old_and_new_installs
    got = _probe(new, scratch, refuses="intentions")
    assert "came due but germination did not start: run `levain intentions germinate`" in got["start"], got["start"]


def test_the_parser_refuses_what_the_old_hook_spawns_and_accepts_what_the_new_one_does(tmp_path):
    old = _levain(REPO, tmp_path / "home", "spores", "germinate", "--path", str(tmp_path), "--agent", "claude_code")
    assert old.returncode == 2
    new = _levain(REPO, tmp_path / "home", "intentions", "germinate", "--help")
    assert new.returncode == 0 and "--agent" in new.stdout and "--path" in new.stdout


def _coherent(install: Path, scratch: Path) -> None:
    got = _probe(install, scratch)
    assert _DORMANT in got["start"] and _COLLISION in got["prompt"], got


def test_update_moves_all_three_hooks_together_when_none_is_edited(old_install, scratch):
    out, text = _update(old_install)
    hooks = old_install / "activation" / "hooks"
    for name in HOOK_FILES:
        body = (hooks / name).read_text()
        assert "spores" not in body.lower().replace("spores_json", ""), name
    receipt = json.loads(activation_receipt_path(old_install).read_text())["files"]
    for rel in COUPLED:
        assert receipt[rel]["installed"] == _sha((old_install / "activation" / rel).read_bytes()), rel
    assert out.review == [], (out.review, text)
    _coherent(old_install, scratch)


@pytest.mark.parametrize("edited", HOOK_FILES)
def test_one_edited_hook_holds_the_other_two_so_the_set_never_mixes(old_install, scratch, edited):
    hooks = old_install / "activation" / "hooks"
    before = {n: (hooks / n).read_bytes() for n in HOOK_FILES}
    receipt_before = json.loads(activation_receipt_path(old_install).read_text())["files"]
    (hooks / edited).write_bytes(before[edited] + b"\n# my own edit\n")
    edited_bytes = (hooks / edited).read_bytes()
    out, text = _update(old_install)
    # nothing of the three moved: the edited one is kept as it is, the others are the old generation still
    assert (hooks / edited).read_bytes() == edited_bytes
    for n in HOOK_FILES:
        if n != edited:
            assert (hooks / n).read_bytes() == before[n], f"{n} moved on its own: the set is now mixed"
    # said, and the package's versions of the set are staged to merge together
    for n in HOOK_FILES:
        key = f"activation/hooks/{n}"
        assert key in out.review, (key, out.review)
        assert old_install.joinpath(*PENDING_REL, "activation", "hooks", n).is_file(), n
    assert "together" in text
    # the files left behind are not recorded as brought up to date: once the operator settles the set, the next update
    # still replaces an unedited member instead of treating the old copy as kept
    receipt_after = json.loads(activation_receipt_path(old_install).read_text())["files"]
    for n in HOOK_FILES:
        if n != edited:
            assert receipt_after[f"hooks/{n}"] == receipt_before[f"hooks/{n}"], n
    _coherent(old_install, scratch)  # the held set still works: it is the old generation, whole


def _edit(path: Path) -> bytes:
    path.write_bytes(path.read_bytes() + b"\n# my own edit\n")
    return path.read_bytes()


def test_a_later_update_still_holds_the_set_while_the_edit_stands(old_install, scratch):
    # the first update lists the edited file once and records it, so a second update sees it as kept: the hold must not
    # depend on that listing, or the second update would move the other two and mix the set
    hooks = old_install / "activation" / "hooks"
    before = {n: (hooks / n).read_bytes() for n in HOOK_FILES}
    _edit(hooks / "session_start.py")
    _update(old_install)
    out, text = _update(old_install)
    assert (hooks / "_levain_hook.py").read_bytes() == before["_levain_hook.py"]
    assert (hooks / "user_prompt_submit.py").read_bytes() == before["user_prompt_submit.py"]
    assert "activation/hooks/_levain_hook.py" in out.review and "together" in text
    _coherent(old_install, scratch)


def test_once_the_edited_hook_is_merged_the_set_moves(old_install, scratch):
    hooks = old_install / "activation" / "hooks"
    _edit(hooks / "session_start.py")
    _update(old_install)
    # the operator adopts the staged version (their edit merged in)
    staged = old_install.joinpath(*PENDING_REL, "activation", "hooks", "session_start.py")
    merged = staged.read_bytes() + b"\n# merged\n"
    (hooks / "session_start.py").write_bytes(merged)
    out, text = _update(old_install)
    assert (hooks / "session_start.py").read_bytes() == merged
    for n in ("_levain_hook.py", "user_prompt_submit.py"):
        assert b"open_intentions" in (hooks / n).read_bytes() and b"open_spores" not in (hooks / n).read_bytes()
    assert out.review == [], (out.review, text)
    _coherent(old_install, scratch)


def test_an_edit_to_a_hook_that_calls_no_renamed_name_does_not_hold_the_set(old_install, scratch):
    hooks = old_install / "activation" / "hooks"
    mine = _edit(hooks / "post_tool_use.py")
    out, text = _update(old_install)
    assert (hooks / "post_tool_use.py").read_bytes() == mine
    for n in HOOK_FILES:
        assert b"open_spores" not in (hooks / n).read_bytes(), n
    assert "activation/hooks/_levain_hook.py" not in out.review
    _coherent(old_install, scratch)


def test_a_hook_that_cannot_be_written_puts_the_others_back(old_install, scratch, monkeypatch):
    hooks = old_install / "activation" / "hooks"
    before = {n: (hooks / n).read_bytes() for n in HOOK_FILES}
    receipt_before = json.loads(activation_receipt_path(old_install).read_text())["files"]
    real = install_mod._atomic_write_bytes

    def failing(target, data, *, like=None):
        if Path(target).name == "user_prompt_submit.py" and b"open_intentions" in data:
            raise OSError(28, "No space left on device")
        return real(target, data, like=like)

    with monkeypatch.context() as m:
        m.setattr(install_mod, "_atomic_write_bytes", failing)
        out, text = _update(old_install)
    for n in HOOK_FILES:
        assert (hooks / n).read_bytes() == before[n], f"{n} was left on this release's version"
    assert "activation/hooks/user_prompt_submit.py" in out.review and "put back as it was" in text
    receipt_after = json.loads(activation_receipt_path(old_install).read_text())["files"]
    for rel in COUPLED:
        assert receipt_after[rel] == receipt_before[rel], rel
    _coherent(old_install, scratch)


@pytest.mark.parametrize("failing_name", HOOK_FILES)
def test_a_failed_hook_write_holds_every_remaining_hook_whichever_file_fails(old_install, scratch, monkeypatch,
                                                                              failing_name):
    """The hook set moves as ONE transaction (ruling H9): the first moving file that cannot be written puts the ones
    already written back AND stops the rest from being written, so the first, a middle and the last file failing all end
    on the all-old set (1111-32 L3 round 1: the loop used to go on and write the files after the failure)."""
    hooks = old_install / "activation" / "hooks"
    before = {n: (hooks / n).read_bytes() for n in HOOK_FILES}
    receipt_before = json.loads(activation_receipt_path(old_install).read_text())["files"]
    real = install_mod._atomic_write_bytes

    def failing(target, data, *, like=None):
        if Path(target).name == failing_name and data != before[failing_name]:
            raise OSError(28, "No space left on device")
        return real(target, data, like=like)

    with monkeypatch.context() as m:
        m.setattr(install_mod, "_atomic_write_bytes", failing)
        out, text = _update(old_install)
    for n in HOOK_FILES:
        assert (hooks / n).read_bytes() == before[n], f"{n} is not the old generation after {failing_name} failed"
    failed_at = HOOK_FILES.index(failing_name)
    for n in HOOK_FILES[failed_at:]:   # the failed file and every one held behind it are listed for review
        assert f"activation/hooks/{n}" in out.review, (n, out.review)
    for rel in COUPLED:
        assert f"activation/{rel}" not in out.refreshed, (rel, out.refreshed)
    assert "could not be written" in text and "together" in text, text
    receipt_after = json.loads(activation_receipt_path(old_install).read_text())["files"]
    for rel in COUPLED:
        assert receipt_after[rel] == receipt_before[rel], rel
    _coherent(old_install, scratch)
