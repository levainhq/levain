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
    needs._reset_owned_for_tests()
    yield
    needs._reset_owned_for_tests()


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


def _hold_lock_elsewhere(path):
    """Another open file description holding the queue dir's lock: this process's own take then loses, as a foreign one would."""
    import fcntl
    import os
    fd = os.open(path / needs.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_non_owner_badge_is_unknown_though_its_local_count_stays_for_the_page(tmp_path, monkeypatch):
    """Finding 1: the preview's computed 0 must not ride a push (production sends badge_value explicitly)."""
    import os
    _no_later(monkeypatch)
    fd = _hold_lock_elsewhere(tmp_path)
    try:
        assert needs.badge_value(tmp_path, _Store(0)) is None        # was 0: cleared the real badge
        assert needs.open_needs_count(tmp_path, _Store(0)) == 0      # the page still renders its own count
        assert (tmp_path / needs.CONTEST_FILE).exists()
    finally:
        os.close(fd)


def test_the_owner_badge_is_its_count(tmp_path, monkeypatch):
    _no_later(monkeypatch)
    assert needs.badge_value(tmp_path, _Store(3)) == 3
    assert needs.badge_value(tmp_path) == 0           # no source: the native store, untouched by ownership


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="needs fork")
def test_a_forked_child_does_not_inherit_ownership(tmp_path, monkeypatch):
    """Finding 2: the child inherited _OWNED and the fd, so an empty-store child overwrote the record with 0, uncontested."""
    import os
    _no_later(monkeypatch)
    assert needs.open_needs_count(tmp_path, _Store(6)) == 6
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            ok = needs.badge_value(tmp_path, _Store(0)) is None and not needs._OWNED
            code = 0 if ok else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads((tmp_path / needs.COUNT_FILE).read_text())["n"] == 6     # the child never wrote its 0
    assert needs.open_needs_count(tmp_path, _Store(7)) == 7                    # the parent still owns
    assert json.loads((tmp_path / needs.COUNT_FILE).read_text())["n"] == 7


def test_threads_taking_the_lock_together_never_contest_themselves(tmp_path, monkeypatch):
    """Finding 3: two threads both saw the key absent, opened separate fds, and the loser stamped contested against itself."""
    import os
    import threading
    _no_later(monkeypatch)
    real_open = os.open

    def slow_open(path, *a, **k):
        fd = real_open(path, *a, **k)
        if str(path).endswith(needs.LOCK_FILE):
            time.sleep(0.05)       # widen the check-then-act window
        return fd

    monkeypatch.setattr(os, "open", slow_open)
    results = []
    barrier = threading.Barrier(6)

    def run():
        barrier.wait()
        results.append(needs.badge_value(tmp_path, _Store(2)))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [2] * 6
    assert not (tmp_path / needs.CONTEST_FILE).exists()


def test_the_owned_key_is_the_resolved_path(tmp_path, monkeypatch):
    """Finding 4: a symlinked or dotted spelling of one dir must not take a second lock and contest itself."""
    _no_later(monkeypatch)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    real.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert needs.badge_value(real, _Store(2)) == 2
    assert needs._own(link)                  # (the listing refuses a symlinked dir itself, so ownership is asked directly)
    assert needs.badge_value(real / ".." / "real", _Store(2)) == 2
    assert len(needs._OWNED) == 1
    assert not (real / needs.CONTEST_FILE).exists()


def test_a_fresh_contest_with_no_count_file_reads_host_unknown_not_native(tmp_path):
    """Finding 5: the contest marker alone made the reader say 'not a host', so the badge fell to a native 0."""
    (tmp_path / needs.CONTEST_FILE).write_text(json.dumps({"at": time.time()}))
    assert not (tmp_path / needs.COUNT_FILE).exists()
    assert needs._read_record(tmp_path, None) == (True, None)
    assert needs.resolved_badge(tmp_path) is None
    (tmp_path / needs.CONTEST_FILE).write_text(json.dumps({"at": time.time() - needs.COUNT_FRESH_S - 5}))
    assert needs._read_record(tmp_path, None) == (False, None)     # aged out: a plain install again
    (tmp_path / needs.CONTEST_FILE).write_bytes(b"\xff\xfe junk")
    assert needs._read_record(tmp_path, None) == (True, None)


def test_the_reset_helper_closes_the_held_fds(tmp_path, monkeypatch):
    """Finding 6."""
    import os
    _no_later(monkeypatch)
    needs.open_needs_count(tmp_path, _Store(1))
    (fd, _pid), = needs._OWNED.values()
    needs._reset_owned_for_tests()
    assert not needs._OWNED
    with pytest.raises(OSError):
        os.fstat(fd)
