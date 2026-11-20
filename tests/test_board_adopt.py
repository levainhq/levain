"""Design revision 8 (levain 37, 1118-6): a ceremony ADOPTS the items that exist before it, by identity, never by copy.
Every test goes through the CLI (``levain board ...``, ``levain charter init``) on a scratch store."""
from __future__ import annotations

import json
import os
import re

import pytest

from levain.cli import main
from tests.test_board_registry import (  # noqa: F401  (fixtures and helpers of the registry tests)
    A, CHARTER, HASH_A, append_legacy_decide, bare_store, decision_seal, git, home, item_lines, land, migration_of, mkrepo, ok, reg_lines, run, store)


def seeded(capsys, bare_store, repo_names, per=3):
    """Items that exist BEFORE any ceremony (the registry is not yet active, so I1 is off): ``{repo: [ids]}``."""
    out = {}
    for name in repo_names:
        out[name] = [ok(capsys, "add", "--repo", name, "--title", f"old {name} {n}", "--origin", "desk 1", *A)
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
    ok(capsys, "abandon-done", "alpha", *A)
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


# ---------------------------------------------------------------------------------------------------------- adoptfix1 (L1 + L2)


def test_k1_an_adopted_item_keyed_to_this_ceremony_is_refused_so_abort_never_cuts_an_adopted_item(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    plain = [ok(capsys, "add", "--repo", "alpha", "--title", f"old {n}", "--origin", "d", *A) for n in range(2)]
    keyed = ok(capsys, "add", "--repo", "alpha", "--title", "pre-keyed", "--origin", "d", "--key", "foo:k", "--payload-hash", HASH_A, *A)
    (bare_store / "repos.jsonl").touch()                       # the registry becomes active after the items exist (I1 was off)
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(man(tmp_path, plan_items=["k"], adopt_items=[plain[0], keyed])),
                     "--words", "y", "--migration", "foo", *A)
    assert rc == 2 and keyed in err and "keyed foo:k to this ceremony" in err and plain[0] not in err
    assert [r for r in reg_lines(bare_store) if r["op"] == "begin-track"] == []
    # the real path: the same item is the ceremony's keyed item (not adopted); abort cuts it and only it
    ok(capsys, "begin-track", "alpha", "--manifest", str(man(tmp_path, "m2.json", plan_items=["k"], adopt_items=plain)),
       "--words", "y", "--migration", "foo", *A)
    assert ok(capsys, "abandon-track", "alpha", "--words", "no", *A) == f"aborting alpha; cut 1 items: {keyed}"
    assert [r["id"] for r in item_lines(bare_store) if r.get("op") == "decide"] == [keyed]


@pytest.mark.parametrize("decision", ["parked", "out-of-horizon", "cut"])
def test_k2_begin_track_refuses_an_adopted_item_that_is_parked_or_out_of_horizon(decision_seal, bare_store, tmp_path, mkrepo, capsys, decision):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    ok(capsys, "decide", "--id", ids[1], "--decision", decision, *(["--horizon", "launch"] if decision == "out-of-horizon" else []), "--words", "w", *A)
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(man(tmp_path, adopt_items=[ids[0], ids[1]])), "--words", "y", *A)
    assert rc == 2 and ids[1] in err and decision in err and ids[0] not in err
    assert [r for r in reg_lines(bare_store) if r["op"] == "begin-track"] == []


def test_k2_begin_track_refuses_an_adopted_item_a_record_stored_with_the_legacy_word_put_out_of_horizon(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    """The writer refuses ``out-of-1.0`` now, so the raw line is written by hand: the registry must read it as ``out-of-horizon``."""
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    append_legacy_decide(bare_store, ids[1])
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(man(tmp_path, adopt_items=[ids[0], ids[1]])), "--words", "y", *A)
    assert rc == 2 and ids[1] in err and "out-of-horizon" in err and "out-of-1.0" not in err and ids[0] not in err
    assert [r for r in reg_lines(bare_store) if r["op"] == "begin-track"] == []


def test_k5_every_bad_adopt_entry_is_named_in_one_refusal(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ids = seeded(capsys, bare_store, ["alpha", "beta"])
    ok(capsys, "decide", "--id", ids["alpha"][2], "--decision", "cut", "--words", "gone", *A)
    ok(capsys, "decide", "--id", ids["alpha"][1], "--decision", "parked", "--words", "later", *A)
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    bad = ["alpha#99", ids["beta"][0], ids["alpha"][2], ids["alpha"][1]]
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(man(tmp_path, adopt_items=[ids["alpha"][0], *bad])), "--words", "y", *A)
    assert rc == 2 and "4 bad entries" in err and all(i in err for i in bad) and err.count("--manifest adopt_items") == 1
    assert ids["alpha"][0] not in err


def test_k4_the_track_record_carries_the_adopted_ids(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    begin(capsys, tmp_path, a, "alpha", adopt_items=ids)
    ok(capsys, "decide", "--id", ids[2], "--decision", "cut", "--words", "dropped", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan="\n".join(f"- {i}" for i in ids[:2])))
    ok(capsys, "track", "alpha", "--words", "go", *A)
    track = [r for r in reg_lines(bare_store) if r["op"] == "track"]
    assert len(track) == 1 and track[0]["adopted"] == ids[:2]                   # what the switch moved in; the cut one is not
    assert ok(capsys, "repos").count("alpha") >= 1                              # the registry still folds with the new key


def test_k4_a_track_record_with_a_malformed_adopted_is_torn(bare_store):
    from levain.desk import repos_fold as rf
    sid = "s"
    base = {"v": 1, "store": sid, "at": "2026-11-18T10:00:00Z", "by": "d", "repo": "x", "words": ""}
    lines = [json.dumps({**base, "op": "register", "path": "/p", "origin": "", "root": ["a"]}),
             json.dumps({**base, "op": "track", "migration": "m", "charter_blob": "b", "retire": [], "adopted": "x#1"})]
    assert rf.fold_repos(lines, store_id=sid).torn


def _fold_track(manifest_extra, **track_extra):
    """Fold a hand-written registry: register, begin-track (manifest = plan_items/contract_items + ``manifest_extra``), track."""
    from levain.desk import repos_fold as rf
    sid = "s"
    base = {"v": 1, "store": sid, "at": "2026-11-18T10:00:00Z", "by": "d", "repo": "alpha", "words": ""}
    man_ = {"plan_items": [], "contract_items": [], **manifest_extra}
    lines = [json.dumps({**base, "op": "register", "path": "/p", "origin": "", "root": ["a"]}),
             json.dumps({**base, "op": "begin-track", "migration": "m", "manifest": man_, "manifest_sha256": "a" * 64,
                         "charter_before": "c"}),
             json.dumps({**base, "op": "track", "migration": "m", "charter_blob": "b", "retire": [], **track_extra})]
    return rf.fold_repos(lines, store_id=sid)


def test_f1_a_track_record_without_adopted_is_a_problem_when_the_manifest_adopts():
    f = _fold_track({"adopt_items": ["alpha#1", "alpha#2"]})
    assert f.problems and "alpha#1" in " ".join(f.problems) and f.repos["alpha"].mode == "tracking"


def test_f1_a_track_record_with_a_duplicate_adopted_is_refused():
    f = _fold_track({"adopt_items": ["alpha#1", "alpha#2"]}, adopted=["alpha#1", "alpha#1"])
    assert f.torn or f.problems
    assert f.repos["alpha"].mode == "tracking"


def test_f1_a_track_record_naming_an_id_the_manifest_never_adopted_is_a_problem():
    f = _fold_track({"adopt_items": ["alpha#1"]}, adopted=["alpha#1", "beta#9"])
    assert f.problems and "beta#9" in " ".join(f.problems) and f.repos["alpha"].mode == "tracking"


def test_f1_adopted_ids_against_a_manifest_with_no_adopt_items_are_a_problem():
    f = _fold_track({}, adopted=["beta#9"])
    assert f.problems and "beta#9" in " ".join(f.problems) and f.repos["alpha"].mode == "tracking"


def test_f1_adopted_ids_against_an_empty_adopt_items_are_a_problem():
    f = _fold_track({"adopt_items": []}, adopted=["beta#9"])
    assert f.problems and "beta#9" in " ".join(f.problems) and f.repos["alpha"].mode == "tracking"


def test_f1_an_empty_adopted_against_a_manifest_with_no_adopt_items_folds_clean():
    f = _fold_track({}, adopted=[])
    assert not f.problems and not f.torn and f.repos["alpha"].mode == "tracked"


def test_f1_a_strict_subset_of_the_manifest_adopt_items_folds_clean():
    f = _fold_track({"adopt_items": ["alpha#1", "alpha#2"]}, adopted=["alpha#2"])
    assert not f.problems and not f.torn and f.repos["alpha"].mode == "tracked"


def test_f1_a_record_from_before_rev_8_folds_clean():
    f = _fold_track({})
    assert not f.problems and not f.torn and f.repos["alpha"].mode == "tracked"


def test_f2_a_repeated_adopt_items_id_is_named():
    from levain.desk import repos_fold as rf
    why = rf.check_manifest({"plan_items": [], "contract_items": [], "adopt_items": ["alpha#99", "alpha#3", "alpha#99"]})
    assert why and "distinct existing item ids (X#N)" in why and why.endswith("repeated alpha#99")


def test_f3_next_step_reads_the_items_once(decision_seal, bare_store, tmp_path, mkrepo, capsys, monkeypatch):
    from levain.desk import board_registry as br
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    begin(capsys, tmp_path, a, "alpha", adopt_items=ids[:1])
    calls = []
    real = br._items_facts
    monkeypatch.setattr(br, "_items_facts", lambda *x, **k: (calls.append(1), real(*x, **k))[1])
    out = run(capsys, "repos")[1]
    assert "neither adopted nor keyed" in out and len(calls) == 1


def test_k6_the_reporter_names_the_live_items_neither_adopted_nor_keyed(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"], per=26)["alpha"]
    ok(capsys, "decide", "--id", ids[25], "--decision", "parked", "--words", "later", *A)        # parked: not live, not named
    begin(capsys, tmp_path, a, "alpha", adopt_items=ids[:1])
    rc, out, _ = run(capsys, "repos")
    assert rc == 0 and "24 live items of alpha neither adopted nor keyed" in out and "and 4 more" in out
    assert ids[1] in out and ids[20] in out and ids[21] not in out and not re.search(r"alpha#1\b", out.split("neither adopted")[1]) and ids[25] not in out
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}"))
    rc, out, _ = run(capsys, "repos")
    assert "levain board track alpha" in out and "24 live items" in out                       # information, never a refusal
    ok(capsys, "track", "alpha", "--words", "go", *A)


def test_k6_no_unadopted_live_item_no_note(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    begin(capsys, tmp_path, a, "alpha", adopt_items=ids)
    assert "neither adopted nor keyed" not in run(capsys, "repos")[1]


def test_k3_charter_init_refuses_when_the_registry_cannot_be_read_and_keeps_today_s_prefill_when_untracked(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    items = str(bare_store / "items.jsonl")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert main(["charter", "init", "alpha", "--stdout", "--repo", str(a), "--items", items]) == 0      # untracked: today's prefill
    plan_out = capsys.readouterr().out
    assert all(f"- {i}\n" in plan_out for i in ids)
    with open(bare_store / "repos.jsonl", "a") as fh:
        fh.write("garbage\n")
    for argv in (["--stdout", "--repo", str(a)], ["--stdout"]):       # --stdout skips the wrong-repo guard: the prefill's own refusal
        rc = main(["charter", "init", "alpha", *argv, "--items", items])
        cap = capsys.readouterr()
        assert rc == 2 and "cannot tell whether alpha is tracking" in cap.err and "registry is unreadable" in cap.err and cap.out == "", argv
    rc = main(["charter", "init", "alpha", "--repo", str(a), "--items", items])                # the writing path: the guard refuses first
    assert rc == 2 and "registry is unreadable" in capsys.readouterr().err
    assert not (a / "CHARTER.md").exists()


def test_k_an_adopted_item_parked_between_begin_and_track_stays_in_the_plan(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    begin(capsys, tmp_path, a, "alpha", adopt_items=ids[:2])
    ok(capsys, "decide", "--id", ids[1], "--decision", "parked", "--words", "later", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}"))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and f"the manifest's items not in the charter's Plan: {ids[1]}" in err       # parked is not cut: still adopted
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}\n- {ids[1]}"))
    ok(capsys, "track", "alpha", "--words", "go", *A)


def test_k_an_adopted_item_cut_then_reopened_between_begin_and_track_returns_to_the_plan(decision_seal, bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ids = seeded(capsys, bare_store, ["alpha"])["alpha"]
    begin(capsys, tmp_path, a, "alpha", adopt_items=ids[:2])
    ok(capsys, "decide", "--id", ids[1], "--decision", "cut", "--words", "gone", *A)
    ok(capsys, "decide", "--id", ids[1], "--decision", "reopen", "--words", "back", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}"))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and f"the manifest's items not in the charter's Plan: {ids[1]}" in err
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}\n- {ids[1]}"))
    ok(capsys, "track", "alpha", "--words", "go", *A)
