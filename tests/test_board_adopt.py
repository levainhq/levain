"""Design revision 8 (levain 37, 1118-6): a ceremony ADOPTS the items that exist before it, by identity, never by copy.
Every test goes through the CLI (``levain board ...``, ``levain charter init``) on a scratch store."""
from __future__ import annotations

import json
import os

import pytest

from levain.cli import main
from tests.test_board_registry import (  # noqa: F401  (fixtures and helpers of the registry tests)
    A, CHARTER, HASH_A, bare_store, decision_seal, git, home, item_lines, land, migration_of, mkrepo, ok, reg_lines, run, store)


def seeded(capsys, bare_store, repo_names, per=3):
    """Items that exist BEFORE any ceremony (the registry is not yet active, so I1 is off): ``{repo: [ids]}``."""
    out = {}
    for name in repo_names:
        out[name] = [ok(capsys, "add", "--repo", name, "--title", f"old {name} {n}", "--origin", "desk 1", "--payload-hash", HASH_A, *A)
                     for n in range(per)]
    (bare_store / "repos.jsonl").touch()
    return out


def man(tmp_path, name="manifest.json", **kw):
    p = tmp_path / name
    p.write_text(json.dumps({"plan_items": [], "contract_items": [], **kw}))
    return p


def begin(capsys, tmp_path, repo, name, **kw):
    ok(capsys, "register", name, "--path", str(repo), "--words", "first", *A)
    ok(capsys, "begin-track", name, "--manifest", str(man(tmp_path, **kw)), "--words", "yes", *A)


def test_adopt_then_track_leaves_the_adopted_items_untouched(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    before = (bare_store / "items.jsonl").read_bytes()
    begin(capsys, tmp_path, a, "alpha", plan_items=["n"], adopt_items=ids[:2])
    m = migration_of(bare_store, "alpha")
    new = ok(capsys, "add", "--repo", "alpha", "--title", "new", "--origin", "d", "--key", f"{m}:n", "--payload-hash", HASH_A, *A)
    after_add = (bare_store / "items.jsonl").read_bytes()
    assert after_add.startswith(before)                                   # the adopted items' lines: not one byte rewritten
    rc, out, _ = run(capsys, "repos")
    assert "next: make the charter's Plan match" in out or "next: land CHARTER.md" in out
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan="\n".join(f"- {i}" for i in [*ids[:2], new])))
    assert "next: levain board track alpha" in run(capsys, "repos")[1]
    assert ok(capsys, "track", "alpha", "--words", "go", *A) == f"tracked alpha (migration {m})"
    assert (bare_store / "items.jsonl").read_bytes() == after_add         # track writes no item record
    assert [r["id"] for r in item_lines(bare_store) if r.get("op") == "add"] == [*ids, new]    # same ids, no copies
    assert reg_lines(bare_store)[-2]["manifest"]["adopt_items"] == ids[:2]


def test_track_refuses_a_plan_that_misses_an_adopted_item_or_holds_an_id_adopted_by_nothing(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    begin(capsys, tmp_path, a, "alpha", adopt_items=ids[:2])
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}"))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and f"the manifest's items not in the charter's Plan: {ids[1]}" in err
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan="\n".join(f"- {i}" for i in ids)))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and f"the charter's Plan ids not in the manifest: {ids[2]}" in err
    ok(capsys, "decide", "--id", ids[1], "--decision", "cut", "--words", "dropped", *A)   # an adopted item cut mid-ceremony leaves the set
    ok(capsys, "decide", "--id", ids[2], "--decision", "cut", "--words", "dropped", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}"))
    ok(capsys, "track", "alpha", "--words", "go", *A)


def test_begin_track_refuses_each_bad_adopt_entry(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ids = seeded(capsys, bare_store, ["alpha", "beta"])
    ok(capsys, "decide", "--id", ids["alpha"][2], "--decision", "cut", "--words", "gone", *A)
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    cases = {"absent": ([ "alpha#99"], "alpha#99"), "other repo": ([ids["beta"][0]], ids["beta"][0]),
             "cut": ([ids["alpha"][2]], ids["alpha"][2]), "repeated": ([ids["alpha"][0], ids["alpha"][0]], "distinct")}
    for label, (entries, said) in cases.items():
        rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(man(tmp_path, adopt_items=entries)), "--words", "y", *A)
        assert rc == 2 and said in err, (label, err)
        assert [r for r in reg_lines(bare_store) if r["op"] == "begin-track"] == [], label    # nothing written
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(man(tmp_path, adopt_items=["nope"])), "--words", "y", *A)
    assert rc == 2 and "adopt_items" in err


def test_abort_after_adopting_cuts_the_keyed_items_and_leaves_the_adopted_exactly_as_before(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    before = (bare_store / "items.jsonl").read_bytes()
    begin(capsys, tmp_path, a, "alpha", plan_items=["n"], adopt_items=ids)
    m = migration_of(bare_store, "alpha")
    new = ok(capsys, "add", "--repo", "alpha", "--title", "new", "--origin", "d", "--key", f"{m}:n", "--payload-hash", HASH_A, *A)
    out = ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    assert out == f"aborting alpha; cut 1 items: {new}"
    ok(capsys, "abandon-done", "alpha", "--words", "done", *A)
    lines = (bare_store / "items.jsonl").read_bytes().split(b"\n")
    assert b"\n".join(lines[:len(before.split(b"\n")) - 1]) + b"\n" == before         # the adopted items' lines: untouched
    cut = [r["id"] for r in item_lines(bare_store) if r.get("op") == "decide"]
    assert cut == [new]


def test_charter_init_prefills_adopt_and_keyed_in_tracking_mode_and_today_s_rule_otherwise(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"], per=4)["alpha"]
    items = str(bare_store / "items.jsonl")
    assert main(["charter", "init", "alpha", "--stdout", "--repo", str(a), "--items", items]) == 0
    untracked = capsys.readouterr().out
    assert all(f"- {i}\n" in untracked for i in ids)                       # untracked: every live item (today's rule)
    begin(capsys, tmp_path, a, "alpha", plan_items=["n"], adopt_items=ids[:2])
    m = migration_of(bare_store, "alpha")
    new = ok(capsys, "add", "--repo", "alpha", "--title", "new", "--origin", "d", "--key", f"{m}:n", "--payload-hash", HASH_A, *A)
    assert main(["charter", "init", "alpha", "--stdout", "--repo", str(a), "--items", items]) == 0
    tracking = capsys.readouterr().out
    plan = [ln for ln in tracking.split("## Plan\n")[1].split("\n") if ln.startswith("- ")]
    assert plan == [f"- {i}" for i in [*ids[:2], new]]                     # adopt U keyed, not the other two live items
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    assert main(["charter", "init", "alpha", "--stdout", "--repo", str(a), "--items", items]) == 0
    aborting = capsys.readouterr().out
    assert f"- {ids[3]}\n" in aborting                                       # any other mode: today's rule


def test_begin_track_refuses_a_retire_entry_in_an_unregistered_repo(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest",
                     str(man(tmp_path, retire=[{"repo": "flow", "path": "projects/alpha/next.md"}])), "--words", "y", *A)
    assert rc == 2 and "flow" in err and "levain board register" in err
    assert [r for r in reg_lines(store) if r["op"] == "begin-track"] == []
    f = mkrepo("flow")
    ok(capsys, "register", "flow", "--path", str(f), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest",
       str(man(tmp_path, retire=[{"repo": "flow", "path": "projects/alpha/next.md"}])), "--words", "y", *A)


def test_a_gitignored_retire_path_is_read_in_the_working_tree_i3(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    (a / ".gitignore").write_text("project_memory/\n")
    git(a, "add", ".")
    git(a, "commit", "-qm", "ignore")
    git(a, "push", "-q", "origin", "main")
    git(a, "fetch", "-q")
    (a / "project_memory").mkdir()
    (a / "project_memory" / "STATUS.md").write_text("pre-track content\n")
    assert git(a, "status", "--porcelain") == ""                           # git does not see it: nothing to commit
    track_retire = [{"repo": "alpha", "path": "project_memory/STATUS.md"}]
    begin(capsys, tmp_path, a, "alpha", plan_items=["n"], retire=track_retire)
    m = migration_of(store, "alpha")
    iid = ok(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", "--key", f"{m}:n", "--payload-hash", HASH_A, *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {iid}"))
    ok(capsys, "track", "alpha", "--words", "go", *A)
    rc, out, _ = run(capsys, "repos")
    assert rc == 3 and f"stale: {os.path.realpath(a)}/project_memory/STATUS.md still holds pre-track content" in out
    (a / "project_memory" / "STATUS.md").write_text("<!-- levain: tracked stub -->\nretired\n")
    rc, out, _ = run(capsys, "repos")
    assert rc == 0 and "stale" not in out
