"""T3: ``levain board state`` with no ``--repo`` reads every tracked repo of the registry, each by its own evidence, from one
snapshot of the store (items, repos.jsonl, store.id read under one shared lock that is let go before any git read)."""
from __future__ import annotations

import json
import shutil

from levain.desk import board_cli as bc
from levain.desk import board_evidence as be
from levain.desk import board_store as bs
from tests.test_board_registry import A, bare_store, git, home, mkrepo, ok, run, store, track_ready  # noqa: F401


def _two_tracked(capsys, tmp_path, store, mkrepo):
    a, b = mkrepo("alpha"), mkrepo("beta")
    track_ready(capsys, tmp_path, store, a, "alpha")
    ok(capsys, "track", "alpha", "--words", "go", *A)
    track_ready(capsys, tmp_path, store, b, "beta")
    ok(capsys, "track", "beta", "--words", "go", *A)
    return a, b


def _state(capsys, *argv):
    rc, out, err = run(capsys, "state", "--json", *argv)
    return rc, (json.loads(out) if out else None), err


def _states(doc):
    return {s["id"]: s["state"] for s in doc["items"]}


def test_no_repo_reads_every_tracked_repo_and_only_those(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    c = mkrepo("gamma")
    ok(capsys, "register", "gamma", "--path", str(c), "--words", "w", *A)                  # untracked: holds no items, not read
    rc, doc, err = _state(capsys)
    assert rc in (0, 3), err
    assert set(_states(doc)) == {"alpha#1", "beta#1"}
    assert doc["reader"]["repo"] == "alpha" and [x["repo"] for x in doc["reader"]["also"]] == ["beta"]


def test_each_item_is_derived_from_its_own_repos_evidence(store, tmp_path, mkrepo, capsys):
    a, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    _, before, _ = _state(capsys)
    git(b, "commit", "-q", "--allow-empty", "-m", "done it\n\nBoard-Item: beta#1\nBoard-Closes: beta#1")
    git(b, "push", "-q", "origin", "main")
    git(b, "fetch", "-q")
    _, after, _ = _state(capsys)
    assert _states(after)["alpha#1"] == _states(before)["alpha#1"]                            # alpha's own git said nothing new
    assert _states(after)["beta#1"] != _states(before)["beta#1"]                              # beta's git did
    _, single, _ = _state(capsys, "--repo", str(a))
    assert _states(single)["beta#1"] != _states(after)["beta#1"]                              # --repo alone is unchanged: beta's item read from alpha's git


def test_a_tracked_repo_whose_path_is_gone_is_named_on_the_audit_and_the_others_still_derive(store, tmp_path, mkrepo, capsys):
    _, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(b)
    rc, doc, _ = _state(capsys)
    assert rc == 3
    assert "alpha#1" in _states(doc)
    why = [e["why"] for e in doc["audit"] if "beta" in e["why"] and "cannot be read" in e["why"]]
    assert why and "is gone" in why[0]


def test_a_tracked_repo_that_is_not_a_git_repository_is_named_too(store, tmp_path, mkrepo, capsys):
    _, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(b / ".git")
    rc, doc, _ = _state(capsys)
    assert rc == 3 and "alpha#1" in _states(doc)
    assert any("beta cannot be read" in e["why"] and "not a git repository" in e["why"] for e in doc["audit"])


def test_every_tracked_repo_unreadable_refuses_naming_them(store, tmp_path, mkrepo, capsys):
    a, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(a)
    shutil.rmtree(b)
    rc, out, err = run(capsys, "state", "--json")
    assert rc == 2 and "alpha cannot be read" in err and "beta cannot be read" in err


def test_a_paused_tracked_repo_is_still_read(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    ok(capsys, "pause", "beta", "--words", "later", *A)
    _, doc, _ = _state(capsys)
    assert set(_states(doc)) == {"alpha#1", "beta#1"}


def test_the_store_lock_is_let_go_before_any_git_read(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    real = be.read_git_evidence
    seen: list[str] = []

    def hooked(*a, **kw):
        with bs.store_lock(store, exclusive=True, wait=0.5):                     # a writer gets in at every git read, or this refuses
            seen.append("writer in")
        return real(*a, **kw)

    monkeypatch.setattr(be, "read_git_evidence", hooked)
    rc, doc, err = _state(capsys)
    assert rc in (0, 3), err
    assert len(seen) == 2


def test_the_wait_for_a_writer_is_bounded_and_named(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    monkeypatch.setattr(bc, "STATE_LOCK_WAIT", 0.2)
    with bs.store_lock(store, exclusive=True):
        rc, out, err = run(capsys, "state", "--json")
    assert rc == 2 and "the board store is locked by a writer; try again" in err
    rc, _, _ = _state(capsys)                                                     # the writer gone: it reads
    assert rc in (0, 3)


def test_a_torn_registry_refuses_the_whole_board_read(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    with open(store / "repos.jsonl", "ab") as f:
        f.write(b'{"torn')
    rc, out, err = run(capsys, "state", "--json")
    assert rc == 2 and "refused" in err


def test_repo_given_keeps_the_single_repo_read(store, tmp_path, mkrepo, capsys):
    a, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    rc, doc, err = _state(capsys, "--repo", str(b))
    assert rc in (0, 3), err
    assert doc["reader"]["repo"] == "beta" and "also" not in doc["reader"]
    rc, doc, err = _state(capsys, "--repo", str(a), "--also-repo", f"beta={b}")
    assert [x["repo"] for x in doc["reader"]["also"]] == ["beta"] and set(_states(doc)) == {"alpha#1", "beta#1"}


def test_a_store_with_no_registry_reads_the_repo_here_as_before(bare_store, tmp_path, mkrepo, capsys, monkeypatch):
    a = mkrepo("alpha")
    monkeypatch.chdir(a)
    rc, doc, err = _state(capsys)
    assert rc in (0, 3), err and doc["reader"]["repo"] == "alpha"
