"""ONESURFACE OS-2, the consent family on the cockpit: a consent request answered (pending) or ruled (undecided) with a
passkey through the cockpit server, by the phone port's OWN steps (:class:`levain.web_server.ConsentSheet` calls
``levain.phone_server._PhoneServer.answer_step`` / ``rule_step`` on itself), over the Signer's queue, rpId and pinned page.
These are the phone port's consent tests (tests/test_phone_server.py) ported onto ``/cockpit/consent/<id>/...``, run
against a real authority log (a laptop key and a software passkey enrolled on ``phone.example``), a real consent queue
and real ``ssh-keygen`` verification; plus the cockpit's own: the sign routes' gate, the sheet's read, and the refusal
before any ceremony on a page the queue's re-verification would refuse.

Reproduced red before the build: with a Signer attached, ``POST /cockpit/consent/<id>/challenge`` and
``GET /cockpit/consent/<id>/sheet.json`` were 404."""
from __future__ import annotations

import json
import shutil
import urllib.parse
from datetime import timedelta
from typing import Any

import pytest

from levain import authority as auth
from levain.consent.answering import passkey_keys
from levain.consent.passkey import challenge
from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.record import RULING_NAMESPACE, answer_message, ruling_message
from levain.consent.sign import Signer
from tests._h4 import h4_home
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_dispatch_phone import _b64u
from tests.test_grant_phone import RP, world  # noqa: F401  (world is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NS = "flow-needs-phill"
PAGE = f"https://{RP}"
PORTED = f"https://{RP}:8443"


def _signer(world: Any, origin: str = PAGE) -> Signer:
    source = authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root))
    signer = Signer(world.qdir, (NS,), RP, lambda: {k.fingerprint: k for k in passkey_keys(source(), RP)})
    signer.page_origin = origin
    return signer


def _queue(world: Any) -> ConsentQueue:
    return ConsentQueue(world.qdir, authority=authority_source(world.root / "cockpit.db",
                                                               anchor=auth.anchor_path(world.root)), home=h4_home())


def _file(world: Any, tier: str = "T2", *, name: str = "a.txt", hold: timedelta = timedelta(minutes=5),
          content: str = "hi") -> str:
    return _queue(world).file(agent="claude_code", agent_session_id="s1", tool_name="Write",
                              tool_input={"file_path": str(world.ws / name), "content": content}, cwd=str(world.ws),
                              hold=hold, tier=tier).id


def _q(rid: str) -> str:
    return urllib.parse.quote(rid, safe="")


@pytest.fixture
def home(world, monkeypatch):
    """The cockpit's consent cards are read from the Signer's queue (the condition the routes are served on)."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    return world


def test_the_consent_routes_ride_the_sign_gate(home, tmp_path) -> None:
    rid = _file(home)
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        base = f"/cockpit/consent/{_q(rid)}"
        assert _call(url, base + "/sheet.json", token=None)[0] == 403
        assert _call(url, base + "/sheet.json", sfs="cross-site")[0] == 403
        for token in (None, "wrong-token"):
            status, out = _call(url, base + "/challenge", {"decision": "deny", "signer_fp": home.phone.fp},
                                token=token)
            assert (status, out.get("error")) == (403, "credential_required"), (token, status, out)
        assert _call(url, base + "/challenge", {"decision": "deny", "signer_fp": home.phone.fp},
                     ctype="text/plain")[0] == 415
        assert _call(url, base + "/challenge", {"decision": "deny", "signer_fp": home.phone.fp}, sfs="cross-site")[0] == 403
        # an action the phone port does not serve is no route
        assert _call(url, base + "/withdraw", {})[0] == 404
    assert _queue(home).get(rid)[1] == "pending"


def test_without_a_signer_or_over_another_queue_no_consent_route_exists(home, tmp_path, monkeypatch) -> None:
    rid = _file(home)
    with _serving(tmp_path / "inst") as (url, _httpd):
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")[0] == 404
        assert _call(url, f"/sign/consent/{_q(rid)}")[0] == 404
        for action in ("challenge", "answer", "rule-challenge", "rule"):
            assert _call(url, f"/cockpit/consent/{_q(rid)}/{action}", {})[0] == 404
        status, body = _call(url, f"/cockpit/consent/{_q(rid)}.json")
        assert status == 200 and body["sheet"] is False, body       # the card keeps its copy lines
    # a Signer over another queue than the one the consent cards are read from: a button on one card must never answer
    # a same-id request in another queue
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "elsewhere"))
    with _serving(tmp_path / "inst2", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")[0] == 404
        assert _call(url, f"/cockpit/consent/{_q(rid)}/challenge",
                     {"decision": "deny", "signer_fp": home.phone.fp})[0] == 404


def test_a_challenge_is_the_answer_record_and_a_passkey_never_allows_t3(home, tmp_path) -> None:
    """Ported: test_phone_server.test_a_challenge_is_the_answer_record_and_a_passkey_never_allows_t3."""
    t2, t3 = _file(home, "T2", name="a"), _file(home, "T3", name="b")
    fp = home.phone.fp
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        status, body = _call(url, f"/cockpit/consent/{_q(t2)}/challenge", {"decision": "allow", "signer_fp": fp})
        assert status == 200 and body["rp_id"] == RP and body["payload"]["signer_fp"] == fp, body
        assert body["challenge"] == _b64u(challenge(answer_message(body["payload"])))
        assert body["line"] == answer_message(body["payload"]).decode()      # the exact text signed
        status, body = _call(url, f"/cockpit/consent/{_q(t3)}/challenge", {"decision": "allow", "signer_fp": fp})
        assert (status, body["error"]) == (422, "answer_refused") and "T3" in body["message"], body
        assert _call(url, f"/cockpit/consent/{_q(t3)}/challenge", {"decision": "deny", "signer_fp": fp})[0] == 200
        status, body = _call(url, f"/cockpit/consent/{_q(t2)}/challenge",
                             {"decision": "allow", "signer_fp": "SHA256:nope"})
        assert (status, body["error"]) == (422, "no_passkey")
        status, body = _call(url, f"/cockpit/consent/{_q(t2)}/challenge", {"decision": "allow"})
        assert (status, body["error"]) == (400, "bad_request")
        # a key the phone port's body never carries is refused, as there (the exact key set)
        status, body = _call(url, f"/cockpit/consent/{_q(t2)}/challenge",
                             {"decision": "allow", "signer_fp": fp, "request_sha256": "x"})
        assert (status, body["error"]) == (400, "bad_request")
        assert _call(url, "/cockpit/consent/nope/challenge", {"decision": "deny", "signer_fp": fp})[0] == 404


def test_a_passkey_answer_verifies_once_and_a_foreign_signature_records_nothing(home, tmp_path) -> None:
    """Ported: test_phone_server.test_an_answer_whose_signature_is_not_the_passkeys_is_refused_and_records_nothing, then
    the answer the phone page makes, end to end in the queue."""
    rid = _file(home)
    fp = home.phone.fp
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        base = f"/cockpit/consent/{_q(rid)}"
        c = _call(url, base + "/challenge", {"decision": "allow", "signer_fp": fp})[1]
        # signed on another page than the one the cockpit is pinned to: refused, nothing recorded
        status, out = _call(url, base + "/answer", {"payload": c["payload"],
                                                    **home.phone.assertion(c["challenge"], origin=PORTED)})
        assert (status, out["error"]) == (422, "answer_refused"), out
        # a payload the signature is not over
        other = dict(c["payload"], decision="deny")
        status, out = _call(url, base + "/answer", {"payload": other, **home.phone.assertion(c["challenge"], origin=PAGE)})
        assert (status, out["error"]) == (422, "answer_refused"), out
        assert _queue(home).get(rid)[1] == "pending"
        status, out = _call(url, base + "/answer", {"payload": c["payload"], **home.phone.assertion(c["challenge"], origin=PAGE)})
        assert status == 200 and out["decision"] == "allow" and out["signer_fp"] == fp, out
        assert _queue(home).get(rid)[1] == "allowed"
        # answered: the sheet reads it closed, and a second answer is a conflict
        assert _call(url, base + "/sheet.json") == (404, {"error": "not_open",
                                                          "message": "this consent request is not open"})
        status, out = _call(url, base + "/answer", {"payload": c["payload"], **home.phone.assertion(c["challenge"], origin=PAGE)})
        assert (status, out["error"]) == (409, "conflict"), out


def test_the_sheet_reads_a_pending_request_with_what_a_passkey_may_sign(home, tmp_path) -> None:
    t2, t3 = _file(home, "T2", name="a"), _file(home, "T3", name="b")
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        status, got = _call(url, f"/cockpit/consent/{_q(t2)}/sheet.json")
        assert status == 200 and got["origin"] == PAGE and got["rp_id"] == RP, got
        r = got["request"]
        assert (r["id"], r["kind"], r["mode"], r["state"]) == (t2, "consent", "answer", "pending"), r
        assert r["signers"] == got["signers"] == [home.phone.fp] and r["requester"] == "claude_code"
        assert [o["key"] for o in r["options"]] == ["allow", "deny"] and len(r["sha256"]) == 64, r
        assert "a" in r["message"] and r["title"].startswith("Write")
        # T3: a passkey denies it but never allows it, and the sheet says where it is allowed
        r3 = _call(url, f"/cockpit/consent/{_q(t3)}/sheet.json")[1]["request"]
        assert [o["key"] for o in r3["options"]] == ["deny"] and "laptop" in r3["note"], r3
        # the card's body names the sheet
        status, body = _call(url, f"/cockpit/consent/{_q(t2)}.json")
        assert status == 200 and body["sheet"] is True and body["sheet_v"] == r["sha256"], body
        # the sheet's page is the same shell as the sign sheet's
        status, page = _call(url, f"/sign/consent/{_q(t2)}", token=None)
        assert status == 200 and b"/sign.js" in page


def test_an_undecided_request_is_ruled_in_the_ruling_namespace_and_delivered(home, tmp_path, monkeypatch) -> None:
    """The late ruling (``levain consent rule`` on the phone's ``rule-challenge`` / ``rule``): only an undecided request
    takes one, signed in the ruling namespace; its delivery runs once it verifies (stubbed here: no session resumes).
    L3 r1 C1: the ruling is answered as soon as it is recorded, never held on its delivery (the stub below does not
    return until after the reply has arrived; held, the reply would wait out the delivery)."""
    import threading

    from levain.consent import delivery

    delivered: list[str] = []
    release, finished = threading.Event(), threading.Event()

    def slow_deliver(queue, request_id, notify=None):
        release.wait(30)
        delivered.append(request_id)
        finished.set()
        return type("D", (), {"route": "hook", "reason": "stub"})()

    monkeypatch.setattr(delivery, "deliver", slow_deliver)
    rid = _file(home, hold=timedelta(microseconds=1))
    assert _queue(home).mark_undecided(rid).state == "undecided"
    fp = home.phone.fp
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        base = f"/cockpit/consent/{_q(rid)}"
        r = _call(url, base + "/sheet.json")[1]["request"]
        assert (r["mode"], r["state"]) == ("rule", "undecided"), r
        # an answer is refused on an undecided request; a ruling is the phone's own
        assert _call(url, base + "/challenge", {"decision": "allow", "signer_fp": fp})[0] == 409
        status, c = _call(url, base + "/rule-challenge", {"decision": "allow", "signer_fp": fp})
        assert status == 200 and c["challenge"] == _b64u(challenge(ruling_message(c["payload"]), RULING_NAMESPACE))
        assert c["line"] == ruling_message(c["payload"]).decode()
        status, out = _call(url, base + "/rule", {"payload": c["payload"], **home.phone.assertion(c["challenge"], origin=PAGE)})
        assert status == 200 and out["decision"] == "allow" and out["delivery"]["route"] == "pending", out
        assert delivered == [] and _queue(home).get(rid)[1] == "allowed"     # recorded, its delivery still running
        release.set()
        assert finished.wait(30)
    assert delivered == [rid]


def test_a_cockpit_whose_page_the_queue_would_refuse_starts_no_ceremony(home, tmp_path) -> None:
    """The queue re-verifies a passkey's answer against ``https://<rpId>`` with no port, at answer and at every delivery
    (levain.consent.passkey.answer_signature_problem), so an answer signed on a cockpit served on :8443 can never land.
    The sheet lists no signer there and says why; every step is refused before a challenge is made."""
    rid = _file(home)
    with _serving(tmp_path / "inst", signer=_signer(home, origin=PORTED), extra_hosts=[RP]) as (url, _httpd):
        status, got = _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")
        assert status == 200 and got["signers"] == [] and PORTED in got["note"], got
        status, out = _call(url, f"/cockpit/consent/{_q(rid)}/challenge",
                            {"decision": "deny", "signer_fp": home.phone.fp})
        assert (status, out["error"]) == (422, "answer_refused") and "nothing was signed" in out["message"], out
    assert _queue(home).get(rid)[1] == "pending"


def test_the_refusal_before_the_ceremony_stands_on_the_queues_own_refusal(home, tmp_path, monkeypatch) -> None:
    """The premise of the refusal above, measured: with it lifted, an answer signed on the ported page converts (the page
    is the pinned one) and the queue itself refuses it, recording nothing."""
    from levain import web_server

    monkeypatch.setattr(web_server.ConsentSheet, "elsewhere", lambda self: None)
    rid = _file(home)
    with _serving(tmp_path / "inst", signer=_signer(home, origin=PORTED), extra_hosts=[RP]) as (url, _httpd):
        base = f"/cockpit/consent/{_q(rid)}"
        status, c = _call(url, base + "/challenge", {"decision": "deny", "signer_fp": home.phone.fp})
        assert status == 200, c
        status, out = _call(url, base + "/answer", {"payload": c["payload"],
                                                    **home.phone.assertion(c["challenge"], origin=PORTED)})
        assert (status, out["error"]) == (422, "answer_refused") and f"https://{RP}" in out["message"], out
    assert _queue(home).get(rid)[1] == "pending"


def test_a_consent_link_opened_from_another_site_reaches_the_sheet_and_nothing_else(home, tmp_path) -> None:
    """L3 r1 C2: a consent sheet link opened from mail or another site is a top-level navigation, admitted as
    ``/sign/<id>`` is (the sign sheet's cross-site test, on the consent sheet); its data stays same-origin only."""
    rid = _file(home)
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        page = f"/sign/consent/{_q(rid)}"
        status, body = _call(url, page, token=None, extra=nav)
        assert status == 200 and b"/sign.js" in body
        assert _call(url, page + "?utm=x", token=None, extra=nav)[0] == 200
        assert _call(url, page, token=None, extra={**nav, "Sec-Fetch-Dest": "iframe"})[0] == 403
        assert _call(url, page, token=None, extra={**nav, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})[0] == 403
        assert _call(url, page + "/", token=None, extra=nav)[0] == 403
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json", extra=nav)[0] == 403


def test_the_card_body_names_only_the_answers_the_sheet_offers(home, tmp_path) -> None:
    """L3 r1 C3: ``sheet_choices`` is the sheet row's own options, so a card routes to the sheet only an answer a passkey
    may sign here: T2 both, T3 deny only, and none where no passkey may sign (a ported page) while the sheet still opens
    to say why."""
    t2, t3 = _file(home, "T2", name="a"), _file(home, "T3", name="b")
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        b2 = _call(url, f"/cockpit/consent/{_q(t2)}.json")[1]
        b3 = _call(url, f"/cockpit/consent/{_q(t3)}.json")[1]
        assert b2["sheet_choices"] == ["allow", "deny"] and b3["sheet_choices"] == ["deny"], (b2, b3)
        assert b3["sheet_v"] == _call(url, f"/cockpit/consent/{_q(t3)}/sheet.json")[1]["request"]["sha256"]
    with _serving(tmp_path / "inst2", signer=_signer(home, origin=PORTED), extra_hosts=[RP]) as (url, _httpd):
        b2 = _call(url, f"/cockpit/consent/{_q(t2)}.json")[1]
        assert b2["sheet"] is True and b2["sheet_choices"] == [], b2


def test_a_request_past_its_hold_inside_the_grace_is_not_open_to_the_sheet(home, tmp_path) -> None:
    """L3 r1 C4: past ``expires_at`` but inside UNDECIDED_GRACE the queue still reads the request pending (for the
    hook's undecided mark) and applies no answer to it, so the sheet reads it closed and the card names no sheet."""
    rid = _file(home, hold=timedelta(microseconds=1))
    assert _queue(home).get(rid)[1] == "pending"            # inside the grace: the queue's own read
    with _serving(tmp_path / "inst", signer=_signer(home), extra_hosts=[RP]) as (url, _httpd):
        assert _call(url, f"/cockpit/consent/{_q(rid)}/sheet.json")[0] == 404
        status, body = _call(url, f"/cockpit/consent/{_q(rid)}.json")
        if status == 200:                                    # the listing may already show it past its hold
            assert body["sheet"] is False and body["sheet_choices"] == [], body


def test_the_version_binds_every_field_the_sheet_shows_as_decided() -> None:
    """L3 r1 C5: the sheet's version changes when any field it shows as the thing decided changes."""
    from levain.web_server import ConsentSheet

    allow = {"key": "allow", "text": "Allow", "line": "answer allow: request r1"}
    deny = {"key": "deny", "text": "Deny", "line": "answer deny: request r1"}
    row = {"id": "r1", "state": "pending", "title": "Write · T2", "requester": "claude_code", "message": "m",
           "expires_at": "2026-10-26T00:00:00Z", "options": [allow, deny]}
    base = ConsentSheet.version(row, "d")
    for key, value in (("title", "Bash · T2"), ("requester", "codex"), ("message", "m2"), ("expires_at", None),
                       ("state", "undecided"), ("options", [deny]), ("note", "laptop only")):
        assert ConsentSheet.version({**row, key: value}, "d") != base, key
    assert ConsentSheet.version(row, "d2") != base


def test_the_version_binds_each_options_shown_words_and_signed_line() -> None:
    """L3 r2 C5: an option whose shown text or exact signed line changes under the same key reads as changed, so an
    open card's link never preselects words it did not show."""
    from levain.web_server import ConsentSheet

    allow = {"key": "allow", "text": "Allow", "line": "answer allow: request r1"}
    deny = {"key": "deny", "text": "Deny", "line": "answer deny: request r1"}
    row = {"id": "r1", "state": "pending", "title": "Write · T2", "requester": "claude_code", "message": "m",
           "expires_at": "2026-10-26T00:00:00Z", "options": [allow, deny]}
    base = ConsentSheet.version(row, "d")
    for field, value in (("text", "Allow always"), ("line", "rule allow: request r1")):
        assert ConsentSheet.version({**row, "options": [{**allow, field: value}, deny]}, "d") != base, field
    # the same content reads as the same version, whatever order a dict's keys were built in
    swapped = [{"line": o["line"], "text": o["text"], "key": o["key"]} for o in row["options"]]
    assert ConsentSheet.version({**row, "options": swapped}, "d") == base
