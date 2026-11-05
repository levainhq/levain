"""CH-H1 H1-3, the glass half: the head card on the drop sheet (sign.js, ``/sign/drop#thread=<id>``) and the thread view's link.

Two kinds of test, and neither is the device. The structure tests read the page's source for the few things that are
structural (the fragment is read before the bar is cleaned, no text field of its own, the link's safe id). The render tests
run the REAL sign.js in headless Chrome against a STUB cockpit (a canned server speaking the draft-* reply shapes of
``levain.coordinator.draft_sign``; the real handlers are tested in tests/test_coordinator_draft_sign.py) with a virtual
authenticator. The stub REFUSES a request whose key set is not the one the real server's ``_need`` accepts, so a page that
sends a key the server would refuse fails here as it would live. They prove the order of the page's calls, that Sign mints
the challenge at the tap, what the page draws and what it says when a reply is missing or wrong; they prove nothing about the
real cards' wording on glass, or Phill's passkey on his own device (spore-1066)."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from levain.coordinator import draft_sign
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_coord_drop import _undeclared_assignments

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "levain" / "templates" / "web"
SIGN = (WEB / "sign.js").read_text()
HEAD = SIGN[SIGN.index("// ---- the head card of a thread"):SIGN.index("    form.start();\n    // a drop kept from")]


def test_the_page_reads_the_fragment_before_the_bar_is_cleaned() -> None:
    # the fragment is taken out of the bar by one replaceState; `thread=` is read before it
    assert SIGN.index("const tm = ") < SIGN.index('history.replaceState(null, "", location.pathname)')
    assert 'location.pathname === "/sign/drop" && tm' in SIGN
    assert "thread=([A-Za-z0-9._:-]{1,160})" in SIGN


def test_the_head_card_adds_no_text_field_so_there_is_no_name_to_classify() -> None:
    """Safari's AutoFill classifier reads a field's name (tests/test_drop_box_is_prose.py). The card has no input of its own; a
    field added later must carry a dash-free name, and this test is where that is made to be noticed."""
    assert not re.search(r'el\("(input|textarea|select)"', HEAD)
    for name in re.findall(r'setAttribute\("name", "([^"]*)"\)|\["name", "([^"]*)"\]', HEAD):
        assert "-" not in "".join(name), name


def test_the_thread_view_links_the_card_by_a_safe_id_only() -> None:
    tv = (WEB / "thread_view.js").read_text()
    # unencoded (FINDINGS_h13b_r2 E1): the id matched the sheet's own pattern, which refuses "%", so th-a:b encoded is dropped
    assert '"/sign/drop#thread=" + m.id' in tv and "encodeURIComponent(m.id)" not in tv and "[A-Za-z0-9._:-]{1,160}" in tv
    assert 'setAttribute("rel", "noopener")' in tv


def test_the_page_parses_and_assigns_no_undeclared_name() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    for n in ("sign.js", "thread_view.js"):
        out = subprocess.run([node, "--check", str(WEB / n)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
    assert _undeclared_assignments(SIGN) == set()


def test_the_passkey_cancel_text_is_one_plain_sentence_in_every_ceremony() -> None:
    """The three passkey ceremonies of this page (the sign request's, the drop's, the head card's) say a cancel in one
    voice; none prints a raw DOMException name."""
    assert "(NotAllowedError)" not in SIGN
    assert "You cancelled, or the passkey prompt timed out" in SIGN
    assert len(re.findall(r"[(]passkeyWhy\(e\)", SIGN)) == 3          # the drop's, the sheet's and the head card's
    assert len(re.findall(r'"The passkey did not sign \("', SIGN)) == 1       # said once, in the helper


def test_the_stub_asks_for_the_keys_the_real_server_asks_for() -> None:
    """A drift guard: the stub's key table is the server's own. (The stub copy exists so a request can be refused in the
    browser tests; this is the line that says the copy is current.)"""
    assert NEED == {k: set(v) for k, v in draft_sign._KEYS.items()}


# ---- the render: the real page against a stub cockpit ---------------------------------------------------------------------

FP = "SHA256:" + "A" * 43
CRED = b"levain-head-cred"
RP = "localhost"
THREAD = "th-head-1"
HEAD_ID = "ev-" + "1" * 32
FILES = {"/sign/drop": "sign.html"}
NEED = {"draft-cards": {"thread_id", "store_id"},
        "draft-withdraw": {"draft_id", "revision", "store_id"},
        "draft-review": {"draft_id", "revision", "digest", "signer_fp", "store_id"},
        "draft-challenge": {"review_id"},
        "draft-accept": {"review_id", "body_text", "payload", "authenticator_data", "client_data_json", "signature"}}
RULE_REQUIRED = {"thread_id", "store_id", "rule", "head"}


def _keys_ok(action: str, req: dict[str, Any]) -> bool:
    """The real server's ``_need``: the exact key set (draft-rule: the four, and ``prefill`` optionally)."""
    if action == "draft-rule":
        return RULE_REQUIRED <= set(req) <= RULE_REQUIRED | {"prefill"}
    return action in NEED and set(req) == NEED[action]


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _card(kind: str, line: str, actions: list[str], detail: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "line": line, "actions": actions, "head": {"envelope_id": HEAD_ID, "kind": "revoked", "thread_id": THREAD},
            "detail": detail}


FIELDS = [{"field": "where", "value": [{"repo": "/r", "paths": ["."]}], "cites": [{"field": "where"}]},
          {"field": "budget", "value": {"tool_calls": 40, "tokens": None, "dollars_cents": 250, "hours": 2}, "cites": []},
          {"field": "stop_at", "value": "2026-11-05T10:00:00.000000Z", "cites": []}]
ENVELOPE = _card("ENVELOPE", "a draft envelope", ["sign", "withdraw"],
                 {"draft_id": "dr-1", "revision": 1, "digest": "sha256:" + "d" * 64, "complete": True, "unset": [], "capped": False,
                  "fields": FIELDS, "cite_note": "each value cites where it came from", "since_parent": [], "against": {}})
REVOKED = _card("REVOKED", "the head was killed", ["replace", "close"], {"since": "2026-11-04T00:00:00.000000Z"})
LAPSED = _card("LAPSED", "the head ran out", ["extend"], {"since": "2026-11-04T00:00:00.000000Z"})
DIFF = {"where": {"added": ["/r  ."], "removed": []}, "effects": {"lands": {"added": ["lands:sha256:ab"], "removed": [], "changed": []}},
        "budget": {"tool_calls": [None, 40], "dollars_cents": [100, 250]}, "stop_at": None}


def _against(kind: str, wider: bool, diff: dict[str, Any], members: bool = False) -> dict[str, Any]:
    return {"base_kind": kind, "reason": "killed", "wider": wider, "members_added": [{"drop_id": "dp-1"}] if members else [],
            "members_removed": [], "diff": diff}


EMPTY_DIFF = {"where": {"added": [], "removed": []}, "effects": {"lands": {"added": [], "removed": [], "changed": []}}, "budget": {}, "stop_at": None}
Step = Callable[["Stub"], "tuple[int, dict[str, Any]]"]


class Stub:
    """A cockpit that answers the drop sheet's read and the six draft actions with canned bodies, and records every post.
    ``script[action]`` is a list of replies used up one per call (``None`` = the normal reply); ``delay[action]`` holds a reply."""

    def __init__(self, cards: list[dict[str, Any]], against: dict[str, Any] | None = None) -> None:
        self.cards = cards
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.bad: list[tuple[str, list[str]]] = []
        self.script: dict[str, list[Step | None]] = {}
        self.delay: dict[str, float] = {}
        self.against = against or _against("revoked", True, DIFF)
        self.origin = ""
        self.signers = [FP]
        self.body_text = "THE BODY THAT IS SIGNED"

    def reply(self, action: str, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not _keys_ok(action, req):
            self.bad.append((action, sorted(req)))
            return 400, {"error": "bad_request", "message": f"request keys must be {sorted(NEED.get(action, RULE_REQUIRED))}"}
        queue = self.script.get(action) or []
        step = queue.pop(0) if queue else None
        if self.delay.get(action):
            time.sleep(self.delay[action])
        if step is not None:
            return step(self)
        return self.normal(action)

    def normal(self, action: str) -> tuple[int, dict[str, Any]]:
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
            return 200, {"review_id": "dv-" + "a" * 32, "body_text": self.body_text, "rp_id": RP, "since_parent": [],
                         "against": self.against, "view": ["the head no longer authorises anything: every value below is a new grant"], **sid}
        if action == "draft-challenge":
            return 200, {"payload": {"digest": "sha256:" + "d" * 64}, "rp_id": RP, "challenge": _b64u(b"c" * 32), "line": "SIGNED LINE", **sid}
        if action == "draft-accept":
            self.cards = []
            return 200, {"created": True, "envelope": {"envelope_id": "ev-" + "2" * 32}, "cards": [], **sid}
        return 404, {"error": "not_found", "message": "no such action"}

    def actions(self) -> list[str]:
        return [a for a, _r in self.posts]


def _fail(status: int, error: str = "unavailable", **extra: Any) -> Step:
    return lambda _s: (status, {"error": error, "message": "the stub says no", **extra})


def _signed_anyway(status: int, cards: list[dict[str, Any]] | None = None) -> Step:
    """A reply that FAILS although the write committed: the card then shows ``cards`` (no draft by default)."""
    def go(s: Stub) -> tuple[int, dict[str, Any]]:
        s.cards = [] if cards is None else cards
        return status, {"error": "unavailable", "message": "the stub lost the reply"}
    return go


def _lost(cards: list[dict[str, Any]] | None = None) -> Step:
    """No reply at all (status 0 on the page: the connection closes with nothing sent). With ``cards`` the write committed
    first and the card now shows them; without, nothing was written."""
    def go(s: Stub) -> tuple[int, dict[str, Any]]:
        if cards is not None:
            s.cards = cards
        return 0, {}
    return go


def _slow(action: str, seconds: float) -> Step:
    """The normal reply to ``action``, late."""
    def go(s: Stub) -> tuple[int, dict[str, Any]]:
        time.sleep(seconds)
        return s.normal(action)
    return go


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
            if path == "/__posts":
                return self._send(200, json.dumps(stub.actions()).encode(), "application/json")
            if path == "/cockpit/coord/drops.json":
                held = {fp: {"credential_id": _b64u(CRED if fp == FP else b"other-" + fp.encode()[-8:])} for fp in stub.signers}
                return self._send(200, json.dumps({"origin": stub.origin, "rp_id": RP, "credentials": held, "coord_signers": stub.signers,
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
            if status == 0:              # a lost reply: the connection closes with nothing on it (HTTP/1.0, so no retry)
                self.close_connection = True
                return
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
    # a person's press (trusted): the passkey door opens only for one
    return {"tap": f"document.getElementById('{i}')", "ms": 5000}


def _until(js: str) -> dict[str, Any]:
    return {"until": js, "ms": 30000}


def _text(i: str) -> str:
    return f"(document.getElementById('{i}') || {{}}).textContent || ''"


NOTE = _text("head-note")
UNSURE = _text("head-unsure")
READ = _text("head-read")


TWO_READS = "fetch('/__posts').then(r => r.json()).then(a => a.filter(x => x === 'draft-cards').length === 2)"


def _tag(name: str, expr: str) -> dict[str, Any]:
    return {"eval": f"'{name}|' + String({expr})"}


def _seen(out: dict[str, Any]) -> dict[str, str]:
    return {v.split("|", 1)[0]: v.split("|", 1)[1] for v in out["values"] if isinstance(v, str) and "|" in v[:24]}


def _run(tmp_path: Path, stub: Stub, steps: list[dict[str, Any]]) -> dict[str, str]:
    httpd = _serve(stub)
    try:
        out = _drive(tmp_path, stub.origin + "/nope", [*_open(stub), *steps])
    finally:
        httpd.shutdown()
    return _seen(out)


def _review_then_sign() -> list[dict[str, Any]]:
    return [_click("head-review"), _until("!!document.getElementById('head-sign')"), _click("head-sign")]


@needs_browser
def test_a_revoked_card_is_replaced_reviewed_and_signed_with_the_challenge_made_at_the_tap(tmp_path: Path) -> None:
    stub = Stub([REVOKED])
    v = _run(tmp_path, stub, [
        _tag("box", "document.querySelector('#head-card .head-card-box').textContent"),
        _tag("buttons", "[...document.querySelectorAll('#head-card button')].map(b => b.id + '=' + b.textContent).join(';')"),
        _click("head-replace"),
        _until("!!document.getElementById('head-review')"),
        _tag("note1", NOTE),
        _click("head-review"),
        _until("!!document.getElementById('head-sign') && !!document.getElementById('head-diffs')"),
        {"eval": "fetch('/__posts').then(r => r.text()).then(t => 'before|' + t)"},
        _tag("diffs", "document.getElementById('head-diffs').textContent"),
        _click("head-sign"),
        _until("/^Signed/.test(" + NOTE + ")"),
        _tag("note2", NOTE),
    ])
    assert v["box"].startswith("Revoked: the head was killed") and "head-extend" not in v["buttons"], v
    assert "head-replace=Draft a replacement" in v["buttons"] and "close" not in v["buttons"].lower(), v    # no draft-side close verb
    assert "grants nothing" in v["note1"], v                   # the unsigned draft says so
    assert "draft-challenge" not in json.loads(v["before"]), v    # the challenge is minted at the tap, not when the review is drawn
    assert "not an extension" in v["diffs"] and "MORE than" not in v["diffs"], v
    assert "adds where it may work: /r  ." in v["diffs"] and "THE BODY THAT IS SIGNED" in v["diffs"], v
    assert v["note2"].startswith("Signed"), v
    acts = stub.actions()
    assert acts == ["draft-cards", "draft-rule", "draft-review", "draft-challenge", "draft-accept"], acts
    assert stub.bad == []
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
def test_the_accept_carries_exactly_the_keys_the_server_takes(tmp_path: Path) -> None:
    """Reproduced on c22a0e3a: the page put `store_id` on draft-accept, and the real `_need` refuses that key set (400
    bad_request) on a live signing. The stub is strict now, so this is the test that fails when a key is added or lost."""
    stub = Stub([ENVELOPE])
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/^Signed/.test(" + NOTE + ")"), _tag("note", NOTE)])
    assert stub.bad == [], stub.bad
    assert set(dict(stub.posts)["draft-accept"]) == NEED["draft-accept"] and v["note"].startswith("Signed"), v


@needs_browser
def test_a_second_tap_while_the_first_is_in_flight_posts_one_challenge(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.delay["draft-challenge"] = 0.6
    v = _run(tmp_path, stub, [
        _click("head-review"), _until("!!document.getElementById('head-sign')"),
        {"tap": "document.getElementById('head-sign')", "times": 2, "ms": 5000},
        _until("/^Signed/.test(" + NOTE + ")"), _tag("note", NOTE),
    ])
    assert stub.actions().count("draft-challenge") == 1 and stub.actions().count("draft-accept") == 1, stub.actions()


@needs_browser
def test_a_stale_challenge_reads_the_card_again_and_signs_nothing(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-challenge"] = [_fail(409, "draft_stale", message="the draft changed since the card was drawn")]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/nothing was signed/.test(" + NOTE + ")"), _tag("note", NOTE)])
    assert "nothing was signed" in v["note"], v
    assert "draft-accept" not in stub.actions() and stub.actions().count("draft-cards") == 2, stub.actions()   # read again after the 409


@needs_browser
def test_a_409_on_withdraw_reads_the_card_again(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-withdraw"] = [_fail(409, "draft_stale", message="the draft is at revision 2")]
    v = _run(tmp_path, stub, [_click("head-withdraw"), _until("/changed since/.test(" + NOTE + ")"), _tag("note", NOTE)])
    assert "changed since" in v["note"] and "withdrawn." not in v["note"], v
    assert stub.actions() == ["draft-cards", "draft-withdraw", "draft-cards"], stub.actions()


@needs_browser
def test_a_409_on_a_rule_reads_the_card_again(tmp_path: Path) -> None:
    stub = Stub([REVOKED])
    stub.script["draft-rule"] = [_fail(409, "draft_stale", message="the head is not the envelope the card showed")]
    v = _run(tmp_path, stub, [_click("head-replace"), _until("/changed since/.test(" + NOTE + ")"), _tag("note", NOTE)])
    assert "changed since" in v["note"], v
    assert stub.actions() == ["draft-cards", "draft-rule", "draft-cards"], stub.actions()


@needs_browser
def test_withdraw_posts_the_drafts_own_revision_and_redraws(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    v = _run(tmp_path, stub, [_click("head-withdraw"), _until("!!document.getElementById('head-replace')"), _tag("note", NOTE)])
    assert v["note"] == "Draft withdrawn.", v
    assert dict(stub.posts)["draft-withdraw"] == {"store_id": "st-1", "draft_id": "dr-1", "revision": 1}
    assert stub.bad == []


@needs_browser
def test_a_head_thread_shows_one_sign_button_and_no_drop_form(tmp_path: Path) -> None:
    """Reproduced on c22a0e3a: the drop form's own Sign and Send stayed live under the card, and the intro still said
    'You sign once'. With a head thread the drop form is not on the page."""
    stub = Stub([ENVELOPE])
    v = _run(tmp_path, stub, [
        _click("head-review"), _until("!!document.getElementById('head-sign')"),
        _tag("ids", "['drop-sign','drop-send','drop-words','drop-sign-why','drop-send-note'].filter(i => document.getElementById(i)).join(',')"),
        _tag("signs", "[...document.querySelectorAll('button')].filter(b => /sign/i.test(b.textContent)).map(b => b.textContent).join('|')"),
        _tag("intro", "document.getElementById('sheet-msg').textContent + ' ' + document.getElementById('sheet-title').textContent"),
        _tag("textareas", "document.querySelectorAll('textarea,input,select').length"),
    ])
    assert v["ids"] == "" and v["textareas"] == "0", v
    assert v["signs"] == "Sign this draft", v
    assert "Say what you want done" not in v["intro"] and "sign once" not in v["intro"], v


@needs_browser
def test_unsure_stays_when_the_card_read_fails_too(tmp_path: Path) -> None:
    """Reproduced on c22a0e3a: an accept with no known outcome, then the card read failed, and the failure REPLACED the unsure
    line. (The outcome is unknown when no reply came back; a 503 is a definite "not signed", its own test below.)"""
    stub = Stub([ENVELOPE])
    stub.script["draft-accept"] = [_lost()]
    stub.script["draft-cards"] = [None, _fail(503)]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/could not be read/.test(" + READ + ")"),
                              _tag("unsure", UNSURE), _tag("read", READ), _tag("sign", "!!document.getElementById('head-sign')")])
    assert "Not sure this was signed" in v["unsure"] and "could not be read" in v["read"], v
    assert v["sign"] == "false", v          # an unknown outcome is never signed again from this page


@needs_browser
def test_a_draft_simply_gone_after_a_lost_accept_is_not_called_signed(tmp_path: Path) -> None:
    """FINDINGS_h13b_r2 S1, reproduced on 6022235c: the reply was lost and the card no longer showed the draft (it may have
    been withdrawn on another device, or the head lapsed), and the page said 'Signed, as far as the card shows'."""
    stub = Stub([ENVELOPE])
    stub.script["draft-accept"] = [_lost([REVOKED])]          # the card moved on; nothing on it names this signature's envelope
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/no longer on the card/.test(" + UNSURE + ")"),
                              _tag("unsure", UNSURE), _tag("signing", _text("head-signing"))])
    assert "Not sure this was signed" in v["unsure"] and "may have been signed, or withdrawn" in v["unsure"], v
    assert "Signed:" not in v["unsure"] and "as far as the card shows" not in v["unsure"], v
    assert v["signing"].startswith("Not confirmed"), v


@needs_browser
def test_a_lost_accept_is_called_signed_only_when_the_card_names_this_envelope(tmp_path: Path) -> None:
    eid = "ev-" + "3" * 32
    stub = Stub([ENVELOPE])
    stub.body_text = json.dumps({"envelope_id": eid, "format": "stub"})
    after = dict(LAPSED, head={"envelope_id": eid, "kind": "lapsed", "thread_id": THREAD})
    stub.script["draft-accept"] = [_lost([after])]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/Signed:/.test(" + UNSURE + ")"),
                              _tag("unsure", UNSURE), _tag("signing", _text("head-signing")),
                              _tag("detail", "document.querySelector('#head-card .head-detail').textContent")])
    assert "Signed: the card shows this signature's envelope" in v["unsure"] and "Not signed" not in v["unsure"], v
    assert v["signing"].startswith("Signed"), v
    assert eid in v["detail"], v


@needs_browser
def test_unsure_resolves_to_not_signed_when_the_card_still_shows_the_draft(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-accept"] = [_lost()]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/Not signed/.test(" + UNSURE + ")"), _tag("unsure", UNSURE)])
    assert "Not sure this was signed" in v["unsure"] and "Not signed: the card still shows the draft" in v["unsure"], v


@needs_browser
def test_a_503_at_accept_is_not_signed_and_the_same_sign_button_signs_again(tmp_path: Path) -> None:
    """FINDINGS_h13b_r2 S1: every 503 the cockpit answers is before the write (phone.py: '503 a fault on this machine (nothing
    written)'), and the server keeps the review for a machine fault, so it is 'not signed', never 'not sure'."""
    stub = Stub([ENVELOPE])
    stub.script["draft-accept"] = [_fail(503, message="the coordinator store cannot answer now (OSError: disk): nothing was signed")]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/^Not signed/.test(" + NOTE + ")"),
                              _tag("note", NOTE), _tag("unsure", UNSURE), _tag("sign", "!!document.getElementById('head-sign')"),
                              _click("head-sign"), _until("/^Signed/.test(" + NOTE + ")"), _tag("note2", NOTE)])
    assert "Not signed" in v["note"] and "tap Sign again" in v["note"] and v["unsure"] == "", v
    assert v["sign"] == "true" and v["note2"].startswith("Signed"), v
    acts = stub.actions()
    assert acts.count("draft-challenge") == 2 and acts.count("draft-accept") == 2, acts
    assert acts.count("draft-cards") == 1, acts          # the second accept's reply carried the cards; no read in between


@needs_browser
def test_a_failed_card_read_after_a_refusal_keeps_the_refusal(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-challenge"] = [_fail(409, "draft_stale", message="the draft changed")]
    stub.script["draft-cards"] = [None, _fail(503)]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/could not be read/.test(" + READ + ")"), _tag("note", NOTE), _tag("read", READ)])
    assert "nothing was signed" in v["note"], v
    assert "could not be read" in v["read"], v


@needs_browser
def test_null_cards_after_a_committed_write_mean_unknown_not_no_card(tmp_path: Path) -> None:
    """Reproduced on c22a0e3a: `cards: null` after a committed accept drew 'shows no head card' beside the Signed note."""
    stub = Stub([ENVELOPE])
    stub.script["draft-accept"] = [lambda s: (200, {"created": True, "envelope": {"envelope_id": "ev-" + "2" * 32}, "cards": None,
                                                    "cards_note": "the cards could not be read just now: read the cards again", "store_id": "st-1"})]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/^Signed/.test(" + NOTE + ")"),
                              _until(TWO_READS), _tag("card", "document.getElementById('head-card').textContent")])
    assert "shows no head card" not in v["card"], v
    assert stub.actions().count("draft-cards") == 2, stub.actions()       # the note asked for the cards again


@needs_browser
def test_a_withdraw_reply_without_cards_reads_the_card_again(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-withdraw"] = [lambda s: (200, {"withdrawn": {"draft_id": "dr-1", "revision": 1}, "store_id": "st-1"})]
    v = _run(tmp_path, stub, [_click("head-withdraw"), _until("/withdrawn/.test(" + NOTE + ")"), _until(TWO_READS),
                              _tag("card", "document.getElementById('head-card').textContent")])
    assert "shows no head card" not in v["card"], v
    assert stub.actions() == ["draft-cards", "draft-withdraw", "draft-cards"], stub.actions()


@needs_browser
def test_a_spent_review_is_read_again_after_a_refusal_and_the_sign_button_goes(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-accept"] = [_fail(400, "review_gone")]
    v = _run(tmp_path, stub, [*_review_then_sign(), _until("/Not signed/.test(" + NOTE + ")"),
                              _tag("sign", "!!document.getElementById('head-sign')"), _tag("note", NOTE)])
    assert v["sign"] == "false" and "Not signed" in v["note"], v
    assert stub.actions().count("draft-cards") == 2, stub.actions()


@needs_browser
def test_a_card_that_cannot_be_drawn_says_so_instead_of_hanging(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-cards"] = [lambda s: (200, {"cards": [None], "store_id": "st-1"})]
    httpd = _serve(stub)
    try:
        out = _drive(tmp_path, stub.origin + "/nope", [*_open(stub)[:3], _until("/could not be drawn/.test(" + READ + ")"), _tag("read", READ)])
    finally:
        httpd.shutdown()
    assert "could not be drawn" in _seen(out)["read"], out["values"]


@needs_browser
def test_the_passkey_cancel_is_said_in_words(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    v = _run(tmp_path, stub, [
        _click("head-review"), _until("!!document.getElementById('head-sign')"),
        {"eval": "navigator.credentials.get = () => Promise.reject(Object.assign(new Error('x'), {name: 'NotAllowedError'})); 1"},
        _click("head-sign"), _until("/nothing was signed/.test(" + NOTE + ")"), _tag("note", NOTE),
    ])
    assert "You cancelled, or the passkey prompt timed out" in v["note"] and "NotAllowedError" not in v["note"], v
    assert "draft-accept" not in stub.actions()


@needs_browser
def test_the_envelope_card_speaks_plainly(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE], against=_against("effective", True, DIFF))
    v = _run(tmp_path, stub, [
        _tag("box", "document.querySelector('#head-card .head-card-box').textContent"),
        _tag("label", "document.querySelector('#head-card > .env-label, #head-card .head-title').textContent"),
        _click("head-review"), _until("!!document.getElementById('head-diffs')"),
        _tag("diffs", "document.getElementById('head-diffs').textContent"),
        _tag("wider", "(document.querySelector('#head-diffs .head-wider') || {}).textContent || ''"),
        _tag("order", "(() => { const d = document.getElementById('head-diffs'); const w = d.querySelector('.head-wider'); const p = d.querySelector('pre'); "
                      "return !!w && !!p && !!(w.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING); })()"),
    ])
    box = v["box"]
    assert '[{"repo"' not in box and "{\"tool_calls\"" not in box and "(cited)" not in box and "(no cite)" not in box, box
    assert "/r" in box and "40 tool calls" in box and "2 hours" in box and "$2.50" in box, box
    assert "Draft to sign" in box and "ENVELOPE" not in box, box
    assert THREAD not in v["label"], v
    for raw in ("lands:sha256", "tool_calls", "dollars_cents", "adds lands"):
        assert raw not in v["diffs"], (raw, v["diffs"])
    assert "grants MORE than" in v["wider"] and v["order"] == "true", v


@needs_browser
def test_members_only_is_not_called_no_change(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE], against=_against("effective", False, EMPTY_DIFF, members=True))
    v = _run(tmp_path, stub, [_click("head-review"), _until("!!document.getElementById('head-diffs')"),
                              _tag("diffs", "document.getElementById('head-diffs').textContent")])
    assert "unchanged except members" in v["diffs"] and "no change from the head" not in v["diffs"], v


@needs_browser
def test_a_lapsed_card_offers_a_draft_of_an_extension_and_the_signed_note_hides_the_raw_id(tmp_path: Path) -> None:
    stub = Stub([LAPSED], against=_against("effective", False, DIFF))
    v = _run(tmp_path, stub, [
        _tag("buttons", "[...document.querySelectorAll('#head-card button')].map(b => b.id + '=' + b.textContent).join(';')"),
        _tag("box", "document.querySelector('#head-card .head-card-box').textContent"),
        _click("head-extend"), _until("!!document.getElementById('head-review')"),
        *_review_then_sign(), _until("/^Signed/.test(" + NOTE + ")"), _tag("note", NOTE),
    ])
    assert v["buttons"] == "head-extend=Draft an extension" and v["box"].startswith("Lapsed:"), v
    assert "ev-" not in v["note"], v
    assert dict(stub.posts)["draft-rule"]["rule"] == "renew"


# ---- FINDINGS_h13b_r2 (L3 r1 on the glass diff), each from a reproduced failure on 6022235c ------------------------------

FP2 = "SHA256:" + "B" * 43


@needs_browser
def test_a_review_the_server_refuses_with_422_shows_no_sign_button(tmp_path: Path) -> None:
    """The G3 path: a head of a kind the sheet cannot describe is a 422 at review (draft_sign `_view_lines`), and the page
    must offer nothing to sign."""
    stub = Stub([ENVELOPE])
    stub.script["draft-review"] = [_fail(422, "refused", message="the head is of a kind this sheet cannot describe (weird): nothing was signed")]
    v = _run(tmp_path, stub, [_click("head-review"), _until("/refused/.test(" + NOTE + ")"), _tag("note", NOTE),
                              _tag("sign", "!!document.getElementById('head-sign')"), _tag("diffs", "!!document.getElementById('head-diffs')")])
    assert v["note"].startswith("Reviewing was refused: the head is of a kind this sheet cannot describe"), v
    assert v["sign"] == "false" and v["diffs"] == "false", v
    assert stub.actions() == ["draft-cards", "draft-review"], stub.actions()


@needs_browser
def test_with_two_passkeys_the_picker_stays_on_the_card_and_a_new_choice_retires_the_review(tmp_path: Path) -> None:
    """S2, reproduced: `grid.remove()` took the #drop-key picker with it, so the operator could not choose the signer."""
    stub = Stub([ENVELOPE])
    stub.signers = [FP, FP2]
    v = _run(tmp_path, stub, [
        _tag("picker", "!!document.querySelector('#head-card #drop-key')"),
        _click("head-review"), _until("!!document.getElementById('head-sign')"),
        {"do": f"(() => {{ const s = document.getElementById('drop-key'); s.value = '{FP2}'; s.dispatchEvent(new Event('change')); }})()",
         "until": "!document.getElementById('head-sign')", "ms": 5000},
        _tag("note", NOTE), _tag("diffs", "!!document.getElementById('head-diffs')"),
        _click("head-review"), _until("!!document.getElementById('head-sign')"),
    ])
    assert v["picker"] == "true", v
    assert "signing key changed" in v["note"] and v["diffs"] == "false", v
    reviews = [r["signer_fp"] for a, r in stub.posts if a == "draft-review"]
    assert reviews == [FP, FP2], reviews
    assert "draft-challenge" not in stub.actions()


@needs_browser
def test_another_store_on_withdraw_stops_the_sheet_instead_of_calling_the_card_stale(tmp_path: Path) -> None:
    """S3, reproduced: a 409 other_store was read as a stale card ('changed since it was shown') and the card read again."""
    stub = Stub([ENVELOPE])
    stub.script["draft-withdraw"] = [_fail(409, "other_store", message="this was read from another Levain")]
    v = _run(tmp_path, stub, [_click("head-withdraw"), _until("/different Levain store/.test(" + NOTE + ")"), _tag("note", NOTE)])
    assert "changed since" not in v["note"], v
    assert stub.actions() == ["draft-cards", "draft-withdraw"], stub.actions()


@needs_browser
def test_another_store_on_the_card_read_stops_the_sheet(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-cards"] = [_fail(409, "other_store", message="this was read from another Levain")]
    httpd = _serve(stub)
    try:
        out = _drive(tmp_path, stub.origin + "/nope", [*_open(stub)[:3], _until("/different Levain store/.test(" + NOTE + ")"),
                                                       _tag("note", NOTE), _tag("read", READ)])
    finally:
        httpd.shutdown()
    v = _seen(out)
    assert "different Levain store" in v["note"] and v["read"] == "", v


@needs_browser
def test_a_tap_while_a_write_asks_for_the_card_again_is_held_until_the_read_lands(tmp_path: Path) -> None:
    """S4, reproduced: hbusy was released before the card read a write asked for, so a Review tapped meanwhile drew a Sign
    button that the late redraw then wiped (a dead end). The tap is now held off until the read has landed."""
    stub = Stub([ENVELOPE])
    stub.script["draft-withdraw"] = [lambda s: (200, {"withdrawn": {"draft_id": "dr-1", "revision": 1}, "store_id": "st-1"})]
    stub.script["draft-cards"] = [None, _slow("draft-cards", 1.5)]
    v = _run(tmp_path, stub, [
        _click("head-withdraw"), _until("/Reading the head card/.test(" + READ + ")"),
        _click("head-review"),
        _until(TWO_READS), _until("!/Reading/.test(" + READ + ")"),
        _tag("sign", "!!document.getElementById('head-sign')"),
    ])
    assert "draft-review" not in stub.actions(), stub.actions()
    assert v["sign"] == "false", v


@needs_browser
def test_a_lost_reply_to_a_rule_reads_the_card_and_offers_no_second_draft(tmp_path: Path) -> None:
    """S5, reproduced: no reply (status 0) on draft-rule said 'was refused' and left 'Draft a replacement' live, so a retry
    wrote another revision."""
    stub = Stub([REVOKED])
    stub.script["draft-rule"] = [_lost([ENVELOPE])]       # it committed; the reply was lost
    v = _run(tmp_path, stub, [_click("head-replace"), _until("!!document.getElementById('head-review')"), _tag("note", NOTE),
                              _tag("replace", "!!document.getElementById('head-replace')")])
    assert "not known whether it was done" in v["note"] and "refused" not in v["note"], v
    assert v["replace"] == "false", v
    assert stub.actions() == ["draft-cards", "draft-rule", "draft-cards"], stub.actions()


@needs_browser
def test_a_lost_reply_to_a_withdraw_reads_the_card(tmp_path: Path) -> None:
    stub = Stub([ENVELOPE])
    stub.script["draft-withdraw"] = [_lost([REVOKED])]
    v = _run(tmp_path, stub, [_click("head-withdraw"), _until("!!document.getElementById('head-replace')"), _tag("note", NOTE)])
    assert "not known whether it was done" in v["note"], v
    assert stub.actions() == ["draft-cards", "draft-withdraw", "draft-cards"], stub.actions()


@needs_browser
def test_the_signing_line_of_an_earlier_attempt_is_not_shown_beside_the_next(tmp_path: Path) -> None:
    """E4, reproduced: after a cancelled passkey the next attempt's refusal showed 'Not signed: <the earlier line>'."""
    stub = Stub([ENVELOPE])
    stub.script["draft-challenge"] = [None, _fail(409, "draft_stale", message="the draft changed")]
    v = _run(tmp_path, stub, [
        _click("head-review"), _until("!!document.getElementById('head-sign')"),
        {"eval": "window.__get = navigator.credentials.get.bind(navigator.credentials); "
                 "navigator.credentials.get = () => Promise.reject(Object.assign(new Error('x'), {name: 'NotAllowedError'})); 1"},
        _click("head-sign"), _until("/You cancelled/.test(" + NOTE + ")"), _tag("first", _text("head-signing")),
        _click("head-sign"), _until("/the draft changed/.test(" + NOTE + ")"), _until(TWO_READS),
        _tag("second", _text("head-signing")), _tag("hidden", "document.getElementById('head-signing').hidden"),
    ])
    assert v["first"] == "Not signed: SIGNED LINE", v
    assert v["second"] == "" and v["hidden"] == "true", v
