"""levain#86: a dream rolled back AFTER a dream-seam rotation is told to the desk that loaded it. Each test is a real
sequence on a real store: a real card filed through the dream's own builder, a real ``rotate``, the successor activated,
then a ``rolled-back`` card, then ``coord open`` through ``main``. The notice is derived from the cards and the
rotation's handoff, so nothing here writes a notice and none is read back from a table."""
from __future__ import annotations

import os
import sqlite3
from datetime import timedelta

from levain.cockpit.consent import parse_stamp
from levain.desk import dream_rotation
from levain.desk.store import CoordStore
from tests.test_dream_rotation import BODY
from tests.test_dream_seam import (_activate_successor, _file, _home_env, _open, _requested, desk)  # noqa: F401

MARK = "Dream rolled back:"


def _notes(out):
    return [ln for ln in out.splitlines() if ln.startswith(MARK)]


def _rotated(desk, occurrence="occ-1"):
    """A save a minute after the desk was requested, rotated at a store clock ten minutes on. Returns (rotation, the
    successor's token, the base time the stamps below hang off)."""
    base = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", base + timedelta(minutes=1), occurrence=occurrence)
    later = CoordStore(desk.directory, clock=lambda: base + timedelta(minutes=10))
    got = dream_rotation.rotate(later, token=desk.token, body=BODY)
    assert isinstance(got, dream_rotation.Rotation) and got.occurrence == occurrence
    return got, _activate_successor(later, got.successor), base


def _handoffs(desk):
    return sqlite3.connect(desk.path).execute("SELECT close_key FROM seam_handoffs ORDER BY handoff_id").fetchall()


def _launches(desk):
    return sqlite3.connect(desk.path).execute("SELECT launch_id FROM launches ORDER BY gseq").fetchall()


def test_a_rollback_after_a_rotation_is_told_to_the_next_desk_on_its_open(desk, capsys):
    got, token, base = _rotated(desk)
    desk.token = token
    assert _notes(_open(desk, capsys)) == []                         # nothing to say before the rollback
    _file(desk.directory, "rolled-back", base + timedelta(minutes=30), occurrence="occ-1", reason="rolled back by operator")
    lines = _notes(_open(desk, capsys))
    assert len(lines) == 1
    assert "occurrence occ-1" in lines[0] and "rolled back at" in lines[0]
    at = sqlite3.connect(desk.path).execute("SELECT at FROM review_items WHERE dedupe_key = 'dream-rollback:occ-1'").fetchone()[0]
    assert at in lines[0]
    assert "nothing was opened, ended or launched for you" in lines[0]


def test_the_notice_carries_no_authority_it_opens_ends_and_launches_nothing(desk, capsys):
    got, token, base = _rotated(desk)
    desk.token = token
    _file(desk.directory, "rolled-back", base + timedelta(minutes=30), occurrence="occ-1")
    _open(desk, capsys)                                              # the open's own writes (its build) settle first
    keys, launches = _handoffs(desk), _launches(desk)
    assert _notes(_open(desk, capsys, "--light"))
    assert _handoffs(desk) == keys and _launches(desk) == launches
    assert desk.pending_desk() is None


def test_a_repeated_rollback_files_and_tells_once(desk, capsys):
    got, token, base = _rotated(desk)
    desk.token = token
    for _ in range(2):                                               # the card's dedupe key holds the second filing out
        _file(desk.directory, "rolled-back", base + timedelta(minutes=30), occurrence="occ-1")
    assert sqlite3.connect(desk.path).execute(
        "SELECT COUNT(*) FROM review_items WHERE dedupe_key = 'dream-rollback:occ-1'").fetchone()[0] == 1
    assert len(_notes(_open(desk, capsys))) == 1 and len(_notes(_open(desk, capsys))) == 1


def test_a_rollback_of_a_dream_no_rotation_used_writes_and_tells_nothing(desk, capsys):
    # the desk started after the save, so it loaded that dream's text: only the missing rotation keeps it quiet
    base = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", base - timedelta(hours=1), occurrence="occ-1")
    _file(desk.directory, "rolled-back", base + timedelta(minutes=30), occurrence="occ-1")
    assert _handoffs(desk) == []
    assert _notes(_open(desk, capsys)) == [] and _notes(_open(desk, capsys, "--light")) == []


def test_a_rollback_of_another_occurrence_is_not_told_as_the_rotated_one(desk, capsys):
    got, token, base = _rotated(desk)
    desk.token = token
    _file(desk.directory, "consolidated", base + timedelta(minutes=5), occurrence="occ-2")
    _file(desk.directory, "rolled-back", base + timedelta(minutes=30), occurrence="occ-2")
    assert _notes(_open(desk, capsys)) == []


def test_a_desk_that_started_after_the_rollback_loaded_the_restored_text_and_is_told_nothing(desk, capsys):
    got, token, base = _rotated(desk)
    _file(desk.directory, "rolled-back", base + timedelta(minutes=11), occurrence="occ-1")
    later = CoordStore(desk.directory, clock=lambda: base + timedelta(minutes=20))
    later.launch_replace_done(got.successor.launch_id, asked=True)
    nxt = later.pending_desk()
    later.launch_claim_spawn(nxt.launch_id, os.getpid())
    _, why = later.launch_activate(nxt.launch_id, harness_session_id="h-" + nxt.launch_id)
    assert why is None
    desk.token = later.coord_status()["lease"]["token"]
    assert _notes(_open(desk, capsys)) == []


def _activated_at(desk, got, when):
    """The rotation's successor activated at a store clock of ``when`` (its request was earlier): returns its token."""
    later = CoordStore(desk.directory, clock=lambda: when)
    return _activate_successor(later, got.successor)


def test_a_successor_requested_before_the_rollback_but_activated_after_it_is_told_nothing(desk, capsys):
    # launches.at is the REQUEST stamp (base+10); the desk loaded memory at activation (base+20), after the rollback (+15)
    base = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", base + timedelta(minutes=1), occurrence="occ-1")
    got = dream_rotation.rotate(CoordStore(desk.directory, clock=lambda: base + timedelta(minutes=10)), token=desk.token, body=BODY)
    _file(desk.directory, "rolled-back", base + timedelta(minutes=12), occurrence="occ-1")
    desk.token = _activated_at(desk, got, base + timedelta(minutes=14))
    assert _notes(_open(desk, capsys)) == []


def test_a_successor_requested_and_activated_before_the_rollback_is_told(desk, capsys):
    base = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", base + timedelta(minutes=1), occurrence="occ-1")
    got = dream_rotation.rotate(CoordStore(desk.directory, clock=lambda: base + timedelta(minutes=10)), token=desk.token, body=BODY)
    desk.token = _activated_at(desk, got, base + timedelta(minutes=11))
    _file(desk.directory, "rolled-back", base + timedelta(minutes=15), occurrence="occ-1")
    assert len(_notes(_open(desk, capsys))) == 1


def test_a_failing_rollback_read_does_not_drop_the_seam_note(desk, capsys, monkeypatch):
    from levain.desk import dream_status

    base = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", base + timedelta(hours=1), occurrence="occ-9")  # a save after the desk started

    def boom(*a, **k):
        raise sqlite3.OperationalError("no such column: close_key")

    monkeypatch.setattr(dream_status, "rollback_notes", boom)
    out = _open(desk, capsys, "--light")
    assert "Dream seam: the" in out and "dream saved new memory" in out
    assert "Dream rolled back: cannot-derive (OperationalError" in out


def test_a_failing_seam_note_does_not_drop_the_rollback_line(desk, capsys, monkeypatch):
    from levain.desk import dream_status

    got, token, base = _rotated(desk)
    desk.token = token
    _file(desk.directory, "rolled-back", base + timedelta(minutes=30), occurrence="occ-1")

    def boom(*a, **k):
        raise RuntimeError("tail unreadable")

    monkeypatch.setattr(dream_status, "seam_note", boom)
    out = _open(desk, capsys)
    assert len(_notes(out)) == 1 and "occurrence occ-1" in _notes(out)[0]
    assert "Dream seam: cannot-derive (RuntimeError" in out


def test_two_rollbacks_of_distinct_occurrences_are_told_newest_first(desk, capsys):
    base = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", base + timedelta(minutes=1), occurrence="occ-1", day="2026-11-14")
    first = dream_rotation.rotate(CoordStore(desk.directory, clock=lambda: base + timedelta(minutes=10)), token=desk.token, body=BODY)
    token = _activated_at(desk, first, base + timedelta(minutes=11))
    _file(desk.directory, "consolidated", base + timedelta(minutes=12), occurrence="occ-2", day="2026-11-15")
    second = dream_rotation.rotate(CoordStore(desk.directory, clock=lambda: base + timedelta(minutes=13)), token=token, body=BODY)
    desk.token = _activated_at(desk, second, base + timedelta(minutes=14))
    # the desk running now loaded both saves; both are then rolled back, occ-1 first
    _file(desk.directory, "rolled-back", base + timedelta(minutes=20), occurrence="occ-1")
    _file(desk.directory, "rolled-back", base + timedelta(minutes=25), occurrence="occ-2")
    lines = _notes(_open(desk, capsys))
    assert len(lines) == 2 and "occ-2" in lines[0] and "occ-1" in lines[1]


def test_the_newest_save_of_an_occurrence_at_or_before_the_load_decides(desk, capsys):
    from levain.desk import dream_status

    # two saves of occ-1: an early one and a later one; the desk loaded between them is judged by the later one at or
    # before its load, never the oldest
    recs = [dream_status.DreamRecord("rolled-back", "2026-11-15T09:00:00Z", "o"),
            dream_status.DreamRecord("consolidated", "2026-11-15T07:00:00Z", "o"),
            dream_status.DreamRecord("consolidated", "2026-11-15T05:00:00Z", "o")]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE seam_handoffs (close_key TEXT)")
    conn.execute("INSERT INTO seam_handoffs VALUES ('dream-rotation:o')")
    (line,) = dream_status.rollback_notes(conn, recs, "2026-11-15T08:00:00Z")
    assert "saved 2026-11-15T07:00:00Z" in line


def test_a_dream_saved_between_the_successors_request_and_activation_is_not_told_as_saved_after_it_started(desk, capsys):
    # launches.at is the REQUEST stamp (base+10); the successor loaded memory at activation (base+14), after the
    # occ-2 save (+12): that dream WAS loaded, so no "Dream seam:" line may say it saved after the desk started
    base = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", base + timedelta(minutes=1), occurrence="occ-1")
    got = dream_rotation.rotate(CoordStore(desk.directory, clock=lambda: base + timedelta(minutes=10)), token=desk.token, body=BODY)
    _file(desk.directory, "consolidated", base + timedelta(minutes=12), occurrence="occ-2", day="2026-11-15")
    desk.token = _activated_at(desk, got, base + timedelta(minutes=14))
    assert "Dream seam:" not in _open(desk, capsys)


def test_two_rolled_back_cards_of_one_occurrence_after_the_load_tell_one_line_the_newest(desk, capsys):
    from levain.desk import dream_status

    recs = [dream_status.DreamRecord("rolled-back", "2026-11-15T10:00:00Z", "o"),
            dream_status.DreamRecord("rolled-back", "2026-11-15T09:00:00Z", "o"),
            dream_status.DreamRecord("consolidated", "2026-11-15T07:00:00Z", "o")]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE seam_handoffs (close_key TEXT)")
    conn.execute("INSERT INTO seam_handoffs VALUES ('dream-rotation:o')")
    (line,) = dream_status.rollback_notes(conn, recs, "2026-11-15T08:00:00Z")
    assert "rolled back at 2026-11-15T10:00:00Z" in line


def test_a_rollback_whose_save_is_not_at_or_before_the_load_is_told_nothing_not_a_cannot_derive():
    from levain.desk import dream_status

    # the rotation's handoff exists and the rollback is after the load, but the only save of "o" was filed after the load:
    # this desk's memory did not include it, so there is nothing to tell (and no save to name in a line)
    recs = [dream_status.DreamRecord("rolled-back", "2026-11-15T10:00:00Z", "o"),
            dream_status.DreamRecord("consolidated", "2026-11-15T09:00:00Z", "o")]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE seam_handoffs (close_key TEXT)")
    conn.execute("INSERT INTO seam_handoffs VALUES ('dream-rotation:o')")
    assert dream_status.rollback_notes(conn, recs, "2026-11-15T08:00:00Z") == []
