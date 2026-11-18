"""Relayed begin-track and track (levain#97, P3-b; the restack wiring): ``--source <q-id>`` on the registry's ceremony verbs, held to
the manifest's sha256 by ``board_relay.check_manifest``. Each path was first run once by hand on a scratch store, a scratch git
repo and a scratch native band (levain's own QuestionStore); the band here is a fake source keyed by q- ids."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_relay as br

Q1 = "q-" + "1" * 24
Q2 = "q-" + "2" * 24
CHARTER = ("# Charter: alpha\nCharter-Version: 1\nExport: repo\n\n## Purpose\nTest.\n\n## Criteria\n- C1: it works [judged: phill]\n\n"
           "## Constraints\n- K1: none\n\n## Plan\n- alpha#1\n")
BY = ["--by", "desk 24"]


class FakeBand:
    def __init__(self, **rows: br.BandRow) -> None:
        self.rows = dict(rows)

    def row(self, np_id: str) -> br.BandRow:
        if np_id not in self.rows:
            raise br.BandRefusal(f"{np_id} is not a row this band holds")
        return self.rows[np_id]


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env).stdout.decode().strip()


def run(capsys, *argv):
    rc = main(["board", *argv])
    out, err = capsys.readouterr()
    return rc, out.strip(), err.strip()


def reg(store: Path) -> list[dict]:
    return [json.loads(x) for x in (store / "repos.jsonl").read_text("utf-8").splitlines()]


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lhome"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    monkeypatch.delenv("LEVAIN_LAUNCH_KEY", raising=False)
    store = tmp_path / "store"
    assert main(["board", "use", str(store)]) == 0
    store = Path(os.path.realpath(store))
    (store / "repos.jsonl").touch()
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = tmp_path / "alpha"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hi\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q")
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"plan_items": ["a"], "contract_items": []}))
    sha = hashlib.sha256(mf.read_bytes()).hexdigest()
    band = FakeBand()
    monkeypatch.setattr(br, "band_source", lambda directory=None: band)
    assert run(capsys, "register", "alpha", "--path", str(repo), "--words", "first", *BY)[0] == 0
    return store, repo, mf, sha, band


def test_a_relayed_begin_track_needs_the_manifest_digest_in_the_question(world, capsys):
    store, _, mf, sha, band = world
    band.rows[Q1] = br.BandRow("Begin tracking alpha?", "yes begin it", "unsigned-terminal", "native")
    argv = ("begin-track", "alpha", "--manifest", str(mf), "--words", "yes begin it", "--source", Q1, *BY)
    rc, _, err = run(capsys, *argv)
    assert rc == 2 and "does not carry the manifest's sha256" in err
    assert not any(r["op"] == "begin-track" for r in reg(store))
    band.rows[Q1] = br.BandRow(f"Begin tracking alpha? manifest sha256 {sha}", "yes begin it", "unsigned-terminal", "native")
    rc, out, err = run(capsys, *argv)
    assert rc == 0, err
    rec = [r for r in reg(store) if r["op"] == "begin-track"][-1]
    assert rec["grade"] == "relayed" and rec["author"] == {"kind": "agent", "name": "desk 24"}
    assert rec["source"]["np"] == Q1 and rec["source"]["question_sha256"] == br.digest(band.rows[Q1].question)
    assert br.verify_relayed(rec, band).ok
    assert list(rec)[-2:] == ["grade", "source"]


def test_a_question_that_carries_another_manifest_digest_is_refused(world, capsys):
    _, _, mf, _, band = world
    band.rows[Q1] = br.BandRow("Begin? manifest sha256 " + "e" * 64, "yes", "unsigned-terminal", "native")
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes", "--source", Q1, *BY)
    assert rc == 2 and "does not carry the manifest's sha256" in err


def test_a_relayed_track_binds_the_manifest_the_begin_track_recorded(world, capsys):
    store, repo, mf, sha, band = world
    band.rows[Q1] = br.BandRow(f"Begin? manifest sha256 {sha}", "yes begin", "signed-phone", "native")
    assert run(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes begin", "--source", Q1, *BY)[0] == 0
    m = [r for r in reg(store) if r["op"] == "begin-track"][-1]["migration"]
    assert run(capsys, "add", "--repo", "alpha", "--title", "t", "--origin", "d", "--key", f"{m}:a", "--payload-hash", "a" * 64, *BY)[0] == 0
    (repo / "CHARTER.md").write_text(CHARTER)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "charter")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q")
    band.rows[Q2] = br.BandRow("Track alpha now?", "yes track", "signed-phone", "native")
    rc, _, err = run(capsys, "track", "alpha", "--words", "yes track", "--source", Q2, *BY)
    assert rc == 2 and "does not carry the manifest's sha256" in err
    assert not any(r["op"] == "track" for r in reg(store))
    band.rows[Q2] = br.BandRow(f"Track alpha now? manifest {sha}", "yes track", "signed-phone", "native")
    rc, out, err = run(capsys, "track", "alpha", "--words", "yes track", "--source", Q2, *BY)
    assert rc == 0, err
    rec = [r for r in reg(store) if r["op"] == "track"][-1]
    assert rec["grade"] == "relayed" and rec["source"]["np"] == Q2 and br.verify_relayed(rec, band).ok


def test_an_np_id_is_refused_up_front_on_the_ceremony_verbs(world, capsys):
    store, _, mf, _, _ = world
    rc, _, err = run(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "y", "--source", "np-1b3dd088", *BY)
    assert rc == 2 and "np-" in err
    rc, _, err = run(capsys, "track", "alpha", "--words", "y", "--source", "np-1b3dd088", *BY)
    assert rc == 2 and "np-" in err
    assert [r["op"] for r in reg(store)] == ["register"]


@pytest.mark.parametrize("kind", ["agent", "operator"])
def test_source_with_sign_is_refused_on_every_verb_and_writes_nothing(world, capsys, kind):
    store, _, mf, sha, band = world
    band.rows[Q1] = br.BandRow(f"Begin? {sha}", "yes", "unsigned-terminal", "native")
    verbs = (["add", "--repo", "alpha", "--title", "x", "--origin", "d", "--words", "yes"],
             ["amend", "--id", "alpha#1", "--why", "w", "--words", "yes"],
             ["decide", "--id", "alpha#1", "--decision", "cut", "--words", "yes"],
             ["begin-track", "alpha", "--manifest", str(mf), "--words", "yes"],
             ["track", "alpha", "--words", "yes"])
    for verb in verbs:
        rc, _, err = run(capsys, *verb, "--source", Q1, "--sign", "--author-kind", kind, *BY)
        assert rc == 2 and "--source and --sign cannot be used together" in err, verb
    assert [r["op"] for r in reg(store)] == ["register"] and not (store / "items.jsonl").exists()


def test_the_writers_refuse_a_relay_and_a_seal_together_whatever_the_cli(world):
    from levain.desk import board_registry as brg
    from levain.desk import board_store as bs
    store, _, mf, sha, band = world
    target = bs.resolve_store(str(store))
    relay, seal, author = (Q1, band), object(), {"kind": "operator", "name": "desk 24"}
    with pytest.raises(bs.BoardError, match="never both"):
        bs.add(target, "alpha", "t", "d", "desk 24", author=author, relay=relay, seal=seal)
    with pytest.raises(bs.BoardError, match="never both"):
        bs.amend(target, "alpha#1", "w", "desk 24", {"words": "y"}, author=author, relay=relay, seal=seal)
    with pytest.raises(bs.BoardError, match="never both"):
        bs.decide(target, "alpha#1", "cut", "y", "desk 24", author=author, relay=relay, seal=seal)
    with pytest.raises(bs.BoardError, match="never both"):
        brg.begin_track(target, "alpha", mf, "y", "desk 24", author=author, relay=relay, seal=seal)
    with pytest.raises(bs.BoardError, match="never both"):
        brg.track(target, "alpha", "y", "desk 24", author=author, relay=relay, seal=seal)


# ------------------------------------------------------------------------------------- a relayed abandon is a cut's cause


def _begun(world, capsys):
    """begin-track relayed, two items of the ceremony added; returns (store, mf, sha, band, migration)."""
    store, _, mf, sha, band = world
    band.rows[Q1] = br.BandRow(f"Begin? manifest sha256 {sha}", "yes begin", "unsigned-terminal", "native")
    assert run(capsys, "begin-track", "alpha", "--manifest", str(mf), "--words", "yes begin", "--source", Q1, *BY)[0] == 0
    m = [r for r in reg(store) if r["op"] == "begin-track"][-1]["migration"]
    for k in "ab":
        assert run(capsys, "add", "--repo", "alpha", "--title", k, "--origin", "d", "--key", f"{m}:{k}", "--payload-hash", k * 64, *BY)[0] == 0
    band.rows[Q2] = br.BandRow("Abandon alpha's ceremony?", "no, abandon it", "unsigned-terminal", "native")
    return store, mf, sha, band, m


def items(store: Path) -> list[dict]:
    return [json.loads(x) for x in (store / "items.jsonl").read_text("utf-8").splitlines()]


def test_a_relayed_abandon_track_is_a_cut_cause_through_the_one_predicate(world, capsys):
    store, _, _, band, _ = _begun(world, capsys)
    rc, out, err = run(capsys, "abandon-track", "alpha", "--words", "no, abandon it", "--source", Q2, *BY)
    assert rc == 0, err
    assert out.startswith("aborting alpha; cut 2 items")
    ab = reg(store)[-1]
    assert ab["op"] == "abandon-track" and ab["grade"] == "relayed" and ab["source"]["np"] == Q2 and "sig" not in ab
    cuts = [r for r in items(store) if r["op"] == "decide"]
    line = (store / "repos.jsonl").read_text("utf-8").splitlines()[-1]
    assert len(cuts) == 2 and all(c["cause"] == hashlib.sha256(line.encode()).hexdigest() and c["grade"] == "asserted" for c in cuts)
    # the same predicate judges a cut written by hand: with the band it holds, without it (or a band that no longer says it) it does not
    cause = cuts[0]["cause"]
    base = ("decide", "--id", "alpha#1", "--decision", "cut", "--words", "again", *BY, "--cause", cause)
    rc, _, err = run(capsys, *base)
    assert rc == 0, err                                               # the CLI hands the cut the band lazily: the fake band here holds it
    band.rows[Q2] = br.BandRow("Abandon alpha's ceremony?", "no, keep it", "unsigned-terminal", "native")
    rc, _, err = run(capsys, *base)
    assert rc == 2 and "relayed abandon-track does not verify" in err and "digest mismatch" in err


def test_a_relayed_cause_with_no_band_is_refused_not_trusted(world):
    from levain.desk import board_sign as sg
    from levain.desk import board_store as bs
    store = world[0]
    cut = {"op": "decide", "id": "alpha#1", "decision": "cut", "cause": "0" * 64}
    held = {"op": "abandon-track", "grade": "relayed"}
    assert "no band was given" in sg.abandon_problem(held, None)
    with pytest.raises(bs.BoardError, match="cause names no abandon-track|cause cannot be checked"):
        sg.require_policy("decide", "asserted", cut, store=None, snapshot=None)


def test_a_cut_owed_after_a_relayed_abandon_needs_the_band_to_finish(world, capsys, monkeypatch):
    from levain.desk import board_store as bs
    store, _, _, band, _ = _begun(world, capsys)
    real = bs.decide
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise bs.BoardError("a crash after the abandon landed")
        return real(*a, **kw)
    monkeypatch.setattr(bs, "decide", flaky)
    rc, _, err = run(capsys, "abandon-track", "alpha", "--words", "no, abandon it", "--source", Q2, *BY)
    assert rc == 2 and "a crash" in err and reg(store)[-1]["op"] == "abandon-track" and not [r for r in items(store) if r["op"] == "decide"]
    rc, _, err = run(capsys, "abandon-track", "alpha", "--words", "no, abandon it", *BY)       # unsigned, no band: the cut is refused, by name
    assert rc == 2 and "no band was given" in err
    rc, out, err = run(capsys, "abandon-track", "alpha", "--words", "no, abandon it", "--source", Q2, *BY)
    assert rc == 0 and out.startswith("aborting alpha; cut 2 items"), err
    assert sum(r["op"] == "abandon-track" for r in reg(store)) == 1                           # the relayed abandon held: no fresh one


def test_a_relayed_abandon_whose_answer_changed_is_rewritten_by_a_relayed_rerun(world, capsys, monkeypatch):
    from levain.desk import board_store as bs
    store, _, _, band, _ = _begun(world, capsys)
    real = bs.decide
    monkeypatch.setattr(bs, "decide", lambda *a, **kw: (_ for _ in ()).throw(bs.BoardError("crash")))
    assert run(capsys, "abandon-track", "alpha", "--words", "no, abandon it", "--source", Q2, *BY)[0] == 2
    monkeypatch.setattr(bs, "decide", real)
    band.rows[Q2] = br.BandRow("Abandon alpha's ceremony?", "no, abandon it, and say so twice", "unsigned-terminal", "native")
    rc, out, err = run(capsys, "abandon-track", "alpha", "--words", "x", "--source", Q2, *BY)   # the first abandon no longer verifies: a fresh one
    assert rc == 0, err
    assert sum(r["op"] == "abandon-track" for r in reg(store)) == 2
    assert reg(store)[-1]["source"]["answer_sha256"] == br.digest("no, abandon it, and say so twice")
    assert out.startswith("aborting alpha; cut 2 items")
