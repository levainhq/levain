"""CHANGELOG.md merges as a union (.gitattributes), so two links adding an entry at the same spot never hand each other back.

The test runs real git in a scratch repository: the repo's own .gitattributes is copied in, two branches each add an entry at the same
place, and the merge must succeed with both entries once each. The control repeats it WITHOUT the attributes file and must conflict, so
the pass cannot come from a merge that would have succeeded anyway. A last test pins the scope: the attributes file names CHANGELOG.md
and nothing else (a code or test path under a union merge would keep both sides of a real conflict)."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "# Changelog\n\n## [Unreleased]\n\n### Changed\n\n- the entry already on main\n\n## [0.1.0]\n\n- an old release\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false",
           "-c", "core.hooksPath=/dev/null", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _commit(repo: Path, message: str) -> None:
    assert _git(repo, "add", "-A").returncode == 0
    r = _git(repo, "commit", "-q", "-m", message)
    assert r.returncode == 0, r.stderr


def _two_branches(tmp_path: Path, with_attributes: bool) -> subprocess.CompletedProcess:
    repo = tmp_path / ("with" if with_attributes else "without")
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    (repo / "CHANGELOG.md").write_text(BASE)
    if with_attributes:
        shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    _commit(repo, "base")
    for name in ("a", "b"):
        assert _git(repo, "checkout", "-q", "-b", name, "main").returncode == 0
        text = (repo / "CHANGELOG.md").read_text().replace("- the entry already on main\n", f"- the entry already on main\n- entry {name.upper()}\n")
        (repo / "CHANGELOG.md").write_text(text)
        _commit(repo, f"link {name}")
    assert _git(repo, "checkout", "-q", "a").returncode == 0
    merged = _git(repo, "merge", "--no-edit", "b")
    merged.changelog = (repo / "CHANGELOG.md").read_text()
    return merged


def test_two_links_adding_an_entry_at_the_same_spot_merge_with_both_entries_once(tmp_path: Path) -> None:
    merged = _two_branches(tmp_path, with_attributes=True)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    text = merged.changelog
    assert text.count("- entry A\n") == 1 and text.count("- entry B\n") == 1, text
    assert text.count("- the entry already on main\n") == 1 and text.count("- an old release\n") == 1, text
    assert "<<<<<<<" not in text and ">>>>>>>" not in text, text


def test_the_same_merge_without_the_attributes_file_conflicts(tmp_path: Path) -> None:
    merged = _two_branches(tmp_path, with_attributes=False)
    assert merged.returncode != 0 and "<<<<<<<" in merged.changelog, (merged.stdout, merged.changelog)


def test_the_attributes_file_names_only_the_changelog() -> None:
    lines = [ln.strip() for ln in (ROOT / ".gitattributes").read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert lines == ["CHANGELOG.md merge=union"], lines
