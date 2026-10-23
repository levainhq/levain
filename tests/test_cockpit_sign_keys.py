"""S1b (DESIGN_SIGN, head 1023+21): the keys a sign request is signed with, and the words a refusal answers with, are
ONE implementation the phone port and the cockpit both call: :func:`levain.consent.sign.passkey_key_source` and
:func:`levain.consent.sign.error_word`. Real ``ssh-keygen`` signs the laptop's enrol links; the passkey is the
dispatch tests' software authenticator."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from levain import web_server
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent import sign
from levain.consent.sign import SignRefused, passkey_key_source
from levain.phone_server import PhoneError, make_phone_server
from tests.test_dispatch_phone import SoftPasskey
from tests.test_phone_server import _enrol

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

RP = "phone.example"


def _cockpit(tmp_path: Path, name: str, rp: str = RP) -> "tuple[Path, SoftPasskey]":
    """A cockpit at ``tmp_path/name`` with a laptop key and one passkey made on ``rp`` enrolled."""
    laptop = tmp_path / f"{name}-laptop"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
    lpub = " ".join(Path(f"{laptop}.pub").read_text().split()[:2])
    lfp = fingerprint_of(lpub)
    root = tmp_path / name
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    phone = SoftPasskey(rp)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": lfp, "public_key": lpub,
                                     "class": "laptop-presence", "custody": "passphrase"})
    _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": phone.fp, "public_key": phone.line,
                                     "class": "display-phone", "custody": "passkey"})
    conn.close()
    return root, phone


def _hook(qdir: Path, root: Path, adapter: str = "claude_code") -> None:
    (qdir / f"{adapter}.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))


@pytest.fixture
def qdir(tmp_path: Path) -> Path:
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    os.chmod(q, 0o700)
    return q


def _phone_keys(qdir: Path) -> "dict[str, Any]":
    srv = make_phone_server(qdir, RP, port=0)
    try:
        return srv._sign_keys()
    finally:
        srv.server_close()


def _refusal(fn: Any) -> SignRefused:
    with pytest.raises(SignRefused) as refused:
        fn()
    return refused.value


def test_the_factory_and_the_phone_port_return_the_same_passkeys(tmp_path: Path, qdir: Path) -> None:
    root, phone = _cockpit(tmp_path, "cockpit")
    _hook(qdir, root)
    keys = passkey_key_source(qdir, RP)()
    assert list(keys) == [phone.fp]                       # the passkey, never the laptop key
    assert keys[phone.fp].public_key == phone.line
    assert _phone_keys(qdir) == keys
    assert passkey_key_source(qdir, "other.example")() == {}     # a passkey is made on one name


def test_the_source_rereads_the_hooks_on_every_call_and_refuses_what_is_not_one_cockpit(tmp_path: Path,
                                                                                        qdir: Path) -> None:
    keys = passkey_key_source(qdir, RP)
    for fn in (keys, lambda: _phone_keys(qdir)):
        r = _refusal(fn)
        assert r.status == 503 and "no consent hook names a cockpit" in str(r)
    root, phone = _cockpit(tmp_path, "cockpit")
    _hook(qdir, root)
    assert list(keys()) == [phone.fp]                     # the same source, the hook written after it was made
    other, _ = _cockpit(tmp_path, "other")
    _hook(qdir, other, "codex")
    for fn in (keys, lambda: _phone_keys(qdir)):
        r = _refusal(fn)
        assert r.status == 503 and "more than one cockpit" in str(r)
    (qdir / "codex.json").unlink()
    (root / "cockpit.db").write_bytes(b"not a database")
    for fn in (keys, lambda: _phone_keys(qdir)):
        r = _refusal(fn)
        assert r.status == 503 and "cannot be read now" in str(r)


def test_one_error_word_table_answers_for_the_phone_port_and_the_cockpit(qdir: Path, monkeypatch) -> None:
    """Both ports read the table when they answer: a word changed in it changes what each one says."""
    from types import SimpleNamespace

    assert not hasattr(web_server, "_SIGN_ERRORS")
    monkeypatch.setattr(sign, "ERROR_WORDS", {**sign.ERROR_WORDS, 409: "conflict-x"})
    srv = make_phone_server(qdir, RP, port=0)
    try:
        for status in (400, 404, 409, 503, 422, 403):
            def refuse(_rid: str, _req: Any, status: int = status) -> Any:
                raise SignRefused("no", status)

            word = sign.error_word(status)
            assert word == {409: "conflict-x", 422: "sign_refused", 403: "sign_refused"}.get(status, word)
            srv.signer.challenge = refuse              # type: ignore[method-assign]
            with pytest.raises(PhoneError) as refused:
                srv.sign_step("np-sign-0001", "challenge", {})
            assert (refused.value.status, refused.value.error) == (status, word)
            fake = SimpleNamespace(server=SimpleNamespace(signer=SimpleNamespace(challenge=refuse)))
            body, code = web_server._Handler._sign_post(fake, "np-sign-0001", "challenge", {})  # type: ignore[arg-type]
            assert (code, body["error"]) == (status, word)
    finally:
        srv.server_close()
