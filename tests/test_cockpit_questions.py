"""Agents' questions in the decisions panel (Q-2b): rows without bodies, a body behind the consent gate, a C1 answer verb."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit import Cockpit, ProviderSpec, Read, RowIn
from levain.cockpit.results import Absent
from levain.consent.questions import QuestionStore
from tests.test_cockpit_k2a import TOKEN, _get, _install, _post, _serve

WHAT = "Ship the sweep fix tonight or hold it for the morning review?"
BLOCKS = "the nightly sweep stays red until it ships"
OPTIONS = [("ship", "Ship it tonight (zealous label)"), ("hold", "Hold for the morning")]
T0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


def _file(store: QuestionStore, **kw):
    args = dict(agent="claude_code", agent_session_id="sess-1", what=WHAT, options=OPTIONS, rec="hold",
                blocks=BLOCKS, cwd="/w")
    args.update(kw)
    return store.file(**args)


@pytest.fixture
def lh(tmp_path, monkeypatch):
    """A levain home whose consent directory the server's question store reads (the default queue dir)."""
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    return tmp_path / "lh" / "consent"


def _row(base: str, n: int, token: str | None = TOKEN) -> dict:
    rows = _get(f"{base}/cockpit/panel/decisions.json", token)["rows"]
    return next(r for r in rows if r["id"] == f"question:{n}")


def _verb(base: str, row: dict, params: dict, token: str | None = TOKEN, version: str | None = None):
    return _post(f"{base}/cockpit/verb", {"verb": "question_answer", "panel_id": "decisions", "row_id": row["id"],
                                          "row_version": version or row["version"], "params": params}, token)


def _get_status(url: str, token: str | None) -> tuple[int, dict]:
    headers = {"X-Levain-Write-Token": token} if token else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# --- R-q10: a row carries no body ----------------------------------------------------------------------


def test_a_question_row_carries_no_what_labels_or_blocks_and_even_a_token_free_read_lists_it(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    q = _file(QuestionStore(lh))
    with _serve(src, write_token=TOKEN) as (base, _h):
        for token in (TOKEN, None):                          # the panel is token-free on loopback
            row = _row(base, q.n, token)
            dumped = json.dumps(row)
            for secret in (WHAT, BLOCKS, "zealous label", "Hold for the morning"):
                assert secret not in dumped
            f = row["facets"]
            assert f["agent"] == "claude_code" and f["question_n"] == q.n and f["question_state"] == "open"
            assert f["question_options"] == 2 and f["at"] == q.filed_at
            assert row["group"] == "needs_input"
            assert row["title"] == f"claude_code · question #{q.n}"


def test_an_answered_question_row_is_resolved_with_its_answer_time_and_a_new_version(tmp_path, lh):
    store = QuestionStore(lh)
    q = _file(store)
    ck = Cockpit(clock=lambda: datetime.now(timezone.utc))
    ck.attach_question_rows(store)
    open_row = ck.panel("decisions", credential_class="token")["rows"][0]
    store.answer(q.n, option="ship", channel="phone")
    rows = ck.panel("decisions", credential_class="token")["rows"]
    assert [r["id"] for r in rows] == [f"question:{q.n}"]
    assert rows[0]["group"] == "resolved" and rows[0]["facets"]["question_state"] == "answered"
    assert "decided_at" in rows[0]["facets"] and rows[0]["version"] != open_row["version"]


def test_no_questions_db_lists_nothing_and_creates_nothing(tmp_path):
    qdir = tmp_path / "never"
    ck = Cockpit(clock=lambda: T0)
    ck.attach_question_rows(QuestionStore(qdir))
    p = ck.panel("decisions", credential_class="token")
    assert p["rows"] == [] and "question" not in p.get("note", "")
    assert not qdir.exists()
    assert isinstance(ck.read_one("decisions", "question:1"), Absent) and not qdir.exists()


def test_a_store_fault_is_a_note_not_a_dark_panel():
    class Boom:
        def open_questions(self):
            raise RuntimeError("store gone")
    ck = Cockpit(clock=lambda: T0)
    ck.attach_question_rows(Boom())
    p = ck.panel("decisions", credential_class="token")
    assert p["status"] != "error" and "agent questions unavailable" in p["note"]


# --- R-q16: the kernel prefix --------------------------------------------------------------------------


def test_a_downstream_row_with_a_question_id_is_skipped(tmp_path, lh):
    ck = Cockpit(clock=lambda: T0)
    ck.attach_decision_source(ProviderSpec(
        "decisions", "triage-list", "Decisions waiting", "gate",
        lambda ctx: Read(rows=(RowIn(id="question:forged", title="forged", stored={"s": 1}),
                               RowIn(id="np:1", title="real", stored={"s": 1}))),
        order="decision.dated", version_fields=("*",), rowset="needs_phill",
        read_one=lambda ctx, rid: Absent("gone")))
    ck.attach_question_rows(QuestionStore(tmp_path / "none"))
    p = ck.panel("decisions", credential_class="token")
    assert [r["id"] for r in p["rows"]] == ["np:1"]
    assert any("question:" in str(s.get("reason")) for s in p["skipped"])


# --- R-q11: the body behind the consent-body gate -------------------------------------------------------


def test_the_body_route_refuses_without_a_token_on_loopback_and_serves_with_one(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    q = _file(QuestionStore(lh))
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _get_status(f"{base}/cockpit/question/{q.n}.json", None)
        assert status == 403 and WHAT not in json.dumps(body)
        status, body = _get_status(f"{base}/cockpit/question/{q.n}.json", "wrong-token")
        assert status == 403
        status, body = _get_status(f"{base}/cockpit/question/{q.n}.json", TOKEN)
        assert status == 200
        assert body["what"] == WHAT and body["blocks"] == BLOCKS and body["rec"] == "hold" and body["answer"] is None
        assert body["options"] == [{"key": k, "label": label} for k, label in OPTIONS]
        assert body["agent"] == "claude_code" and body["filed_at"] == q.filed_at
        for bad in ("99", "0", "01", "x", "1a", "9" * 13, "-1"):    # digits 1-12 only; none names a question
            assert _get_status(f"{base}/cockpit/question/{bad}.json", TOKEN)[0] == 404, bad


def test_the_body_shows_the_answer_once_given(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    store.answer(q.n, text="ship it, but page me first", channel="cli")
    with _serve(src, write_token=TOKEN) as (base, _h):
        body = _get_status(f"{base}/cockpit/question/{q.n}.json", TOKEN)[1]
    assert body["answer"]["kind"] == "text" and body["answer"]["value"] == "ship it, but page me first"
    assert body["answer"]["channel"] == "cli"


def test_the_body_route_creates_no_questions_db(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        assert _get_status(f"{base}/cockpit/question/1.json", TOKEN)[0] == 404
        assert _get_status(f"{base}/cockpit/panel/decisions.json", TOKEN)[0] == 200
    assert not (lh / "questions.db").exists()


# --- R-q12 / R-q13 / R-q14: the verb --------------------------------------------------------------------


def test_the_verb_is_refused_without_the_write_credential_and_records_nothing(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    with _serve(src, write_token=TOKEN) as (base, _h):
        row = _row(base, q.n)
        status, body = _verb(base, row, {"option": "ship"}, token=None)
        assert status == 403 and body["error"] == "credential_required"
        status, body = _verb(base, row, {"option": "ship"}, token="not-the-token")
        assert status == 403
        # a credential-less read is told the verb is not for it: the gesture is refused, nothing to tap
        gestures = [a["gesture"] for a in _row(base, q.n, None)["actions"] if a["verb"] == "question_answer"]
        assert gestures == ["refused"]
    assert store.get(q.n).answer is None


def test_it_answers_once_with_channel_cockpit_and_starts_delivery_on_a_thread(tmp_path, lh, monkeypatch):
    from levain.consent import band

    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    seen: dict = {}
    done = threading.Event()

    def fake_deliver(st, ref, **kw):
        seen.update(store=st, ref=ref, thread=threading.current_thread())
        done.set()

    monkeypatch.setattr(band, "deliver_answer", fake_deliver)
    with _serve(src, write_token=TOKEN) as (base, _h):
        row = _row(base, q.n)
        act = next(a for a in row["actions"] if a["verb"] == "question_answer")
        assert act["tier"] == "C1" and act["gesture"] == "direct"
        status, body = _verb(base, row, {"option": "ship"})
        assert status == 200, body
        assert body["answered"] == q.n and body["delivery"] == "started"
        assert done.wait(5)
        assert seen["ref"] == q.id and seen["thread"] is not threading.main_thread()
        assert seen["thread"].daemon
        got = store.get(q.n).answer
        assert (got.kind, got.value, got.channel) == ("option", "ship", "cockpit")
        # answered once: the row it was offered on is stale, and the answered row offers the verb no more
        status, body = _verb(base, row, {"option": "hold"})
        assert status == 409
        assert not [a for a in _row(base, q.n)["actions"] if a["verb"] == "question_answer"]
        again = _row(base, q.n)
        status, body = _verb(base, again, {"option": "hold"})
        assert status == 409
    assert store.get(q.n).answer.value == "ship"


def test_words_are_kept_verbatim(tmp_path, lh, monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "deliver_answer", lambda *a, **k: None)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    words = "  ship it -- but {not} tonight\nask Som first  "
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _verb(base, _row(base, q.n), {"text": words})
        assert status == 200, body
    got = store.get(q.n).answer
    assert (got.kind, got.value, got.channel) == ("text", words, "cockpit")


def test_a_stale_row_version_is_a_409_when_it_was_answered_elsewhere(tmp_path, lh, monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "deliver_answer", lambda *a, **k: None)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    with _serve(src, write_token=TOKEN) as (base, _h):
        row = _row(base, q.n)                                 # rendered open ...
        store.answer(q.n, option="hold", channel="phone")    # ... then answered on the phone
        status, body = _verb(base, row, {"option": "ship"})
        assert status == 409 and body["error"] == "stale"
        status, body = _verb(base, row, {"option": "ship"}, version="not-a-version")
        assert status == 409
    got = store.get(q.n).answer
    assert (got.value, got.channel) == ("hold", "phone")


def test_an_option_outside_the_question_and_both_or_neither_are_refused_with_nothing_recorded(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    with _serve(src, write_token=TOKEN) as (base, _h):
        row = _row(base, q.n)
        for params in ({"option": "nope"}, {"option": "ship", "text": "and words"}, {}, {"option": ["ship"]},
                       {"text": "   "}, {"text": 7}, {"option": None}):
            status, body = _verb(base, row, params)
            assert status == 400, (params, status, body)
        status, body = _verb(base, row, {"option": "ship", "extra": 1})
        assert status == 400 and body["error"] == "field_not_allowed"
    assert store.get(q.n).answer is None


def test_the_verb_is_not_offered_on_a_row_of_another_kind(tmp_path, lh):
    _root, src, ids = _install(tmp_path)
    _file(QuestionStore(lh))
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _post(f"{base}/cockpit/verb", {"verb": "question_answer", "panel_id": "tray",
                                                       "row_id": f"spore:{ids['seed']}", "row_version": "x",
                                                       "params": {"option": "ship"}}, TOKEN)
        assert status == 404 and body["error"] == "not_offered"


def test_a_delivery_error_never_undoes_the_answer(tmp_path, lh, monkeypatch):
    from levain.consent import band

    done = threading.Event()

    def boom(*a, **k):
        done.set()
        raise RuntimeError("resume blew up")

    monkeypatch.setattr(band, "deliver_answer", boom)
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _verb(base, _row(base, q.n), {"option": "ship"})
        assert status == 200 and done.wait(5)
    assert store.get(q.n).answer.value == "ship"


def test_old_answers_leave_the_panel_after_a_day(tmp_path):
    store = QuestionStore(tmp_path / "consent", clock=lambda: T0 - timedelta(days=2))
    q = _file(store)
    store.answer(q.n, option="ship", channel="cli")
    ck = Cockpit(clock=lambda: T0)
    ck.attach_question_rows(store)
    assert ck.panel("decisions", credential_class="token")["rows"] == []


# --- review fixes: one row id, a store fault, a read-only serve ------------------------------------------


def test_a_question_answered_between_the_two_reads_is_one_row_in_its_answered_state(tmp_path):
    store = QuestionStore(tmp_path / "consent")
    q = _file(store)
    stale_open = store.open_questions()           # the first read, taken before the answer lands
    store.answer(q.n, option="ship", channel="phone")

    class Split:
        def open_questions(self):
            return stale_open

        def recent(self, n):
            return store.recent(n)

    ck = Cockpit(clock=lambda: datetime.now(timezone.utc))
    ck.attach_question_rows(Split())
    p = ck.panel("decisions", credential_class="token")
    assert [r["id"] for r in p["rows"]] == [f"question:{q.n}"]
    assert p["rows"][0]["facets"]["question_state"] == "answered" and "duplicate" not in (p.get("note") or "")


def test_a_database_error_reading_one_question_is_a_fault_not_an_exception():
    import sqlite3

    from levain.cockpit.results import Fault

    class Locked:
        def open_questions(self):
            return []

        def get(self, ref):
            raise sqlite3.OperationalError("database is locked")

    ck = Cockpit(clock=lambda: T0)
    ck.attach_question_rows(Locked())
    assert isinstance(ck.read_one("decisions", "question:1"), Fault)


def test_a_locked_store_answers_503_not_500(tmp_path, lh, monkeypatch):
    import sqlite3

    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)

    def locked(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(QuestionStore, "answer", locked)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _verb(base, _row(base, q.n), {"option": "ship"})
    assert status == 503 and body["error"] == "source_unavailable"
    assert store.get(q.n).answer is None


def test_a_read_only_serve_refuses_an_answer_and_records_nothing(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    with _serve(src) as (base, _h):                          # no write_token: `levain serve` without --write
        status, body = _verb(base, _row(base, q.n, None), {"option": "ship"}, token=TOKEN)
        assert status == 403 and body["error"] == "credential_required"      # no token is configured to match
    assert store.get(q.n).answer is None
