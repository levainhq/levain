"""DESIGN_SIGN D5 + D6 5 (project_memory/1023-21/sign/DESIGN_SIGN.md; the compose contract ruled in 1023-21/HEAD_rulings.md
"D5 COMPOSE CONTRACT"): the cockpit page answers a signed question with levain's own sign-request ceremony, and sends a
signed verb only after the operator's passkey signed exactly its params.

The kernel half: ``ActionVerb.sign`` (default off) reaches the verb head and admits one extra param, ``sign_request``; the
compose route ``POST /cockpit/sign/compose`` rides the write gate, files nothing itself and carries a ``ValueError``'s words.
The page half runs in headless Chrome on ``http://localhost:<port>`` (rpId localhost, the Signer pinned to that page): Chrome's
virtual authenticator holds the passkey the Signer's keys name, so the page's own challenge, ``navigator.credentials.get``
and answer write a ``.sig`` that ``ssh-keygen -Y verify`` accepts; a refused user verification (a cancelled Face ID) signs
nothing and keeps the draft."""
from __future__ import annotations

import base64
import dataclasses
import json
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization

from levain.cockpit import ProviderSpec, Read, RowIn
from levain.cockpit.authority import UNKNOWN_POLICY
from levain.cockpit.effects import DownstreamEffect
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.verbs import SIGN_PARAM, VerbRegistry
from levain.consent.questions import QuestionStore
from levain.consent.sign import Signer
from levain.web_server import make_server
from levain.writes import ActionVerb
from tests._inbox_fake import FakeInbox, send_inbox_effect
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_sign_facet import _Signed
from tests.test_cockpit_sign_origin import _Passkey
from tests.test_cockpit_sign_routes import _call

NS = "flow-needs-phill"
QRID = "np-card-question-01"
TOK = "T"
TXN = "draft-0001-aaaa"
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_request(qdir: Path, rid: str, message: str, *, options: "list[dict] | None" = None,
                   write_in: bool = False) -> None:
    d = qdir / "sign"
    d.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"format": "levain-sign-choice/1" if options else "levain-sign-request/1", "id": rid,
                            "namespace": NS, "message": message, "title": "t", "requester": "test",
                            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    if options:
        body["options"] = options
        if write_in:
            body["write_in"] = True
    (d / f".{rid}.tmp").write_text(json.dumps(body))
    (d / f".{rid}.tmp").replace(d / f"{rid}.json")


def _compose_card(enum: "list[str]") -> dict:
    return {"verb": "send_inbox", "fields": [{"name": "message", "type": "text", "label": "Message"},
                                             {"name": "for", "type": "recipient", "label": "To", "enum": enum,
                                              "default": ["fanin"]}]}      # the provider declares its default reader; the page has none


class _Rig:
    """One cockpit over a real install: an inbox panel whose compose sends the signed verb ``send_inbox`` (its effect is
    the door: it applies only params that name a sign request the passkey signed over exactly that message), an open
    question whose answer is signed through ``QRID``, and a Signer on rpId localhost pinned to this page."""

    def __init__(self, tmp_path: Path, monkeypatch: Any, *, sign: bool = True, compose: bool = True,
                 two_keys: bool = False, fillers: int = 0) -> None:
        home = tmp_path / "levain_home"
        monkeypatch.setenv("LEVAIN_HOME", str(home))
        (home / "consent").mkdir(parents=True, mode=0o700)
        self.qdir = tmp_path / "signq"
        self.question = QuestionStore(home / "consent").file(
            agent="claude_code", agent_session_id="s1", what="Ship the sweep fix tonight or hold it?",
            options=[("ship", "Ship it tonight"), ("hold", "Hold for the morning")], rec="hold",
            blocks="the nightly sweep stays red", cwd="/w")
        _write_request(self.qdir, QRID, "Ship the sweep fix tonight or hold it?\nnonce: 9a1f",
                       options=[{"key": "a", "text": "Ship it tonight"}, {"key": "b", "text": "Hold for the morning"}],
                       write_in=True)
        # open requests that sort before every other id here, so a listing's cap (50) is spent before it reaches them
        for i in range(fillers):
            _write_request(self.qdir, f"np-aaa-{i:04d}", f"filler {i}")
        self.port = _free_port()
        self.origin = f"http://localhost:{self.port}"
        self.pk = _Passkey("localhost")
        # a second enrolled passkey this browser does not hold: with no record of which it used, the page shows a pick list
        self.other = _Passkey("localhost")
        keys = {self.pk.key.fingerprint: self.pk.key}
        if two_keys:
            keys[self.other.key.fingerprint] = self.other.key
        self.signer = Signer(self.qdir, (NS,), "localhost", lambda: dict(keys))
        self.signer.page_origin = self.origin
        (tmp_path / "allowed").write_text(f'x namespaces="{NS}" {self.pk.line}\n')
        self.allowed = tmp_path / "allowed"
        self.composed: list[tuple[str, dict]] = []
        self.applied: list[dict] = []
        self.refused: list[str] = []
        self.inbox = FakeInbox()
        _root, self.src, _ids = _install(tmp_path)
        self.ck = build_default_cockpit(self.src)
        self.ck.register(ProviderSpec(
            id="inbox", kind="list", title="Inbox", priority="feed", order="time.desc", version_fields=("id",),
            facets=frozenset({"source", "for", "at"}), verbs=("send_inbox",), compose=_compose_card(["cli", "fanin"]),
            read=lambda c: Read(rows=(RowIn("a", "hello", facets={"source": "bridge", "for": "cli",
                                                                   "at": "2026-10-23T11:00:00+00:00"},
                                            stored={"id": "a"}),))))
        self.handled: list[dict] = []
        verb = ActionVerb(handler=lambda p: self.handled.append(dict(p)) or {"summary": "sent"}, label="Send to inbox",
                          fields=("message", "for"), sign=sign)
        self.kw: dict[str, Any] = dict(
            cockpit=self.ck, extra_verbs={"send_inbox": verb},
            extra_effects={"send_inbox": DownstreamEffect(self.inbox, self._effect)}, surface_tokens={"browser": TOK},
            install_class="reported", signer=self.signer, port=self.port)
        if compose:
            self.kw["compose_signer"] = self._compose

    def _compose(self, verb: str, params: dict) -> str:
        """flow's compose_signer, faked: files one plain sign request whose message is the params' message."""
        self.composed.append((verb, dict(params)))
        if params.get("message") == "refuse me":
            raise ValueError("the cockpit is not signing messages now")
        rid = f"np-compose-{len(self.composed):04d}"
        _write_request(self.qdir, rid, str(params.get("message")))
        return rid

    def notes(self) -> list[str]:
        return [n["body"] for n in self.inbox.notes.values()]

    def _effect(self, params: Any, *, origin_key: str) -> Any:
        """The door: a send counts only when it names a request this rig filed, signed, over exactly its message."""
        rid = params.get(SIGN_PARAM)
        rec_path = self.qdir / "sign" / f"{rid}.signed.json"
        if not isinstance(rid, str) or not rec_path.exists():
            self.refused.append(repr(rid))
            raise ValueError("the send names no signed request")
        req = json.loads((self.qdir / "sign" / f"{rid}.json").read_text())
        if req["message"] != params.get("message"):
            self.refused.append(rid)
            raise ValueError("the params are not what was signed")
        self.applied.append(dict(params))
        return send_inbox_effect({"body": params.get("message"), "for": params.get("for") or "cli"},
                                 origin_key=origin_key)

    @contextmanager
    def serving(self) -> Iterator[tuple[str, Any]]:
        httpd = make_server(self.src, host="127.0.0.1", **self.kw)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            # the question's caller names its sign request (flow's desk reader does this; the kernel store files none)
            signed = _Signed(httpd.question_store, {self.question.n: QRID})
            httpd.question_store = signed
            self.ck.attach_question_rows(signed)
            yield url, httpd
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def verifies(self, rid: str) -> bool:
        rec = json.loads((self.qdir / "sign" / f"{rid}.signed.json").read_text())
        msg = rec.get("signed_message")
        if msg is None:
            msg = json.loads((self.qdir / "sign" / f"{rid}.json").read_text())["message"]
        return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(self.allowed), "-I", "x", "-n", NS, "-s",
                               str(self.qdir / "sign" / f"{rid}.sig")], input=msg.encode(),
                              capture_output=True).returncode == 0

    def nothing_signed(self, rid: str) -> bool:
        d = self.qdir / "sign"
        return not (d / f"{rid}.sig").exists() and not (d / f"{rid}.signed.json").exists()

    def authenticator(self) -> dict:
        pkcs8 = self.pk.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
        return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                                  "credentialId": base64.b64encode(b"levain-card-cred").decode(),
                                  "userHandle": base64.b64encode(b"levain-card-user").decode()}}


# --- the kernel half: ActionVerb.sign, the verb head, the one extra param -----------------------------------------


def test_action_verb_declares_sign_off_by_default_and_the_head_carries_it() -> None:
    assert {f.name: f.default for f in dataclasses.fields(ActionVerb)}["sign"] is False
    reg = VerbRegistry({"plain": ActionVerb(handler=lambda p: {}, fields=("message",)),
                        "signed": ActionVerb(handler=lambda p: {}, fields=("message",), sign=True)})
    heads = {n: reg.get(n).head("direct-reported", policy=UNKNOWN_POLICY) for n in ("plain", "signed")}
    assert heads["plain"]["sign"] is False and heads["signed"]["sign"] is True
    # a kernel verb is never signed
    assert reg.get("question_answer").sign is False


def test_a_signed_verb_takes_sign_request_beside_its_fields_and_an_unsigned_one_refuses_it(tmp_path, monkeypatch) -> None:
    for sign in (True, False):
        sub = tmp_path / str(sign)
        sub.mkdir()
        rig = _Rig(sub, monkeypatch, sign=sign, compose=sign)
        with rig.serving() as (url, _httpd):
            status, out = _call(url, "/cockpit/verb", {"verb": "send_inbox", "panel_id": "inbox", "confirm": True,
                                                       "intent": "reported", "tier_seen": "T2",
                                                       "gesture_seen": "direct-reported",
                                                       "params": {"message": "hi", SIGN_PARAM: "np-nothing-signed"}},
                                token=TOK)
            if sign:
                # a param the kernel admits, and then refuses: no request of that name was filed and signed (L3 r2 F2),
                # so the door is never reached; never a field refusal
                assert (status, out.get("error")) == (403, "sign_required"), (status, out)
                assert not rig.refused and not rig.applied
            else:
                assert status == 400 and out["error"] == "field_not_allowed" and SIGN_PARAM in out["message"], out
            status, out = _call(url, "/action", {"verb": "send_inbox", "confirm": True,
                                                 "params": {"message": "hi", "other": 1}}, token=TOK)
            assert status == 400 and out["error"] == "field_not_allowed", out


# --- the compose route ----------------------------------------------------------------------------------------------


def test_without_a_compose_signer_the_compose_route_is_404(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch, compose=False)
    with rig.serving() as (url, _httpd):
        status, out = _call(url, "/cockpit/sign/compose", {"verb": "send_inbox", "params": {"message": "hi"}}, token=TOK)
        assert status == 404, out
    assert rig.composed == []


def test_a_compose_signer_needs_a_signer_and_a_callable(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    for bad in ({"signer": None}, {"compose_signer": "not callable"}):
        with pytest.raises(ValueError, match="refusing compose_signer"):
            make_server(rig.src, host="127.0.0.1", **{**rig.kw, "port": 0, **bad})


def test_the_compose_route_rides_the_write_gate(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    body = {"verb": "send_inbox", "params": {"message": "hi"}, "client_txn": TXN}
    with rig.serving() as (url, _httpd):
        for token in (None, "wrong-token"):
            assert _call(url, "/cockpit/sign/compose", body, token=token)[0] == 403, token
        assert _call(url, "/cockpit/sign/compose", body, token=TOK, host="evil.example")[0] == 403
        for sfs in ("cross-site", "same-site", "none"):
            assert _call(url, "/cockpit/sign/compose", body, token=TOK, sfs=sfs)[0] == 403, sfs
        assert _call(url, "/cockpit/sign/compose", body, token=TOK, ctype="text/plain")[0] == 415
        assert rig.composed == []                         # nothing reached the compose signer through a refused gate
        status, out = _call(url, "/cockpit/sign/compose", body, token=TOK, sfs="same-origin")
        assert status == 200 and out == {"request": "np-compose-0001"}, out
    assert rig.composed == [("send_inbox", {"message": "hi"})]


def test_the_compose_route_names_only_a_signed_verb_and_its_own_fields(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        t = {"client_txn": TXN}
        cases = [({"verb": "send_inbox"}, 400, "bad_request"),
                 ({"verb": "send_inbox", "params": [], "x": 1}, 400, "bad_request"),
                 # the draft's identity is required, and is an id
                 ({"verb": "send_inbox", "params": {"message": "hi"}}, 400, "bad_request"),
                 ({"verb": "send_inbox", "params": {"message": "hi"}, "client_txn": "short"}, 400, "bad_request"),
                 ({"verb": "send_inbox", "params": {"message": "hi"}, "client_txn": 12345678}, 400, "bad_request"),
                 ({"verb": "no_such", "params": {}, **t}, 404, "unknown_verb"),
                 ({"verb": "question_answer", "params": {"option": "a"}, **t}, 400, "not_signed"),
                 ({"verb": "send_inbox", "params": "hi", **t}, 400, "bad_request"),
                 ({"verb": "send_inbox", "params": {"message": "hi", "other": 1}, **t}, 400, "field_not_allowed"),
                 ({"verb": "send_inbox", "params": {"message": "hi", SIGN_PARAM: "np-compose-0001"}, **t}, 400,
                  "field_not_allowed")]
        for body, want, word in cases:
            status, out = _call(url, "/cockpit/sign/compose", body, token=TOK)
            assert (status, out.get("error")) == (want, word), (body, status, out)
    assert rig.composed == []


def test_a_compose_signers_value_error_is_a_400_carrying_its_words_and_a_bad_id_is_a_500(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, httpd):
        status, out = _call(url, "/cockpit/sign/compose",
                            {"verb": "send_inbox", "params": {"message": "refuse me"}, "client_txn": TXN}, token=TOK)
        assert status == 400 and out["message"] == "the cockpit is not signing messages now", out
        httpd.compose_signer = lambda verb, params: "no good"
        status, out = _call(url, "/cockpit/sign/compose",
                            {"verb": "send_inbox", "params": {"message": "x"}, "client_txn": "draft-0002-bbbb"}, token=TOK)
        assert status == 500 and out["error"] == "internal", out


def test_the_listing_names_the_page_the_ceremony_is_pinned_to(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        status, out = _call(url, "/cockpit/sign.json", token=TOK)
    assert status == 200 and out["origin"] == rig.origin, out


# --- the page half, in headless Chrome (D6 5 and the compose round trip) ---------------------------------------------

# the answer controls live in the focus view: the first read of them taps the strip's question row, which opens it
OPEN_Q = ("(document.querySelector('#levain-modal .needs-question') || "
          "((r) => r && (r.click(), null))(document.querySelector('#ck-needs .needs-row.needs-question')))")
STRIP_FACET = f"{OPEN_Q} && document.querySelector('#levain-modal .needs-question .sign-facet .sign-option')"
FACET_NOTE = f"(({OPEN_Q}, document.querySelector('#levain-modal .needs-question .sign-note')) || {{}}).textContent"
OPTION_B = ("[...document.querySelectorAll('#levain-modal .needs-question .sign-option')]"
            ".find(b => /^\\(b\\)/.test(b.textContent))")
WRITE_IN = "document.querySelector('#levain-modal .needs-question .sign-write-in')"
WORDS_BTN = ("[...document.querySelectorAll('#levain-modal .needs-question .sign-option')]"
             ".find(b => /sign my words/.test(b.textContent))")
BOX = "document.querySelector('[data-ck-compose=recipient]').closest('.tray-dump')"
BOX_MSG = f"({BOX}.querySelector('.edit-msg') || {{}}).textContent"
TYPE_AND_SEND = ("(() => {{ const b = {box}; const t = b.querySelector('.tray-dump-ta'); t.value = {text}; "
                 "t.dispatchEvent(new Event('input')); b.querySelector('.tray-dump-btn').click(); "
                 "const y = b.querySelector('.verb-confirm'); if (y) y.click(); return 1; }})()")


# whether request %s is still listed open (nothing was signed or denied): the page's own token read of the listing
OPEN = ("fetch('/cockpit/sign.json', {headers: {'X-Levain-Write-Token': 'T'}}).then(r => r.json())"
        ".then(b => b.requests.some(r => r.id === '%s'))")


def _send(text: str) -> dict:
    return {"eval": TYPE_AND_SEND.format(box=BOX, text=json.dumps(text))}


@needs_browser
@needs_ssh
def test_d6_5_a_signed_question_and_a_signed_message_end_to_end(tmp_path, monkeypatch) -> None:
    """D6 5: the card's tap signs through the page's own ceremony; a refused user verification first signs nothing and
    keeps the typed words; the compose box files, signs, then posts the verb with sign_request (the door applies it)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            # the request's own options are the buttons, the recommendation is not; no unsigned answer is drawn
            {"eval": "JSON.stringify({opts: [...document.querySelectorAll('#levain-modal .needs-question .sign-option')]"
                     ".map(b => b.textContent), unsigned: !!document.querySelector('#levain-modal .needs-question "
                     ".question-words'), lines: [...document.querySelectorAll('#levain-modal .needs-question .sign-line')]"
                     ".map(x => x.textContent), blocks: getComputedStyle(document.querySelector('#levain-modal "
                     ".needs-q-body .card-p')).overflowWrap})"},
            # 1. a cancelled passkey on the write-in: nothing signed, the words stay and the box is usable again
            {"user_verified": False},
            {"eval": f"(() => {{ const i = {WRITE_IN}; i.value = 'only after the backup'; "
                     "i.dispatchEvent(new Event('input')); return 1; })()"},
            {"eval": f"{WORDS_BTN}.click() || 1"},
            {"until": f"/did not sign/.test({FACET_NOTE}) && {FACET_NOTE}"},
            {"until": f"!{WRITE_IN}.disabled && {WRITE_IN}.value"},
            {"eval": OPEN % QRID},
            # 2. the same card, verified: option (b) is signed, and the line shown is the server's
            {"user_verified": True},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}"},
            # 3. a compose signer's refusal shows its words and keeps the draft
            {"until": BOX + " && 1"},
            _send("refuse me"),
            {"until": f"/not signing messages now/.test({BOX_MSG}) && {BOX_MSG}"},
            {"eval": f"{BOX}.querySelector('.tray-dump-ta').value"},
            # 4. a cancelled passkey on a message: nothing posted, the draft kept
            {"user_verified": False},
            _send("ship it after the backup"),
            {"until": f"/did not sign/.test({BOX_MSG}) && {BOX_MSG}"},
            {"eval": f"{BOX}.querySelector('.tray-dump-ta').value"},
            {"eval": OPEN % "np-compose-0002"},
            # 5. the retry signs the request already filed and posts the verb with its id; the landed send clears the box
            {"user_verified": True},
            _send("ship it after the backup"),
            {"until": f"{BOX} && {BOX}.querySelector('.tray-dump-ta').value === '' && 1", "ms": 30000},
        ])
    v = out["values"]
    shown = json.loads(v[1])
    assert shown["opts"][:2] == ["(a) Ship it tonight", "(b) Hold for the morning"], shown
    assert shown["opts"][2] == "(*) sign my words" and not shown["unsigned"], shown
    assert shown["lines"][0] == "levain-sign-choice/1 chosen (a): Ship it tonight" and shown["blocks"] == "anywhere", shown
    cancelled, kept = v[4], v[5]
    assert cancelled.startswith("the passkey did not sign (NotAllowedError); nothing was signed"), cancelled
    assert kept == "only after the backup", kept
    assert v[6] is True                                  # the cancelled request is still open: nothing was signed
    signed = v[8]
    assert signed.startswith("signed (b) · by " + rig.pk.key.fingerprint) and signed.endswith(": levain-sign-choice/1 chosen (b): Hold for the morning"), signed
    assert rig.verifies(QRID)
    assert json.loads((rig.qdir / "sign" / f"{QRID}.signed.json").read_text())["choice"] == "b"
    refused_words, refused_draft = v[11], v[12]
    assert "not sent: the cockpit is not signing messages now" in refused_words and refused_draft == "refuse me"
    msg_cancel, msg_draft = v[14], v[15]
    assert "the passkey did not sign (NotAllowedError); nothing was signed" in msg_cancel, msg_cancel
    assert msg_draft == "ship it after the backup"
    assert v[16] is True                                 # the message's request too
    # the compose signer filed ONE request for the message (the retry reused it), and the door applied exactly one send
    assert [p["message"] for _, p in rig.composed] == ["refuse me", "ship it after the backup"], rig.composed
    rid = "np-compose-0002"
    assert rig.verifies(rid)
    # the effect is computed at consent and again at apply: every computation saw the signed request, and one note landed
    assert rig.applied and all(a == {"message": "ship it after the backup", "for": ["fanin"], SIGN_PARAM: rid} for a in rig.applied), rig.applied
    assert [n["body"] for n in rig.inbox.notes.values()] == ["ship it after the backup"]
    verb_posts = [p for p in out["posts"] if p.startswith("/cockpit/verb ") and '"dry_run":true' not in p]
    assert verb_posts and all(f'"{SIGN_PARAM}":"{rid}"' in p for p in verb_posts), out["posts"]
    # nothing reached the verb route before the passkey signed
    first_verb = next(i for i, p in enumerate(out["posts"]) if p.startswith("/cockpit/verb "))
    first_answer = next(i for i, p in enumerate(out["posts"]) if p.startswith(f"/cockpit/sign/{rid}/answer"))
    assert first_answer < first_verb, out["posts"]


@needs_browser
def test_a_page_the_ceremony_is_not_pinned_to_says_where_to_sign_and_draws_no_answer(tmp_path, monkeypatch) -> None:
    """127.0.0.1 is not the pinned http://localhost:<port>: the card names the page to sign on, offers no button, and a
    signed message sends nothing (the compose signer is never asked)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token={TOK}", [
            DESKTOP,
            {"until": f"/^sign on /.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": "document.querySelectorAll('#levain-modal .needs-question .sign-option, #levain-modal .needs-question "
                     ".question-option, #levain-modal .needs-question .question-words').length"},
            {"until": BOX + " && 1"},
            _send("hello"),
            {"until": f"/^sign on /.test({BOX_MSG}) && {BOX_MSG}"},
            {"eval": f"{BOX}.querySelector('.tray-dump-ta').value"},
        ])
    v = out["values"]
    assert v[0] == f"sign on {rig.origin}: this page cannot use your passkey", v[0]
    assert v[1] == 0
    assert v[4].startswith(f"sign on {rig.origin}") and v[5] == "hello", v
    assert rig.composed == [] and rig.nothing_signed(QRID)
    assert not any(p.startswith("/cockpit/verb ") for p in out["posts"]), out["posts"]
