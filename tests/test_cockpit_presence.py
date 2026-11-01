"""The cockpit's presence word: GET /webpush/presence and POST /webpush/presence/set ride the push family's gate and call
presence.set_word as the operator's own act. Tap tokens (a purpose-bound credential for a Shortcut or NFC tag) are
tested further down."""
from __future__ import annotations

import fcntl
import json
import time

import pytest

from levain import web_server
from levain.webpush import presence
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
        assert presence.policy_view(q)["status_kind"] == "default"
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
                   "Tap tokens", "X-Levain-Presence-Token", "Copy", "Presence settings", "presence-settings", "Pings go to",
                   "Until back", "route_source", "the route file is unreadable: pings are being pushed"):
        assert needle in js, needle


@needs_browser
def test_the_chip_reads_the_word_and_none_and_the_sheet_posts_each_body(rig, tmp_path, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            {"eval": _CHIP_TEXT},
            {"eval": f"[...document.querySelectorAll('#pr-sheet .pr-sheet-word')].map(b=>b.textContent)"},
            _opened(),
            {"eval": "document.getElementById('pr-sheet').contains(document.activeElement)"},
            {"do": "document.querySelector('.pr-dur[data-for=\"2h\"]').click(); document.querySelector('.pr-sheet-word[data-word=\"quiet\"]').click()",
             "until": f"{_CHIP_TEXT}.startsWith('Quiet \\u00b7 ')", "ms": 8000},
            {"eval": _CHIP_TEXT},
            {"eval": f"[document.getElementById('pr-sheet').hidden, document.activeElement===({_CHIP}), localStorage.getItem('levain_presence_for')]"},
            _opened(),
            {"do": "document.querySelector('.pr-sheet-word[data-word=\"away\"]').click()", "until": f"{_CHIP_TEXT}==='Away'", "ms": 8000},
            _opened(),
            {"do": "document.querySelector('.pr-route-btn[data-route=\"devices\"]').click()", "until": f"{_CHIP_TEXT}==='Away \\u00b7 devices'", "ms": 8000},
            {"eval": f"fetch('/webpush/presence', {{headers: {{'X-Levain-Write-Token': '{T}'}}}}).then(r => r.json()).then(j => Math.round((j.route_until - Date.now() / 1000) / 3600))"},
            _opened(),
            {"do": "document.querySelector('.pr-dur[data-for=\"\"]').click(); document.querySelector('.pr-route-btn[data-route=\"desk\"]').click()",
             "until": f"{_CHIP_TEXT}==='Away'", "ms": 8000},
            _opened(),
            {"do": "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))", "until": "document.getElementById('pr-sheet').hidden", "ms": 4000},
            {"eval": f"document.activeElement===({_CHIP})"},
            _opened(),
            {"do": "document.body.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))", "until": "document.getElementById('pr-sheet').hidden", "ms": 4000},
        ])
    v = out["values"]
    assert v[1].startswith("Holding")                                      # no word: the chip shows what the rule does (the desk is active)
    assert v[2] == ["Here", "Away", "Quiet", "Sleep", "Loud"]
    assert v[4] is True                                                    # focus moved into the sheet
    assert v[6].startswith("Quiet · ") and v[6].endswith(("m", "h"))
    assert v[7] == [True, True, "2h"]                                      # closed after the write, focus back, the pick remembered
    assert v[-3] is True                                                   # Escape put focus back on the chip
    assert v[12] == 2                                                      # Devices took the picked 2h
    rows = [json.loads(l) for l in (q / "webpush" / presence.LOG_NAME).read_text().splitlines()]
    assert [(r["word"], r.get("duration")) for r in rows] == [("quiet", "2h"), ("away", None), ("devices", None), ("desk", None)]


@needs_browser
def test_a_standing_word_shows_in_the_chip_with_its_time_left(rig, tmp_path, monkeypatch):
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
    assert v[1] == "Quiet · 1h" or v[1].startswith("Quiet · 59m")      # an hour from the moment it was set
    assert v[2].startswith("Presence: Quiet") and v[3] == "BUTTON"


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


# ---- the route axis over HTTP ------------------------------------------------------------------------------------
def test_the_view_carries_the_route_and_a_route_word_leaves_the_presence_word(rig):
    from levain.webpush import route

    src, q = rig
    with _signed(src, q) as (port, _):
        got = _call(port, "GET", GET, token=T)[1]
        assert (got["route"], got["route_source"], got["route_until"]) == ("desk", "default", None)
        presence.set_word(q, "quiet", duration="1h")
        t0 = time.time()
        st, got, _ = _call(port, "POST", SET, {"word": "devices", "for": "2h"}, token=T)
        assert st == 200 and got["route"] == "devices" and got["route_source"] == "operator" and got["word"] == "quiet"
        assert abs(got["route_until"] - (t0 + 7200)) < 30
        assert _call(port, "POST", SET, {"word": "desk"}, token=T)[1]["route"] == "desk"
        assert _call(port, "POST", SET, {"word": "devices"}, token=T)[1]["route"] == "devices"
        assert route.read_route(q)[0] == "devices"
        assert _call(port, "POST", SET, {"word": "back"}, token=T)[1]["route"] == "desk"


def test_the_view_never_runs_the_suggestion_and_a_ping_decision_does(rig, monkeypatch):
    from levain.webpush import presence as pres, route as route_mod

    src, q = rig
    ran = []
    monkeypatch.setattr("levain.webpush.route._run_who", lambda: ran.append(1) or "phill ttys004 Nov 1 09:12 (203.0.113.7)")
    with _signed(src, q) as (port, _):
        got = _call(port, "GET", GET, token=T)[1]
        assert got["route"] == "desk" and not ran                    # a GET (a tap token's too) shells out to nothing
        pres.should_push(q, "ping", idle=lambda: 900.0)
        got = _call(port, "GET", GET, token=T)[1]
        assert got["route"] == "devices" and got["route_source"] == "auto"
        _call(port, "POST", SET, {"word": "desk"}, token=T)
        got = _call(port, "GET", GET, token=T)[1]
        assert got["route"] == "desk" and got["route_source"] == "operator"      # his word beats the suggestion


def test_a_tap_token_with_devices_sets_the_route_and_one_without_is_refused(rig):
    from levain.webpush import route

    src, q = rig
    with _signed(src, q) as (port, _):
        narrow, _ = _mint(port, "narrow", ["away"])
        st, got, _ = _call(port, "POST", SET, {"word": "devices"}, headers=_tap(narrow))
        assert st == 403 and "away" in got["message"] and route.read_route(q)[0] == "desk"
        tok, _ = _mint(port)
        st, got, _ = _call(port, "POST", SET, {"word": "devices"}, headers=_tap(tok))
        assert st == 200 and got["route"] == "devices"
        log = (q / "webpush" / presence.LOG_NAME).read_text()
        assert json.loads(log.splitlines()[-1])["via"] == "presence:door"


def test_a_revoke_before_the_route_write_refuses_it(rig, monkeypatch):
    from levain.webpush import route

    src, q = rig
    with _signed(src, q) as (port, _):
        tok, _ = _mint(port)
        monkeypatch.setattr(presence_tokens, "verify", lambda *a, **k: None)
        assert _call(port, "POST", SET, {"word": "devices"}, headers=_tap(tok))[0] == 403
        assert route.read_route(q)[0] == "desk"


def test_a_route_set_flushes_held_pings_outside_the_lock_and_without_the_spawn_gap(rig, monkeypatch):
    from levain.webpush import held

    src, q = rig
    calls = []
    monkeypatch.setattr(held, "flush_if_due", lambda queue, *, throttle=True: calls.append((queue, throttle)) or False)
    with _signed(src, q) as (port, _):
        for word in ("devices", "desk", "back"):
            assert _call(port, "POST", SET, {"word": word}, token=T)[0] == 200
        # desk holds, so it flushes nothing; each devices-bound set flushes once (not twice)
        assert len(calls) == 2 and all(t is False for _, t in calls)
        _call(port, "POST", SET, {"word": "away"}, token=T)
        assert len(calls) == 2


# ---- P1: the `remote` word is gone, and the view and the chip carry the EFFECTIVE state -----------------------------

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


def test_the_view_carries_effective_and_it_agrees_with_the_push_decision(rig, monkeypatch):
    src, q = rig
    idle = [5.0]
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: idle[0])
    with _signed(src, q) as (port, _):
        got = _call(port, "GET", GET, token=T)[1]
        eff = got["effective"]
        assert got["word"] is None and (eff["state"], eff["pushing"], eff["source"], eff["since"]) == ("holding", False, "measured", None)
        assert eff["pushing"] is presence.should_push(q, "ping", suggest=False).push
        idle[0] = 900.0                                             # the desk went idle: the same stored (no) word now pushes
        eff = _call(port, "GET", GET, token=T)[1]["effective"]
        assert (eff["state"], eff["pushing"], eff["source"]) == ("pushing", True, "measured") and "desk inactive" in eff["reason"]
        _call(port, "POST", SET, {"word": "quiet", "for": "1h"}, token=T)
        eff = _call(port, "GET", GET, token=T)[1]["effective"]
        assert (eff["state"], eff["pushing"], eff["source"]) == ("muted", False, "word") and abs(eff["since"] - time.time()) < 30


def _chip_text_when(rig, tmp_path, monkeypatch, idle_secs, word=None, word_age=0.0):
    src, q = rig
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: idle_secs)
    if word:
        presence.set_word(q, word, now=time.time() - word_age)
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000}, {"eval": _CHIP_TEXT}, {"eval": f"{_CHIP}.dataset.state"}])
    return out["values"][1], out["values"][2]


@needs_browser
def test_the_chip_paints_the_effective_state_not_the_stored_word(rig, tmp_path, monkeypatch):
    # no word on file, the desk idle 15 min: pushing
    text, state = _chip_text_when(rig, tmp_path, monkeypatch, 900.0)
    assert text.startswith("Pushing") and "desk inactive" in text and state == "pushing"
    # no word, the desk active: holding
    text, state = _chip_text_when(rig, tmp_path, monkeypatch, 5.0)
    assert text.startswith("Holding") and state == "holding"
    # `away` on file but he typed more than the grace after it: the word is spent, the chip says what the rule does
    text, state = _chip_text_when(rig, tmp_path, monkeypatch, 5.0, word="away", word_age=1200.0)
    assert text.startswith("Holding") and "Away" not in text and state == "holding"


@needs_browser
def test_an_older_server_without_effective_paints_the_stored_word(rig, tmp_path, monkeypatch):
    src, q = rig
    real = web_server._Handler._presence_view

    def old_view(self, queue):
        v = real(self, queue)
        v.pop("effective", None)
        return v

    monkeypatch.setattr(web_server._Handler, "_presence_view", old_view)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: 900.0)
    presence.set_word(q, "quiet", duration="1h")
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000}, {"eval": _CHIP_TEXT}])
    assert out["values"][1].startswith("Quiet \u00b7 ")


def test_the_panel_copy_cuts_a_reason_at_a_word_and_does_not_promise_sleep_a_duration():
    js = _asset("devices_panel.js")
    assert "u2026" in js and "slice(0, 40)" in js
    assert "Quiet, Sleep, Loud" not in js and "Sleep takes no duration" in js
