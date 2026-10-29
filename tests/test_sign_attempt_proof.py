"""The attempt mark (``<id>.<attempt>.sig256``) is read like every other file in the sign store: a bounded,
non-blocking, no-symlink read of a regular file, holding exactly 64 lowercase hex characters (a newline may follow).
Anything else is no proof, so the attempt settles refused."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from levain.consent import sign as signmod
from levain.consent.sign import attempts_dir
from tests.test_sign_attempt import AID, RID, _challenged
from tests.test_sign_choice import _file, passkey  # noqa: F401 -- the fixture


def _signed(tmp_path: Path, passkey) -> Path:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    signer.answer(RID, {**_challenged(signer, key, sign), "attempt": AID})
    assert signmod._proof(tmp_path, RID, AID) is not None
    return attempts_dir(tmp_path) / f"{RID}.{AID}.sig256"


def test_a_mark_that_is_not_exactly_64_lowercase_hex_is_no_proof(tmp_path: Path, passkey) -> None:
    mark = _signed(tmp_path, passkey)
    good = mark.read_text().strip()
    for bad in ("  " + good + "  \n", good + "\n\n", good + "x", "﻿" + good):
        mark.write_text(bad)
        assert signmod._proof(tmp_path, RID, AID) is None, repr(bad)
    mark.write_text(good)
    assert signmod._proof(tmp_path, RID, AID) is not None


def test_a_fifo_in_the_marks_place_is_no_proof_and_never_waited_on(tmp_path: Path, passkey) -> None:
    mark = _signed(tmp_path, passkey)
    mark.unlink()
    os.mkfifo(mark)
    out: list = []
    t = threading.Thread(target=lambda: out.append(signmod._proof(tmp_path, RID, AID)), daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():   # unblock the reader so the test process can end
        os.close(os.open(mark, os.O_WRONLY | os.O_NONBLOCK))
    assert out == [None]


def test_a_symlink_in_the_marks_place_is_no_proof(tmp_path: Path, passkey) -> None:
    mark = _signed(tmp_path, passkey)
    real = tmp_path / "elsewhere"
    real.write_text(mark.read_text())
    mark.unlink()
    mark.symlink_to(real)
    assert signmod._proof(tmp_path, RID, AID) is None


def test_a_symlink_in_the_attempts_dir_settles_unknown_never_a_500(tmp_path: Path, passkey) -> None:
    signer, key, sign = passkey
    _file(tmp_path, RID)
    real = tmp_path / "elsewhere.json"
    real.write_text("{}")
    record = attempts_dir(tmp_path) / f"{RID}.{AID}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.symlink_to(real)
    assert signmod._settle_orphan(tmp_path, RID, AID) == {"state": "unknown"}
    assert signmod._settle_orphan(tmp_path, RID, AID) == {"state": "unknown"}   # nothing was written through it
    assert real.read_text() == "{}"
