"""S0-b: the doctor and status output moved behind LedgerTransport members; its text and order are unchanged
(compared line by line against the pre-move code on main, 1012+100)."""
import os
import re
import shutil
import subprocess
import sys

import pytest

from levain.cli import main as levain_main
from levain.team.ledger_api import LedgerTransport, open_ledger
from levain.team.transport import Repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ssh-keygen") is None
                                or os.name != "posix", reason="needs git + ssh-keygen + POSIX")


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _team(capsys, *a, repo):
    capsys.readouterr()
    rc = levain_main(["team", *a, "--repo", str(repo)])
    return rc, capsys.readouterr().out


def _mk(tmp, name, bare=None):
    if bare:
        _git("clone", "-q", str(bare), name, cwd=tmp)
    else:
        (tmp / name).mkdir()
        _git("init", "-q", cwd=tmp / name)
    d = tmp / name
    _git("config", "user.email", f"{name}@x.io", cwd=d)
    _git("config", "user.name", name, cwd=d)
    (d / "f").write_text("x")
    _git("add", "f", cwd=d)
    _git("commit", "-qm", "i", cwd=d)
    return d


@pytest.fixture
def key(tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "k", "-f", str(tmp_path / "k")], check=True)
    return tmp_path / "k.pub"


def test_doctor_and_status_text_local_only(tmp_path, key, capsys):
    solo = _mk(tmp_path, "solo")
    assert _team(capsys, "init", "--owner", "solo", "--member", "solo=solo@x.io", "--project", "p",
                 "--signing-key", str(key), repo=solo)[0] == 0
    rc, out = _team(capsys, "doctor", repo=solo)
    lines = out.splitlines()
    assert rc == 0
    assert re.fullmatch(r"ok    ledger worktree \S+/worktree \(device [0-9a-f]+\)", lines[0])
    assert lines[1:4] == ["ok    team.toml valid: p, owner solo, mode ask-once", "ok    you are solo",
                          "ok    0 entries, chains intact"]
    assert lines[4] == "ok    no remote: ledger is local only"
    rc, out = _team(capsys, "status", repo=solo)
    assert out.splitlines()[0] == "p: owner solo, you are solo, mode ask-once"
    assert re.fullmatch(r"ledger levain-team-ledger, genesis [0-9a-f]{12}, owner in force solo", out.splitlines()[1])
    assert not any(ln.startswith("last fetched") for ln in out.splitlines())


def test_doctor_and_status_text_with_a_remote(tmp_path, key, capsys):
    _git("init", "-q", "--bare", "origin.git", cwd=tmp_path)
    ana = _mk(tmp_path, "ana", bare=tmp_path / "origin.git")
    _git("push", "-q", "origin", "HEAD", cwd=ana)
    assert _team(capsys, "init", "--owner", "ana", "--member", "ana=ana@x.io", "--project", "p",
                 "--signing-key", str(key), repo=ana)[0] == 0
    rc, out = _team(capsys, "doctor", repo=ana)
    lines = out.splitlines()
    assert rc == 0 and lines[4].startswith("ok    remote origin: last fetch ")
    assert lines[5] == "ok    0 local ledger commit(s) not pushed"
    rc, out = _team(capsys, "status", repo=ana)
    lines = out.splitlines()
    assert lines[1].startswith("ledger levain-team-ledger, genesis ")
    assert re.fullmatch(r"last fetched (\d+ min ago|never); newest ledger commit .+\. A host can withhold newer commits "
                        r"without any signal: compare the tip id with a teammate if anything looks stale", lines[2])


def test_the_moved_members_are_on_the_protocol(tmp_path, key):
    solo = _mk(tmp_path, "solo")
    gl = open_ledger(Repo.discover(solo))
    for name in ("confirm_own_key", "ledger_of", "forget_derivation", "has_local_ledger", "has_remote_ledger",
                 "export_pos", "canon_path", "ledger_label", "freshness_lines", "foreign_ledger_lines",
                 "location_row", "publish_rows"):
        assert hasattr(LedgerTransport, name) and hasattr(gl, name), name
    assert gl.canon_path.name == "PROJECT.md" and gl.ledger_label() == "ledger levain-team-ledger"
    assert gl.freshness_lines() == [] and gl.foreign_ledger_lines(None) == []
    assert not sys.modules["levain.team.cli_tenure"].__dict__.get("_nested_genesis")
