"""H1-4 shape A, step 3: the cockpit half of a room ENVELOPE card, through the real ``levain serve`` envelope. The card is
a proposal of a second kind (made here by ``room.propose_spec``: no door exists yet); the read names its kind, thread,
title, his quoted words and whether a newer card replaced it; ``preview`` reads the posted bytes back as plain words and
the hash the tap must echo; the tap goes to ``spec_card.tap`` only with the thread and the hash it was shown. Each test
names the failure it reproduces. The page's own checks are string-level (the glass is Phill's)."""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from levain.cockpit import room as cockpit_room
from levain.desk import room
from levain.desk.chat import ChatService
from levain.desk.store import words_sha256
from tests.test_chat_room_send import LIVE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_room import _assets, _boot
from tests.test_desk_room import say
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_spec_card import (_TABLES, BUDGET, built, card, counts, ctx, head_thread, keys, plain_work,  # noqa: F401
                                  propose, repo, snapshot, spec_text)

JS = (Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web" / "room_view.js").read_text()


def _call(base, path, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Levain-Write-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _serving(card, tmp_path, monkeypatch, **kw):
    real = cockpit_room.serve_room
    # the server reads the room from the consent directory; this store lives in the fixture's own
    monkeypatch.setattr(cockpit_room, "serve_room",
                        lambda path, *, directory, **kw: real(path, directory=card.store.directory, **kw))
    svc = ChatService(card.cs, **LIVE)
    host = cockpit_room.RoomHost(svc, card.store)
    _root, src, _ids = _install(tmp_path / "entity")
    with _serve(src, write_token=TOKEN, chat_service=svc, room_host=host, **kw) as (base, _h):
        card.base = base
        yield card


@pytest.fixture
def served(card, tmp_path, monkeypatch):
    yield from _serving(card, tmp_path, monkeypatch)


@pytest.fixture
def paged(card, tmp_path, monkeypatch):
    yield from _serving(card, tmp_path, monkeypatch, extra_assets=_assets(_boot()))


def cards(c):
    status, got = _call(c.base, cockpit_room.ROOM_PATH, method="GET")
    assert status == 200, got
    return got["proposals"]


def act(c, p, action, body):
    return _call(c.base, f"/cockpit/room/proposal/{p.proposal_id}/{action}", body)


def commit_body(c, p, words, **over):
    body = {"words": words, "outbox_id": "ob-tap00001", "thread_id": c.thread, "input_sha256": words_sha256(words)}
    body.update(over)
    return body


@pytest.fixture
def one(served):
    _d, t = plain_work(served.ctx)
    served.thread = t.thread_id
    served.text = spec_text(served.ctx)
    served.p = propose(served, t.thread_id, served.text)
    return served


# --- the read ---------------------------------------------------------------------------------------------------------
def test_the_read_names_the_envelope_card_its_thread_title_quotes_and_replacement(one):
    """Failure: a card the page cannot tell from a plain drop card, or one that hides which thread it is for."""
    [c] = cards(one)
    assert c["kind"] == "envelope" and c["thread_id"] == one.thread and c["words"] == one.text
    assert c["title"] and c["quoted_words"] == "give it two hours" and c["replaced_by"] is None
    assert c["state"] == "open" and c["signed_head"] is False
    newer = propose(one, one.thread, one.text, "again")
    first, second = cards(one)
    assert first["replaced_by"] == newer.proposal_id and second["replaced_by"] is None


def test_a_plain_drop_card_keeps_its_kind_and_is_not_replaced_by_an_envelope_card(served):
    mid = say(served.cs, served.room, "tell the desk hi", n=7)
    with served.store._write() as (conn, now):
        room.propose(conn, served.launch, [mid], "he said hi", now=now)
    [c] = cards(served)
    assert c["kind"] == "drop" and c["thread_id"] is None and c["replaced_by"] is None


def test_the_read_says_when_a_signed_grant_is_running(served, monkeypatch):
    """Failure: the 'drafts a replacement' line shown on a thread with nothing signed (or hidden on one with a grant)."""
    _d, _b, t = head_thread(served.ctx)
    monkeypatch.setattr(cockpit_room, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: served.ctx.clk[0])}))
    p = propose(served, t.thread_id, served.text if hasattr(served, "text") else spec_text(served.ctx))
    [c] = cards(served)
    assert c["proposal_id"] == p.proposal_id and c["signed_head"] is True


def test_a_stopped_grant_is_not_called_running(served, monkeypatch):
    """Failure: 'what you signed keeps running' said of a head whose stop has passed."""
    from datetime import timedelta
    _d, _b, t = head_thread(served.ctx)
    later = served.ctx.clk[0] + timedelta(days=30)
    monkeypatch.setattr(cockpit_room, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: later)}))
    propose(served, t.thread_id, spec_text(served.ctx))
    assert cards(served)[0]["signed_head"] is False


# --- preview ----------------------------------------------------------------------------------------------------------
def test_preview_reads_the_posted_bytes_and_returns_the_hash_the_tap_must_echo(one):
    """Failure: a reading of the stored spec rather than of what the field holds."""
    edited = spec_text(one.ctx, budget={**BUDGET, "hours": 1})
    before = counts(one.store)
    status, got = act(one, one.p, "preview", {"words": edited})
    assert status == 200, got
    assert got["input_sha256"] == words_sha256(edited) and got["thread_id"] == one.thread
    assert {ln["field"] for ln in got["lines"]} >= {"where", "budget", "stop"}
    assert any(ln["wider"] for ln in got["lines"])
    assert counts(one.store) == before                               # a reading writes nothing


@pytest.mark.parametrize("words", ["not json", json.dumps({"launches": []}), "{}", json.dumps({"wher": 1}),
                                   '{"stop": "2h", "stop": "3h"}'])
def test_preview_names_the_grammars_refusal_and_writes_nothing(one, words):
    before = counts(one.store)
    status, got = act(one, one.p, "preview", {"words": words})
    assert status == 409 and got["error"] == "spec_refused" and got["message"], got
    assert counts(one.store) == before


def test_preview_of_a_plain_card_or_a_missing_one_is_refused(served):
    mid = say(served.cs, served.room, "hi", n=9)
    with served.store._write() as (conn, now):
        p = room.propose(conn, served.launch, [mid], "s", now=now)
    assert act(served, p, "preview", {"words": "x"})[0] == 404
    ghost = type("P", (), {"proposal_id": "pr-" + "0" * 32})
    assert act(served, ghost, "preview", {"words": "x"})[0] == 404


def test_preview_needs_a_named_credential_and_a_string(one):
    status, got = act(one, one.p, "preview", {"words": 3})
    assert status == 400 and got["error"] == "bad_request"
    status, got = act(one, one.p, "preview", {})
    assert status == 400


# --- the tap ----------------------------------------------------------------------------------------------------------
def test_the_tap_writes_the_bytes_he_posted_not_the_stored_spec(one):
    """Failure: the proposal row feeding the draft. Stored A, posted B: the drop and the draft are B's."""
    posted = spec_text(one.ctx, budget={**BUDGET, "hours": 1})
    status, got = act(one, one.p, "commit", commit_body(one, one.p, posted))
    assert status == 200 and got["created"] is True and got["draft_id"] and got["revision"] >= 1, got
    c = sqlite3.connect(one.store.path)
    try:
        assert c.execute("SELECT words FROM drops WHERE surface = 'cockpit'").fetchall() == [(posted,)]
        assert c.execute("SELECT words_sha256 FROM spec_card_taps").fetchall() == [(words_sha256(posted),)]
        assert c.execute("SELECT count(*) FROM envelopes").fetchone()[0] == 0            # nothing signed by a tap
    finally:
        c.close()
    [card_] = cards(one)
    assert card_["state"] == "committed" and card_["words"] == posted and card_["draft_id"] == got["draft_id"]


@pytest.mark.parametrize("over,error", [
    ({"thread_id": "th-" + "1" * 32}, "thread_mismatch"),
    ({"input_sha256": "sha256:" + "0" * 64}, "stale_preview"),
])
def test_a_tap_that_adopts_another_thread_or_other_bytes_writes_nothing(one, over, error):
    """Failure: a tap adopting a target or bytes he did not see."""
    before = counts(one.store)
    status, got = act(one, one.p, "commit", commit_body(one, one.p, one.text, **over))
    assert status == 409 and got["error"] == error, got
    assert counts(one.store) == before and cards(one)[0]["state"] == "open"


@pytest.mark.parametrize("drop", ["thread_id", "input_sha256"])
def test_an_old_page_commit_on_an_envelope_card_is_refused_update_page(one, drop):
    """Failure: an old cached page committing spec JSON as a plain drop (the card of a second kind it cannot read)."""
    body = commit_body(one, one.p, one.text)
    del body[drop]
    before = counts(one.store)
    status, got = act(one, one.p, "commit", body)
    assert status == 409 and got["error"] == "update_page", got
    assert counts(one.store) == before


def test_a_replaced_card_is_refused_and_the_newer_one_taps(one):
    """Failure: a stale card revising over a newer one."""
    newer = propose(one, one.thread, one.text, "newer")
    before = counts(one.store)
    status, got = act(one, one.p, "commit", commit_body(one, one.p, one.text))
    assert status == 409 and got["error"] == "proposal_replaced" and newer.proposal_id in got["message"], got
    assert counts(one.store) == before
    assert act(one, newer, "commit", commit_body(one, newer, one.text, outbox_id="ob-tap00002"))[0] == 200


def test_two_devices_settle_the_card_once_and_a_resend_answers_the_first(one):
    """Failure: a second drop or revision from a second tap."""
    ok = commit_body(one, one.p, one.text)
    status, first = act(one, one.p, "commit", ok)
    assert status == 200
    status, again = act(one, one.p, "commit", ok)
    assert status == 200 and again["created"] is False and again["drop_id"] == first["drop_id"] \
        and again["draft_id"] == first["draft_id"]
    status, other = act(one, one.p, "commit", commit_body(one, one.p, one.text, outbox_id="ob-tap00002"))
    assert status == 409 and other["error"] == "proposal_settled"
    assert counts(one.store)["spec_card_taps"] == 1


@pytest.mark.parametrize("words", [
    json.dumps({"stop": "2h", "where": [{"repo": "/r‮", "paths": ["a"]}]}),          # escaped U+202E in a value
    '{"stop": "2h", "where": [{"repo": "/r‮", "paths": ["a"]}]}',                    # the literal character
    '{"\\u202estop": "2h"}',                                                              # escaped, in a key
])
def test_bidi_is_refused_at_preview_and_at_the_tap_escaped_or_literal(one, words):
    """Failure (M5): an escaped override passes an ASCII check; the check runs on what he POSTS, at both doors."""
    before = counts(one.store)
    assert act(one, one.p, "preview", {"words": words})[1].get("error") == "spec_refused"
    status, got = act(one, one.p, "commit", commit_body(one, one.p, words))
    assert status in (400, 409) and got["error"] in ("spec_refused", "bad_words"), got
    assert counts(one.store) == before and cards(one)[0]["state"] == "open"


def test_words_over_the_cap_are_a_400_at_the_tap_and_the_preview(one):
    big = "x" * (room.WORDS_MAX + 1)
    assert act(one, one.p, "preview", {"words": big})[0] == 400
    assert act(one, one.p, "commit", commit_body(one, one.p, big, input_sha256="sha256:0"))[0] == 400


def test_a_thread_that_closed_since_the_card_refuses_the_tap_by_name(one):
    one.store.set_state(one.thread, "closed", by="operator")
    before = counts(one.store)
    status, got = act(one, one.p, "commit", commit_body(one, one.p, one.text))
    assert status == 409 and got["error"] == "thread_closed", got
    assert counts(one.store) == before


def test_dismiss_on_an_envelope_card_is_unchanged(one):
    status, got = act(one, one.p, "dismiss", {})
    assert status == 200 and got["state"] == "dismissed"
    assert act(one, one.p, "commit", commit_body(one, one.p, one.text))[1]["error"] == "proposal_settled"


def test_the_route_knows_preview():
    assert cockpit_room.post_route("/cockpit/room/proposal/pr-" + "0" * 32 + "/preview") == ("preview", "pr-" + "0" * 32)


# --- the page (string-level: headless Chrome clamps width, intention-1066; the glass is Phill's) ----------------------------
def _fn(name, nxt):
    return JS[JS.index("function " + name):JS.index("function " + nxt)]


def test_the_page_draws_an_envelope_card_quotes_first_summary_last_and_never_calls_the_prefill_his_words():
    env = _fn("renderEnvelopeCard", "renderRoom")
    assert env.index("You said") < env.index("For thread") < env.index("Drafted by the room") < env.index("rv-reading") \
        < env.index("rv-summary")
    assert "Edit freely" in env and "The words to drop" not in env
    assert "Draft it" in env and "Not now" in env
    plain = _fn("renderCard", "renderEnvelopeCard")
    assert 'c.kind === "envelope"' in plain and "Nothing is sent until you tap" in plain   # the plain card is untouched
    assert "Drop these words" in plain


def test_the_page_enables_the_tap_only_on_the_reading_of_the_bytes_in_the_field():
    env = _fn("renderEnvelopeCard", "renderRoom")
    assert "onPreview" in env and "r.json.input_sha256" in env
    assert "ta.value !== shown.words" in env and "ta.value !== words" in env      # a reading is for exactly these bytes
    assert env.index("go.disabled = true") < env.index("hooks.onPreview")
    mount = JS[JS.index("function mount"):]
    assert '"/preview"' in mount
    assert "sent.thread_id = echo.thread_id" in mount and "sent.input_sha256 = echo.input_sha256" in mount
    assert "thread_id: c.thread" in env and "input_sha256: shown.sha" in env


def test_the_page_says_a_signed_grant_keeps_running_and_marks_widening_and_replacement():
    env = _fn("renderEnvelopeCard", "renderRoom")
    assert "What you signed keeps running until you sign the new one" in env
    assert "(more than now)" in env and "replaced by the newer card" in env
    assert "/sign/drop#thread=" in env and "review and sign" in env


@needs_browser
def test_in_a_browser_the_tap_waits_for_the_reading_of_the_edited_bytes_then_drafts(paged, tmp_path):
    """The card on the real page: a reading on screen, the tap disabled the moment the field is edited and enabled only
    when the reading of THOSE bytes returns; the tap writes the edited bytes and the page turns to 'Drafted'."""
    _d, t = plain_work(paged.ctx)
    paged.thread = t.thread_id
    paged.text = spec_text(paged.ctx)
    paged.p = propose(paged, t.thread_id, paged.text)
    edited = spec_text(paged.ctx, budget={**BUDGET, "hours": 1})
    out = _drive(tmp_path, paged.base + "/rvc.html", [
        {"until": "document.querySelector('.rv-card .rv-readline') && !document.querySelector('.rv-commit').disabled"},
        {"eval": "JSON.stringify({quote: document.querySelectorAll('.rv-card .rv-quote')[0].textContent,"
                 " label: document.querySelector('.rv-summary-label').textContent, go: document.querySelector('.rv-commit').textContent})"},
        {"eval": "(() => { const ta = document.querySelector('.rv-cardwords'); ta.value = %s; ta.dispatchEvent(new Event('input')); "
                 "return document.querySelector('.rv-commit').disabled; })()" % json.dumps(edited)},
        {"until": "!document.querySelector('.rv-commit').disabled"},
        {"eval": "document.querySelector('.rv-commit').click(); 1"},
        {"until": "document.querySelector('.rv-card-committed')"},
        {"eval": "document.querySelector('.rv-card-committed').textContent"},
    ])
    first = json.loads(out["values"][1])
    assert first == {"quote": "give it two hours", "label": "The room's summary (information, not your words)", "go": "Draft it"}
    assert out["values"][2] is True                       # disabled the instant the field changed
    assert "Drafted" in out["values"][-1]
    c = sqlite3.connect(paged.store.path)
    try:
        assert c.execute("SELECT words FROM drops WHERE surface = 'cockpit'").fetchall() == [(edited,)]
    finally:
        c.close()


# --- L3 round 1 (h14c) ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("words", [
    json.dumps({"pushes": [1], "stop": "2h"}),                         # crashed render (TypeError in the effect text)
    json.dumps({"budget": {"hours": True}, "stop": "2h"}),
    "[" * 9_000 + "]" * 9_000,                                         # deeper than the decoder recurses, under the cap
])
def test_preview_of_a_value_the_writer_refuses_is_named_not_a_fault(one, words):
    """codex L3 r1 MED/LOW: a preview that crashed (500) instead of naming the refusal a tap would give."""
    status, got = act(one, one.p, "preview", {"words": words})
    assert status == 409 and got["error"] == "spec_refused", (status, got)


def test_a_grammar_refusal_is_named_by_its_type_not_by_words_it_echoes(one):
    """complement L3 r1 LOW: a repeated key spelled like a store sentence ("x is already y") was routed as a settled
    proposal, because the refusal was picked by matching the sentence."""
    words = '{"stop": "2h", "x is already y": 1, "x is already y": 2}'
    status, got = act(one, one.p, "preview", {"words": words})
    assert status == 409 and got["error"] == "spec_refused", got
    status, got = act(one, one.p, "commit", commit_body(one, one.p, words))
    assert status == 409 and got["error"] == "spec_refused", got


def test_a_resend_naming_another_thread_is_refused_and_a_resend_answers_the_stored_thread(one):
    """codex L3 r1 LOW: a resend answered 200 with whatever thread the request named."""
    ok = commit_body(one, one.p, one.text)
    assert act(one, one.p, "commit", ok)[0] == 200
    status, got = act(one, one.p, "commit", {**ok, "thread_id": "th-" + "1" * 32})
    assert status == 409 and got["error"] == "thread_mismatch", got
    status, got = act(one, one.p, "commit", ok)
    assert status == 200 and got["created"] is False and got["thread_id"] == one.thread


def test_the_page_locks_the_field_while_a_tap_is_in_flight_and_reads_no_dead_card():
    """complement L3 r1 LOW: an edit during the tap re-enabled 'Draft it' and a second tap showed a spurious 'Not drafted';
    deepseek L3 r1 LOW: a replaced or expired card still asked the server for a reading it could never use."""
    env = _fn("renderEnvelopeCard", "renderRoom")
    assert "pending" in env and "ta.readOnly = true" in env
    assert env.index("if (pending) return") < env.index("go.disabled = dead || replaced")
    assert "if (dead || replaced)" in env
    mount = JS[JS.index("function mount"):]
    assert "echo.settled" in mount


# --- H1-5 lane 3: a MISSION card on the cockpit ------------------------------------------------------------------------
from tests.test_spec_card_mission import family  # noqa: E402
from tests.test_spec_card_mission import propose as mission_propose  # noqa: E402

def test_the_read_of_a_mission_card_names_every_child_by_title_and_no_budget_of_its_own(served):
    """Failure: a mission card the page could only draw as ONE thread (the anchor), hiding the other children. And (fix 3,
    L1 F2 + L2 F7) its ``budget_line`` was read from the ROOM's stored spec, beside a field he may have edited and after
    a tap over other bytes: the card now carries no budget; the reading of the bytes (preview) and the sign sheet do."""
    _d, ts = family(served.ctx)
    p = mission_propose(served, [t.thread_id for t in ts], spec_text(served.ctx))
    [c] = cards(served)
    assert c["proposal_id"] == p.proposal_id and c["kind"] == "envelope" and c["thread_id"] == ts[0].thread_id
    assert c["threads"] == [{"thread_id": t.thread_id, "title": t.title, "ended": False} for t in ts] and len(ts) == 3
    assert all(t.title for t in ts)
    assert "budget_line" not in c and c["mission_id"] is None and c["grant"] is None


def test_a_thread_card_read_carries_no_mission_key(one):
    """A thread card's read is what it was: none of the mission's keys."""
    [c] = cards(one)
    assert not {"threads", "budget_line", "mission_id", "grant"} & set(c)


def _mission_tap(served):
    _d, ts = family(served.ctx)
    text = spec_text(served.ctx)
    p = mission_propose(served, [t.thread_id for t in ts], text)
    body = {"words": text, "outbox_id": "ob-tap00001", "thread_id": ts[0].thread_id, "input_sha256": words_sha256(text)}
    status, got = act(served, p, "commit", body)
    assert status == 200, got
    return ts, p, got


def test_a_tapped_mission_card_names_the_mission_his_tap_made(served):
    _ts, _p, _got = _mission_tap(served)
    [c] = cards(served)
    conn = sqlite3.connect(served.store.path)
    try:
        (mission,) = conn.execute("SELECT mission_id FROM missions").fetchone()
    finally:
        conn.close()
    assert c["state"] == "committed" and c["mission_id"] == mission and len(c["threads"]) == 3
    assert c["grant"] == "unsigned" and "budget_line" not in c


def test_the_committed_card_reads_the_grant_as_it_stands_signed_then_after_a_detach(served, monkeypatch):
    """Failure (fix 3, L2 F4 + F6, L1 F6): the committed card said "Mission ms-... covers 3 threads" under "Nothing is
    signed yet", and kept saying it after a detach killed a child's grant; nothing read the draft or the kills."""
    from levain.desk import envelope_drafts as drafts
    from levain.desk import missions

    monkeypatch.setattr(cockpit_room, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: served.ctx.clk[0])}))
    ts, _p, got = _mission_tap(served)
    signed, _since, _against = built(served.ctx, drafts.latest(served.store, got["draft_id"]))
    served.ctx.accept(signed)
    [c] = cards(served)
    assert c["grant"] == "signed" and [t["ended"] for t in c["threads"]] == [False, False, False]
    missions.detach(served.store, c["mission_id"], ts[1].thread_id)
    [c] = cards(served)
    assert c["grant"] == "signed" and [t["ended"] for t in c["threads"]] == [False, True, False]


def test_a_withdrawn_mission_card_says_withdrawn(served):
    from levain.desk import envelope_drafts as drafts

    _ts, _p, got = _mission_tap(served)
    drafts.withdraw(served.store, got["draft_id"], by="operator")
    [c] = cards(served)
    assert c["grant"] == "withdrawn"


def test_the_preview_route_of_a_mission_card_reads_every_child_and_the_budget(served):
    """Failure (fix 3, L1 F1, reproduced on d2d74713a): the page's preview route called ``spec_card.preview`` with no
    ``threads``, so the mission lines (every child, one signature, the budget per thread) never reached him."""
    from levain.desk import spec_card

    _d, ts = family(served.ctx)
    text = spec_text(served.ctx)
    p = mission_propose(served, [t.thread_id for t in ts], text)
    status, got = act(served, p, "preview", {"words": text})
    assert status == 200, got
    fields = {line["field"]: line["text"] for line in got["lines"]}
    assert {"mission", "budget_together", "mission_later"} <= set(fields), got["lines"]
    assert all(t.thread_id in fields["mission"] for t in ts)
    assert fields["budget_together"] == spec_card.budget_words(BUDGET, 3)
    # the reading follows the bytes he edited, never the room's stored spec
    edited = spec_text(served.ctx, budget={**BUDGET, "tool_calls": 7})
    _s, got = act(served, p, "preview", {"words": edited})
    assert "7 tool calls" in {line["field"]: line["text"] for line in got["lines"]}["budget_together"]


def test_a_tap_refused_because_a_child_joined_another_mission_is_not_called_settled(served):
    """Failure (fix 3, L1 F7, reproduced on d2d74713a): "thread ... is already in mission ..." matched the sentence test
    for a settled card, so the page was told ``proposal_settled`` about a card that is still open."""
    from levain.desk import missions

    _d, ts = family(served.ctx)
    text = spec_text(served.ctx)
    p = mission_propose(served, [t.thread_id for t in ts], text)
    missions.create_mission(served.store, [ts[1].thread_id])
    body = {"words": text, "outbox_id": "ob-tap00001", "thread_id": ts[0].thread_id, "input_sha256": words_sha256(text)}
    status, got = act(served, p, "commit", body)
    assert status == 409 and got["error"] == "in_mission", got


def test_the_page_draws_a_mission_card_from_its_children_and_adds_no_sign_control():
    env = _fn("renderEnvelopeCard", "renderRoom")
    assert "c.children" in env or "kids" in env
    assert "budgetLine" not in env and "not covered" in env
    assert " covers " not in env                         # no present-tense cover claim on a card (fix 3, L2 F6)
    assert "Sign" not in env.replace("signed", "").replace("signing", "").replace("sign.", "")    # the card has no Sign control
    assert env.count('"Draft it"') == 1


def test_before_the_tap_the_card_says_the_tap_makes_the_mission_and_holds_the_threads():
    """Failure (fix 3, L2 F5): the pre-tap card said only "nothing is signed until you tap"; that the tap also makes the
    mission and that none of the threads can then hold a draft of its own was said only on the sign sheet, after it."""
    env = _fn("renderEnvelopeCard", "renderRoom")
    pre = env[env.index("var replaced"):]
    assert "also makes these" in pre and "one mission" in pre
    assert "none of them can hold a draft of its own" in pre


@needs_browser
def test_in_a_browser_a_mission_card_lists_every_child_and_the_budget_with_no_sign_control(paged, tmp_path):
    """The card on the real page: every child's title, the budget line and the not-covered line, one 'Draft it' that is a
    draft and no Sign button, before the tap; after it, the mission and a link per child."""
    _d, ts = family(paged.ctx)
    text = spec_text(paged.ctx)
    mission_propose(paged, [t.thread_id for t in ts], text)
    out = _drive(tmp_path, paged.base + "/rvc.html", [
        {"until": "document.querySelector('.rv-card .rv-readline') && !document.querySelector('.rv-commit').disabled"},
        {"eval": "document.querySelector('.rv-card').textContent"},
        {"eval": "[...document.querySelectorAll('.rv-card button')].map(b => b.textContent).join('|')"},
        {"eval": "document.querySelector('.rv-commit').click(); 1"},
        {"until": "document.querySelector('.rv-card-committed')"},
        {"eval": "document.querySelector('.rv-card-committed').textContent"},
        {"eval": "[...document.querySelectorAll('.rv-card-committed a')].map(a => a.getAttribute('href')).join('|')"},
        {"eval": "document.querySelectorAll('.rv-card-committed button').length"},
    ])
    first, buttons, committed, links, nbuttons = out["values"][1], out["values"][2], out["values"][5], out["values"][6], out["values"][7]
    for t in ts:
        assert t.title in first and t.thread_id in first and t.title in committed
    assert "one signature covers these 3 threads" in first and "under one signature" in first
    # the budget is the reading of the bytes (the preview route, now carrying the threads), in the sheet's words
    assert "counted for each thread on its own" in first and "dollars are not counted" in first
    assert "$15.00" not in first and "not covered" in first
    assert "also makes these 3 threads one mission" in first and "none of them can hold a draft of its own" in first
    assert buttons == "Draft it|Not now"
    assert "Mission ms-" in committed and "would cover exactly these 3 threads" in committed
    assert "Nothing is signed yet" in committed and " covers " not in committed and "$" not in committed
    assert links.split("|") == ["/sign/drop#thread=" + t.thread_id for t in ts] and nbuttons == 0
