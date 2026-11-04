"""The cockpit's presence word: GET /webpush/presence and POST /webpush/presence/set ride the push family's gate and call
presence.set_word as the operator's own act. Tap tokens (a purpose-bound credential for a Shortcut or NFC tag) are
tested further down."""
from __future__ import annotations

import fcntl
import json
import time

import pytest

from levain import web_server
from levain.webpush import intent, presence
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call
from tests.test_cockpit_webpush import _signed

GET = "/webpush/presence"
SET = "/webpush/presence/set"


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)
    # the status line asks the machine's idle clock; a test never reads the real one
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 5.0)


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def test_read_with_no_word_and_the_token_requirement(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY):
            assert _call(port, "GET", GET, headers=hdrs)[0] == 403
        assert _call(port, "GET", GET, token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        st, got, _ = _call(port, "GET", GET, token=T)
        assert st == 200 and got["word"] is None and got["until"] is None and got["text"] == presence.describe(q, idle=lambda: 5.0)
        assert "tokens" not in got or isinstance(got["tokens"], list)


@pytest.mark.parametrize("word", ["away", "sleep"])
def test_an_untimed_word_is_set_and_read_back(rig, word):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, {"word": word}, token=T)
        assert st == 200 and got["word"] == word and got["until"] is None
        assert _call(port, "GET", GET, token=T)[1]["word"] == word
        rows = [json.loads(l) for l in (q / "webpush" / presence.LOG_NAME).read_text().splitlines()]
        assert rows[-1]["word"] == word and rows[-1]["via"] == "browser" and "by" not in rows[-1]
        assert T not in (q / "webpush" / presence.LOG_NAME).read_text()


@pytest.mark.parametrize("word", ["quiet", "loud"])
def test_a_timed_word_takes_a_duration_and_names_its_end(rig, word):
    src, q = rig
    with _signed(src, q) as (port, _):
        t0 = time.time()
        st, got, _ = _call(port, "POST", SET, {"word": word, "for": "2h"}, token=T)
        assert st == 200 and got["word"] == word and abs(got["until"] - (t0 + 7200)) < 30
        st, got, _ = _call(port, "POST", SET, {"word": word}, token=T)       # no duration: the default hour
        assert st == 200 and abs(got["until"] - (time.time() + presence.DEFAULT_SECS)) < 30


def test_back_clears_and_loud_off_clears_a_loud(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        _call(port, "POST", SET, {"word": "away"}, token=T)
        got = _call(port, "POST", SET, {"word": "back"}, token=T)[1]
        assert got["word"] is None
        _call(port, "POST", SET, {"word": "loud", "for": "1h"}, token=T)
        assert _call(port, "POST", SET, {"word": "back"}, token=T)[1]["word"] == "loud"   # back leaves a loud
        got = _call(port, "POST", SET, {"word": "loud", "for": "off"}, token=T)[1]
        assert got["word"] is None


@pytest.mark.parametrize("body,status", [({"word": "napping"}, 422), ({"word": "away", "for": "1h"}, 422),
                                         ({"word": "quiet", "for": "soon"}, 422), ({}, 400), ({"word": 3}, 400),
                                         ({"word": "away", "for": 3}, 400), ({"word": "away", "x": 1}, 400),
                                         ([1], 400), ("x", 400)])
def test_a_bad_word_or_shape_is_refused_and_changes_nothing(rig, body, status):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, body, token=T)
        assert st == status, got
        assert got["error"] in ("presence_refused", "bad_request")
        assert presence.read_word(q) is None


def test_bad_json_is_400(rig):
    import http.client
    src, q = rig
    with _signed(src, q) as (port, _):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", SET, body=b"{nope", headers={"Content-Type": "application/json", "X-Levain-Write-Token": T})
        r = c.getresponse()
        assert r.status == 400 and json.loads(r.read())["error"] == "bad_json"


def test_an_unauthorised_write_changes_nothing(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY, {"Sec-Fetch-Site": "cross-site"}):
            assert _call(port, "POST", SET, {"word": "away"}, headers=hdrs)[0] in (403, 400)
        assert _call(port, "POST", SET, {"word": "away"}, token="wrong")[0] == 403
        assert presence.read_word(q) is None


def test_a_held_presence_lock_answers_503_and_writes_nothing(rig, monkeypatch):
    src, q = rig
    monkeypatch.setattr(web_server, "_POLICY_LOCK_WAIT_SECS", 0.4)
    d = q / "webpush"
    d.mkdir(mode=0o700)
    holder = open(d / presence.LOCK_NAME, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with _signed(src, q) as (port, _):
            t0 = time.monotonic()
            st, got, _ = _call(port, "POST", SET, {"word": "away"}, token=T)
            assert st == 503 and got["error"] == "presence_busy" and "nothing was changed" in got["message"]
            assert time.monotonic() - t0 < 5
    finally:
        holder.close()
    assert presence.read_word(q) is None


def test_a_quiet_word_set_here_does_not_stop_an_urgent_consent_ping(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        assert _call(port, "POST", SET, {"word": "quiet", "for": "1h"}, token=T)[0] == 200
    word = presence.read_word(q)
    assert word["word"] == "quiet"
    now = time.time()
    assert presence.decide(now, word, 99999.0, urgent=True).push is True
    assert presence.decide(now, word, 99999.0, urgent=False).push is False


# ---- tap tokens: the purpose-bound credential ------------------------------------------------------------------------

import logging

from levain.webpush import presence_tokens

TAP = "X-Levain-Presence-Token"
NEW = "/webpush/presence/token/new"
REVOKE = "/webpush/presence/token/revoke"


def _mint(port, name="door", words=None):
    body = {"name": name} if words is None else {"name": name, "words": words}
    st, got, _ = _call(port, "POST", NEW, body, token=T)
    assert st == 200, got
    return got["token"], got


def _tap(tok):
    return {TAP: tok}


def test_a_tap_token_is_minted_once_and_stored_only_as_a_hash(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, got = _mint(port)
        assert got["record"]["name"] == "door" and got["record"]["words"] == ["away", "back", "quiet", "desk", "devices"]
        assert "sha256" not in json.dumps(got)
        f = q / "webpush" / presence_tokens.FILE_NAME
        assert (f.stat().st_mode & 0o777) == 0o600
        assert tok not in f.read_text() and presence_tokens._hash(tok) in f.read_text()
        # the secret is in no later body: the list, the view, a set, a refusal
        bodies = [_call(port, "GET", GET, token=T)[1], _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))[1],
                  _call(port, "POST", SET, {"word": "sleep"}, headers=_tap(tok))[1],
                  _call(port, "POST", REVOKE, {"name": "door"}, token=T)[1],
                  _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))[1]]
        assert all(tok not in json.dumps(b) for b in bodies)
        assert _call(port, "GET", GET, token=T)[1]["tokens"] == []


def test_a_tap_token_reads_and_sets_the_word_and_the_row_names_it(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        st, got, hdrs = _call(port, "POST", SET, {"word": "quiet", "for": "15m"}, headers=_tap(tok))
        assert st == 200 and got["word"] == "quiet" and "tokens" not in got
        assert tok not in hdrs.get("vary", "") and tok not in json.dumps(hdrs)
        st, got, _ = _call(port, "GET", GET, headers=_tap(tok))
        assert st == 200 and got["word"] == "quiet" and "tokens" not in got
        log = (q / "webpush" / presence.LOG_NAME).read_text()
        assert json.loads(log.splitlines()[-1])["via"] == "presence:door" and tok not in log
        assert presence_tokens.list_public(q)[0]["last_used"] is not None
        assert "tokens" in _call(port, "GET", GET, token=T)[1]


def test_the_allow_list_is_enforced_and_names_the_allowed_words(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        for w in ("sleep", "loud"):
            st, got, _ = _call(port, "POST", SET, {"word": w}, headers=_tap(tok))
            assert st == 403 and "away, back, quiet, desk, devices" in got["message"], got
        assert presence.read_word(q) is None
        wide, _ = _mint(port, "bed", ["sleep", "loud"])
        assert _call(port, "POST", SET, {"word": "sleep"}, headers=_tap(wide))[0] == 200
        assert _call(port, "POST", SET, {"word": "away"}, headers=_tap(wide))[0] == 403


def test_a_revoked_or_unknown_tap_token_is_refused(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        assert _call(port, "POST", REVOKE, {"name": "door"}, token=T)[0] == 200
        for t in (tok, "nope", "x" * 300):
            assert _call(port, "POST", SET, {"word": "away"}, headers=_tap(t))[0] == 403
            assert _call(port, "GET", GET, headers=_tap(t))[0] == 403
        assert _call(port, "POST", REVOKE, {"name": "door"}, token=T)[0] == 404
        assert presence.read_word(q) is None


def test_a_revoke_that_lands_after_the_first_check_refuses_the_write(rig, monkeypatch):
    # the revoke runs at the last seam before set_word takes the lock: after the request's credential was resolved
    src, q = rig
    real = presence._locked
    fired = []

    def revoking(queue_dir, **kw):
        if not fired:
            fired.append(presence_tokens.revoke(q, "door"))
        return real(queue_dir, **kw)

    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        monkeypatch.setattr(presence, "_locked", revoking)
        st, got, _ = _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))
        assert fired == [True]
        assert st == 403 and got["error"] == "forbidden", got
    assert presence.read_word(q) is None
    rows = [json.loads(l) for l in (q / "webpush" / presence.LOG_NAME).read_text().splitlines()]
    assert rows[-1]["outcome"] == "refused" and rows[-1]["via"] == "presence:door"
    assert presence_tokens.list_public(q) == []


def test_a_tap_token_reaches_nothing_else(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        h = _tap(tok)
        # a write route, a read route, chat, the policy routes, the token routes, the other push routes
        for method, path, body in (("POST", "/edit", {"x": 1}), ("POST", "/action", {"x": 1}),
                                   ("GET", "/cockpit/manifest.json", None), ("GET", "/cockpit/sign.json", None),
                                   ("POST", "/chat", {"x": 1}), ("GET", "/chat.json", None),
                                   ("GET", "/webpush/policy", None), ("POST", "/webpush/policy/set",
                                                                      {"mute": "sleep", "class": "approval", "value": "push"}),
                                   ("POST", "/webpush/policy/reset", {}), ("POST", NEW, {"name": "evil"}),
                                   ("POST", REVOKE, {"name": "door"}), ("GET", "/webpush/key", None),
                                   ("POST", "/webpush/subscribe", {"x": 1}), ("POST", "/webpush/test", {}),
                                   ("POST", "/cockpit/pair/offer", {"label": "x"}), ("GET", "/cockpit/pair/list", None)):
            for hdrs in (h, {**h, **PROXY}):
                st, got, _ = _call(port, method, path, body, headers=hdrs)
                miss, miss_got, _ = _call(port, method, path, body, headers={k: v for k, v in hdrs.items() if k != TAP})
                assert st == miss, (method, path, st, miss)         # exactly as a missing credential answers
                if method == "POST" or "X-Forwarded-For" in hdrs or hdrs is not h:
                    assert st in (403, 404), (method, path, st)     # (a plain loopback read is token-free, same as with none)
        assert not (q / "webpush" / "intent.json").exists()   # nothing a tap token reached wrote the record
        assert [r["name"] for r in presence_tokens.list_public(q)] == ["door"]


def test_a_tap_token_in_the_url_or_body_is_not_a_credential(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        assert _call(port, "GET", f"{GET}?token={tok}")[0] == 403
        assert _call(port, "POST", SET, {"word": "away", "token": tok})[0] == 403
        assert _call(port, "POST", f"{SET}?presence_token={tok}", {"word": "away"})[0] == 403
        assert presence.read_word(q) is None


def test_the_full_credential_wins_and_a_tap_token_cannot_mint(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        # both headers: the full credential's authority applies (sleep is not on the tap's list)
        assert _call(port, "POST", SET, {"word": "sleep"}, token=T, headers=_tap(tok))[0] == 200
        assert _call(port, "POST", NEW, {"name": "x"}, headers=_tap(tok))[0] == 403


@pytest.mark.parametrize("body", [{}, {"name": 1}, {"name": "ok", "x": 1}, {"name": "ok", "words": "away"},
                                  {"name": "ok", "words": ["flying"]}, {"name": ""}, {"name": "a/b"}, {"name": "x" * 41},
                                  {"name": "ok", "words": []}])
def test_a_bad_mint_is_refused(rig, body):
    src, q = rig
    with _signed(src, q) as (port, _):
        assert _call(port, "POST", NEW, body, token=T)[0] in (400, 422)
        assert presence_tokens.list_public(q) == []


def test_a_duplicate_live_name_is_refused(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        _mint(port)
        assert _call(port, "POST", NEW, {"name": "door"}, token=T)[0] == 422


def test_the_rate_limit_is_429_with_retry_after(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        other, _ = _mint(port, "other")
        for _i in range(10):
            assert _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))[0] == 200
        st, got, hdrs = _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))
        assert st == 429 and int(hdrs["retry-after"]) >= 1
        assert _call(port, "POST", SET, {"word": "away"}, headers=_tap(other))[0] == 200   # per token
        assert _call(port, "POST", SET, {"word": "away"}, token=T)[0] == 200               # not the full credential


def test_no_log_line_carries_a_tap_token(rig, caplog, capfd, monkeypatch):
    monkeypatch.setenv("LEVAIN_SERVE_VERBOSE", "1")
    src, q = rig
    caplog.set_level(logging.DEBUG)
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))
        _call(port, "GET", GET, headers=_tap(tok))
        _call(port, "POST", SET, {"word": "sleep"}, headers=_tap(tok))
        _call(port, "POST", "/edit", {}, headers=_tap(tok))
    out = capfd.readouterr()
    assert tok not in caplog.text and tok not in out.out and tok not in out.err


# ---- the Presence chip, its sheet and the settings section ------------------------------------------------------------

from levain.web_server import load_web_asset
from tests.test_pair_card_w1b import _drive, needs_browser

_CHIP = "document.getElementById('presencebtn')"
_SHOWN = f"{_CHIP} && !{_CHIP}.hidden"
_OPEN = "!document.getElementById('pr-sheet').hidden"
_CHIP_TEXT = f"{_CHIP}.querySelector('.pr-chip-t').textContent"


def _opened(extra: str = "") -> dict:
    return {"do": f"{_CHIP}.click()", "until": _OPEN + extra, "ms": 8000}


def _asset(name: str) -> str:
    js = load_web_asset(name)
    return js[0].decode() if isinstance(js, tuple) else js


def test_the_page_carries_the_chip_and_the_script_the_sheet_and_no_client_name():
    html = _asset("dashboard.html")
    assert 'id="presencebtn"' in html and html.index('id="presencebtn"') < html.index('id="refresh"')
    js = _asset("devices_panel.js")
    for needle in ("/webpush/presence", "/webpush/presence/set", "/webpush/presence/token/new", "/webpush/presence/token/revoke",
                   "Tap tokens", "X-Levain-Presence-Token", "Copy", "presence-settings", "pr-settings-grid",
                   "Lets through", "Until I say or type", "chip.word"):
        assert needle in js, needle
    for gone in ("Pings go to", "buildPolicy", "/webpush/policy", "route_source", "PR_ROUTES"):   # the route row and the policy block are gone
        assert gone not in js, gone


@needs_browser
def test_the_chip_reads_the_record_and_the_sheet_posts_each_body_and_repaints_from_the_reply(rig, tmp_path, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            {"eval": _CHIP_TEXT},
            {"eval": "[...document.querySelectorAll('#pr-sheet .pr-sheet-word')].map(b=>b.textContent)"},
            _opened(),
            {"eval": "document.getElementById('pr-sheet').contains(document.activeElement)"},
            {"eval": "document.querySelector('.pr-sheet-text').textContent"},
            {"do": "document.querySelector('.pr-sheet-word[data-value=\\\"quiet\\\"]').click()",
             "until": f"{_CHIP_TEXT}.startsWith('Quiet 6')", "ms": 8000},
            {"eval": "[document.getElementById('pr-sheet').hidden, document.querySelector('.pr-sheet-text').textContent]"},
            {"do": "document.querySelector('.pr-sheet-word[data-value=\\\"away\\\"]').click()", "until": f"{_CHIP_TEXT}.startsWith('Away')", "ms": 8000},
            # the options rows belong to the standing preset: Away is changed to let approvals through, and keeps that
            {"do": "document.querySelector('#pr-sheet [aria-label=\\\"What reaches your devices\\\"] [data-value=\\\"approvals\\\"]').click()",
             "until": "document.querySelector('.pr-sheet-word[data-value=\\\"away\\\"]').classList.contains('pr-custom')", "ms": 8000},
            {"eval": "[document.querySelector('.pr-sheet-text').textContent, document.querySelector('.pr-custom-line').hidden]"},
            # Back's options are the standing default: Devices Off there, which clears the overlay
            {"do": "document.querySelector('.pr-sheet-word[data-value=\\\"back\\\"]').click()", "until": f"!{_CHIP_TEXT}.startsWith('Away')", "ms": 8000},
            {"do": "document.querySelector('#pr-sheet [aria-label=\\\"Whether pings reach your devices\\\"] [data-value=\\\"off\\\"]').click()",
             "until": f"{_CHIP_TEXT}.startsWith('Desk only')", "ms": 8000},
            {"do": "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))", "until": "document.getElementById('pr-sheet').hidden", "ms": 4000},
            {"eval": "[...document.querySelectorAll('#pr-sheet [aria-label=\"What reaches your devices\"] button')].map(b => [b.dataset.value, b.textContent, b.title])"},
            {"eval": f"document.activeElement===({_CHIP})"},
        ])
    v = out["values"]
    assert v[1].startswith("Follow")                                       # no overlay: the standing default
    assert v[2] == ["Away", "Back", "Quiet", "Sleep", "Loud"]
    assert v[4] is True and v[5].startswith("Pings now: ")                 # focus in the sheet, which opens on the server's own line
    assert v[7][0] is False and "quiet" in v[7][1]                         # the sheet stays open to show the new line
    from levain.webpush import intent
    assert f"Lets through: {intent.lets_label('approvals')}" in v[10][0] and "a message you type into a session" in v[10][0] and v[10][1] is False
    from levain.webpush import intent as _in
    assert v[-2] == [[o["value"], o["label"], o["describe"]] for o in _in.lets_options()]     # the buttons read the server's words
    assert v[-1] is True
    rows = [json.loads(l) for l in (q / "webpush" / presence.LOG_NAME).read_text().splitlines()]
    assert [r["word"] for r in rows][:3] == ["quiet", "away", "set away lets_through"]
    assert rows[3]["word"] == "back" and rows[4]["word"] == "set devices off"


@needs_browser
def test_a_lets_button_made_during_a_write_is_disabled_and_a_level_the_server_drops_leaves_the_page(rig, tmp_path, monkeypatch):
    src, q = rig
    group = "'#pr-sheet [aria-label=\"What reaches your devices\"] button'"
    hook = ("(() => { window.__made = {}; const ap = Node.prototype.appendChild;"
            " Node.prototype.appendChild = function (c) { if (c && c.tagName === 'BUTTON' && c.dataset && c.dataset.value === 'zz') window.__made.zz = c.disabled;"
            " return ap.call(this, c); };"
            # the reply to a presence write offers one more level and drops 'approvals'
            " const f = window.fetch.bind(window); window.fetch = async (u, o) => { const r = await f(u, o);"
            " if (!String(u).endsWith('/webpush/presence/set')) return r; const d = await r.json();"
            " d.lets = d.lets.filter((x) => x.value !== 'approvals').concat([{value: 'zz', label: 'Extra', describe: 'x'}]);"
            " return new Response(JSON.stringify(d), {status: r.status, headers: {'Content-Type': 'application/json'}}); }; return 1; })()")
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            _opened(),
            {"eval": hook},
            {"do": "document.querySelector('.pr-sheet-word[data-value=\"quiet\"]').click()", "until": f"{_CHIP_TEXT}.startsWith('Quiet')", "ms": 8000},
            {"eval": "window.__made.zz"},
            {"eval": f"[...document.querySelectorAll({group})].map(b => b.dataset.value)"},
        ])
    v = out["values"]
    assert v[-2] is True, v                                    # made mid-write, it was born disabled
    assert v[-1][-1] == "zz" and "approvals" not in v[-1], v     # the dropped level is gone, the new one is there


@needs_browser
def test_a_standing_overlay_shows_in_the_chip_with_its_time_left(rig, tmp_path, monkeypatch):
    src, q = rig
    presence.set_word(q, "quiet", duration="1h")
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            {"eval": _CHIP_TEXT},
            {"eval": f"{_CHIP}.getAttribute('aria-label')"},
            {"eval": f"{_CHIP}.tagName"},
        ])
    v = out["values"]
    assert v[1] in ("Quiet 60m → devices", "Quiet 59m → devices")     # the chip object from the server, verbatim
    assert v[2].startswith("Presence: Pings now: ") and v[3] == "BUTTON"


@needs_browser
def test_the_link_reveals_the_settings_and_a_tap_token_is_made_shown_once_and_revoked(rig, tmp_path, monkeypatch):
    src, q = rig
    settings = "document.getElementById('presence-settings')"
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN + f" && {settings} && !{settings}.hidden", "ms": 20000},
            {"eval": f"{settings}.querySelector('.zone-title').textContent"},
            {"eval": f"[...document.querySelectorAll('.pr-sheet-word')].length"},
            _opened(),
            {"do": "document.querySelector('.pr-sheet-link').click()", "until": "document.getElementById('pr-sheet').hidden", "ms": 4000},
            {"eval": f"[document.activeElement.id, Math.round({settings}.getBoundingClientRect().top) < window.innerHeight]"},
            {"do": "document.querySelector('.pr-name').value='Door'; document.querySelector('.pr-new-form button[type=submit]').click()",
             "until": "!document.querySelector('.pr-new').hidden", "ms": 8000},
            {"eval": "document.querySelector('.pr-new .dev-url').value.length"},
            {"eval": "location.reload() || 1"},
            {"until": _SHOWN + " && document.querySelector('.pr-tap')", "ms": 20000},
            {"eval": "[document.querySelector('.pr-new').hidden, document.querySelector('.pr-tap .dev-meta').textContent]"},
            {"do": "document.querySelector('.pr-revoke').click()", "until": "!document.querySelector('.pr-tap')", "ms": 8000},
        ])
    v = out["values"]
    assert v[1] == "Presence & notification settings"
    assert v[5][0] == "presence-settings" and v[5][1] is True
    assert v[7] >= 40                                                       # the token was shown
    assert v[10][0] is True and "away, back, quiet, desk, devices" in v[10][1]
    assert presence_tokens.list_public(q) == []
    assert intent.read(q)["overlay"] is None


# ---- the record over HTTP ------------------------------------------------------------------------------------------
def test_the_view_is_the_intent_view_with_the_legacy_keys_and_no_route(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        got = _call(port, "GET", GET, token=T)[1]
        for k in ("line", "chip", "base", "overlay", "effective", "you", "desk", "tokens"):
            assert k in got, k
        assert "route" not in got and "route_source" not in got
        assert got["text"] == got["line"] == intent.describe_line(q, idle=lambda: 5.0) and got["word"] is None and got["until"] is None
        t0 = time.time()
        got = _call(port, "POST", SET, {"preset": "quiet", "for": "2h"}, token=T)[1]
        assert got["word"] == "quiet" and abs(got["until"] - (t0 + 7200)) < 30 and got["overlay"]["preset"] == "quiet"


def test_the_direct_settings_write_the_base_or_an_overlay_and_a_bad_one_is_refused(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, {"devices": "follow", "follow_after": 900}, token=T)
        assert st == 200 and got["base"]["follow_after"] == 900 and got["overlay"] is None and "after 15 min" in got["line"]
        st, got, _ = _call(port, "POST", SET, {"lets_through": "approvals", "until": "30m"}, token=T)
        assert st == 200 and got["overlay"]["preset"] is None and got["overlay"]["lets_through"] == "approvals" and got["base"]["lets_through"] == "everything"
        for body, status in (({"devices": "maybe"}, 422), ({"follow_after": 5}, 422), ({"lets_through": "all"}, 422),
                             ({"devices": "on", "until": "soon"}, 422), ({"devices": 3}, 400), ({"until": "30m"}, 422),
                             ({"preset": "away", "devices": "on"}, 400), ({"follow_after": True}, 400)):
            assert _call(port, "POST", SET, body, token=T)[0] == status, body
        assert intent.read(q)["base"]["follow_after"] == 900            # nothing a refusal sent changed the record


def test_a_policy_write_is_refused_with_the_pointer(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        for path, body in (("/webpush/policy/set", {"mute": "sleep", "class": "approval", "value": "push"}), ("/webpush/policy/reset", {})):
            st, got, _ = _call(port, "POST", path, body, token=T)
            assert st == 422 and "retired" in got["message"] and "levain ping lets" in got["message"]
        got = _call(port, "GET", "/webpush/policy", token=T)[1]
        assert got["retired"] is True and not (q / "webpush" / "intent.json").exists()


def test_a_tap_token_sets_presets_only_and_the_aliases_only_when_listed(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        narrow, _ = _mint(port, "narrow", ["away"])
        st, got, _ = _call(port, "POST", SET, {"word": "devices"}, headers=_tap(narrow))
        assert st == 403 and "away" in got["message"] and not (q / "webpush" / "intent.json").exists()
        st, got, _ = _call(port, "POST", SET, {"preset": "quiet"}, headers=_tap(narrow))
        assert st == 403
        for body in ({"devices": "off"}, {"lets_through": "nothing"}, {"follow_after": 900}):    # the settings are the full credential's
            st, got, _ = _call(port, "POST", SET, body, headers=_tap(narrow))
            assert st == 403 and got["error"] == "word_not_allowed", body
        tok, _ = _mint(port)
        st, got, _ = _call(port, "POST", SET, {"word": "devices"}, headers=_tap(tok))
        assert st == 200 and got["effective"]["devices"] == "on"
        st, got, _ = _call(port, "POST", SET, {"preset": "away"}, headers=_tap(tok))
        assert st == 200 and got["word"] == "away"
        assert json.loads((q / "webpush" / presence.LOG_NAME).read_text().splitlines()[-1])["via"] == "presence:door"


def test_a_revoke_before_the_write_refuses_it(rig, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        monkeypatch.setattr(presence_tokens, "verify", lambda *a, **k: None)
        assert _call(port, "POST", SET, {"word": "devices"}, headers=_tap(tok))[0] == 403
        assert not (q / "webpush" / "intent.json").exists()


def test_every_write_flushes_held_pings_once_outside_the_lock_and_without_the_spawn_gap(rig, monkeypatch):
    from levain.webpush import held

    src, q = rig
    calls = []
    monkeypatch.setattr(held, "flush_if_due", lambda queue, *, throttle=True: calls.append((queue, throttle)) or False)
    with _signed(src, q) as (port, _):
        for body in ({"preset": "away"}, {"preset": "back"}, {"word": "devices"}, {"devices": "off"}, {"lets_through": "needs", "until": "1h"}):
            assert _call(port, "POST", SET, body, token=T)[0] == 200
        assert len(calls) == 5 and all(t is False for _, t in calls)
        _call(port, "POST", SET, {"preset": "napping"}, token=T)           # a refused write changes nothing and flushes nothing
        assert len(calls) == 5


# ---- the `remote` word is gone, and the view carries the one rule's answer ------------------------------------------------

def test_the_remote_word_is_refused_with_a_message_and_a_token_that_lists_it_fails_closed(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, {"word": "remote"}, token=T)
        assert st == 422 and "`remote` word was dropped" in got["message"] and presence.read_word(q) is None
        # a token minted before the drop still names `remote` in its list: it stays a token, the word is refused
        tok, _ = _mint(port)
        f = q / "webpush" / presence_tokens.FILE_NAME
        doc = json.loads(f.read_text())
        doc["tokens"][0]["words"] = ["away", "remote"]
        f.write_text(json.dumps(doc))
        st, got, _ = _call(port, "POST", SET, {"word": "remote"}, headers=_tap(tok))
        assert st == 422 and "dropped" in got["message"] and presence.read_word(q) is None
        assert _call(port, "POST", SET, {"word": "away"}, headers=_tap(tok))[0] == 200


def test_the_view_agrees_with_the_push_decision_as_the_last_desk_message_ages(rig, monkeypatch):
    from levain.webpush import desk

    src, q = rig
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 5.0)   # the laptop's own input, which is not the desk
    with _signed(src, q) as (port, _):
        desk._write(q, desk.DESK_MESSAGE_NAME, {"at": time.time() - 5, "source": "test", "desk": ""})   # he typed to a desk just now
        got = _call(port, "GET", GET, token=T)[1]
        assert (got["state"], got["pushing"], got["source"], got["since"]) == ("holding", False, "measured", None)
        assert got["pushing"] is presence.should_push(q, "ping", suggest=False).push and got["chip"]["state"] == "desk"
        desk._write(q, desk.DESK_MESSAGE_NAME, {"at": time.time() - 900, "source": "test", "desk": ""})   # 15 min on: the same record pushes
        got = _call(port, "GET", GET, token=T)[1]
        assert (got["state"], got["pushing"], got["source"]) == ("pushing", True, "measured") and "no desk message for 15 min" in got["reason"]
        assert got["you"]["state"] == "here" and got["desk"]["live"] is False and got["chip"]["state"] == "devices"   # laptop input (5 s) is not the desk
        _call(port, "POST", SET, {"preset": "quiet", "for": "1h"}, token=T)
        got = _call(port, "GET", GET, token=T)[1]
        assert got["source"] == "word" and abs(got["since"] - time.time()) < 30 and got["effective"]["lets_through"] == "needs"


def _chip_when(rig, tmp_path, monkeypatch, idle_secs):
    src, q = rig
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: idle_secs)
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000}, {"eval": _CHIP_TEXT}, {"eval": f"{_CHIP}.dataset.state"}])
    return out["values"][1], out["values"][2]


@needs_browser
def test_the_chip_paints_the_servers_chip_not_a_state_of_its_own(rig, tmp_path, monkeypatch):
    # 1a (Phill 11-03): laptop input of any age is not a desk message, so Follow stays on devices
    assert _chip_when(rig, tmp_path, monkeypatch, 900.0) == ("Follow → devices", "devices")
    assert _chip_when(rig, tmp_path, monkeypatch, 5.0) == ("Follow → devices", "devices")


def test_the_panel_copy_says_what_the_next_setting_does_and_what_the_standing_default_is():
    js = _asset("devices_panel.js")
    assert "A time applies to this activation only" in js and "Standing default" not in js     # the bottom panel is gone
    assert "paintChip" in js and "v.line" in js


def test_laptop_input_never_returns_follow_to_the_desk_only_a_typed_desk_message_does(rig, monkeypatch):
    """Phill 11-03 "1a": keyboard, mouse and a screen wake are not a desk message. Every reader (a cockpit on the laptop, one on the
    phone, the terminal line) gets this from the one server derivation."""
    from levain.webpush import desk

    src, q = rig
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 0.0)       # input at the laptop just now
    with _signed(src, q) as (port, _):
        for _reader in ("laptop page", "phone page"):
            got = _call(port, "GET", GET, token=T)[1]
            assert got["chip"]["state"] == "devices" and got["pushing"] is True, got["line"]
        assert got["line"] == intent.describe_line(q, idle=lambda: 0.0)                       # the terminal's sentence is the same
        desk.note_desk_message(q, "test")
        for _reader in ("laptop page", "phone page"):
            got = _call(port, "GET", GET, token=T)[1]
            assert got["chip"]["state"] == "desk" and got["pushing"] is False and "desk message just now" in got["reason"], got["line"]


@needs_browser
def test_an_open_page_follows_the_server_across_follow_after_without_a_reload(rig, tmp_path, monkeypatch):
    """A cockpit left open (a phone's) re-reads the server's one derivation on a short pulse. The desk message ages past
    ``follow_after`` only AFTER the page has rendered and after its own startup re-read has been answered (the record changes on
    the server a few seconds after the first presence request), so only the interval can show it with no reload; the wait is
    ms * LEVAIN_TEST_WAIT_SCALE, kept under the old 30 s pulse at the CI scale of 2."""
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "2")
    from levain.webpush import desk

    src, q = rig
    intent.apply_settings(q, devices="follow", follow_after=60)
    desk.note_desk_message(q, "test")                                    # just now: well inside follow_after
    from levain import web_server

    real, first, flipped = web_server._Handler._presence_view, [], []

    def reads(self, queue):      # the clock starts at the first presence request, not at any desk read inside it
        if not first:
            first.append(time.monotonic())
        elif not flipped and time.monotonic() - first[0] > 4.0:           # past the 2.5 s startup re-read, before the 6 s pulse
            desk._write(q, desk.DESK_MESSAGE_NAME, {"at": time.time() - 900, "source": "test", "desk": ""})
            flipped.append(True)
        return real(self, queue)

    monkeypatch.setattr(web_server._Handler, "_presence_view", reads)
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            {"eval": _CHIP_TEXT},
            {"eval": "performance.getEntriesByType('navigation').length"},
            {"until": f"{_CHIP_TEXT} === 'Follow → devices'", "ms": 10000},
            {"eval": "performance.getEntriesByType('navigation').length"},
        ])
    v = out["values"]
    assert v[1] == "Follow → desk" and v[2] == v[-1] == 1, v
    assert flipped, "the record never changed under the page: the test proved nothing"


@needs_browser
def test_a_slow_presence_read_still_paints_and_is_never_asked_twice_at_once(rig, tmp_path, monkeypatch):
    """A server slower than the 6 s pulse: each tick used to supersede the read before it, so every answer was dropped and the
    chip stayed hidden, with a new request piling on each tick. The page keeps ONE read in flight and paints its answer."""
    import threading

    src, q = rig
    from levain import web_server

    real, lock, live, peak = web_server._Handler._presence_view, threading.Lock(), [0], [0]

    def slow(self, queue):       # one 7 s delay per REQUEST (a request reads the desk message several times)
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        try:
            time.sleep(7)
            return real(self, queue)
        finally:
            with lock:
                live[0] -= 1

    monkeypatch.setattr(web_server._Handler, "_presence_view", slow)
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 30000}, {"eval": _CHIP_TEXT}])
    assert out["values"][1].startswith("Follow"), out["values"]
    assert peak[0] == 1, f"{peak[0]} presence reads were in flight at once"


def test_the_page_runs_one_timer_for_the_follow_line_and_the_device_list():
    """Shape only: the 6 s and the 30 s timers both read the line; one timer now carries both."""
    start = _asset("devices_panel.js").split("function start()")[1]
    assert start.count("setInterval(") == 1 and "clearInterval(pulse)" in start


# ---- presets (Phill np-8a2d86ca, 11-04): the Until row paints the record, never a state of its own -----------------------

_UNTIL_SEL = ("#pr-sheet [aria-label=\"How long this preset lasts\"] [aria-pressed=true], "
              "#pr-sheet [aria-label=\"How long\"] [aria-pressed=true]")


@needs_browser
def test_the_until_row_shows_the_ending_the_record_holds_after_a_preset_is_tapped(rig, tmp_path, monkeypatch):
    src, q = rig
    pressed = "[...document.querySelectorAll('" + _UNTIL_SEL + "')].map(b => b.dataset.value)"
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            _opened(),
            {"do": "document.querySelector('.pr-sheet-word[data-value=\"away\"]').click()", "until": f"{_CHIP_TEXT}.startsWith('Away')", "ms": 8000},
            {"eval": pressed},
            {"do": "document.querySelector('.pr-sheet-word[data-value=\"quiet\"]').click()", "until": f"{_CHIP_TEXT}.startsWith('Quiet')", "ms": 8000},
            {"eval": pressed},
        ])
    rec = json.loads((q / "webpush" / intent.FILE_NAME).read_text())
    assert rec["overlay"]["preset"] == "quiet" and rec["overlay"]["until"]["kind"] == "timer"
    v = out["values"]
    assert v[-3] == ["input"], v     # Away ends on his next word or a typed message: the row says so, not "I switch it"
    assert v[-1] == ["timer", "1h"], v   # Quiet ends on a timer: the row says so and shows its duration


@needs_browser
def test_i_switch_it_makes_the_preset_the_standing_default_and_the_row_paints_the_record(rig, tmp_path, monkeypatch):
    """Until offers I switch it again (no schema change: the direct write with until=switch). On Back it is pressed because no
    overlay is on file; after Away + I switch it the overlay is gone and Away's options are the base."""
    src, q = rig
    pressed = "[...document.querySelectorAll('" + _UNTIL_SEL + "')].map(b => b.dataset.value)"
    sw = "document.querySelector('#pr-sheet [aria-label=\"How long this preset lasts\"] [data-value=\"switch\"]')"
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            _opened(),
            {"eval": pressed},
            {"do": "document.querySelector('.pr-sheet-word[data-value=\"away\"]').click()", "until": f"{_CHIP_TEXT}.startsWith('Away')", "ms": 8000},
            {"eval": pressed},
            {"do": f"{sw}.click()", "until": f"{pressed}[0] === 'switch'", "ms": 8000},
            {"eval": pressed},
        ])
    rec = json.loads((q / "webpush" / intent.FILE_NAME).read_text())
    assert rec["overlay"] is None and rec["base"]["devices"] == "on", rec
    v = [x for x in out["values"] if isinstance(x, list)]      # the eval results (the do/until steps answer True)
    assert v == [["switch"], ["input"], ["switch"]], out["values"]


def _overlay(q) -> dict:
    return json.loads((q / "webpush" / intent.FILE_NAME).read_text())["overlay"]


def test_a_preset_keeps_the_options_it_was_changed_to_and_only_the_ending_type_outlives_an_activation(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, {"customize": "away", "lets_through": "approvals"}, token=T)
        assert st == 200 and got["presets"]["away"]["custom"] is True and got["presets"]["quiet"]["custom"] is False
        assert "Lets through: Approvals" in got["presets"]["away"]["summary"]
        _call(port, "POST", SET, {"preset": "back"}, token=T)
        _call(port, "POST", SET, {"preset": "away"}, token=T)                 # a later tap starts from what Away was changed to
        assert _overlay(q)["lets_through"] == "approvals" and _overlay(q)["until"] == {"kind": "input"}
        _call(port, "POST", SET, {"preset": "quiet"}, token=T)
        assert _overlay(q)["lets_through"] == "needs"                          # another preset is untouched
        # this activation ends on a 2h timer; the type is remembered, the 2h is not
        st, got, _ = _call(port, "POST", SET, {"preset": "away", "ending": "timer", "for": "2h"}, token=T)
        assert st == 200 and abs(_overlay(q)["until"]["at"] - (time.time() + 7200)) < 30
        _call(port, "POST", SET, {"preset": "back"}, token=T)
        _call(port, "POST", SET, {"preset": "away"}, token=T)
        assert _overlay(q)["until"]["kind"] == "timer" and abs(_overlay(q)["until"]["at"] - (time.time() + presence.DEFAULT_SECS)) < 30
        # Quiet remembered to end on input still takes a duration a caller gives, and an ending Back cannot have is refused
        _call(port, "POST", SET, {"preset": "quiet", "ending": "input"}, token=T)
        assert _call(port, "POST", SET, {"preset": "quiet", "for": "30m"}, token=T)[0] == 200 and _overlay(q)["until"]["kind"] == "timer"
        assert _call(port, "POST", SET, {"preset": "back", "ending": "garbage"}, token=T)[0] == 422
        _call(port, "POST", SET, {"preset": "away"}, token=T)
        # a change to the preset that stands lands now and keeps how it ends
        end = _overlay(q)["until"]
        _call(port, "POST", SET, {"customize": "away", "devices": "follow"}, token=T)
        assert _overlay(q)["devices"] == "follow" and _overlay(q)["until"] == end


def test_reset_returns_a_preset_and_back_to_the_system_defaults(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        _call(port, "POST", SET, {"customize": "sleep", "lets_through": "nothing", "until": "timer"}, token=T)
        _call(port, "POST", SET, {"customize": "back", "devices": "off"}, token=T)
        st, got, _ = _call(port, "POST", SET, {"reset": "sleep"}, token=T)
        assert st == 200 and got["presets"]["sleep"]["custom"] is False and got["presets"]["back"]["custom"] is True
        _call(port, "POST", SET, {"preset": "sleep"}, token=T)
        assert _overlay(q)["lets_through"] == "needs" and _overlay(q)["until"] == {"kind": "input"}
        st, got, _ = _call(port, "POST", SET, {"reset": "back"}, token=T)
        assert st == 200 and got["presets"]["back"]["custom"] is False and got["base"]["devices"] == "follow"
        assert _call(port, "POST", SET, {"customize": "nowhere", "devices": "on"}, token=T)[0] == 422
