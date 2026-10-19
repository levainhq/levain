"""Q17 Q-2a: the phone port lists open questions and takes an unsigned answer (an option key or verbatim words)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain.consent import band
from levain.consent.questions import DB_NAME, QuestionStore
from levain.phone_server import PHONE_TOKEN_HEADER, make_phone_server

_TOK = "phone-q-token"
_CALLS: list = []   # the refs the stubbed delivery was asked for


@contextmanager
def _serving(qdir: Path):
    httpd = make_phone_server(qdir, "localhost", port=0, token=_TOK)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(url: str, body: object = None, *, token: str | None = _TOK, raw: bytes | None = None):
    h = {}
    if token is not None:
        h[PHONE_TOKEN_HEADER] = token
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


@pytest.fixture
def qdir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    _CALLS.clear()
    calls = _CALLS
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: (
        calls.append(ref), type("D", (), {"route": "hook", "reason": "next hook delivers it"})())[1])
    return d


def _file(qdir: Path, what: str = "Ship it?") -> int:
    q = QuestionStore(qdir).file(agent="claude_code", agent_session_id="s1", what=what,
                                 options=[("yes", "Ship"), ("no", "Hold")], rec="yes", blocks="the release",
                                 cwd="/w")
    return q.n


def test_listing_has_open_questions_only_and_makes_no_store(qdir: Path) -> None:
    with _serving(qdir) as base:
        status, body = _call(base + "/consent.json")
        assert status == 200 and body["questions"] == [] and body["questions_overflow"] == 0
        assert not (qdir / DB_NAME).exists()
        a, b = _file(qdir, "first"), _file(qdir, "second")
        QuestionStore(qdir).answer(a, option="no", channel="cli")
        (row,) = _call(base + "/consent.json")[1]["questions"]
    assert row["n"] == b and row["what"] == "second" and row["rec"] == "yes" and row["blocks"] == "the release"
    assert row["options"] == [{"key": "yes", "label": "Ship"}, {"key": "no", "label": "Hold"}]
    assert row["agent"] == "claude_code" and row["id"].startswith("q-") and row["filed_at"]


def test_answer_by_option_records_phone_channel_and_reports_delivery(qdir: Path) -> None:
    n = _file(qdir)
    with _serving(qdir) as base:
        status, body = _call(base + f"/question/{n}/answer", {"option": "no"})
    assert status == 200 and body == {"answered": n, "delivery": {"route": "hook", "reason": "next hook delivers it"}}
    a = QuestionStore(qdir).get(n).answer
    assert (a.kind, a.value, a.channel) == ("option", "no", "phone")
    assert len(_CALLS) == 1


def test_answer_by_text_is_verbatim(qdir: Path) -> None:
    n = _file(qdir)
    words = "  ship it,\nbut after lunch <b>ok</b>  "
    with _serving(qdir) as base:
        assert _call(base + f"/question/{n}/answer", {"text": words})[0] == 200
    a = QuestionStore(qdir).get(n).answer
    assert (a.kind, a.value, a.channel) == ("text", words, "phone")


def test_second_answer_is_refused_and_changes_nothing(qdir: Path) -> None:
    n = _file(qdir)
    with _serving(qdir) as base:
        assert _call(base + f"/question/{n}/answer", {"option": "yes"})[0] == 200
        status, body = _call(base + f"/question/{n}/answer", {"option": "no"})
    assert status == 409 and "already answered" in body["message"]
    assert QuestionStore(qdir).get(n).answer.value == "yes"


def test_no_token_is_refused(qdir: Path) -> None:
    n = _file(qdir)
    with _serving(qdir) as base:
        assert _call(base + f"/question/{n}/answer", {"option": "yes"}, token=None)[0] == 403
        assert _call(base + f"/question/{n}/answer", {"option": "yes"}, token="wrong")[0] == 403
    assert QuestionStore(qdir).get(n).answer is None


@pytest.mark.parametrize("path", ["/question/abc/answer", "/question/1234567890123/answer", "/question//answer",
                                  "/question/1x/answer", "/question/1/other"])
def test_bad_n_is_not_a_route(qdir: Path, path: str) -> None:
    _file(qdir)
    with _serving(qdir) as base:
        assert _call(base + path, {"option": "yes"})[0] == 404


def test_unknown_question_and_bad_bodies_are_refused(qdir: Path) -> None:
    n = _file(qdir)
    with _serving(qdir) as base:
        assert _call(base + "/question/999/answer", {"option": "yes"})[0] == 404
        for bad in ({"option": "yes", "text": "x"}, {}, {"option": "maybe"}, {"option": 3}, {"text": "   "},
                    {"other": "x"}, ["yes"]):
            status, body = _call(base + f"/question/{n}/answer", bad)
            assert 400 <= status < 500 and body["error"] and body["message"], (bad, status, body)
        status, body = _call(base + f"/question/{n}/answer", raw=b"not json")
        assert status == 400 and body["error"] == "bad_request"
    assert QuestionStore(qdir).get(n).answer is None
    assert not _CALLS
