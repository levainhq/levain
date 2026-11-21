"""CHANGELOG.md merges as a union (.gitattributes), so two links adding an entry at the same spot never hand each other back.

The test runs real git in a scratch repository: the repo's own .gitattributes is copied in, two branches each add an entry at the same
place, and the merge must succeed with both entries once each. The control repeats it WITHOUT the attributes file and must conflict, so
the pass cannot come from a merge that would have succeeded anyway. One test pins the known edge (two branches editing the SAME existing
line both keep their version: a union merge never raises a conflict), so a change to that behaviour is seen. A last test pins the scope:
the attributes file names CHANGELOG.md and nothing else (a code or test path under a union merge would keep both sides of a real conflict).
Every git call runs with the GIT_* variables of the caller removed (a pre-push hook sets GIT_DIR and GIT_INDEX_FILE, which would point the
scratch repository's commands at the real one) and with no user or system git configuration."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "# Changelog\n\n## [Unreleased]\n\n### Changed\n\n- the entry already on main\n\n## [0.1.0]\n\n- an old release\n"


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false",
           "-c", "core.hooksPath=/dev/null", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=_env())


def _commit(repo: Path, message: str) -> None:
    assert _git(repo, "add", "-A").returncode == 0
    r = _git(repo, "commit", "-q", "-m", message)
    assert r.returncode == 0, r.stderr


def _merge_two(tmp_path: Path, name: str, with_attributes: bool, edit) -> tuple:
    """Two branches off one base, `edit(branch_name, text) -> text` applied on each; b is merged into a. Returns (the merge, the CHANGELOG)."""
    repo = tmp_path / name
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "checkout", "-q", "-b", "main").returncode == 0
    (repo / "CHANGELOG.md").write_text(BASE)
    if with_attributes:
        shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    _commit(repo, "base")
    for branch in ("a", "b"):
        assert _git(repo, "checkout", "-q", "-b", branch, "main").returncode == 0
        (repo / "CHANGELOG.md").write_text(edit(branch, (repo / "CHANGELOG.md").read_text()))
        _commit(repo, f"link {branch}")
    assert _git(repo, "checkout", "-q", "a").returncode == 0
    merged = _git(repo, "merge", "--no-edit", "b")
    return merged, (repo / "CHANGELOG.md").read_text()


def _add_entry(branch: str, text: str) -> str:
    return text.replace("- the entry already on main\n", f"- the entry already on main\n- entry {branch.upper()}\n")


def test_two_links_adding_an_entry_at_the_same_spot_merge_with_both_entries_once(tmp_path: Path) -> None:
    merged, text = _merge_two(tmp_path, "with", True, _add_entry)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert text.count("- entry A\n") == 1 and text.count("- entry B\n") == 1, text
    assert text.count("- the entry already on main\n") == 1 and text.count("- an old release\n") == 1, text
    assert "<<<<<<<" not in text and ">>>>>>>" not in text, text


def test_the_same_merge_without_the_attributes_file_conflicts(tmp_path: Path) -> None:
    merged, text = _merge_two(tmp_path, "without", False, _add_entry)
    assert merged.returncode != 0 and "<<<<<<<" in text, (merged.stdout, text)


def test_known_edge_two_links_editing_the_same_existing_line_both_keep_their_version(tmp_path: Path) -> None:
    """Accepted by the desk (11-21): a union merge never raises a conflict, so a same-line edit by two links lands as two lines. The release
    cut reads CHANGELOG by eye; the per-link fragment scheme is the answer if this ever misfires."""
    merged, text = _merge_two(tmp_path, "sameline", True, lambda b, t: t.replace("- the entry already on main\n", f"- the entry, as link {b} words it\n"))
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "- the entry, as link a words it\n" in text and "- the entry, as link b words it\n" in text, text


def test_the_attributes_file_names_only_the_changelog() -> None:
    lines = [ln.strip() for ln in (ROOT / ".gitattributes").read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert lines == ["CHANGELOG.md merge=union"], lines
