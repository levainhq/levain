"""ONESURFACE OS-2, the consent family through the sheet: headless Chrome (its own profile, ``_drive``) on a real cockpit
server (make_server + a Signer on rpId localhost pinned to the page), a real authority log holding a laptop key and a
passkey enrolled for localhost, a real consent queue whose hook names that log, and Chrome's virtual authenticator holding
the passkey's private key. A tap in the sheet runs the phone port's own ceremony on the cockpit and the queue verifies the
signature (ssh-keygen and its passkey checks): the request ends ``allowed`` / ``denied`` in the queue itself.

Reproduced red before the build: ``/sign/consent/<id>`` was 404 and the Needs-you card's Allow copied a terminal line."""
from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from cryptography.hazmat.primitives import serialization

from levain.authority import anchor_path
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.keys import fingerprint_of
from levain.consent.answering import passkey_keys
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.sign import Signer
from levain.web_server import make_server
from tests._h4 import h4_home
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_sign_card import DESKTOP, TOK, _free_port, needs_ssh
from tests.test_cockpit_sign_origin import NS, _Passkey
from tests._phone_fixtures import _enrol

STATE = "document.getElementById('sheet').dataset.state"
NOTE = "document.getElementById('sheet-note').textContent"
FRAME = "document.getElementById('levain-sign-sheet-frame')"
IN = f"({FRAME} && {FRAME}.contentDocument)"


class _ConsentRig:
    """A consent request filed in ``LEVAIN_HOME/consent`` (the queue the cockpit's cards read and the Signer's), answered
    with a passkey enrolled for localhost in the cockpit its hook names."""

    def __init__(self, tmp_path: Path, monkeypatch: Any, *, tier: str = "T2") -> None:
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
        self.queue = ConsentQueue(self.qdir, authority=source, home=h4_home())
        self.rid = self.file(tier)
        self.port = _free_port()
        self.origin = f"http://localhost:{self.port}"
        self.signer = Signer(self.qdir, (NS,), "localhost",
                             lambda: {k.fingerprint: k for k in passkey_keys(source(), "localhost")})
        self.signer.page_origin = self.origin
        _root, self.src, _ids = _install(tmp_path)

    def file(self, tier: str = "T2", hold: timedelta = timedelta(minutes=30)) -> str:
        return self.queue.file(agent="claude_code", agent_session_id="s1", tool_name="Write",
                               tool_input={"file_path": "/w/CHANGELOG.md", "content": "x"}, cwd="/w", hold=hold,
                               tier=tier).id

    def state(self, rid: "str | None" = None) -> str:
        return self.queue.get(rid or self.rid)[1]

    def enc(self, rid: "str | None" = None) -> str:
        return urllib.parse.quote(rid or self.rid, safe="")

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
                                  "credentialId": base64.b64encode(b"levain-consent-cred").decode(),
                                  "userHandle": base64.b64encode(b"levain-consent-user").decode()}}

    def open_top(self, rid: "str | None" = None) -> list[dict]:
        """Put the cockpit's token in this browser's storage, then open the sheet top-level."""
        path = f"/sign/consent/{self.enc(rid)}"
        return [{"eval": f"(() => {{ window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
                         f"location.href = '{path}'; return 1; }})()"},
                {"until": f"location.pathname === '{path}' && {STATE} && {STATE} !== 'reading' && {STATE}", "ms": 30000}]


@needs_browser
@needs_ssh
def test_a_consent_answer_signs_in_the_sheet_and_the_queue_verifies_it(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "document.getElementById('sheet-title').textContent"},
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice).join(',')"},
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"allow\"]').click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "(document.getElementById('sheet-signing') || {}).textContent"},
        ])
    v = out["values"]
    assert v[1] == "open" and v[2].startswith("Write") and "T2" in v[2], v
    assert v[3] == "allow,deny", v
    assert str(v[5]).startswith("signed (allow) · by " + rig.pk.key.fingerprint), v
    assert str(v[6]).startswith("Signed: {"), v         # the server's line: the answer record's exact JCS text
    assert rig.state() == "allowed"                     # the queue itself verified it
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/consent/{rig.enc()}/challenge", f"/cockpit/consent/{rig.enc()}/answer"], posts


@needs_browser
@needs_ssh
def test_the_needs_card_allow_opens_the_sheet_with_allow_picked_and_one_tap_signs(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch)
    allow = "document.querySelector('#levain-modal .needs-consent [data-consent-sheet=\"allow\"]')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            # the decision controls live in the item's focus view: open it from the strip row first
            {"until": "document.querySelector('#ck-needs .needs-consent')", "ms": 30000},
            {"eval": "document.querySelector('#ck-needs .needs-consent').click() || 1"},
            {"until": f"{allow} && 1", "ms": 30000},
            {"eval": f"{allow}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"eval": f"{FRAME}.getAttribute('src')"},
            {"eval": f"!!{IN}.getElementById('sheet-picked') && {IN}.getElementById('sheet-picked').textContent"},
            {"eval": f"{IN}.getElementById('sheet-picked').click() || 1"},
            {"until": f"/^signed/.test({IN}.getElementById('sheet-note').textContent) && "
                      f"{IN}.getElementById('sheet-note').textContent", "ms": 30000},
        ])
    v = out["values"]
    assert v[4] == "open", v
    assert v[5].startswith(f"/sign/consent/{rig.enc()}#v=") and v[5].endswith("&choice=allow"), v
    assert v[6] == "Sign (allow)", v
    assert str(v[8]).startswith("signed (allow)"), v
    assert rig.state() == "allowed"


@needs_browser
@needs_ssh
def test_an_undecided_request_is_ruled_in_the_sheet(tmp_path: Path, monkeypatch) -> None:
    from levain.consent import delivery

    delivered: list[str] = []
    monkeypatch.setattr(delivery, "deliver", lambda queue, request_id, notify=None: delivered.append(request_id)
                        or type("D", (), {"route": "hook", "reason": "it reaches the session at its next step"})())
    rig = _ConsentRig(tmp_path, monkeypatch)
    rid = rig.file(hold=timedelta(microseconds=1))
    assert rig.queue.mark_undecided(rid).state == "undecided"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(rid),
            {"eval": "document.querySelector('#sheet-actions [data-choice=\"deny\"]').click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
        ])
    v = out["values"]
    # answered once recorded (L3 r1 C1): the line says its delivery is running, never waits for it
    assert str(v[3]).startswith("signed (deny)") and "delivery to the session is running" in v[3], v
    assert rig.state(rid) == "denied"
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/consent/{rig.enc(rid)}/rule-challenge", f"/cockpit/consent/{rig.enc(rid)}/rule"], posts


@needs_browser
@needs_ssh
def test_a_t3_request_offers_only_deny_and_says_where_it_is_allowed(tmp_path: Path, monkeypatch) -> None:
    rig = _ConsentRig(tmp_path, monkeypatch, tier="T3")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"eval": "[...document.querySelectorAll('#sheet-actions [data-choice]')].map(b => b.dataset.choice).join(',')"},
            {"eval": "document.getElementById('sheet-request-note').textContent"},
        ])
    v = out["values"]
    assert v[2] == "deny" and "laptop" in v[3], v
    assert rig.state() == "pending"


@needs_browser
@needs_ssh
def test_a_t3_cards_allow_never_opens_a_sheet_that_offers_only_deny(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 C3: a passkey cannot allow a T3 request, so the card's Allow keeps the terminal line (copied, as before the
    sheet) and only Deny opens the sheet: Allow must never open a sheet whose only option is Deny."""
    rig = _ConsentRig(tmp_path, monkeypatch, tier="T3")
    card = "document.querySelector('#levain-modal .needs-consent')"
    btn = card + ".querySelector('.needs-btn[data-label=\"{}\"]')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            # the decision controls live in the item's focus view: open it from the strip row first
            {"until": "document.querySelector('#ck-needs .needs-consent')", "ms": 30000},
            {"eval": "document.querySelector('#ck-needs .needs-consent').click() || 1"},
            {"until": f"{btn.format('Deny')} && {btn.format('Allow')} && 1", "ms": 30000},
            {"eval": f"{btn.format('Allow')}.dataset.consentSheet || ''"},
            {"eval": f"{btn.format('Deny')}.dataset.consentSheet || ''"},
            {"eval": f"{btn.format('Allow')}.click() || 1"},
            {"until": f"{FRAME} && {FRAME}.getAttribute('src')", "ms": 6000, "may_timeout": True},
            {"eval": f"{btn.format('Deny')}.click() || 1"},
            {"until": f"{FRAME} && {FRAME}.getAttribute('src')", "ms": 30000, "may_timeout": True},
        ])
    v = out["values"]
    assert (v[3], v[4]) == ("", "deny"), v          # Allow is the terminal line; Deny is the sheet's
    assert isinstance(v[6], dict) and "timeout" in v[6], v      # Allow opened no sheet
    assert str(v[8]).endswith("&choice=deny"), v
    assert rig.state() == "pending"
