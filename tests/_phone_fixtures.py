"""Test helpers the cockpit's consent, sign, dispatch and review tests share (moved here from ``tests/test_phone_server.py``
when the phone port's token routes retired, W1b slice C): a cockpit whose authority log holds a laptop key and the
fixture's passkey, the enrol helper, and the passkey fixture's public key. None of them starts a phone server."""
from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent.passkey import public_key_line
from levain.consent.queue import ConsentQueue, authority_source
from tests._h4 import h4_home

_FIX = json.loads((Path(__file__).parent / "fixtures" / "passkey_assertion_virtual.json").read_text(encoding="utf-8"))
_SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + bytes.fromhex(_FIX["pub_point_hex"])


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _enrol(conn: sqlite3.Connection, root: Path, laptop: Path, lfp: str, op: dict) -> None:
    auth.sign_and_append(
        conn, op, signer_fp=lfp,
        sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(laptop), "-n", auth.NAMESPACE],
                                      input=m, capture_output=True, check=True).stdout.decode(),
        clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))


@pytest.fixture
def world(tmp_path: Path):
    """A cockpit whose authority log holds a laptop key and the fixture's passkey (rpId localhost, display-phone,
    custody passkey), and a consent queue whose claude_code hook verifies against it."""
    laptop = tmp_path / "laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
    lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    lfp = fingerprint_of(lpub)
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    passkey = public_key_line(_SPKI, "localhost")
    pfp = fingerprint_of(passkey)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": lfp, "public_key": lpub,
                                     "class": "laptop-presence", "custody": "passphrase"})
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": pfp, "public_key": passkey,
                                     "class": "display-phone", "custody": "passkey"})
    conn.close()
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))
    queue = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)), home=h4_home())
    seq = iter(range(1, 100))

    def file(tier: str = "T2") -> str:
        n = next(seq)
        return queue.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                          tool_input={"file_path": f"/tmp/phone{n}.txt", "content": "hi"}, cwd="/w",
                          hold=timedelta(minutes=5), tier=tier, event_id=f"phone-{tier}-{n}").id

    return {"qdir": qdir, "root": root, "passkey_fp": pfp, "passkey": passkey, "file": file}
