"""The work thread's authority header (one server-made line drawn from the envelope records), read through the real
``make_server`` envelope beside a ChatService over a ScriptedSession. The ambiguous-send reconcile (``mark``) is the
room's now: ``tests/test_cockpit_room.py``."""
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
from levain.coordinator import envelope as env
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


def test_a_drop_effect_attenuation_leaves_the_effects_named_line_counting_what_stands(amb, qdir):
    # L3_chatstack_r1 codex-7: the line shares the effective body's rule (EnvelopeStore._effective), not the signed count
    _svc, _cs, world, _store, _m = amb
    body = json.dumps({"budget": {"tool_calls": 1}, "lands": [{"repo": "/a"}, {"repo": "/b"}], "pushes": [],
                       "publishes": [], "stop_at": S18})
    _bind(qdir, world["thread"], body=body)
    assert "effects named: 2 land, 0 push, 0 publish" in _header(qdir, world["thread"])["line"]
    _attenuate(qdir, "drop_effect", env.effect_identity("lands", {"repo": "/a"}))
    assert "effects named: 1 land, 0 push, 0 publish" in _header(qdir, world["thread"])["line"]
    _attenuate(qdir, "drop_effect", env.effect_identity("lands", {"repo": "/b"}))
    assert "effects named: 0 land, 0 push, 0 publish" in _header(qdir, world["thread"])["line"]


def test_a_drop_effect_naming_no_held_effect_changes_nothing(amb, qdir):
    _svc, _cs, world, _store, _m = amb
    _bind(qdir, world["thread"])
    _attenuate(qdir, "drop_effect", env.effect_identity("pushes", {"repo": "/elsewhere"}))
    assert "effects named: 1 land, 0 push, 0 publish" in _header(qdir, world["thread"])["line"]


_BOOT = ('window.__tv = LevainThreadView.mount(document.getElementById("host"),'
         ' {pollMs: 0, headers: () => ({"X-Levain-Write-Token": "%s"})});' % TOKEN)


@needs_browser
def test_the_page_draws_the_authority_line_and_no_reconcile_buttons(tmp_path, amb, qdir):
    svc, cs, world, _store, message = amb
    _bind(qdir, world["thread"], stop_at=utc_stamp(datetime.now(timezone.utc) + timedelta(days=1)))   # the page reads the wall clock
    _root, src, _ids = _install(tmp_path)
    extra = {"/tvc.html": ("text/html; charset=utf-8", _PAGE.encode()),
             "/tvc.js": ("text/javascript; charset=utf-8", _BOOT.encode())}
    with _serve(src, write_token=TOKEN, chat_service=svc, extra_assets=extra) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-authority')"},
            {"eval": "document.querySelector('.tv-authority').textContent"},
            {"eval": "document.querySelectorAll('.tv-mark, .tv-mark-yes, .tv-mark-no').length"},
        ])
    assert out["values"][3].startswith("Standing authority: envelope ev-aaaaaaaa") and "Not in force yet" in out["values"][3]
    assert out["values"][4] == 0                    # reconciling an ambiguous send is the room's, not a work thread's
