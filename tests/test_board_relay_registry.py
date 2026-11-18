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
