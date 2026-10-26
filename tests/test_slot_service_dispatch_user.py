"""The dispatch service runs only as ``_levain_dispatch``. Root exits 78 (EX_CONFIG) first, which the Linux unit does
not restart; any other euid but the account's exits 77 (EX_NOPERM), the queue broker service's rule
(:func:`levain.slot.services.qbroker.main`). Both come before it parses argv or touches a store."""
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
#: a non-root euid the in-process tests fake, so they hold whoever runs pytest, root included
EUID = 50001


def _dispatch_uid():
    try:
        return pwd.getpwnam("_levain_dispatch").pw_uid
    except KeyError:
        return None


def test_the_program_as_this_user_exits_ex_noperm(tmp_path):
    me = os.geteuid()
    if me == 0 or me == _dispatch_uid():
        pytest.skip("this test needs a user that is neither root nor the service account")
    sdir = tmp_path / "store"
    sdir.mkdir(mode=0o700)
    r = subprocess.run([sys.executable, "-m", "levain.slot.services.dispatch", "--store-dir", str(sdir),
                        "--socket", str(tmp_path / "c.sock"), "--export-dir", str(tmp_path / "export")],
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, cwd=str(ROOT), capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 77, r.stderr
    assert "must run as _levain_dispatch" in r.stderr
    assert not (sdir / SD.LOCK_NAME).exists()              # refused before the store was touched


def _store(tmp_path: Path) -> tuple[Path, list]:
    sdir = tmp_path / "store"
    sdir.mkdir(mode=0o700)
    return sdir, ["--store-dir", str(sdir), "--socket", "/x/c.sock", "--export-dir", "/x/e"]


def test_main_refuses_any_user_but_the_dispatch_account(monkeypatch, tmp_path, capfd):
    def missing(name):
        raise Refused("account-missing", name)

    sdir, argv = _store(tmp_path)
    real = H.Seams
    calls: list = []
    monkeypatch.setattr(os, "geteuid", lambda: EUID)
    for uid in (lambda n: EUID + 1, missing):
        monkeypatch.setattr(SD.H, "Seams", lambda uid=uid: real(account_uid=uid))
        assert SD.main(argv, follow=calls.append) == SD.EX_WRONG_USER == 77
    assert "must run as _levain_dispatch" in capfd.readouterr().err
    assert SD.main(["--no-such-flag"], follow=calls.append) == SD.EX_WRONG_USER     # before argv is parsed
    assert calls == [] and not (sdir / SD.LOCK_NAME).exists()


@pytest.mark.parametrize("account", (EUID, 0))
def test_root_exits_ex_config_before_the_account_rule(monkeypatch, tmp_path, capfd, account):
    """Root stays down on Linux (78 is in the unit's RestartPreventExitStatus), whichever uid the account resolves to:
    an account resolved to uid 0 never reaches the account comparison (L3 r1, codex)."""
    sdir, argv = _store(tmp_path)
    real = H.Seams
    calls: list = []
    monkeypatch.setattr(SD.H, "Seams", lambda: real(account_uid=lambda n: account))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert SD.main(argv, follow=calls.append) == SD.EX_CONFIG == 78
    err = capfd.readouterr().err
    assert "refusing to run as root" in err and "error:" not in err
    assert calls == [] and not (sdir / SD.LOCK_NAME).exists()
