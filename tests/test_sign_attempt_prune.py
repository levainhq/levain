"""The attempt prune (_attempt_room) drops a settled record only under the record's own lock: a record whose lock is
held (an answer or a settle is on it) is kept whatever it reads, so a prune never races a settle into a missing
record that a later status read would fence as refused."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from levain.consent import sign as signmod
from levain.consent.sign import attempts_dir

RID = "np-att0001-aa11"
AID = "attempt-0000000000000001"


def _old_record(tmp_path: Path, state: str) -> Path:
    d = attempts_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{RID}.{AID}.json"
    path.write_text(json.dumps({"id": RID, "attempt": AID, "state": state, "at": "x"}) + "\n")
    old = time.time() - signmod.ATTEMPT_KEEP_S - 60
    os.utime(path, (old, old))
    return path


def test_an_old_settled_record_whose_lock_is_held_is_kept(tmp_path: Path) -> None:
    path = _old_record(tmp_path, "signed")
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        signmod._attempt_room(tmp_path)
        assert path.exists()
    finally:
        os.close(fd)


def test_an_old_settled_record_with_no_lock_held_is_dropped_and_a_pending_one_never(tmp_path: Path) -> None:
    path = _old_record(tmp_path, "refused")
    signmod._attempt_room(tmp_path)
    assert not path.exists()
    path = _old_record(tmp_path, "pending")
    signmod._attempt_room(tmp_path)
    assert path.exists()
