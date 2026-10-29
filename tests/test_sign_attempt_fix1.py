"""L3 r1 fix round on the sign-attempt construct (project_memory/1029-5/L3_attempt_r1.txt): a status read whose orphan
settle faults answers ``unknown`` (never a 500), and the pending record is written whole even on a short write."""
from __future__ import annotations

import json
import os
from pathlib import Path

from levain.consent import sign as signmod
from levain.consent.sign import attempts_dir
from tests.test_sign_attempt import AID, RID
from tests.test_sign_choice import _file, passkey  # noqa: F401 -- the fixture


def test_a_status_read_whose_orphan_settle_faults_reads_unknown_not_a_500(tmp_path: Path, passkey, monkeypatch) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    os.close(signmod._begin_attempt(tmp_path, RID, AID))      # its answer's process ended: pending, the lock free

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(signmod, "_write_over", full)
    monkeypatch.setattr(signmod, "ATTEMPT_WAIT_S", 0.3)
    assert signer.attempt(RID, {"attempt": AID})["state"] == "unknown"


def test_the_pending_record_is_written_whole_over_short_writes(tmp_path: Path, monkeypatch) -> None:
    _file(tmp_path, RID)
    real = os.write

    def short(fd, data):
        return real(fd, bytes(data)[:7])                       # never more than 7 bytes per call

    monkeypatch.setattr(signmod.os, "write", short)
    fd = signmod._begin_attempt(tmp_path, RID, AID)
    os.close(fd)
    rec = json.loads((attempts_dir(tmp_path) / f"{RID}.{AID}.json").read_text())
    assert rec["state"] == "pending" and rec["attempt"] == AID
