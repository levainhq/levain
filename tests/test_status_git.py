"""scripts/status_git.py derives the git-checkable half of the 1.0 status from ancestry, so it is not kept by hand."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "status_git.py"


def _load():
    spec = importlib.util.spec_from_file_location("status_git", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(cwd)}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    shas = {}
    for name, tag in (("a", "v0.1.0"), ("b", "v0.2.0"), ("c", None)):
        (work / name).write_text(name)
        _git(work, "add", name)
        _git(work, "commit", "-q", "-m", name)
        shas[name] = _git(work, "rev-parse", "--short", "HEAD")
        if tag:
            _git(work, "tag", tag)
    _git(work, "push", "-q", "origin", "HEAD:main", "--tags")
    _git(work, "fetch", "-q", "origin")
    _git(work, "checkout", "-q", "-b", "side")
    (work / "side").write_text("s")
    _git(work, "add", "side")
    _git(work, "commit", "-q", "-m", "side")
    shas["side"] = _git(work, "rev-parse", "--short", "HEAD")
    _git(work, "push", "-q", "origin", "side:refs/heads/seat/x")
    _git(work, "fetch", "-q", "origin")
    mod = _load()
    mod.ROOT = str(work)
    return mod, work, shas


def test_a_pair_is_ok_only_when_the_first_containing_tag_matches_and_the_sha_is_on_main(repo) -> None:
    mod, _work, s = repo
    table = ("| id | piece | status | derive |\n|---|---|---|---|\n"
             f"| R1 | right | RELEASED v0.1.0 | `rel {s['a']}` -> v0.1.0 | none |\n"
             f"| R2 | wrong tag | RELEASED v0.1.0 | `rel {s['b']}` -> v0.1.0 | none |\n"
             f"| R3 | untagged | RELEASED v0.9.0 | `rel {s['c']}` -> v0.9.0 | none |\n"
             f"| R4 | not on main | RELEASED v0.2.0 | `rel {s['side']}` -> v0.2.0 | none |\n"
             "| R5 | unknown | RELEASED v0.2.0 | `rel deadbeef1` -> v0.2.0 | none |\n")
    out = mod.ancestry(table)
    rows = {line.split("|")[1].strip(): line for line in out.splitlines() if line.startswith("| R")}
    assert rows["R1"].rstrip().endswith("| ok |")
    assert "| v0.2.0 |" in rows["R2"] and rows["R2"].rstrip().endswith("| DRIFT |")
    assert "(no tag)" in rows["R3"] and rows["R3"].rstrip().endswith("| DRIFT |")
    assert "| NO |" in rows["R4"] and rows["R4"].rstrip().endswith("| DRIFT |")
    assert rows["R5"].rstrip().endswith("| UNRESOLVED |")
    assert "5 pairs, 4 not ok" in out


def test_write_replaces_its_own_block_and_nothing_else(repo, tmp_path: Path) -> None:
    mod, _work, s = repo
    f = tmp_path / "STATUS.md"
    f.write_text(f"# status\nintro\n\n## The table\n| R1 | x | y | `rel {s['a']}` -> v0.1.0 | z |\n", encoding="utf-8")
    mod.write_block(f, mod.ancestry(f.read_text(encoding="utf-8")))
    once = f.read_text(encoding="utf-8")
    assert once.index(mod.START) < once.index("## The table") and "intro" in once
    mod.write_block(f, mod.ancestry(f.read_text(encoding="utf-8")))
    assert f.read_text(encoding="utf-8") == once, "a second write changes nothing"
    assert once.count(mod.START) == 1


def test_the_state_header_names_main_the_tag_and_each_branch(repo) -> None:
    mod, _work, s = repo
    out = mod.state(5)
    assert s["c"] in out and "newest tag: v0.2.0, 1 commits on main since it" in out
    assert "origin/seat/x" in out and "1 ahead, NOT merged" in out
