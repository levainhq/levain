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
from levain.desk.legacy import ROLE_DESK
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
    assert "never while the operator is typing" in line and "never in the middle of work" in line
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


# --- levain#81: rotation_due, the read-only predicate the rotation will ask --------------------------------------------
def _requested(desk):
    """The launch's ``launches.at`` (its request stamp): the start rotation_due compares a save against."""
    return sqlite3.connect(desk.path).execute("SELECT at FROM launches WHERE launch_id = ?",
                                              (desk.launch.launch_id,)).fetchone()[0]


def _due(desk):
    from levain.desk import dream_status

    return dream_status.rotation_due(desk, desk.launch, at_turn_boundary=True)


def _saved_after(desk, hours=1, occurrence="occ-1"):
    _file(desk.directory, "consolidated", parse_stamp(_requested(desk)) + timedelta(hours=hours), occurrence=occurrence)


def _snapshot(directory):
    """Every file under the store's directory: its bytes and its mtime (ns). Equal before and after is "wrote
    nothing". Two SQLite sidecars are read differently, and only these: a ``mode=ro`` reader of a WAL database maps
    the ``-shm`` index (shared memory where every reader keeps its read mark, never store content: left out) and
    opens a ``-wal`` it cannot delete, so an absent ``-wal`` and an empty one are the same content, and the ``-wal`` is
    compared by its bytes (a page written through it changes them)."""
    snap = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(directory.iterdir())
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}
    wal = [p for p in directory.iterdir() if p.name.endswith("-wal")]
    snap["<wal bytes>"] = b"".join(p.read_bytes() for p in sorted(wal))
    return snap


def test_rotation_is_due_only_at_a_seam_after_a_real_save(desk):
    assert _due(desk)[0] is False                       # nothing saved yet
    _saved_after(desk)
    due, why = _due(desk)
    assert due is True and why.startswith("at a seam:") and "occurrence occ-1" in why


def test_rotation_is_not_due_mid_work_while_the_desk_holds_a_claimed_event(desk, tmp_path):
    from levain.desk import events

    _saved_after(desk)
    path = tmp_path / "mon.cred"
    events.register(desk, "mon", "first_party", path)
    ev = events.post(desk, "mon", 1, "site_status", {"subject": "taosrv", "state": "down"},
                     credential=events.read_credential(path)).event
    assert [e.event_id for e in events.claim_for_launch(desk, desk.launch.launch_id)] == [ev.event_id]
    due, why = _due(desk)
    assert due is False and why.startswith("mid-work:") and ev.event_id in why
    events.ack(desk, ev.event_id, "looked: the site is back", token=desk.token)
    assert _due(desk)[0] is True                        # the claim was the only thing standing in the way


def test_rotation_is_not_due_with_an_open_turn_an_unacknowledged_open(desk):
    from levain.desk import seam_build, seam_open

    _saved_after(desk)
    b = seam_build.build(desk, kind="light", signal_kind="silence", signal_key="k-81", queue_dir=desk.directory,
                         token=desk.token)
    assert seam_open.record_delivery(desk, b.open_id, "light_verb", recipient=desk.launch.launch_id)
    due, why = _due(desk)
    assert due is False and why == f"mid-work: open {b.open_id} was handed to this desk and this desk has not acknowledged it"
    seam_open.acknowledge(desk, b.open_id, token=desk.token)
    assert _due(desk)[0] is True                        # the store's half; the turn boundary is the caller's


def test_rotation_is_not_due_when_the_save_predates_the_launch(desk):
    _saved_after(desk, hours=-1)
    due, why = _due(desk)
    assert due is False and "is not after this desk started" in why


def test_rotation_is_not_due_after_a_rollback_of_the_save(desk):
    _saved_after(desk, hours=1, occurrence="occ-1")
    _file(desk.directory, "rolled-back", parse_stamp(_requested(desk)) + timedelta(hours=2), occurrence="occ-1",
          reason="rolled back by operator")
    due, why = _due(desk)
    assert due is False and "was rolled back" in why


def test_rotation_is_not_due_for_a_shadow_night(desk, tmp_path):
    from levain.dream import shadow

    day = SimpleNamespace(day="2026-11-14", run_id="r", run_dir=tmp_path)
    c = shadow.shadow_card(day, "shadow\n", occurrence="occ-s", occ_dir=_dir(tmp_path, "occ"), flow_text="flow\n")
    when = parse_stamp(_requested(desk)) + timedelta(hours=1)
    review.file_item(CoordStore(desk.directory, clock=lambda: when), "agent", c.body, dedupe_key=c.dedupe_key)
    due, why = _due(desk)
    assert due is False and why.startswith("no dream has saved memory")


# the timestamp boundary: a save is compared with the launch's REQUEST stamp (launches.at), as fixed-width text.
# Equal is not after. The request comes before the session starts and loads its memory, so the error direction is
# toward an extra rotation (a save between request and load is called due), never toward a missed one.
@pytest.mark.parametrize("offset, due", [
    (timedelta(microseconds=-1), False),     # a hair before the request: the desk loaded it
    (timedelta(0), False),                   # equal stamps: not after
    (timedelta(microseconds=1), True),       # a hair after the request
    (timedelta(hours=1), True),
])
def test_rotation_due_timestamp_boundary(desk, offset, due):
    _file(desk.directory, "consolidated", parse_stamp(_requested(desk)) + offset)
    assert _due(desk)[0] is due


def test_a_save_between_the_request_and_the_activation_is_called_due(desk):
    # skew direction: the session may already hold this save (it loads after the request), so the call is an extra,
    # harmless rotation onto the same memory; the opposite error (a save after the load read as not due) cannot happen.
    # The same holds for a save made before the request whose card is filed after it (a card's ``at`` is its filing,
    # seconds after the save; codex HIGH, L3 r1): the store records neither the save's own stamp nor the memory a launch
    # loaded, so this is the input's limit, not the predicate's. It is the extra-rotation direction, at a real seam
    requested, active = parse_stamp(_requested(desk)), parse_stamp(_started(desk))
    assert requested < active
    _file(desk.directory, "consolidated", requested + (active - requested) / 2)
    assert _due(desk)[0] is True


@pytest.mark.parametrize("seam_reached", [False, True])
def test_rotation_due_writes_nothing(desk, seam_reached):
    if seam_reached:
        _saved_after(desk)
    before = _snapshot(desk.directory)
    assert desk.path.name in before                      # the store is a file, and it is in the snapshot
    assert _due(desk)[0] is seam_reached
    assert _snapshot(desk.directory) == before           # byte-identical, mtimes included; the WAL's bytes too


def test_rotation_due_fails_closed_on_a_launch_that_is_not_an_active_desk_or_a_store_it_cannot_read(desk, tmp_path):
    from levain.desk import dream_status

    _saved_after(desk)
    assert dream_status.rotation_due(desk, SimpleNamespace(), at_turn_boundary=True)[0] is False
    assert dream_status.rotation_due(desk, SimpleNamespace(launch_id="ln-" + "0" * 32), at_turn_boundary=True) == \
        (False, f"no launch ln-{'0' * 32} in this store")
    due, why = dream_status.rotation_due(CoordStore(tmp_path / "nowhere"), desk.launch, at_turn_boundary=True)
    assert due is False and why.startswith("cannot-derive (")


# --- levain#81 L3 round 1: the seam's definition, per-session acks, the caller's re-check, a strict card read -------
def _successor(desk):
    """The desk ends (a done session replaced) and the next desk launch is activated; it holds the lease now."""
    desk.launch_replace_done(desk.launch.launch_id, asked=True)
    nxt = desk.pending_desk()
    desk.launch_claim_spawn(nxt.launch_id, os.getpid())
    got, why = desk.launch_activate(nxt.launch_id, harness_session_id="h-" + nxt.launch_id)
    assert why is None and got.state == "active"
    return got, desk.coord_status()["lease"]["token"]


def _light_open(desk, key, token=None):
    from levain.desk import seam_build

    return seam_build.build(desk, kind="light", signal_kind="silence", signal_key=key, queue_dir=desk.directory,
                            token=desk.token if token is None else token)


def test_an_ack_by_another_session_does_not_close_the_open_handed_to_this_desk(desk):
    # codex HIGH (dream_status.py:173): open_acks is keyed (open_id, reader_session). The open the earlier desk
    # acknowledged is handed to the next desk too (a stored build is returned, never rebuilt); the next desk has
    # not taken it in, so it is mid-arrival
    from levain.desk import dream_status, seam_open

    b = _light_open(desk, "k-shared")
    seam_open.record_delivery(desk, b.open_id, "light_verb", recipient=desk.launch.launch_id)
    seam_open.acknowledge(desk, b.open_id, token=desk.token)
    nxt, token = _successor(desk)
    seam_open.record_delivery(desk, b.open_id, "light_verb", recipient=nxt.launch_id)
    _file(desk.directory, "consolidated", parse_stamp(nxt.at) + timedelta(hours=1))
    due, why = dream_status.rotation_due(desk, nxt, at_turn_boundary=True)
    assert due is False and why == f"mid-work: open {b.open_id} was handed to this desk and this desk has not acknowledged it"
    desk.token = token
    seam_open.acknowledge(desk, b.open_id, token=token)
    assert dream_status.rotation_due(desk, nxt, at_turn_boundary=True)[0] is True


def test_an_acknowledged_open_is_not_a_finished_turn_the_caller_reads_the_boundary(desk):
    # codex HIGH (dream_status.py:226): the ack is the turn's FIRST act. The store records no desk turn ending, so a
    # store with nothing held is half a seam; the other half (the harness is between turns) is the caller's input
    from levain.desk import dream_status, seam_open

    _saved_after(desk)
    b = _light_open(desk, "k-turn")
    seam_open.record_delivery(desk, b.open_id, "light_verb", recipient=desk.launch.launch_id)
    seam_open.acknowledge(desk, b.open_id, token=desk.token)
    due, why = dream_status.rotation_due(desk, desk.launch, at_turn_boundary=False)
    assert due is False and why.startswith("mid-turn:")
    assert dream_status.rotation_due(desk, desk.launch, at_turn_boundary=True)[0] is True
    for not_a_bool in (1, "yes", None):              # only True says "between turns"
        assert dream_status.rotation_due(desk, desk.launch, at_turn_boundary=not_a_bool)[0] is False
    with pytest.raises(TypeError):                   # no default: a caller says which it is
        dream_status.rotation_due(desk, desk.launch)


def test_a_claim_after_a_true_answer_is_seen_by_the_callers_re_check_in_its_own_transaction(desk, tmp_path):
    # codex HIGH (dream_status.py:201): the answer is one snapshot; a claim after it is inherent. The caller re-checks
    # with seam_check on ITS write connection, inside the transaction that would rotate, and sees the claim
    from levain.desk import dream_status, events

    _saved_after(desk)
    assert dream_status.rotation_due(desk, desk.launch, at_turn_boundary=True)[0] is True
    path = tmp_path / "mon.cred"
    events.register(desk, "mon", "first_party", path)
    ev = events.post(desk, "mon", 1, "site_status", {"subject": "taosrv", "state": "down"},
                     credential=events.read_credential(path)).event
    with desk._write(ROLE_DESK, desk.token) as (conn, now):
        assert dream_status.seam_check(conn, desk.launch.launch_id, now, at_turn_boundary=True)[0] is True
        events._state(conn, now, ev.event_id, "claimed", launch_id=desk.launch.launch_id, claim_until=now)
        due, why = dream_status.seam_check(conn, desk.launch.launch_id, now, at_turn_boundary=True)
        assert due is False and ev.event_id in why     # the uncommitted claim, in the caller's own transaction
    assert dream_status.rotation_due(desk, desk.launch, at_turn_boundary=True)[0] is False


def test_a_desk_that_does_not_hold_the_live_lease_is_not_the_live_desk(desk):
    # codex MED (dream_status.py:205): a release leaves the launch active; it is no longer the live desk
    from levain.desk import dream_status

    _saved_after(desk)
    assert _due(desk)[0] is True
    desk.coord_release(desk.token, by=ROLE_DESK)
    due, why = _due(desk)
    assert due is False and "does not hold the live lease (the lease is not held)" in why


def test_a_rollback_card_the_predicate_cannot_read_is_cannot_derive_never_a_seam(desk):
    # codex MED (dream_status.py:82): the brief skips an unreadable card; the predicate must not, or a rollback it
    # cannot pair with its save leaves the save live and calls the desk due
    from levain.desk import dream_status

    _saved_after(desk, occurrence="occ-1")
    when = parse_stamp(_requested(desk)) + timedelta(hours=2)
    review.file_item(CoordStore(desk.directory, clock=lambda: when), "agent",
                     {"kind": "rolled-back", "occurrence": "occ-1"}, dedupe_key="dream-rollback:occ-1")
    due, why = dream_status.rotation_due(desk, desk.launch, at_turn_boundary=True)
    assert due is False and why.startswith("cannot-derive (") and "dream-rollback:occ-1" in why
    # the display read keeps its old reading: a rollback paired with no occurrence, so the save stands there
    assert [(r.kind, r.occurrence) for r in dream_status.records(sqlite3.connect(desk.path))] == \
        [("rolled-back", ""), ("consolidated", "occ-1")]


def test_a_stamp_the_predicate_cannot_compare_is_cannot_derive_not_a_raise(desk, monkeypatch):
    # deepseek MED-1: every step after the read sits inside the fail-closed handler
    from levain.desk import dream_status

    _saved_after(desk)
    monkeypatch.setattr(dream_status, "records",
                        lambda conn, strict=False: [dream_status.DreamRecord("consolidated", None, "occ-1")])
    due, why = dream_status.rotation_due(desk, desk.launch, at_turn_boundary=True)
    assert due is False and why.startswith("cannot-derive (TypeError")


def test_saves_all_rolled_back_before_the_desk_started_are_named_as_such(desk):
    # deepseek MED-2: a save was made and undone; "no dream has saved memory" would send the reader looking for one
    from levain.desk import dream_status

    start = parse_stamp(_requested(desk))
    _file(desk.directory, "consolidated", start - timedelta(hours=3), occurrence="occ-1")
    _file(desk.directory, "rolled-back", start - timedelta(hours=2), occurrence="occ-1", reason="rolled back by operator")
    due, why = dream_status.rotation_due(desk, desk.launch, at_turn_boundary=True)
    assert due is False and why.startswith("every dream save in this store was rolled back")


# --- levain#83: the dream rotation end to end -------------------------------------------------------------------------
# One sequence on real stores: a real dream save (``consolidate_store``, a canned mind) into a real anneal store, the dream's
# own card filed into the desk's review store, a due seam, the rotation, the next desk's launch request, its activation,
# and what that desk reads. The launched desk's memory is the file its install's CLAUDE.md imports
# (``@.levain/memory.continuity.md``), so "loaded" below is a read of that path under the SUCCESSOR launch's own cwd.
_DREAMED = """\
## State
The dream of the 14th: the freshly saved memory a desk must load, with a marker the test reads back: PELICAN-83.

## Active Threads
- Getting my bearings: pointer: this session.

## Patterns
Nothing graduated yet.

## Decisions
Nothing committed yet.

## Context
The dream metabolized the opening episodes.

## Understanding
Early days.
"""


class _CannedMind:
    name, source, unit, model = "canned", "canned", "tokens", "canned"

    def compose(self, req):
        from levain.dream import executor as ex

        return ex.ComposeResult(text=_DREAMED, unit="tokens", input_tokens=1, output_tokens=1)


def _dream_saves(install, tmp_path):
    """A real dream save into ``install/.levain/memory.db``: returns the text anneal says it saved and the occurrence."""
    from anneal_memory import FLOW_SCHEMA, Store

    from levain.dream import executor as ex

    store_dir = install / ".levain"
    store_dir.mkdir(parents=True)
    with Store(str(store_dir / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
    got = ex.consolidate_store(store_dir / "memory.db", mind=_CannedMind(), occurrence_dir=tmp_path / "occ")
    assert (got.outcome, got.exit_code) == ("saved", 0), got
    with Store(str(store_dir / "memory.db"), read_only=True) as s:
        saved = s.load_continuity()
    assert "PELICAN-83" in saved
    return saved, "occ-83"


def _loaded_memory(launch):
    """What the next desk reads as its memory: the file the install's CLAUDE.md imports, under the launch's own cwd."""
    from pathlib import Path

    template = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "adapters" / "claude-code"
                / "CLAUDE.md.template").read_text(encoding="utf-8")
    assert "@.levain/memory.continuity.md" in template.splitlines()
    return (Path(launch.cwd) / ".levain" / "memory.continuity.md").read_bytes().decode("utf-8")


@pytest.fixture
def dreamed(desk, tmp_path):
    """The desk's agent runs in an install whose memory a dream then saves; the dream's card is filed after the desk
    started. Returns (saved text, occurrence)."""
    install = _dir(tmp_path, "install")
    desk.coord_set_agent("claude", cwd=str(install))
    saved, occ = _dream_saves(install, tmp_path)
    _file(desk.directory, "consolidated", parse_stamp(_started(desk)) + timedelta(hours=1), occurrence=occ)
    return saved, occ


def _activate_successor(desk, successor):
    """What the watcher does for a pending desk launch: spawn claimed, session activated. Returns the new lease token."""
    assert desk.pending_desk().launch_id == successor.launch_id
    desk.launch_claim_spawn(successor.launch_id, os.getpid())
    got, why = desk.launch_activate(successor.launch_id, harness_session_id="h-" + successor.launch_id)
    assert why is None and got.state == "active"
    return desk.coord_status()["lease"]["token"]


def test_a_dream_save_rotates_the_desk_and_the_next_desk_loads_the_saved_text_and_the_dream_open(desk, dreamed, capsys):
    from levain.desk import dream_rotation

    saved, occ = dreamed
    # the seam is due on the dream's card (the step between the save and the rotation), and the rotation names that save
    assert _due(desk)[0] is True
    got = dream_rotation.rotate(desk, token=desk.token, body=BODY)
    assert isinstance(got, dream_rotation.Rotation) and got.occurrence == occ
    assert got.handoff.seam_kind == "dream" and got.handoff.close_key == f"dream-rotation:{occ}"
    token = _activate_successor(desk, got.successor)
    # the new desk's loaded memory is the saved text, byte for byte
    assert _loaded_memory(got.successor) == saved
    # its open is the dream open, built from the rotation's handoff
    desk.token = token
    out = _open(desk, capsys)
    assert "SEAM OPEN dream" in out and "--read-back-file PATH" in out
    row = sqlite3.connect(desk.path).execute("SELECT seam_kind, signal_kind, signal_key FROM open_builds").fetchone()
    assert row == ("dream", "dream", str(got.handoff.handoff_id))


def _has_open_wiring():
    from pathlib import Path

    from levain.desk import cli as desk_cli

    return "--dream-body-file" in Path(desk_cli.__file__).read_text(encoding="utf-8")


def test_coord_open_with_a_dream_body_file_hands_over_and_the_next_desk_loads_the_saved_text(desk, dreamed, tmp_path, capsys):
    # THROUGH ``coord open``: needs levain#82 slice 2 (the open wiring, branch seat/1116-26-r82b). A probe, not a skip mark:
    # it skips while the flag is absent and runs the moment the branch carries it.
    if not _has_open_wiring():
        pytest.skip("needs levain#82 slice 2 (the open wiring), branch seat/1116-26-r82b")
    saved, occ = dreamed
    bf = tmp_path / "rot.json"
    bf.write_text(json.dumps(BODY))
    rotated = json.loads(_open(desk, capsys, "--dream-body-file", str(bf), "--json"))
    assert rotated["rotated"] is True and rotated["occurrence"] == occ
    successor = desk.pending_desk()
    assert successor.launch_id == rotated["successor_launch_id"]
    token = _activate_successor(desk, successor)
    assert _loaded_memory(successor) == saved
    desk.token = token
    out = _open(desk, capsys)
    assert "SEAM OPEN dream" in out
    row = sqlite3.connect(desk.path).execute("SELECT seam_kind, signal_kind, signal_key FROM open_builds").fetchone()
    assert row == ("dream", "dream", str(rotated["handoff_id"]))
