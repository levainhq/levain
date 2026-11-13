"""The B1 known edge that B2 closed: landed work behind an orphan branch AND an expired gate its wait does not rest on.

B1 audited that item once, as ``gate_expired``, and the landed text was dropped (the orphan branch skipped rule 6, rule 7 read
the gate, and nothing said the work is on main). The landed text now rides in the ``gate_expired`` reason."""
from __future__ import annotations

import json

from levain.desk import board_state as bs

NOW = bs.parse_timestamp("2026-11-12T20:00:00Z")
GATE = {"kind": "gate", "words": "Phill's look", "recheck": "2026-11-10"}


def _item(n: int, depends=()) -> str:
    return json.dumps({"v": 1, "op": "add", "id": f"levain#{n}", "at": "2026-11-12T10:00:00Z", "by": "t", "repo": "levain",
                       "title": "t", "origin": "o", "done": "lands", "tier": "sonnet", "depends_on": list(depends)})


def _evidence(**kw) -> dict:
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:00:00Z", "unreadable": {}, "main": [], "chain": [], "refs": [],
            "merged": [], "branches": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {},
            "unattributed": []}
    base.update(kw)
    return base


def test_landed_work_behind_an_orphan_branch_and_an_expired_gate_is_named_in_the_gate_expired_reason():
    ev = _evidence(main=[{"sha": "a" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#1"], "closes": []}],
                   branches=[{"name": "seat/x-i1-y", "tip": "b" * 40, "date": "2026-11-12T13:00:00Z", "ahead": 2, "items": ["levain#1"]}])
    board = bs.derive_all(bs.fold([_item(1, [GATE])]), bs.Evidence.from_dict(ev), now=NOW)
    (s,) = board.items
    assert s.state == "unknown" and "past its recheck" in s.reason     # the orphan skipped rule 6, rule 7 reads the gate
    (entry,) = board.audit
    assert entry.kind == "gate_expired" and entry.id == "levain#1"
    assert "Phill's look" in entry.why and "aaaaaaaaa" in entry.why and "commits on main" in entry.why, entry.why


def test_an_expired_gate_alone_keeps_its_plain_reason():
    ev = _evidence(branches=[{"name": "seat/x-i1-y", "tip": "b" * 40, "date": "2026-11-12T13:00:00Z", "ahead": 2, "items": ["levain#1"]}])
    board = bs.derive_all(bs.fold([_item(1, [GATE])]), bs.Evidence.from_dict(ev), now=NOW)
    (entry,) = board.audit
    assert entry.kind == "gate_expired" and entry.why == 'gate "Phill\'s look" past its recheck 2026-11-10'
