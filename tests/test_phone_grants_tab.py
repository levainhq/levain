"""M17 S-b, the phone half: a Grants tab in phone.js over the ``/grant`` routes (S-a), revoking with the passkey.

The page is driven in node (``tests/_phone_grants_drive.js``) over a small DOM that keeps a real tree. The scripted
drive pins the page's own rules (the grant with a refused call first and its refused call first, the frozen link shown
before any passkey prompt, only the sheet's tap asks for Face ID, the revoke sends the assertion and nothing else, a
link that names another grant is never signed). The live drive runs the same page against a real phone port: its fetch
reaches the server, a software P-256 authenticator signs, and the revoke lands as a link the authority log's replay
verifies with the real ``ssh-keygen``."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from levain import authority as auth
from levain.consent.grants import create_grant, grant_listing
from levain.consent.ledger import Ledger
from levain.consent import policy
from levain.consent.queue import ConsentQueue, authority_source
from levain.phone_server import make_phone_server
from tests.test_grant_phone import ORIGIN, RP, _now, world  # noqa: F401  (world is a fixture)

HERE = Path(__file__).resolve().parent
PHONE_JS = HERE.parent / "levain" / "templates" / "web" / "phone.js"
PHONE_HTML = HERE.parent / "levain" / "templates" / "web" / "phone.html"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
_TOK = "phone-token-for-the-grants-drive"


def _drive(*args: str) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_phone_grants_drive.js"), str(PHONE_JS), *args],
                        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def test_the_phone_page_has_a_grants_tab_and_its_view():
    html = PHONE_HTML.read_text()
    assert 'id="tab-grants" data-view="grants"' in html
    assert 'id="grants"' in html and 'id="grants-status"' in html and 'id="grants-list"' in html


@needs_node
def test_the_grants_tab_shows_refused_first_and_signs_only_the_link_it_showed():
    """Reproduced red on the base phone.js (98ae455e): phone.js drew no grant card, so the drive had none to tap
    ("TypeError: Cannot read properties of null (reading 'querySelectorAll')" at the ceremony's revoke tap)."""
    got = _drive("scripted")
    r = got["render"]
    # the grant with a refused call leads the list, and inside it the refused call leads its calls
    assert r["order"] == ["cg-bbbbbbbbbbbb", "cg-aaaaaaaaaaaa"], r
    assert r["calls"][0].startswith("refused") and r["refusedMarked"] == 1, r
    assert "answered 7" in r["text"] and "newest 3 of 7" in r["text"], r
    assert r["revoke"] and not r["disabled"], r
    assert "2 standing grants" in r["status"] and "1 with a refused call" in r["status"], r

    c = got["ceremony"]
    # the challenge names the grant's instance as listed, with the passkey this browser made
    assert c["posts"][0] == ["/grant/cg-bbbbbbbbbbbb/revoke-challenge",
                             {"signer_fp": "SHA256:phonekeyphonekeyphonekeyphonekeyphonekey0", "instance": "4:bbbb"}], c
    # the frozen link is on the card before any passkey prompt, and Face ID waits for its own tap
    sb = c["shownBefore"]
    assert sb["webauthn"] == 0 and sb["faceId"] and len(sb["line"]) == 1, sb
    assert json.loads(sb["line"][0])["op"] == "revoke_grant" and json.loads(sb["line"][0])["grant_id"] == "cg-bbbbbbbbbbbb"
    assert c["heldStill"], c
    assert c["webauthn"] == 1 and c["allow"] == 1 and c["uv"] == "required" and c["rpId"] == "phone.example", c
    # the revoke carries the assertion and nothing else: no link, no grant data from the page
    route, body = c["posts"][1]
    assert route == "/grant/cg-bbbbbbbbbbbb/revoke" and set(body) == {"authenticator_data", "client_data_json", "signature"}, c
    assert any(t.startswith("revoked") and "seq 9" in t for t in c["notes"]), c
    assert "revoked cg-bbbbbbbbbbbb" in c["status"], c

    m = got["mismatch"]
    assert m["webauthn"] == 0 and not m["faceId"] and m["posts"] == ["/grant/cg-bbbbbbbbbbbb/revoke-challenge"], m
    assert any("does not name this grant" in t for t in m["notes"]), m
    assert any(t.startswith("not revoked") and "revoke again" in t for t in got["stale"]["notes"]), got["stale"]
    assert any(t.startswith("result unknown") for t in got["unknown"]["notes"]), got["unknown"]
    k = got["cancel"]
    assert k["webauthn"] == 0 and k["posts"] == ["/grant/cg-bbbbbbbbbbbb/revoke-challenge"] and k["moved"] and k["sheet"] == 0, k
    assert got["hidden"] == {"sheet": 0, "posts": 1}, got["hidden"]
    n = got["nosigner"]
    assert n["disabled"] is True and any("localhost" in t for t in n["notes"]), n
    assert got["empty"]["cards"] == 0 and "no standing grants" in got["empty"]["status"], got["empty"]


def _grant(world, gid: str, tool: str) -> None:
    create_grant(world.root, agent="claude_code", tools=[tool], workspace=world.ws, postures=["default"],
                 expires=timedelta(days=1), signer_fp=world.lfp, sign=world.laptop_sign, clock=_now, grant_id=gid)


def _answered(world, tool: str, name: str, outcome: str) -> str:
    """One call a standing grant answered at the gate, then ran (``accepted``) or was refused after the allow."""
    source = authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root))
    queue = ConsentQueue(world.qdir, authority=source)
    tool_input = {"file_path": str(world.ws / name), "content": "x"}
    r = queue.file(agent="claude_code", agent_session_id="s1", tool_name=tool, tool_input=tool_input, cwd=str(world.ws),
                   hold=timedelta(minutes=5))
    d = policy.guarded_policy(r, queue, posture="default", tool_input=tool_input)
    assert d is not None and d.via == "grant", "the grant did not answer"
    ledger = Ledger(world.qdir)
    common = dict(agent="claude_code", agent_session_id="s1", tool_name=tool, tool_input=tool_input, cwd=str(world.ws),
                  tool_use_id="tu-" + name)
    ledger.record_call(**common, phase="pre", stamp="grant", request_id=r.id)
    if outcome == "refused":
        ledger.record_call(**common, phase="post_failure", stamp="unknown", outcome="refused")
    else:
        ledger.record_call(**common, phase="post", stamp="grant")
    return d.grant_id


@needs_node
def test_the_page_revokes_a_grant_through_a_real_phone_port_and_the_log_takes_it(world):
    """End to end: phone.js's fetch reaches a real phone port; the revoke the page signs lands as a link the replay
    verifies (real ssh-keygen over a webauthn-sk SSHSIG in levain-authority)."""
    _grant(world, "cg-0000000000aa", "Write")
    _grant(world, "cg-0000000000bb", "Edit")
    assert _answered(world, "Write", "a.txt", "accepted") == "cg-0000000000aa"
    assert _answered(world, "Edit", "b.txt", "accepted") == "cg-0000000000bb"
    assert _answered(world, "Edit", "c.txt", "refused") == "cg-0000000000bb"
    pem = world.root.parent / "phone-key.pem"
    pem.write_bytes(world.phone.priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
    httpd = make_phone_server(world.qdir, RP, port=0, token=_TOK, dispatch_dir=world.qdir)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        got = _drive("live", f"http://127.0.0.1:{httpd.server_address[1]}", _TOK, str(pem), RP, ORIGIN, "cg-0000000000bb")
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    # the grant whose call was refused led the list, its refused call first
    assert got["before"]["order"] == ["cg-0000000000bb", "cg-0000000000aa"], got
    assert got["before"]["calls"][0].startswith("refused · Edit"), got
    line = json.loads(got["line"][0])
    assert line["op"] == "revoke_grant" and line["grant_id"] == "cg-0000000000bb" and got["promptsBeforeTap"] == 0, got
    assert any(t.startswith("revoked") for t in got["said"]) or "revoked cg-0000000000bb" in got["status"], got
    assert got["after"] == ["cg-0000000000aa"], got
    conn = sqlite3.connect(world.root / "cockpit.db", isolation_level=None)
    try:
        snap = auth.snapshot(conn, cache=auth.ReplayCache())     # a fresh replay verifies every link again
    finally:
        conn.close()
    assert "cg-0000000000bb" not in snap.policy.grants and "cg-0000000000aa" in snap.policy.grants
    assert snap.seq == line["seq"]
    assert [g.id for g in grant_listing(world.root, now=_now())[0]] == ["cg-0000000000aa"]
