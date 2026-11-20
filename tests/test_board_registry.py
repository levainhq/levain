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
def decision_seal(tmp_path, monkeypatch):
    """For the tests that REQUEST it (not autouse: L2 L4, levain 26), whose subject is the registry's rules, not grades.
    begin-track, track, abandon-track and untrack are decision-class (P2): each is called with a seal over a THROWAWAY ssh
    key (the seam test_board_sign.py uses; a real Touch ID ceremony cannot run in a test) whose authority log enrols it, so
    the writer earns ``signed-terminal`` for real. It also seals a CLI call made without --sign, so a test using it can
    assert NO unsigned-write refusal: the policy itself, unsigned and signed, is tested in test_board_registry_sign.py and
    by every test here that does not request this fixture."""
    seal_decisions(tmp_path, monkeypatch)


def seal_decisions(tmp_path, monkeypatch):
    """The body of ``decision_seal``, callable with any ``MonkeyPatch`` (a module-scoped store seed, tests/_board_v2_seed.py,
    has no function-scoped fixture to ask for)."""
    from tests.test_board_sign import Rig
    rig = Rig(tmp_path)

    def wrap(name):
        orig = getattr(br, name)

        def sealed(*a, seal=None, **kw):
            return orig(*a, seal=seal if seal is not None else rig.seal(), **kw)
        monkeypatch.setattr(br, name, sealed)
    for name in ("begin_track", "track", "abandon_track", "untrack"):
        wrap(name)
    real_decide = bs.decide

    def decide(*a, **kw):                       # an item decide (not an abandon's cut, which carries a cause) is decision-class too
        if kw.get("seal") is None and kw.get("cause") is None:
            kw["seal"] = rig.seal()
        return real_decide(*a, **kw)
    monkeypatch.setattr(bs, "decide", decide)


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
def bare_store(home, tmp_path, capsys):
    """A pointered store with NO repos.jsonl: the registry is inactive (W6)."""
    d = tmp_path / "store"
    ok(capsys, "use", str(d))
    return Path(os.path.realpath(d))


@pytest.fixture
def store(bare_store):
    """A pointered store whose registry is ACTIVE (an empty repos.jsonl exists)."""
    (bare_store / "repos.jsonl").touch()
    return bare_store


def reg_lines(store):
    return [json.loads(ln) for ln in (store / "repos.jsonl").read_text("utf-8").splitlines()]


def append_legacy_decide(store, iid):
    """Write by hand (the writer refuses it now) a chained ``decide`` that carries the legacy word ``out-of-1.0``."""
    path = store / "items.jsonl"
    rec = {"v": 1, "op": "decide", "id": iid, "at": "2026-11-12T10:00:00Z", "prev": bs.chain_prev(path.read_bytes()), "by": "desk 1",
           "decision": "out-of-1.0", "words": "stored before the horizon word", "date": "2026-11-12"}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


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
            if isinstance(node, ast.ImportFrom) and node.module == "levain.desk" and [a.name for a in node.names] == ["board_chain"]:
                continue                    # the one sibling: the stdlib-only chain checker both folds share (P1b)
            assert not any(n == "levain" or n.startswith("levain.") for n in names), names


def test_a_whole_ceremony_folds_through_every_mode():
    lines = [rec("register", n=1), rec("begin-track", n=2, owner="desk 3"), rec("track", n=3), rec("pause", n=4)]
    f = rf.fold_repos(lines, store_id=SID)
    r = f.repos["x"]
    assert (r.mode, r.attention, r.migration, r.owner, r.charter_blob, r.charter_before) == ("tracked", "paused", "m1", "desk 3", "b1", "none")
    assert r.retire == ({"repo": "x", "path": "next.md"},) and r.since == "2026-11-17T10:00:03Z" and not f.torn and not f.problems
    f = rf.fold_repos([rec("register", n=1), rec("begin-track", n=2), rec("abandon-track", n=3), rec("abandon-done", n=4, migration="m1")], store_id=SID)
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
    f = rf.fold_repos([rec("register", prev=hashlib.sha256(b"").hexdigest(), author={"kind": "agent", "name": "a"}, grade="asserted", future="x")], store_id=SID)
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


def test_add_refuses_an_unregistered_an_untracked_and_an_aborting_repo(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_add_to_a_tracked_repo_needs_no_key_and_amend_and_decide_are_never_refused(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_a_store_with_no_repos_file_has_an_inactive_registry_w6(bare_store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    (bare_store / "items.jsonl").write_text(json.dumps({"v": 1, "op": "add", "id": "x#1", "at": "2026-11-17T00:00:00Z", "by": "b", "repo": "x", "title": "t", "origin": "o"}) + "\n")      # a store that holds items
    assert not (bare_store / "repos.jsonl").exists()
    assert br.read_registry(bs.resolve_store(None), allow_inactive=True).active is False
    assert ok(capsys, "repos") == "no registry yet (levain board use --import-focus, or register a repo)"
    assert run(capsys, "repos", "--json")[0] == 0
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", *A)      # I1 is off
    assert rc == 0, err
    assert br.register_for_launch(a) is False and not (bare_store / "repos.jsonl").exists()      # the launch hook does nothing
    rc, _, err = run(capsys, "pause", "alpha", "--words", "w", *A)                               # no verb but register starts the file
    assert rc == 2 and "no registry yet" in err and not (bare_store / "repos.jsonl").exists()
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)                        # the file's existence activates it
    assert (bare_store / "repos.jsonl").exists()
    rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "t2", "--origin", "d", *A)
    assert rc == 2 and "alpha is untracked" in err


def test_the_flow_live_store_copy_takes_an_add_and_a_launch_hook_call_w6(home, tmp_path, mkrepo, capsys):
    live = Path("/Users/phillipclapham/Briefcase/flow/state/board/items.jsonl")
    if not live.exists():
        pytest.skip("flow's live items.jsonl is not on this machine")
    copy = tmp_path / "flowcopy"
    copy.mkdir()
    shutil.copyfile(live, copy / "items.jsonl")                         # a scratch COPY; the source is only read
    assert (copy / "items.jsonl").stat().st_size > 0
    ok(capsys, "use", str(copy))
    a = mkrepo("alpha")
    rc, out, err = run(capsys, "add", "--repo", "alpha", "--title", "on a copy of flow's store", "--origin", "desk 1", *A)
    assert rc == 0, err
    assert br.register_for_launch(a) is False
    assert not (copy / "repos.jsonl").exists()
    assert run(capsys, "state")[0] in (0, 3)                            # the state read is not wedged either


# --------------------------------------------------------------------------------------------------------------------
# begin-track, track


def test_begin_track_records_the_manifest_owner_and_charter_before(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_charter_before_is_none_when_origin_main_has_no_charter(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    assert reg_lines(store)[-1]["charter_before"] == "none"


def test_a_repeated_begin_track_with_the_same_words_is_a_no_op_t2a(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_a_second_ceremony_gets_another_migration_id(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    mf = manifest(tmp_path)
    ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", *A)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    ok(capsys, "abandon-done", "alpha", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", *A)
    ms = [r["migration"] for r in reg_lines(store) if r["op"] == "begin-track"]
    assert len(ms) == 2 and ms[0] != ms[1]


def test_begin_track_refuses_an_unregistered_a_tracking_a_tracked_repo_and_a_bad_manifest(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_track_refuses_without_a_begun_ceremony_a_charter_or_a_valid_one(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_track_checks_the_plan_against_the_manifest_both_ways(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_track_refuses_a_manifest_item_that_was_never_added(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path, plan_items=["a", "b"])), "--words", "y", *A)
    m = migration_of(store, "alpha")
    one = add_item(capsys, "alpha", m, "a")
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan=f"- {one}"))
    rc, _, err = run(capsys, "track", "alpha", "--words", "go", *A)
    assert rc == 2 and "never added" in err and "b" in err


def test_track_writes_the_blob_that_passed_and_the_retire_entries_and_does_not_wait_for_stubs(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "flow", "--path", str(mkrepo("flow")), "--words", "w", *A)        # a retire entry's repo must be registered at begin (rev 8, rule 6)
    retire = [{"repo": "alpha", "path": "next.md"}, {"repo": "flow", "path": "projects/alpha/next.md"}]
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha", retire=retire)
    (a / "next.md").write_text("pre-track content\n")                    # not stubbed yet: track does not wait
    out = ok(capsys, "track", "alpha", "--words", "go", *A)
    r = reg_lines(store)[-1]
    assert out == f"tracked alpha (migration {m})" and r["op"] == "track" and r["migration"] == m
    assert r["charter_blob"] == git(a, "rev-parse", "origin/main:CHARTER.md") and r["retire"] == retire


def test_a_repeated_track_is_a_no_op_t2a_but_other_words_refuse(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_abandon_track_cuts_every_item_with_the_abandons_words_and_cause(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_abandon_track_rerun_after_a_crash_writes_only_the_missing_cuts_t2d(decision_seal, store, tmp_path, mkrepo, capsys, monkeypatch):
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
    assert out.splitlines()[0] == f"aborting alpha; cut 2 items: {ids[1]}, {ids[2]}" and len(reg_lines(store)) == before        # no second registry record
    assert out.splitlines()[1].endswith(") stands")                     # P3 fix 5: the re-run names the abandon that stands
    cuts = [r for r in item_lines(store) if r["op"] == "decide"]
    assert len(cuts) == 3 and len({c["cause"] for c in cuts}) == 1
    assert ok(capsys, "abandon-track", "alpha", "--words", "no", *A).splitlines()[0] == "aborting alpha; nothing to cut"


def test_abandon_track_refuses_other_modes_and_keeps_the_repo_locked(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    rc, _, err = run(capsys, "abandon-track", "alpha", "--words", "no", *A)
    assert rc == 2 and "needs a tracking repo" in err
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y2", *A)
    assert rc == 2 and "aborting" in err


def test_abandon_done_needs_every_item_cut_and_the_charter_compensation(decision_seal, store, tmp_path, mkrepo, capsys, monkeypatch):
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


# --------------------------------------------------------------------------------------------------------------------
# untrack


def test_untrack_refuses_while_an_item_is_not_done_cut_or_parked(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and f"items not done, cut, parked or out of horizon: {iid}" in err
    ok(capsys, "decide", "--id", iid, "--decision", "out-of-horizon", "--horizon", "1.0", "--words", "later", *A)
    assert ok(capsys, "untrack", "alpha", "--words", "bye", *A) == "untracked alpha"
    assert ok(capsys, "untrack", "alpha", "--words", "bye", *A) == "already untracked alpha"


def test_untrack_counts_an_item_stored_with_the_legacy_out_of_1_0_word_as_settled(decision_seal, store, tmp_path, mkrepo, capsys):
    """The writer refuses ``out-of-1.0`` now, so the raw line is written by hand: untrack must read it as out of horizon."""
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and iid in err
    append_legacy_decide(store, iid)
    assert ok(capsys, "untrack", "alpha", "--words", "bye", *A) == "untracked alpha"


def test_untrack_refuses_a_repo_that_is_not_tracked(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and "tracking" in err


# --------------------------------------------------------------------------------------------------------------------
# repos: the read


def test_repos_prints_entries_next_step_and_the_72_hour_audit_t2b(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_the_audit_is_quiet_inside_72_hours_and_for_settled_repos(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert run(capsys, "repos", "--now", "2030-01-01T00:00:00Z")[0] == 0
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    assert run(capsys, "repos")[0] == 0
    fold = br.read_registry(bs.resolve_store(None)).fold
    since = datetime.strptime(fold.repos["alpha"].since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert br.stale_audit(fold, since + timedelta(hours=72)) == []
    assert len(br.stale_audit(fold, since + timedelta(hours=72, seconds=1))) == 1


def test_board_state_audit_carries_the_stale_tracking_entry_t2b(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    rc, out, _ = run(capsys, "state", "--json", "--repo", str(a), "--repo-name", "alpha", "--now", "2030-01-01T00:00:00Z")
    doc = json.loads(out)
    assert any(e["why"].startswith("registry: alpha has been tracking") for e in doc["audit"]) and rc == 3
    assert br.audit_for_state(bs.resolve_store(None), datetime(2026, 11, 17, tzinfo=timezone.utc)) == []


def test_repos_reports_a_stale_stub_and_a_path_that_is_another_repository_i3(decision_seal, store, tmp_path, mkrepo, capsys):
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


def test_repos_warns_about_a_tracked_repos_broken_charter_i2(decision_seal, store, tmp_path, mkrepo, capsys):
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
    assert ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "2", "--why", "test", *A).startswith("acknowledged repos.jsonl line 2")
    r = reg_lines_tolerant(store)[-1]
    assert r["op"] == "ack-torn" and r["line"] == 2 and r["sha256"] == hashlib.sha256(b"garbage").hexdigest() and r["why"] == "test"
    assert run(capsys, "repos")[0] == 0
    assert ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "2", "--why", "again", *A).endswith("already acknowledged")
    assert ok(capsys, "pause", "alpha", "--words", "w", *A) == "paused alpha"


OLD_REG = b'{"v":1,"store":"%s","op":"pause","repo":"alpha","at":"2026-11-17T00:00:00Z","by":"old","words":"w"}'


def _registry_with_old_writer_line(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "pause", "alpha", "--words", "w", *A)
    line = OLD_REG % (store / "store.id").read_bytes().strip()
    with open(store / "repos.jsonl", "ab") as fh:
        fh.write(line + b"\n")                                      # an old writer appends a record with no prev
    return line


def test_p1b_an_unchained_registry_line_after_the_cutover_is_torn_and_every_reader_and_writer_refuses_naming_it(store, mkrepo, capsys):
    _registry_with_old_writer_line(store, mkrepo, capsys)
    fold = rf.fold_repos((store / "repos.jsonl").read_bytes().split(b"\n")[:-1], store_id=(store / "store.id").read_text().strip())
    assert fold.torn and fold.torn_why.startswith("line 3: unchained record after the cutover (an old writer?)")
    before = (store / "repos.jsonl").read_bytes()
    for argv in (("repos",), ("resume", "alpha", "--words", "w", *A)):
        rc, _, err = run(capsys, *argv)
        assert rc == 2 and "an old writer?" in err and "ack-torn --file repos.jsonl --line 3" in err, argv
    assert (store / "repos.jsonl").read_bytes() == before


def test_p1b_ack_torn_on_the_registry_re_anchors_and_a_second_break_after_it_refuses_again(store, mkrepo, capsys):
    line = _registry_with_old_writer_line(store, mkrepo, capsys)
    assert ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "3", "--why", "stale checkout", *A).startswith("acknowledged repos.jsonl line 3")
    ack = reg_lines(store)[-1]
    assert ack["op"] == "ack-torn" and ack["sha256"] == hashlib.sha256(line).hexdigest() and ack["prev"] == ack["sha256"]
    assert run(capsys, "repos")[0] == 0                            # derives again; the old writer's record is acknowledged and KEPT
    assert br.read_registry(bs.resolve_store(None)).fold.repos["alpha"].attention == "paused"
    assert ok(capsys, "resume", "alpha", "--words", "w", *A) == "resumed alpha"
    with open(store / "repos.jsonl", "ab") as fh:
        fh.write(line + b"\n")
    rc, _, err = run(capsys, "repos")
    assert rc == 2 and "line 6: unchained record after the cutover (an old writer?)" in err
    assert ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "6", "--why", "again", *A).startswith("acknowledged repos.jsonl line 6")
    assert run(capsys, "repos")[0] == 0


def test_p1b_a_registry_prev_that_does_not_match_is_named(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "pause", "alpha", "--words", "w", *A)
    ok(capsys, "resume", "alpha", "--words", "w", *A)
    lines = (store / "repos.jsonl").read_bytes().split(b"\n")[:-1]
    del lines[1]
    (store / "repos.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    rc, _, err = run(capsys, "repos")
    assert rc == 2 and "line 2: prev does not match line 1" in err


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


def test_charter_check_cli_reads_the_stubs_key_off_the_store(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    ok(capsys, "add", "--repo", "alpha", "--title", "stubs", "--origin", "d", "--key", "m77:stubs:alpha", "--payload-hash", HASH_A, *A)
    rc = main(["charter", "check", "--repo", str(a), "--items", str(store), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["valid"] and doc["warnings"] == []


def test_decide_takes_an_optional_cause_that_must_be_a_sha(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    target = bs.resolve_store(None)
    with pytest.raises(bs.BoardError):
        bs.decide(target, iid, "cut", "w", "d", author={"kind": "operator", "name": "d"}, cause="nothex")
    bs.decide(target, iid, "parked", "w", "d", author={"kind": "operator", "name": "d"})
    assert "cause" not in item_lines(store)[-1]


# --------------------------------------------------------------------------------------------------------------------
# round 1 fixes (levain#93): R0 one write path, W1..W9


def test_r0_there_is_one_write_path_and_every_registry_verb_goes_through_append(store, tmp_path, mkrepo, capsys, monkeypatch):
    assert "_reg_append" not in Path(br.__file__).read_text("utf-8")
    seen = []
    real = bs._append

    def spy(target, build, **kw):
        seen.append((kw.get("filename", "items.jsonl"), kw.get("parse") is not None))
        return real(target, build, **kw)

    monkeypatch.setattr(bs, "_append", spy)
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "pause", "alpha", "--words", "w", *A)
    assert seen == [("repos.jsonl", True), ("repos.jsonl", True)]


def test_r0_a_registry_write_replaced_after_the_write_refuses_and_appends_nothing_to_the_new_file(store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    reg = store / "repos.jsonl"
    old = reg.read_bytes()
    real, calls = bs._write_all, {"n": 0}

    def write_then_swap_back(fd, payload):
        calls["n"] += 1
        real(fd, payload)
        reg.with_name("repos.old").write_bytes(old)
        os.replace(reg.with_name("repos.old"), reg)          # a checkout restored the file without our line

    monkeypatch.setattr(bs, "_write_all", write_then_swap_back)
    rc, out, err = run(capsys, "pause", "alpha", "--words", "w", *A)
    assert rc == 2 and calls["n"] == 1                       # no retry: one write, then a refusal
    assert "was replaced while alpha was being written" in err and "may or may not be in the new file" in err
    assert reg.read_bytes() == old


def test_r0_an_ack_torn_after_an_unterminated_tail_goes_through_append_with_a_newline_first(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    with open(store / "repos.jsonl", "ab") as fh:
        fh.write(b'{"v":1,"op":"paus')
    ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "2", "--why", "crash", *A)
    lines = (store / "repos.jsonl").read_bytes().split(b"\n")
    assert json.loads(lines[2])["prev"] == hashlib.sha256(lines[1]).hexdigest()


def test_w1_add_refuses_a_stubs_key_while_the_repo_is_tracking_design_5e(decision_seal, store, tmp_path, mkrepo, capsys):
    """Design 5 (e): contract_items are added AFTER `track`, so "no stub item is ever on the board while X's old files are
    still its source of truth"."""
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    before = (store / "items.jsonl").read_bytes()
    for k in (f"{m}:stubs:alpha", f"{m}:stubs:flow"):
        rc, _, err = run(capsys, "add", "--repo", "alpha", "--title", "s", "--origin", "d", "--key", k, "--payload-hash", HASH_A, *A)
        assert rc == 2 and "no stub item is ever on the board" in err and "design 5 (e)" in err, k
    assert (store / "items.jsonl").read_bytes() == before
    ok(capsys, "track", "alpha", "--words", "go", *A)
    ok(capsys, "add", "--repo", "alpha", "--title", "s", "--origin", "d", "--key", f"{m}:stubs:alpha", "--payload-hash", HASH_A, *A)


def test_w2_track_records_the_blob_whose_text_passed_even_if_origin_main_moves_during_it(decision_seal, store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    passed = git(a, "rev-parse", "origin/main:CHARTER.md")
    real, moved = br._git, {"n": 0}

    def git_then_move(path, *args):
        out = real(path, *args)
        if args and args[0] == "rev-parse" and args[-1].endswith(":CHARTER.md") and not moved["n"]:
            moved["n"] += 1
            land(a, "CHARTER.md", "# something else entirely\n")        # a fetch moves origin/main right after the blob sha was read
        return out

    monkeypatch.setattr(br, "_git", git_then_move)
    ok(capsys, "track", "alpha", "--words", "go", *A)
    assert moved["n"] == 1
    assert reg_lines(store)[-1]["charter_blob"] == passed != git(a, "rev-parse", "origin/main:CHARTER.md")


def test_w3_the_tombstones_directory_is_fsynced_before_the_pointer_and_a_failure_is_a_warning(home, tmp_path, mkrepo, capsys, monkeypatch):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    order = []
    real_fsync_dir, real_use = bs._fsync_dir, bs.use

    def failing_dir_fsync(path):
        if Path(path) == focus.parent:
            order.append("dir-fsync")
            raise OSError("disk says no")
        return real_fsync_dir(path)

    def spy_use(directory, environ=None):
        order.append("pointer")
        return real_use(directory, environ)

    monkeypatch.setattr(bs, "_fsync_dir", failing_dir_fsync)
    monkeypatch.setattr(bs, "use", spy_use)
    rc, out, err = run(capsys, "use", str(tmp_path / "store"), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    assert rc == 0, err
    assert order == ["dir-fsync", "pointer"]
    assert "could not be fsynced" in err and (home / "board" / "STORE").exists()


def test_w4_a_tombstone_whose_store_is_gone_refuses_and_creates_nothing(home, tmp_path, mkrepo, capsys):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    argv = ("use", str(tmp_path / "store"), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    ok(capsys, *argv)
    shutil.rmtree(tmp_path / "store")
    (home / "board" / "STORE").unlink()
    rc, _, err = run(capsys, *argv)
    assert rc == 2 and "holds no store" in err and "first-time use" in err
    assert not (tmp_path / "store").exists() and not (home / "board" / "STORE").exists()


def test_w5_a_torn_line_that_is_not_utf8_can_be_acknowledged_by_its_raw_bytes(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    with open(store / "repos.jsonl", "ab") as fh:
        fh.write(b'\xff\xfe not utf-8\n')
    rc, _, err = run(capsys, "repos")
    assert rc == 2 and "not valid UTF-8" in err
    ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "2", "--why", "bad bytes", *A)
    ack = json.loads((store / "repos.jsonl").read_bytes().split(b"\n")[2])
    assert ack["sha256"] == hashlib.sha256(b'\xff\xfe not utf-8').hexdigest()
    assert run(capsys, "repos")[0] == 0
    assert ok(capsys, "pause", "alpha", "--words", "w", *A) == "paused alpha"


def test_w7_an_explicit_migration_that_an_earlier_ceremony_used_refuses(decision_seal, store, tmp_path, mkrepo, capsys):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "register", "beta", "--path", str(b), "--words", "w", *A)
    mf = str(manifest(tmp_path))
    ok(capsys, "begin-track", "alpha", "--manifest", mf, "--words", "y", "--migration", "mfirst1", *A)
    assert ok(capsys, "begin-track", "alpha", "--manifest", mf, "--words", "y", "--migration", "mfirst1", *A).startswith("already tracking")    # the retry
    rc, _, err = run(capsys, "begin-track", "beta", "--manifest", mf, "--words", "y", "--migration", "mfirst1", *A)
    assert rc == 2 and "already used" in err
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    ok(capsys, "abandon-done", "alpha", *A)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", mf, "--words", "again", "--migration", "mfirst1", *A)
    assert rc == 2 and "already used" in err and len([r for r in reg_lines(store) if r["op"] == "begin-track"]) == 1
    ok(capsys, "begin-track", "alpha", "--manifest", mf, "--words", "again", *A)                          # a derived id is new


def test_w8_audit_for_state_names_a_registry_that_cannot_be_read_instead_of_swallowing_it(store, mkrepo, capsys):
    target = bs.resolve_store(None)
    now = datetime.now(timezone.utc)
    assert br.audit_for_state(target, now) == []
    (store / "store.id").unlink()
    (entry,) = br.audit_for_state(target, now)
    assert entry.startswith("registry: unreadable: ") and "store.id" in entry


def test_w9_move_at_the_same_path_with_a_new_origin_records_it_and_only_an_identical_move_is_a_no_op(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert ok(capsys, "move", "alpha", "--path", str(a), "--words", "same", *A) == "already at that path alpha"
    git(a, "remote", "set-url", "origin", str(tmp_path / "bare" / "renamed.git"))
    rc, out, _ = run(capsys, "repos")
    assert rc == 3 and "is not the registered repository" in out
    assert ok(capsys, "move", "alpha", "--path", str(a), "--words", "remote renamed", *A) == "moved alpha"
    r = reg_lines(store)[-1]
    assert r["op"] == "move" and r["origin"].endswith("renamed.git") and r["path"] == os.path.realpath(a)
    assert run(capsys, "repos")[0] == 0
    assert ok(capsys, "move", "alpha", "--path", str(a), "--words", "again", *A) == "already at that path alpha"


def test_n2_a_second_bytes_check_that_refuses_leaves_no_tmp_file_behind(home, tmp_path, mkrepo, capsys, monkeypatch):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    original = focus.read_text()
    real = os.fsync

    def fsync_then_edit(fd):
        real(fd)
        if list(focus.parent.glob(".focus.json.*")):                # the tombstone's tmp file exists: edit the focus file now
            focus.write_text(original.replace("wa", "edited"))

    monkeypatch.setattr(br.os, "fsync", fsync_then_edit)
    rc, _, err = run(capsys, "use", str(tmp_path / "store"), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    monkeypatch.setattr(br.os, "fsync", real)
    assert rc == 2 and "changed while it was imported" in err
    assert list(focus.parent.glob(".focus.json.*")) == []


def test_n10_a_retire_path_must_be_relative_to_its_repo(store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    for bad in ("/etc/passwd", "../x/next.md", "a/../../b"):
        rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path, retire=[{"repo": "alpha", "path": bad}])),
                         "--words", "y", *A)
        assert rc == 2 and "relative to its repo's root" in err, bad


# --------------------------------------------------------------------------------------------------------------------
# round 2 fixes (levain#93): F1..F11


def tear_item_adds(store):
    """Make every add line of the items file unreadable (a torn line the item fold cannot trust)."""
    p = store / "items.jsonl"
    p.write_text("".join("#" + ln + "\n" if json.loads(ln)["op"] == "add" else ln + "\n" for ln in p.read_text().splitlines()))


def test_f1_ack_torn_on_the_items_file_is_taken_since_p1b_and_the_item_store_reads_again(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    track_ready(capsys, tmp_path, store, a, "alpha")
    with open(store / "items.jsonl", "a") as fh:
        fh.write("garbage\n")
    assert run(capsys, "ack-torn", "--file", "items.jsonl", "--line", "2", "--why", "w", *A)[1].startswith("acknowledged items.jsonl line 2")
    assert not item_fold((store / "items.jsonl").read_text("utf-8").split("\n")).torn


def test_f2_abandon_done_on_a_torn_item_store_refuses_instead_of_reading_no_items(decision_seal, store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    m, ids = abandon_ready(capsys, tmp_path, store, a)
    real = bs.decide
    monkeypatch.setattr(bs, "decide", lambda *x, **k: (_ for _ in ()).throw(bs.BoardError("down")))
    run(capsys, "abandon-track", "alpha", "--words", "no", *A)
    monkeypatch.setattr(bs, "decide", real)
    tear_item_adds(store)
    n = len(reg_lines(store))
    rc, _, err = run(capsys, "abandon-done", "alpha", *A)
    assert rc == 2 and "items" in err and "torn" in err
    assert len(reg_lines(store)) == n and br.read_registry(bs.resolve_store(None)).fold.repos["alpha"].mode == "aborting"


def test_f3_untrack_refuses_a_torn_item_store_even_when_no_item_reads_as_the_repos(decision_seal, store, tmp_path, mkrepo, capsys):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    tear_item_adds(store)
    n = len(reg_lines(store))
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and "torn" in err and len(reg_lines(store)) == n


def test_f4_untrack_rechecks_the_items_under_the_lock_a_concurrent_add_is_not_missed(decision_seal, store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    m, iid = track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    ok(capsys, "decide", "--id", iid, "--decision", "out-of-horizon", "--horizon", "1.0", "--words", "later", *A)
    real = br._unsettled_items

    def derive_then_a_concurrent_add(*args, **kw):
        out = real(*args, **kw)                                  # the check saw nothing open ...
        ok(capsys, "add", "--repo", "alpha", "--title", "late", "--origin", "desk 1", *A)   # ... then an add lands
        return out

    monkeypatch.setattr(br, "_unsettled_items", derive_then_a_concurrent_add)
    n = len(reg_lines(store))
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and "changed under the check" in err and len(reg_lines(store)) == n
    monkeypatch.setattr(br, "_unsettled_items", real)
    rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
    assert rc == 2 and "items not done, cut, parked or out of horizon" in err                  # run again: now it sees the add


def test_f4_abandon_done_rechecks_every_item_reads_cut_under_the_lock(decision_seal, store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    m, ids = abandon_ready(capsys, tmp_path, store, a)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    real = br._compensation_missing

    def check_then_a_reopen(*args, **kw):
        out = real(*args, **kw)
        ok(capsys, "decide", "--id", ids[0], "--decision", "reopen", "--words", "wait", *A)
        return out

    monkeypatch.setattr(br, "_compensation_missing", check_then_a_reopen)
    n = len(reg_lines(store))
    rc, _, err = run(capsys, "abandon-done", "alpha", *A)
    assert rc == 2 and "items not cut" in err and ids[0] in err and len(reg_lines(store)) == n


def test_f5_two_repos_with_the_same_directory_name_both_launch(store, home, tmp_path, mkrepo):
    a = mkrepo("alpha")
    other = tmp_path / "elsewhere" / "alpha"
    other.parent.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    (other / "f").write_text("x")
    git(other, "add", ".")
    git(other, "commit", "-qm", "other root")
    env = {"LEVAIN_HOME": str(home)}
    assert br.register_for_launch(a, env) is True
    assert br.register_for_launch(other, env) is True
    names = [r["repo"] for r in reg_lines(store)]
    assert names[0] == "alpha" and names[1].startswith("alpha-") and len(set(names)) == 2
    assert br.register_for_launch(other, env) is False                        # the second is known now


def test_f5_a_repo_with_no_commit_registers_nothing_and_the_launch_proceeds(store, home, tmp_path):
    d = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    assert br.register_for_launch(d, {"LEVAIN_HOME": str(home)}) is False
    assert (store / "repos.jsonl").read_bytes() == b""
    cs = CoordStore(tmp_path / "board")
    token = cs.coord_take()
    assert cs.launch_request(token, harness="claude", role=ROLE_DESK, cwd=str(d)).launch_id


def test_f5_a_concurrent_register_of_the_same_identity_is_a_no_op_not_a_refusal(store, home, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    real = br.read_registry

    def read_then_someone_registers(*args, **kw):
        view = real(*args, **kw)
        monkeypatch.setattr(br, "read_registry", real)
        ok(capsys, "register", "other-name", "--path", str(a), "--words", "w", *A)    # lands between the read and the write
        return view

    monkeypatch.setattr(br, "read_registry", read_then_someone_registers)
    assert br.register_for_launch(a, {"LEVAIN_HOME": str(home)}) is False
    assert [r["repo"] for r in reg_lines(store)] == ["other-name"]


def test_f5_a_worker_asking_for_several_threads_is_refused_before_anything_is_registered(store, home, tmp_path, mkrepo):
    a = mkrepo("alpha")
    cs = CoordStore(tmp_path / "board")
    token = cs.coord_take()
    with pytest.raises(CoordError):
        cs.launch_request(token, harness="claude", role="worker", threads=["t1", "t2"], cwd=str(a))
    assert (store / "repos.jsonl").read_bytes() == b""


def test_f6_an_abandon_done_without_a_migration_or_for_another_one_is_a_problem_and_changes_nothing():
    base = [rec("register", n=1), rec("begin-track", n=2), rec("abandon-track", n=3)]
    for bad in (rec("abandon-done", n=4), rec("abandon-done", n=4, migration="m2")):
        f = rf.fold_repos(base + [bad], store_id=SID)
        assert f.repos["x"].mode == "aborting" and any("abandon-done" in p and "migration" in p for p in f.problems), f.problems
    f = rf.fold_repos(base + [rec("abandon-done", n=4, migration="m1")], store_id=SID)
    assert f.repos["x"].mode == "untracked" and not f.problems


def test_f7_a_corrected_re_ack_repairs_a_line_whose_first_ack_named_the_wrong_hash():
    bad = "{not json"
    mk = lambda sha, n: json.dumps({"v": 1, "store": SID, "op": "ack-torn", "line": 2, "sha256": sha, "why": "w", "at": f"2026-11-17T10:00:0{n}Z", "by": "d"})
    f = rf.fold_repos([rec("register"), bad, mk("f" * 64, 5), mk(hashlib.sha256(bad.encode()).hexdigest(), 6)], store_id=SID)
    assert not f.torn and f.acked == (2,)


def test_f8_an_import_rerun_does_not_re_pause_a_repo_the_operator_resumed(home, tmp_path, mkrepo, capsys, monkeypatch):
    mkrepo("alpha")
    mkrepo("beta")
    mkrepo("gamma")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa"), "beta": ("paused", "wb"), "gamma": ("paused", "wg")})
    store = tmp_path / "store"
    argv = ("use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "desk 1")
    real_pause = br.pause
    calls = []

    def pause_beta_then_crash(*a, **k):
        if calls:
            raise bs.BoardError("crash mid-import")
        calls.append(1)
        return real_pause(*a, **k)

    monkeypatch.setattr(br, "pause", pause_beta_then_crash)
    rc, _, err = run(capsys, *argv)
    assert rc == 2 and "crash mid-import" in err
    sd = Path(os.path.realpath(store))
    monkeypatch.setattr(br, "pause", real_pause)
    tgt = bs.Target(sd, sd / "items.jsonl", True)
    br.resume(tgt, "beta", "back", "desk 1", author={"kind": "operator", "name": "desk 1"})          # he resumed beta after the partial import
    ok(capsys, *argv)
    ops = [(r["op"], r["repo"]) for r in reg_lines(sd)]
    assert ops == [("register", "alpha"), ("register", "beta"), ("pause", "beta"), ("register", "gamma"), ("resume", "beta"), ("pause", "gamma")]
    assert br.read_registry(tgt).fold.repos["beta"].attention == "active"


def test_f9_withdrawn_abandon_done_checks_nothing_when_the_repo_had_a_charter_before(decision_seal, store, tmp_path, mkrepo, capsys):
    """design.md 4.2, the abandon-done bullet: "where X had a charter before, nothing is checked, because section 5 keeps it
    (revision 6, complement r5: revision 5 required the old blob back, which a kept edit never passes)". F9 (require the old blob
    back) was withdrawn by the head: charter_before is a blob sha, origin/main's CHARTER.md differs, abandon-done still succeeds."""
    a = mkrepo("alpha")
    land(a, "CHARTER.md", "# kept\n")
    m, ids = abandon_ready(capsys, tmp_path, store, a)
    before = [r for r in reg_lines(store) if r["op"] == "begin-track"][-1]["charter_before"]
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    land(a, "CHARTER.md", CHARTER.format(name="alpha", plan="- alpha#1"))             # his edit stays
    assert before != "none" and br._blob(str(a)) != before
    assert ok(capsys, "abandon-done", "alpha", *A) == "untracked alpha"


def test_f10_load_has_no_flock_parameter():
    import inspect
    assert "flock" not in inspect.signature(br._load).parameters


def test_f11_untrack_evidence_that_cannot_be_read_is_a_refusal_not_a_traceback(decision_seal, store, tmp_path, mkrepo, capsys, monkeypatch):
    from levain.desk import board_evidence as be
    a = mkrepo("alpha")
    track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    for exc in (OSError("disk"), subprocess.SubprocessError("git")):
        monkeypatch.setattr(be, "read_git_evidence", lambda *x, _e=exc, **k: (_ for _ in ()).throw(_e))
        rc, _, err = run(capsys, "untrack", "alpha", "--words", "bye", *A)
        assert rc == 2 and "refused" in err and "Traceback" not in err


# ---- round 3 (N1-N4) ------------------------------------------------------------------------------------------------


def test_n1_a_git_failure_while_identifying_a_repo_refuses_the_launch_unregistered_never_proceeds(store, home, tmp_path, mkrepo, monkeypatch):
    a = mkrepo("alpha")
    real = br._git

    def failing_rev_list(path, *args):
        if args and args[0] == "rev-list":
            return 128, "fatal: could not read object database"
        return real(path, *args)

    monkeypatch.setattr(br, "_git", failing_rev_list)
    with pytest.raises(br.BoardError) as e:
        br.register_for_launch(a, {"LEVAIN_HOME": str(home)})
    assert not isinstance(e.value, br.NoCommit)
    cs = CoordStore(tmp_path / "board")
    token = cs.coord_take()
    with pytest.raises(CoordError):
        cs.launch_request(token, harness="claude", role=ROLE_DESK, cwd=str(a))
    assert (store / "repos.jsonl").read_bytes() == b""


def test_n1_only_a_repo_with_no_commit_is_the_no_commit_signal(tmp_path):
    d = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    with pytest.raises(br.NoCommit):
        br.identity(d)


def _two_alphas(tmp_path, mkrepo, capsys):
    """A store holding ``alpha`` at repo ``other``'s path, and a briefcase whose ``alpha`` is a different repository."""
    mkrepo("alpha")
    other = mkrepo("other")
    store = tmp_path / "store"
    sd = Path(os.path.realpath(store))
    ok(capsys, "use", str(store), "--by", "d")
    tgt = bs.Target(sd, sd / "items.jsonl", True)
    br.register(tgt, "alpha", str(other), "w", "d", author={"kind": "operator", "name": "d"})
    return sd, store


def test_n2_an_import_whose_name_is_registered_at_another_repository_refuses_and_tombstones_nothing(home, tmp_path, mkrepo, capsys):
    sd, store = _two_alphas(tmp_path, mkrepo, capsys)
    focus = focus_file(tmp_path, {"alpha": ("paused", "wa")})
    before = focus.read_bytes()
    n = len(reg_lines(sd))
    rc, _, err = run(capsys, "use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    assert rc == 2 and "alpha" in err and str(tmp_path / "repos" / "alpha") in err and "other" in err
    assert focus.read_bytes() == before and len(reg_lines(sd)) == n


def test_n2_a_matching_partial_import_registration_is_skipped_not_refused(home, tmp_path, mkrepo, capsys):
    mkrepo("alpha")
    store = tmp_path / "store"
    sd = Path(os.path.realpath(store))
    ok(capsys, "use", str(store), "--by", "d")
    tgt = bs.Target(sd, sd / "items.jsonl", True)
    br.register(tgt, "alpha", str(tmp_path / "repos" / "alpha"), "w", "d", author={"kind": "operator", "name": "d"})
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    ok(capsys, "use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    assert [r["op"] for r in reg_lines(sd)] == ["register"]


def test_n3_an_acknowledged_torn_pause_or_resume_line_is_not_a_decision_of_the_registry():
    torn = rec("resume", n=2, store="z" * 32)                    # another store's line: torn
    lines = [rec("register", via="import-focus"), torn,
             json.dumps({"v": 1, "store": SID, "op": "ack-torn", "line": 2, "sha256": hashlib.sha256(torn.encode()).hexdigest(),
                         "why": "w", "at": "2026-11-17T10:00:05Z", "by": "d"})]
    f = rf.fold_repos(lines, store_id=SID)
    assert not f.torn and f.acked == (2,) and f.applied == (1,)              # the dropped line is never applied
    x = f.repos["x"]
    assert x.via == "import-focus" and x.history == 0
    f = rf.fold_repos(lines[:1] + [rec("pause", n=2)], store_id=SID)
    assert f.repos["x"].history == 1 and f.applied == (1, 2)
    f = rf.fold_repos(lines[:1] + [rec("resume", n=2, repo="ghost")], store_id=SID)      # a soft problem: not accepted
    assert f.repos["x"].history == 0 and f.applied == (1,)                   # a record, but not applied


def test_n3_a_repo_registered_by_hand_with_no_pause_record_is_not_paused_by_an_import_rerun(home, tmp_path, mkrepo, capsys):
    mkrepo("alpha")
    store = tmp_path / "store"
    sd = Path(os.path.realpath(store))
    ok(capsys, "use", str(store), "--by", "d")
    tgt = bs.Target(sd, sd / "items.jsonl", True)
    br.register(tgt, "alpha", str(tmp_path / "repos" / "alpha"), "by hand", "d", author={"kind": "operator", "name": "d"})
    focus = focus_file(tmp_path, {"alpha": ("paused", "wa")})
    ok(capsys, "use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "d")
    assert br.read_registry(tgt).fold.repos["alpha"].attention == "active"


def test_n4_launch_request_takes_a_one_shot_threads_iterable_whole(store, home, tmp_path, mkrepo, monkeypatch):
    from levain.desk import lease
    a = mkrepo("alpha")
    seen = []
    real = lease.request_launch

    def spy(conn, now, token, **kw):
        seen.append(tuple(kw["threads"]))
        return real(conn, now, token, **kw)

    monkeypatch.setattr(lease, "request_launch", spy)
    cs = CoordStore(tmp_path / "board")
    token = cs.coord_take()
    try:
        cs.launch_request(token, harness="claude", role="worker", threads=(t for t in ["t1"]), cwd=str(a))
    except CoordError:
        pass                                                         # an unknown thread: refused AFTER the lease read what was asked
    assert seen == [("t1",)]


def test_p1b_fix1_a_registry_chain_ack_keeps_the_line_and_binds_the_break(store, mkrepo, capsys):
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    ok(capsys, "pause", "alpha", "--words", "w", *A)
    ok(capsys, "resume", "alpha", "--words", "w", *A)
    p = store / "repos.jsonl"
    lines = p.read_bytes().split(b"\n")[:-1]
    lines[1] = lines[1].replace(b'"words":"w"', b'"words":"edited"')                # line 2 edited: line 3 no longer chains onto it
    p.write_bytes(b"\n".join(lines) + b"\n")
    rc, _, err = run(capsys, "repos")
    assert rc == 2 and "line 2 was edited or deleted" in err and "ack-torn --file repos.jsonl --line 3" in err
    out = ok(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "3", "--why", "hand edit", *A)
    assert "line 2 is now " in out and "an edit, a deletion, an insertion or a reorder before line 3" in out
    assert run(capsys, "repos")[0] == 0
    assert br.read_registry(bs.resolve_store(None)).fold.repos["alpha"].attention == "active"     # line 3 (the resume) is KEPT
    lines = p.read_bytes().split(b"\n")[:-1]
    lines[1] = lines[1].replace(b'"words":"edited"', b'"words":"edited-again"')
    p.write_bytes(b"\n".join(lines) + b"\n")
    assert run(capsys, "repos")[0] == 2                                              # the same line before is edited again: breaks again


# --------------------------------------------------------------------------------------------------------------------
# charter init / check act only on the registered repository; one name rule (levain#105)


def _charter(capsys, *argv):
    rc = main(["charter", *argv])
    out, err = capsys.readouterr()
    return rc, out.strip(), err.strip()


def test_charter_init_and_check_refuse_a_repo_that_is_not_the_registered_one(store, tmp_path, mkrepo, capsys, monkeypatch):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    monkeypatch.chdir(b)                                                # the wrong repository, by the default of `here`
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 2 and f"alpha is registered at {os.path.realpath(a)}" in err and f"Pass --repo {os.path.realpath(a)}" in err
    assert os.path.realpath(b) in err and not (b / "CHARTER.md").exists()
    rc, _, err = _charter(capsys, "check", "--repo-name", "alpha", "--worktree")
    assert rc == 2 and f"Pass --repo {os.path.realpath(a)}" in err
    rc, _, err = _charter(capsys, "init", "gamma")                        # an unregistered name: the old behaviour (it writes here)
    assert rc == 0 and (b / "CHARTER.md").exists()
    rc, out, err = _charter(capsys, "init", "alpha", "--repo", str(a))    # the registered path: written
    assert rc == 0 and (a / "CHARTER.md").exists()
    rc, out, _ = _charter(capsys, "check", "--repo-name", "alpha", "--repo", str(a), "--worktree")
    assert rc == 0 and "CHARTER.md" in out


def test_charter_guard_accepts_a_worktree_of_the_registered_repo_and_still_refuses_another_repo(store, tmp_path, mkrepo, capsys, monkeypatch):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    wt = tmp_path / "alpha-wt"
    git(a, "worktree", "add", "-q", "-b", "seat/x", str(wt))
    monkeypatch.chdir(wt)                                               # a worktree of alpha: the same repository
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 0, err
    assert (wt / "CHARTER.md").exists() and not (a / "CHARTER.md").exists()      # written THERE, not in the main checkout
    rc, _, err = _charter(capsys, "check", "--repo-name", "alpha", "--worktree")
    assert rc == 0 and "registered at" not in err
    monkeypatch.chdir(b)                                                # an unrelated repository: still refused, nothing written
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 2 and "Pass --repo" in err and not (b / "CHARTER.md").exists()


def test_one_name_rule_a_registered_name_the_charter_can_use_and_track(decision_seal, store, tmp_path, mkrepo, capsys):
    name = "9lives.app"                                                 # a digit first and a dot: `register` takes it, so must the charter
    a = mkrepo(name)
    ok(capsys, "register", name, "--path", str(a), "--words", "w", *A)
    ok(capsys, "begin-track", name, "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    m = migration_of(store, name)
    iid = add_item(capsys, name, m, "a")
    rc, out, err = _charter(capsys, "init", name, "--repo", str(a), "--items", str(store / "items.jsonl"))
    assert rc == 0, err
    text = (a / "CHARTER.md").read_text("utf-8")
    assert f"# Charter: {name}" in text and f"- {iid}" in text
    land(a, "CHARTER.md", CHARTER.format(name=name, plan=f"- {iid}"))
    assert charter.check_text(CHARTER.format(name=name, plan=f"- {iid}"), repo_name=name)[0] == []
    ok(capsys, "track", name, "--words", "go", *A)
    assert reg_lines(store)[-1]["op"] == "track"


def test_charter_guard_refuses_when_the_registry_or_the_registered_path_cannot_be_read(store, tmp_path, mkrepo, capsys, monkeypatch):
    import shutil
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    monkeypatch.chdir(b)
    # a registered path that is gone: refused, with the move hint, nothing written
    moved = tmp_path / "alpha-moved"
    a.rename(moved)
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 2 and "cannot be read as a git repository" in err and "levain board move" in err and f"--repo {os.path.realpath(a)}" in err
    assert not (b / "CHARTER.md").exists()
    rc, _, err = _charter(capsys, "check", "--repo-name", "alpha", "--worktree")
    assert rc == 2 and "levain board move" in err
    # --file reads only that file: not refused
    f = tmp_path / "C.md"
    f.write_text("x\n", encoding="utf-8")
    rc, _, err = _charter(capsys, "check", "--repo-name", "alpha", "--file", str(f))
    assert rc == 0 and "registered at" not in err
    # --json refused: one document, rc 2
    rc, out, _ = _charter(capsys, "check", "--repo-name", "alpha", "--worktree", "--json")
    assert rc == 2 and json.loads(out)["valid"] is False and "levain board move" in json.loads(out)["why"]
    # --stdout writes nothing: the guard does not run
    rc, out, _ = _charter(capsys, "init", "alpha", "--stdout")
    assert rc == 0 and out.startswith("# Charter: alpha")
    moved.rename(a)
    # a torn registry (a store IS found): refused, not open
    p = store / "repos.jsonl"
    p.write_bytes(p.read_bytes() + b'{"torn')
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 2 and "cannot tell where alpha is registered" in err and not (b / "CHARTER.md").exists()


def test_charter_guard_stays_open_with_no_store_or_an_unregistered_name(home, tmp_path, mkrepo, capsys, monkeypatch):
    b = mkrepo("beta")
    monkeypatch.chdir(b)                                                # no store configured at all
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 0 and (b / "CHARTER.md").exists(), err


def _drop_pointer(home):
    """The store stays on disk (store.id, repos.jsonl); only the pointer to it goes: a store reached by --items or the environment."""
    (bs._home_dir(None) / bs.POINTER_NAME).unlink()


def test_charter_guard_holds_for_a_store_reached_by_items_or_the_environment_with_no_pointer(store, home, tmp_path, mkrepo, capsys, monkeypatch):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    _drop_pointer(home)
    monkeypatch.chdir(b)
    rc, _, err = _charter(capsys, "init", "alpha", "--items", str(store))              # the ceremony's own command, from the wrong repo
    assert rc == 2 and f"Pass --repo {os.path.realpath(a)}" in err and not (b / "CHARTER.md").exists()
    rc, _, err = _charter(capsys, "check", "--repo-name", "alpha", "--worktree", "--items", str(store))
    assert rc == 2 and f"Pass --repo {os.path.realpath(a)}" in err
    monkeypatch.setenv("LEVAIN_BOARD_ITEMS", str(store))                               # the environment alone, no --items
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 2 and not (b / "CHARTER.md").exists()
    rc, out, err = _charter(capsys, "init", "alpha", "--repo", str(a))                 # the registered repository: written
    assert rc == 0 and (a / "CHARTER.md").exists(), err


def test_charter_guard_refuses_when_a_path_or_the_pointer_cannot_be_resolved(store, home, tmp_path, mkrepo, capsys, monkeypatch):
    a, b = mkrepo("alpha"), mkrepo("beta")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    monkeypatch.chdir(b)
    real = Path.resolve
    for exc in (OSError("Permission denied"), RuntimeError("Symlink loop")):
        for bad_path in (os.path.realpath(a), os.path.realpath(b)):                    # the REGISTERED path, then the target
            def resolve(self, *a_, _exc=exc, _bad=bad_path, **k):
                if str(self) == _bad:
                    raise _exc
                return real(self, *a_, **k)
            monkeypatch.setattr(Path, "resolve", resolve)
            rc, _, err = _charter(capsys, "init", "alpha")
            assert rc == 2 and "cannot tell" in err and str(exc) in err and not (b / "CHARTER.md").exists(), (exc, bad_path, err)
            monkeypatch.setattr(Path, "resolve", real)

    def denied():
        raise PermissionError("denied")
    monkeypatch.setattr(bs, "read_pointer", denied)                                    # a pointer failure that is not a BoardError
    rc, _, err = _charter(capsys, "init", "alpha")
    assert rc == 2 and "cannot tell" in err and "denied" in err and not (b / "CHARTER.md").exists()


def test_charter_guard_returns_none_only_for_no_store_an_unregistered_name_and_the_same_repository(home, tmp_path, mkrepo, capsys, monkeypatch):
    """Every open (None) path of ``_wrong_repo``, enumerated; every other reading of the world is a refusal text."""
    a, b = mkrepo("alpha"), mkrepo("beta")
    top = Path(os.path.realpath(b))
    assert charter._wrong_repo("init", "alpha", top, None) is None                     # 1. no store anywhere (no items, no env, no pointer, no home store)
    ok(capsys, "use", str(tmp_path / "store"))
    sdir = Path(os.path.realpath(tmp_path / "store"))
    (sdir / "repos.jsonl").touch()
    assert charter._wrong_repo("init", "alpha", top, None) is None                     # 2. a store, NAME not registered
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert charter._wrong_repo("init", "alpha", Path(os.path.realpath(a)), None) is None     # 3. the registered repository itself
    assert charter._wrong_repo("init", "alpha", top, None) is not None                 # ... and the wrong one is a text
    assert charter._wrong_repo("init", "alpha", None, None) is None                    # (a target that is no repository: out of scope, as before)
    refusals = {
        "torn registry": lambda: (sdir / "repos.jsonl").write_bytes((sdir / "repos.jsonl").read_bytes() + b'{"torn'),
        "pointer names no store": lambda: (sdir / "store.id").unlink(),
    }
    for label, break_it in refusals.items():
        break_it()
        assert isinstance(charter._wrong_repo("init", "alpha", Path(os.path.realpath(a)), None), str), label
    (sdir / "store.id").write_text("x")
    monkeypatch.setenv("LEVAIN_BOARD_ITEMS", str(tmp_path / "elsewhere"))              # env names a store other than the pointer's: two stores
    assert isinstance(charter._wrong_repo("init", "alpha", top, None), str)
