"""The dispatch service runs only as ``_levain_dispatch``: any other euid, root included, exits 77 (EX_NOPERM), the
queue broker service's rule (:func:`levain.slot.services.qbroker.main`), before it parses argv or touches a store."""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import runtime_helper as H
from levain.slot.services import dispatch as SD

ROOT = Path(__file__).resolve().parents[1]
ME = os.geteuid()


def _dispatch_uid():
    try:
        return pwd.getpwnam("_levain_dispatch").pw_uid
    except KeyError:
        return None


@pytest.mark.skipif(ME == 0 or ME == _dispatch_uid(), reason="this test needs a user that is not the service account")
def test_the_program_as_this_user_exits_ex_noperm(tmp_path):
    sdir = tmp_path / "store"
    sdir.mkdir(mode=0o700)
    r = subprocess.run([sys.executable, "-m", "levain.slot.services.dispatch", "--store-dir", str(sdir),
                        "--socket", str(tmp_path / "c.sock"), "--export-dir", str(tmp_path / "export")],
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, cwd=str(ROOT), capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 77, r.stderr
    assert "must run as _levain_dispatch" in r.stderr
    assert not (sdir / SD.LOCK_NAME).exists()              # refused before the store was touched


def test_main_refuses_any_user_but_the_dispatch_account(monkeypatch):
    def missing(name):
        raise Refused("account-missing", name)

    real = H.Seams
    calls: list = []
    for uid in (lambda n: ME + 1, missing):
        monkeypatch.setattr(SD.H, "Seams", lambda uid=uid: real(account_uid=uid))
        assert SD.main(["--no-such-flag"], follow=calls.append) == SD.EX_WRONG_USER == 77
    monkeypatch.setattr(SD.H, "Seams", lambda: real(account_uid=lambda n: ME))
    monkeypatch.setattr(os, "geteuid", lambda: 0)            # root is one more user that is not the account
    assert SD.main([], follow=calls.append) == SD.EX_WRONG_USER
    assert calls == []
