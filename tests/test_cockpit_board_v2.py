"""T6 (levain#99): the cockpit board and the List read the v2 board (T3's ``levain board state``, every tracked repo of the
registry from one snapshot). Unknown is shown as its own column and never folded; off is a count; needs-you is still the band's."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from levain.cockpit import board, list_panel
from levain.desk import board_cli
from tests import _board_v2_seed as seed

NODE = shutil.which("node")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """One scratch board store for the module: two tracked repos, one untracked, items in every git-derivable state."""
    root = tmp_path_factory.mktemp("v2")
    mp = pytest.MonkeyPatch()
    mp.setenv("LEVAIN_HOME", str(root / "lhome"))
    mp.delenv("LEVAIN_LAUNCH_KEY", raising=False)
    ids = seed.seed(root)
    mp.setenv("LEVAIN_BOARD_ITEMS", str(root / "store"))
    yield ids
    mp.undo()


def _by_col(value):
    return {c["category"]: [k["id"] for k in c["cards"]] for c in value["columns"]}


def test_unknown_is_its_own_column_first_and_never_folded_into_another(store):
    v = board.build(None, None)
    assert v["queue"] == "ok" and v["unreadable"] == []
    cols = _by_col(v)
    assert [c["category"] for c in v["columns"]][0] == "unknown"
    assert cols["unknown"] == [store["unknown"]]
    assert all(store["unknown"] not in ids for c, ids in cols.items() if c != "unknown")      # folded nowhere
    (card,) = v["columns"][0]["cards"]
    assert card["status"] == "unknown" and "is not in the store" in card["held_reason"]      # and it says why


def test_every_tracked_repo_is_read_and_the_untracked_one_is_not(store):
    v = board.build(None, None)
    seen = {k["repo"] for c in v["columns"] for k in c["cards"] if k["id"] != "band"}
    assert seen == {"alpha", "beta"}                                                        # gamma is registered, never tracked
    cols = _by_col(v)
    assert store["done"] in cols["done"] and store["ready"] in cols["ready"] and store["landed"] in cols["ready"]
    assert store["groom"] in cols["backlog"] and store["blocked"] in cols["backlog"]


def test_off_is_a_count_and_never_a_column_of_cards(store):
    v = board.build(None, None)
    assert v["off_count"] == 3                                                              # cut, parked, out of 1.0
    assert "off" not in [c["category"] for c in v["columns"]]
    shown = {i for c in v["columns"] for i in (k["id"] for k in c["cards"])}
    assert not shown & {store["cut"], store["parked"], store["out"]}


def test_an_empty_unknown_column_is_left_out_and_the_rest_is_the_six(store, monkeypatch):
    doc = {"as_of": "2026-11-17T00:00:00Z", "items": [{"id": "a#1", "repo": "a", "title": "T", "tier": None, "state": "ready", "column": "ready",
                                                      "reason": "r", "refs": [], "as_of": None, "kind": "work"}]}
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None, read_evidence=None: (0, doc))
    v = board.build(None, None)
    assert [c["category"] for c in v["columns"]] == list(board.CATEGORIES) and v["off_count"] == 0


def test_every_state_lands_in_its_columns_and_needs_you_stays_the_bands(store, monkeypatch):
    from levain.desk.board_state import STATE_COLUMN

    items = [{"id": f"a#{n}", "repo": "a", "title": s, "tier": None, "state": s, "column": col, "reason": f"{s} reason", "refs": [], "as_of": None,
              "kind": None} for n, (s, col) in enumerate(STATE_COLUMN.items(), 1)]
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None, read_evidence=None: (3, {"as_of": None, "items": items}))
    v = board.build(None, None)
    cols = {c["category"]: [k["title"] for k in c["cards"]] for c in v["columns"]}
    assert cols["unknown"] == ["unknown"] and cols["running"] == ["in_flight"] and cols["waiting"] == ["landing"]
    assert cols["done"] == ["done"] and sorted(cols["ready"]) == ["landed_not_closed", "ready", "ready_with_start"]
    assert sorted(cols["backlog"]) == ["blocked", "needs_grooming"]
    assert cols["needs-you"] == []                                                          # a needs_you item is NOT drawn from the board
    assert v["off_count"] == 3 and sum(c["count"] for c in v["columns"]) == len(items) - 3 - 1


def test_the_needs_you_column_is_the_bands_alone(store, monkeypatch, tmp_path):
    from levain.consent.questions import QuestionStore

    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh2"))
    (tmp_path / "lh2" / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(board_cli, "state_doc", lambda args, say=None, read_evidence=None: (0, {"as_of": None, "items": [
        {"id": "a#1", "repo": "a", "title": "in the band", "tier": None, "state": "needs_you", "column": "needs_you", "reason": "band 1",
         "refs": [], "as_of": None, "kind": None}]}))
    v = board.build(None, QuestionStore(tmp_path / "lh2" / "consent"))
    assert _by_col(v)["needs-you"] == []                                                    # the board's needs_you item adds nothing here


def test_a_board_store_that_cannot_be_read_is_named_not_shown_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    (tmp_path / "lh" / "board").mkdir(parents=True)
    (tmp_path / "lh" / "board" / "STORE").write_text(str(tmp_path / "gone") + "\n")                  # the pointer names a store that is not there
    v = board.build(None, None)
    assert v["queue"] == "unreadable" and "desk" in v["unreadable"]
    assert "unknown" not in [c["category"] for c in v["columns"]]


def test_with_no_board_store_the_dispatch_queue_is_read_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    v = board.build(None, None)
    assert v["queue"] == "none" and v["off_count"] == 0 and [c["category"] for c in v["columns"]] == list(board.CATEGORIES)


def test_the_list_says_unknown_and_never_folds_it_into_the_ready_rows(store):
    out = list_panel._board_part(None, None, date(2026, 11, 17), datetime(2026, 11, 17, tzinfo=timezone.utc))
    ids = {r.id for r in out.rows}
    assert f"board:{store['ready']}" in ids and f"board:{store['unknown']}" not in ids
    assert any("read unknown" in why for _n, why in out.skipped)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_draws_the_unknown_column_while_it_holds_a_card_and_the_off_count():
    root = Path(__file__).resolve().parent
    res = subprocess.run([NODE, str(root / "js_board_unknown_harness.js"), str(root.parent / "levain/templates/web/board_view.js")],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["with"]["heads"][:2] == ["unknown 1", "backlog 0"] and "ready 1" in got["with"]["heads"] and "3 off the board (cut, parked or out of 1.0)." in got["with"]["notes"][0]
    assert got["with"]["held"] == ["held: item:a#9 is not in the store"]
    assert "unknown" not in " ".join(got["without"]["heads"]) and "ready 1" in got["without"]["heads"] and got["without"]["notes"] == []


# ---- the board config: `levain board use --producer/--no-land-waiter` writes it, `board state` and the cockpit read it ----

import argparse
import os

from levain.desk import board_evidence, board_producers, board_store


def _args(items, **kw):
    base = dict(json=True, items=str(items), evidence_extra=None, producers=[], land_waiter=None, repo=None, repo_name=None,
                also_repo=[], since=board_evidence.BOARD_EPOCH, now=None)
    return argparse.Namespace(**{**base, **kw})


def _unknown_ids(doc):
    return sorted(i["id"] for i in doc["items"] if i["state"] == "unknown")


@pytest.fixture()
def bare_copy(store, tmp_path):
    """The module's store copied with NO board config (its registry still names the same repos): today's flagless install."""
    src = Path(os.environ["LEVAIN_BOARD_ITEMS"])
    dst = tmp_path / "noconf"
    shutil.copytree(src, dst)
    (dst / board_store.CONFIG_NAME).unlink()
    return dst


def test_use_writes_the_config_atomically_and_a_second_use_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    d = tmp_path / "s"
    assert seed.board("use", str(d), "--producer", "flow", "--producer", "ci", "--no-land-waiter").startswith("using")
    assert board_store.read_config(d) == {"producers": ["flow", "ci"], "no_land_waiter": True}
    assert seed.board("use", str(d), "--producer", "flow", "--producer", "ci", "--no-land-waiter").startswith("already using")
    assert [p.name for p in d.iterdir() if p.name.startswith(".")] == []                  # no temp file left behind
    seed.board("use", str(d), "--producer", "flow")                                        # the config is EXACTLY the flags: no_land_waiter off
    assert board_store.read_config(d) == {"producers": ["flow"], "no_land_waiter": False}
    seed.board("use", str(d))                                                              # no flag is the default: `use` clears the config
    assert board_store.read_config(d) == {"producers": [], "no_land_waiter": False}
    fresh = tmp_path / "fresh"
    seed.board("use", str(fresh))                                                          # no flag and no config: nothing is written
    assert not (fresh / board_store.CONFIG_NAME).exists()


def test_a_store_with_no_config_reads_as_the_flagless_cli(tmp_path):
    assert board_store.read_config(tmp_path) == {"producers": [], "no_land_waiter": False}


def test_a_config_that_is_not_a_config_is_refused_not_defaulted(tmp_path):
    (tmp_path / board_store.CONFIG_NAME).write_text("{not json")
    with pytest.raises(board_store.BoardError):
        board_store.read_config(tmp_path)
    (tmp_path / board_store.CONFIG_NAME).write_text(json.dumps({"config_v": 1, "producers": [3], "no_land_waiter": True}))
    with pytest.raises(board_store.BoardError):
        board_store.read_config(tmp_path)


def test_state_reads_the_config_and_the_flag_adds_nothing_the_config_did_not_say(store, bare_copy):
    items = os.environ["LEVAIN_BOARD_ITEMS"]
    rc, from_config = board_cli.state_doc(_args(items))
    rc2, from_flag = board_cli.state_doc(_args(bare_copy, land_waiter=False))
    rc3, none = board_cli.state_doc(_args(bare_copy))
    assert rc in (0, board_cli.EXIT_AUDIT) and rc2 in (0, board_cli.EXIT_AUDIT) and from_config is not None and from_flag is not None and none is not None
    assert _unknown_ids(from_config) == _unknown_ids(from_flag)                             # config == the flag it stands for
    assert _unknown_ids(none) != _unknown_ids(from_config)                                  # no config: waiters unknown, as today's CLI
    assert len(_unknown_ids(none)) > len(_unknown_ids(from_config))


def test_the_cockpit_reads_the_same_config_the_same_way(store, bare_copy):
    items = os.environ["LEVAIN_BOARD_ITEMS"]
    _rc, doc = board_cli.state_doc(_args(items))
    v = board.build(None, None)
    unk = [k["id"] for c in v["columns"] if c["category"] == "unknown" for k in c["cards"]]
    assert sorted(unk) == _unknown_ids(doc)
    bare = board.build(None, None, board_items=bare_copy)
    assert len([k for c in bare["columns"] if c["category"] == "unknown" for k in c["cards"]]) > len(unk)


def test_flags_override_the_config(store, monkeypatch):
    items = os.environ["LEVAIN_BOARD_ITEMS"]
    board_store.write_config(items, ["from-config"], True)
    seen = []
    real = board_producers.read_external
    monkeypatch.setattr(board_producers, "read_external", lambda d, names: (seen.append(list(names)), real(d, names))[1])
    board_cli.state_doc(_args(items))
    board_cli.state_doc(_args(items, producers=["from-flag"]))
    assert seen == [["from-config"], ["from-flag"]]
    board_store.write_config(items, [], True)


@pytest.mark.parametrize("saved_producers, saved_nlw, flag_producers, flag_land, want_producers, want_nlw", [
    ([], False, [], None, [], False),                      # nothing saved, nothing given: the default
    (["c"], True, [], None, ["c"], True),                  # nothing given: the config stands, both fields
    (["c"], True, ["f"], None, ["f"], True),               # producers given replace; the land waiter stays the config's
    (["c"], True, [], True, ["c"], False),                 # --land-waiter turns a saved no_land_waiter OFF for the run
    (["c"], False, [], False, ["c"], True),                # --no-land-waiter turns it on
    (["c"], True, ["f"], False, ["f"], True),              # both given: both replace
    ([], False, ["f"], True, ["f"], False),
])
def test_the_flag_given_overrides_the_config_and_a_flag_left_off_leaves_it(store, bare_copy, monkeypatch, saved_producers, saved_nlw,
                                                                          flag_producers, flag_land, want_producers, want_nlw):
    board_store.write_config(bare_copy, saved_producers, saved_nlw)
    seen = {}
    real_native = board_producers.native_blocks
    monkeypatch.setattr(board_producers, "native_blocks", lambda **kw: (seen.setdefault("nlw", kw["no_land_waiter"]), real_native(**kw))[1])
    monkeypatch.setattr(board_producers, "read_external", lambda d, names: (seen.setdefault("producers", list(names)), ({}, {}))[1])
    board_cli.state_doc(_args(bare_copy, producers=flag_producers, land_waiter=flag_land))
    assert seen == {"producers": want_producers, "nlw": want_nlw}


def test_the_state_parser_takes_land_waiter_and_no_land_waiter_with_none_as_the_default():
    top = argparse.ArgumentParser()
    board_cli.register(top.add_subparsers(dest="cmd"))
    assert top.parse_args(["board", "state"]).land_waiter is None
    assert top.parse_args(["board", "state", "--land-waiter"]).land_waiter is True
    assert top.parse_args(["board", "state", "--no-land-waiter"]).land_waiter is False


def test_a_broken_config_makes_the_board_unreadable_never_the_default(store, bare_copy):
    (bare_copy / board_store.CONFIG_NAME).write_text("garbage")
    said = []
    rc, doc = board_cli.state_doc(_args(bare_copy), said.append)
    assert rc != 0 and doc is None and "board config" in said[0]
    v = board.build(None, None, board_items=bare_copy)
    assert v["queue"] == "unreadable" and "desk" in v["unreadable"]


def test_the_evidence_is_kept_while_the_refs_are_the_same_and_read_again_when_one_moves(store, monkeypatch):
    board._EVIDENCE_CACHE.clear()
    calls = []
    real = board_evidence.read_git_evidence
    monkeypatch.setattr(board_evidence, "read_git_evidence", lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])
    first = board.build(None, None)
    n = len(calls)
    assert n == 2                                                                          # alpha, beta
    again = board.build(None, None)
    assert len(calls) == n and _by_col(again) == _by_col(first)                            # refs unmoved: nothing re-read
    repo = Path(calls[0])
    seed.git(repo, "commit", "-q", "--allow-empty", "-m", "a ref moves")                   # the checked-out branch's ref moves
    board.build(None, None)
    assert len(calls) == n + 1 and Path(calls[-1]) == repo                                 # that repo alone is read again


# ---- the evidence cache: never a degraded read, a complete key, one build at a time; the patch-id memo ----

import threading
import time


def _one_repo(tmp_path):
    repo = seed.mkrepo(tmp_path, "solo")
    return repo


def test_a_degraded_read_is_never_served_from_the_cache_once_git_recovers(tmp_path, monkeypatch):
    board._EVIDENCE_CACHE.clear()
    repo = _one_repo(tmp_path)
    real = board_evidence.read_git_evidence
    seen = []

    def flaky(*a, **k):
        ev, summary = real(*a, **k)
        if not seen:
            ev = {**ev, "unreadable": {"main": "git log failed: timed out"}}
        seen.append(1)
        return ev, summary

    monkeypatch.setattr(board_evidence, "read_git_evidence", flaky)
    first, _ = board._cached_read_evidence(repo, repo_name="solo", since=board_evidence.BOARD_EPOCH)
    assert first["unreadable"]                                                             # the degraded read is handed out once ...
    second, _ = board._cached_read_evidence(repo, repo_name="solo", since=board_evidence.BOARD_EPOCH)
    assert len(seen) == 2 and not second.get("unreadable")                                 # ... refs unchanged, and git is asked again
    third, _ = board._cached_read_evidence(repo, repo_name="solo", since=board_evidence.BOARD_EPOCH)
    assert len(seen) == 2 and not third.get("unreadable")                                  # a full read IS kept


def _counting(monkeypatch):
    calls = []
    real = board_evidence.read_git_evidence
    monkeypatch.setattr(board_evidence, "read_git_evidence", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    return calls


def test_a_fetch_head_change_reads_again_and_so_does_a_different_window(tmp_path, monkeypatch):
    board._EVIDENCE_CACHE.clear()
    repo = _one_repo(tmp_path)
    calls = _counting(monkeypatch)
    board._cached_read_evidence(repo, repo_name="solo", since=board_evidence.BOARD_EPOCH)
    board._cached_read_evidence(repo, repo_name="solo", since=board_evidence.BOARD_EPOCH)
    assert len(calls) == 1
    board._cached_read_evidence(repo, repo_name="solo", since="2026-11-14T00:00:00Z")      # `since` is part of the key
    assert len(calls) == 2
    fh = repo / ".git" / "FETCH_HEAD"
    fh.write_text("0" * 40 + "\t\tbranch 'main' of nowhere\n")                            # a fetch wrote FETCH_HEAD: no ref moved
    board._cached_read_evidence(repo, repo_name="solo", since="2026-11-14T00:00:00Z")
    assert len(calls) == 3
    board._cached_read_evidence(repo, repo_name="solo", since="2026-11-14T00:00:00Z")
    assert len(calls) == 3


@pytest.mark.parametrize("what", ["config.worktree", "alternates", "config"])
def test_every_file_git_reads_for_the_repo_is_in_the_key(tmp_path, monkeypatch, what):
    board._EVIDENCE_CACHE.clear()
    repo = _one_repo(tmp_path)
    calls = _counting(monkeypatch)
    board._cached_read_evidence(repo, repo_name="solo")
    board._cached_read_evidence(repo, repo_name="solo")
    assert len(calls) == 1
    git = repo / ".git"
    if what == "config.worktree":
        (git / "config.worktree").write_text("[diff]\n\trenames = false\n")
    elif what == "alternates":
        (git / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (git / "objects" / "info" / "alternates").write_text(str(tmp_path / "nowhere") + "\n")
    else:
        seed.git(repo, "config", "diff.algorithm", "patience")
    board._cached_read_evidence(repo, repo_name="solo")
    assert len(calls) == 2


def test_the_global_git_config_is_in_the_key(tmp_path, monkeypatch):
    board._EVIDENCE_CACHE.clear()
    repo = _one_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    glob = home / ".gitconfig"
    glob.write_text("[diff]\n\trenames = true\n")
    monkeypatch.setenv("HOME", str(home))          # git_env drops every GIT_* variable, so the global file is found by HOME
    calls = _counting(monkeypatch)
    board._cached_read_evidence(repo, repo_name="solo")
    glob.write_text("[diff]\n\trenames = false\n[url \"x\"]\n\tinsteadOf = y\n")
    board._cached_read_evidence(repo, repo_name="solo")
    assert len(calls) == 2


def test_a_second_build_during_a_build_waits_for_it_and_takes_its_result(tmp_path, monkeypatch):
    board._EVIDENCE_CACHE.clear()
    repo = _one_repo(tmp_path)
    real = board_evidence.read_git_evidence
    started, release, calls = threading.Event(), threading.Event(), []

    def slow(*a, **k):
        calls.append(1)
        started.set()
        release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(board_evidence, "read_git_evidence", slow)
    out = []
    t1 = threading.Thread(target=lambda: out.append(board._cached_read_evidence(repo, repo_name="solo")))
    t1.start()
    assert started.wait(10)
    t2 = threading.Thread(target=lambda: out.append(board._cached_read_evidence(repo, repo_name="solo")))
    t2.start()
    time.sleep(0.3)
    assert len(calls) == 1                                                                  # the second one is waiting, not building
    release.set()
    t1.join(30)
    t2.join(30)
    assert len(calls) == 1 and len(out) == 2 and out[0][0]["main"] == out[1][0]["main"]


def test_a_build_past_the_wait_answers_the_last_read_stale_or_building_and_the_build_runs_on():
    flight = board.Flight()
    gate = threading.Event()
    good = {"schema": 1, "queue": "ok", "unreadable": [], "note": None, "columns": []}
    assert flight.get(lambda: dict(good), 5)["queue"] == "ok"
    started = threading.Event()

    def slow():
        started.set()
        gate.wait(10)
        return {**good, "generated": "later"}

    late = flight.get(slow, 0.2)                                                            # past the wait: the last good read, marked
    assert late.get("stale") is True and "last read" in late["note"] and late["queue"] == "ok"
    again = flight.get(lambda: pytest.fail("a second build must not start"), 0.05)         # still the same build
    assert again.get("stale") is True
    gate.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        got = flight.get(lambda: {**good, "generated": "fresh"}, 5)
        if got.get("generated") in ("later", "fresh"):
            break
    assert got.get("stale") is None


def test_the_first_build_past_the_wait_says_it_is_building_in_a_board_the_kernel_accepts():
    from levain.cockpit import engine

    flight = board.Flight()
    gate = threading.Event()
    v = flight.get(lambda: (gate.wait(10), {})[1], 0.1)
    assert v["building"] is True and v["unreadable"] == ["building"] and v["queue"] == "unreadable"
    engine._v_board(type("S", (), {"id": "board", "kind": "board"})(), v)                   # raises when the board is refused
    gate.set()


def test_the_done_column_is_capped_at_the_kernels_card_limit_and_says_how_many_more(monkeypatch):
    from levain.cockpit import engine

    assert board.CARDS_MAX == engine.BOARD_CARDS_MAX
    monkeypatch.setattr(board, "CARDS_MAX", 5)
    cards = [{"id": f"x#{i}", "repo": "x", "title": "t", "tier": None, "status": "done", "status_category": "done", "kind": None,
              "reason": None, "journal": None} for i in range(9)]
    monkeypatch.setattr(board, "_v2", lambda items, now=None: (cards, 0, [], None))
    v = board.build(None, None)
    (done,) = [c for c in v["columns"] if c["category"] == "done"]
    assert len(done["cards"]) == 5 and done["count"] == 9 and "[4 more]" in v["note"]


def test_a_broken_config_is_refused_before_any_git_read(store, bare_copy, monkeypatch):
    (bare_copy / board_store.CONFIG_NAME).write_text("garbage")
    reads = []
    rc, doc = board_cli.state_doc(_args(bare_copy), lambda t: None, lambda *a, **k: reads.append(1))
    assert doc is None and reads == []


def test_a_commits_patch_id_is_kept_so_a_miss_diffs_only_new_commits(tmp_path, monkeypatch):
    board_evidence._PID_MEMO.clear()
    repo = _one_repo(tmp_path)
    seed.git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    for i in range(3):
        (repo / f"f{i}.txt").write_text(f"{i}\n")
        seed.git(repo, "add", ".")
        seed.git(repo, "commit", "-q", "-m", f"c{i}")
    shas = seed.git(repo, "rev-list", "HEAD").split()
    first = board_evidence._patch_ids(repo, shas)
    runs = []
    real = board_evidence._git
    monkeypatch.setattr(board_evidence, "_git", lambda r, *a, **k: (runs.append((a[0], k.get("stdin"))), real(r, *a, **k))[1])
    assert board_evidence._patch_ids(repo, shas) == first
    assert [a for a, _ in runs if a == "diff-tree"] == []                                  # nothing new: no diff-tree at all
    (repo / "g.txt").write_text("new\n")
    seed.git(repo, "add", ".")
    seed.git(repo, "commit", "-q", "-m", "new")
    new = seed.git(repo, "rev-parse", "HEAD")
    runs.clear()
    got = board_evidence._patch_ids(repo, [new, *shas])
    (stdin,) = [st for a, st in runs if a == "diff-tree"]
    assert stdin.decode().split() == [new]                                                 # only the unseen commit is diffed
    assert {s: got.get(s) for s in shas} == {s: first.get(s) for s in shas} and new in got
    seed.git(repo, "config", "diff.algorithm", "patience")                                  # the config stamp moved: the memo is dropped
    runs.clear()
    board_evidence._patch_ids(repo, shas)
    assert [st for a, st in runs if a == "diff-tree"] and len(runs) >= 2
