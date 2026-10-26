"""`levain consent pending` / `levain consent answer` (row 12, slice A): the CLI is a client of
``ConsentQueue.answer()``. Each test is from a failure seen in the scratch RUN."""
from __future__ import annotations

import json
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
from tests._h4 import h4_home


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
    queue = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)), home=h4_home())
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


def test_answer_refuses_a_display_cut_at_its_limit(scratch, capsys):
    # seen red (probe_fix1 item 1): a 3,000-char Write showed a cut display and was signed and allowed
    queue, laptop, _, root = scratch
    request = queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                         tool_input={"file_path": "/w/big.txt", "content": "benign " * 300 + "rm -rf ~"}, cwd="/w",
                         hold=timedelta(minutes=5), event_id="e3")
    assert main(["consent", "pending", "--queue-dir", str(queue.directory)]) == 0
    assert "too long to answer here" in capsys.readouterr().out
    rc = main(["consent", "answer", request.id, "allow", "--signing-key", str(laptop),
               "--queue-dir", str(queue.directory), "--data-root", str(root)])
    out = capsys.readouterr()
    assert rc == 1 and "too long to show here" in out.err and "the agent's own dialog stands" in out.err
    assert "answering" not in out.out
    assert queue.get(request.id)[1] == "pending"


def test_agent_sourced_fields_print_escaped(scratch, capsys):
    # seen red (probe_fix1 item 3): a cwd holding a newline, a fake tier line and an ESC sequence printed raw
    queue, laptop, _, root = scratch
    cwd = "/w\n  tier            T1\x1b[2K‮ \x9b؜"
    request = queue.file(agent="claude_code", agent_session_id="s\x1b]0;x\x07", tool_name="Write",
                         tool_input={"file_path": "/w/a‮.txt", "content": "hi"}, cwd=cwd,
                         hold=timedelta(minutes=5), event_id="e4")
    assert main(["consent", "pending", "--queue-dir", str(queue.directory)]) == 0
    listed = capsys.readouterr().out
    assert main(["consent", "answer", request.id, "deny", "--signing-key", str(laptop),
                 "--queue-dir", str(queue.directory), "--data-root", str(root)]) == 0
    answered = capsys.readouterr().out
    for out in (listed, answered):
        assert not any(c in out for c in "\x1b\x07\x9b‮ ؜")
        assert "\\u000a  tier            T1\\u001b[2K\\u202e\\u2028\\u009b\\u061c" in out
    assert len(listed.splitlines()) == 1
    assert [ln for ln in answered.splitlines() if ln.lstrip().startswith("tier")] == ["  tier            T2"]


def test_answer_verifies_against_the_queue_settings_cockpit(scratch, capsys, tmp_path):
    # seen red (probe_fix1 item 4): --data-root naming another cockpit than the hook's was signed and recorded
    # (never delivered); with no root flag the CLI fell back to the cwd's install and refused after signing
    queue, laptop, fp, root = scratch
    other = tmp_path / "other-cockpit"
    other.mkdir()
    (queue.directory / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root)}))
    request = _file(queue, "/w/a.txt", "e5")
    rc = main(["consent", "answer", request.id, "allow", "--signing-key", str(laptop),
               "--queue-dir", str(queue.directory), "--data-root", str(other)])
    out = capsys.readouterr()
    assert rc == 1 and "the hook verifies against" in out.err and "answering" not in out.out
    assert queue.get(request.id)[1] == "pending"
    assert main(["consent", "answer", request.id, "allow", "--signing-key", str(laptop),
                 "--queue-dir", str(queue.directory)]) == 0, capsys.readouterr().err
    got = queue.decision(request.id)
    assert got is not None and got.decision == "allow" and got.signer_fp == fp


def test_a_display_nested_too_deep_to_read_back_is_unshown():
    # r2 complement LOW (item 10): json.loads raised RecursionError out of the refusal path
    from types import SimpleNamespace

    from levain.cli import _UNSHOWN, _filed_input

    deep = "[" * 100_000 + "]" * 100_000
    assert _filed_input(SimpleNamespace(tool_name="Write", display=f"Write {deep}")) is _UNSHOWN


def test_answer_refuses_a_root_holding_no_cockpit_before_signing(scratch, capsys, tmp_path):
    # seen red (fix2, item 11's reachable shape): --path on a directory with no install resolved to a cockpit
    # root that does not exist; the answer was signed, then refused by the queue
    queue, laptop, fp, root = scratch
    request = _file(queue, "/w/a.txt", "e7")
    bare = tmp_path / "no-install"
    bare.mkdir()
    rc = main(["consent", "answer", request.id, "allow", "--signing-key", str(laptop),
               "--queue-dir", str(queue.directory), "--path", str(bare)])
    out = capsys.readouterr()
    assert rc == 1 and "holds no cockpit" in out.err and "answering" not in out.out
    assert queue.get(request.id)[1] == "pending"
