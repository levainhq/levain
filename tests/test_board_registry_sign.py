"""P2 wired into the registry (levain#96, levain#88 slice): every ``repos.jsonl`` write verb goes through the fixed 1.0 policy.

Each test was written after the path was run once by hand (scratch store, scratch git repo, the CLI). A REAL Touch ID
signature cannot run in a test: the seal is ``board_sign.terminal_seal`` over a THROWAWAY ed25519 key with a touch-id sidecar,
its authority log a hand-built snapshot that enrols the key (``tests.test_board_sign.Rig``, the seam P2's own tests use). The
signer is the real ``ssh_signer``; the grade is earned by ``admit``'s replay, never by the label.

This module does NOT use test_board_registry.py's autouse seam (which seals the decision-class verbs for that file's rules
tests): here the policy itself is the subject."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from levain.desk import board_registry as br
from levain.desk import board_sign as sg
from levain.desk import board_store as bs
from tests.test_board_registry import (A, HASH_A, bare_store, focus_file, git, home, land, manifest, mkrepo,  # noqa: F401 (fixtures)
                                       reg_lines, run, store)
from tests.test_board_sign import Rig

OP = {"kind": "operator", "name": "desk 1"}
AGENT = {"kind": "agent", "name": "seat-1"}
NOW = "2026-11-17T12:00:00Z"
DECISION_VERBS = ("begin-track", "track", "abandon-track", "untrack")


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


@pytest.fixture
def tgt(store) -> bs.Target:
    return bs.resolve_store(None)


def raw(store) -> bytes:
    return (store / "repos.jsonl").read_bytes()


def items_raw(store) -> bytes:
    p = store / "items.jsonl"
    return p.read_bytes() if p.exists() else b""


def registered(tgt, mkrepo, name="alpha"):
    repo = mkrepo(name)
    br.register(tgt, name, str(repo), "first", "desk 1", author=OP)
    return repo


def tracking(tgt, tmp_path, mkrepo, rig, name="alpha", plan=("a",)):
    repo = registered(tgt, mkrepo, name)
    br.begin_track(tgt, name, manifest(tmp_path, plan_items=list(plan)), "yes", "desk 1", author=OP, seal=rig.seal())
    return repo, br.read_registry(tgt).fold.repos[name].migration


def keyed(tgt, repo, key):
    return bs.add(tgt, repo, "an item", "desk 1", "desk 1", author=OP, now=NOW, key=key,
                  payload_hash=hashlib.sha256(key.encode()).hexdigest())[0]


def tracked(tgt, tmp_path, mkrepo, rig, name="alpha"):
    """``name`` is tracking with its one item and a matching charter landed on origin/main: ready for ``track``."""
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, name=name)
    iid = keyed(tgt, name, f"{m}:a")
    land(repo, "CHARTER.md", f"# Charter: {name}\nCharter-Version: 1\nExport: repo\n\n## Purpose\nT.\n\n## Criteria\n- C1: ok [judged: phill]\n\n"
                              f"## Constraints\n- K1: none\n\n## Plan\n- {iid}\n")
    return repo, m, iid


# --------------------------------------------------------------------------- decision-class verbs refuse without a grade


def test_every_decision_class_registry_verb_is_refused_unsigned_and_writes_nothing(tgt, store, tmp_path, mkrepo, rig):
    repo, m, iid = tracked(tgt, tmp_path, mkrepo, rig)
    other = registered(tgt, mkrepo, "beta")
    # begin-track on beta
    before = raw(store)
    with pytest.raises(bs.BoardError, match="decision-class write: it needs --sign"):
        br.begin_track(tgt, "beta", manifest(tmp_path), "y", "desk 1", author=OP)
    # track on alpha (tracking, charter landed)
    with pytest.raises(bs.BoardError, match="decision-class write: it needs --sign"):
        br.track(tgt, "alpha", "go", "desk 1", author=OP)
    assert raw(store) == before
    br.track(tgt, "alpha", "go", "desk 1", author=OP, seal=rig.seal())
    bs.decide(tgt, iid, "parked", "later", "desk 1", author=OP, now=NOW, seal=rig.seal())          # untrack needs every item settled
    before = raw(store)
    with pytest.raises(bs.BoardError, match="decision-class write: it needs --sign"):
        br.untrack(tgt, "alpha", "done with it", "desk 1", author=OP)
    assert raw(store) == before
    # abandon-track on gamma (tracking)
    tracking(tgt, tmp_path, mkrepo, rig, name="gamma")
    before, items_before = raw(store), items_raw(store)
    with pytest.raises(bs.BoardError, match="decision-class write: it needs --sign"):
        br.abandon_track(tgt, "gamma", "no", "desk 1", author=OP)
    assert raw(store) == before and items_raw(store) == items_before
    assert other.exists()


def test_a_signed_decision_class_record_is_graded_signed_terminal_and_verifies(tgt, store, tmp_path, mkrepo, rig):
    repo, m, iid = tracked(tgt, tmp_path, mkrepo, rig)
    br.track(tgt, "alpha", "go", "desk 1", author=OP, seal=rig.seal())
    bs.decide(tgt, iid, "parked", "later", "desk 1", author=OP, now=NOW, seal=rig.seal())
    br.untrack(tgt, "alpha", "leaving", "desk 1", author=OP, seal=rig.seal())
    recs = reg_lines(store)
    by_op = {r["op"]: r for r in recs}
    for op in ("begin-track", "track", "untrack"):
        r = by_op[op]
        assert r["grade"] == "signed-terminal" and list(r)[-1] == "sig" and r["author"]["kind"] == "operator"
        assert r["repo"] == "alpha"                                  # the cause check reads repo and op as named
        assert sg.verify_record(r, rig.snapshot()).state == "valid"
    assert [r["op"] for r in recs] == ["register", "begin-track", "track", "untrack"]


def test_a_signed_registry_record_by_a_key_the_log_does_not_enrol_writes_nothing(tgt, store, tmp_path, mkrepo, rig):
    registered(tgt, mkrepo)
    stranger = Rig(tmp_path / "other")
    before = raw(store)
    seal = rig.seal(authority=stranger.source())                       # signs with rig's key; the log holds another key only
    with pytest.raises(bs.BoardError, match="not written"):
        br.begin_track(tgt, "alpha", manifest(tmp_path), "y", "desk 1", author=OP, seal=seal)
    assert raw(store) == before


def test_a_signed_registry_write_needs_an_operator_author(tgt, store, tmp_path, mkrepo, rig):
    registered(tgt, mkrepo)
    before = raw(store)
    with pytest.raises(bs.BoardError, match="author kind must be operator"):
        br.begin_track(tgt, "alpha", manifest(tmp_path), "y", "seat-1", author=AGENT, seal=rig.seal())
    assert raw(store) == before


# ------------------------------------------------------------------------------- asserted is enough where the policy says


def test_the_open_registry_verbs_are_accepted_asserted(tgt, store, tmp_path, mkrepo, rig, capsys):
    repo = registered(tgt, mkrepo)
    moved = tmp_path / "moved"
    git(tmp_path, "clone", "-q", str(repo), str(moved))                  # the same repository (same root commits), another path
    br.move(tgt, "alpha", str(moved), "moved it", "desk 1", author=OP)
    br.pause(tgt, "alpha", "later", "desk 1", author=OP)
    br.resume(tgt, "alpha", "now", "desk 1", author=OP)
    with open(store / "repos.jsonl", "ab") as f:
        f.write(b"garbage\n")
    n = len(reg_lines_tolerant(store))
    assert br.ack_torn(tgt, "repos.jsonl", n, "test", "desk 1", author=OP)
    recs = reg_lines_tolerant(store)
    assert [r["op"] for r in recs if r["op"] != "?"] == ["register", "move", "pause", "resume", "ack-torn"]
    assert all(r.get("grade") == "asserted" and "sig" not in r for r in recs if r["op"] != "?")


def reg_lines_tolerant(store):
    out = []
    for ln in (store / "repos.jsonl").read_text("utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append({"op": "?"})
    return out


def test_abandon_done_is_asserted_ok(tgt, store, tmp_path, mkrepo, rig):
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=())
    br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    br.abandon_done(tgt, "alpha", "", "desk 1", author=OP)
    last = reg_lines(store)[-1]
    assert last["op"] == "abandon-done" and last["grade"] == "asserted" and "sig" not in last


def test_the_launch_hook_and_import_focus_register_and_pause_as_asserted(home, tmp_path, mkrepo, capsys, monkeypatch):
    for n in ("alpha", "beta"):
        mkrepo(n)
    focus = focus_file(tmp_path, {"alpha": ("active", "wa"), "beta": ("paused", "wb")})
    store = tmp_path / "store"
    rc, out, err = run(capsys, "use", str(store), "--import-focus", str(focus), "--briefcase", str(tmp_path / "repos"), "--by", "desk 1")
    assert rc == 0, err
    recs = reg_lines(Path(os.path.realpath(store)))
    assert [r["op"] for r in recs] == ["register", "register", "pause"] and {r["grade"] for r in recs} == {"asserted"}
    gamma = mkrepo("gamma")                                              # the launch hook itself (L1 6a): its register is asserted
    assert br.register_for_launch(gamma, os.environ) is True
    last = reg_lines(Path(os.path.realpath(store)))[-1]
    assert last["op"] == "register" and last["repo"] == "gamma" and last["grade"] == "asserted" and "sig" not in last
    assert last["author"]["kind"] == "agent"


def test_use_import_focus_refuses_sign_there_is_nothing_to_sign(home, tmp_path, mkrepo, capsys):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    rc, out, err = run(capsys, "use", str(tmp_path / "store"), "--import-focus", str(focus), "--author-kind", "operator", "--sign")
    assert rc == 2 and "--sign does not apply" in err and focus.read_text().count("moved_to") == 0
    assert not (tmp_path / "store" / "repos.jsonl").exists()


def test_a_plain_use_with_sign_has_its_own_refusal_nothing_to_sign(home, tmp_path, capsys):
    rc, out, err = run(capsys, "use", str(tmp_path / "store"), "--sign")
    assert rc == 2 and "use writes no record" in err and "--import-focus" not in err
    assert not (tmp_path / "store").exists()


# ---------------------------------------------------------------------------------------------- the CLI and --sign


def test_the_cli_refuses_each_decision_class_verb_without_sign_naming_both_ways(store, tmp_path, mkrepo, rig, capsys):
    tgt = bs.resolve_store(None)
    _, _, iid = tracked(tgt, tmp_path, mkrepo, rig)                          # alpha: tracking, ready for track
    tracked(tgt, tmp_path, mkrepo, rig, name="delta")
    br.track(tgt, "delta", "go", "desk 1", author=OP, seal=rig.seal())      # delta: tracked, its item settled: ready for untrack
    bs.decide(tgt, "delta#1", "parked", "later", "desk 1", author=OP, now=NOW, seal=rig.seal())
    tracking(tgt, tmp_path, mkrepo, rig, name="gamma")                       # gamma: tracking, ready for abandon-track
    registered(tgt, mkrepo, "beta")
    before = raw(store)
    mf = str(manifest(tmp_path))
    for argv in (("begin-track", "beta", "--manifest", mf, "--words", "y"), ("track", "alpha", "--words", "go"),
                 ("abandon-track", "gamma", "--words", "no"), ("untrack", "delta", "--words", "no")):
        rc, _, err = run(capsys, *argv, *A)
        assert rc == 2 and "--sign" in err and "--source" in err, (argv, err)
    assert raw(store) == before


def test_sign_with_no_authority_log_is_refused_before_any_write_on_every_registry_verb(store, tmp_path, mkrepo, rig, capsys):
    tgt = bs.resolve_store(None)
    repo = tracked(tgt, tmp_path, mkrepo, rig)[0]
    before = raw(store)
    mf = str(manifest(tmp_path))
    argvs = (("register", "zeta", "--path", str(repo), "--words", "w"), ("move", "alpha", "--path", str(repo), "--words", "w"),
             ("begin-track", "alpha", "--manifest", mf, "--words", "y"), ("track", "alpha", "--words", "go"),
             ("abandon-track", "alpha", "--words", "no"), ("abandon-done", "alpha"), ("untrack", "alpha", "--words", "no"),
             ("pause", "alpha", "--words", "w"), ("resume", "alpha", "--words", "w"))
    for argv in argvs:
        rc, _, err = run(capsys, *argv, *A, "--sign")
        assert rc == 2 and "nobody can verify" in err, (argv, err)
    rc, _, err = run(capsys, "ack-torn", "--file", "repos.jsonl", "--line", "1", "--why", "x", *A, "--sign")
    assert rc == 2 and "nobody can verify" in err
    assert raw(store) == before


def test_sign_by_an_agent_author_is_refused_before_any_prompt(store, tmp_path, mkrepo, rig, capsys):
    tgt = bs.resolve_store(None)
    registered(tgt, mkrepo)
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", "--by", "d", "--sign")
    assert rc == 2 and "--author-kind operator" in err


def test_the_cli_sign_writes_a_signed_begin_track_and_track_through_the_seal(store, tmp_path, mkrepo, rig, capsys, monkeypatch):
    tgt = bs.resolve_store(None)
    repo = registered(tgt, mkrepo)
    made = rig.seal()                        # before the patch: rig.seal() itself calls terminal_seal
    monkeypatch.setattr(sg, "terminal_seal", lambda **kw: made)
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
    rc, out, err = run(capsys, "begin-track", "alpha", "--manifest", str(manifest(tmp_path)), "--words", "y", *A, "--sign")
    assert rc == 0, err
    r = reg_lines(store)[-1]
    assert r["op"] == "begin-track" and r["grade"] == "signed-terminal"
    assert sg.verify_record(r, rig.snapshot()).state == "valid"
    m = r["migration"]
    iid = keyed(tgt, "alpha", f"{m}:a")
    land(repo, "CHARTER.md", f"# Charter: alpha\nCharter-Version: 1\nExport: repo\n\n## Purpose\nT.\n\n## Criteria\n- C1: ok [judged: phill]\n\n"
                              f"## Constraints\n- K1: none\n\n## Plan\n- {iid}\n")
    rc, out, err = run(capsys, "track", "alpha", "--words", "go", *A, "--sign")
    assert rc == 0, err
    assert reg_lines(store)[-1]["grade"] == "signed-terminal"


# ---------------------------------------------------------------------------- abandon-track's cuts carry the cause (P2-d)


def abandon_line(store) -> str:
    return [ln for ln in raw(store).decode().splitlines() if '"op":"abandon-track"' in ln][-1]


def test_abandon_track_cuts_carry_the_cause_of_the_signed_abandon_and_the_cuts_are_asserted(tgt, store, tmp_path, mkrepo, rig):
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a", "b"))
    ids = [keyed(tgt, "alpha", f"{m}:a"), keyed(tgt, "alpha", f"{m}:b")]
    _, cut = br.abandon_track(tgt, "alpha", "changed his mind", "desk 1", author=OP, seal=rig.seal())
    assert cut == ids
    abandon = [r for r in reg_lines(store) if r["op"] == "abandon-track"][0]
    assert abandon["grade"] == "signed-terminal" and abandon["migration"] == m and abandon["repo"] == "alpha"
    cause = hashlib.sha256(abandon_line(store).encode()).hexdigest()
    cuts = [json.loads(x) for x in items_raw(store).decode().splitlines()][-2:]
    assert [c["id"] for c in cuts] == ids
    assert all(c["op"] == "decide" and c["decision"] == "cut" and c["cause"] == cause and c["grade"] == "asserted" for c in cuts)


def test_a_cut_is_bound_to_the_abandons_migration_and_the_item_keyed_to_it_not_the_repo(tgt, store, tmp_path, mkrepo, rig):
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    _, m2 = tracking(tgt, tmp_path, mkrepo, rig, name="beta")
    stray = keyed(tgt, "beta", f"{m2}:a")                                # another repo's ceremony, another migration
    br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    cause = hashlib.sha256(abandon_line(store).encode()).hexdigest()
    before = items_raw(store)
    with pytest.raises(bs.BoardError, match="is not keyed to it"):
        bs.decide(tgt, stray, "cut", "x", "seat-1", author=AGENT, now=NOW, cause=cause, authority=rig.source())
    assert items_raw(store) == before


def test_a_cut_citing_an_asserted_abandon_track_is_refused(tgt, store, tmp_path, mkrepo, rig):
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    iid = keyed(tgt, "alpha", f"{m}:a")
    forged = {"v": 1, "store": bs.read_store_id(store), "op": "abandon-track", "repo": "alpha", "at": NOW,
              "prev": bs.chain_prev(raw(store)), "by": "x", "words": "w", "migration": m, "author": OP, "grade": "asserted"}
    line = bs.canonical_bytes(forged)
    with open(store / "repos.jsonl", "ab") as f:
        f.write(line + b"\n")
    with pytest.raises(bs.BoardError, match="must be signed"):
        bs.decide(tgt, iid, "cut", "x", "seat-1", author=AGENT, now=NOW, cause=hashlib.sha256(line).hexdigest(), authority=rig.source())


def test_an_unsigned_abandon_track_cuts_nothing(tgt, store, tmp_path, mkrepo, rig):
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    keyed(tgt, "alpha", f"{m}:a")
    before, items_before = raw(store), items_raw(store)
    with pytest.raises(bs.BoardError, match="decision-class write"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=rig.source())     # an authority source does not make it signed
    assert raw(store) == before and items_raw(store) == items_before


def test_a_rerun_that_finishes_cuts_needs_an_authority_source_and_an_unsigned_cut_without_one_is_refused(tgt, store, tmp_path, mkrepo, rig, monkeypatch):
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a", "b"))
    ids = [keyed(tgt, "alpha", f"{m}:a"), keyed(tgt, "alpha", f"{m}:b")]
    real = bs.decide
    calls = []

    def crash_after_one(*args, **kw):
        if calls:
            raise bs.BoardError("simulated crash")
        calls.append(args[1])
        return real(*args, **kw)

    monkeypatch.setattr(bs, "decide", crash_after_one)
    with pytest.raises(bs.BoardError, match="simulated crash"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    monkeypatch.setattr(bs, "decide", real)
    n_reg = len(reg_lines(store))
    with pytest.raises(bs.BoardError, match="no authority log"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP)                               # aborting: no new abandon, cuts need a source
    assert len(reg_lines(store)) == n_reg
    _, cut = br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=rig.source())
    assert cut == [ids[1]] and len(reg_lines(store)) == n_reg


def test_the_cli_abandon_track_sign_cuts_with_the_cause(store, tmp_path, mkrepo, rig, capsys, monkeypatch):
    tgt = bs.resolve_store(None)
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    iid = keyed(tgt, "alpha", f"{m}:a")
    made = rig.seal()                        # before the patch: rig.seal() itself calls terminal_seal
    monkeypatch.setattr(sg, "terminal_seal", lambda **kw: made)
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
    rc, out, err = run(capsys, "abandon-track", "alpha", "--words", "no", *A, "--sign")
    assert rc == 0 and out == f"aborting alpha; cut 1 items: {iid}", err
    cut = [json.loads(x) for x in items_raw(store).decode().splitlines()][-1]
    assert cut["cause"] == hashlib.sha256(abandon_line(store).encode()).hexdigest() and cut["grade"] == "asserted"


# ------------------------------------------------- fix 1 (levain 26 rulings): each case run once by hand on a scratch store first


def test_the_abandon_names_the_items_files_last_line_l2_m2(tgt, store, tmp_path, mkrepo, rig):
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    keyed(tgt, "alpha", f"{m}:a")
    last_item = [ln for ln in items_raw(store).split(b"\n") if ln][-1]
    br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    abandon = json.loads(abandon_line(store))
    assert abandon["items_last"] == hashlib.sha256(last_item).hexdigest()
    assert sg.verify_record(abandon, rig.snapshot()).state == "valid"            # items_last is inside the signed bytes


def test_a_cut_of_another_repos_item_keyed_to_the_abandons_migration_is_refused_l2_m2(tgt, store, tmp_path, mkrepo, rig):
    """Ruling 1: alpha's ceremony M abandoned signed; beta is tracked, so an add for beta takes ANY key, M:x included; an
    unsigned cut of that item citing alpha's abandon is refused (the item is not of the abandon's repo)."""
    tracking(tgt, tmp_path, mkrepo, rig, name="alpha", plan=("a",))
    m = br.read_registry(tgt).fold.repos["alpha"].migration
    keyed(tgt, "alpha", f"{m}:a")
    tracked(tgt, tmp_path, mkrepo, rig, name="beta")
    br.track(tgt, "beta", "go", "desk 1", author=OP, seal=rig.seal())
    br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    cause = hashlib.sha256(abandon_line(store).encode()).hexdigest()
    stray = keyed(tgt, "beta", f"{m}:x")
    before = items_raw(store)
    with pytest.raises(bs.BoardError, match="decision-class write.*was added for 'beta'"):
        bs.decide(tgt, stray, "cut", "x", "seat-1", author=AGENT, now=NOW, cause=cause, authority=rig.source())
    assert items_raw(store) == before


def test_a_cut_of_the_abandoned_repos_item_added_after_the_abandon_is_refused_l2_m2(tgt, store, tmp_path, mkrepo, rig):
    """Ruling 1: alpha abandoned (M), done, re-tracked (M2) and tracked, so an add for alpha takes any key again; an item keyed
    M:y added now, after the abandon, cannot be cut unsigned by citing the old abandon."""
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    keyed(tgt, "alpha", f"{m}:a")
    br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    cause = hashlib.sha256(abandon_line(store).encode()).hexdigest()
    br.abandon_done(tgt, "alpha", "", "desk 1", author=OP)
    br.begin_track(tgt, "alpha", manifest(tmp_path), "again", "desk 1", author=OP, seal=rig.seal())
    m2 = br.read_registry(tgt).fold.repos["alpha"].migration
    iid = keyed(tgt, "alpha", f"{m2}:a")
    land(repo, "CHARTER.md", f"# Charter: alpha\nCharter-Version: 1\nExport: repo\n\n## Purpose\nT.\n\n## Criteria\n- C1: ok [judged: phill]\n\n"
                              f"## Constraints\n- K1: none\n\n## Plan\n- {iid}\n")
    br.track(tgt, "alpha", "go", "desk 1", author=OP, seal=rig.seal())
    late = keyed(tgt, "alpha", f"{m}:y")
    before = items_raw(store)
    with pytest.raises(bs.BoardError, match="decision-class write.*was added after the cited abandon-track"):
        bs.decide(tgt, late, "cut", "x", "seat-1", author=AGENT, now=NOW, cause=cause, authority=rig.source())
    assert items_raw(store) == before


def test_a_move_that_changes_a_tracking_or_tracked_repos_origin_is_decision_class_l2_m1(tgt, store, tmp_path, mkrepo, rig):
    """Ruling 2: a clone shares the root commits and has ANOTHER origin (the path it was cloned from)."""
    repo, m, iid = tracked(tgt, tmp_path, mkrepo, rig)                   # alpha: tracking
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(repo), str(fork))
    before = raw(store)
    with pytest.raises(bs.BoardError, match="move of a tracking or tracked repo that changes its origin or root set is a decision-class"):
        br.move(tgt, "alpha", str(fork), "m", "seat-1", author=AGENT)
    assert raw(store) == before
    br.track(tgt, "alpha", "go", "desk 1", author=OP, seal=rig.seal())  # alpha: tracked
    with pytest.raises(bs.BoardError, match="decision-class"):
        br.move(tgt, "alpha", str(fork), "m", "desk 1", author=OP)
    br.move(tgt, "alpha", str(fork), "m", "desk 1", author=OP, seal=rig.seal())
    r = reg_lines(store)[-1]
    assert r["op"] == "move" and r["grade"] == "signed-terminal" and sg.verify_record(r, rig.snapshot()).state == "valid"
    assert br.read_registry(tgt).fold.repos["alpha"].origin == r["origin"] != ""


def test_a_relocation_that_keeps_origin_and_roots_and_any_untracked_move_stay_asserted_l2_m1(tgt, store, tmp_path, mkrepo, rig):
    repo, m, iid = tracked(tgt, tmp_path, mkrepo, rig)                   # alpha: tracking
    reloc = tmp_path / "reloc"
    shutil.copytree(repo, reloc)                                         # the same repository, origin kept, another path
    br.move(tgt, "alpha", str(reloc), "relocated", "seat-1", author=AGENT)
    r = reg_lines(store)[-1]
    assert r["op"] == "move" and r["grade"] == "asserted" and r["path"] == os.path.realpath(reloc)
    beta = registered(tgt, mkrepo, "beta")                               # untracked
    fork = tmp_path / "bfork"
    git(tmp_path, "clone", "-q", str(beta), str(fork))                   # another origin
    br.move(tgt, "beta", str(fork), "moved", "seat-1", author=AGENT)
    r = reg_lines(store)[-1]
    assert r["op"] == "move" and r["repo"] == "beta" and r["grade"] == "asserted"


def test_signed_asserted_ok_registry_records_are_written_and_verify_l1_6b(tgt, store, tmp_path, mkrepo, rig):
    repo = mkrepo("alpha")
    br.register(tgt, "alpha", str(repo), "first", "desk 1", author=OP, seal=rig.seal())
    reloc = tmp_path / "reloc"
    shutil.copytree(repo, reloc)
    br.move(tgt, "alpha", str(reloc), "moved", "desk 1", author=OP, seal=rig.seal())
    br.pause(tgt, "alpha", "later", "desk 1", author=OP, seal=rig.seal())
    br.resume(tgt, "alpha", "now", "desk 1", author=OP, seal=rig.seal())
    tracking(tgt, tmp_path, mkrepo, rig, name="gamma", plan=())
    br.abandon_track(tgt, "gamma", "no", "desk 1", author=OP, seal=rig.seal())
    br.abandon_done(tgt, "gamma", "", "desk 1", author=OP, seal=rig.seal())
    with open(store / "repos.jsonl", "ab") as f:
        f.write(b"garbage")                                              # an unterminated torn tail: the ack gets a newline first
    n = len(reg_lines_tolerant(store))
    assert br.ack_torn(tgt, "repos.jsonl", n, "test", "desk 1", author=OP, seal=rig.seal())
    recs = [json.loads(x) for x in raw(store).decode().splitlines() if x != "garbage"]
    want = ("register", "move", "pause", "resume", "abandon-done", "ack-torn")
    signed = [r for r in recs if r["op"] in want and r.get("repo") in ("alpha", "gamma", None)]
    assert sorted({r["op"] for r in signed}) == sorted(want)
    for r in signed:
        if r["op"] == "register" and r["repo"] == "gamma":
            continue                                                     # the helper registered gamma unsigned
        assert r["grade"] == "signed-terminal" and sg.verify_record(r, rig.snapshot()).state == "valid", r["op"]
    assert not br.read_registry(tgt).fold.torn


def test_the_cli_rerun_of_an_unsigned_abandon_track_refuses_without_a_log_and_finishes_with_one_l1_6c(store, tmp_path, mkrepo, rig, capsys, monkeypatch):
    tgt = bs.resolve_store(None)
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a", "b"))
    ids = [keyed(tgt, "alpha", f"{m}:a"), keyed(tgt, "alpha", f"{m}:b")]
    real = bs.decide
    calls = []

    def crash_after_one(*args, **kw):
        if calls:
            raise bs.BoardError("simulated crash")
        calls.append(args[1])
        return real(*args, **kw)

    monkeypatch.setattr(bs, "decide", crash_after_one)
    with pytest.raises(bs.BoardError, match="simulated crash"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    monkeypatch.setattr(bs, "decide", real)
    before, items_before = raw(store), items_raw(store)
    rc, out, err = run(capsys, "abandon-track", "alpha", "--words", "no", *A)     # this install has no authority log
    assert rc == 2 and "decision-class write" in err and "no authority log" in err, err
    assert raw(store) == before and items_raw(store) == items_before
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
    rc, out, err = run(capsys, "abandon-track", "alpha", "--words", "no", *A)
    assert rc == 0 and out == f"aborting alpha; cut 1 items: {ids[1]}", err
    assert raw(store) == before                                          # no new abandon: the cut cites the signed one


def test_an_aborting_repo_with_an_asserted_abandon_recovers_by_a_signed_rerun_ruling_5(store, tmp_path, mkrepo, rig, capsys, monkeypatch):
    tgt = bs.resolve_store(None)
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    iid = keyed(tgt, "alpha", f"{m}:a")
    sid = br.read_registry(tgt).store_id
    forged = {"v": 1, "store": sid, "op": "abandon-track", "repo": "alpha", "at": NOW, "by": "x", "words": "w",
              "migration": m, "author": OP, "grade": "asserted", "prev": bs.chain_prev(raw(store))}
    with open(store / "repos.jsonl", "ab") as f:
        f.write(bs.canonical_bytes(forged) + b"\n")
    assert br.read_registry(tgt).fold.repos["alpha"].mode == "aborting"
    monkeypatch.setattr(sg, "resolve_authority", lambda **kw: rig.source())
    before, items_before = raw(store), items_raw(store)
    rc, out, err = run(capsys, "abandon-track", "alpha", "--words", "no", *A)    # unsigned: stuck, and says how to recover
    assert rc == 2 and "graded 'asserted': it must be signed (re-run `abandon-track` with --sign)" in err, err
    assert raw(store) == before and items_raw(store) == items_before
    _, cut = br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    assert cut == [iid]
    fresh = json.loads(abandon_line(store))
    assert fresh["grade"] == "signed-terminal" and fresh["migration"] == m and fresh["words"] == "w"   # re-signs the recorded words
    assert json.loads([x for x in items_raw(store).decode().splitlines()][-1])["cause"] == hashlib.sha256(abandon_line(store).encode()).hexdigest()
    n = len(reg_lines(store))
    assert br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())[1] == []
    assert len(reg_lines(store)) == n                                    # it holds now: a signed re-run writes nothing


def test_a_signed_abandon_with_another_authority_source_is_refused_l1_5(tgt, store, tmp_path, mkrepo, rig):
    tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    other = Rig(tmp_path / "other")
    before, items_before = raw(store), items_raw(store)
    with pytest.raises(bs.BoardError, match="the seal's own authority source"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal(), authority=other.source())
    assert raw(store) == before and items_raw(store) == items_before


def test_register_cli_with_no_signer_refuses_sign_instead_of_ignoring_it_l1_4(store, mkrepo, capsys):
    tgt = bs.resolve_store(None)
    registered(tgt, mkrepo)
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()

    def author_args(q):
        q.add_argument("--sign", action="store_true")
        q.add_argument("--author-kind", default="agent")
    br.register_cli(sub, author_args, lambda a: {"kind": a.author_kind, "name": a.by}, "items")
    before = raw(store)
    args = p.parse_args(["pause", "alpha", "--words", "w", "--by", "desk 1", "--author-kind", "operator", "--sign"])
    assert args.func(args) == 2 and "no signer is wired" in capsys.readouterr().err
    assert raw(store) == before
    args = p.parse_args(["pause", "alpha", "--words", "w", "--by", "desk 1"])
    assert args.func(args) == 0 and reg_lines(store)[-1]["op"] == "pause"


# ------------------------------------------------------------ fix 2 (levain 29 rulings A and D): each case reproduced first


def _crash_cuts(monkeypatch):
    """bs.decide raising: the abandon lands, no cut does (a crash between the two files)."""
    real = bs.decide

    def crash(*a, **kw):
        raise bs.BoardError("simulated crash")
    monkeypatch.setattr(bs, "decide", crash)
    return real


def _lockfree_add(store, iid: str, key: str) -> None:
    """An add appended by a writer that takes no lock and skips I1 (the item lands after the abandon's anchor)."""
    data = items_raw(store)
    rec = bs.build_record("add", iid, OP["name"], {"repo": "alpha", "title": "t", "words": "", "origin": "o", "done": None,
                                                    "done_kind": "merged", "tier": None, "depends_on": [], "key": key,
                                                    "payload_hash": "0" * 64}, NOW, author=OP)
    with open(store / "items.jsonl", "ab") as f:
        f.write(bs.canonical_bytes(bs._with_prev(rec, bs.chain_prev(data))) + b"\n")


def test_an_item_added_after_the_anchor_is_recovered_by_a_signed_rerun_ruling_a(tgt, store, tmp_path, mkrepo, rig, monkeypatch):
    """complement 16 / deepseek 75 / gemini 724 / codex MED 60, reproduced on 77bd80ff8: an add of M:late landing between the
    abandon's anchor and its cuts made the cut refuse ("added after"), and the signed re-run wrote NO fresh abandon (the old
    one "held"): the repo stayed aborting for good. Now the latest abandon does not hold while an uncut item of M sits after
    its anchor: an unsigned re-run says so by name, a signed one writes a fresh abandon whose anchor covers it."""
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    first = keyed(tgt, "alpha", f"{m}:a")
    real_read = br.read_registry
    state = {"n": 0}

    def add_between(target, **kw):
        state["n"] += 1
        if state["n"] == 2:                           # abandon_track's read right after its abandon lands, before the cuts
            _lockfree_add(store, "alpha#2", f"{m}:late")
        return real_read(target, **kw)
    monkeypatch.setattr(br, "read_registry", add_between)
    with pytest.raises(bs.BoardError, match="alpha#2 was added after the cited abandon-track"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    monkeypatch.setattr(br, "read_registry", real_read)
    n_abandons = raw(store).count(b'"op":"abandon-track"')
    before = raw(store), items_raw(store)
    with pytest.raises(bs.BoardError, match=r"alpha#2 was added after the cited abandon-track: .*\): re-run abandon-track signed"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=rig.source())
    assert (raw(store), items_raw(store)) == before
    tail_before = [ln for ln in items_raw(store).split(b"\n") if ln][-1]
    _, cut = br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    assert cut == ["alpha#2"] and raw(store).count(b'"op":"abandon-track"') == n_abandons + 1
    assert json.loads(abandon_line(store))["items_last"] == hashlib.sha256(tail_before).hexdigest()
    decides = [json.loads(x) for x in items_raw(store).decode().splitlines() if '"op":"decide"' in x]
    assert {d["id"] for d in decides} == {first, "alpha#2"}
    assert all(d["cause"] == hashlib.sha256(abandon_line(store).encode()).hexdigest() for d in decides if d["id"] == "alpha#2")
    br.abandon_done(tgt, "alpha", "", "desk 1", author=OP)
    assert br.read_registry(tgt).fold.repos["alpha"].mode == "untracked"


def test_a_lock_taking_add_during_the_abandons_prompt_refuses_the_abandon_and_the_rerun_covers_it_ruling_a(tgt, store, tmp_path, mkrepo, rig):
    """The reviewers' interleaving through levain's own writer: on 77bd80ff8 the add WAITED for the whole prompt (store.lock
    held) and was then refused (aborting). With the lock released for the prompt, the add lands while the repo still reads
    tracking, and the abandon finds the store changed: it is refused, nothing is written, and a re-run anchors after it."""
    import threading
    from tests.test_board_sign import _slow_seal
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    keyed(tgt, "alpha", f"{m}:a")
    started, out = threading.Event(), {}

    def abandon():
        try:
            br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=_slow_seal(rig, started, 1.5))
        except bs.BoardError as exc:
            out["err"] = str(exc)
    th = threading.Thread(target=abandon)
    th.start()
    assert started.wait(30)
    late = keyed(tgt, "alpha", f"{m}:late")
    th.join()
    assert "the board changed while you signed: sign again" in out["err"]
    assert b'"op":"abandon-track"' not in raw(store)
    _, cut = br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    assert late in cut and len(cut) == 2


def test_a_signed_rerun_recovers_when_the_abandons_anchor_left_the_items_file_codex_med_60(tgt, store, tmp_path, mkrepo, rig, monkeypatch):
    """codex MED 60: the items file was cut back past the abandon's anchor (a repair), leaving an uncut item. On 77bd80ff8 the
    signed re-run wrote no fresh abandon and every cut failed on the missing anchor."""
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a", "b"))
    a = keyed(tgt, "alpha", f"{m}:a")
    keyed(tgt, "alpha", f"{m}:b")
    real = _crash_cuts(monkeypatch)
    with pytest.raises(bs.BoardError, match="simulated crash"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    monkeypatch.setattr(bs, "decide", real)
    lines = items_raw(store).split(b"\n")
    (store / "items.jsonl").write_bytes(b"\n".join(lines[:-2]) + b"\n")          # the last add (the anchor) repaired away
    with pytest.raises(bs.BoardError, match=r"\(items_last\) is not in the items file\): re-run abandon-track signed"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=rig.source())
    _, cut = br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    assert cut == [a]


def test_an_unsigned_rerun_whose_abandon_no_longer_verifies_refuses_by_name_ruling_d(tgt, store, tmp_path, mkrepo, rig, monkeypatch):
    """gemini 121/983: an unsigned re-run skipped the latest abandon's validity check (each cut then refused with the
    cause text). Now it refuses up front, naming the recovery; with nothing left to cut it is the no-op it was."""
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    keyed(tgt, "alpha", f"{m}:a")
    real = _crash_cuts(monkeypatch)
    stranger = Rig(tmp_path / "gone", label="gone")
    with pytest.raises(bs.BoardError, match="simulated crash"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    monkeypatch.setattr(bs, "decide", real)
    before = raw(store), items_raw(store)
    with pytest.raises(bs.BoardError, match=r"cannot be the cause of the cuts still owed \(.*revoked.*\): re-run abandon-track signed"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=rig.source(revoked_seq=2))
    with pytest.raises(bs.BoardError, match="re-run abandon-track signed"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=stranger.source())
    assert (raw(store), items_raw(store)) == before


# ------------------------------------------- fix 3 (levain 29 ruling 1): one cause predicate for the cut and the recovery


def _signed_line(rig, rec: dict) -> bytes:
    """``rec`` signed by the throwaway key as a terminal signature (the bytes a real seal would write)."""
    rec = {**{k: v for k, v in rec.items() if k not in ("sig", "grade")}, "grade": "signed-terminal"}
    return bs.canonical_bytes(sg._attach(rec, rig.sign(sg.signing_bytes(rec)), rig.fp, "terminal"))


def _append_reg(store, line: bytes) -> None:
    with open(store / "repos.jsonl", "ab") as f:
        f.write(line + b"\n")


def _owed_after_crash(tgt, store, tmp_path, mkrepo, rig, monkeypatch):
    """alpha aborting, its signed abandon landed, its one item NOT cut (a crash between the two files)."""
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    iid = keyed(tgt, "alpha", f"{m}:a")
    real = _crash_cuts(monkeypatch)
    with pytest.raises(bs.BoardError, match="simulated crash"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    monkeypatch.setattr(bs, "decide", real)
    return m, iid, json.loads(abandon_line(store))


def _foreign_store_line(tgt, store, rig, m, held):
    """A signed abandon of alpha's migration naming ANOTHER store, at the chain's end, acknowledged as torn (the fold then
    skips it, and it is the latest abandon line the cuts would cite)."""
    _append_reg(store, _signed_line(rig, {**held, "store": "another-store", "at": "2026-11-17T13:00:00Z",
                                          "prev": bs.chain_prev(raw(store))}))
    n = len(raw(store).splitlines())
    br.ack_torn(tgt, "repos.jsonl", n, "a foreign line, kept", "desk 1", author=OP)
    return "another store"


def _prev_mismatch_line(tgt, store, rig, m, held):
    """A signed abandon of alpha's migration whose signed prev places it where the first abandon stands (a line copied)."""
    _append_reg(store, _signed_line(rig, {**held, "at": "2026-11-17T13:00:00Z"}))
    return "does not stand where its signed prev places it"


def _item_after_anchor(tgt, store, rig, m, held):
    _lockfree_add(store, "alpha#2", f"{m}:late")
    return "added after the cited abandon-track"


@pytest.mark.parametrize("make", [_foreign_store_line, _prev_mismatch_line, _item_after_anchor],
                         ids=["foreign-store", "prev-mismatch", "item-after-anchor"])
def test_whenever_a_cut_citing_the_latest_abandon_is_refused_a_signed_rerun_writes_a_fresh_abandon_fix3(
        make, tgt, store, tmp_path, mkrepo, rig, monkeypatch):
    """gemini MED 205 / deepseek HIGH 244 / complement 43, 46, reproduced on c946cc866 with the throwaway key: the recovery
    judged the latest abandon by ``abandon_problem`` alone, so a foreign-store line or a prev mismatch "held", the signed
    re-run wrote nothing, and every cut it then tried was refused by ``verify_cause``: the repo stayed aborting. Now the
    recovery calls ``board_sign.cut_cause_problem``, the predicate ``verify_cause`` is: the invariant is that whenever a cut
    citing the latest abandon would be refused, a signed re-run writes a fresh signed abandon and its cuts verify."""
    m, iid, held = _owed_after_crash(tgt, store, tmp_path, mkrepo, rig, monkeypatch)
    why = make(tgt, store, rig, m, held)
    view = br.read_registry(tgt)
    cause = br._abandon_record_hash(view, "alpha", m)
    lines = bs._store_lines(tgt)
    uncut = br._uncut(store, "alpha", m)
    problems = [sg.verify_cause({"id": i, "cause": cause}, lines, rig.snapshot()) for i in uncut]
    assert any(p and why in p for p in problems), problems                  # the cut citing the latest abandon is refused
    before = raw(store), items_raw(store)
    with pytest.raises(bs.BoardError, match=r"cannot be the cause of the cuts still owed \(.*" + why + r".*\): re-run abandon-track signed"):
        br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=rig.source())
    assert (raw(store), items_raw(store)) == before
    n_abandons = raw(store).count(b'"op":"abandon-track"')
    _, cut = br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    assert sorted(cut) == sorted(uncut) and raw(store).count(b'"op":"abandon-track"') == n_abandons + 1
    fresh = json.loads(abandon_line(store))
    assert fresh["grade"] == "signed-terminal" and fresh["store"] == view.store_id
    lines = bs._store_lines(tgt)
    decides = [json.loads(x) for x in lines.items if b'"op":"decide"' in x]
    assert {d["id"] for d in decides} == set(uncut)
    assert all(sg.verify_cause(d, lines, rig.snapshot()) is None for d in decides)
    br.abandon_done(tgt, "alpha", "", "desk 1", author=OP)
    assert br.read_registry(tgt).fold.repos["alpha"].mode == "untracked"


def test_a_signed_rerun_with_no_uncut_item_writes_no_abandon_even_when_the_latest_does_not_hold_complement_40(
        tgt, store, tmp_path, mkrepo, rig):
    """complement 40, reproduced on c946cc866: the signed branch re-signed an aborting repo whose latest abandon did not hold
    even with every item already cut (a new decision-class record with nothing for it to cause). It is guarded as the
    unsigned branch is: no uncut item, no new abandon."""
    repo, m = tracking(tgt, tmp_path, mkrepo, rig, plan=("a",))
    iid = keyed(tgt, "alpha", f"{m}:a")
    _, cut = br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())
    assert cut == [iid]
    held = json.loads(abandon_line(store))
    asserted = {k: v for k, v in held.items() if k != "sig"}
    asserted.update(grade="asserted", at="2026-11-17T13:00:00Z", prev=bs.chain_prev(raw(store)))
    _append_reg(store, bs.canonical_bytes(asserted))                       # the latest abandon no longer holds
    assert br.read_registry(tgt).fold.repos["alpha"].mode == "aborting"
    before = raw(store), items_raw(store)
    assert br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, seal=rig.seal())[1] == []
    assert (raw(store), items_raw(store)) == before
    assert br.abandon_track(tgt, "alpha", "no", "desk 1", author=OP, authority=rig.source())[1] == []
    assert (raw(store), items_raw(store)) == before


def test_the_sign_help_says_no_lock_is_held_during_the_prompt_fix3():
    """complement 37 / gemini 207 / deepseek 248: --sign's help still said the lock is held while the prompt is up."""
    from levain.desk import board_cli
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    board_cli.register(sub)
    board = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction)).choices["board"]
    verbs = next(a for a in board._actions if isinstance(a, argparse._SubParsersAction)).choices
    text = " ".join(verbs["decide"].format_help().split())
    assert "No lock is held while the prompt is up" in text and "sign again" in text
    assert "The lock is held while the prompt is up" not in text
