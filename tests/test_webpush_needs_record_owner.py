"""One process owns the host's recorded needs count (board row 186). A second serve on the same queue dir (a hand-launched
preview with an empty question store) used to overwrite the real host's ``n`` with 0, and a push carrying badge 0 cleared the
real app badge. Now the first recorder holds an exclusive lock for its life; a second never writes the count and marks the
record contested, and the reader treats a contested record as unknown (None), never as a count. Scratch dirs only."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest

from levain.webpush import needs

ROOT = str(__import__("pathlib").Path(needs.__file__).resolve().parents[2])


class _Store:
    def __init__(self, n):
        self.n = n

    def open_questions(self):
        return [type("Q", (), {"section": "", "carried": False, "id": i})() for i in range(self.n)]


@pytest.fixture(autouse=True)
def _fresh_owner_state():
    needs._OWNED.clear()
    yield
    needs._OWNED.clear()


def _no_later(monkeypatch):
    import levain.band_view as bv
    monkeypatch.setattr(bv, "later_facets", lambda store, q: None)


def test_the_first_recorder_keeps_the_count_and_reads_back(tmp_path, monkeypatch):
    _no_later(monkeypatch)
    assert needs.open_needs_count(tmp_path, _Store(6)) == 6
    assert needs.recorded_count(tmp_path) == 6


def test_a_second_process_with_an_empty_store_never_overwrites_and_the_count_reads_unknown(tmp_path):
    """The measured leak: another process, same queue dir, empty store, ran the same record call."""
    probe = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[2])
        from levain.webpush import needs
        class S:
            def open_questions(self): return []
        needs.open_needs_count(sys.argv[1], S())
    """)
    # the real host, this process
    import levain.band_view as bv
    orig = bv.later_facets
    bv.later_facets = lambda store, q: None
    try:
        assert needs.open_needs_count(tmp_path, _Store(6)) == 6
    finally:
        bv.later_facets = orig
    subprocess.run([sys.executable, "-I", "-c", probe, str(tmp_path), ROOT], check=True, timeout=60)
    doc = json.loads((tmp_path / needs.COUNT_FILE).read_text())
    assert doc["n"] == 6                      # the foreign serve did not write its 0
    assert needs.recorded_count(tmp_path) is None    # contested: unknown, never a count
    assert needs.resolved_badge(tmp_path) is None


def test_the_contest_ages_out_and_the_owner_is_believed_again(tmp_path, monkeypatch):
    _no_later(monkeypatch)
    needs.open_needs_count(tmp_path, _Store(4))
    (tmp_path / needs.CONTEST_FILE).write_text(json.dumps({"at": time.time() - needs.COUNT_FRESH_S - 5}))
    assert needs.recorded_count(tmp_path) == 4


def test_an_unreadable_contest_marker_is_contested(tmp_path, monkeypatch):
    _no_later(monkeypatch)
    needs.open_needs_count(tmp_path, _Store(4))
    (tmp_path / needs.CONTEST_FILE).write_bytes(b"\xff\xfe not json")
    assert needs.recorded_count(tmp_path) is None


def test_the_owner_keeps_recording_through_its_own_lock(tmp_path, monkeypatch):
    _no_later(monkeypatch)
    needs.open_needs_count(tmp_path, _Store(2))
    assert needs.open_needs_count(tmp_path, _Store(3)) == 3
    assert needs.recorded_count(tmp_path) == 3


def test_a_dead_owner_frees_the_lock(tmp_path):
    """The lock dies with its process: a later serve takes the record."""
    probe = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[2])
        from levain.webpush import needs
        class S:
            def open_questions(self): return []
        needs.open_needs_count(sys.argv[1], S())
    """)
    subprocess.run([sys.executable, "-I", "-c", probe, str(tmp_path), ROOT], check=True, timeout=60)
    assert needs.recorded_count(tmp_path) == 0
    import levain.band_view as bv
    orig = bv.later_facets
    bv.later_facets = lambda store, q: None
    try:
        assert needs.open_needs_count(tmp_path, _Store(5)) == 5
    finally:
        bv.later_facets = orig
    assert needs.recorded_count(tmp_path) == 5
