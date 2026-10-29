"""Q17-ALL S5a: the cockpit's `band_answer` verb. Same shared answer path as the phone (channel `cockpit`, viewer
`cockpit:<credential>`, bound to the view that viewer was shown).

Q1 RULED (a): "Information answers stay unsigned and are accepted only from human channels; agent-typed or relayed
answers are refused and the item stays open." The cockpit write credential is the human gate; no credential: nothing
recorded. The verb never signs."""
from __future__ import annotations

import sqlite3

import pytest

from levain import band_view
from levain.consent import band
from levain.consent.questions import QuestionStore
from tests.test_cockpit_k2a import TOKEN, _get, _install, _post, _serve

OPTS = [("ship", "Ship it tonight"), ("hold", "Hold for the morning")]


@pytest.fixture
def lh(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    return tmp_path / "lh" / "consent"


@pytest.fixture(autouse=True)
def _no_delivery(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: calls.append(ref))
    return calls


def _file(store: QuestionStore, what: str = "Ship the sweep fix?"):
    return store.file(agent="claude_code", agent_session_id="s1", what=what, options=OPTS, rec="hold",
                      blocks="the sweep", cwd="/w")


def _shown(store: QuestionStore) -> dict:
    """The cockpit viewer is shown the band; the binding its answer carries back."""
    b = band_view.band_payload(store, "cockpit:browser")
    return {"store_id": b["store_id"], "view_id": b["view_id"]}


def _verb(base: str, params: dict, token: str | None = TOKEN):
    return _post(f"{base}/cockpit/verb", {"verb": "band_answer", "panel_id": "decisions", "params": params}, token)


def test_band_answer_is_offered_on_the_decisions_panel_and_records_cockpit_channel(tmp_path, lh, _no_delivery):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    bind = _shown(store)                                   # the cockpit read that numbers and records the view
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _verb(base, {"text": "1b", **bind})
        assert status == 200, body
        assert body["recorded"] == ["1b"] and body["refused"] == []
    a = store.get(q.n).answer
    assert (a.kind, a.value, a.channel) == ("option", "hold", "cockpit")
    with sqlite3.connect(store.path) as c:
        ctx = c.execute("SELECT via, viewer, seen_through FROM answer_context WHERE question_id = ?",
                        (q.id,)).fetchone()
    assert ctx == ("cockpit", "cockpit:browser", 1)
    assert _no_delivery == [q.id]


def test_band_answer_needs_the_write_credential_and_records_nothing_without_it(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    params = {"text": "1a", **_shown(store)}
    with _serve(src, write_token=TOKEN) as (base, _h):
        assert _verb(base, params, token=None)[0] == 403
        assert _verb(base, params, token="not-the-token")[0] == 403
    assert store.get(q.n).answer is None


def test_band_answer_refuses_a_stale_store_or_a_view_never_recorded(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    with _serve(src, write_token=TOKEN) as (base, _h):
        sid = store.store_id
        s, body = _verb(base, {"text": "1a", "store_id": sid, "view_id": 1})             # no view recorded yet
        assert s == 409 and body["error"] == "stale_band"
        bind = _shown(store)
        s, body = _verb(base, {"text": "1a", **bind, "store_id": "another"})
        assert s == 409 and body["error"] == "stale_band"
        s, body = _verb(base, {"text": "1a", **bind, "view_id": bind["view_id"] + 3})
        assert s == 409 and body["error"] == "stale_band"
        _file(store, "A second one?")
        _shown(store)                                      # a newer view: the first binding is stale now
        s, body = _verb(base, {"text": "1a", **bind})
        assert s == 409 and body["error"] == "stale_band"
    assert store.get(q.n).answer is None


def test_band_answer_takes_exactly_its_fields_and_the_whole_text_must_parse(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    good = {"text": "1a", **_shown(store)}
    with _serve(src, write_token=TOKEN) as (base, _h):
        assert _verb(base, {**good, "channel": "phone"})[0] == 400
        assert _verb(base, {"text": "1a"})[0] == 400
        assert _verb(base, {**good, "view_id": "1"})[0] == 400
        assert _verb(base, {"text": "1a", "seen_through": 1, "store_id": good["store_id"]})[0] == 400   # r1's binding
        s, body = _verb(base, {**good, "text": "just ship it"})
        assert s == 422 and body["error"] == "answer_refused"
    assert store.get(q.n).answer is None


@pytest.mark.parametrize("fault", [sqlite3.OperationalError("disk I/O error"),
                                   sqlite3.DatabaseError("database disk image is malformed"),
                                   OSError(28, "No space left on device")])
def test_a_store_write_failure_is_503_never_a_refusal(tmp_path, lh, monkeypatch, fault):
    """codex MED 4: an infrastructure failure inside the answer's write reaches the 503 handler."""
    _root, src, _ids = _install(tmp_path)
    store = QuestionStore(lh)
    q = _file(store)
    bind = _shown(store)

    def faulting(self, *a, **k):
        raise fault

    monkeypatch.setattr(QuestionStore, "answer_with_context", faulting)
    with _serve(src, write_token=TOKEN) as (base, _h):
        s, body = _verb(base, {"text": "1a", **bind})
    assert s == 503 and body["error"] == "source_unavailable", body
    assert store.get(q.n).answer is None


def test_the_verb_is_listed_by_the_decisions_panel_actions(tmp_path, lh):
    _root, src, _ids = _install(tmp_path)
    QuestionStore(lh)
    with _serve(src, write_token=TOKEN) as (base, _h):
        panel = _get(f"{base}/cockpit/panel/decisions.json", TOKEN)
    assert "band_answer" in [a["verb"] for a in panel.get("actions", [])]
