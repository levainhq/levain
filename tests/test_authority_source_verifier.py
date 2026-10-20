"""A verifier that cannot run during the authority log's replay is a machine fault, never a refusal of the log:
:class:`levain.consent.queue.AuthoritySource` raises it as :class:`levain.authority.VerifierUnavailable`, so a
caller that tells the two apart (dispatch sign/cancel: exit 3 against exit 2) can."""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import AuthorityError, ReplayCache, VerifierUnavailable, anchor_path
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.firejournal import CockpitDB
from levain.consent.queue import authority_source


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A reported cockpit whose anchored log enrols one laptop-presence key."""
    key = tmp_path / "laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(key)],
                   check=True, capture_output=True, timeout=30)
    pub = " ".join(Path(f"{key}.pub").read_text().split()[:2])
    fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], check=True,
                        capture_output=True, text=True).stdout.split()[1]
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")

    def sign(m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", auth.NAMESPACE],
                              input=m, capture_output=True, check=True).stdout.decode()

    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        auth.sign_and_append(conn, {"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "laptop-presence",
                                    "custody": "passphrase"}, signer_fp=fp, sign=sign,
                             clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))
    finally:
        conn.close()
    return root


def test_a_verifier_that_cannot_run_raises_as_itself(root, tmp_path):
    source = authority_source(root / "cockpit.db", anchor=anchor_path(root), cache=ReplayCache(),
                              ssh_keygen=tmp_path / "no-ssh-keygen")
    with pytest.raises(VerifierUnavailable):
        source()


def test_a_refused_log_is_still_a_plain_refusal(root):
    anchor_path(root).write_text("not an anchor\n")
    source = authority_source(root / "cockpit.db", anchor=anchor_path(root), cache=ReplayCache())
    with pytest.raises(AuthorityError) as got:
        source()
    assert not isinstance(got.value, VerifierUnavailable)


def test_capture_still_raises_nothing_and_names_the_verifier(root, tmp_path):
    conn = sqlite3.connect((root / "cockpit.db").as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute("BEGIN")
        got = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=anchor_path(root),
                                       cache=ReplayCache(), ssh_keygen=tmp_path / "no-ssh-keygen")
    finally:
        conn.close()
    assert got.authority is None and got.verifier_unavailable
    assert "could not be verified" in got.authority_error


def test_a_good_log_replays(root):
    assert authority_source(root / "cockpit.db", anchor=anchor_path(root), cache=ReplayCache())() is not None
