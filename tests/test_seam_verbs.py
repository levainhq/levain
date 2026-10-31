"""C15 S3: ``coord close``, ``coord open`` (and ``--ack``), ``coord handoff show`` through the CLI entry, and the launch
brief's embedded open. Every expectation was first read from a real run on a scratch store (an active coordinator launch
holding the lease), the round trip printed in full by the lane's run script."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from levain.cli import main
from levain.coordinator import launch as cl
from levain.coordinator import seam_handoffs, seam_open
from levain.coordinator.store import CoordStore


def _body(**over):
    body = {"state_line": "main is green", "in_flight": [{"item": "S3", "owner": "seat", "why": "review"}],
            "next": "open the queue", "contingencies": [{"if": "ci red", "then": "revert"}], "notes": "n",
            "attested_operator_present": False, "attested_walked": False}
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return d


def _activate(store):
    d, _ = store.accept_drop("words", surface="cli")
    lid = store.woken_launch(d.drop_id)
    store.launch_claim_spawn(lid, os.getpid())
    got, why = store.launch_activate(lid, harness_session_id="h-" + lid)
    assert why is None and got.state == "active"
    return got, store.coord_status()["lease"]["token"]


@pytest.fixture
def world(qdir, tmp_path):
    s = CoordStore(qdir)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.launch, s.token = _activate(s)
    return s


def run(qdir, capsys, *argv, rc=0):
    got = main(["coord", *argv, "--queue-dir", str(qdir)])
    out = capsys.readouterr()
    assert got == rc, (argv, out.out, out.err)
    return out


def body_file(tmp_path, body=None, name="body.json"):
    p = tmp_path / name
    p.write_text(json.dumps(_body() if body is None else body))
    return p


def sql(store, query, *args):
    c = sqlite3.connect(store.path)
    try:
        return c.execute(query, args).fetchall()
    finally:
        c.close()


# --- coord close ---------------------------------------------------------------------------------------------------
def test_close_writes_a_handoff_and_a_retry_names_the_same_one(world, qdir, tmp_path, capsys):
    bf = body_file(tmp_path)
    args = ("close", "--kind", "rotation", "--body-file", str(bf), "--request-id", "r1", "--token", str(world.token))
    out = run(qdir, capsys, *args)
    assert "closed: handoff 1 (rotation)" in out.out and world.launch.session_key in out.out
    assert run(qdir, capsys, *args).out == out.out and len(sql(world, "SELECT 1 FROM seam_handoffs")) == 1
    j = json.loads(run(qdir, capsys, *args, "--json").out)
    assert j["handoff_id"] == 1 and j["prev_handoff_id"] is None


def test_close_refusals_exit_one_on_stderr_and_write_nothing(world, qdir, tmp_path, capsys):
    bf = body_file(tmp_path)
    ok = ("close", "--kind", "rotation", "--body-file", str(bf), "--request-id", "r1")
    err = run(qdir, capsys, *ok, "--token", str(world.token + 99), rc=1).err
    assert err.startswith("levain coord close:") and "not the live lease's" in err
    err = run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(tmp_path / "nope.json"),
              "--request-id", "r1", "--token", str(world.token), rc=1).err
    assert "cannot read" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    err = run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(bad), "--request-id", "r1",
              "--token", str(world.token), rc=1).err
    assert "not one JSON object" in err
    dup = tmp_path / "dup.json"
    dup.write_text('{"state_line": "a", "state_line": "b"}')
    assert "appears twice" in run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(dup),
                                  "--request-id", "r1", "--token", str(world.token), rc=1).err
    short = body_file(tmp_path, {"state_line": "x"}, "short.json")
    assert "exactly the keys" in run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(short),
                                     "--request-id", "r1", "--token", str(world.token), rc=1).err
    assert sql(world, "SELECT COUNT(*) FROM seam_handoffs") == [(0,)]
    run(qdir, capsys, *ok, "--token", str(world.token))
    err = run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(bf), "--request-id", "r2",
              "--token", str(world.token), rc=1).err
    assert "latest is 1" in err                                        # no --prev-handoff-id when one exists
    run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(bf), "--request-id", "r2",
        "--prev-handoff-id", "1", "--token", str(world.token))


def test_close_has_no_plan_flag_yet(qdir, capsys):
    with pytest.raises(SystemExit):
        main(["coord", "close", "--plan", "--queue-dir", str(qdir)])
    capsys.readouterr()


# --- coord open ----------------------------------------------------------------------------------------------------
def _rotation_close(world, qdir, tmp_path, capsys):
    run(qdir, capsys, "close", "--kind", "rotation", "--body-file", str(body_file(tmp_path)), "--request-id", "r1",
        "--token", str(world.token))


def test_open_with_no_lease_prints_read_only_and_cannot_be_acked(world, qdir, tmp_path, capsys):
    _rotation_close(world, qdir, tmp_path, capsys)
    out = run(qdir, capsys, "open").out
    assert "SEAM OPEN rotation" in out and "cannot be acknowledged (no --token was given)" in out
    assert "--prev-handoff-id 1" in out and "coord open --ack" not in out
    assert sql(world, "SELECT COUNT(*) FROM open_builds") == [(0,)] and sql(world, "SELECT COUNT(*) FROM open_deliveries") == [(0,)]
    world.coord_release(world.token, by="coordinator")
    op = world.coord_take()
    out = run(qdir, capsys, "open", "--token", str(op)).out
    assert "an operator token never acknowledges" in out and sql(world, "SELECT COUNT(*) FROM open_builds") == [(0,)]


def test_open_for_the_coordinator_builds_delivers_and_names_the_ack_line(world, qdir, tmp_path, capsys):
    _rotation_close(world, qdir, tmp_path, capsys)
    out = run(qdir, capsys, "open", "--token", str(world.token)).out
    lines = out.strip().splitlines()
    assert "SEAM OPEN rotation" in out and lines[-1] == "your next close names --prev-handoff-id 1"
    assert f"levain coord open --ack 1 --read-back-file PATH --token {world.token} --queue-dir {qdir}" in lines[-2]
    assert sql(world, "SELECT open_id, surface, recipient_launch_id FROM open_deliveries") == [
        (1, "light_verb", world.launch.launch_id)]
    again = run(qdir, capsys, "open", "--token", str(world.token)).out
    assert again.split("\n--\n")[0] == out.split("\n--\n")[0] and sql(world, "SELECT COUNT(*) FROM open_builds") == [(1,)]


def test_open_with_no_handoff_is_read_only_and_says_why(world, qdir, capsys):
    out = run(qdir, capsys, "open", "--token", str(world.token)).out
    assert "SEAM OPEN light" in out and "no open is built for this launch and the latest handoff is absent" in out
    assert "no handoff exists yet" in out


def test_read_back_file_without_ack_is_refused(world, qdir, tmp_path, capsys):
    err = run(qdir, capsys, "open", "--read-back-file", str(tmp_path / "x"), rc=1).err
    assert "goes with --ack" in err


# --- coord open --ack ----------------------------------------------------------------------------------------------
def test_ack_refusals_then_the_ack_moves_the_window(world, qdir, tmp_path, capsys):
    _rotation_close(world, qdir, tmp_path, capsys)
    tok = str(world.token)
    run(qdir, capsys, "open", "--token", tok)
    assert "add --token N" in run(qdir, capsys, "open", "--ack", "1", rc=1).err
    err = run(qdir, capsys, "open", "--ack", "1", "--token", tok, rc=1).err
    assert "needs" not in err and "read-back" in err and "--read-back-file PATH" in err
    assert "there is no open build 9" in run(qdir, capsys, "open", "--ack", "9", "--token", tok, rc=1).err
    assert "not the live lease's" in run(qdir, capsys, "open", "--ack", "1", "--token", str(world.token + 5), "--read-back-file",
                                         str(body_file(tmp_path)), rc=1).err
    assert sql(world, "SELECT COUNT(*) FROM open_acks") == [(0,)]
    rb = tmp_path / "rb.txt"
    rb.write_text("main is green; I open the queue")
    got = run(qdir, capsys, "open", "--ack", "1", "--read-back-file", str(rb), "--token", tok).out
    assert got.startswith("acknowledged open 1 as ") and "starts after gseq" in got
    cursor = sql(world, "SELECT shown_through_gseq FROM open_builds")[0][0]
    assert f"after gseq {cursor}" in got
    assert "already acknowledged" in run(qdir, capsys, "open", "--ack", "1", "--token", tok).out
    assert sql(world, "SELECT read_back FROM open_acks") == [("main is green; I open the queue",)]
    # the window the next open shows starts after the ack
    world.accept_drop("later words", surface="cli")
    assert f"the window since opens after gseq {cursor}" in run(qdir, capsys, "open").out


def test_a_light_ack_needs_no_read_back(world, qdir, capsys):
    b = seam_open_light(world, qdir)
    out = run(qdir, capsys, "open", "--ack", str(b), "--token", str(world.token)).out
    assert out.startswith(f"acknowledged open {b} as ")


def seam_open_light(world, qdir):
    """A light build delivered to the launch (S4 owns a light signal's builder; the build and delivery are S2's)."""
    from levain.coordinator import seam_build

    b = seam_build.build(world, kind="light", signal_kind="silence", signal_key="d-1", queue_dir=qdir,
                         token=world.token)
    seam_open.record_delivery(world, b.open_id, "light_verb", recipient=world.launch.launch_id)
    return b.open_id


# --- coord handoff show --------------------------------------------------------------------------------------------
def test_handoff_show_latest_named_and_md(world, qdir, tmp_path, capsys):
    assert "no handoff exists yet" in run(qdir, capsys, "handoff", "show", rc=1).err
    _rotation_close(world, qdir, tmp_path, capsys)
    run(qdir, capsys, "close", "--kind", "dream", "--body-file", str(body_file(tmp_path, _body(state_line="second"))),
        "--request-id", "r2", "--prev-handoff-id", "1", "--token", str(world.token))
    latest = run(qdir, capsys, "handoff", "show").out
    assert latest.startswith("handoff 2 (dream)") and "state: second" in latest
    first = run(qdir, capsys, "handoff", "show", "1").out
    assert first.startswith("handoff 1 (rotation)") and "- if ci red: revert" in first and "previous handoff none" in first
    md = run(qdir, capsys, "handoff", "show", "--md", "1").out
    assert md.startswith("# Handoff 1 (rotation)") and "## Contingencies" in md and "- S3 (owner seat): review" in md
    assert json.loads(run(qdir, capsys, "handoff", "show", "--json").out)["body"]["state_line"] == "second"
    assert "there is no handoff 7" in run(qdir, capsys, "handoff", "show", "7", rc=1).err


# --- the launch brief ----------------------------------------------------------------------------------------------
@pytest.fixture
def pending(qdir, tmp_path):
    s = CoordStore(qdir)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    d, _ = s.accept_drop("first words", surface="cli")
    s.launch_id = s.woken_launch(d.drop_id)
    return s


def test_the_brief_embeds_the_open_and_the_ack_first_act(pending):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    text = cl._brief_text(pending, pending.launch(pending.launch_id))
    head, rest = text.split("## The open (your first act)")
    assert "## The lease" in rest and "## The lease" not in head          # the open comes before the lease section
    assert "SEAM OPEN light" in rest and "coord open --ack 1 --token N --queue-dir" in rest
    assert "coord close --kind rotation|operator" in rest
    assert "## Your writes" in text and "## The drops waiting" in text    # the existing content is kept
    assert sql(pending, "SELECT signal_kind, signal_key FROM open_builds") == [("launch", pending.launch_id)]
    cl.record_brief_delivery(pending, pending.launch_id)
    cl.record_brief_delivery(pending, pending.launch_id)                   # a repeat writes nothing more
    assert sql(pending, "SELECT open_id, surface, recipient_launch_id FROM open_deliveries") == [
        (1, "launch_brief", pending.launch_id)]


def test_the_brief_says_plainly_when_the_open_could_not_be_built_and_still_carries_the_rest(pending):
    text = cl._brief_text(pending, pending.launch(pending.launch_id))      # no spawn claim: starter_build refuses
    assert "Levain could not build your open:" in text and "not claimed" in text
    assert "SEAM OPEN" not in text and "coord open --ack" not in text
    assert "## The lease" in text and "## The drops waiting" in text
    assert sql(pending, "SELECT COUNT(*) FROM open_builds") == [(0,)]
    cl.record_brief_delivery(pending, pending.launch_id)                   # nothing to deliver: silent
    assert sql(pending, "SELECT COUNT(*) FROM open_deliveries") == [(0,)]


def test_the_cold_start_ack_round_trip(pending, qdir, capsys):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    cl._brief_text(pending, pending.launch(pending.launch_id))
    cl.record_brief_delivery(pending, pending.launch_id)
    got, why = pending.launch_activate(pending.launch_id, harness_session_id="h")
    assert why is None
    tok = str(pending.coord_status()["lease"]["token"])
    out = run(qdir, capsys, "open", "--ack", "1", "--token", tok).out
    assert out.startswith("acknowledged open 1 as ")
