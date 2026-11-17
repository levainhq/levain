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
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain"], capsys)     # not a git repo: git cannot say
    assert rc == 0 and "could not be read" in out


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


# ---- L3 r1 -------------------------------------------------------------------------------------------------
def _json(args, capsys):
    import json
    rc = main(["charter", "check", "--json", *args])
    cap = capsys.readouterr()
    return rc, json.loads(cap.out), cap.err        # json.loads fails on anything but exactly one document


_KEYS = {"repo", "source", "present", "readable", "why", "valid", "store_checked", "store_note", "problems", "warnings"}


def test_a_torn_or_problem_store_skips_the_store_checks_visibly(tmp_path, capsys):
    f = tmp_path / "CHARTER.md"
    f.write_text(REAL, encoding="utf-8")
    torn = tmp_path / "items.jsonl"
    torn.write_text('{"op": "add", "id": "levain#7"', encoding="utf-8")        # a torn last line
    rc, doc, err = _json(["--file", str(f), "--repo-name", "levain", "--items", str(torn)], capsys)
    assert rc == 0 and doc["store_checked"] is False and doc["store_note"] and "skipped" in err
    assert not [p for p in doc["problems"] if p["rule"] == "store"] and doc["warnings"] == []
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"op": "add"}\n', encoding="utf-8")                          # a whole line that is not a valid add
    rc, doc, err = _json(["--file", str(f), "--repo-name", "levain", "--items", str(bad)], capsys)
    assert rc == 0 and doc["store_checked"] is False
    rc, doc, err = _json(["--file", str(f), "--repo-name", "levain"], capsys)
    assert doc["store_checked"] is False and doc["valid"] is True


def test_json_is_one_document_with_the_same_keys_on_every_path(tmp_path, capsys):
    f = tmp_path / "CHARTER.md"
    f.write_text(REAL + "x\n", encoding="utf-8")
    seen = []
    for args in (["--file", str(f)], ["--file", str(tmp_path / "none")], ["--file", str(tmp_path)],
                 ["--worktree", "--repo", str(REPO)]):
        rc, doc, _ = _json([*args, "--repo-name", "levain"], capsys)
        assert rc == 0
        seen.append(set(doc))
    assert all(k == _KEYS for k in seen)
    store = tmp_path / "items.jsonl"
    store.write_text("", encoding="utf-8")
    rc, doc, _ = _json(["--worktree", "--repo", str(REPO), "--repo-name", "levain", "--items", str(store)], capsys)
    assert set(doc) == _KEYS


def test_a_git_failure_that_is_not_a_missing_path_is_unreadable_not_absent(tmp_path, capsys):
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain"], capsys)        # not a git repository
    assert rc == 0 and "could not be read: unreadable (git exit 128: fatal: not a git repository" in out
    import subprocess
    g = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*g, "init", "-q"], check=True)
    (tmp_path / "x").write_text("x")
    subprocess.run([*g, "add", "x"], check=True)
    subprocess.run([*g, "commit", "-q", "-m", "c"], check=True)
    subprocess.run([*g, "update-ref", charter.DEFAULT_REF, "HEAD"], check=True)
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain"], capsys)         # the ref has no CHARTER.md: absent
    assert rc == 0 and "levain has no charter" in out
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain", "--ref", "refs/remotes/origin/none"], capsys)
    assert rc == 0 and "levain has no charter" in out


def test_a_crlf_charter_in_git_and_a_bom_read_like_the_worktree(tmp_path, capsys):
    import subprocess
    crlf = REAL.replace("\n", "\r\n")
    g = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false"]
    subprocess.run([*g, "init", "-q"], check=True)
    (tmp_path / "CHARTER.md").write_bytes(crlf.encode())
    subprocess.run([*g, "add", "CHARTER.md"], check=True)
    subprocess.run([*g, "commit", "-q", "-m", "c"], check=True)
    subprocess.run([*g, "update-ref", charter.DEFAULT_REF, "HEAD"], check=True)
    rc, out = _run(["--repo", str(tmp_path), "--repo-name", "levain"], capsys)
    assert out == "levain charter check: levain's CHARTER.md (origin/main) is a valid charter.\n"
    bom = tmp_path / "bom.md"
    bom.write_bytes(b"\xef\xbb\xbf" + REAL.encode())
    rc, out = _run(["--file", str(bom), "--repo-name", "levain"], capsys)
    assert "is a valid charter" in out


def test_a_user_that_does_not_exist_and_a_looping_repo_path_warn_and_exit_zero(tmp_path, capsys):
    rc, out = _run(["--file", "~nouser_zz/x", "--repo-name", "levain"], capsys)
    assert rc == 0 and out.startswith(WARN := "levain: warning (charter not yet enforced):")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    rc, out = _run(["--repo", str(loop)], capsys)
    assert rc == 0 and WARN in out
    rc, out = _run(["--repo", "~nouser_zz/r"], capsys)
    assert rc == 0 and WARN in out


def test_a_bare_tag_a_duplicate_plan_id_and_a_quoted_repo_hint():
    problems, warnings = charter.check_text(REAL.replace("- C3: A failed read shows as unknown, never as a false all-clear. [judged: the operator]",
                                                         "- C3: [judged: the operator]"), repo_name="levain")
    assert [p.rule for p in problems] == ["criteria"] and "no statement" in problems[0].text
    problems, warnings = charter.check_text(REAL.replace("- levain#9\n", "- levain#9\n- levain#9\n"), repo_name="levain")
    assert problems == [] and any("more than once" in w for w in warnings)
    assert "--repo 'a b'" in charter.invalid_text("levain", "x", [charter.Problem(1, "r", "t")], __import__("shlex").quote("a b"))


def test_a_file_past_the_byte_cap_is_refused_before_it_is_decoded(tmp_path, capsys):
    big = tmp_path / "big.md"
    big.write_bytes(b"a" * (charter.MAX_BYTES + 1))
    rc, out = _run(["--file", str(big), "--repo-name", "levain"], capsys)
    assert rc == 0 and "unreadable (larger than" in out


# ---- levain charter init NAME and the placeholder refusal (levain#89) ----
def _git_repo(path):
    import subprocess
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path


def _store_file(path, rows):
    import json
    lines = []
    for iid, decision in rows:
        repo, _, n = iid.partition("#")
        lines.append(json.dumps({"v": 1, "op": "add", "id": iid, "at": "2026-11-13T12:05:09Z", "by": "desk 21", "repo": repo,
                                 "title": "t", "words": "w", "origin": "np-4f29d079", "done": "d", "done_kind": "document",
                                 "tier": "sonnet", "depends_on": []}))
        if decision:
            lines.append(json.dumps({"v": 1, "op": "decide", "id": iid, "at": "2026-11-13T12:06:09Z", "by": "desk 21",
                                     "decision": decision, "words": "w", "date": "2026-11-13"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fill(text):
    import re
    return re.sub(r"<fill:[^<>]*>", "filled", text)


def test_a_placeholder_token_is_a_problem_on_any_line_and_the_real_charter_has_none():
    assert [p for p in charter.check_text(REAL, repo_name="levain")[0] if p.rule == "placeholder"] == []
    probs, _ = charter.check_text(REAL.replace("Python 3.12 or later", "<fill: version> or later"), repo_name="levain")
    hit = [p for p in probs if p.rule == "placeholder"]
    assert len(hit) == 1 and hit[0].text == f"placeholder: <fill: version> on line {hit[0].line} is not filled"


def test_only_the_fill_token_is_refused_and_other_angle_brackets_are_prose():
    for ok in ("<br>", "Vec<T>", "p99<200ms and p50>5ms", "<>", "< todo>", "[check: pytest <a.txt >out]"):
        probs, _ = charter.check_text(REAL.replace("Python 3.12 or later", f"{ok} or later"), repo_name="levain")
        assert [p for p in probs if p.rule == "placeholder"] == [], ok
    for bad in ("<fill:>", "<fill: x>"):
        probs, _ = charter.check_text(REAL.replace("Python 3.12 or later", f"{bad} or later"), repo_name="levain")
        assert len([p for p in probs if p.rule == "placeholder"]) == 1, bad


def test_init_leaves_no_file_when_the_write_fails_and_refuses_a_trailing_newline_name(tmp_path, capsys, monkeypatch):
    import os
    repo = _git_repo(tmp_path / "levain")
    def boom(fd):
        raise OSError("disk")
    monkeypatch.setattr(os, "fsync", boom)
    assert main(["charter", "init", "levain", "--repo", str(repo)]) == 1
    assert "cannot write" in capsys.readouterr().err
    assert sorted(p.name for p in repo.iterdir()) == [".git"]
    monkeypatch.undo()
    assert main(["charter", "init", "levain\n", "--repo", str(repo), "--stdout"]) == 1
    assert "must be lowercase" in capsys.readouterr().err


def test_init_refuses_an_oversize_skeleton_before_writing_and_says_when_the_store_has_no_item(tmp_path, capsys):
    repo = _git_repo(tmp_path / "levain")
    big = _store_file(tmp_path / "big.jsonl", [(f"levain#{i}", None) for i in range(1, charter.MAX_LINES + 1)])
    assert main(["charter", "init", "levain", "--repo", str(repo), "--items", str(big)]) == 1
    err = capsys.readouterr().err
    assert "lines" in err and str(charter.MAX_LINES) in err and not (repo / "CHARTER.md").exists()
    other = _store_file(tmp_path / "other.jsonl", [("flow#1", None)])
    assert main(["charter", "init", "levain", "--repo", str(repo), "--items", str(other), "--stdout"]) == 0
    assert "no live items of levain in the store" in capsys.readouterr().err


def test_init_writes_the_skeleton_with_the_plan_prefilled_in_id_order(tmp_path, capsys):
    repo = _git_repo(tmp_path / "levain")
    store = _store_file(tmp_path / "items.jsonl", [("levain#10", None), ("levain#2", None), ("levain#3", "cut"),
                                                   ("levain#4", "parked"), ("levain#5", "out-of-1.0"), ("flow#1", None)])
    assert main(["charter", "init", "levain", "--repo", str(repo), "--items", str(store)]) == 0
    text = (repo / "CHARTER.md").read_text(encoding="utf-8")
    assert text.startswith("# Charter: levain\nCharter-Version: 1\nExport: memory\n\n## Purpose\n<")
    assert [ln for ln in text.splitlines() if ln.startswith("## ")] == [f"## {s}" for s in charter.SECTIONS]
    assert text.split("## Plan\n")[1].split() == ["-", "levain#2", "-", "levain#10"]


def test_init_round_trip_fails_only_on_placeholders_then_passes_once_filled(tmp_path, capsys):
    repo = _git_repo(tmp_path / "levain")
    store = _store_file(tmp_path / "items.jsonl", [("levain#1", None), ("levain#2", None)])
    assert main(["charter", "init", "levain", "--repo", str(repo), "--items", str(store), "--export", "repo"]) == 0
    text = (repo / "CHARTER.md").read_text(encoding="utf-8")
    items = {"levain#1": ("levain", None), "levain#2": ("levain", None)}
    probs, warns = charter.check_text(text, repo_name="levain", items=items)
    assert probs and {p.rule for p in probs} == {"placeholder"} and warns == []
    filled, _ = charter.check_text(_fill(text), repo_name="levain", items=items)
    assert filled == []


def test_init_without_a_store_says_so_and_holds_one_placeholder_plan_line(tmp_path, capsys):
    repo = _git_repo(tmp_path / "levain")
    assert main(["charter", "init", "levain", "--repo", str(repo)]) == 0
    assert "no --items store was read" in capsys.readouterr().err
    text = (repo / "CHARTER.md").read_text(encoding="utf-8")
    assert text.endswith("## Plan\n- <fill: a board item id, NAME#n>\n")
    assert charter.check_text(_fill(text), repo_name="levain")[0][0].rule == "plan"   # `- filled` is not an item id: not a charter either


def test_init_never_overwrites_and_refuses_a_bad_name_and_a_non_repo(tmp_path, capsys):
    repo = _git_repo(tmp_path / "levain")
    (repo / "CHARTER.md").write_text("mine\n", encoding="utf-8")
    assert main(["charter", "init", "levain", "--repo", str(repo)]) == 1
    assert str(repo / "CHARTER.md") in capsys.readouterr().err
    assert (repo / "CHARTER.md").read_text(encoding="utf-8") == "mine\n"
    for bad in ("Levain", "1x", "a b", "", "a#1"):
        assert main(["charter", "init", bad, "--repo", str(repo), "--stdout"]) == 1
        assert "must be lowercase" in capsys.readouterr().err
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["charter", "init", "levain", "--repo", str(plain)]) == 1
    assert "not inside a git work tree" in capsys.readouterr().err
    assert not (plain / "CHARTER.md").exists()


def test_init_stdout_prints_and_writes_nothing_and_a_torn_store_writes_nothing(tmp_path, capsys):
    repo = _git_repo(tmp_path / "levain")
    assert main(["charter", "init", "levain", "--repo", str(repo), "--stdout"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Charter: levain\n") and not (repo / "CHARTER.md").exists()
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    assert main(["charter", "init", "levain", "--repo", str(repo), "--items", str(bad)]) == 1
    assert not (repo / "CHARTER.md").exists()
