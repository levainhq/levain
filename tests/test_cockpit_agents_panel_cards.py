"""P9b L3 fixes: the card count matches the right agent, a store without tables is said, the wiring carries a clock."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from levain.cockpit.agents_panel import AgentsWiring, _card_index, _card_marks, _cards_of, wire_agents


def _agent(agent_id: str, name: str) -> dict:
    return {"agent_id": agent_id, "name": name}


def test_a_name_equal_to_a_kind_or_suffix_is_not_that_agents_card() -> None:
    cards = [("dream-night:ag-1:d1", False), ("agent:both", False), ("dream-night:ag-2:d1", True)]
    idx = _card_index(cards)
    assert _cards_of(_agent("ag-9", "agent"), idx) == (0, 0)        # "agent" is a kind here, not a subject
    assert _cards_of(_agent("ag-3", "d1"), idx) == (0, 0)           # a suffix
    assert _cards_of(_agent("ag-3", "dream-night"), idx) == (0, 0)  # the kind
    assert _cards_of(_agent("ag-1", "x"), idx) == (1, 0)            # by id
    assert _cards_of(_agent("ag-2", "x"), idx) == (0, 1)
    assert _cards_of(_agent("ag-9", "ag-1"), _card_index([("k:w-paused:d3", False)])) == (0, 0)
    assert _cards_of(_agent("ag-9", "w-paused"), _card_index([("k:w-paused:d3", False)])) == (1, 0)   # by name as subject


def test_a_coordinator_file_without_tables_is_said_and_no_file_is_not(tmp_path: Path) -> None:
    from levain.coordinator.store import CoordStore

    w = AgentsWiring(dispatch_dir=lambda: tmp_path)
    assert _card_marks(w) == ([], None)                              # no store at all
    path = CoordStore(tmp_path).path
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE review_items (x)")
    c.commit()
    c.close()
    cards, why = _card_marks(w)
    assert cards == [] and why and "tables" in why


def test_wire_agents_carries_the_clock() -> None:
    class Ck:
        def discover(self, *a, **k) -> None: ...

    t = datetime(2020, 1, 1, tzinfo=timezone.utc)
    w = wire_agents(Ck(), object(), clock=lambda: t)
    assert w.now() == t
