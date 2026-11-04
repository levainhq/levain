"""CH-H1 H1-3, the glass half: the head card on the drop sheet (sign.js, ``/sign/drop#thread=<id>``) and the thread view's link.

Two kinds of test, and neither is the device. The structure tests read the page's source: the six draft-* routes it posts, the
fragment read before the bar is cleaned, no text field of its own. The render test runs the REAL sign.js in headless Chrome
against a STUB cockpit (a canned server speaking the draft-* reply shapes of ``levain.coordinator.draft_sign``; the real
handlers are tested in tests/test_coordinator_draft_sign.py) with a virtual authenticator: it proves the order of the page's
calls, that Sign mints the challenge at the tap, and what the page draws; it proves nothing about the real cards' wording on
glass, or Phill's passkey on his own device (spore-1066)."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_coord_drop import _undeclared_assignments

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "levain" / "templates" / "web"
SIGN = (WEB / "sign.js").read_text()
HEAD = SIGN[SIGN.index("// ---- the head card of a thread"):SIGN.index("    form.start();\n    // a drop kept from")]
ACTIONS = ("draft-cards", "draft-rule", "draft-withdraw", "draft-review", "draft-challenge", "draft-accept")


def test_the_sheet_posts_each_draft_route_and_reads_the_fragment_before_the_bar_is_cleaned() -> None:
    for a in ACTIONS:
        assert f'"{a}"' in HEAD, a
    # the fragment is taken out of the bar by one replaceState; `thread=` is read before it
    assert SIGN.index("const tm = ") < SIGN.index('history.replaceState(null, "", location.pathname)')
    assert 'location.pathname === "/sign/drop" && tm' in SIGN
    assert "thread=([A-Za-z0-9._:-]{1,160})" in SIGN


def test_sign_mints_its_challenge_at_the_tap_and_the_accept_follows_the_passkey() -> None:
    sign = HEAD[HEAD.index("const hsign = () =>"):HEAD.index("function hdrawCard")]
    assert sign.index('post("draft-challenge"') < sign.index("navigator.credentials.get") < sign.index('post("draft-accept"')
    # the review's challenge is NOT asked for when the review is drawn
    review = HEAD[HEAD.index("const hreviewTap"):HEAD.index("const hdone")]
    assert "draft-challenge" not in review and "navigator.credentials" not in review
    # the signed body is the server's own review text, and the key is held to the one reviewed
    assert "body_text: r.body_text" in sign and "k.fp !== r.fp" in sign


def test_lapsed_offers_extend_and_revoked_replace_and_nothing_offers_a_verb_the_server_lacks() -> None:
    assert 'card.kind === "LAPSED" && has(card, "extend")' in HEAD and 'hrule(card, "renew")' in HEAD
    assert 'card.kind === "REVOKED" && has(card, "replace")' in HEAD and 'hrule(card, "replace")' in HEAD
    assert '"close"' not in HEAD       # no draft-side close verb exists to call


def test_the_head_card_adds_no_text_field_so_there_is_no_name_to_classify() -> None:
    """Safari's AutoFill classifier reads a field's name (tests/test_drop_box_is_prose.py). The card has no input of its own; a
    field added later must carry a dash-free name, and this test is where that is made to be noticed."""
    assert not re.search(r'el\("(input|textarea|select)"', HEAD)
    for name in re.findall(r'setAttribute\("name", "([^"]*)"\)|\["name", "([^"]*)"\]', HEAD):
        assert "-" not in "".join(name), name


def test_the_thread_view_links_the_card_by_a_safe_id_only() -> None:
    tv = (WEB / "thread_view.js").read_text()
    assert '"/sign/drop#thread=" + encodeURIComponent(m.id)' in tv and "[A-Za-z0-9._:-]{1,160}" in tv
    assert 'setAttribute("rel", "noopener")' in tv


def test_the_page_parses_and_assigns_no_undeclared_name() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    for n in ("sign.js", "thread_view.js"):
        out = subprocess.run([node, "--check", str(WEB / n)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
    assert _undeclared_assignments(SIGN) == set()


# ---- the render: the real page against a stub cockpit ---------------------------------------------------------------------

FP = "SHA256:" + "A" * 43
CRED = b"levain-head-cred"
RP = "localhost"
THREAD = "th-head-1"
HEAD_ID = "ev-" + "1" * 32
FILES = {"/sign/drop": "sign.html"}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _card(kind: str, line: str, actions: list[str], detail: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "line": line, "actions": actions, "head": {"envelope_id": HEAD_ID, "kind": "revoked", "thread_id": THREAD},
            "detail": detail}


FIELDS = [{"field": "where", "value": [{"repo": "/r", "paths": ["."]}], "cites": [{"field": "where"}]},
          {"field": "budget", "value": {"tool_calls": 40}, "cites": []}]
ENVELOPE = _card("ENVELOPE", "a draft envelope", ["sign", "withdraw"],
                 {"draft_id": "dr-1", "revision": 1, "digest": "sha256:" + "d" * 64, "complete": True, "unset": [], "capped": False,
                  "fields": FIELDS, "cite_note": "each value cites where it came from", "since_parent": [], "against": {}})
REVOKED = _card("REVOKED", "the head was killed", ["replace", "close"], {"since": "2026-11-04T00:00:00.000000Z"})


class Stub:
    """A cockpit that answers the drop sheet's read and the six draft actions with canned bodies, and records every post."""

    def __init__(self, cards: list[dict[str, Any]], refuse_challenge: bool = False) -> None:
        self.cards = cards
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.refuse_challenge = refuse_challenge
        self.origin = ""

    def reply(self, action: str, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        sid = {"store_id": "st-1"}
        if action == "draft-cards":
            return 200, {"cards": self.cards, **sid}
        if action == "draft-rule":
            self.cards = [ENVELOPE]
            return 200, {"draft": {"draft_id": "dr-1", "revision": 1, "digest": "sha256:" + "d" * 64, "thread_id": THREAD}, "cards": self.cards, **sid}
        if action == "draft-withdraw":
            self.cards = [REVOKED]
            return 200, {"withdrawn": {"draft_id": "dr-1", "revision": 1}, "cards": self.cards, **sid}
        if action == "draft-review":
            return 200, {"review_id": "dv-" + "a" * 32, "body_text": "THE BODY THAT IS SIGNED", "rp_id": RP, "since_parent": [],
                         "against": {"base_kind": "revoked", "reason": "killed", "wider": True, "members_added": [], "members_removed": [],
                                     "diff": {"where": {"added": ["/r  ."], "removed": []}, "effects": {"lands": {"added": ["lands:sha256:ab"], "removed": [], "changed": []}},
                                              "budget": {"tool_calls": [None, 40]}, "stop_at": None}},
                         "view": ["the head no longer authorises anything: every value below is a new grant"], **sid}
        if action == "draft-challenge":
            if self.refuse_challenge:
                return 409, {"error": "draft_stale", "message": "the draft changed since the card was drawn"}
            return 200, {"payload": {"digest": "sha256:" + "d" * 64}, "rp_id": RP, "challenge": _b64u(b"c" * 32), "line": "SIGNED LINE", **sid}
        if action == "draft-accept":
            self.cards = []
            return 200, {"created": True, "envelope": {"envelope_id": "ev-" + "2" * 32}, "cards": [], **sid}
        return 404, {"error": "not_found", "message": "no such action"}


def _serve(stub: Stub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("#")[0].split("?")[0]
            if path == "/cockpit/coord/drops.json":
                held = {FP: {"credential_id": _b64u(CRED)}}
                return self._send(200, json.dumps({"origin": stub.origin, "rp_id": RP, "credentials": held, "coord_signers": [FP],
                                                   "coord_store_id": "st-1", "coord_store_read": True, "coord_drops": [], "coord_repos": []}).encode(),
                                  "application/json")
            name = FILES.get(path, path.lstrip("/"))
            f = WEB / name
            if "/" in name or not f.is_file():
                return self._send(404, b"{}", "application/json")
            ctype = {"js": "text/javascript", "css": "text/css", "html": "text/html"}.get(f.suffix[1:], "text/plain")
            self._send(200, f.read_bytes(), ctype)

        def do_POST(self) -> None:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            action = self.path.rsplit("/", 1)[-1]
            stub.posts.append((action, req))
            status, body = stub.reply(action, req)
            self._send(status, json.dumps(body).encode(), "application/json")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    stub.origin = f"http://localhost:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _authenticator() -> dict[str, Any]:
    pkcs8 = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                                  serialization.NoEncryption())
    return {"authenticator": {"rpId": RP, "privateKey": base64.b64encode(pkcs8).decode(), "credentialId": base64.b64encode(CRED).decode(),
                              "userHandle": base64.b64encode(b"levain-head-user").decode()}}


def _open(stub: Stub) -> list[dict[str, Any]]:
    go = (f"(() => {{ window.localStorage.setItem('levain_write_token', 'T'); "
          f"location.href = '/sign/drop#thread={THREAD}'; return 1; }})()")
    return [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, _authenticator(), {"eval": go},
            {"until": "location.pathname === '/sign/drop' && !!document.getElementById('head-card') && "
                      "!!document.querySelector('#head-card .head-card-box')", "ms": 30000}]


def _click(i: str) -> dict[str, Any]:
    return {"do": f"document.getElementById('{i}').click()", "until": "1", "ms": 5000}


def _until(js: str) -> dict[str, Any]:
    return {"until": js, "ms": 30000}


NOTE = "document.getElementById('head-note').textContent"


def _tag(name: str, expr: str) -> dict[str, Any]:
    return {"eval": f"'{name}|' + String({expr})"}


def _seen(out: dict[str, Any]) -> dict[str, str]:
    return {v.split("|", 1)[0]: v.split("|", 1)[1] for v in out["values"] if isinstance(v, str) and "|" in v[:24]}


@needs_browser
def test_a_revoked_card_is_replaced_reviewed_and_signed_with_the_challenge_made_at_the_tap(tmp_path: Path) -> None:
    stub = Stub([REVOKED])
    httpd = _serve(stub)
    try:
        out = _drive(tmp_path, stub.origin + "/nope", [
            *_open(stub),
            _tag("box", "document.querySelector('#head-card .head-card-box').textContent"),
            _tag("extend", "!!document.getElementById('head-extend')"),
            _click("head-replace"),
            _until("!!document.getElementById('head-review')"),
            _tag("note1", NOTE),
            _click("head-review"),
            _until("!!document.getElementById('head-sign') && !!document.getElementById('head-diffs')"),
            _tag("diffs", "document.getElementById('head-diffs').textContent"),
            _click("head-sign"),
            _until("/^Signed/.test(" + NOTE + ")"),
            _tag("note2", NOTE),
        ])
    finally:
        httpd.shutdown()
    v = _seen(out)
    assert v["box"].startswith("REVOKED: the head was killed") and v["extend"] == "false", v
    assert "grants nothing" in v["note1"], v                   # the unsigned draft says so
    assert "MORE than the head it replaces" in v["diffs"] and "adds where it may work: /r  ." in v["diffs"], v
    assert "budget tool_calls: unset -> 40" in v["diffs"] and "THE BODY THAT IS SIGNED" in v["diffs"], v
    assert v["note2"].startswith("Signed: envelope ev-"), v
    acts = [a for a, _r in stub.posts]
    assert acts == ["draft-cards", "draft-rule", "draft-review", "draft-challenge", "draft-accept"], acts
    posts = dict(stub.posts)
    rule = posts["draft-rule"]
    assert rule["rule"] == "replace" and rule["head"] == HEAD_ID and rule["thread_id"] == THREAD and rule["store_id"] == "st-1"
    review = posts["draft-review"]
    assert review["draft_id"] == "dr-1" and review["revision"] == 1 and review["signer_fp"] == FP
    accept = posts["draft-accept"]
    assert accept["review_id"] == "dv-" + "a" * 32 and accept["body_text"] == "THE BODY THAT IS SIGNED"
    assert accept["payload"] == {"digest": "sha256:" + "d" * 64}
    assert accept["signature"] and accept["authenticator_data"] and accept["client_data_json"]


@needs_browser
def test_a_stale_challenge_reads_the_card_again_and_signs_nothing(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE], refuse_challenge=True)
    httpd = _serve(stub)
    try:
        out = _drive(tmp_path, stub.origin + "/nope", [
            *_open(stub), _click("head-review"), _until("!!document.getElementById('head-sign')"), _click("head-sign"),
            _until("/nothing was signed/.test(" + NOTE + ")"), _tag("note", NOTE),
        ])
    finally:
        httpd.shutdown()
    assert "changed since" in _seen(out)["note"], out["values"]
    acts = [a for a, _r in stub.posts]
    assert "draft-accept" not in acts and acts.count("draft-cards") == 2, acts       # read again after the 409


@needs_browser
def test_withdraw_posts_the_drafts_own_revision_and_redraws(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    httpd = _serve(stub)
    try:
        out = _drive(tmp_path, stub.origin + "/nope", [
            *_open(stub), _click("head-withdraw"), _until("!!document.getElementById('head-replace')"),
            _tag("note", NOTE),
        ])
    finally:
        httpd.shutdown()
    assert _seen(out)["note"] == "Draft withdrawn.", out["values"]
    assert dict(stub.posts)["draft-withdraw"] == {"store_id": "st-1", "draft_id": "dr-1", "revision": 1}
