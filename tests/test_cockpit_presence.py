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
