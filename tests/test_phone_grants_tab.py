"""M17 S-b, the phone half: a Grants tab in phone.js over the ``/grant`` routes (S-a), revoking with the passkey.

The page is driven in node (``tests/_phone_grants_drive.js``) over a small DOM that keeps a real tree. The scripted
drive pins the page's own rules (the grant with a refused call first and its refused call first, the frozen link shown
before any passkey prompt, only the sheet's tap asks for Face ID, the revoke sends the assertion and nothing else, a
link that names another grant is never signed). The live drive runs the same page against a real phone port: its fetch
reaches the server, a software P-256 authenticator signs, and the revoke lands as a link the authority log's replay
verifies with the real ``ssh-keygen``."""
from __future__ import annotations

import json
import re
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
from tests.test_cockpit_flip_parity import _drive as _page_drive, needs_browser
from tests.test_grant_phone import ORIGIN, RP, _now, world  # noqa: F401  (world is a fixture)
from tests._h4 import h4_home

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

    # the platform's prompt is named by platform: Face ID on an iPhone, "your screen lock" on Android and wherever the
    # page cannot tell (a missing or empty user agent), and never both
    pf = got["platform"]
    assert "revoke with Face ID" in pf["ios"]["buttons"] and "Face ID signs exactly this authority link" in pf["ios"]["text"], pf
    for other in ("android", "none"):
        assert "revoke with your screen lock" in pf[other]["buttons"], pf[other]
        assert "Your screen lock signs exactly this authority link" in pf[other]["text"], pf[other]
        assert "Face ID" not in pf[other]["text"] and not any("Face ID" in b for b in pf[other]["buttons"]), pf[other]

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


@needs_node
def test_the_l3_r1_fixes_hold_on_the_grants_tab():
    """M17 L3 r1 M-1, M-3..M-9. Each scenario reproduced its finding on 7ae3def0's phone.js
    (``node tests/_phone_grants_drive.js <7ae3def0 phone.js> scripted``, saved as m17/fix/red_drive_on_7ae3def0.json):
    bound/noprev signed (webauthn 1, the revoke posted); viewMid/viewSheet kept the sheet and held the list; failRead
    kept the old status and enabled buttons; noToken left the status empty; refusedCount neither led nor flagged OLD;
    badRaw threw InvalidCharacterError out of the tap and left the sheet up; notes carried the old note to the new
    instance; cancelKeep forgot the mapping; nullCred said "result unknown"."""
    got = _drive("scripted")
    # M-1: Face ID is asked for the challenge the page recomputed from the line it shows, and nothing else
    c = got["ceremony"]
    assert c["challenge"] == c["want"] and c["webauthn"] == 1, c
    for name, why in (("bound", "not over the link it shows"), ("noprev", "which log head")):
        b = got[name]
        assert b["webauthn"] == 0 and not b["faceId"] and b["posts"] == ["/grant/cg-bbbbbbbbbbbb/revoke-challenge"], b
        assert any(why in t and "nothing was signed" in t for t in b["notes"]), b
    # M-3: leaving the view while the challenge is in flight, or with the sheet up, drops the ask and frees the list
    assert got["viewMid"] == {"sheet": 0, "webauthn": 0, "moved": True, "posts": 1}, got["viewMid"]
    assert got["viewSheet"] == {"up": 1, "sheet": 0, "webauthn": 0, "moved": True}, got["viewSheet"]
    # M-4: a failed read is said on the Grants tab and turns revoke off until a good read; no token is said too
    f = got["failRead"]
    assert f["before"] is False and f["after"] is False and f["during"]["disabled"] is True, f
    assert f["during"]["status"].startswith("the grants could not be read: this page's token is needed"), f
    assert "no token" in got["noToken"]["status"] and got["noToken"]["gets"] == 0, got["noToken"]
    # M-5: the server's count flags and orders a grant whose refusals are older than the calls it sends
    r = got["refusedCount"]
    assert r["order"] == ["cg-aaaaaaaaaaaa", "cg-cccccccccccc"] and r["demote"] is True, r
    assert "1 with a refused call" in r["status"] and "2 refused, 2 of them older than the calls below" in r["text"], r
    # M-6: a credential id that does not decode: the sheet goes, nothing is put to the passkey, the list moves again
    k = got["badRaw"]
    assert k["threw"] == "" and k["sheet"] == 0 and k["webauthn"] == 0 and k["moved"], k
    assert k["posts"] == ["/grant/cg-bbbbbbbbbbbb/revoke-challenge"] and "nothing was signed" in k["notes"][0], k
    # M-7: the note said about one instance is not carried to the grant made again under the same id
    assert got["notes"]["first"][0].startswith("not revoked") and got["notes"]["renewed"] == [""], got["notes"]
    # M-8: a cancelled Face ID keeps this browser's passkey mapping
    fp = "SHA256:phonekeyphonekeyphonekeyphonekeyphonekey0"
    assert got["cancelKeep"]["mapping"] == {fp: "cmF3aWQ"}, got["cancelKeep"]
    assert "nothing was revoked" in got["cancelKeep"]["notes"][0], got["cancelKeep"]
    # M-9: a passkey that gives nothing: nothing is sent, and the page says nothing was revoked (not "unknown")
    n = got["nullCred"]
    assert n["posts"] == ["/grant/cg-bbbbbbbbbbbb/revoke-challenge"] and "nothing was revoked" in n["notes"][0], n
    assert not n["notes"][0].startswith("result unknown"), n


_SHEET = "document.querySelectorAll('#grants-list .sheet').length"


@needs_browser
def test_a_tab_switch_in_the_phone_app_drops_an_open_revoke_sheet(world, tmp_path):
    """M17 L3 r1 M-3, through the real tabs (phone_app.js's show) in headless Chrome against a real phone port.
    Reproduced red on 7ae3def0's phone.js + phone_app.js: after the Board tap the sheet was still in the hidden
    Grants view (1), and back on Grants the card's revoke button was still disabled (the list held)."""
    _grant(world, "cg-0000000000aa", "Write")
    httpd = make_phone_server(world.qdir, RP, port=0, token=_TOK, dispatch_dir=world.qdir)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/?view=grants#phone_token={_TOK}"
        out = _page_drive(tmp_path, url, [
            {"until": "document.querySelectorAll('#grants-list article').length === 1"},
            {"eval": "document.querySelector('#grants-list article .deny').click(), 1"},
            {"until": "!!document.querySelector('#grants-list .sheet pre.line')"},
            {"eval": "document.querySelector('#tabs button[data-view=\"board\"]').click(), 1"},
            {"eval": f"new Promise(r => setTimeout(() => r({_SHEET}), 300))"},
            {"eval": "document.querySelector('#tabs button[data-view=\"grants\"]').click(), 1"},
            {"eval": "new Promise(r => setTimeout(() => r(document.querySelector('#grants-list article .deny').disabled), 2500))"},
        ])
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    sheet_after_leave, button_disabled = out["values"][-3], out["values"][-1]
    assert sheet_after_leave == 0 and button_disabled is False, out["values"]
    # nothing was signed: the only write was the challenge
    assert [p for p in out["posts"] if "/grant/" in str(p)] and all("revoke-challenge" in str(p)
                                                                    for p in out["posts"] if "/grant/" in str(p)), out


def _grant(world, gid: str, tool: str) -> None:
    create_grant(world.root, agent="claude_code", tools=[tool], workspace=world.ws, postures=["default"],
                 expires=timedelta(days=1), signer_fp=world.lfp, sign=world.laptop_sign, clock=_now, grant_id=gid)


def _answered(world, tool: str, name: str, outcome: str) -> str:
    """One call a standing grant answered at the gate, then ran (``accepted``) or was refused after the allow."""
    source = authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root))
    queue = ConsentQueue(world.qdir, authority=source, home=h4_home())
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


@needs_node
def test_no_passkey_ceremony_on_the_page_forgets_its_passkey_on_a_refusal():
    """1022+19 Q2: the M-8 "don't forget" rule holds on every navigator.credentials.get ceremony, through one helper.
    Reproduced red on the merge 182e43f6: the consent answer, the late ruling, the sign request, the dispatch contract
    and the question's push each emptied this browser's mapping on NotAllowedError. (The revoke is cancelKeep above;
    the cockpit write's signPending is pinned in test_cockpit_k8a_phone_authority.)"""
    got = _drive("scripted")["keepAll"]
    assert set(got) == {"consent", "ruling", "sign", "dispatch", "question"}, got
    for name, r in got.items():
        assert r["tapped"] and r["webauthn"] == 1, (name, r)
        assert r["mapping"] == {"SHA256:phonekeyphonekeyphonekeyphonekeyphonekey0": "cmF3aWQ"}, (name, r)
        assert any(t.startswith("the passkey did not sign (NotAllowedError); ") for t in r["said"]), (name, r)
    # one rule, one home: no ceremony keeps its own copy of the forget. The behavioural pin is the loop above (every
    # ceremony's refusal leaves the mapping intact); this grep only guards the helper's removal
    src = PHONE_JS.read_text()
    assert not re.search(r"\bforget\s*[(=]", src), "a forget is back"


# an open K8 review (window.levainSign) on the real page: GET /cockpit/pending/<id> answers a WAITING pending from the
# page's own fetch (no cockpit write is filed), the review records what phone.js tells it, and a probe asks whether a
# second write would be filed now (it is only when the page is not held)
_OPEN_REVIEW = """(() => {
  const real = window.fetch.bind(window);
  const k = window.__k = { reviewed: 0, issued: 0, closed: [], done: [], said: [] };
  window.fetch = (route, init) => /^\\/cockpit\\/pending\\//.test(String(route))
    ? Promise.resolve(new Response(JSON.stringify({ state: "WAITING", verb: "spore_update", tier: "C1", challenge: "AAAA",
                                                    rp_id: location.hostname }),
                                   { status: 200, headers: { "Content-Type": "application/json" } }))
    : real(route, init);
  k.issue = () => { k.issued++; return Promise.resolve({ ok: true, status: 200,
                                                         body: { state: "WAITING", pending_id: "cp" + "0".repeat(32) } }); };
  const say = (t, kind) => k.said.push([t, kind || ""]);
  window.levainSign(["SHA256:onlyone"], say, document.createElement("div"), k.issue,
                    () => { k.reviewed++; return (why) => k.closed.push(why); }, (a) => k.done.push(a));
  k.probe = () => { const before = k.issued;
    window.levainSign(["SHA256:onlyone"], () => {}, document.createElement("div"), k.issue, (_b, _s, d) => d(), () => {});
    return new Promise((r) => setTimeout(() => r(k.issued > before), 200)); };
  return 1;
})()"""


@needs_browser
def test_a_tab_switch_in_the_phone_app_dismisses_an_open_review(world, tmp_path):
    """L3 r4 (codex MED): the view switch phone_app.js's show(v) makes, reached by a real tab tap in headless Chrome,
    dismisses an open K8 review through levainViewChanged -> levainDismissReviews. Red with that one call removed from
    levainViewChanged (m17k8a/wire_red.txt): the page stayed held, the sheet was never told and done never answered."""
    httpd = make_phone_server(world.qdir, RP, port=0, token=_TOK, dispatch_dir=world.qdir)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/?view=needs#phone_token={_TOK}"
        out = _page_drive(tmp_path, url, [
            {"until": "typeof window.levainSign === 'function' && !!document.querySelector('#tabs button[data-view=\"board\"]')"},
            {"eval": _OPEN_REVIEW},
            {"until": "window.__k.reviewed === 1"},
            {"eval": "window.__k.probe()"},
            {"eval": "document.querySelector('#tabs button[data-view=\"board\"]').click(), 1"},
            {"eval": "new Promise(r => setTimeout(() => r(JSON.stringify({ closed: window.__k.closed, "
                     "done: window.__k.done.map(a => a.body && a.body.state), said: window.__k.said.slice(-1) })), 300))"},
            {"eval": "window.__k.probe()"},
        ])
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    held_before, told, free_after = out["values"][3], json.loads(out["values"][5]), out["values"][6]
    assert held_before is False, out["values"]          # the open review holds the page: a second write is not filed
    assert told["closed"] == ["dismissed"] and told["done"] == ["DISMISSED"], told
    assert "dismissed" in told["said"][0][0], told
    assert free_after is True, out["values"]
