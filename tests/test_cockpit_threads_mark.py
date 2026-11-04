"""CH-U1b slice 2: the authority header (one server-made line drawn from the envelope records) and the ambiguous-send
reconcile route (``mark``), through the real ``make_server`` envelope beside a ChatService over a ScriptedSession."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.cockpit import threads
from levain.cockpit.consent import utc_stamp
from levain.coordinator.chat import ChatStore, next_gseq
from levain.coordinator.driver import ScriptedSession
from levain.coordinator.envelopes import NOT_IN_FORCE, EnvelopeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads import clock  # noqa: F401  (fixture)
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_cockpit_threads_send import _PAGE, _call
from levain.coordinator.store import Part
from tests.test_coordinator_chat_send import T0, _world, ready, send

EID = "ev-" + "a" * 32
S12, S1230, S13, S18 = (utc_stamp(T0 + timedelta(hours=h)) for h in (0, 0.5, 1, 6))


@pytest.fixture
def amb(qdir, clock):
    """A running worker launch whose one send came back ambiguous."""
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    session = ScriptedSession(sends=("ambiguous",))
    svc = ready(cs, world, session)
    sent = send(svc, cs, world, "go")
    assert sent.state == "ambiguous"
    return svc, cs, world, store, sent.message_id


def _bind(qdir, thread, *, budget=None, stop_at=S18, lands=1, body=None):
    """One envelope row bound to ``thread``, written the way the record holds it (the signature path is S1's, not read)."""
    raw = body if body is not None else json.dumps({
        "budget": {"tool_calls": 200, "tokens": 5_000_000, "dollars_cents": 1050, "hours": 4} if budget is None else budget,
        "lands": [{"repo": "/r"}] * lands, "pushes": [], "publishes": [], "stop_at": stop_at})
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys = ON")
        drop = c.execute("SELECT drop_id FROM drops LIMIT 1").fetchone()[0]
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO envelopes (envelope_id, gseq, origin, drop_id, body, digest, payload, signature, signer_fp, "
                  "signer_class, supersedes, stop_at, accepted_at) VALUES (?, ?, 'worded', ?, ?, 'd', '{}', 's', 'fp', "
                  "'passkey', NULL, ?, ?)", (EID, next_gseq(c, S12), drop, raw, stop_at,
                                              S12))
        c.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES (?, ?, 1, ?, ?)",
                  (EID, thread, next_gseq(c, S12), S12))
        c.execute("COMMIT")
    finally:
        c.close()


def _attenuate(qdir, kind, value="", by="operator", thread=None):
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) VALUES "
                  "(?, ?, ?, ?, ?, ?, ?)", (EID, thread, kind, value, by, next_gseq(c, S12),
                                            S1230))
        c.execute("COMMIT")
    finally:
        c.close()


def _header(qdir, thread, now=T0):
    c = threads._open(qdir)
    try:
        return threads.authority_header(c, thread, now)
    finally:
        c.close()


# --- the authority header ----------------------------------------------------------------------------------------------

def test_no_bound_envelope_reads_no_standing_authority(amb, qdir):
    _svc, _cs, world, _store, _m = amb
    got = _header(qdir, world["thread"])
    assert got["state"] == "none" and got["envelope_id"] is None and got["line"].startswith("No standing authority")
    assert threads.thread_stream(qdir, world["thread"])["authority"] == got


def test_a_bound_envelope_is_one_line_of_its_caps_effects_and_stop_and_says_it_is_not_in_force(amb, qdir):
    _svc, _cs, world, _store, _m = amb
    _bind(qdir, world["thread"])
    got = _header(qdir, world["thread"])
    assert got["state"] == "bound" and got["envelope_id"] == EID and got["in_force"] is False
    assert got["line"] == ("Standing authority: envelope ev-aaaaaaaa, up to 200 tool calls, 5,000,000 tokens, 4 hours, "
                           "$10.50; effects named: 1 land, 0 push, 0 publish; stops 2026-10-23 18:00 UTC. "
                           "Not in force yet: every land, push and publish still asks you separately.")
    assert "\n" not in got["line"]
    assert threads.thread_stream(qdir, world["thread"])["authority"]["state"] == "expired"     # the stream reads the wall clock


def test_an_earlier_stop_narrows_a_kill_ends_it_and_a_passed_stop_is_expired(amb, qdir):
    _svc, _cs, world, _store, _m = amb
    _bind(qdir, world["thread"])
    _attenuate(qdir, "stop_earlier", S13)
    got = _header(qdir, world["thread"])
    assert got["state"] == "bound" and got["stop_at"] == S13 and "13:00 UTC" in got["line"]
    assert _header(qdir, world["thread"], T0 + timedelta(hours=2))["state"] == "expired"
    _attenuate(qdir, "kill")
    got = _header(qdir, world["thread"])
    assert got["state"] == "ended" and got["line"].startswith("No standing authority") and "killed" in got["line"]


def test_a_record_this_reader_cannot_read_fails_closed_never_as_none(amb, qdir):
    _svc, _cs, world, _store, _m = amb
    _bind(qdir, world["thread"], body="{not json")
    got = _header(qdir, world["thread"])
    assert got["state"] == "unreadable" and "none" not in got["line"].lower().split("standing")[0]
    assert got["line"] == "The standing authority for this thread could not be read."


@pytest.mark.parametrize("body", [
    {"budget": {"tool_calls": "many"}, "lands": [], "pushes": [], "publishes": []},
    {"budget": {"tool_calls": -1}, "lands": [], "pushes": [], "publishes": []},
    {"budget": {"tool_calls": True}, "lands": [], "pushes": [], "publishes": []},
    {"lands": [], "pushes": [], "publishes": []},
    {"budget": {}, "lands": "x", "pushes": [], "publishes": []},
])
def test_a_malformed_body_is_unreadable(amb, qdir, body):
    _svc, _cs, world, _store, _m = amb
    _bind(qdir, world["thread"], body=json.dumps(body))
    assert _header(qdir, world["thread"])["state"] == "unreadable"


def test_a_store_with_no_envelope_tables_has_no_standing_authority(tmp_path):
    c = sqlite3.connect(":memory:")
    assert threads.authority_header(c, "th-" + "0" * 32)["state"] == "none"


def test_the_not_in_force_premise_is_pinned_so_the_day_s1_lands_this_line_is_revisited(amb):
    _svc, _cs, world, store, _m = amb
    assert threads.IN_FORCE is False
    assert store.authority_refusal(EID, world["thread"]) == NOT_IN_FORCE


# --- the mark route ----------------------------------------------------------------------------------------------------

def _mark(world) -> str:
    return f"/cockpit/thread/{world['thread']}/mark"


def _states(cs, message_id):
    return cs._read(lambda c: [r[0] for r in c.execute(
        "SELECT state FROM chat_outbox_events WHERE message_id = ? ORDER BY seq", (message_id,))])


def test_marking_delivered_or_not_resolves_the_ambiguous_send_by_the_operator_under_the_credentials_owner(tmp_path, amb):
    svc, cs, world, _store, message = amb
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        # hostile body: it names an owner, an author, a state and a launch; none is read
        status, body = _call(base, _mark(world), {
            "message_id": message, "delivered": True, "owner": "phone:attacker", "by": "service", "state": "refused",
            "author_kind": "coordinator", "launch_id": "ln-" + "0" * 32, "cause": "marked by someone else"})
    assert status == 200 and body == {"message_id": message, "state": "accepted"}, body
    assert _states(cs, message)[-1] == "accepted"
    cause, by = cs._read(lambda c: c.execute(
        "SELECT cause, by FROM chat_outbox_events WHERE message_id = ? ORDER BY seq DESC LIMIT 1", (message,)).fetchone())
    assert by == "operator" and cause == "marked by cockpit:browser"


def test_marking_not_delivered(tmp_path, amb):
    svc, cs, world, _store, message = amb
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _mark(world), {"message_id": message, "delivered": False})
    assert status == 200 and body["state"] == "not_delivered" and _states(cs, message)[-1] == "not_delivered"


def test_the_same_gate_as_send_and_stop_no_token_or_a_wrong_one_writes_nothing(tmp_path, amb):
    svc, cs, world, _store, message = amb
    before = _states(cs, message)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        for token in (None, "wrong-token"):
            status, _b = _call(base, _mark(world), {"message_id": message, "delivered": True}, token)
            assert status == 403
        status, _b = _call(base, _mark(world), {"message_id": message, "delivered": True}, TOKEN, ctype="text/plain")
        assert status in (400, 403, 415)
    assert _states(cs, message) == before


def test_the_route_is_absent_without_a_chat_service(tmp_path, amb):
    _svc, _cs, world, _store, message = amb
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _call(base, _mark(world), {"message_id": message, "delivered": True})
    assert status == 404 and body["error"] == "not_found"


@pytest.mark.parametrize("req", [
    {"delivered": True}, {"message_id": "cm-" + "0" * 32}, {"message_id": 7, "delivered": True},
    {"message_id": "not-a-message", "delivered": True}, [1],
])
def test_a_malformed_mark_is_a_400(tmp_path, amb, req):
    svc, cs, world, _store, message = amb
    before = _states(cs, message)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _mark(world), req)
    assert status == 400 and body["error"] == "bad_request" and _states(cs, message) == before


@pytest.mark.parametrize("delivered", ["true", "false", 1, 0, None, "yes", [], {}])
def test_delivered_is_a_json_boolean_and_nothing_else(tmp_path, amb, delivered):
    svc, cs, world, _store, message = amb
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _mark(world), {"message_id": message, "delivered": delivered})
    assert status == 400 and _states(cs, message)[-1] == "ambiguous"


def test_an_unknown_message_or_thread_is_404_and_a_message_resolved_already_is_a_409(tmp_path, amb):
    svc, cs, world, _store, message = amb
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, _b = _call(base, _mark(world), {"message_id": "cm-" + "0" * 32, "delivered": True})
        assert status == 404
        status, _b = _call(base, "/cockpit/thread/th-" + "0" * 32 + "/mark", {"message_id": message, "delivered": True})
        assert status == 404
        assert _call(base, _mark(world), {"message_id": message, "delivered": False})[0] == 200
        status, body = _call(base, _mark(world), {"message_id": message, "delivered": True})
        assert status == 409 and body["error"] == "not_ambiguous"
    assert _states(cs, message)[-1] == "not_delivered"           # the second mark wrote nothing


def test_a_message_that_is_not_ambiguous_is_not_markable(tmp_path, qdir, clock):
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    svc = ready(cs, world, ScriptedSession())
    sent = send(svc, cs, world, "go")
    assert sent.state == "accepted"
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _mark(world), {"message_id": sent.message_id, "delivered": False})
    assert status == 409 and body["error"] == "not_ambiguous" and _states(cs, sent.message_id)[-1] == "accepted"


def test_a_message_of_another_thread_is_not_markable_through_this_one(tmp_path, amb):
    svc, cs, world, store, message = amb
    other = store.split(store.accept_drop("second job", surface="cli")[0].drop_id,
                        [Part("work", "second")])[0]
    token = store.coord_take()
    store.launch_request(token, harness="claude", role="worker", threads=[other.thread_id])
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, _b = _call(base, f"/cockpit/thread/{other.thread_id}/mark", {"message_id": message, "delivered": True})
    assert status == 404 and _states(cs, message)[-1] == "ambiguous"


def test_the_composer_read_still_names_the_unresolved_send(tmp_path, amb):
    svc, _cs, world, _store, message = amb
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, got = _call(base, f"/cockpit/thread/{world['thread']}.json", None, method="GET")
        assert status == 200 and got["composer"]["unresolved"] == [message]
        assert got["authority"]["state"] == "none"
        _call(base, _mark(world), {"message_id": message, "delivered": True})
        status, got = _call(base, f"/cockpit/thread/{world['thread']}.json", None, method="GET")
        assert got["composer"]["unresolved"] == []


_BOOT = ('window.__tv = LevainThreadView.mount(document.getElementById("host"),'
         ' {pollMs: 0, compose: true, headers: () => ({"X-Levain-Write-Token": "%s"})});' % TOKEN)


@needs_browser
def test_the_page_draws_the_authority_line_and_the_two_reconcile_buttons_and_marks(tmp_path, amb, qdir):
    svc, cs, world, _store, message = amb
    _bind(qdir, world["thread"], stop_at=utc_stamp(datetime.now(timezone.utc) + timedelta(days=1)))   # the page reads the wall clock
    _root, src, _ids = _install(tmp_path)
    extra = {"/tvc.html": ("text/html; charset=utf-8", _PAGE.encode()),
             "/tvc.js": ("text/javascript; charset=utf-8", _BOOT.encode())}
    with _serve(src, write_token=TOKEN, thread_chat=svc, extra_assets=extra) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-mark-yes')"},
            {"eval": "document.querySelector('.tv-authority').textContent"},
            {"eval": "document.querySelectorAll('.tv-mark button').length"},
            {"eval": "document.querySelector('.tv-mark-no').click(); 1"},
            {"until": "!document.querySelector('.tv-mark-yes')"},
        ])
    assert out["values"][3].startswith("Standing authority: envelope ev-aaaaaaaa") and "Not in force yet" in out["values"][3]
    assert out["values"][4] == 2
    assert _states(cs, message)[-1] == "not_delivered"
