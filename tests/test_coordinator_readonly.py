"""C4 integration item 1: CoordStore's ONE public read-only opener, and the coordinator reads that go through it."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from levain.coordinator import morning, review
from levain.coordinator.store import CoordError, CoordStore, StoreAbsent
from levain.dispatch.jobs import DB_NAME


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def test_the_opener_never_creates_a_directory_or_a_file(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"
    with pytest.raises(StoreAbsent):
        CoordStore(missing).open_readonly()
    assert not missing.exists()
    d = _dir(tmp_path)
    with pytest.raises(StoreAbsent):
        CoordStore(d).open_readonly()
    assert list(d.iterdir()) == []


def test_a_job_store_file_without_the_coordinator_tables_is_absent_and_stays_unchanged(tmp_path: Path) -> None:
    from levain.dispatch.jobs import JobStore

    d = _dir(tmp_path)
    JobStore(d)._connect().close()                             # makes dispatch.db with the job store's schema only
    before = (d / DB_NAME).read_bytes()
    with pytest.raises(StoreAbsent):
        CoordStore(d).open_readonly()
    assert (d / DB_NAME).read_bytes() == before


def test_the_opener_runs_the_write_paths_directory_checks(tmp_path: Path) -> None:
    from levain.dispatch.jobs import JobStoreError

    d = _dir(tmp_path)
    review.file_item(CoordStore(d), "seat", {"text": "x"}, dedupe_key="a")
    os.chmod(d, 0o755)
    with pytest.raises(JobStoreError):
        CoordStore(d).open_readonly()


def test_the_connection_refuses_a_write_and_the_file_has_no_flag_to_set(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    review.file_item(CoordStore(d), "seat", {"text": "x"}, dedupe_key="a")
    store = CoordStore(d)
    conn = store.open_readonly()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM review_items")
    finally:
        conn.close()
    assert not hasattr(store, "_jobs") or store._jobs._ready is False       # opening it marked nothing ready
    assert store._ready is False


def test_a_path_with_a_question_mark_is_opened_as_itself(tmp_path: Path) -> None:
    d = tmp_path / "a?b#c"
    d.mkdir(mode=0o700)
    review.file_item(CoordStore(d), "seat", {"text": "x"}, dedupe_key="a")
    assert [i.body["text"] for i in review.list_items(CoordStore(d))] == ["x"]


def test_the_reads_use_the_opener_and_never_a_connect(tmp_path: Path, monkeypatch) -> None:
    d = _dir(tmp_path)
    review.file_item(CoordStore(d), "seat", {"text": "x"}, dedupe_key="a")
    fresh = CoordStore(d)

    def boom(*a, **k):
        raise AssertionError("a read took a creating connect")

    monkeypatch.setattr(fresh, "_connect", boom)
    monkeypatch.setattr(fresh, "_read_connect", boom, raising=False)
    assert [i.body["text"] for i in review.list_items(fresh)] == ["x"]
    assert review.waiting_on_you(fresh, sign_open=review.queue_request_open(d),
                                 answer_open=review.question_open(d))["threads"] == {}
    c = morning.card(fresh)
    assert [i["text"] for i in c["review"]] == ["x"] and c["errors"] == []


def test_a_read_of_an_absent_store_raises_and_creates_nothing(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    with pytest.raises(CoordError):
        review.list_items(CoordStore(d))
    with pytest.raises(CoordError):
        morning.card(CoordStore(d))
    assert list(d.iterdir()) == []


def test_an_item_whose_commit_failed_is_not_reported_created(tmp_path: Path, monkeypatch) -> None:
    """Item 7: _CREATED records an id only after the write that inserted it has committed."""
    from contextlib import contextmanager

    d = _dir(tmp_path)
    store = CoordStore(d)
    real = store._write

    @contextmanager
    def failing_commit(*a, **k):
        with real(*a, **k) as got:
            yield got
        raise sqlite3.OperationalError("disk I/O error at COMMIT")

    created: list[str] = []
    mark = review._CREATED.set(created)
    try:
        monkeypatch.setattr(store, "_write", failing_commit)
        with pytest.raises(sqlite3.OperationalError):
            review.file_item(store, "seat", {"text": "x"}, dedupe_key="a")
        assert created == []
        monkeypatch.undo()
        item, new = review.file_item(store, "seat", {"text": "x"}, dedupe_key="b")
        assert new and created == [item.item_id]
    finally:
        review._CREATED.reset(mark)


def test_the_card_takes_the_queue_the_caller_already_read_and_reads_it_no_second_time(tmp_path: Path, monkeypatch) -> None:
    """Item 4: one snapshot per response; the injected queue keeps the card's own shape."""
    d = _dir(tmp_path)
    store = CoordStore(d)
    review.file_item(store, "doubt", {"text": "d"}, dedupe_key="a", authority_relevant=True)
    done, _ = review.file_item(store, "seat", {"text": "s"}, dedupe_key="b")
    review.clear_item(store, done.item_id)
    items = review.list_items(store, include_cleared=True)
    own = morning.card(store)["review"]

    def boom(*a, **k):
        raise AssertionError("the card read the queue a second time")

    monkeypatch.setattr(review, "list_items", boom)
    got = morning.card(store, items=items)["review"]
    assert got == own and [i["feed"] for i in got] == ["doubt"]


def test_a_section_the_card_could_not_read_is_not_printed_as_zero(tmp_path: Path, monkeypatch) -> None:
    """Item 5."""
    d = _dir(tmp_path)
    store = CoordStore(d)
    store.threads()

    def bad(_qdir):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(morning, "_undecided", bad)
    lines = morning.render(morning.card(store)).split("\n")
    assert "undecided: could not read" in lines and not any(ln.startswith("undecided (") for ln in lines)
    assert any(ln.startswith("could not read: undecided:") for ln in lines)


def test_the_card_says_review_queue_n_open_items(tmp_path: Path) -> None:
    """Item 8: the CLI's wording."""
    d = _dir(tmp_path)
    store = CoordStore(d)
    review.file_item(store, "doubt", {"text": "d"}, dedupe_key="a")
    assert "review queue: 1 open item(s)" in morning.render(morning.card(store)).split("\n")


def test_a_store_without_the_envelope_tables_is_readable_by_the_envelope_store(tmp_path: Path) -> None:
    """Team drafts make a plain CoordStore; a view built on the envelope store must still list what it filed."""
    from levain.coordinator.envelopes import EnvelopeStore

    d = _dir(tmp_path)
    item, _ = review.file_item(CoordStore(d), "seat", {"text": "x"}, dedupe_key="a")
    conn = sqlite3.connect(d / DB_NAME)
    try:
        assert "envelopes" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert [i.item_id for i in review.list_items(EnvelopeStore(d))] == [item.item_id]


def test_the_card_reads_every_section_of_the_store_in_one_snapshot_on_one_connection(tmp_path: Path, monkeypatch) -> None:
    """C4 r1 item 3: a write that lands between the card's sections is in none of them."""
    d = _dir(tmp_path)
    store = CoordStore(d)
    review.file_item(store, "seat", {"text": "first"}, dedupe_key="a")
    opened: list[int] = []
    real_open = CoordStore.open_readonly

    def counting(self):
        opened.append(1)
        return real_open(self)

    real_waiting = review.waiting_on_you

    def moving(st, **kw):
        out = real_waiting(st, **kw)
        review.file_item(CoordStore(d), "seat", {"text": "second"}, dedupe_key="b")   # the world moves mid-card
        return out

    monkeypatch.setattr(CoordStore, "open_readonly", counting)
    monkeypatch.setattr(review, "waiting_on_you", moving)
    c = morning.card(store)
    assert opened == [1]
    assert [i["text"] for i in c["review"]] == ["first"] and c["review_total"] == 1


def test_the_opener_validates_the_directory_without_a_path_that_can_make_it(tmp_path: Path, monkeypatch) -> None:
    """C4 r1 item 6: a directory that vanishes after the existence check stays vanished; the read path has no mkdir."""
    from levain.dispatch.jobs import JobStore, JobStoreError

    d = _dir(tmp_path)
    review.file_item(CoordStore(d), "seat", {"text": "x"}, dedupe_key="a")

    def boom(self):
        raise AssertionError("the read-only opener used the directory check that creates a missing directory")

    monkeypatch.setattr(JobStore, "_check_directory", boom)
    CoordStore(d).open_readonly().close()
    link = tmp_path / "link"
    link.symlink_to(d)
    with pytest.raises(JobStoreError):
        CoordStore(link).open_readonly()
    gone = tmp_path / "gone"
    with pytest.raises(StoreAbsent):
        CoordStore(gone).open_readonly()
    assert not gone.exists()


def test_a_delivery_pass_on_no_store_reads_nothing_and_makes_none(tmp_path: Path) -> None:
    from levain.coordinator import deliver

    d = _dir(tmp_path)
    out = deliver.deliver(CoordStore(d))
    assert out.errors and "no coordinator store" in out.errors[0]
    assert list(d.iterdir()) == []


def test_first_reader_names_the_question_store_through_the_one_table() -> None:
    from levain.coordinator import first_reader

    src = Path(first_reader.__file__).read_text(encoding="utf-8")
    assert "'questions.db'" not in src and '"questions.db"' not in src


def test_a_closed_thread_is_not_an_open_thread_on_the_card(tmp_path: Path) -> None:
    store = CoordStore(_dir(tmp_path))
    store.threads()
    conn = sqlite3.connect(store.path)
    try:
        ids = [r[0] for r in conn.execute("SELECT thread_id FROM threads")]
    finally:
        conn.close()
    assert ids == [] and morning.card(store)["budgets"] == []


def test_the_read_only_coord_verbs_make_nothing_in_a_queue_dir_with_no_store(tmp_path: Path, capsys) -> None:
    # the shadow finding (10-30 17:08): `levain coord status --queue-dir <dir>` made dispatch.db and awake.lock
    from levain.cli import main

    d = _dir(tmp_path)
    for argv, rc in ((["coord", "status"], 0), (["drops", "list"], 0), (["thread", "list"], 0),
                     (["drops", "show", "d-none"], 1), (["thread", "show", "t-none"], 1)):
        assert main([*argv, "--queue-dir", str(d)]) == rc, argv
        capsys.readouterr()
        assert sorted(p.name for p in d.iterdir()) == [], (argv, list(d.iterdir()))
    assert main(["coord", "status", "--queue-dir", str(d)]) == 0
    assert "no coordinator store here" in capsys.readouterr().out
    gone = tmp_path / "never-made"
    assert main(["coord", "status", "--queue-dir", str(gone)]) == 0 and not gone.exists()
