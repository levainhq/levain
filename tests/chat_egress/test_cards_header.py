"""CH-D2 S7 header fix: the decisions listing serves the egress card's state word, and the decided row's header draws it.

The decisions panel (the real `Cockpit` over the real `_ConsentReader`) is read over a real consent queue and a real
dispatch store holding real egress rows. Before the fix a completed or ambiguous card's row carried only the queue's
state (allowed), so its header read ALLOWED until its details opened. The page draw of that word is the headless test
in `test_cards_states_page.py` (ruling 5's header assertion).
"""

from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

from levain.chat_egress import records as rec, wire
from levain.chat_egress.cards import file_card
from levain.chat_egress.lifecycle import Verdict
from levain.cockpit import Cockpit
from levain.consent.queue import ConsentQueue
from levain.coordinator.store import CoordStore
from levain.web_server import _ConsentReader
from tests.chat_egress.test_cards_states_page import _card, _launch
from tests.chat_egress.test_records import GET
from tests.test_consent_queue import Key, Log, signed
from tests._h4 import h4_home

needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen signs the answers")


def _setup(tmp_path, monkeypatch):
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    consent = home / "consent"
    consent.mkdir(parents=True, mode=0o700)
    keydir = tmp_path / "keys"
    keydir.mkdir()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": Key(keydir, "laptop")})
    log.enrol("laptop", "laptop-presence")
    return consent, log, ConsentQueue(consent, authority=log.source, home=h4_home())


@needs_ssh
def test_the_listing_serves_the_cards_state_for_egress_rows_only(tmp_path, monkeypatch):
    consent, log, q = _setup(tmp_path, monkeypatch)
    store = CoordStore(consent)
    records = rec.EgressRecords(store)
    live, stopped = _launch(store, live=True), _launch(store, live=False)
    rows = {}
    for name, lid in (("completed", live), ("ambiguous", stopped), ("in_flight", live)):
        c = _card(lid, name.replace("_", "-") + ".example")
        r = file_card(q, c, agent_session_id="s", cwd="/w")
        payload, sig = signed(q, r, log.keys["laptop"], "allow")
        q.answer(r.id, payload, sig)
        records.write_attempt(lid, c["request"], Verdict.ALLOW, GET)
        rows[name] = (c, r)
    records.write_outcome(live, wire.ObsResponse(rows["completed"][0]["request"], 201, None, None), rec.CT_ABSENT)
    other = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"},
                   cwd="/w", hold=timedelta(minutes=5), event_id="e")
    ck = Cockpit()
    ck.attach_consent_rows(_ConsentReader(consent))
    by_id = {r["id"]: r["facets"] for r in ck.panel("decisions", credential_class="token")["rows"]}
    got = {n: by_id[f"consent:{r.id}"].get("egress_state") for n, (_, r) in rows.items()}
    assert got == {"completed": "completed", "ambiguous": "ambiguous", "in_flight": "in_flight"}, got
    assert {n: by_id[f"consent:{r.id}"]["consent_state"] for n, (_, r) in rows.items()} == {
        "completed": "allowed", "ambiguous": "allowed", "in_flight": "allowed"}   # the queue word stays its own facet
    assert "egress_state" not in by_id[f"consent:{other.id}"]                      # another tool's row is as it was
    one = ck.read_one("decisions", f"consent:{rows['completed'][1].id}").value
    assert one["facets"]["egress_state"] == "completed"                            # the single-row read agrees


def _allowed_cards(q, log, store, n, *, decide=True):
    """`n` egress cards on one live launch, each filed (and, with `decide`, allowed with a recorded attempt)."""
    records = rec.EgressRecords(store)
    live = _launch(store, live=True)
    out = []
    for i in range(n):
        c = _card(live, f"c{i}.example")
        r = file_card(q, c, agent_session_id="s", cwd="/w")
        if decide:
            payload, sig = signed(q, r, log.keys["laptop"], "allow")
            q.answer(r.id, payload, sig)
            records.write_attempt(live, c["request"], Verdict.ALLOW, GET)
        out.append((c, r))
    return out


def _panel_facets(consent):
    ck = Cockpit()
    ck.attach_consent_rows(_ConsentReader(consent))
    return ck, {r["id"]: r["facets"] for r in ck.panel("decisions", credential_class="token")["rows"]}


@needs_ssh
def test_another_tools_entry_is_never_parsed_for_the_word(tmp_path, monkeypatch):
    # r1 ruling 1: the tool check comes before the body is read
    from datetime import datetime, timezone
    from levain.consent import listing

    consent, log, q = _setup(tmp_path, monkeypatch)
    other = q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"},
                   cwd="/w", hold=timedelta(minutes=5), event_id="e")
    entry = next(e for e in _ConsentReader(consent)(datetime.now(timezone.utc)).pending if e.request.id == other.id)

    def boom(_request):
        raise AssertionError("filed_input was read for a Bash entry")
    monkeypatch.setattr(listing, "filed_input", boom)
    assert listing.egress_state_of(entry, lambda a, b: (None, False)) is None


@needs_ssh
def test_an_egress_row_whose_body_cannot_be_read_says_unreadable(tmp_path, monkeypatch):
    from levain.consent import listing

    consent, log, q = _setup(tmp_path, monkeypatch)
    store = CoordStore(consent)
    (c, r), = _allowed_cards(q, log, store, 1)

    def boom(_request):
        raise ValueError("a display that does not parse")
    monkeypatch.setattr(listing, "filed_input", boom)
    _, by_id = _panel_facets(consent)
    assert by_id[f"consent:{r.id}"]["egress_state"] == "unreadable"


@needs_ssh
def test_a_store_that_cannot_be_read_is_unreadable_never_the_queue_word(tmp_path, monkeypatch):
    # r1 ruling 3: a lookup that was configured and failed is its own word
    consent, log, q = _setup(tmp_path, monkeypatch)
    store = CoordStore(consent)
    cards = _allowed_cards(q, log, store, 2)
    (consent / "dispatch.db").write_bytes(b"this is not a sqlite file" * 400)
    _, by_id = _panel_facets(consent)
    for _, r in cards:
        f = by_id[f"consent:{r.id}"]
        assert f["egress_state"] == "unreadable" and f["consent_state"] == "allowed", f


@needs_ssh
def test_the_first_store_failure_ends_the_lookups_for_that_build(tmp_path, monkeypatch):
    # r1 ruling 2: a locked store costs one failed read per build, not one per card
    consent, log, q = _setup(tmp_path, monkeypatch)
    store = CoordStore(consent)
    cards = _allowed_cards(q, log, store, 4)
    calls = []

    def locked(self, launch_id, request_id):
        calls.append(request_id)
        raise rec.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(rec.EgressReader, "reading", locked)
    _, by_id = _panel_facets(consent)
    assert len(calls) == 1, calls
    assert {by_id[f"consent:{r.id}"]["egress_state"] for _, r in cards} == {"unreadable"}


@needs_ssh
def test_a_pending_row_never_reads_the_store_and_read_one_builds_only_its_row(tmp_path, monkeypatch):
    # r1 rulings 4 and 5
    consent, log, q = _setup(tmp_path, monkeypatch)
    store = CoordStore(consent)
    pend = _allowed_cards(q, log, store, 1, decide=False)
    done = _allowed_cards(q, log, store, 3)
    real = rec.EgressReader.reading
    calls = []

    def counting(self, launch_id, request_id):
        calls.append(request_id)
        return real(self, launch_id, request_id)
    monkeypatch.setattr(rec.EgressReader, "reading", counting)
    ck, by_id = _panel_facets(consent)
    assert "egress_state" not in by_id[f"consent:{pend[0][1].id}"]
    assert sorted(calls) == sorted(c["request"] for c, _ in done), calls      # the three decided, never the pending
    calls.clear()
    one = ck.read_one("decisions", f"consent:{done[1][1].id}").value
    assert one["facets"]["egress_state"] == by_id[f"consent:{done[1][1].id}"]["egress_state"]
    assert calls == [done[1][0]["request"]], calls


def test_the_registry_enum_is_every_card_state_and_unreadable():
    from levain.chat_egress.cards import CardState
    from levain.cockpit.registry import FACETS

    check = FACETS["egress_state"][1]
    words = {s.value for s in CardState} | {"unreadable"}
    assert all(check(w) for w in words)
    assert not check("nonsense")
    assert not check("")
    assert not check("Unreadable")


def test_the_page_has_a_label_for_every_card_state_and_unreadable():
    # r1 ruling 6: every word the server can send has its own text in the header's table (the six reachable states
    # are drawn by the headless test in test_cards_states_page.py; this holds the rest to the table)
    import re
    from pathlib import Path
    from levain.chat_egress.cards import CardState

    js = (Path(__file__).resolve().parents[2] / "levain/templates/web/dashboard_core.js").read_text()
    table = js[js.index("const EGRESS_WORDS = {"):js.index("};", js.index("const EGRESS_WORDS = {"))]
    assert set(re.findall(r"^\s*([a-z_]+):", table, re.M)) == {s.value for s in CardState} | {"unreadable"}


@needs_ssh
def test_a_write_locked_dispatch_store_reads_unreadable_fast_for_the_panel(tmp_path, monkeypatch):
    # the store is WAL: a plain BEGIN IMMEDIATE / EXCLUSIVE never blocks a reader, so the holder takes the file whole
    # (locking_mode=EXCLUSIVE) as a connection that has it open would
    import sqlite3
    import time

    consent, log, q = _setup(tmp_path, monkeypatch)
    store = CoordStore(consent)
    (c, r), = _allowed_cards(q, log, store, 1)
    holder = sqlite3.connect(consent / "dispatch.db", isolation_level=None)
    holder.execute("PRAGMA locking_mode = EXCLUSIVE")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE IF NOT EXISTS lockprobe (a)")
    try:
        ck = Cockpit()
        ck.attach_consent_rows(_ConsentReader(consent))
        t0 = time.monotonic()
        by_id = {x["id"]: x["facets"] for x in ck.panel("decisions", credential_class="token")["rows"]}
        elapsed = time.monotonic() - t0
    finally:
        holder.close()
    assert by_id[f"consent:{r.id}"]["egress_state"] == "unreadable"
    assert elapsed < 1.0, elapsed
