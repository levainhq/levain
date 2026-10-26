"""``levain push`` (levain.consent.push_gate): what it acts on.

A queue row is written by anyone holding the uid; its display is not what the answer binds (the digest is). A row
whose display shows one push and whose digest names another must never carry the operator's ruling onto the push
he was not shown.
"""
from __future__ import annotations

import argparse
import io
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

from levain.cockpit.consent import utc_stamp
from levain.consent import push_gate
from levain.consent.queue import _FIELDS, ConsentQueue
from levain.consent.record import (
    RULING_NAMESPACE, ExternalGateRequest, redact_display, ruling_message, tool_input_digest,
)
from tests.test_consent_queue import authority, keys  # noqa: F401 - the shared fixtures
from tests._h4 import h4_home


def _git(*argv, cwd=None):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True,
                          timeout=60).stdout.strip()


def test_a_ruling_on_a_row_whose_display_names_another_push_never_pushes(tmp_path, keys, authority, monkeypatch):
    # seen red (B6 fix r1, L2 2): a raw row showing a push of claude/x but carrying the digest of a push of main,
    # ruled ALLOW with the operator's real key, pushed main on `levain push --branch main`
    base = tmp_path.resolve()
    repo, remote = base / "repo", base / "remote.git"
    _git("init", "-q", "--bare", str(remote))
    _git("init", "-q", "-b", "main", str(repo))
    for branch in ("main", "claude/x"):
        if branch != "main":
            _git("checkout", "-q", "-b", branch, cwd=repo)
        _git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", branch, cwd=repo)
    url = f"file://{remote}"
    shown = {"repo": str(repo), "remote": url, "branch": "claude/x", "commit": _git("rev-parse", "claude/x", cwd=repo)}
    evil = {**shown, "branch": "main", "commit": _git("rev-parse", "main", cwd=repo)}

    queue = ConsentQueue(base / "home" / "consent", authority=authority, home=h4_home())
    queue.pending()                                     # the store exists before the raw writer opens it
    now = datetime.now(timezone.utc)
    forged = ExternalGateRequest.create(
        agent="claude_code", agent_session_id="s1", event_id="rand:" + os.urandom(8).hex(),
        tool_name=push_gate.PUSH_TOOL, tool_input_digest=tool_input_digest(evil),
        display=redact_display(push_gate.PUSH_TOOL, shown), cwd=str(repo), tier=push_gate.PUSH_TIER,
        received_at=utc_stamp(now), expires_at=utc_stamp(now + timedelta(seconds=1)))
    conn = sqlite3.connect(queue.path, isolation_level=None)
    conn.execute(f"INSERT INTO requests ({', '.join(_FIELDS)}) VALUES ({', '.join('?' for _ in _FIELDS)})",
                 tuple(forged.fields()[f] for f in _FIELDS))
    conn.close()
    assert queue.mark_undecided(forged.id, halt=True).state == "undecided"
    payload = queue.ruling_payload(forged.id, "allow", keys["phone"].fp)
    queue.answer_late(forged.id, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))

    monkeypatch.setattr(push_gate, "_signable", lambda settings: True)
    monkeypatch.setattr(push_gate, "_queue", lambda qdir, settings: queue)
    out = io.StringIO()
    args = argparse.Namespace(remote=url, branch="main", repo=str(repo), hold=1.0, agent="claude_code",
                              session="s1", queue_dir=queue.directory)
    rc = push_gate.run(args, out)
    remote_main = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q", "refs/heads/main"],
                                 capture_output=True, text=True).stdout.strip()
    assert (rc, remote_main) == (push_gate.EXIT_NOT_TAKEN, ""), out.getvalue()
    assert "not the push it was filed for" in out.getvalue()
