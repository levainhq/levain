"""``levain charter check`` (levain#31), WARN mode: the real CHARTER.md passes clean; a broken one warns with the exact
text and exits 0; the check never refuses."""
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import charter

REPO = Path(__file__).resolve().parent.parent
REAL = (REPO / "CHARTER.md").read_text(encoding="utf-8")
# The reproduced broken file: the Constraints section is missing, and Criteria carries an unknown tag.
BROKEN = REAL.replace("""## Constraints
- K1: Python 3.12 or later; the board's fold and readers are stdlib-only and use no network.
- K2: The operator's memory never leaves their machine except by a path they govern.
- K3: Nothing generated from the operator's private memory is written to this public repository.

""", "").replace("[judged: the operator]", "[verified: the operator]", 1)


def _run(args, capsys):
    rc = main(["charter", "check", *args])
    return rc, capsys.readouterr().out


def test_the_real_charter_passes_clean(capsys):
    problems, warnings = charter.check_text(REAL, repo_name="levain")
    assert problems == [] and warnings == []
    rc, out = _run(["--repo", str(REPO), "--worktree", "--repo-name", "levain"], capsys)
    assert rc == 0
    assert out == "levain charter check: levain's CHARTER.md (the worktree) is a valid charter.\n"


def test_a_broken_charter_warns_with_the_exact_text_and_exits_zero(tmp_path, capsys):
    assert BROKEN != REAL
    f = tmp_path / "CHARTER.md"
    f.write_text(BROKEN, encoding="utf-8")
    rc, out = _run(["--file", str(f), "--repo", str(REPO), "--repo-name", "levain"], capsys)
    assert rc == 0
    assert out.startswith(
        f"levain: warning (charter not yet enforced): levain's CHARTER.md on {f} is not a valid charter "
        "(2 problems; the first: line 5: the `##` sections must be exactly Purpose, Criteria, Constraints, Plan, in that "
        "order; found Purpose, Criteria, Plan).\n"
        f"Run `levain charter check --repo {REPO}` for every problem, fix them, commit, and push.\n")
    assert "[criteria] C2 needs exactly one" in out


def test_an_unknown_field_is_reported():
    problems, _ = charter.check_text(REAL.replace("Export: memory", "Export: memory\nStatus: green"), repo_name="levain")
    assert [(p.rule, p.line) for p in problems] == [("header", 4)]
    problems, _ = charter.check_text(REAL.replace("Export: memory", "Export: public"), repo_name="levain")
    assert [p.rule for p in problems] == ["export"]


def test_a_missing_charter_warns_and_exits_zero(tmp_path, capsys):
    rc, out = _run(["--file", str(tmp_path / "none.md"), "--repo-name", "levain"], capsys)
    assert rc == 0
    assert out.startswith("levain: warning (charter not yet enforced): levain has no charter. Levain works on a repository "
                          "only when the file carries a CHARTER.md\n")


@pytest.mark.parametrize("edit, rule", [
    (lambda t: t.replace("- levain#26\n- levain#31", "- levain#26 is done\n- levain#31"), "plan"),
    (lambda t: t.replace("Charter-Version: 1", "Charter-Version: 2"), "version"),
    (lambda t: t.replace("# Charter: levain", "# Charter: flow"), "title"),
    (lambda t: t.replace("- K2:", "- K1:"), "constraints"),
    (lambda t: t + "\n" * 200, "size"),
    (lambda t: t.replace("never leaves", "never leaves (see 0123abc)"), "status"),
    (lambda t: t.replace("never leaves", "never leaves (since 2026-11-15)"), "status"),
    (lambda t: t.replace("- C3:", "- C3: ✅"), "status"),
])
def test_each_structural_rule_fires(edit, rule):
    problems, _ = charter.check_text(edit(REAL), repo_name="levain")
    assert rule in {p.rule for p in problems}


def test_an_ordinary_word_and_a_judged_date_pass():
    text = REAL.replace("never leaves", "is ready, blocked or done, and never leaves").replace(
        "[judged: the operator]", "[judged: the operator, 2026-11-15]", 1)
    assert charter.check_text(text, repo_name="levain")[0] == []


def test_the_store_checks_run_only_when_the_store_is_given():
    store = {"levain#7": ("levain", None), "levain#99": ("levain", None), "levain#98": ("levain", "out-of-1.0"),
             "flow#1": ("flow", None)}
    problems, warnings = charter.check_text(REAL, repo_name="levain", items=store)
    assert {p.text for p in problems if p.rule == "store"} >= {"levain#8 is not in the board store"}
    assert warnings == ["levain#99 is in the store and in no plan group"]


def test_the_check_reads_origin_main_by_default(tmp_path, capsys):
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain"], capsys)     # not a git repo: no charter there
    assert rc == 0 and "levain has no charter" in out


def test_the_check_reads_the_charter_at_origin_main(tmp_path, capsys):
    import subprocess
    g = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*g, "init", "-q"], check=True)
    (tmp_path / "CHARTER.md").write_text(REAL, encoding="utf-8")
    subprocess.run([*g, "add", "CHARTER.md"], check=True)
    subprocess.run([*g, "commit", "-q", "-m", "c"], check=True)
    subprocess.run([*g, "update-ref", charter.DEFAULT_REF, "HEAD"], check=True)
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain"], capsys)
    assert rc == 0 and out == "levain charter check: levain's CHARTER.md (origin/main) is a valid charter.\n"


def test_the_golden_journey_check_names_a_file_that_exists():
    import re
    tag = re.search(r"\[check: python3 -m pytest (\S+) ", REAL).group(1)
    assert (REPO / tag).is_file()


def test_the_orphan_criterion_is_tagged_to_the_orphan_rule_item():
    assert "surfaces as work to do. [item: levain#32]" in REAL


def _unreadable(out, why):
    assert out.startswith(f"levain: warning (charter not yet enforced): levain's CHARTER.md (the file) could not be read: {why}.")
    assert "has no charter" not in out


def test_a_binary_a_directory_and_a_loop_say_unreadable_not_no_charter(tmp_path, capsys):
    b = tmp_path / "bin.md"
    b.write_bytes(b"\xff\xfe\x00\x01" * 50)
    rc, out = _run(["--file", str(b), "--repo-name", "levain"], capsys)
    assert rc == 0
    _unreadable(out, "unreadable (not UTF-8)")
    rc, out = _run(["--file", str(tmp_path), "--repo-name", "levain"], capsys)
    assert rc == 0
    _unreadable(out, "unreadable (IsADirectoryError)")
    loop = tmp_path / "loop.md"
    loop.symlink_to(loop)
    rc, out = _run(["--file", str(loop), "--repo-name", "levain"], capsys)
    assert rc == 0
    _unreadable(out, "unreadable (OSError)")


def test_an_empty_file_and_a_valid_symlink(tmp_path, capsys):
    e = tmp_path / "empty.md"
    e.write_text("")
    rc, out = _run(["--file", str(e), "--repo-name", "levain"], capsys)
    assert rc == 0 and "is not a valid charter" in out
    link = tmp_path / "link.md"
    link.symlink_to(REPO / "CHARTER.md")
    rc, out = _run(["--file", str(link), "--repo-name", "levain"], capsys)
    assert rc == 0 and "is a valid charter" in out


def test_a_dangling_symlink_is_absent(tmp_path, capsys):
    link = tmp_path / "dangling.md"
    link.symlink_to(tmp_path / "nowhere")
    rc, out = _run(["--file", str(link), "--repo-name", "levain"], capsys)
    assert rc == 0 and "levain has no charter" in out


def test_git_missing_or_hung_warns_and_exits_zero(tmp_path, capsys, monkeypatch):
    import subprocess

    for exc in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 30)):
        def boom(*a, _e=exc, **k):
            raise _e
        monkeypatch.setattr(subprocess, "run", boom)
        rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain"], capsys)
        assert rc == 0
        assert out.startswith("levain: warning (charter not yet enforced): levain's CHARTER.md (origin/main) could not be read: "
                              f"unreadable (git: {type(exc).__name__}).")
    monkeypatch.undo()
    rc, out = _run(["--repo", str(tmp_path)], capsys)       # no --repo-name and no git answer: still no traceback
    assert rc == 0


def test_a_ref_that_looks_like_an_option_never_reaches_git(tmp_path, capsys):
    target = tmp_path / "PWNED"
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain", f"--ref=--output={target}"], capsys)
    assert rc == 0 and "a ref cannot start with '-'" in out
    assert not list(tmp_path.glob("PWNED*"))


def test_the_printed_problem_list_is_capped_and_json_keeps_all(tmp_path, capsys):
    f = tmp_path / "CHARTER.md"
    f.write_text(REAL + "\n".join(f"deadbeef{i:02d}" for i in range(200)) + "\n", encoding="utf-8")
    rc, out = _run(["--file", str(f), "--repo-name", "levain"], capsys)
    assert rc == 0 and out.count("\n  line ") == charter.MAX_PRINTED and "more (--json lists every one)" in out
    rc, out = _run(["--file", str(f), "--repo-name", "levain", "--json"], capsys)
    import json
    assert len(json.loads(out)["problems"]) >= 200
