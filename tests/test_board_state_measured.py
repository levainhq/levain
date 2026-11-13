"""The two behaviours that came from a measured failure (design page section 1), written before the function existed.

A row that landed on origin/main while its text said "in flight" must read ``landed_not_closed``, never ready or
running; and evidence older than ``MAX_EVIDENCE_AGE_S`` must read ``unknown``, never the state it would have shown
fresh. Both fail against a draft that falls back to ready / in flight (that run is the red-first record)."""
from __future__ import annotations

import json

from levain.desk import board_state as bs

NOW = bs.parse_timestamp("2026-11-12T20:00:00Z")


def _add(iid, **extra):
    rec = {"v": 1, "op": "add", "id": iid, "at": "2026-11-12T10:00:00Z", "by": "desk 17", "repo": iid.split("#")[0],
           "title": "a row", "words": "", "origin": "desk 17", "done": "merged and closed", "done_kind": "merged",
           "tier": "sonnet", "depends_on": []}
    rec.update(extra)
    return json.dumps(rec)


def _evidence(**over):
    ev = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [], "chain": [],
          "refs": [], "merged": [], "branches": [], "seats": [], "band": {"open": {}, "resolved": {}},
          "waiters": [], "documents": {}, "unattributed": []}
    ev.update(over)
    return bs.Evidence.from_dict(ev)


def _state(lines, ev):
    board = bs.derive_all(bs.fold(lines), ev, now=NOW)
    return {s.id: s for s in board.items}


def test_landed_on_main_while_its_text_said_in_flight_is_landed_not_closed():
    ev = _evidence(main=[{"sha": "a" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#7"], "closes": []}])
    got = _state([_add("levain#7", title="IN FLIGHT on seat/x")], ev)["levain#7"]
    assert got.state == "landed_not_closed", got
    assert "a" * 9 in got.reason


def test_stale_evidence_reads_unknown_not_the_state_it_would_show_fresh():
    ev = _evidence(fetched_at="2026-11-12T08:00:00Z")      # 12 h old, the line is 6 h
    got = _state([_add("levain#8")], ev)["levain#8"]
    assert got.state == "unknown", got
    assert "origin/main as of 2026-11-12T08:00:00Z" in got.reason
