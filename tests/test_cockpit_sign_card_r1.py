"""The sign card's L3 r1 fixes (project_memory/1023-26/d5/FIX1_BRIEF.md R1-R9), over the rig of test_cockpit_sign_card.

R1: a signed draft has ONE identity for its whole life (``client_txn``): the compose route names the same request for it
while that request is open, and the verb post is keyed by it, never by the request it names, so a lost answer, a retry,
a reload or a second compose of one draft files one request and applies the effect once. R2/R7: ``sign`` is a bool and
never beside ``job``. R3: the kernel refuses a signed verb that names no sign request, on every route, before its handler.
R4: a passkey pick list that a redraw detached settles, and the busy lease is released. R5: a request past the listing's
cap is read by its id. R6: a write-in's words survive a "not sure". R8: a refused make_server leaves the Signer unpinned.
R9: a definite "nothing was signed" reads nothing again."""
from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from levain.cockpit.verbs import SIGN_PARAM, VerbRegistry, VerbSpec
from levain.web_server import make_server
from levain.writes import ActionVerb
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import (BOX, BOX_MSG, DESKTOP, FACET_NOTE, OPEN, OPEN_Q, OPTION_B, QRID, STRIP_FACET, TOK,
                                          TXN, WORDS_BTN, WRITE_IN, _Rig, _send, needs_ssh)
from tests.test_cockpit_sign_routes import _call


def _compose(url: str, message: str, txn: str) -> "tuple[int, Any]":
    return _call(url, "/cockpit/sign/compose", {"verb": "send_inbox", "params": {"message": message}, "client_txn": txn},
                 token=TOK)


# --- R1, the kernel half: one draft, one request ---------------------------------------------------------------------


def test_one_draft_files_one_request_while_that_request_is_open(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, _httpd):
        assert _compose(url, "hi", TXN) == (200, {"request": "np-compose-0001"})
        # the retry of a lost reply, or a second tab of the same draft: the same request, the compose signer not asked
        assert _compose(url, "hi", TXN) == (200, {"request": "np-compose-0001"})
        status, out = _compose(url, "other words", TXN)
        assert status == 409 and out["error"] == "txn_conflict", out
        assert _compose(url, "hi", "draft-0002-bbbb") == (200, {"request": "np-compose-0002"})   # another draft
        assert len(rig.composed) == 2
        # once its request is no longer open (here it expired), the same draft files a new one
        path = rig.qdir / "sign" / "np-compose-0001.json"
        body = json.loads(path.read_text())
        body["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(body))
        assert _compose(url, "hi", TXN) == (200, {"request": "np-compose-0003"})
    assert [p["message"] for _, p in rig.composed] == ["hi", "hi", "hi"]


def test_two_composes_of_one_draft_at_once_file_one_request(tmp_path, monkeypatch) -> None:
    """A double click, or two tabs of one draft: the composes are serialised, so the second names the first's request."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (url, httpd):
        filed = httpd.compose_signer

        def slow(verb: str, params: dict) -> str:
            time.sleep(0.4)          # the window two concurrent composes would both file in
            return filed(verb, params)

        httpd.compose_signer = slow
        outs: list[Any] = []
        threads = [threading.Thread(target=lambda: outs.append(_compose(url, "hi", TXN))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
    assert outs == [(200, {"request": "np-compose-0001"})] * 2, outs
    assert len(rig.composed) == 1, rig.composed


# --- R2, R7: sign is a bool, and never a job --------------------------------------------------------------------------


def test_a_signed_verb_is_never_a_job_and_sign_is_a_bool() -> None:
    with pytest.raises(ValueError, match="both sign and job"):
        ActionVerb(handler=lambda p: {}, sign=True, job=True)
    with pytest.raises(ValueError, match="sign must be True or False"):
        ActionVerb(handler=lambda p: {}, sign="yes")    # type: ignore[arg-type]

    def spec(**kw: Any) -> VerbSpec:
        return VerbSpec("dv", "dv", "dv", "panel", fields=("message",), fire=lambda *a: {}, **kw)

    with pytest.raises(ValueError, match="both sign and job"):
        VerbRegistry({"dv": spec(sign=True, job=True)})
    with pytest.raises(ValueError, match="sign must be True or False"):
        VerbRegistry({"dv": spec(sign=1)})
    # either alone registers
    assert VerbRegistry({"dv": spec(sign=True)}).get("dv").sign is True
    assert VerbRegistry({"dv": spec(job=True)}).get("dv").job is True


# --- R3: no sign request, no handler -----------------------------------------------------------------------------------


def test_a_signed_verb_naming_no_sign_request_never_reaches_its_handler_or_effect(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    env = {"verb": "send_inbox", "panel_id": "inbox", "confirm": True, "intent": "reported", "tier_seen": "T2",
           "gesture_seen": "direct-reported"}
    with rig.serving() as (url, _httpd):
        for params in ({"message": "hi"}, {"message": "hi", SIGN_PARAM: "x"}, {"message": "hi", SIGN_PARAM: 7}):
            for route, body in (("/cockpit/verb", {**env, "params": params}),
                                ("/cockpit/verb", {"verb": "send_inbox", "panel_id": "inbox", "dry_run": True,
                                                   "params": params}),
                                ("/action", {"verb": "send_inbox", "confirm": True, "params": params})):
                status, out = _call(url, route, body, token=TOK)
                assert (status, out.get("error")) == (403, "sign_required"), (route, params, out)
    assert rig.handled == [] and rig.applied == [] and rig.refused == []


# --- R5: one request by its id ---------------------------------------------------------------------------------------


def test_one_request_is_read_by_its_id_past_the_listings_cap(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch, fillers=55)
    fp = rig.pk.key.fingerprint
    with rig.serving() as (url, _httpd):
        status, out = _call(url, "/cockpit/sign.json", token=TOK)
        assert status == 200 and len(out["requests"]) == 50 and QRID not in {r["id"] for r in out["requests"]}
        status, out = _call(url, f"/cockpit/sign/{QRID}.json", token=TOK)
        assert status == 200 and out["request"]["id"] == QRID, out
        assert [o["key"] for o in out["request"]["options"]] == ["a", "b"] and out["request"]["write_in"]["key"] == "*"
        assert out["signers"] == [fp] and out["origin"] == rig.origin
        status, out = _call(url, "/cockpit/sign/np-no-such-0001.json", token=TOK)
        assert (status, out["error"]) == (404, "not_open"), out
        assert _call(url, "/cockpit/sign/no%2Fgood.json", token=TOK)[0] == 404
        # the listing's gate: a token on every bind, same-origin only
        for token in (None, "wrong-token"):
            assert _call(url, f"/cockpit/sign/{QRID}.json", token=token)[0] == 403, token
        assert _call(url, f"/cockpit/sign/{QRID}.json", token=TOK, sfs="cross-site")[0] == 403


def test_without_a_signer_one_request_is_404(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch, compose=False)
    rig.kw["signer"] = None
    with rig.serving() as (url, _httpd):
        status, out = _call(url, f"/cockpit/sign/{QRID}.json", token=TOK)
    assert (status, out["error"]) == (404, "not_found"), out


def test_a_challenge_past_the_listings_cap_survives_a_listing_read(tmp_path, monkeypatch) -> None:
    """The Signer drops a challenge only for a request its listing READ and did not list: one past the cap was never
    looked at, so its ceremony is not cut off by a card that read the listing meanwhile."""
    rig = _Rig(tmp_path, monkeypatch, fillers=55)
    fp = rig.pk.key.fingerprint
    sha = rig.signer.row(QRID, [fp])["sha256"]
    rig.signer.challenge(QRID, {"signer_fp": fp, "request_sha256": sha, "choice": "a"})
    rig.signer.challenge("np-aaa-0000", {"signer_fp": fp, "request_sha256": rig.signer.row("np-aaa-0000", [fp])["sha256"]})
    (rig.qdir / "sign" / "np-aaa-0000.json").unlink()          # read by the listing, no longer there
    rows = rig.signer.listing([fp])
    assert QRID not in {r["id"] for r in rows}
    assert QRID in rig.signer._challenged and "np-aaa-0000" not in rig.signer._challenged


# --- R8: a refused server pins nothing -------------------------------------------------------------------------------


def test_a_refused_server_leaves_the_signer_unpinned(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="refusing extra_hosts"):
        make_server(rig.src, host="127.0.0.1", **{**rig.kw, "port": 0, "extra_hosts": ["localhost"]})
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        with pytest.raises(OSError):
            make_server(rig.src, host="127.0.0.1", **{**rig.kw, "port": busy.getsockname()[1]})
    rig.signer.page_origin = "http://localhost:1"     # unpinned: neither refused server held it
    rig.signer.page_origin = rig.origin
    with rig.serving() as (_url, httpd):
        assert httpd.signer_page_origin == rig.origin
        with pytest.raises(ValueError, match="pinned"):
            rig.signer.page_origin = "http://localhost:1"


# --- the page half, in headless Chrome ---------------------------------------------------------------------------------

# loses the answer to the first POST to `route` that is not a dry run: the server did the work, the page hears nothing
LOSE_ANSWER = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
               "const p = real.apply(this, arguments); const dry = o && typeof o.body === 'string' && "
               "o.body.includes('\"dry_run\":true'); if (armed && String(u) === {route} && !dry) {{ armed = false; "
               "return p.then(() => {{ throw new TypeError('the answer was lost'); }}); }} return p; }}; return 1; }})()")
# fails the first POST whose path ends with `tail` before it leaves the page: the server never sees it
NEVER_SENT = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
              "if (armed && String(u).endsWith({tail})) {{ armed = false; return Promise.reject(new TypeError('lost')); }} "
              "return real.apply(this, arguments); }}; return 1; }})()")
CLEARED = f"{BOX} && {BOX}.querySelector('.tray-dump-ta').value === '' && 1"
# what a board read does: the strip's rows are drawn again from a fresh view. The card open in the focus view is not the
# strip's, so it is neither removed nor drawn again by it: `__card` marks it and CARD_KEPT checks it is still the one on the page
REDRAW = ("(() => { window.__card = document.querySelector('#levain-modal .needs-question'); const h = document.getElementById('ck-needs'); "
          "h.replaceChildren(); delete h.dataset.sig; window.LevainCockpitTransport.reload(); return 1; })()")
STRIP_BACK = "document.querySelectorAll('#ck-needs .needs-row').length > 0"
CARD_KEPT = f"(window.__card && window.__card.isConnected && window.__card === document.querySelector('#levain-modal .needs-question') && {STRIP_BACK})"
CLOSE_FOCUS = "(document.querySelector('#levain-modal .modal-close').click(), 1)"
PICKS = "document.querySelectorAll('#levain-modal .needs-question .sign-pick').length"
COUNT_RELOADS = ("(() => { const t = window.LevainCockpitTransport; const o = t.reload; window.__reloads = 0; "
                 "t.reload = function () { window.__reloads++; return o.apply(this, arguments); }; return 1; })()")
RELOADS_AFTER = "new Promise((r) => setTimeout(() => r(window.__reloads), 2500))"


def _pick(fp: str) -> dict:
    return {"eval": f"[...document.querySelectorAll('#levain-modal .needs-question .sign-pick')].find(b => b.title === "
                    f"{json.dumps(fp)}).click() || 1"}


@needs_browser
@needs_ssh
def test_a_lost_answer_then_a_reload_posts_the_effect_once(tmp_path, monkeypatch) -> None:
    """codex's scenario, and a lost compose reply: (1) the compose route filed the request but its answer was lost, and
    the retry files no second one; (2) the verb was applied but its answer was lost, the page was reloaded and the same
    words sent again: the effect lands once, the retry carrying the first send's request and key."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": BOX + " && 1", "ms": 30000},
            {"eval": LOSE_ANSWER.format(route=json.dumps("/cockpit/sign/compose"))},
            _send("one request"),
            {"until": f"/could not be reached/.test({BOX_MSG}) && {BOX_MSG}"},
            _send("one request"),
            {"until": CLEARED, "ms": 30000},
            {"eval": LOSE_ANSWER.format(route=json.dumps("/cockpit/verb"))},
            _send("hello once"),
            {"until": f"/did not answer/.test({BOX_MSG}) && {BOX_MSG}", "ms": 30000},
            {"eval": "setTimeout(() => location.reload(), 50) && 1"},
            {"eval": "new Promise((r) => setTimeout(() => r(1), 1500))"},
            {"until": f"{BOX} && document.readyState === 'complete' && {BOX}.querySelector('.tray-dump-ta').value === '' && 1",
             "ms": 30000},
            _send("hello once"),
            {"until": CLEARED + " && !/…/.test(" + BOX_MSG + ")", "ms": 30000},
            {"eval": "new Promise((r) => setTimeout(() => r(1), 1000))"},
        ])
    composed = [p["message"] for _, p in rig.composed]
    assert composed.count("one request") == 1 and composed.count("hello once") == 1, composed
    assert rig.notes() == ["one request", "hello once"], rig.notes()
    hello = [json.loads(p.split(" ", 1)[1]) for p in out["posts"]
             if p.startswith("/cockpit/verb ") and "hello once" in p and '"dry_run":true' not in p]
    assert len(hello) == 2, out["posts"]
    assert len({h["idempotency_key"] for h in hello}) == 1 and len({h["params"][SIGN_PARAM] for h in hello}) == 1, hello


@needs_browser
@needs_ssh
def test_a_pick_list_a_redraw_detached_releases_the_card_and_a_write_in_survives_not_sure(tmp_path, monkeypatch) -> None:
    """R4: two passkeys enrolled and no record of which this browser holds: the pick list is open when the card is drawn
    again, and the new card's buttons work. R9: a dismissed pick (nothing signed) reads nothing again. R6: the write-in's
    answer never reached the server ("not sure"): its words are kept, and the box opens again once the request reads open."""
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    fp = rig.pk.key.fingerprint
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            # R4: the pick list is open when the card is drawn again; the new card's tap opens a new pick list (the old
            # one released the lease), and the old one's words are what the card now says
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": PICKS},
            {"eval": REDRAW},
            {"until": f"{CARD_KEPT} && {PICKS} === 3 && 1", "ms": 20000},   # the board read left the open card, and its pick list, alone
            {"eval": "[...document.querySelectorAll('#levain-modal .needs-question .sign-pick')]"
                     ".find(b => b.textContent === 'dismiss').click() || 1"},
            # R9
            {"eval": COUNT_RELOADS},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": PICKS, "may_timeout": True},
            {"eval": "[...document.querySelectorAll('#levain-modal .needs-question .sign-pick')]"
                     ".find(b => b.textContent === 'dismiss').click() || 1"},
            {"eval": RELOADS_AFTER},
            # R6
            {"eval": NEVER_SENT.format(tail=json.dumps("/answer"))},
            {"eval": f"(() => {{ const i = {WRITE_IN}; i.value = 'only after the backup'; "
                     "i.dispatchEvent(new Event('input')); return 1; })()"},
            {"eval": f"{WORDS_BTN}.click() || 1"},
            {"until": PICKS},
            _pick(fp),
            {"until": f"/not sure this was signed/.test({FACET_NOTE}) && {FACET_NOTE}"},
            {"until": f"!{WRITE_IN}.disabled && {WRITE_IN}.value", "may_timeout": True},
            {"eval": OPEN % QRID},
            {"eval": REDRAW},
            {"until": f"{CARD_KEPT} && {WRITE_IN}.value && 1", "ms": 20000},   # a board read leaves the typed words in the open card
            {"eval": f"{WRITE_IN}.value"},
            # closed and opened again: the words are offered again (the request still reads open)
            {"eval": CLOSE_FOCUS},
            {"until": f"({OPEN_Q}, {WRITE_IN}) && {WRITE_IN}.value && 1", "ms": 20000},
            {"eval": f"{WRITE_IN}.value"},
        ])
    v = out["values"]
    assert v[2] == 3, v[2]                               # two passkeys and a dismiss
    assert v[8] == 3, v[8]                               # the new card's tap: the redraw released the lease
    assert v[10] == 0, v[10]                             # a dismissed pick read nothing again
    assert "not sure this was signed" in v[16], v[16]
    assert v[17] == "only after the backup", v[17]       # kept, and editable again: the request still reads open
    assert v[18] is True and rig.nothing_signed(QRID)
    assert v[21] == "only after the backup", v[21]       # a board read left the words where they were typed
    assert v[24] == "only after the backup", v[24]       # and a focus view opened again offers the same words


@needs_browser
@needs_ssh
def test_past_the_listings_cap_the_card_signs_and_a_message_sends(tmp_path, monkeypatch) -> None:
    """R5: 55 open requests sort before the question's and the composed message's, so the listing (50) names neither:
    the card draws the question's options, and the message's request, read by its id, is signed and sent."""
    rig = _Rig(tmp_path, monkeypatch, fillers=55)
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000, "may_timeout": True},
            {"eval": "[...document.querySelectorAll('#levain-modal .needs-question .sign-option')].map(b => b.textContent)"},
            {"until": BOX + " && 1"},
            _send("past the cap"),
            {"until": CLEARED, "ms": 30000, "may_timeout": True},
            {"eval": BOX_MSG},
        ])
    v = out["values"]
    assert v[1][:2] == ["(a) Ship it tonight", "(b) Hold for the morning"], v
    assert rig.notes() == ["past the cap"], (rig.notes(), v[-1])
    assert rig.verifies("np-compose-0001")


def test_a_served_cockpit_stores_its_origin_where_a_band_reader_finds_it(tmp_path, monkeypatch) -> None:
    from levain.consent.cockpit_origin import read_origin

    rig = _Rig(tmp_path, monkeypatch)
    assert read_origin(rig.signer.queue_dir) is None              # nothing served yet, nothing stored
    with rig.serving():
        assert read_origin(rig.signer.queue_dir) is None          # an http origin (this machine's browser) is never stored
    (tmp_path / "two").mkdir()
    rig2 = _Rig(tmp_path / "two", monkeypatch)
    rig2.signer.page_origin = "https://localhost:8443"
    with rig2.serving():
        assert read_origin(rig2.signer.queue_dir) == "https://localhost:8443"


def _https_rig(tmp_path, monkeypatch, name, origin="https://localhost:8443"):
    (tmp_path / name).mkdir()
    rig = _Rig(tmp_path / name, monkeypatch)
    rig.signer.page_origin = origin
    return rig


def test_the_stored_origin_follows_the_server_that_holds_it(tmp_path, monkeypatch) -> None:
    from levain.consent.cockpit_origin import read_origin, write_origin

    rig = _https_rig(tmp_path, monkeypatch, "a")
    write_origin(rig.signer.queue_dir, "https://old.ts.net")
    with rig.serving():
        assert read_origin(rig.signer.queue_dir) == "https://localhost:8443"
    assert read_origin(rig.signer.queue_dir) is None               # closed: its own origin is withdrawn
    # a later start whose origin is ineligible (http) removes the earlier https origin
    write_origin(rig.signer.queue_dir, "https://old.ts.net")
    rig3 = _Rig(tmp_path, monkeypatch)
    write_origin(rig3.signer.queue_dir, "https://old.ts.net")
    with rig3.serving():
        assert read_origin(rig3.signer.queue_dir) is None


def test_close_leaves_another_instances_origin(tmp_path, monkeypatch) -> None:
    from levain.consent.cockpit_origin import read_origin, write_origin

    rig = _https_rig(tmp_path, monkeypatch, "b")
    with rig.serving():
        write_origin(rig.signer.queue_dir, "https://other.ts.net")  # another instance replaced it
    assert read_origin(rig.signer.queue_dir) == "https://other.ts.net"


def test_a_startup_failure_after_the_pin_publishes_no_origin(tmp_path, monkeypatch) -> None:
    from levain.consent.cockpit_origin import read_origin
    import levain.web_server as ws

    rig = _https_rig(tmp_path, monkeypatch, "c")

    def boom(*a, **k):
        raise RuntimeError("attach refused")
    monkeypatch.setattr(ws, "DispatchSheet", boom)
    with pytest.raises(RuntimeError):
        make_server(rig.src, host="127.0.0.1", **{**rig.kw, "port": 0})
    assert read_origin(rig.signer.queue_dir) is None
