"""ONESURFACE OS-2, the dispatch family through the sheet: headless Chrome (its own profile, ``_drive``) on a real cockpit
server (make_server + a Signer on rpId localhost pinned to the page), a real authority log holding a laptop key and a
passkey enrolled for localhost, a real dispatch job store in the Signer's queue directory, and Chrome's virtual
authenticator holding the passkey's private key. A tap in the sheet runs the phone port's own dispatch ceremony on the
cockpit and the job store verifies the signature (ssh-keygen and its passkey checks): the job ends ``signed`` /
``cancelled`` in the store itself.

Reproduced red before the build: ``/sign/dispatch/<job>`` was 404 and the Decisions view listed no dispatch job."""
from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cryptography.hazmat.primitives import serialization

from levain.authority import anchor_path
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent.answering import passkey_keys
from levain.consent.queue import authority_source
from levain.consent.sign import Signer
from levain.dispatch.jobs import JobStore
from levain.web_server import make_server
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_sign_card import DESKTOP, TOK, _free_port, needs_ssh
from tests.test_cockpit_sign_origin import NS, _Passkey
from tests.test_dispatch_phone import file_job, repo  # noqa: F401  (repo is a fixture)
from tests.test_phone_server import _enrol

STATE = "document.getElementById('sheet').dataset.state"
NOTE = "document.getElementById('sheet-note').textContent"
FRAME = "document.getElementById('levain-sign-sheet-frame')"
IN = f"({FRAME} && {FRAME}.contentDocument)"


class _DispatchRig:
    """A dispatch job filed in the Signer's queue directory, signed with a passkey enrolled for localhost in the cockpit
    that directory's hook names."""

    def __init__(self, tmp_path: Path, monkeypatch: Any, repo: Any) -> None:
        home = tmp_path / "levain_home"
        monkeypatch.setenv("LEVAIN_HOME", str(home))
        self.qdir = home / "consent"
        self.qdir.mkdir(parents=True, mode=0o700)
        laptop = tmp_path / "laptop"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(laptop)], check=True)
        lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
        lfp = fingerprint_of(lpub)
        root = tmp_path / "ck"
        root.mkdir()
        CockpitDB(root).set_install_class("reported")
        self.pk = _Passkey("localhost")
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": lfp, "public_key": lpub,
                                         "class": "laptop-presence", "custody": "passphrase"})
        _enrol(conn, root, laptop, lfp, {"op": "enrol", "fingerprint": self.pk.key.fingerprint,
                                         "public_key": self.pk.line, "class": "display-phone", "custody": "passkey"})
        conn.close()
        (self.qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                                "away_hold_seconds": 0}))
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
        self.job = file_job({"qdir": self.qdir, "repo": repo})
        self.port = _free_port()
        self.origin = f"http://localhost:{self.port}"
        self.signer = Signer(self.qdir, (NS,), "localhost",
                             lambda: {k.fingerprint: k for k in passkey_keys(source(), "localhost")})
        self.signer.page_origin = self.origin
        _root, self.src, _ids = _install(tmp_path)

    def state(self) -> str:
        return JobStore(self.qdir).get(self.job.id).state

    @contextmanager
    def serving(self) -> Iterator[Any]:
        httpd = make_server(self.src, host="127.0.0.1", port=self.port, surface_tokens={"browser": TOK},
                            install_class="reported", signer=self.signer)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)

    def authenticator(self) -> dict:
        pkcs8 = self.pk.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
        return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                                  "credentialId": base64.b64encode(b"levain-dispatch-cred").decode(),
                                  "userHandle": base64.b64encode(b"levain-dispatch-user").decode()}}

    def open_top(self) -> list[dict]:
        """Put the cockpit's token in this browser's storage, then open the sheet top-level."""
        path = f"/sign/dispatch/{self.job.id}"
        return [{"eval": f"(() => {{ window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
                         f"location.href = '{path}'; return 1; }})()"},
                {"until": f"location.pathname === '{path}' && {STATE} && {STATE} !== 'reading' && {STATE}", "ms": 30000}]


@needs_browser
@needs_ssh
def test_a_dispatch_job_is_signed_in_the_sheet_and_the_store_verifies_it(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "document.getElementById('sheet-title').textContent"},
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice).join(',')"},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"sign\"]').click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "(document.getElementById('sheet-signing') || {}).textContent"},
        ])
    v = out["values"]
    assert v[1] == "open" and v[2].startswith("dispatch"), v
    assert v[3] == "sign,cancel", v
    assert str(v[5]).startswith("signed (signed) · by " + rig.pk.key.fingerprint), v
    assert str(v[6]).startswith("Signed: {"), v          # the server's line: the payload's canonical JSON
    assert rig.state() == "signed"                       # the job store itself verified it
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/dispatch/{rig.job.id}/challenge", f"/cockpit/dispatch/{rig.job.id}/answer"], posts


@needs_browser
@needs_ssh
def test_a_dispatch_job_is_cancelled_in_the_sheet_with_its_reason(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "(() => { const b = document.getElementById('sheet-reason'); b.value = '  wrong repo  '; return 1; })()"},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"cancel\"]').click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
        ])
    v = out["values"]
    assert str(v[4]).startswith("signed (cancelled) · by " + rig.pk.key.fingerprint), v
    assert rig.state() == "cancelled"
    job = JobStore(rig.qdir).get(rig.job.id)
    assert any("wrong repo" in (h.evidence or "") or "wrong repo" in (h.cause or "") for h in job.history), job.history
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/dispatch/{rig.job.id}/cancel-challenge", f"/cockpit/dispatch/{rig.job.id}/cancel"], posts


@needs_browser
@needs_ssh
def test_the_decisions_view_lists_the_job_and_its_sign_button_opens_the_sheet_with_sign_picked(tmp_path: Path, monkeypatch, repo) -> None:  # noqa: F811
    rig = _DispatchRig(tmp_path, monkeypatch, repo)
    sign = "document.querySelector('[data-dispatch-job] [data-dispatch-act=\"sign\"]')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "!!(window.LevainNeeds && window.LevainNeeds.history)", "ms": 30000},
            {"eval": "window.LevainNeeds.history() || 1"},
            {"until": f"{sign} && 1", "ms": 30000},
            {"eval": f"{sign}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"eval": f"{FRAME}.getAttribute('src')"},
            {"eval": f"!!{IN}.getElementById('sheet-picked') && {IN}.getElementById('sheet-picked').textContent"},
            {"eval": f"{IN}.getElementById('sheet-picked').click() || 1"},
            {"until": f"/^signed/.test({IN}.getElementById('sheet-note').textContent) && "
                      f"{IN}.getElementById('sheet-note').textContent", "ms": 30000},
        ])
    v = out["values"]
    assert v[4] == "open", v
    assert v[5].startswith(f"/sign/dispatch/{rig.job.id}#v=") and v[5].endswith("&choice=sign"), v
    assert v[6] == "Sign (sign)", v
    assert str(v[8]).startswith("signed (signed)"), v
    assert rig.state() == "signed"
