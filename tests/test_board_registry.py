"""The registry (levain board repos|register|begin-track|track|abandon-track|abandon-done|untrack|pause|resume|move|ack-torn,
use --import-focus): ``repos.jsonl``, its pure fold, I1 on ``add``, I5 on the launch, T2-a..T2-i. Every repo is a scratch
repository with a bare origin under the test's own temp; the levain home is a scratch home."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_registry as br
from levain.desk import board_store as bs
from levain.desk import charter, repos_fold as rf
from levain.desk.board_state import fold as item_fold
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordError, CoordStore

A = ["--by", "desk 1", "--author-kind", "operator"]
SID = "s" * 32
HASH_A = "a" * 64
CHARTER = """# Charter: {name}
Charter-Version: 1
Export: repo

## Purpose
Test.

## Criteria
- C1: it works [judged: phill]

## Constraints
- K1: none

## Plan
{plan}
"""


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env).stdout.decode().strip()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "lhome"
    monkeypatch.setenv("LEVAIN_HOME", str(h))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    monkeypatch.delenv("LEVAIN_LAUNCH_KEY", raising=False)
    return h


@pytest.fixture
def mkrepo(tmp_path):
    def make(name):
        bare = tmp_path / "bare" / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        d = tmp_path / "repos" / name
        subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
        (d / "README.md").write_text(f"hi {name}\n")
        git(d, "add", ".")
        git(d, "commit", "-qm", "init")
        git(d, "remote", "add", "origin", str(bare))
        git(d, "push", "-q", "origin", "main")
        git(d, "fetch", "-q")
        return d
    return make


def land(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", f"land {name}")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q")


def run(capsys, *argv):
    rc = main(["board", *argv])
    out, err = capsys.readouterr()
    return rc, out.strip(), err.strip()


def ok(capsys, *argv):
    rc, out, err = run(capsys, *argv)
    assert rc == 0, err
    return out


@pytest.fixture
def store(home, tmp_path, capsys):
    d = tmp_path / "store"
    ok(capsys, "use", str(d))
    return Path(os.path.realpath(d))


def reg_lines(store):
    return [json.loads(ln) for ln in (store / "repos.jsonl").read_text("utf-8").splitlines()]


def item_lines(store):
    return [json.loads(ln) for ln in (store / "items.jsonl").read_text("utf-8").splitlines()]


def manifest(tmp_path, **kw):
    doc = {"plan_items": ["a"], "contract_items": [], **kw}
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(doc))
    return p


def begin(capsys, tmp_path, repo, name, words="yes", **kw):
    ok(capsys, "register", name, "--path", str(repo), "--words", "first", *A)
    ok(capsys, "begin-track", name, "--manifest", str(manifest(tmp_path, **kw)), "--words", words, *A)


def migration_of(store, name):
    return [r for r in reg_lines(store) if r["op"] == "begin-track" and r["repo"] == name][-1]["migration"]


def add_item(capsys, name, m, k, title="t"):
    return ok(capsys, "add", "--repo", name, "--title", title, "--origin", "desk 1", "--key", f"{m}:{k}", "--payload-hash", HASH_A, *A)


def track_ready(capsys, tmp_path, store, repo, name, **kw):
    """Register, begin, add item a, land a matching charter: the repo is ready for ``track``."""
    begin(capsys, tmp_path, repo, name, **kw)
    m = migration_of(store, name)
    iid = add_item(capsys, name, m, "a")
    land(repo, "CHARTER.md", CHARTER.format(name=name, plan=f"- {iid}"))
    return m, iid


# --------------------------------------------------------------------------------------------------------------------
# the pure fold


def rec(op, repo="x", n=0, **kw):
    base = {"v": 1, "store": SID, "op": op, "repo": repo, "at": f"2026-11-17T10:00:{n:02d}Z", "by": "d", "words": "w"}
    if op in ("register", "move"):
        base.update(path="/p/" + repo, origin="o", root=["r1"])
    if op == "begin-track":
        base.update(migration="m1", manifest={"plan_items": ["a"], "contract_items": []}, manifest_sha256="a" * 64, charter_before="none")
    if op == "track":
        base.update(migration="m1", charter_blob="b1", retire=[{"repo": "x", "path": "next.md"}])
    if op in ("abandon-track",):
        base.update(migration="m1")
    base.update(kw)
    return json.dumps(base)


def test_the_fold_is_pure_and_imports_nothing_from_levain():
    src = Path(rf.__file__).read_text("utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any(n == "levain" or n.startswith("levain.") for n in names), names


def test_a_whole_ceremony_folds_through_every_mode():
    lines = [rec("register", n=1), rec("begin-track", n=2, owner="desk 3"), rec("track", n=3), rec("pause", n=4)]
    f = rf.fold_repos(lines, store_id=SID)
    r = f.repos["x"]
    assert (r.mode, r.attention, r.migration, r.owner, r.charter_blob, r.charter_before) == ("tracked", "paused", "m1", "desk 3", "b1", "none")
    assert r.retire == ({"repo": "x", "path": "next.md"},) and r.since == "2026-11-17T10:00:03Z" and not f.torn and not f.problems
    f = rf.fold_repos([rec("register", n=1), rec("begin-track", n=2), rec("abandon-track", n=3), rec("abandon-done", n=4)], store_id=SID)
    assert f.repos["x"].mode == "untracked"
    f = rf.fold_repos(lines + [rec("untrack", n=5)], store_id=SID)
    assert f.repos["x"].mode == "untracked"


def test_modes_are_exactly_the_four():
    assert rf.MODES == ("untracked", "tracking", "aborting", "tracked")


def test_a_repeated_record_is_a_no_op_and_an_unallowed_one_is_a_problem_not_a_tear():
    f = rf.fold_repos([rec("register", n=1), rec("register", n=2), rec("begin-track", n=3), rec("begin-track", n=4)], store_id=SID)
    assert f.repos["x"].mode == "tracking" and f.repos["x"].since == "2026-11-17T10:00:03Z" and not f.problems
    f = rf.fold_repos([rec("track", n=1), rec("register", n=2), rec("track", n=3), rec("register", n=4, path="/elsewhere")], store_id=SID)
    assert not f.torn and len(f.problems) == 3
    assert "never registered" in f.problems[0] and "not tracking" in f.problems[1] and "use move" in f.problems[2]


@pytest.mark.parametrize("bad", [
    "not json", "[1]", json.dumps({"v": 2, "op": "register"}), rec("explode"), rec("register", path="relative"),
    rec("register", root=[]), rec("track", retire=[{"repo": "x"}]), rec("begin-track", manifest={"plan_items": "a", "contract_items": []}),
    rec("pause", store="another"), rec("pause", by=""), rec("pause", repo="UPPER")])
def test_a_line_the_fold_cannot_trust_makes_the_registry_torn(bad):
    f = rf.fold_repos([rec("register", n=1), bad, rec("pause", n=2)], store_id=SID)
    assert f.torn and f.torn_why.startswith("line 2")
    assert f.torn_lines[0][0] == 2 and f.torn_lines[0][1] == hashlib.sha256(bad.encode()).hexdigest()


def test_another_stores_record_is_evidence_from_another_store():
    f = rf.fold_repos([rec("register", store="other")], store_id=SID)
    assert f.torn and "another store" in f.torn_why
    assert not rf.fold_repos([rec("register", store="other")]).torn           # no store id given: not judged


def test_an_unterminated_tail_is_torn_even_when_it_parses():
    f = rf.fold_repos([rec("register")], store_id=SID, unterminated_tail=True)
    assert f.torn and "no newline" in f.torn_why


def test_ack_torn_skips_exactly_that_line_by_its_hash():
    bad = "{not json"
    ack = json.dumps({"v": 1, "store": SID, "op": "ack-torn", "line": 2, "sha256": hashlib.sha256(bad.encode()).hexdigest(),
                      "why": "w", "at": "2026-11-17T10:00:05Z", "by": "d"})
    f = rf.fold_repos([rec("register"), bad, ack, rec("pause", n=9)], store_id=SID)
    assert not f.torn and f.acked == (2,) and f.repos["x"].attention == "paused"
    wrong = ack.replace(hashlib.sha256(bad.encode()).hexdigest(), "f" * 64)
    f = rf.fold_repos([rec("register"), bad, wrong], store_id=SID)
    assert f.torn and any("different sha256" in p for p in f.problems)
    f = rf.fold_repos([rec("register"), "{other", ack], store_id=SID)           # an ack for another line's bytes
    assert f.torn
    f = rf.fold_repos([rec("register"), rec("pause", n=2), ack.replace('"line": 2', '"line": 2')], store_id=SID)
    assert not f.torn and any("not torn" in p for p in f.problems) and f.repos["x"].attention == "paused"


def test_fields_the_fold_does_not_know_are_ignored():
    f = rf.fold_repos([rec("register", prev="0" * 64, author={"kind": "agent", "name": "a"}, grade="asserted", future="x")], store_id=SID)
    assert not f.torn and not f.problems and "x" in f.repos


# --------------------------------------------------------------------------------------------------------------------
# register, move, pause, resume


def test_register_records_path_origin_and_sorted_roots_in_the_page_shape(store, mkrepo, capsys):
    a = mkrepo("alpha")
    assert ok(capsys, "register", "alpha", "--path", str(a), "--words", "he said so", *A) == "registered alpha"
    (r,) = reg_lines(store)
    assert list(r)[:8] == ["v", "store", "op", "repo", "at", "prev", "by", "words"]
    assert r["store"] == (store / "store.id").read_text().strip() and r["op"] == "register"
    assert r["path"] == os.path.realpath(a) and r["origin"].endswith("alpha.git")
    assert r["root"] == sorted(git(a, "rev-list", "--max-parents=0", "HEAD").split())
    assert r["author"] == {"kind": "operator", "name": "desk 1"} and r["grade"] == "asserted"
    assert r["prev"] == hashlib.sha256(b"").hexdigest()


def test_the_records_form_a_chain_through_the_one_writer(store, mkrepo, capsys):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "register", "beta", "--path", str(b), "--words", "w", *A)
    ok(capsys, "pause", "beta", "--words", "w", *A)
    lines = (store / "repos.jsonl").read_text("utf-8").splitlines()
    recs = [json.loads(ln) for ln in lines]
    assert recs[1]["prev"] == hashlib.sha256(lines[0].encode()).hexdigest()
    assert recs[2]["prev"] == hashlib.sha256(lines[1].encode()).hexdigest()
    for ln in lines:
        assert ln.isascii() and ln == json.dumps(json.loads(ln), separators=(",", ":"), ensure_ascii=True)


def test_register_twice_is_a_no_op_and_another_path_names_move(store, mkrepo, capsys):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A) == "already registered alpha"
    assert len(reg_lines(store)) == 1
    rc, _, err = run(capsys, "register", "alpha", "--path", str(b), "--words", "w", *A)
    assert rc == 2 and "levain board move alpha" in err
    rc, _, err = run(capsys, "register", "gamma", "--path", str(a), "--words", "w", *A)
    assert rc == 2 and "already registered as alpha" in err


def test_register_refuses_a_non_repository_a_bad_name_and_an_empty_word(store, tmp_path, mkrepo, capsys):
    (tmp_path / "plain").mkdir()
    rc, _, err = run(capsys, "register", "plain", "--path", str(tmp_path / "plain"), "--words", "w", *A)
    assert rc == 2 and "not a git repository" in err
    a = mkrepo("alpha")
    rc, _, err = run(capsys, "register", "Alpha", "--path", str(a), "--words", "w", *A)
    assert rc == 2 and "must match" in err
    rc, _, err = run(capsys, "register", "alpha", "--path", str(a), "--words", " ", *A)
    assert rc == 2 and "words must not be empty" in err
    rc, _, err = run(capsys, "register", "alpha", "--path", str(a / "sub"), "--words", "w", *A)
    assert rc == 2


def test_no_verb_writes_before_the_pointer_exists(home, mkrepo, capsys):
    a = mkrepo("alpha")
    rc, _, err = run(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert rc == 2 and bs.NO_STORE in err
    assert not home.exists() or not list(home.rglob("repos.jsonl"))


def test_pause_and_resume_are_idempotent(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert ok(capsys, "resume", "alpha", "--words", "w", *A) == "already resumed alpha"
    assert ok(capsys, "pause", "alpha", "--words", "w", *A) == "paused alpha"
    assert ok(capsys, "pause", "alpha", "--words", "w", *A) == "already paused alpha"
    assert ok(capsys, "resume", "alpha", "--words", "w", *A) == "resumed alpha"
    assert [r["op"] for r in reg_lines(store)] == ["register", "pause", "resume"]
    rc, _, err = run(capsys, "pause", "ghost", "--words", "w", *A)
    assert rc == 2 and "not registered" in err


def test_move_rebases_onto_a_copy_of_the_same_repository_and_refuses_another(store, tmp_path, mkrepo, capsys):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "move", "alpha", "--path", str(b), "--words", "w", *A)
    assert rc == 2 and "root commits differ" in err
    shutil.copytree(a, tmp_path / "alpha2")
    assert ok(capsys, "move", "alpha", "--path", str(tmp_path / "alpha2"), "--words", "w", *A) == "moved alpha"
    assert ok(capsys, "move", "alpha", "--path", str(tmp_path / "alpha2"), "--words", "w", *A) == "already at that path alpha"
    assert br.read_registry(bs.resolve_store(None)).fold.repos["alpha"].path == os.path.realpath(tmp_path / "alpha2")
    rc, _, err = run(capsys, "move", "ghost", "--path", str(a), "--words", "w", *A)
    assert rc == 2 and "not registered" in err


# --------------------------------------------------------------------------------------------------------------------
# I1: add


def test_add_refuses_an_unregistered_an_untracked_and_an_aborting_repo(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", *A)
    assert rc == 2 and "not registered" in err
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", *A)
    assert rc == 2 and "alpha is untracked" in err
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    m = migration_of(store, "alpha")
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", *A)
    assert rc == 2 and f"--key {m}:<k>" in err
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", "--key", "other:a", "--payload-hash", HASH_A, *A)
    assert rc == 2
    assert add_item(capsys, "alpha", m, "a") == "alpha#1"
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", "--key", f"{m}:b", "--payload-hash", HASH_A, *A)
    assert rc == 2 and "alpha is aborting" in err


def test_add_to_a_tracked_repo_needs_no_key_and_amend_and_decide_are_never_refused(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    assert ok(capsys, "add", "--repo", "alpha", "--title", "later", "--origin", "d", *A) == "alpha#2"
    ok(capsys, "decide", "--id", "alpha#1", "--decision", "parked", "--words", "w", *A)
    ok(capsys, "amend", "--id", "alpha#2", "--why", "w", "--title", "t2", *A)
    ok(capsys, "decide", "--id", "alpha#2", "--decision", "parked", "--words", "w", *A)
    ok(capsys, "untrack", "alpha", "--words", "bye", *A)
    ok(capsys, "amend", "--id", "alpha#1", "--why", "history", "--title", "still amendable", *A)       # an untracked repo's old item
    ok(capsys, "decide", "--id", "alpha#1", "--decision", "cut", "--words", "w", *A)
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "new", "--origin", "d", *A)
    assert rc == 2 and "alpha is untracked" in err


def test_an_unreadable_registry_refuses_add_and_a_store_with_no_items_reads_empty(store, tmp_path, mkrepo, capsys):
    (store / "items.jsonl").write_text("")
    assert not (store / "repos.jsonl").exists()
    assert br.read_registry(bs.resolve_store(None)).fold.repos == {}
    (store / "items.jsonl").write_text(json.dumps({"v": 1, "op": "add", "id": "x#1"}) + "\n")
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", *A)
    assert rc == 2 and "registry cannot be read" in err and "missing from a store that holds items" in err
    rc, _, err = run(capsys, "repos")
    assert rc == 2 and "unreadable" in err


# --------------------------------------------------------------------------------------------------------------------
# begin-track, track


def test_begin_track_records_the_manifest_owner_and_charter_before(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    land(a, "CHARTER.md", "# old\n")
    blob = git(a, "rev-parse", "origin/main:CHARTER.md")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    mf = manifest(tmp_path, retire=[{"repo": "alpha", "path": "next.md"}], record_dir="/rec")
    out = ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "his yes", "--owner", "desk 7", *A)
    r = reg_lines(store)[-1]
    assert out == f"tracking alpha (migration {r['migration']})" and r["op"] == "begin-track"
    assert r["charter_before"] == blob and r["owner"] == "desk 7" and r["words"] == "his yes"
    assert r["manifest"] == json.loads(mf.read_text()) and r["manifest_sha256"] == hashlib.sha256(mf.read_bytes()).hexdigest()


def test_charter_before_is_none_when_origin_main_has_no_charter(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    assert reg_lines(store)[-1]["charter_before"] == "none"


def test_a_repeated_begin_track_with_the_same_words_is_a_no_op_t2a(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    mf = manifest(tmp_path)
    first = ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", "--owner", "d", *A)
    again = ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", "--owner", "d", *A)
    assert again == "already " + first and len(reg_lines(store)) == 2
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "different", "--owner", "d", *A)
    assert rc == 2 and "already tracking" in err
    m = migration_of(store, "alpha")
    assert ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", "--owner", "d", "--migration", m, *A).startswith("already")


def test_a_second_ceremony_gets_another_migration_id(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    mf = manifest(tmp_path)
    ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", *A)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    ok(capsys, "abandon-done", "alpha", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", *A)
    ms = [r["migration"] for r in reg_lines(store) if r["op"] == "begin-track"]
    assert len(ms) == 2 and ms[0] != ms[1]


def test_begin_track_refuses_an_unregistered_a_tracking_a_tracked_repo_and_a_bad_manifest(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    assert rc == 2 and "not registered" in err
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    bad = tmp_path / "bad.json"
    for text, why in (("nope", "cannot be read as JSON"), ('{"plan_items": "a", "contract_items": []}', "plan_items"),
                      ('{"plan_items": ["a", "a"], "contract_items": []}', "distinct"),
                      ('{"plan_items": [], "contract_items": ["BAD"]}', "contract_items")):
        bad.write_text(text)
        rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(bad), "--words", "y", *A)
        assert rc == 2 and why in err, text
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(tmp_path / "missing.json"), "--words", "y", *A)
    assert rc == 2
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", "--migration", "x", *A)
    assert rc == 2 and "migration id" in err
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path, plan_items=["z"])), "--words", "y", *A)
    assert rc == 2 and "already tracking" in err


def test_begin_track_refuses_a_path_that_is_not_the_registered_repository(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    shutil.rmtree(a)
    subprocess.run(["git", "init", "-q", "-b", "main", str(a)], check=True)           # a different repository at the same path
    (a / "other").write_text("x")
    git(a, "add", ".")
    git(a, "commit", "-qm", "another root")
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    assert rc == 2 and "is not the registered repository" in err


def test_track_refuses_without_a_begun_ceremony_a_charter_or_a_valid_one(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and "untracked, not tracking" in err
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    m = migration_of(store, "alpha")
    iid = add_item(capsys, "alpha", m, "a")
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and "no readable CHARTER.md on origin/main (absent)" in err
    land(a, "CHARTER.md", "# nonsense\n")
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and "is not valid" in err and "levain charter check --repo" in err
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {iid}"))
    ok(capsys, "track", "alpha", "--words", "go", *A)
    assert [r["op"] for r in reg_lines(store)] == ["register", "begin-track", "track"]


def test_track_checks_the_plan_against_the_manifest_both_ways(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path, plan_items=["a", "b"])), "--words", "y", *A)
    m = migration_of(store, "alpha")
    one, two = add_item(capsys, "alpha", m, "a"), add_item(capsys, "alpha", m, "b")
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {one}"))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and f"the manifest's items not in the charter's Plan: {two}" in err
    ok(capsys, "add", "--repo", "alpha", "--title", "extra", "--origin", "d", "--key", f"{m}:c", "--payload-hash", HASH_A, *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {one}\n- {two}\n- alpha#3"))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and "the charter's Plan ids not in the manifest: alpha#3" in err
    ok(capsys, "decide", "--id", two, "--decision", "cut", "--words", "dropped", *A)       # cut during the ceremony: out of the comparison
    ok(capsys, "decide", "--id", "alpha#3", "--decision", "cut", "--words", "dropped", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {one}"))
    ok(capsys, "track", "alpha", "--words", "go", *A)


def test_track_refuses_a_manifest_item_that_was_never_added(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path, plan_items=["a", "b"])), "--words", "y", *A)
    m = migration_of(store, "alpha")
    one = add_item(capsys, "alpha", m, "a")
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {one}"))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and "never added" in err and "b" in err


def test_track_writes_the_blob_that_passed_and_the_retire_entries_and_does_not_wait_for_stubs(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    retire = [{"repo": "alpha", "path": "next.md"}, {"repo": "flow", "path": "projects/alpha/next.md"}]
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha", retire=retire)
    (a / "next.md").write_text("pre-track content\n")                    # not stubbed yet: track does not wait
    out = ok(capsys, "track", "alpha", "--words", "go", *A)
    r = reg_lines(store)[-1]
    assert out == f"tracked alpha (migration {m})" and r["op"] == "track" and r["migration"] == m
    assert r["charter_blob"] == git(a, "rev-parse", "origin/main:CHARTER.md") and r["retire"] == retire


def test_a_repeated_track_is_a_no_op_t2a_but_other_words_refuse(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    track_ready(capsys, tmp_path, store, a, "alpha")
    first = ok(capsys, "track", "alpha", "--words", "go", *A)
    assert ok(capsys, "track", "alpha", "--words", "go", *A) == "already " + first
    assert len(reg_lines(store)) == 3
    rc, _, err = run(capsys, "track", "alpha", "--words", "other", *A)
    assert rc == 2 and "tracked, not tracking" in err


# --------------------------------------------------------------------------------------------------------------------
# abandon-track, abandon-done


def abandon_ready(capsys, tmp_path, store, a):
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path, plan_items=["a", "b", "c"])), "--words", "y", *A)
    m = migration_of(store, "alpha")
    return m, [add_item(capsys, "alpha", m, k) for k in "abc"]


def test_abandon_track_cuts_every_item_with_the_abandons_words_and_cause(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, ids = abandon_ready(capsys, tmp_path, store, a)
    out = ok(capsys, "abandon-track", "alpha", "--words", "changed his mind", *A)
    assert out == f"aborting alpha; cut 3 items: {', '.join(ids)}"
    lines = (store / "repos.jsonl").read_text().splitlines()
    cause = hashlib.sha256(lines[-1].encode()).hexdigest()
    cuts = [r for r in item_lines(store) if r["op"] == "decide"]
    assert [c["id"] for c in cuts] == ids and all(c["decision"] == "cut" and c["words"] == "changed his mind" and c["cause"] == cause for c in cuts)
    f = item_fold((store / "items.jsonl").read_text().splitlines())
    assert not f.torn and not f.problems and all(i.decision.decision == "cut" for i in f.items.values())
    assert br.read_registry(bs.resolve_store(None)).fold.repos["alpha"].mode == "aborting"


def test_abandon_track_rerun_after_a_crash_writes_only_the_missing_cuts_t2d(store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    m, ids = abandon_ready(capsys, tmp_path, store, a)
    real = bs.decide
    calls = []

    def crash_after_one(*args, **kw):
        if calls:
            raise bs.BoardError("simulated crash")
        calls.append(args[1])
        return real(*args, **kw)

    monkeypatch.setattr(bs, "decide", crash_after_one)
    rc, _, err = run(capsys, "abandon-track", "alpha", "--words", "no", *A)
    assert rc == 2 and "simulated crash" in err
    assert [r["op"] for r in reg_lines(store)][-1] == "abandon-track"
    assert len([r for r in item_lines(store) if r["op"] == "decide"]) == 1
    monkeypatch.setattr(bs, "decide", real)
    before = len(reg_lines(store))
    out = ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    assert out == f"aborting alpha; cut 2 items: {ids[1]}, {ids[2]}" and len(reg_lines(store)) == before        # no second registry record
    cuts = [r for r in item_lines(store) if r["op"] == "decide"]
    assert len(cuts) == 3 and len({c["cause"] for c in cuts}) == 1
    assert ok(capsys, "abandon-track", "alpha", "--words", "no", *A) == "aborting alpha; cut 0 items"


def test_abandon_track_refuses_other_modes_and_keeps_the_repo_locked(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "abandon-track", "alpha", "--words", "no", *A)
    assert rc == 2 and "needs a tracking repo" in err
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y2", *A)
    assert rc == 2 and "aborting" in err


def test_abandon_done_needs_every_item_cut_and_the_charter_compensation(store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    m, ids = abandon_ready(capsys, tmp_path, store, a)
    rc, _, err = run(capsys, "abandon-done", "alpha", *A)
    assert rc == 2 and "needs an aborting repo" in err
    real = bs.decide
    monkeypatch.setattr(bs, "decide", lambda *x, **k: (_ for _ in ()).throw(bs.BoardError("down")))
    run(capsys, "abandon-track", "alpha", "--words", "no", *A)
    monkeypatch.setattr(bs, "decide", real)
    rc, _, err = run(capsys, "abandon-done", "alpha", *A)
    assert rc == 2 and f"items not cut: {', '.join(ids)}" in err
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {ids[0]}"))
    rc, _, err = run(capsys, "abandon-done", "alpha", *A)
    assert rc == 2 and "charter compensation missing" in err and "had none before" in err
    git(a, "rm", "-q", "CHARTER.md")
    git(a, "commit", "-qm", "revert charter")
    git(a, "push", "-q", "origin", "main")
    git(a, "fetch", "-q")
    assert ok(capsys, "abandon-done", "alpha", *A) == "untracked alpha"
    assert ok(capsys, "abandon-done", "alpha", *A) == "already untracked alpha"
    assert br.read_registry(bs.resolve_store(None)).fold.repos["alpha"].mode == "untracked"


def test_abandon_done_checks_nothing_when_the_repo_had_a_charter_before(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    land(a, "CHARTER.md", "# kept\n")
    m, ids = abandon_ready(capsys, tmp_path, store, a)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan="- alpha#1"))         # his edit stays
    assert ok(capsys, "abandon-done", "alpha", *A) == "untracked alpha"


# --------------------------------------------------------------------------------------------------------------------
# untrack


def test_untrack_refuses_while_an_item_is_not_done_cut_or_parked(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and f"items not done, cut or parked: {iid}" in err
    ok(capsys, "decide", "--id", iid, "--decision", "out-of-1.0", "--words", "later", *A)
    assert ok(capsys, "untrack", "alpha", "--words", "bye", *A) == "untracked alpha"
    assert ok(capsys, "untrack", "alpha", "--words", "bye", *A) == "already untracked alpha"


def test_untrack_refuses_a_repo_that_is_not_tracked(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and "tracking" in err


# --------------------------------------------------------------------------------------------------------------------
# repos: the read


def test_repos_prints_entries_next_step_and_the_72_hour_audit_t2b(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", "--owner", "desk 9", *A)
    rc, out, _ = run(capsys, "repos")
    m = migration_of(store, "alpha")
    assert rc == 0 and f"alpha: tracking ({m}), owner desk 9, since " in out and "next: add the plan items a" in out
    rc, out, _ = run(capsys, "repos", "--now", "2030-01-01T00:00:00Z")
    assert rc == 3 and "audit: registry: alpha has been tracking" in out and "over 72 hours" in out
    rc, out, _ = run(capsys, "repos", "--json", "--now", "2030-01-01T00:00:00Z")
    doc = json.loads(out)
    assert doc["repos"][0]["mode"] == "tracking" and len(doc["audit"]) == 1 and doc["store"] == (store / "store.id").read_text().strip()
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    rc, out, _ = run(capsys, "repos", "--now", "2030-01-01T00:00:00Z")
    assert rc == 3 and "alpha has been aborting" in out


def test_the_audit_is_quiet_inside_72_hours_and_for_settled_repos(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert run(capsys, "repos", "--now", "2030-01-01T00:00:00Z")[0] == 0
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    assert run(capsys, "repos")[0] == 0
    fold = br.read_registry(bs.resolve_store(None)).fold
    since = datetime.strptime(fold.repos["alpha"].since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert br.stale_audit(fold, since + timedelta(hours=72)) == []
    assert len(br.stale_audit(fold, since + timedelta(hours=72, seconds=1))) == 1


def test_board_state_audit_carries_the_stale_tracking_entry_t2b(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    rc, out, _ = run(capsys, "state", "--json", "--repo", str(a), "--repo-name", "alpha", "--now", "2030-01-01T00:00:00Z")
    doc = json.loads(out)
    assert any(e["why"].startswith("registry: alpha has been tracking") for e in doc["audit"]) and rc == 3
    assert br.audit_for_state(bs.resolve_store(None), datetime(2026, 11, 17, tzinfo=timezone.utc)) == []


def test_repos_reports_a_stale_stub_and_a_path_that_is_another_repository_i3(store, tmp_path, mkrepo, capsys):
    a, f = mkrepo("alpha"), mkrepo("flow")
    ok(capsys, "register", "flow", "--path", str(f), "--words", "w", *A)
    retire = [{"repo": "alpha", "path": "next.md"}, {"repo": "flow", "path": "projects/alpha/next.md"}, {"repo": "alpha", "path": "gone.md"}]
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha", retire=retire)
    ok(capsys, "track", "alpha", "--words", "go", *A)
    (a / "next.md").write_text("old\n")
    (f / "projects" / "alpha").mkdir(parents=True)
    (f / "projects" / "alpha" / "next.md").write_text("<!-- levain: tracked stub -->\n# alpha\n")
    rc, out, _ = run(capsys, "repos")
    assert rc == 3 and f"stale: {os.path.realpath(a)}/next.md still holds pre-track content" in out
    assert "projects/alpha" not in out and "gone.md" not in out
    (a / "next.md").write_text("<!-- levain: tracked stub -->\n# alpha\n")
    assert run(capsys, "repos")[0] == 0
    # I3 re-bases with a move
    shutil.copytree(a, tmp_path / "alpha2")
    ok(capsys, "move", "alpha", "--path", str(tmp_path / "alpha2"), "--words", "w", *A)
    (tmp_path / "alpha2" / "next.md").write_text("old again\n")
    rc, out, _ = run(capsys, "repos")
    assert rc == 3 and f"{os.path.realpath(tmp_path / 'alpha2')}/next.md" in out
    # a different repository at a registered path
    shutil.rmtree(f)
    f.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(f)], check=True)
    (f / "x").write_text("x")
    git(f, "add", ".")
    git(f, "commit", "-qm", "other")
    rc, out, _ = run(capsys, "repos")
    assert f"flow: {os.path.realpath(f)} is not the registered repository" in out


def test_repos_warns_about_a_tracked_repos_broken_charter_i2(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    assert "warning" not in run(capsys, "repos")[1]
    land(a, "CHARTER.md", "# broken\n")
    rc, out, _ = run(capsys, "repos")
    assert "warning: charter: alpha's CHARTER.md on origin/main is not valid" in out and rc == 0         # warns, wedges nothing
    assert ok(capsys, "add", "--repo", "alpha", "--title", "still works", "--origin", "d", *A) == "alpha#2"


# --------------------------------------------------------------------------------------------------------------------
# torn and ack-torn


def test_a_torn_registry_refuses_every_reader_and_writer_until_acked(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    with open(store / "repos.jsonl", "a") as fh:
        fh.write("garbage\n")
    for argv in (("repos",), ("pause", "alpha", "--words", "w", *A), ("add", "--repo", "alpha", "--title", "t", "--origin", "d", *A)):
        rc, _, err = run(capsys, *argv)
        assert rc == 2 and "registry" in err and "ack-torn --file repos.jsonl --line 2" in err, argv
    rc, _, err = run(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "1", "--why", "w", *A)
    assert rc == 2 and "is not torn" in err
    rc, _, err = run(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "9", "--why", "w", *A)
    assert rc == 2 and "there is no line 9" in err
    assert ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "2", "--why", "test", *A) == "acknowledged repos.jsonl line 2"
    r = reg_lines_tolerant(store)[-1]
    assert r["op"] == "ack-torn" and r["line"] == 2 and r["sha256"] == hashlib.sha256(b"garbage").hexdigest() and r["why"] == "test"
    assert run(capsys, "repos")[0] == 0
    assert ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "2", "--why", "again", *A).endswith("already acknowledged")
    assert ok(capsys, "pause", "alpha", "--words", "w", *A) == "paused alpha"


def reg_lines_tolerant(store):
    out = []
    for ln in (store / "repos.jsonl").read_text("utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append({"op": "?"})
    return out


def test_a_torn_tail_with_no_newline_is_unreadable_and_ack_terminates_it_first(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    with open(store / "repos.jsonl", "ab") as fh:
        fh.write(b'{"v":1,"op":"paus')
    rc, _, err = run(capsys, "repos")
    assert rc == 2 and "no newline" in err
    ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "2", "--why", "crash", *A)
    raw = (store / "repos.jsonl").read_bytes()
    assert raw.endswith(b"\n") and b'{"v":1,"op":"paus\n{' in raw
    assert run(capsys, "repos")[0] == 0


def test_ack_torn_works_on_the_items_file_too(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    with open(store / "items.jsonl", "a") as fh:
        fh.write("garbage\n")
    rc, _, err = run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "1", "--why", "w", *A)
    assert rc == 2 and "clean record" in err
    assert ok(capsys, "ack-torn", "--file", "items.jsonl", "--line", "2", "--why", "test", *A) == "acknowledged items.jsonl line 2"
    last = json.loads((store / "items.jsonl").read_text().splitlines()[-1])
    assert last["op"] == "ack-torn" and last["line"] == 2 and last["sha256"] == hashlib.sha256(b"garbage").hexdigest()
    assert ok(capsys, "ack-torn", "--file", "items.jsonl", "--line", "2", "--why", "test", *A).endswith("already acknowledged")


# --------------------------------------------------------------------------------------------------------------------
# use --import-focus


def focus_file(tmp_path, entries):
    p = tmp_path / "flow" / "state" / "focus.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({k: {"state": s, "words": w, "by": "d", "date": "x"} for k, (s, w) in entries.items()}))
    return p


def test_import_focus_registers_pauses_tombstones_then_writes_the_pointer_last(home, tmp_path, mkrepo, capsys, monkeypatch):
    for n in ("alpha", "beta"):
        mkrepo(n)
    focus = focus_file(tmp_path, {"alpha": ("active", "wa"), "beta": ("paused", "wb")})
    seen = {}
    real_use = bs.use

    def spy(directory, environ=None):
        seen["pointer_before"] = (home / "board" / "STORE").exists()
        seen["tomb"] = json.loads(focus.read_text())
        seen["records"] = len(reg_lines(Path(os.path.realpath(directory))))
        return real_use(directory, environ)

    monkeypatch.setattr(bs, "use", spy)
    store = tmp_path / "store"
    out = ok(capsys, "use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "desk 1")
    assert "imported 2 repos" in out
    assert seen == {"pointer_before": False, "tomb": {"moved_to": f"{os.path.realpath(store)}/repos.jsonl"}, "records": 3}
    assert (home / "board" / "STORE").read_text().strip() == os.path.realpath(store)
    recs = reg_lines(Path(os.path.realpath(store)))
    assert [(r["op"], r["repo"], r["words"]) for r in recs] == [("register", "alpha", "wa"), ("register", "beta", "wb"), ("pause", "beta", "wb")]
    rc, out, _ = run(capsys, "repos")
    assert rc == 0 and "beta: untracked, paused" in out


def test_import_focus_rerun_skips_the_import_and_a_crash_before_the_pointer_is_repaired(home, tmp_path, mkrepo, capsys, monkeypatch):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    store = tmp_path / "store"
    argv = ("use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "desk 1")
    real_use = bs.use

    def crash(directory, environ=None):
        raise bs.BoardError("crash before the pointer")

    monkeypatch.setattr(bs, "use", crash)
    rc, _, err = run(capsys, *argv)
    assert rc == 2 and not (home / "board" / "STORE").exists()
    assert json.loads(focus.read_text()) == {"moved_to": f"{os.path.realpath(store)}/repos.jsonl"}        # the tombstone landed: loud, not silent
    rc, _, err = run(capsys, "register", "x", "--path", str(mkrepo("x")), "--words", "w", *A)
    assert rc == 2 and bs.NO_STORE in err                                                                  # every other writer refuses
    monkeypatch.setattr(bs, "use", real_use)
    out = ok(capsys, *argv)
    assert "already imported" in out and (home / "board" / "STORE").exists()
    assert len(reg_lines(Path(os.path.realpath(store)))) == 1
    assert ok(capsys, *argv).startswith("already using")


def test_import_focus_rerun_before_the_tombstone_is_a_no_op_per_entry(home, tmp_path, mkrepo, capsys, monkeypatch):
    mkrepo("alpha")
    mkrepo("beta")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa"), "beta": ("paused", "wb")})
    store = tmp_path / "store"
    argv = ("use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "desk 1")
    real_pause = br.pause
    monkeypatch.setattr(br, "pause", lambda *a, **k: (_ for _ in ()).throw(bs.BoardError("crash mid-import")))
    rc, _, err = run(capsys, *argv)
    assert rc == 2 and "crash mid-import" in err and "moved_to" not in focus.read_text() and not (home / "board" / "STORE").exists()
    monkeypatch.setattr(br, "pause", real_pause)
    ok(capsys, *argv)
    ops = [(r["op"], r["repo"]) for r in reg_lines(Path(os.path.realpath(store)))]
    assert ops == [("register", "alpha"), ("register", "beta"), ("pause", "beta")]


def test_import_focus_writes_nothing_when_an_entry_is_not_a_repository(home, tmp_path, mkrepo, capsys):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa"), "ghost": ("active", "wg")})
    before = focus.read_text()
    rc, _, err = run(capsys, "use", str(tmp_path / "store"), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    assert rc == 2 and "nothing written" in err and "ghost" in err
    assert focus.read_text() == before and not (home / "board" / "STORE").exists() and not (tmp_path / "store").exists()


def test_import_focus_refuses_a_focus_file_that_changed_and_a_tombstone_for_another_store(home, tmp_path, mkrepo, capsys, monkeypatch):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    original = focus.read_text()
    real_register = br.register

    def register_then_edit(*a, **k):
        out = real_register(*a, **k)
        focus.write_text(original.replace("wa", "edited"))
        return out

    monkeypatch.setattr(br, "register", register_then_edit)
    rc, _, err = run(capsys, "use", str(tmp_path / "store"), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    assert rc == 2 and "changed while it was imported" in err and "moved_to" not in focus.read_text()
    assert not (home / "board" / "STORE").exists()
    focus.write_text(json.dumps({"moved_to": "/elsewhere/repos.jsonl"}))
    rc, _, err = run(capsys, "use", str(tmp_path / "store2"), "--import-focus", str(focus), "--by", "d")
    assert rc == 2 and "tombstone for /elsewhere/repos.jsonl" in err


# --------------------------------------------------------------------------------------------------------------------
# I5: the launch


def test_the_launch_hook_writes_nothing_while_the_registry_is_inactive(home, mkrepo):
    a = mkrepo("alpha")
    assert br.register_for_launch(a, {"LEVAIN_HOME": str(home)}) is False
    assert not home.exists()


def test_the_launch_hook_registers_an_unknown_repo_once_and_a_worktree_is_its_repo(store, home, tmp_path, mkrepo):
    a = mkrepo("alpha")
    env = {"LEVAIN_HOME": str(home)}
    assert br.register_for_launch(a, env) is True
    (r,) = reg_lines(store)
    assert r["op"] == "register" and r["repo"] == "alpha" and r["words"] == "first seat launched here"
    assert r["author"] == {"kind": "agent", "name": "levain launch"} and r["path"] == os.path.realpath(a)
    assert br.register_for_launch(a, env) is False
    sub = a / "sub"
    sub.mkdir()
    assert br.register_for_launch(sub, env) is False                      # a directory inside it
    git(a, "worktree", "add", "-q", str(tmp_path / "alpha-wt"), "-b", "side")
    assert br.register_for_launch(tmp_path / "alpha-wt", env) is False    # a worktree of a known repo is that repo
    assert br.register_for_launch(tmp_path, env) is False                 # not a repository: nothing to register
    assert len(reg_lines(store)) == 1


def test_a_failed_register_refuses_the_launch_and_writes_no_launch(store, home, tmp_path, mkrepo):
    a = mkrepo("alpha")
    cs = CoordStore(tmp_path / "board")
    token = cs.coord_take()
    ln = cs.launch_request(token, harness="claude", role=ROLE_DESK, cwd=str(a))
    assert ln.launch_id and [r["repo"] for r in reg_lines(store)] == ["alpha"]          # registered BEFORE the launch, then launched
    b = mkrepo("beta")
    with open(store / "repos.jsonl", "a") as fh:
        fh.write("garbage\n")                                                            # a torn registry: the register fails
    n_before = len(cs.launches()) if hasattr(cs, "launches") else None
    with pytest.raises(CoordError) as exc:
        cs.launch_request(token, harness="claude", role=ROLE_DESK, cwd=str(b))
    assert "launch refused" in str(exc.value) and "registry" in str(exc.value)
    if n_before is not None:
        assert len(cs.launches()) == n_before


def test_a_launch_with_no_registry_is_exactly_as_before(home, tmp_path, mkrepo):
    a = mkrepo("alpha")
    cs = CoordStore(tmp_path / "board")
    token = cs.coord_take()
    assert cs.launch_request(token, harness="claude", role=ROLE_DESK, cwd=str(a)).launch_id
    assert not home.exists()


# --------------------------------------------------------------------------------------------------------------------
# T2-i and the item record's cause


def test_a_stubs_item_of_another_ceremony_is_not_flagged_as_in_no_plan_group_t2i():
    text = CHARTER.format(name="y", plan="- y#1")
    items = {"y#1": ("y", None, "m1:a"), "y#2": ("y", None, "m9:stubs:y"), "y#3": ("y", None, "m9:other"), "y#4": ("y", None, None),
             "x#1": ("x", None, "m9:stubs:x")}
    problems, warnings = charter.check_text(text, repo_name="y", items=items)
    assert not problems
    assert warnings == ["y#3 is in the store and in no plan group", "y#4 is in the store and in no plan group"]
    two = {k: v[:2] for k, v in items.items()}                              # the old two-element form still works
    assert len(charter.check_text(text, repo_name="y", items=two)[1]) == 3


def test_charter_check_cli_reads_the_stubs_key_off_the_store(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    ok(capsys, "add", "--repo", "alpha", "--title", "stubs", "--origin", "d", "--key", "m77:stubs:alpha", "--payload-hash", HASH_A, *A)
    rc = main(["charter", "check", "--repo", str(a), "--items", str(store), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["valid"] and doc["warnings"] == []


def test_decide_takes_an_optional_cause_that_must_be_a_sha(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    target = bs.resolve_store(None)
    with pytest.raises(bs.BoardError):
        bs.decide(target, iid, "cut", "w", "d", author={"kind": "operator", "name": "d"}, cause="nothex")
    bs.decide(target, iid, "parked", "w", "d", author={"kind": "operator", "name": "d"})
    assert "cause" not in item_lines(store)[-1]
