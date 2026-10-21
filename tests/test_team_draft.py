"""C8: the coordinator drafts team-ledger entries; only the operator's promote signs one.

Each test replays a failure reproduced in project_memory 1021-18/coord/c8_probe_run.txt: a session Levain launched ran
`levain team record` and the ledger took it as the human's."""
import os
import shutil
import subprocess

import pytest

from levain.cli import main as levain_main
from levain.coordinator import teamdrafts as T
from levain.coordinator.store import CoordError, CoordStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ssh-keygen") is None
                                or os.name != "posix", reason="needs git + ssh-keygen + POSIX")


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ana@t", "-f", str(tmp_path / "k")], check=True)
    git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    git("clone", "-q", str(tmp_path / "origin.git"), "ana", cwd=tmp_path)
    ana = tmp_path / "ana"
    git("config", "user.email", "ana@ex.com", cwd=ana)
    git("config", "user.name", "ana", cwd=ana)
    (ana / "a.py").write_text("x = 1\n")
    git("add", ".", cwd=ana)
    git("commit", "-qm", "init", cwd=ana)
    git("push", "-q", "origin", "HEAD:main", cwd=ana)
    assert levain_main(["team", "init", "--project", "p", "--owner", "ana", "--signing-key", str(tmp_path / "k.pub"),
                        "--member", "ana=ana@ex.com", "--repo", str(ana), "--no-install"]) == 0
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    store = CoordStore(qdir)
    store.coord_set_agent("claude", cwd=str(tmp_path))
    store.accept_drop("hello", surface="cli")
    ln = store.coord_status()["launches"][0]
    store.launch_activate(ln["launch_id"], harness_session_id="sess-1")
    token = store.coord_claim(ln["launch_id"], store.launch(ln["launch_id"]).session_key)
    return ana, qdir, store, token


def test_a_draft_reaches_the_ledger_only_by_the_operators_promote(world, monkeypatch, capsys):
    ana, qdir, store, token = world
    d = T.add_draft(store, "decision", words="use the queue", kind="practice", paths=["a.py"], by="coordinator",
                    token=token)
    assert d["state"] == "drafted" and d["drafted_by"] == "coordinator"
    with pytest.raises(CoordError):          # the lease fences a coordinator's draft
        T.add_draft(store, "decision", words="x", by="coordinator", token=token + 1)
    before = git("rev-parse", "levain-team-ledger", cwd=ana)
    args = ["team", "draft", "promote", d["draft_id"], "--yes", "--no-push", "--repo", str(ana), "--queue-dir", str(qdir)]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")      # a session Levain launched
    assert levain_main(args) == 2
    assert git("rev-parse", "levain-team-ledger", cwd=ana) == before and T.get_draft(store, d["draft_id"])["state"] == "drafted"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")                # the operator's own terminal
    assert levain_main(args) == 0
    v = T.get_draft(store, d["draft_id"])
    assert v["state"] == "promoted" and v["ledger_id"]
    assert v["ledger_id"] in git("show", "levain-team-ledger:" + _ledger_file(ana), cwd=ana)
    with pytest.raises(CoordError):          # promoted once
        T.mark_promoted(store, d["draft_id"], "x-1")


def _ledger_file(ana):
    return git("ls-tree", "-r", "--name-only", "levain-team-ledger", cwd=ana).split()[0]


def test_draft_rows_are_append_only_and_final(world):
    import sqlite3
    ana, qdir, store, token = world
    d = T.add_draft(store, "finding", summary="s", by="operator")
    T.withdraw_draft(store, d["draft_id"])
    with pytest.raises(CoordError):
        T.mark_promoted(store, d["draft_id"], "x-1")
    c = sqlite3.connect(qdir / "dispatch.db")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE team_drafts SET body = '{}'")
