"""The sign card's L3 r3 fixes (project_memory/1023-30/d5/FIX3_BRIEF.md G1, G2), over the rig of test_cockpit_sign_card.

G1: the binding a compose files, ``(verb, client_txn) -> (params fingerprint, request id)``, is durable and kernel-owned,
kept beside the Signer's sign directory, and the kernel's sign check reads it there. A signed effect that landed and lost
its answer is answered by the at-most-once gate's replay after a restart, or after any number of other drafts, instead
of a 403 that drops the draft and asks the passkey again (and runs the effect twice). A binding leaves only with its
request: denied, gone, or expired before it was signed.
G2: a make_server that fails after it pinned the Signer releases the pin and leaves the caller's cockpit as it was."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from levain.cockpit.verbs import SIGN_PARAM
from levain.consent.sign import sign_dir
from levain.web_server import make_server
from levain.writes import ActionVerb
from tests.test_cockpit_sign_card import TOK, TXN, _Rig, _write_request
from tests.test_cockpit_sign_card_r1 import _compose
from tests.test_cockpit_sign_card_r2 import _c1_rig, _signed
from tests.test_cockpit_sign_routes import _call


def _send(url: str, rid: str, message: str = "hi", txn: str = TXN) -> "tuple[int, Any]":
    return _call(url, "/cockpit/verb", {"verb": "send_inbox", "panel_id": "inbox",
                                        "params": {"message": message, SIGN_PARAM: rid},
                                        "idempotency_key": "sign-" + txn}, token=TOK)


def _store(rig: _Rig) -> Any:
    from levain.consent.sign import COMPOSE_BINDINGS     # imported here so the red run against 3cd6284a collects

    return sign_dir(rig.qdir) / COMPOSE_BINDINGS


def _bindings(rig: _Rig) -> list[dict]:
    path = _store(rig)
    return json.loads(path.read_text()) if path.exists() else []


# --- G1: the compose binding outlives the server that filed it ---------------------------------------------------------


def test_a_lost_answer_retried_on_a_restarted_server_replays_and_the_effect_runs_once(tmp_path, monkeypatch) -> None:
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        status, out = _compose(url, "hi", TXN)
        assert status == 200, out
        rid = out["request"]
        _signed(rig, rid)
        first = _send(url, rid)                 # the effect lands; its answer is what the page never reads
    assert first[0] == 200 and first[1]["ok"] is True, first
    with rig.serving() as (url, _httpd):    # a restart: a new server over the same directories
        again = _send(url, rid)
    assert again[0] == 200 and again[1].get("replayed") is True, again
    assert rig.handled == [{"message": "hi", SIGN_PARAM: rid}], rig.handled


def test_a_lost_answer_retried_after_300_other_drafts_replays_and_the_effect_runs_once(tmp_path, monkeypatch) -> None:
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        rid = _compose(url, "hi", TXN)[1]["request"]
        _signed(rig, rid)
        first = _send(url, rid)
        assert first[0] == 200 and first[1]["ok"] is True, first
        for i in range(300):
            status, out = _compose(url, f"other {i}", f"draft-{i:04d}-other")
            assert status == 200, (i, out)
        again = _send(url, rid)
    assert again[0] == 200 and again[1].get("replayed") is True, again
    assert rig.handled == [{"message": "hi", SIGN_PARAM: rid}], rig.handled


def test_a_restarted_server_refuses_a_request_bound_to_other_params_another_verb_or_nothing(tmp_path,
                                                                                            monkeypatch) -> None:
    rig = _c1_rig(tmp_path, monkeypatch)
    rig.kw["extra_verbs"] = {**rig.kw["extra_verbs"],
                             "send_other": ActionVerb(handler=lambda p: rig.handled.append(dict(p)) or {"summary": "x"},
                                                      label="Other", fields=("message", "for"), sign=True)}
    with rig.serving() as (url, _httpd):
        rid = _compose(url, "hi", TXN)[1]["request"]
    _signed(rig, rid)
    _write_request(rig.qdir, "np-unbound-0001", "hi")      # filed and signed, but by no compose
    _signed(rig, "np-unbound-0001")
    with rig.serving() as (url, _httpd):
        outs = [_send(url, rid, "bye"),                                         # other params
                _call(url, "/action", {"verb": "send_other", "confirm": True,      # another verb
                                       "params": {"message": "hi", SIGN_PARAM: rid},
                                       "idempotency_key": "sign-" + TXN}, token=TOK),
                _send(url, "np-unbound-0001")]                                  # no binding at all
        bad = [o for o in outs if (o[0], o[1].get("error")) != (403, "sign_required")]
        assert not bad, bad
        assert rig.handled == [] and rig.applied == []
        good = _send(url, rid)                                                  # the bound one still passes
    assert good[0] == 200, good
    assert rig.handled == [{"message": "hi", SIGN_PARAM: rid}], rig.handled


def test_a_restarted_server_gives_a_draft_its_open_request_and_refuses_its_id_for_other_words(tmp_path,
                                                                                            monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        first = _compose(url, "hi", TXN)
    with rig.serving() as (url, _httpd):
        again = _compose(url, "hi", TXN)
        other = _compose(url, "bye", TXN)
    assert first == again == (200, {"request": "np-compose-0001"}), (first, again)
    assert other[0] == 409 and other[1]["error"] == "txn_conflict", other
    assert len(rig.composed) == 1, rig.composed


def test_a_binding_leaves_only_with_its_request(tmp_path, monkeypatch) -> None:
    """Denied, gone, or expired while open: the binding is pruned at the next compose, and so is any binding filed more
    than BINDING_KEEP_S ago. A signed one stays, whatever its request's expiry, and so does an open, unexpired one; a
    re-compose of a draft whose request was signed files a new request beside it and never drops the signed binding."""
    from levain.consent.sign import BINDING_KEEP_S

    rig = _c1_rig(tmp_path, monkeypatch)
    d = sign_dir(rig.qdir)
    with rig.serving() as (url, _httpd):
        ids = [_compose(url, f"m{i}", f"draft-{i:04d}-prune")[1]["request"] for i in range(6)]
        _signed(rig, ids[5])                                           # signed, but bound too long ago
        old = _bindings(rig)
        for b in old:
            if b["request"] == ids[5]:
                b["filed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=BINDING_KEEP_S + 60)).isoformat()
        _store(rig).write_text(json.dumps(old))
        (d / f"{ids[0]}.denied").write_text("{}")                     # denied
        (d / f"{ids[1]}.json").unlink()                                # gone
        stale = json.loads((d / f"{ids[2]}.json").read_text())         # expired before it was signed
        stale["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        (d / f"{ids[2]}.json").write_text(json.dumps(stale))
        signed = json.loads((d / f"{ids[3]}.json").read_text())        # signed, then expired: it stays
        signed["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        (d / f"{ids[3]}.json").write_text(json.dumps(signed))
        _signed(rig, ids[3])
        again = _compose(url, "m3", "draft-0003-prune")                # the signed draft composed again
        assert again[0] == 200 and again[1]["request"] not in ids, again
        _compose(url, "later", "draft-9999-later")
        kept = {(b["txn"], b["request"]) for b in _bindings(rig)}
        status, out = _send(url, ids[3], "m3", "draft-0003-prune")     # the signed one still sends
        aged = _send(url, ids[5], "m5", "draft-0005-prune")             # the aged one is refused
    assert kept == {("draft-0003-prune", ids[3]), ("draft-0003-prune", again[1]["request"]),
                    ("draft-0004-prune", ids[4]), ("draft-9999-later", "np-compose-0008")}, kept
    assert status == 200, out
    assert (aged[0], aged[1].get("error")) == (403, "sign_required"), aged
    assert rig.handled == [{"message": "m3", SIGN_PARAM: ids[3]}], rig.handled


def test_an_unreadable_binding_store_is_unavailable_never_a_refusal_that_drops_the_draft(tmp_path,
                                                                                       monkeypatch) -> None:
    rig = _c1_rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        rid = _compose(url, "hi", TXN)[1]["request"]
        _signed(rig, rid)
        path = _store(rig)
        path.write_text("{not json")
        sent = _send(url, rid)
        composed = _compose(url, "new", "draft-0002-bbbb")
        assert path.read_text() == "{not json"                          # never overwritten
    assert (sent[0], sent[1].get("error")) == (503, "unavailable"), sent
    assert composed[0] == 503, composed
    assert rig.handled == [], rig.handled


# --- G2: a server refused after the pin releases it and leaves the cockpit as it was ----------------------------------


def test_attachments_takes_every_decisions_field() -> None:
    from levain.cockpit.engine import _DEC_FIELDS, _Decisions

    assert set(_DEC_FIELDS) == set(_Decisions.__annotations__)


def _attached(ck: Any) -> tuple:
    dec = ck._dec
    return (ck._verb_view, ck._capture, ck._edits_reader, dec.rows, dec.consent, dec.questions, dec.notes, dec.spec,
            dict(ck._specs), dict(ck._rowsets))


@pytest.mark.parametrize("which", ["attach_verbs", "attach_consent_rows", "attach_note_rows"])
def test_an_attach_that_raises_after_the_pin_unpins_and_detaches(tmp_path, monkeypatch, which) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    before = _attached(rig.ck)
    real = getattr(rig.ck, which)

    def boom(*a: Any, **k: Any) -> Any:
        if which != "attach_verbs":
            real(*a, **k)                      # half-applied: the attach changed the cockpit, then raised
        raise RuntimeError("a panel was registered between check_verbs and the attach")

    monkeypatch.setattr(rig.ck, which, boom)
    with pytest.raises(RuntimeError, match="registered between"):
        make_server(rig.src, host="127.0.0.1", **rig.kw)
    assert _attached(rig.ck) == before
    rig.signer.page_origin = "http://localhost:1"        # unpinned: another page may be set again
    rig.signer.page_origin = rig.origin
    with socket.socket() as s:
        s.bind(("127.0.0.1", rig.port))
    monkeypatch.setattr(rig.ck, which, real)
    with rig.serving() as (_url, httpd):
        assert httpd.signer_page_origin == rig.origin
        with pytest.raises(ValueError, match="pinned"):
            rig.signer.page_origin = "http://localhost:1"


def test_a_failed_server_never_unpins_a_signer_another_server_holds(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _httpd):
        monkeypatch.setattr(rig.ck, "attach_consent_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
        kw = {**rig.kw, "port": 0}
        with pytest.raises(RuntimeError, match="no"):
            make_server(rig.src, host="127.0.0.1", **kw)
        with pytest.raises(ValueError, match="pinned"):
            rig.signer.page_origin = "http://localhost:1"
