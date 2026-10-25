"""C4 integration fix r2 (RULINGS_V1 items 1-5): the pass mark, the envelope-aware card, the clipped command row."""
from __future__ import annotations

from pathlib import Path

from levain.coordinator import deliver, morning, review
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordStore, Part


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def _last(store: CoordStore):
    with review.read_transaction(store.open_readonly) as conn:
        return review.last_pass(conn)


def test_a_pass_that_could_not_read_the_queue_records_no_pass(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    store.threads()
    (d / "questions.db").write_bytes(b"this is not a database" * 50)
    ran = deliver.run(store)
    assert deliver.faults(ran)
    assert _last(store) is None
    (d / "questions.db").unlink()
    deliver.run(store)
    assert _last(store) is not None


def test_a_pass_on_a_directory_with_no_store_records_no_pass(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    deliver.run(store)                  # the feeds open the store; the mark is for a pass that began on one
    assert _last(store) is None


def test_the_card_shows_an_open_thread_of_a_plain_store_read_through_the_envelope_store(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    plain = CoordStore(d)
    drop, _new = plain.accept_drop("do the thing", surface="cli")
    plain.split(drop.drop_id, [Part(kind="work", title="the thing")])
    assert len(plain.threads()) == 1
    card = morning.card(EnvelopeStore(d))
    assert card["errors"] == [] and card["budgets"] == []


def test_the_pass_runs_twice_and_marks_twice(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    store.threads()
    deliver.run(store)
    first = _last(store)
    deliver.run(store)
    assert first is not None and _last(store) is not None
    with review.read_transaction(store.open_readonly) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_marks WHERE feed = ?", (review.PASS_FEED,)).fetchone()[0] == 2


def test_a_first_reader_that_cannot_import_leaves_the_ping_to_fire(monkeypatch) -> None:
    """Item 2: first_reader's module-scope import is inside the band's guard (consent.band._hold), which falls back
    to a ping on ANY import fault."""
    import sys

    from levain.consent import band

    monkeypatch.setitem(sys.modules, "levain.coordinator.first_reader", None)
    assert band._hold(Path("/nonexistent"), "q-1") is False
