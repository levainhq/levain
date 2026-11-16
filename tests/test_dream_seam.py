"""D1-L3: the dream's status in the desk brief ("no dream since X"), and the dream seam for a running desk (a line that
says a dream saved memory after it started, and the dream handoff's open). Each test is a reproduced sequence: a fixed
clock, a real card filed through the dream's own card builder into the desk's review store, a real brief or ``coord
open`` read of it."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from levain.cli import main
from levain.cockpit.consent import parse_stamp
from levain.desk import review, seam, seam_handoffs
from levain.desk.store import CoordStore
from levain.dream import card as dream_card

NOW = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
BODY = {"state_line": "night work", "in_flight": [], "next": "carry on", "contingencies": [], "notes": "",
        "attested_operator_present": False, "attested_walked": False}


def _dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir(mode=0o700)
    return d


def _file(directory, kind, when, occurrence="occ-1", day="2026-11-14", **kw):
    """One dream card filed into the desk's review store at ``when`` (the store's clock), the way the dream files it."""
    s = CoordStore(directory, clock=lambda: when)
    c = dream_card.build_card(kind, occurrence=occurrence, store_dir=str(directory), day=day, **{"reason": "r", **kw})
    review.file_item(s, "agent", c.body, dedupe_key=c.dedupe_key, authority_relevant=c.authority_relevant)


@pytest.fixture
def home(tmp_path):
    return _dir(tmp_path, "d")


def _brief(directory, now=NOW):
    s = CoordStore(directory, clock=lambda: now)
    s._connect().close()
    return seam.open_brief(s, directory, "light", now).text


def _dream_lines(text):
    return [ln for ln in text.splitlines() if ln.startswith("Dream")]


# --- part 1: the freshness gauge ------------------------------------------------------------------------------------
def test_a_store_that_never_dreamed_says_nothing_of_dreaming(home):
    assert _dream_lines(_brief(home)) == []


def test_a_fresh_save_names_the_dream_that_refreshed_the_memory(home):
    _file(home, "consolidated", NOW - timedelta(hours=8))
    assert _dream_lines(_brief(home)) == ["Dream: memory refreshed by the 00:00 dream, 2026-11-15T00:00:00.000000Z (8h00m ago)"]


def test_a_night_with_no_dream_shows_no_dream_since_without_anyone_asking(home):
    # the last save was two nights ago and nothing was filed since: the brief says so on its own
    _file(home, "consolidated", NOW - timedelta(hours=56))
    assert _dream_lines(_brief(home)) == ["Dream: no dream since 2026-11-13T00:00:00.000000Z (56h00m ago)"]


def test_a_failed_night_after_a_save_shows_no_dream_since_and_the_outcome(home):
    _file(home, "consolidated", NOW - timedelta(hours=30), occurrence="occ-1", day="2026-11-13")
    _file(home, "did-not-consolidate", NOW - timedelta(hours=8), occurrence="occ-2", day="2026-11-14")
    assert _dream_lines(_brief(home)) == ["Dream: no dream since 2026-11-14T02:00:00.000000Z (30h00m ago); the night after it "
                                          "did-not-consolidate (2026-11-15T00:00:00.000000Z)"]


def test_a_store_whose_only_dreams_failed_says_none_has_saved(home):
    _file(home, "failed", NOW - timedelta(hours=8))
    assert _dream_lines(_brief(home)) == ["Dream: no dream has saved memory in this store; the last outcome is failed, "
                                          "2026-11-15T00:00:00.000000Z (8h00m ago)"]


def test_a_rollback_after_the_save_puts_memory_reverted_on_the_brief(home):
    _file(home, "consolidated", NOW - timedelta(hours=8))
    _file(home, "rolled-back", NOW - timedelta(hours=2), occurrence="occ-1", reason="rolled back by operator")
    lines = _dream_lines(_brief(home))
    assert lines[1] == "Dream: memory reverted at 06:00 (2026-11-15T06:00:00.000000Z); a revert asks for no rotation"


def test_the_brief_no_longer_says_dreaming_is_not_built(home):
    assert not hasattr(seam, "NOT_BUILT")
    assert not any("not built" in ln.lower() for ln in seam.verbose_lines())
    assert any(ln.startswith("Dream:") for ln in seam.verbose_lines())
    stub = seam.open_brief(CoordStore(home / "nowhere"), home, "light", NOW).text
    assert "not built" not in stub.lower() and "Dream: cannot-derive" in stub


def test_a_shadow_nights_save_is_not_a_dream_that_wrote_memory(home, tmp_path):
    from levain.dream import shadow
    from levain.desk import dream_status

    day = SimpleNamespace(day="2026-11-14", run_id="r", run_dir=tmp_path)
    c = shadow.shadow_card(day, "shadow\n", occurrence="occ-s", occ_dir=_dir(tmp_path, "occ"), flow_text="flow\n")
    assert c.body["remedy"].startswith(dream_status.SHADOW_REMEDY)
    review.file_item(CoordStore(home, clock=lambda: NOW - timedelta(hours=2)), "agent", c.body, dedupe_key=c.dedupe_key)
    assert _dream_lines(_brief(home)) == []


# --- part 2: the dream seam ----------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))


def _active(store):
    d, _ = store.accept_drop("words", surface="cli")
    lid = store.woken_launch(d.drop_id)
    store.launch_claim_spawn(lid, os.getpid())
    got, why = store.launch_activate(lid, harness_session_id="h-" + lid)
    assert why is None and got.state == "active"
    return got, store.coord_status()["lease"]["token"]


@pytest.fixture
def desk(tmp_path):
    s = CoordStore(_dir(tmp_path, "q"))
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.launch, s.token = _active(s)
    return s


def _open(desk, capsys, *extra, rc=0):
    got = main(["coord", "open", "--token", str(desk.token), "--queue-dir", str(desk.directory), *extra])
    out = capsys.readouterr()
    assert got == rc, out
    return out.out


def _started(desk):
    return sqlite3.connect(desk.path).execute("SELECT at FROM launch_events WHERE launch_id = ? AND kind = 'active'",
                                              (desk.launch.launch_id,)).fetchone()[0]


def test_a_running_desk_is_told_when_a_dream_saved_after_it_started(desk, capsys):
    started = parse_stamp(_started(desk))
    assert "Dream seam" not in _open(desk, capsys, "--light")
    _file(desk.directory, "consolidated", started + timedelta(hours=1))
    out = _open(desk, capsys, "--light")
    line = next(ln for ln in out.splitlines() if ln.startswith("Dream seam:"))
    assert "saved new memory at" in line and "after this desk started" in line
    assert "never while he is typing" in line and "never in the middle of work" in line
    assert "coord close --kind dream" in line and "Nothing rotates you on its own" in line


def test_a_dream_that_saved_before_the_desk_started_asks_nothing_of_it(desk, capsys):
    started = parse_stamp(_started(desk))
    _file(desk.directory, "consolidated", started - timedelta(hours=1))
    assert "Dream seam" not in _open(desk, capsys, "--light")


def test_a_failed_night_or_a_rollback_never_asks_a_desk_to_rotate(desk, capsys):
    started = parse_stamp(_started(desk))
    _file(desk.directory, "failed", started + timedelta(hours=1), occurrence="o1")
    _file(desk.directory, "rolled-back", started + timedelta(hours=2), occurrence="o2", reason="rolled back by operator")
    assert "Dream seam" not in _open(desk, capsys, "--light")


def test_the_note_is_a_read_the_open_writes_no_handoff_and_starts_no_launch(desk, capsys):
    started = parse_stamp(_started(desk))
    _file(desk.directory, "consolidated", started + timedelta(hours=1))

    def state():
        c = sqlite3.connect(desk.path)
        try:
            return (c.execute("SELECT COUNT(*) FROM launches").fetchone(), c.execute("SELECT COUNT(*) FROM launch_events")
                    .fetchone(), c.execute("SELECT COUNT(*) FROM seam_handoffs").fetchone(),
                    c.execute("SELECT kind FROM lease_events ORDER BY seq").fetchall())
        finally:
            c.close()

    before = state()
    out = _open(desk, capsys, "--light")
    assert "Dream seam:" in out
    after = state()
    assert after[:3] == before[:3]                                  # no launch, no launch event, no handoff
    assert {k for (k,) in after[3][len(before[3]):]} <= {"beat"}    # the lease only beats: no claim, no release


def test_a_dream_handoff_is_opened_for_the_next_desk_with_a_dream_open(desk, tmp_path, capsys):
    # a dream handoff by an earlier session is built as a dream open for the desk that reads it (as a rotation is)
    bf = tmp_path / "b.json"
    bf.write_text(json.dumps(BODY))
    assert main(["coord", "close", "--kind", "dream", "--body-file", str(bf), "--request-id", "r1", "--token",
                 str(desk.token), "--queue-dir", str(desk.directory)]) == 0
    capsys.readouterr()
    c = sqlite3.connect(desk.path)
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'seam_handoffs' "
                             "AND sql LIKE '%UPDATE%'").fetchall():
        c.execute(f"DROP TRIGGER {name}")        # scratch store only: make the handoff another session's
    c.execute("UPDATE seam_handoffs SET writer = 'earlier-session'")
    c.commit()
    c.close()
    out = _open(desk, capsys)
    assert "SEAM OPEN dream" in out and "open 1 (dream)" in out
    assert "--read-back-file PATH" in out and "built from" not in out
    row = sqlite3.connect(desk.path).execute("SELECT seam_kind, signal_kind, signal_key FROM open_builds").fetchone()
    assert row == ("dream", "dream", "1")


def test_the_launch_brief_names_the_dream_close():
    from levain.desk import launch as cl

    text = "\n".join(cl._arrival_section("levain", ""))
    assert "--kind rotation|operator|dream" in text and "`Dream seam:` line" in text


# --- review fixes (levain#57): rollback by occurrence, the night after, a failing read, the close command ----------------
def test_a_save_that_was_rolled_back_is_not_a_seam_the_desk_hands_over_onto(desk, capsys):
    started = parse_stamp(_started(desk))
    _file(desk.directory, "consolidated", started + timedelta(hours=1), occurrence="occ-1")
    _file(desk.directory, "rolled-back", started + timedelta(hours=2), occurrence="occ-1", reason="rolled back by operator")
    assert "Dream seam" not in _open(desk, capsys, "--light")


def test_a_rollback_of_another_occurrence_does_not_cancel_the_save_a_desk_is_told_of(desk, capsys):
    started = parse_stamp(_started(desk))
    _file(desk.directory, "consolidated", started + timedelta(hours=1), occurrence="occ-1")
    _file(desk.directory, "rolled-back", started + timedelta(hours=2), occurrence="occ-0", reason="rolled back by operator")
    assert "Dream seam: the" in _open(desk, capsys, "--light")


def test_a_newer_save_after_a_rollback_of_an_older_one_reads_refreshed_and_not_reverted(home):
    _file(home, "consolidated", NOW - timedelta(hours=12), occurrence="occ-1", day="2026-11-14")
    _file(home, "rolled-back", NOW - timedelta(hours=8), occurrence="occ-1", reason="rolled back by operator")
    _file(home, "consolidated", NOW - timedelta(hours=2), occurrence="occ-2", day="2026-11-15")
    assert _dream_lines(_brief(home)) == ["Dream: memory refreshed by the 06:00 dream, 2026-11-15T06:00:00.000000Z (2h00m ago)"]


def test_the_night_after_the_save_is_the_oldest_later_outcome(home):
    _file(home, "consolidated", NOW - timedelta(hours=60), occurrence="occ-1", day="2026-11-12")
    _file(home, "failed", NOW - timedelta(hours=36), occurrence="occ-2", day="2026-11-13")
    _file(home, "did-not-consolidate", NOW - timedelta(hours=8), occurrence="occ-3", day="2026-11-14")
    assert _dream_lines(_brief(home)) == ["Dream: no dream since 2026-11-12T20:00:00.000000Z (60h00m ago); the night after it "
                                          "failed (2026-11-13T20:00:00.000000Z)"]


def test_a_dream_read_that_raises_names_itself_and_leaves_the_brief_undegraded(home, monkeypatch):
    from levain.desk import dream_status

    _file(home, "consolidated", NOW - timedelta(hours=8))
    s = CoordStore(home, clock=lambda: NOW)
    s._connect().close()
    ok = seam.open_brief(s, home, "light", NOW)

    def boom(conn):
        raise RuntimeError("cards unreadable")

    monkeypatch.setattr(dream_status, "records", boom)
    got = seam.open_brief(s, home, "light", NOW)
    assert any(ln.startswith("Dream: cannot-derive (RuntimeError") for ln in got.text.splitlines())
    assert got.degraded is None and ok.degraded is None
    assert [ln for ln in got.text.splitlines() if not ln.startswith("Dream")] == \
           [ln for ln in ok.text.splitlines() if not ln.startswith("Dream")]


def test_the_seam_notes_close_command_is_copy_pasteable():
    import shlex

    from levain.desk import dream_status

    at = "2026-11-15T01:00:00.000000Z"
    note = dream_status.seam_note([dream_status.DreamRecord("consolidated", "2026-11-15T02:00:00.000000Z", "o")], at,
                                  "levain", " --queue-dir '/a b/q'")
    cmd = note.split("`")[1]
    argv = shlex.split(cmd)
    assert argv[argv.index("--token") + 1] == "N" and argv[argv.index("--queue-dir") + 1] == "/a b/q"
    assert "N--queue-dir" not in cmd


def test_a_launch_with_no_start_stamp_leaves_the_dream_seam_silent(desk):
    from levain.desk import seam_verbs

    assert seam_verbs._dream_notes(desk, SimpleNamespace()) == []
