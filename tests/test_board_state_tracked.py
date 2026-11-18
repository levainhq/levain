"""T3: ``levain board state`` with no ``--repo`` reads every tracked repo of the registry, each by its own evidence, from one
snapshot of the store (items, repos.jsonl, store.id read under one shared lock that is let go before any git read)."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import time

from levain.desk import board_cli as bc
from levain.desk import board_evidence as be
from levain.desk import board_store as bs
from tests.test_board_registry import (A, add_item, bare_store, git, home, manifest, migration_of, mkrepo, ok, run, store,  # noqa: F401
                                       track_ready)


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
    assert doc["reader"]["repo"] == "alpha, beta" and [x["repo"] for x in doc["reader"]["repos"]] == ["alpha", "beta"]


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




def _gone_items(doc, repo):
    return [i for i in doc["items"] if i["id"].startswith(f"{repo}#")]


def test_a_tracked_repo_whose_path_is_gone_is_named_on_the_audit_and_its_items_read_unknown(store, tmp_path, mkrepo, capsys):
    _, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(b)
    rc, doc, _ = _state(capsys)
    assert rc == 3
    assert "alpha#1" in _states(doc)
    assert _states(doc)["beta#1"] == "unknown"                                                # never dropped from the board
    (gone,) = [i for i in doc["items"] if i["id"] == "beta#1"]
    assert "is gone" in gone["reason"]                                                        # the item itself says why
    why = [e["why"] for e in doc["audit"] if e["id"] is None and e["why"].startswith("beta: tracked repo cannot be read")]
    assert len(why) == 1 and "is gone" in why[0]                                              # one repo line, not one per source
    assert any(p.startswith("beta: tracked repo cannot be read") for p in doc["problems"])


def test_a_tracked_repo_that_is_not_a_git_repository_is_named_too(store, tmp_path, mkrepo, capsys):
    _, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(b / ".git")
    rc, doc, _ = _state(capsys)
    assert rc == 3 and "alpha#1" in _states(doc) and _states(doc)["beta#1"] == "unknown"
    assert any(e["why"].startswith("beta: tracked repo cannot be read") and "not a git repository" in e["why"] for e in doc["audit"])


def test_a_repo_whose_git_is_gone_inside_another_repository_is_not_read_as_that_repository(store, tmp_path, mkrepo, capsys):
    _, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(b / ".git")
    git(tmp_path / "repos", "init", "-q", "-b", "main")                                       # the parent directory is now a repository
    rc, doc, _ = _state(capsys)
    assert rc == 3 and _states(doc)["beta#1"] == "unknown"
    assert any("beta: tracked repo cannot be read" in e["why"] and "lies inside the one at" in e["why"] for e in doc["audit"])


def test_every_tracked_repo_unreadable_refuses_naming_them(store, tmp_path, mkrepo, capsys):
    a, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(a)
    shutil.rmtree(b)
    rc, out, err = run(capsys, "state", "--json")
    assert rc == 2 and "alpha: tracked repo cannot be read" in err and "beta: tracked repo cannot be read" in err


def test_an_unreadable_first_repo_leaves_the_others_to_name_the_board(store, tmp_path, mkrepo, capsys):
    a, _ = _two_tracked(capsys, tmp_path, store, mkrepo)
    shutil.rmtree(a)                                                                          # alpha is first by name
    rc, doc, err = _state(capsys)
    assert rc == 3, err
    assert [x["repo"] for x in doc["reader"]["repos"]] == ["beta"] and doc["reader"]["repo"] == "beta"
    assert doc["as_of"] is not None                                                           # beta's fetch, not alpha's absence
    assert _states(doc)["alpha#1"] == "unknown" and "beta#1" in _states(doc)
    assert any(e["why"].startswith("alpha: tracked repo cannot be read") for e in doc["audit"])


def test_a_tracking_repo_and_an_untracked_repo_holding_items_are_not_read_and_say_why(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    c, d = mkrepo("gamma"), mkrepo("delta")
    track_ready(capsys, tmp_path, store, c, "gamma")                                          # begin-track, item, charter: no `track` yet
    _, did = track_ready(capsys, tmp_path, store, d, "delta")
    ok(capsys, "track", "delta", "--words", "go", *A)
    ok(capsys, "decide", "--id", did, "--decision", "out-of-1.0", "--words", "later", *A)
    ok(capsys, "untrack", "delta", "--words", "bye", *A)                                      # untracked, and its item is still in the store
    rc, doc, err = _state(capsys)
    assert rc == 3, err
    assert _states(doc)["gamma#1"] == "unknown" and "delta#1" in _states(doc)                 # both still on the board
    reasons = {e["why"] for e in doc["audit"] if e["id"] is None}
    assert any(w.startswith("gamma: the repo is tracking, not tracked: not read") for w in reasons)
    assert any(w.startswith("delta: the repo is untracked, not tracked: not read") for w in reasons)
    assert [x["repo"] for x in doc["reader"]["repos"]] == ["alpha", "beta"]                   # neither was read


def test_an_aborted_repos_cut_items_are_settled_not_unknown_and_make_no_audit_line_levain93(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    c, d = mkrepo("gamma"), mkrepo("delta")
    ok(capsys, "register", "gamma", "--path", str(c), "--words", "w", *A)
    ok(capsys, "begin-track", "gamma", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    add_item(capsys, "gamma", migration_of(store, "gamma"), "a")
    ok(capsys, "abandon-track", "gamma", "--words", "never mind", *A)
    ok(capsys, "abandon-done", "gamma", *A)
    _, did = track_ready(capsys, tmp_path, store, d, "delta")                                 # an undecided item of a repo not read stays unknown
    rc, doc, err = _state(capsys)
    assert rc == 3, err
    assert _states(doc)["gamma#1"] == "cut" and _states(doc)["delta#1"] == "unknown"
    reasons = [e["why"] for e in doc["audit"] if e["id"] is None]
    assert not any(w.startswith("gamma:") for w in reasons) and not any(p.startswith("gamma:") for p in doc["problems"])
    assert any(w.startswith("delta: the repo is tracking, not tracked: not read") for w in reasons)


def test_one_freshness_and_one_labeling_across_repos(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    stamps = {"alpha": "2026-11-17T09:00:00Z", "beta": "2026-11-16T09:00:00Z"}                 # beta is the stale one, alpha the primary
    real = be.read_git_evidence

    def hooked(repo, **kw):
        ev, summary = real(repo, **kw)
        ev["fetched_at"] = stamps[kw["repo_name"]]
        return ev, summary

    monkeypatch.setattr(be, "read_git_evidence", hooked)
    rc, doc, err = _state(capsys, "--now", "2026-11-17T10:00:00Z")
    assert rc == 3, err
    assert doc["as_of"] == "2026-11-16T09:00:00Z"                                             # the stalest repo, not the first by name
    stamps["alpha"], stamps["beta"] = "2026-11-10T09:00:00Z", "2026-11-17T09:00:00Z"
    _, doc, _ = _state(capsys, "--now", "2026-11-17T10:00:00Z")
    assert doc["as_of"] == "2026-11-10T09:00:00Z"                                             # and it follows the stalest, whoever it is
    git_lines = [e["why"] for e in doc["audit"] if e["kind"] == "unattributed_work"]
    assert git_lines and all(w.startswith(("alpha: ", "beta: ")) for w in git_lines)          # the primary's lines carry its name too
    assert any(w.startswith("alpha: ") for w in git_lines) and any(w.startswith("beta: ") for w in git_lines)
    assert any(e["why"].startswith("stale_evidence: alpha: ") for e in doc["audit"])
    r = doc["reader"]
    assert r["unattributed"] == sum(x["unattributed"] for x in r["repos"]) == len(git_lines)  # the counts cover every repo read
    assert r["main_commits"] == sum(x["main_commits"] for x in r["repos"])
    stamps["alpha"] = None                                                                    # a repo with no fetch time: the board has none
    stamps["beta"] = "2026-11-17T09:00:00Z"
    _, doc, _ = _state(capsys, "--now", "2026-11-17T10:00:00Z")
    assert doc["as_of"] is None


def test_repo_name_without_repo_on_the_whole_board_refuses(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    rc, out, err = run(capsys, "state", "--json", "--repo-name", "flow")
    assert rc == 2 and "--repo-name" in err and "--repo" in err and out == ""
    a = mkrepo("alpha2")
    rc, _, err = run(capsys, "state", "--json", "--repo", str(a), "--repo-name", "alpha2")     # with --repo it is the single-repo flag again
    assert rc in (0, 3), err


def test_a_paused_tracked_repo_is_still_read(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    ok(capsys, "pause", "beta", "--words", "later", *A)
    _, doc, _ = _state(capsys)
    assert set(_states(doc)) == {"alpha#1", "beta#1"}


def test_the_store_lock_is_let_go_before_any_git_or_document_read(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    seen: list[str] = []

    def hook(module, name):
        real = getattr(module, name)

        def hooked(*a, **kw):
            with bs.store_lock(store, exclusive=True, wait=0.5):                              # a writer gets in at every read, or this refuses
                seen.append(name)
            return real(*a, **kw)

        monkeypatch.setattr(module, name, hooked)

    hook(be, "read_git_evidence")
    hook(be, "read_documents")
    rc, doc, err = _state(capsys)
    assert rc in (0, 3), err
    assert seen.count("read_git_evidence") == 2 and seen.count("read_documents") == 2


def test_one_hold_and_no_second_lock_for_the_registry_audit_on_any_path(store, tmp_path, mkrepo, capsys, monkeypatch):
    a, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    taken: list[bool] = []
    real = bs.store_lock

    def counting(*args, **kw):
        taken.append(True)
        return real(*args, **kw)

    monkeypatch.setattr(bs, "store_lock", counting)
    for argv in ([], ["--repo", str(a)], ["--repo", str(a), "--also-repo", f"beta={b}"]):
        taken.clear()
        rc, _, err = _state(capsys, *argv)[0], None, None
        assert rc in (0, 3)
        assert len(taken) == 2, argv                                                         # the hold, and the items read's own inside it


def test_the_wait_for_a_writer_is_bounded_named_and_has_its_own_exit(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    monkeypatch.setattr(bc, "STATE_LOCK_WAIT", 0.3)
    t0 = time.monotonic()
    with bs.store_lock(store, exclusive=True):
        rc, out, err = run(capsys, "state", "--json")
    waited = time.monotonic() - t0
    assert rc == bc.EXIT_LOCKED == 4 and "the board store is locked by a writer; try again" in err
    assert 0.25 <= waited < 3                                                                # about the wait, not the wait times the retries
    rc, _, _ = _state(capsys)                                                                # the writer gone: it reads
    assert rc in (0, 3)


def test_readers_do_not_wait_for_each_other(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    monkeypatch.setattr(bc, "STATE_LOCK_WAIT", 0.3)
    with bs.store_lock(store, exclusive=False):
        rc, _, err = _state(capsys)[0], None, None
    assert rc in (0, 3)


def test_the_lock_wait_raises_a_typed_error_that_is_not_matched_by_its_text(store):
    with bs.store_lock(store, exclusive=True):
        try:
            with bs.store_lock(store, exclusive=False, wait=0.05):
                raise AssertionError("the lock was free")
        except bs.LockWaiting as exc:
            assert isinstance(exc, bs.BoardError) and str(exc) == bs.LOCK_WAITING
    assert not issubclass(bs.BoardError, bs.LockWaiting)


def test_one_deadline_spans_every_retry_pass(store, monkeypatch):
    seen: list[float] = []

    def lock(fd, op, deadline):
        seen.append(deadline)

    monkeypatch.setattr(bs, "_lock_bounded", lock)
    monkeypatch.setattr(bs, "_same_file", lambda fd, path: False)                             # every pass is a replaced file
    try:
        bs._open_checked(store / bs.LOCK_NAME, os.O_RDONLY, fcntl.LOCK_SH, deadline=bs._deadline_of(5))
    except bs.BoardError as exc:
        assert "keeps being replaced" in str(exc)
    assert len(seen) == bs.OPEN_RETRIES and len(set(seen)) == 1


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_the_lock_fd_is_closed_on_any_exception_including_an_interrupt(store, monkeypatch):
    before = _open_fds()
    with bs.store_lock(store, exclusive=True):
        held = _open_fds()

        def interrupted(_):
            raise KeyboardInterrupt

        monkeypatch.setattr(bs.time, "sleep", interrupted)
        try:
            bs._open_checked(store / bs.LOCK_NAME, os.O_RDONLY, fcntl.LOCK_SH, deadline=bs._deadline_of(5))
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("no interrupt")
        assert _open_fds() == held
    assert _open_fds() == before


def test_a_torn_registry_refuses_the_whole_board_read(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    with open(store / "repos.jsonl", "ab") as f:
        f.write(b'{"torn')
    rc, out, err = run(capsys, "state", "--json")
    assert rc == 2 and "refused" in err and "the registry is unreadable" in err and out == ""
    a = mkrepo("alpha3")
    rc, doc, _ = _state(capsys, "--repo", str(a))                                             # one repo: a board and an audit line
    assert rc == 3 and any(e["why"].startswith("registry: ") for e in doc["audit"])


def test_a_registry_that_cannot_be_read_refuses_the_whole_board_read(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    (store / "store.id").unlink()
    rc, out, err = run(capsys, "state", "--json")
    assert rc == 2 and "refused" in err and "store.id" in err and out == ""


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


def test_a_pointered_store_with_no_registry_file_behaves_as_before_whatever_the_registry_load_says(bare_store, tmp_path, mkrepo, capsys,
                                                                                                    monkeypatch):
    a = mkrepo("alpha")
    monkeypatch.chdir(a)
    rc, doc, err = _state(capsys)
    assert rc in (0, 3) and doc["reader"]["repo"] == "alpha" and not [e for e in doc["audit"] if e["why"].startswith("registry:")]

    def unreadable(store_dir, deadline=None):
        raise bs.BoardError(f"{store_dir} holds no store.id; run `levain board use`")

    monkeypatch.setattr(bc.br, "_load", unreadable)                                           # the audit says so, as before; no refusal
    rc, doc, err = _state(capsys)
    assert doc is not None, err
    assert doc["reader"]["repo"] == "alpha"
    assert rc == 3 and any(e["why"].startswith("registry: unreadable:") and "store.id" in e["why"] for e in doc["audit"])


# --------------------------------------------------------------------------------------------------- T3 fix 2 (L3 round 1)


def test_a_flock_on_the_items_file_alone_is_a_bounded_wait_too(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)                                             # flow's old board.py flocks items.jsonl, not store.lock
    monkeypatch.setattr(bc, "STATE_LOCK_WAIT", 0.3)
    fd = os.open(store / bs.ITEMS_NAME, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        rc, out, err = run(capsys, "state", "--json")
        waited = time.monotonic() - t0
    finally:
        os.close(fd)
    assert rc == bc.EXIT_LOCKED and "locked by a writer" in err and out == ""
    assert 0.25 <= waited < 3


def test_every_state_path_read_takes_the_deadline(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    missing: list[str] = []
    real_read, real_open = bs._read_stable, bs._open_checked

    def read(path, flock_op=None, deadline=None):
        if flock_op is not None and deadline is None:
            missing.append(f"_read_stable {path.name}")
        return real_read(path, flock_op, deadline)

    def opened(path, flags, flock_op=None, missing_ok=False, deadline=None):
        if flock_op is not None and deadline is None:
            missing.append(f"_open_checked {path.name}")
        return real_open(path, flags, flock_op, missing_ok, deadline)

    monkeypatch.setattr(bs, "_read_stable", read)
    monkeypatch.setattr(bs, "_open_checked", opened)
    for argv in ([], ["--repo", str(tmp_path / "repos" / "alpha")]):
        assert _state(capsys, *argv)[0] in (0, 3)
    assert missing == []


def _unpoint(store):
    (bs._home_dir(None) / bs.POINTER_NAME).unlink()


def test_an_explicit_store_with_an_id_is_read_like_a_pointered_one(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    _unpoint(store)
    taken: list[bool] = []
    real = bs.store_lock

    def counting(*args, **kw):
        taken.append(True)
        return real(*args, **kw)

    monkeypatch.setattr(bs, "store_lock", counting)
    monkeypatch.chdir(tmp_path)                                                               # not a git repository: the cwd must not matter
    for given in (store, store / bs.ITEMS_NAME):
        taken.clear()
        rc, doc, err = _state(capsys, "--items", str(given))
        assert rc in (0, 3), err
        assert [x["repo"] for x in doc["reader"]["repos"]] == ["alpha", "beta"] and {"alpha#1", "beta#1"} <= set(_states(doc))
        assert len(taken) == 2
    monkeypatch.setattr(bc, "STATE_LOCK_WAIT", 0.3)
    monkeypatch.setattr(bs, "store_lock", real)
    with bs.store_lock(store, exclusive=True):
        assert run(capsys, "state", "--json", "--items", str(store))[0] == bc.EXIT_LOCKED    # and it waits on the store lock


def test_an_explicit_dir_with_no_store_id_stays_a_plain_items_file(tmp_path, mkrepo, capsys, home, monkeypatch):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "items.jsonl").write_text("")
    a = mkrepo("alpha")
    monkeypatch.chdir(a)
    rc, doc, err = _state(capsys, "--items", str(d))
    assert rc in (0, 3), err
    assert doc["reader"]["repo"] == "alpha"


def test_a_repo_holding_items_but_absent_from_the_registry_reads_unknown_and_says_so(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    g = mkrepo("ghost")
    track_ready(capsys, tmp_path, store, g, "ghost")                                          # an item in the store
    path = store / "repos.jsonl"                                                              # the registry loses the repo (its own tail), the item stays
    path.write_text("".join(ln for ln in path.read_text("utf-8").splitlines(True) if json.loads(ln)["repo"] != "ghost"))
    rc, doc, err = _state(capsys)
    assert rc == 3, err
    assert _states(doc)["ghost#1"] == "unknown" and "alpha#1" in _states(doc)
    lines = [e["why"] for e in doc["audit"] if e["id"] is None and e["why"].startswith("ghost: ")]
    assert len(lines) == 1 and "not registered" in lines[0]
    assert [x["repo"] for x in doc["reader"]["repos"]] == ["alpha", "beta"]


def test_the_top_level_unattributed_holds_every_repos_commits_prefixed(store, tmp_path, mkrepo, capsys):
    a, b = _two_tracked(capsys, tmp_path, store, mkrepo)
    for repo in (a, b):
        (repo / "x.txt").write_text("x")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", f"stray work in {repo.name}")
        git(repo, "push", "-q", "origin", "main")
        git(repo, "fetch", "-q")
    rc, doc, err = _state(capsys)
    assert rc == 3, err
    top = [u["subject"] for u in doc["unattributed"]]
    assert any(s.startswith("alpha: ") for s in top) and any(s.startswith("beta: ") for s in top)
    lines = [e["why"] for e in doc["audit"] if e["kind"] == "unattributed_work"]
    assert len(top) == len(lines) == doc["reader"]["unattributed"]


def test_a_stale_non_primary_repo_fails_closed_like_a_stale_primary(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    stamps = {"alpha": "2026-11-17T09:00:00Z", "beta": "2026-11-10T09:00:00Z"}                 # alpha, the primary, is fresh
    real = be.read_git_evidence

    def hooked(repo, **kw):
        ev, summary = real(repo, **kw)
        ev["fetched_at"] = stamps[kw["repo_name"]]
        return ev, summary

    monkeypatch.setattr(be, "read_git_evidence", hooked)
    rc, doc, err = _state(capsys, "--now", "2026-11-17T10:00:00Z")
    assert rc == 3, err
    stale = [e["why"] for e in doc["audit"] if e["why"].startswith("stale_evidence")]
    assert len(stale) == 1 and stale[0].startswith("stale_evidence: beta: ")
    assert doc["as_of"] == "2026-11-10T09:00:00Z"
    beta = next(s for s in doc["items"] if s["id"] == "beta#1")
    assert beta["state"] == "unknown" and "older than" in beta["reason"]


def test_the_registry_audit_fallback_is_bounded_and_a_busy_lock_is_not_an_audit_line(store, tmp_path, mkrepo, capsys, monkeypatch):
    import pytest
    from datetime import datetime, timezone
    _two_tracked(capsys, tmp_path, store, mkrepo)
    target = bs.resolve_store(None)
    now = datetime.now(timezone.utc)
    with bs.store_lock(store, exclusive=True):
        t0 = time.monotonic()
        with pytest.raises(bs.LockWaiting):                                                   # never an "unreadable" line: the caller exits 4
            bc.br.audit_for_state(target, now, deadline=bs._deadline_of(0.3))
        waited = time.monotonic() - t0
    assert 0.25 <= waited < 3
    assert bc.br.audit_for_state(target, now, deadline=bs._deadline_of(2)) == []                # the writer gone: nothing to say
    assert bc.br.audit_for_state(target, now) == []                                            # no deadline: as before

    def torn(*a, **k):
        raise bs.BoardError("torn beyond reading")

    monkeypatch.setattr(bc.br, "_load", torn)
    assert bc.br.audit_for_state(target, now) == ["registry: unreadable: torn beyond reading"]  # every other BoardError keeps its line


def test_run_state_hands_the_state_deadline_to_the_audit_and_a_busy_lock_there_is_exit_4(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    real = bc.br.audit_for_state
    seen: list = []

    def spy(*args, **kw):
        seen.append(kw.get("deadline"))
        return real(*args, **kw)

    monkeypatch.setattr(bc.br, "audit_for_state", spy)
    assert _state(capsys)[0] in (0, 3)
    assert len(seen) == 1 and isinstance(seen[0], float)                                       # the one state deadline, never None

    def busy(*args, **kw):
        raise bs.LockWaiting(bs.LOCK_WAITING)

    monkeypatch.setattr(bc.br, "audit_for_state", busy)
    rc, out, err = run(capsys, "state", "--json")
    assert rc == bc.EXIT_LOCKED and out == "" and "locked" in err


def test_an_explicit_store_dir_with_a_mistyped_items_file_name_is_refused_never_an_empty_board(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    _unpoint(store)
    (store / "itemz.jsonl").write_text("")
    rc, out, err = run(capsys, "state", "--json", "--items", str(store / "itemz.jsonl"))
    assert rc == 2 and out == "" and "refused" in err
    rc, out, err = run(capsys, "state", "--json", "--items", str(store / "nope.jsonl"))
    assert rc == 2 and out == ""


def _store_id_replaced_by(store, kind):
    sid = store / "store.id"
    sid.unlink()
    if kind == "dir":
        sid.mkdir()
    elif kind == "dangling":
        os.symlink(str(store / "nowhere"), sid)
    elif kind == "fifo":
        os.mkfifo(sid)


def test_a_store_id_that_is_not_a_regular_file_is_refused_never_a_plain_items_read(store, tmp_path, mkrepo, capsys, monkeypatch):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    _unpoint(store)
    monkeypatch.chdir(tmp_path / "alpha" if (tmp_path / "alpha").exists() else tmp_path)
    for kind in ("dir", "dangling", "fifo"):
        _store_id_replaced_by(store, kind)
        for given in (store, store / bs.ITEMS_NAME):
            rc, out, err = run(capsys, "state", "--json", "--items", str(given))              # a FIFO must not hang the read
            assert rc == 2 and out == "" and "store.id" in err, (kind, rc, err)
        if kind == "dir":
            (store / "store.id").rmdir()
        else:
            (store / "store.id").unlink()
        (store / "store.id").write_text("store-" + "a" * 32 + "\n")


                                            # no deadline: as before


def test_an_aborting_repos_cut_item_keeps_its_audit_line_until_abandon_done_levain93(store, tmp_path, mkrepo, capsys):
    _two_tracked(capsys, tmp_path, store, mkrepo)
    c = mkrepo("gamma")
    ok(capsys, "register", "gamma", "--path", str(c), "--words", "w", *A)
    ok(capsys, "begin-track", "gamma", "--manifest", str(manifest(tmp_path)), "--words", "y", *A)
    add_item(capsys, "gamma", migration_of(store, "gamma"), "a")
    ok(capsys, "abandon-track", "gamma", "--words", "never mind", *A)                         # aborting: the abort is not done
    rc, doc, err = _state(capsys)
    assert rc == 3, err
    assert any(e["why"].startswith("gamma: the repo is aborting") for e in doc["audit"] if e["id"] is None), doc["audit"]
    ok(capsys, "abandon-done", "gamma", *A)
    rc, doc, err = _state(capsys)
    assert _states(doc)["gamma#1"] == "cut"
    assert not any(e["why"].startswith("gamma:") for e in doc["audit"] if e["id"] is None), doc["audit"]
