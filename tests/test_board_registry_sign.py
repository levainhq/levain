"""P2 wired into the registry (levain#96, levain#88 slice): every ``repos.jsonl`` write verb goes through the fixed 1.0 policy.

Each test was written after the path was run once by hand (scratch store, scratch git repo, the CLI). A REAL Touch ID
signature cannot run in a test: the seal is ``board_sign.terminal_seal`` over a THROWAWAY ed25519 key with a touch-id sidecar,
its authority log a hand-built snapshot that enrols the key (``tests.test_board_sign.Rig``, the seam P2's own tests use). The
signer is the real ``ssh_signer``; the grade is earned by ``admit``'s replay, never by the label.

This module does NOT use test_board_registry.py's autouse seam (which seals the decision-class verbs for that file's rules
tests): here the policy itself is the subject."""
from __future__ import annotations

import hashlib
import json
import os
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


def test_use_import_focus_refuses_sign_there_is_nothing_to_sign(home, tmp_path, mkrepo, capsys):
    mkrepo("alpha")
    focus = focus_file(tmp_path, {"alpha": ("active", "wa")})
    rc, out, err = run(capsys, "use", str(tmp_path / "store"), "--import-focus", str(focus), "--author-kind", "operator", "--sign")
    assert rc == 2 and "--sign does not apply" in err and focus.read_text().count("moved_to") == 0
    assert not (tmp_path / "store" / "repos.jsonl").exists()


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
    forged = {"v": 1, "store": "s", "op": "abandon-track", "repo": "alpha", "at": NOW, "by": "x", "words": "w",
              "migration": m, "author": OP, "grade": "asserted", "prev": "0" * 64}
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
