"""The sign card's L3 r2 fixes (project_memory/1023-30/d5/FIX2_BRIEF.md F1-F5), over the rig of test_cockpit_sign_card.

F1: every signed verb is at-most-once, whatever its tier or ``idempotent`` flag: the page keys its post by the draft's
identity and the kernel dedupes that key, so a C1 signed verb whose answer was lost and is sent again applies once.
F2: the kernel's sign check is the invariant at the point of use: ``sign_request`` must name a request this server's
compose filed for THIS verb and THESE params, and the Signer must hold it signed, on every route, dry runs included.
F3: a compose never waits on another draft's compose. F4: make_server pins the Signer before it touches the caller's
cockpit, and a server refused after that leaves the cockpit as it was. F5: a ceremony that throws is "not sure"."""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest

from levain import web_server
from levain.cockpit.verbs import SIGN_PARAM, VerbSpec
from levain.web_server import make_server
from levain.writes import ActionVerb
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import (BOX, BOX_MSG, DESKTOP, FACET_NOTE, OPEN, QRID, STRIP_FACET, TOK, TXN, WORDS_BTN,
                                          WRITE_IN, _Rig, _send, needs_ssh)
from tests.test_cockpit_sign_card_r1 import CLEARED, LOSE_ANSWER, _compose
from tests.test_cockpit_sign_routes import _call


def _c1_rig(tmp_path: Any, monkeypatch: Any) -> _Rig:
    """The rig with ``send_inbox`` a signed C1 VerbSpec that is NOT declared idempotent: it fires inline, with no broker
    pending to key its replay, so only the signed-verb rule makes it at-most-once."""
    rig = _Rig(tmp_path, monkeypatch)

    def fire(scope: Any, params: dict, target: Any, confirm: bool) -> dict:
        rig.handled.append(dict(params))
        return {"summary": "sent"}

    rig.kw["extra_verbs"] = {"send_inbox": VerbSpec(
        "send_inbox", "Send to inbox", "sends a note to the inbox", "panel", fields=("message", "for"), floor="C1",
        tier_fn=lambda p, r, pr, d: "C1", fire=fire, sign=True)}
    rig.kw["extra_effects"] = {}
    return rig


def _signed(rig: _Rig, rid: str) -> None:
    """What the Signer's answer leaves when the passkey signed ``rid`` (its completion marker; the door checks the rest)."""
    (rig.qdir / "sign" / f"{rid}.signed.json").write_text("{}")


# --- F1: a signed verb is at-most-once whatever its tier ---------------------------------------------------------------


def test_a_signed_c1_verb_not_declared_idempotent_dedupes_its_draft_key(tmp_path, monkeypatch) -> None:
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        status, out = _compose(url, "hi", TXN)
        assert status == 200, out
        _signed(rig, out["request"])
        body = {"verb": "send_inbox", "panel_id": "inbox", "params": {"message": "hi", SIGN_PARAM: out["request"]},
                "idempotency_key": "sign-" + TXN}
        first = _call(url, "/cockpit/verb", body, token=TOK)
        again = _call(url, "/cockpit/verb", body, token=TOK)     # the retry of an answer that was lost
    assert first[0] == 200 and first[1]["ok"] is True, first
    assert again[0] == 200 and again[1].get("replayed") is True, again
    assert rig.handled == [{"message": "hi", SIGN_PARAM: out["request"]}], rig.handled


@needs_browser
@needs_ssh
def test_a_lost_answer_to_a_signed_c1_verb_then_a_reload_runs_its_effect_once(tmp_path, monkeypatch) -> None:
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": BOX + " && 1", "ms": 30000},
            {"eval": LOSE_ANSWER.format(route=json.dumps("/cockpit/verb"))},
            _send("hello once"),
            {"until": f"/did not answer|could not be reached/.test({BOX_MSG}) && {BOX_MSG}", "ms": 30000},
            {"eval": "setTimeout(() => location.reload(), 50) && 1"},
            {"eval": "new Promise((r) => setTimeout(() => r(1), 1500))"},
            {"until": f"{BOX} && document.readyState === 'complete' && {BOX}.querySelector('.tray-dump-ta').value === '' && 1",
             "ms": 30000},
            _send("hello once"),
            {"until": CLEARED + " && !/…/.test(" + BOX_MSG + ")", "ms": 30000, "may_timeout": True},
            {"eval": "new Promise((r) => setTimeout(() => r(1), 1000))"},
        ])
    posts = [json.loads(p.split(" ", 1)[1]) for p in out["posts"]
             if p.startswith("/cockpit/verb ") and "hello once" in p and '"dry_run":true' not in p]
    assert len(posts) == 2, out["posts"]
    keys = {p.get("idempotency_key") for p in posts}
    assert len(keys) == 1 and str(next(iter(keys))).startswith("sign-"), posts     # one draft, one key
    assert [h["message"] for h in rig.handled] == ["hello once"], rig.handled


# --- F2: the sign request names a signed request bound to this verb and these params ---------------------------------


ENV = {"verb": "send_inbox", "panel_id": "inbox", "confirm": True, "intent": "reported", "tier_seen": "T2",
       "gesture_seen": "direct-reported"}


def test_a_sign_request_must_be_signed_and_filed_for_this_verb_and_these_params(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rig.kw["extra_verbs"] = {**rig.kw["extra_verbs"],
                             "send_other": ActionVerb(handler=lambda p: rig.handled.append(dict(p)) or {"summary": "x"},
                                                      label="Other", fields=("message", "for"), sign=True)}
    with rig.serving() as (url, _httpd):
        rid = _compose(url, "hi", TXN)[1]["request"]
        refusals = []

        def refused(route: str, body: dict) -> None:
            status, out = _call(url, route, body, token=TOK)
            refusals.append((route, body.get("verb"), body["params"], (status, out.get("error"))))

        def each(params: dict, verb: str = "send_inbox") -> None:
            refused("/cockpit/verb", {**ENV, "verb": verb, "params": params})
            refused("/cockpit/verb", {"verb": verb, "panel_id": "inbox", "dry_run": True, "params": params})
            refused("/action", {"verb": verb, "confirm": True, "params": params})

        each({"message": "hi", SIGN_PARAM: "np-nothing-0001"})       # format-valid, filed by nobody
        each({"message": "hi", SIGN_PARAM: rid})                     # filed for these words, never signed
        _signed(rig, rid)
        each({"message": "bye", SIGN_PARAM: rid})                    # signed, for other words
        each({"message": "hi", "for": "fanin", SIGN_PARAM: rid})     # signed, for these words plus one more param
        refused("/action", {"verb": "send_other", "confirm": True,   # signed, for another verb
                            "params": {"message": "hi", SIGN_PARAM: rid}})
        bad = [r for r in refusals if r[3] != (403, "sign_required")]
        assert not bad, bad
        assert rig.refused == [] and rig.applied == [] and rig.handled == []    # no door was ever reached
        # the one it was filed and signed for passes the kernel, dry run and fire alike
        good = {"message": "hi", SIGN_PARAM: rid}
        status, out = _call(url, "/cockpit/verb", {"verb": "send_inbox", "panel_id": "inbox", "dry_run": True,
                                                   "params": good, "idempotency_key": "sign-" + TXN}, token=TOK)
        assert status == 200 and out["dry_run"] is True, out
        status, out = _call(url, "/cockpit/verb", {**ENV, "params": good, "idempotency_key": "sign-" + TXN}, token=TOK)
        assert status == 200, out
    assert rig.notes() == ["hi"] and all(a == good for a in rig.applied), (rig.notes(), rig.applied, rig.refused)


def test_a_signed_request_is_sent_only_under_its_drafts_key(tmp_path, monkeypatch) -> None:
    """Q1 (head 1023+30): a signed request's key is "sign-" + the draft id its compose filed it under, so a re-post of
    that request under a fresh key (or none) is refused, dry runs included, and its effect runs once."""
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        rid = _compose(url, "hi", TXN)[1]["request"]
        _signed(rig, rid)
        good = {"verb": "send_inbox", "panel_id": "inbox", "params": {"message": "hi", SIGN_PARAM: rid}}
        first = _call(url, "/cockpit/verb", {**good, "idempotency_key": "sign-" + TXN}, token=TOK)
        assert first[0] == 200 and first[1]["ok"] is True, first
        outs = []
        for extra in ({"idempotency_key": "sign-draft-9999-zzzz"}, {"idempotency_key": "fresh-key-0001"}, {},
                      {"idempotency_key": "sign-draft-9999-zzzz", "dry_run": True}, {"dry_run": True}):
            status, out = _call(url, "/cockpit/verb", {**good, **extra}, token=TOK)
            outs.append((extra, status, out.get("error")))
        assert all(o[1:] == (403, "sign_required") for o in outs), outs
        status, out = _call(url, "/cockpit/verb", {**good, "dry_run": True, "idempotency_key": "sign-" + TXN}, token=TOK)
        assert status == 200 and out["dry_run"] is True, out
    assert rig.handled == [{"message": "hi", SIGN_PARAM: rid}], rig.handled


# --- F3: one draft's compose never holds up another's --------------------------------------------------------------


def test_a_slow_compose_for_one_draft_does_not_delay_another(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    with rig.serving() as (url, httpd):
        filed = httpd.compose_signer

        def compose(verb: str, params: dict) -> str:
            if params.get("message") == "slow":
                entered.set()
                release.wait(20)
            return filed(verb, params)

        httpd.compose_signer = compose
        outs: dict[str, Any] = {}
        a = threading.Thread(target=lambda: outs.__setitem__("a", _compose(url, "slow", TXN)))
        a2 = threading.Thread(target=lambda: outs.__setitem__("a2", _compose(url, "slow", TXN)))
        b = threading.Thread(target=lambda: outs.__setitem__("b", _compose(url, "fast", "draft-0002-bbbb")))
        try:
            a.start()
            assert entered.wait(10)
            a2.start()                  # the same draft again, while its first compose is still filing: it waits
            t0 = time.monotonic()
            b.start()
            b.join(timeout=5)
            took = time.monotonic() - t0
            assert not b.is_alive() and outs.get("b") == (200, {"request": "np-compose-0001"}), (outs, took)
            assert "a" not in outs and "a2" not in outs
        finally:
            release.set()
            for t in (a, a2, b):
                t.join(timeout=20)
    assert outs["a"] == outs["a2"] == (200, {"request": "np-compose-0002"}), outs
    assert [p["message"] for _, p in rig.composed] == ["fast", "slow"], rig.composed


# --- F4: the Signer is pinned before the caller's cockpit is touched ---------------------------------------------------

_ATTACH = ("attach_server", "attach_verbs", "attach_authority", "attach_edits_reader", "attach_decision_rows",
           "attach_consent_rows", "attach_question_rows", "attach_note_rows")


def test_a_server_refused_after_the_bind_leaves_the_cockpit_unmodified_and_closes(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    calls: list[str] = []
    for name in _ATTACH:
        real = getattr(rig.ck, name)
        monkeypatch.setattr(rig.ck, name, lambda *a, _n=name, _r=real, **k: (calls.append(_n), _r(*a, **k))[1])
    real_broker = web_server._build_broker

    def moved(*a: Any, **k: Any) -> Any:
        rig.signer.page_origin = "https://elsewhere.example"     # moved between the pre-bind check and the pin
        return real_broker(*a, **k)

    monkeypatch.setattr(web_server, "_build_broker", moved)
    with pytest.raises(ValueError, match="refusing signer"):
        make_server(rig.src, host="127.0.0.1", **rig.kw)
    assert calls == [], calls
    with socket.socket() as s:                                   # the refused server's socket was closed
        s.bind(("127.0.0.1", rig.port))

    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("the broker could not be built")

    rig.signer.page_origin = rig.origin
    monkeypatch.setattr(web_server, "_build_broker", broken)
    with pytest.raises(RuntimeError, match="could not be built"):
        make_server(rig.src, host="127.0.0.1", **rig.kw)
    assert calls == [], calls
    with socket.socket() as s:
        s.bind(("127.0.0.1", rig.port))
    rig.signer.page_origin = "http://localhost:1"                # a failure before the pin pins nothing
    rig.signer.page_origin = rig.origin
    monkeypatch.setattr(web_server, "_build_broker", real_broker)
    with rig.serving() as (_url, httpd):
        assert httpd.signer_page_origin == rig.origin
        # one transaction attaches everything (the rig's serving() then attaches its own question store)
        assert calls == ["attach_server", "attach_question_rows"], calls
        ck, dec = rig.ck, rig.ck._dec
        assert None not in (ck._verb_view, ck._capture, ck._edits_reader, dec.rows, dec.consent, dec.notes, dec.spec)


# --- F5: a ceremony that throws is "not sure", and the write-in's words open again -----------------------------------

# throws (not rejects) on the first POST whose path ends with `tail`: signCeremony itself throws, before its own catch
THROWS = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
          "if (armed && String(u).endsWith({tail})) {{ armed = false; throw new TypeError('the page broke'); }} "
          "return real.apply(this, arguments); }}; return 1; }})()")


@needs_browser
@needs_ssh
def test_a_ceremony_that_throws_before_it_posts_signs_nothing_and_releases_the_write_in(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": THROWS.format(tail=json.dumps("/challenge"))},
            {"eval": f"(() => {{ const i = {WRITE_IN}; i.value = 'only after the backup'; "
                     "i.dispatchEvent(new Event('input')); return 1; })()"},
            {"eval": f"{WORDS_BTN}.click() || 1"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "may_timeout": True},
            {"until": f"!{WRITE_IN}.disabled && {WRITE_IN}.value", "may_timeout": True},
            {"eval": OPEN % QRID},
        ])
    v = out["values"]
    assert "the page failed (TypeError); nothing was signed" in str(v[4]), v   # the throw came before any answer was posted
    assert v[5] == "only after the backup", v           # released
    assert v[6] is True and rig.nothing_signed(QRID)