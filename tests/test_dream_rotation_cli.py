"""levain#82 slice 2: ``coord open --dream-body-file`` (the dream rotation's caller), driven through ``main([...])`` on
a real store: a real filed card, a real active desk launch, no process spawned."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.cli import main
from levain.desk import lease, seam_handoffs
from tests.test_dream_rotation import BODY, _handoffs, _launches
from tests.test_dream_seam import (_home_env, _requested, _saved_after, desk)  # noqa: F401


def _run(desk, capsys, *extra, rc=0, body=None, token=None):
    bf = desk.directory.parent / "dream_body.json"
    bf.write_text(json.dumps(BODY if body is None else body))
    args = ["coord", "open", "--token", str(desk.token if token is None else token), "--queue-dir",
            str(desk.directory), *extra]
    if "--no-body" not in extra:
        args += ["--dream-body-file", str(bf)]
    got = main([a for a in args if a != "--no-body"])
    cap = capsys.readouterr()
    assert got == rc, cap
    return cap


def _snapshot(desk):
    """Every row of every table that holds one (the file's bytes move, and empty tables appear, when ``main`` opens a
    store: neither is a row written): equal before and after is "no row written"."""
    c = sqlite3.connect(desk.path)
    try:
        names = [n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        rows = {n: c.execute(f"SELECT * FROM {n}").fetchall() for n in names}
        return {n: r for n, r in rows.items() if r}
    finally:
        c.close()


def _holder(desk):
    held = desk.coord_status()["lease"]
    return None if held is None else held["holder"]


def test_a_due_desk_opens_with_a_body_and_writes_one_dream_handoff_and_asks_for_the_next_desk(desk, capsys):
    _saved_after(desk)
    before = _launches(desk)
    out = _run(desk, capsys, "--json").out
    got = json.loads(out)
    assert got["rotated"] is True and got["occurrence"] == "occ-1"
    assert [h[1:4] for h in _handoffs(desk)] == [("dream-rotation:occ-1", "dream", desk.launch.session_key)]
    assert got["handoff_id"] == _handoffs(desk)[0][0]
    after = _launches(desk)
    assert [r[0] for r in after] == [*(r[0] for r in before), got["successor_launch_id"]]
    row = sqlite3.connect(desk.path).execute(
        "SELECT kind, cause FROM launch_events WHERE launch_id = ? ORDER BY seq DESC LIMIT 1",
        (desk.launch.launch_id,)).fetchone()
    assert row == ("ended", lease.DREAM_ROTATED)
    assert _holder(desk) != desk.launch.session_key
    pending = sqlite3.connect(desk.path).execute("SELECT kind FROM launch_events WHERE launch_id = ?",
                                                 (got["successor_launch_id"],)).fetchall()
    assert pending == [("pending",)]


def test_the_plain_output_names_the_handoff_and_the_successor(desk, capsys):
    _saved_after(desk)
    out = _run(desk, capsys).out
    h = _handoffs(desk)[0][0]
    succ = _launches(desk)[-1][0]
    assert f"handoff {h} (dream, occurrence occ-1)" in out and succ in out and "Stop here" in out


def test_a_retry_by_the_rotated_desk_is_refused_naming_its_handoff_and_writes_nothing(desk, capsys):
    _saved_after(desk)
    _run(desk, capsys)
    before = _snapshot(desk)
    cap = _run(desk, capsys, rc=1)
    h = _handoffs(desk)[0]
    assert f"already handed over onto the dream at handoff {h[0]}" in cap.err and h[1] in cap.err
    assert _snapshot(desk) == before and len(_handoffs(desk)) == 1


def test_a_handoff_already_stored_for_the_occurrence_is_refused_naming_it_and_the_desk_stays_live(desk, capsys):
    _saved_after(desk)
    stored = seam_handoffs.close(desk, kind="dream", body=BODY, request_id="dream-rotation:occ-1",
                                 prev_handoff_id=None, token=desk.token)
    before = _snapshot(desk)
    cap = _run(desk, capsys, "--json", rc=1)
    assert cap.out == "" and "this is not a rotation" in cap.err
    assert f"handoff {stored.handoff_id}" in cap.err and "dream-rotation:occ-1" in cap.err
    assert _snapshot(desk) == before and _holder(desk) == desk.launch.session_key
    assert len(_launches(desk)) == 1


def test_a_manual_close_for_an_older_dream_is_not_adopted_through_the_verb(desk, capsys):
    from datetime import timedelta

    from levain.cockpit.consent import parse_stamp

    _saved_after(desk, hours=0.001, occurrence="occ-A")
    _saved_after(desk, hours=0.002, occurrence="occ-B")
    desk._clock = lambda: parse_stamp(_requested(desk)) + timedelta(minutes=1)
    by_hand = seam_handoffs.close(desk, kind="dream", body=BODY, request_id="by-hand-A", prev_handoff_id=None,
                                  token=desk.token)
    got = json.loads(_run(desk, capsys, "--json").out)
    assert got["rotated"] is True and got["occurrence"] == "occ-B" and got["handoff_id"] != by_hand.handoff_id
    assert [h[1] for h in _handoffs(desk)] == ["by-hand-A", "dream-rotation:occ-B"]


def test_a_desk_that_is_not_due_writes_nothing(desk, capsys):
    before = _snapshot(desk)
    got = json.loads(_run(desk, capsys, "--json").out)
    assert got["rotated"] is False and "no dream has saved memory" in got["why"]
    assert _snapshot(desk) == before and _handoffs(desk) == []


def test_a_body_that_attests_the_operator_is_present_writes_nothing(desk, capsys):
    _saved_after(desk)
    before = _snapshot(desk)
    got = json.loads(_run(desk, capsys, "--json", body={**BODY, "attested_operator_present": True}).out)
    assert got["rotated"] is False and "operator is present" in got["why"]
    assert _snapshot(desk) == before


@pytest.mark.parametrize("extra", [["--light"], ["--ack", "1"], ["--gap-hours", "2"], ["--light", "--gap-hours", "2"]])
def test_the_flag_is_refused_with_light_ack_or_gap_hours_and_writes_nothing(desk, capsys, extra):
    _saved_after(desk)
    before = _snapshot(desk)
    cap = _run(desk, capsys, *extra, rc=1)
    assert "--dream-body-file hands this desk over" in cap.err and "nothing was written" in cap.err
    assert _snapshot(desk) == before and _handoffs(desk) == []


def test_the_flag_is_refused_with_a_read_back_file(desk, capsys, tmp_path):
    _saved_after(desk)
    rb = tmp_path / "rb.txt"
    rb.write_text("x")
    before = _snapshot(desk)
    cap = _run(desk, capsys, "--read-back-file", str(rb), rc=1)
    assert "takes no" in cap.err and _snapshot(desk) == before


def test_a_token_that_is_not_the_live_lease_is_refused_and_writes_nothing(desk, capsys):
    _saved_after(desk)
    before = _snapshot(desk)
    cap = _run(desk, capsys, rc=1, token=desk.token + 1000)
    assert "a dream rotation is the live desk's" in cap.err
    assert _snapshot(desk) == before and _handoffs(desk) == []


def test_a_malformed_body_file_is_refused_and_writes_nothing(desk, capsys):
    _saved_after(desk)
    before = _snapshot(desk)
    cap = _run(desk, capsys, rc=1, body={"state_line": "x"})
    assert "handoff body" in cap.err or "key" in cap.err
    assert _snapshot(desk) == before and _handoffs(desk) == []


def test_a_wake_that_asks_for_nothing_rolls_back_and_the_desk_keeps_its_lease(desk, capsys):
    _saved_after(desk)
    desk.coord_set_agent("none")
    before = _snapshot(desk)
    cap = _run(desk, capsys, rc=1)
    assert "no next desk could be asked for" in cap.err
    assert _snapshot(desk) == before and _handoffs(desk) == []
    assert _holder(desk) == desk.launch.session_key


def test_a_plain_open_on_a_due_desk_only_prints_the_note(desk, capsys):
    _saved_after(desk)
    got = main(["coord", "open", "--light", "--token", str(desk.token), "--queue-dir", str(desk.directory)])
    out = capsys.readouterr().out
    assert got == 0 and "Dream seam:" in out and "--dream-body-file PATH" in out
    assert "Nothing rotates you on its own" in out
    assert _handoffs(desk) == [] and _holder(desk) == desk.launch.session_key
    assert len(_launches(desk)) == 1


def test_the_launch_brief_names_the_flag():
    from levain.desk import launch as cl

    text = "\n".join(cl._arrival_section("levain", ""))
    assert "coord open --dream-body-file PATH --token N" in text
