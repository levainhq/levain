"""DISPATCH F5: the model a hook payload names is on the queue record, beside it and never in its signed bytes."""
from __future__ import annotations

import io
import json
import sqlite3

from levain.consent.adapters import codex as cx
from levain.consent.queue import MODEL_UNKNOWN, ConsentQueue, authority_source
from tests._h4 import h4_home


def test_codex_payload_model_is_recorded_beside_the_request_in_a_store_made_before_the_table(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    (q / cx.SETTINGS_NAME).write_text(json.dumps({"away_mode": True, "away_hold_seconds": 0}))
    queue = ConsentQueue(q, authority=authority_source(q / "cockpit.db", anchor=None), home=h4_home())
    first = queue.file(agent=cx.AGENT, agent_session_id="s", tool_name="Bash", tool_input={"command": "true"},
                       cwd=str(tmp_path), hold=cx.MIN_FILE_HOLD)
    # a store made before the table: drop it, as an older levain's store never had it
    db = sqlite3.connect(q / "queue.db")
    db.executescript("DROP TRIGGER IF EXISTS request_models_frozen; DROP TRIGGER IF EXISTS request_models_kept; "
                     "DROP TABLE IF EXISTS request_models;")
    db.close()
    event = {"hook_event_name": cx.EVENT, "session_id": "s", "cwd": str(tmp_path), "tool_name": "Bash",
             "tool_input": {"command": "git push"}, "model": "z-ai/glm-5.2", "permission_mode": "default"}
    out = io.StringIO()
    assert cx.main(["--max-hold", "0", "--queue-dir", str(q)], stdin=io.StringIO(json.dumps(event)), stdout=out) == 0
    reopened = ConsentQueue(q, authority=authority_source(q / "cockpit.db", anchor=None), home=h4_home())
    filed = [r[0] for r in sqlite3.connect(q / "queue.db").execute("SELECT id FROM requests ORDER BY rowid")]
    assert filed[0] == first.id and len(filed) == 2
    assert reopened.model(first.id) == {"requested": MODEL_UNKNOWN, "answered": MODEL_UNKNOWN, "source": "none"}
    assert reopened.model(filed[1]) == {"requested": "z-ai/glm-5.2", "answered": MODEL_UNKNOWN, "source": "hook"}
    # never in the sealed request or the receipt face
    got = reopened.get(filed[1])
    assert got is not None and "glm" not in json.dumps(got[0].fields())
    assert all("glm" not in json.dumps(r["face"]) for r in reopened.receipts(filed[1]))


import pytest


@pytest.mark.parametrize("raw_model", ['"\\ud800"', '"claude‮evil"'], ids=["lone-surrogate", "bidi-override"])
def test_a_model_outside_the_id_alphabet_files_and_records_unknown(tmp_path, raw_model):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    (q / cx.SETTINGS_NAME).write_text(json.dumps({"away_mode": True, "away_hold_seconds": 0}))
    # the payload is parsed from JSON TEXT, so an escaped lone surrogate reaches the adapter as the agent sent it
    text = ('{"hook_event_name": "%s", "session_id": "s", "cwd": %s, "tool_name": "Bash", "tool_input": {"command": "git push"}, '
            '"model": %s, "permission_mode": "default"}' % (cx.EVENT, json.dumps(str(tmp_path)), raw_model))
    assert cx.main(["--max-hold", "0", "--queue-dir", str(q)], stdin=io.StringIO(text), stdout=io.StringIO()) == 0
    queue = ConsentQueue(q, authority=authority_source(q / "cockpit.db", anchor=None), home=h4_home())
    filed = [r[0] for r in sqlite3.connect(q / "queue.db").execute("SELECT id FROM requests ORDER BY rowid")]
    assert len(filed) == 1
    assert queue.model(filed[0])["requested"] == MODEL_UNKNOWN
