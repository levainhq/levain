"""CH-D2 S7 header fix: the decisions listing serves the egress card's state word, and the decided row's header draws it.

The decisions panel (the real `Cockpit` over the real `_ConsentReader`) is read over a real consent queue and a real
dispatch store holding real egress rows. Before the fix a completed or ambiguous card's row carried only the queue's
state (allowed), so its header read ALLOWED until its details opened. The page draw of that word is the headless test
in `test_cards_states_page.py` (ruling 5's header assertion).
"""

from __future__ import annotations

import shutil

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
    return consent, log, ConsentQueue(consent, authority=log.source)


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
                   cwd="/w", hold=__import__("datetime").timedelta(minutes=5), event_id="e")
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
