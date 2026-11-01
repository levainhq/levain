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


@pytest.mark.parametrize("word", ["quiet", "remote", "loud"])
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
        assert got["record"]["name"] == "door" and got["record"]["words"] == ["away", "back", "quiet", "remote"]
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
            assert st == 403 and "away, back, quiet, remote" in got["message"], got
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


# ---- the Presence block in the Devices panel --------------------------------------------------------------------------

from levain.web_server import load_web_asset
from tests.test_pair_card_w1b import _drive, needs_browser

_SHOWN = "document.querySelector('.pr-panel') && !document.querySelector('.pr-panel').hidden"


def test_the_panel_script_carries_the_presence_block_and_no_client_name():
    js = load_web_asset("devices_panel.js")
    js = js[0].decode() if isinstance(js, tuple) else js
    for needle in ("Presence", "/webpush/presence", "/webpush/presence/set", "/webpush/presence/token/new",
                   "/webpush/presence/token/revoke", "Tap tokens", "X-Levain-Presence-Token", "Copy"):
        assert needle in js, needle


@needs_browser
def test_the_block_renders_a_word_button_posts_its_body_and_a_new_tap_is_shown_once(rig, tmp_path, monkeypatch):
    src, q = rig
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": _SHOWN, "ms": 20000},
            {"eval": "[...document.querySelectorAll('.pr-word')].map(b=>b.textContent)"},
            {"eval": "document.querySelector('.pr-word.dev-primary').textContent"},
            {"do": "document.querySelector('.pr-duration').value='4h'; document.querySelector('.pr-word[data-word=\"quiet\"]').click()",
             "until": "document.querySelector('.pr-state').textContent.startsWith('Quiet')", "ms": 8000},
            {"eval": "document.querySelector('.pr-state').textContent"},
            {"do": "document.querySelector('.pr-word[data-word=\"away\"]').click()",
             "until": "document.querySelector('.pr-state').textContent==='Away'", "ms": 8000},
            {"do": "document.querySelector('.pr-name').value='Door'; document.querySelector('.pr-new-form button[type=submit]').click()",
             "until": "!document.querySelector('.pr-new').hidden", "ms": 8000},
            {"eval": "document.querySelector('.pr-new .dev-url').value.length"},
            {"eval": "location.reload() || 1"},
            {"until": _SHOWN + " && document.querySelector('.pr-tap')", "ms": 20000},
            {"eval": "[document.querySelector('.pr-new').hidden, document.querySelector('.pr-tap .dev-meta').textContent]"},
            {"do": "document.querySelector('.pr-revoke').click()", "until": "!document.querySelector('.pr-tap')", "ms": 8000},
        ])
    v = out["values"]
    assert v[1] == ["Back", "Away", "Quiet", "Sleep", "Loud", "Remote"] and v[2] == "Back"
    assert v[4].startswith("Quiet (") and "left" in v[4]
    assert v[7] >= 40                                     # the token was shown
    assert v[10][0] is True and "away, back, quiet, remote" in v[10][1]    # gone after a reload; the list names its words
    assert presence_tokens.list_public(q) == []
    rows = [json.loads(l) for l in (q / "webpush" / presence.LOG_NAME).read_text().splitlines()]
    assert [(r["word"], r.get("duration")) for r in rows[:2]] == [("quiet", "4h"), ("away", None)]
