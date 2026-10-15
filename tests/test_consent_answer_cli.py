"""`levain consent pending` / `levain consent answer` (row 12, slice A): the CLI is a client of
``ConsentQueue.answer()``. Each test is from a failure seen in the scratch RUN."""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cli import main
from levain.cockpit.firejournal import CockpitDB
from levain.consent.queue import ConsentQueue, authority_source


def _key(d: Path, name: str) -> tuple[Path, str, str]:
    path = d / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True,
                   capture_output=True, timeout=30)
    pub = " ".join((d / f"{name}.pub").read_text().split()[:2])
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(path)], check=True, capture_output=True,
                        text=True).stdout.split()[1]
    return path, pub, fp


@pytest.fixture
def scratch(tmp_path):
    """A cockpit data root whose anchored authority log enrols one software laptop-presence key, and the queue
    the hook builds over it (claude_code._queue_for)."""
    keys = tmp_path / "keys"
    keys.mkdir()
    laptop, pub, fp = _key(keys, "laptop")
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        auth.sign_and_append(
            conn, {"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "laptop-presence",
                   "custody": "passphrase"}, signer_fp=fp,
            sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(laptop), "-n",
                                           auth.NAMESPACE], input=m, capture_output=True, check=True).stdout.decode(),
            clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))
    finally:
        conn.close()
    qdir = tmp_path / "home" / "consent"
    qdir.mkdir(parents=True, mode=0o700)
    queue = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))
    return queue, laptop, fp, root


def _file(queue: ConsentQueue, path: str, event: str):
    return queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                      tool_input={"file_path": path, "content": "hi"}, cwd="/w", hold=timedelta(minutes=5),
                      event_id=event)


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_answer_signs_in_the_answer_namespace_and_the_hook_delivers_it(scratch, capsys, decision):
    # seen red: signed through ssh_signer's default (the authority log's namespace), every answer was refused
    # with "the answer's signature does not verify"
    queue, laptop, fp, root = scratch
    request = _file(queue, "/w/a.txt", "e1")
    rc = main(["consent", "answer", request.id, decision, "--signing-key", str(laptop),
               "--queue-dir", str(queue.directory), "--data-root", str(root)])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert f"answering {request.id}: {decision.upper()}" in out.out
    got = queue.decision(request.id)
    assert got is not None and got.decision == decision and got.signer_fp == fp


def test_pending_names_each_target_once(scratch, capsys):
    # seen red: a Write's file_path is read both as its file and as a path field, so pending listed it twice
    queue, _, _, _ = scratch
    request = _file(queue, "/w/a.txt", "e2")
    assert main(["consent", "pending", "--queue-dir", str(queue.directory)]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith(request.id)
    assert "targets /w/a.txt  T2" in line
