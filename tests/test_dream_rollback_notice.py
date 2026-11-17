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
