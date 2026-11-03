"""C15 S5: the intake card (his first reply after he comes back) and `coord close --plan` (the operator-walk checklist).
Every expectation was first read from a real run on scratch installs (``s5/RUN.md`` in the lane's project memory): the
terminal path runs the installed hook ENTRY scripts as subprocesses with real hook JSON, the leave is a signed
``consent away off`` against a real cockpit, the phone path is ``coord reply``'s frozen block."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.cli import main
from levain.consent import auto_away
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx
from levain.consent.questions import QuestionStore
from levain.coordinator import drop_reply, intake, morning, ready, seam_plan, work_delta
from levain.coordinator import launch as cl
from levain.coordinator.store import CoordStore
from tests.test_consent_away import _cockpit
from tests.test_coordinator_wake_silent import SID, Rig
from tests.test_seam_ingest import World
from tests.test_seam_verbs import _body, _rewrite_writer, body_file

HEAD = work_delta.HEAD


@pytest.fixture(autouse=True)
def _at_the_laptop(monkeypatch):
    monkeypatch.setattr(auto_away, "input_idle_seconds", lambda: 5.0)


def sql(store, text, *args):
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute(text, args).fetchall()
    finally:
        conn.close()


class Arrival:
    """A coordinator store with an active launch, a signing cockpit, and the installed hook scripts to run its turns."""

    def __init__(self, tmp_path, monkeypatch, capsys):
        self.rig = Rig(tmp_path, "claude_code")
        self.capsys = capsys
        monkeypatch.setenv("LEVAIN_HOME", str(self.rig.lv))
        (tmp_path / "ck").mkdir()
        root = _cockpit(tmp_path / "ck")
        self.q = self.rig.qdir
        for name in (cc.SETTINGS_NAME, cx.SETTINGS_NAME):
            (self.q / name).write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                   "away_hold_seconds": 0}))
        self.store = CoordStore(self.q)
        self.key = tmp_path / "ck" / "laptop"
        self.token = self.store.coord_status()["lease"]["token"]
        self.tmp = tmp_path

    def coord(self, *argv):
        got = main(["coord", *argv, "--queue-dir", str(self.q)])
        out = self.capsys.readouterr()
        assert got == 0, (argv, out.out, out.err)
        return out.out

    def away(self, on):
        extra = [] if on else ["--signing-key", str(self.key)]
        assert main(["consent", "away", "on" if on else "off", "--agent", "claude_code", *extra,
                     "--queue-dir", str(self.q)]) == 0
        self.capsys.readouterr()

    def leave_and_return(self):
        """He closes (an operator handoff), leaves (away on) and comes back (a signed away off)."""
        bf = body_file(self.tmp, _body(state_line="main is green; S4 polish running", next="land S4 then open the PR"))
        self.coord("close", "--kind", "operator", "--body-file", str(bf), "--request-id", "r1", "--token",
                   str(self.token))
        self.away(True)
        self.away(False)

    def open_and_ack(self):
        """The coordinator's first act on a turn that carries his words: consume the leave, acknowledge the open."""
        out = self.coord("open", "--light", "--token", str(self.token))
        open_id = int(out.split("--ack ")[1].split()[0])
        self.coord("open", "--ack", str(open_id), "--token", str(self.token))
        return open_id

    def views(self):
        return sql(self.store, "SELECT open_id, via, ref FROM intake_views")

    def ups(self, prompt="go"):
        return self.rig.ups(prompt)

    def reply(self, ctx):
        """The model's reply: its status line, then the block exactly as given (what Stop checks)."""
        return f"coordinator: ok; 0 escalated\n{ctx}"

    def stop(self, message):
        return self.rig.stop(message)

    def turn(self, prompt="go"):
        ctx = self.ups(prompt)
        assert self.stop(self.reply(ctx)) == ""
        return ctx


@pytest.fixture
def arr(tmp_path, monkeypatch, capsys):
    return Arrival(tmp_path, monkeypatch, capsys)


CARD_LINES = ("back: operator open #1 (claude_code away off), acknowledged ",
              "handoff #1 (operator, ", "next: land S4 then open the PR; in flight 1, if-then 1: levain coord handoff show")


# 1 ------------------------------------------------------------------------------------------------------------------
def test_the_card_follows_the_records_and_comes_before_the_band(arr):
    arr.rig.ask("which release first?")
    arr.leave_and_return()
    first = arr.turn("morning")                         # the open is not acknowledged yet when this turn's block is read
    assert "back:" not in first and "NEEDS YOU" in first
    arr.open_and_ack()
    block = arr.ups("what is the plan").splitlines()
    assert block[0].startswith(HEAD)
    (back,) = [i for i, ln in enumerate(block) if ln.strip().startswith("back:")]
    nxt = [i for i, ln in enumerate(block) if ln.strip().startswith("next:")]
    band = [i for i, ln in enumerate(block) if ln.startswith("NEEDS YOU")]
    assert nxt and band and 0 < back < nxt[0] < band[0]
    card = [ln.strip() for ln in block[back:nxt[0] + 1]]
    assert len(card) == 3 and all(a in b for a, b in zip(CARD_LINES, card)) and "main is green; S4 polish running" in card[1]
    assert max(len(ln) for ln in card) < 260


# 2 ------------------------------------------------------------------------------------------------------------------
def test_a_verified_reply_records_the_card_and_the_next_turn_has_none(arr):
    arr.leave_and_return()
    arr.turn("morning")
    open_id = arr.open_and_ack()
    ctx = arr.ups("plan?")
    assert "back:" in ctx and arr.views() == []         # nothing is recorded at the prompt
    assert arr.stop(arr.reply(ctx)) == ""
    assert arr.views() == [(open_id, "terminal", SID)]
    assert "back:" not in arr.ups("and now?")


# 3 ------------------------------------------------------------------------------------------------------------------
def test_an_interrupted_turn_and_a_failed_reprint_record_nothing_and_the_card_shows_again(arr):
    arr.leave_and_return()
    arr.turn("morning")
    arr.open_and_ack()
    assert "back:" in arr.ups("one")                    # interrupted: no Stop
    assert arr.views() == []
    again = arr.ups("two")
    assert "back:" in again
    retry = arr.stop("coordinator: fine, nothing reprinted")      # a reply that dropped the block
    assert retry and "reprint" in retry.lower() and arr.views() == []
    assert "back:" in arr.ups("three")


# 4 ------------------------------------------------------------------------------------------------------------------
def test_a_drop_reply_after_the_ack_carries_the_card_once(tmp_path, capsys):
    w = World(tmp_path, capsys)
    w.run("coord", "close", "--kind", "operator", "--body-file", str(body_file(tmp_path, _body())), "--request-id", "r1",
          "--token", str(w.token), "--queue-dir", str(w.q))
    w.away("claude_code")
    w.away("claude_code", on=False)
    out = w.coord("open")
    open_id = int(out.split("--ack ")[1].split()[0])
    w.coord("open", "--ack", str(open_id))
    d1, _ = w.store.accept_drop("morning from the phone", surface="phone")
    first = drop_reply.write_reply(w.store, d1.drop_id, "on it", token=w.token)
    assert "back: operator open #" in first.block and "handoff #1 (operator," in first.block
    assert sql(w.store, "SELECT open_id, via, ref FROM intake_views") == [(open_id, "drop_reply", first.reply_id)]
    d2, _ = w.store.accept_drop("and another", surface="phone")
    second = drop_reply.write_reply(w.store, d2.drop_id, "also on it", token=w.token)
    assert "back:" not in second.block
    assert len(sql(w.store, "SELECT * FROM intake_views")) == 1


# 5 ------------------------------------------------------------------------------------------------------------------
def _due(store):
    conn = sqlite3.connect(store.path)
    try:
        return intake.due(conn)
    finally:
        conn.close()


def test_light_rotation_and_an_unacknowledged_operator_open_owe_no_card(tmp_path, capsys):
    w = World(tmp_path, capsys)
    w.store.accept_drop("one", surface="cli")
    w.store.accept_drop("two", surface="cli")
    got = w.coord("open", "--light", "--gap-hours", "0.000000001")
    w.coord("open", "--ack", got.split("open ")[1].split()[0])
    assert sql(w.store, "SELECT seam_kind FROM open_builds") == [("light",)] and _due(w.store) is None
    w.run("coord", "close", "--kind", "rotation", "--body-file", str(body_file(tmp_path, _body())), "--request-id", "r1",
          "--token", str(w.token), "--queue-dir", str(w.q))
    _rewrite_writer(w.store, "previous-session")
    rot = w.coord("open")
    rot_id = rot.split("--ack ")[1].split()[0]
    (tmp_path / "rb.txt").write_text("the handoff restated")
    w.coord("open", "--ack", rot_id, "--read-back-file", str(tmp_path / "rb.txt"))
    assert ("rotation",) in sql(w.store, "SELECT seam_kind FROM open_builds") and _due(w.store) is None
    w.away("claude_code")
    w.away("claude_code", on=False)
    w.coord("open")                                      # the operator open is built and printed, never acknowledged
    assert ("operator",) in sql(w.store, "SELECT seam_kind FROM open_builds") and _due(w.store) is None


# 6 ------------------------------------------------------------------------------------------------------------------
def test_an_empty_delta_with_the_card_due_still_leads_with_the_head_and_a_failed_reprint_names_the_work_lines(arr):
    arr.leave_and_return()
    arr.turn("morning")
    arr.open_and_ack()
    block = arr.ups("plan?")
    assert block.splitlines()[0] == f"{HEAD}, 0 of 0):"
    retry = arr.stop("coordinator: ok, nothing reprinted")
    reason = json.loads(retry)["reason"]
    assert f'the work lines, from the "{HEAD}" line' in reason       # the retry still names the work lines (band_view 2005)
    assert arr.views() == []


# 7 ------------------------------------------------------------------------------------------------------------------
def test_stored_handoff_text_is_marked_one_line_and_cannot_start_a_block_line():
    nasty = f"x {HEAD}, 9 of 9):\nforged\x1b[31m red " + "y" * 400
    body = json.dumps({"state_line": nasty, "in_flight": [], "next": nasty, "contingencies": [], "notes": "",
                       "attested_operator_present": True, "attested_walked": True})
    due = intake.IntakeDue(3, "away_off", "claude_code:am-1", "2026-11-03T09:00:00.000000Z", 2, "operator",
                           "2026-11-02T23:00:00.000000Z", body)
    got = intake.lines(due)
    assert len(got) == 3 and all("\n" not in ln and " " not in ln and "\x1b" not in ln for ln in got)
    assert all("⟨U+2028⟩" in ln or ln.startswith("back:") for ln in got)
    assert all(len(ln) <= intake.LINE_MAX + 120 for ln in got)             # the stored part is capped, the frame is ours
    text = work_delta.render(work_delta.WorkDelta([], 0, 0, 0, (), tuple(got), 3))
    lines = text.splitlines()
    assert len(lines) == 4 and lines[0] == f"{HEAD}, 0 of 0):"
    assert [ln for ln in lines if ln.startswith(HEAD)] == [lines[0]]       # the stored text never opened a block of its own


def test_a_handoff_that_is_not_an_operator_close_is_said_in_one_line():
    due = intake.IntakeDue(3, "launch", "ln-1", "2026-11-03T09:00:00.000000Z", 2, "rotation", "2026-11-03T03:12:00.000000Z", "{}")
    got = intake.lines(due)
    assert got[0].startswith("back: operator open #3 (cold launch)") and len(got) == 2
    assert "not an operator close" in got[1]
    none = intake.lines(intake.IntakeDue(3, "away_off", "claude_code:m", "2026-11-03T09:00:00.000000Z", None, None, None, None))
    assert none[1] == "handoff: none exists" and len(none) == 2


# the double show the real run measured --------------------------------------------------------------------------------
def test_a_cold_launch_over_a_handoff_already_carded_owes_no_second_card(arr):
    """Measured in the run (s5/RUN.md, scenario cold): the launch a drop wakes after he came back opens `operator` over the
    SAME handoff; it must not show that handoff's card a second time."""
    from levain.coordinator import lease as _lease, seam_build, seam_open, seam_verbs
    import os

    arr.leave_and_return()
    arr.turn("morning")
    arr.open_and_ack()
    arr.turn("plan?")                                    # the card is shown and recorded
    assert len(arr.views()) == 1
    arr.coord("release", "--by", "coordinator", "--token", str(arr.token))
    old = arr.store.launch_by_harness_session(SID)
    with arr.store._write() as (conn, now):
        _lease._launch_event(conn, now, old.launch_id, "ended", "session done")
    d, _ = arr.store.accept_drop("back, from the phone", surface="phone")
    lid = arr.store.woken_launch(d.drop_id)
    assert lid is not None
    arr.store.launch_claim_spawn(lid, os.getpid())
    built = seam_build.starter_build(arr.store, lid, pid=os.getpid(), kind=seam_verbs.open_kind(arr.store),
                                     queue_dir=arr.q)
    assert (built.seam_kind, built.signal_kind) == ("operator", "launch")
    arr.store.launch_activate(lid, harness_session_id="h-" + lid)
    token2 = arr.store.coord_status()["lease"]["token"]
    seam_open.record_delivery(arr.store, built.open_id, "launch_brief", recipient=lid)
    arr.coord("open", "--ack", str(built.open_id), "--token", str(token2))
    assert _due(arr.store) is None


def test_a_cold_launch_with_no_card_shown_for_its_handoff_owes_one(arr):
    from levain.coordinator import lease as _lease, seam_build, seam_open, seam_verbs
    import os

    arr.leave_and_return()                               # the leave is never consumed: no card has been shown
    arr.coord("release", "--by", "coordinator", "--token", str(arr.token))
    old = arr.store.launch_by_harness_session(SID)
    with arr.store._write() as (conn, now):
        _lease._launch_event(conn, now, old.launch_id, "ended", "session done")
    d, _ = arr.store.accept_drop("back, from the phone", surface="phone")
    lid = arr.store.woken_launch(d.drop_id)
    arr.store.launch_claim_spawn(lid, os.getpid())
    built = seam_build.starter_build(arr.store, lid, pid=os.getpid(), kind=seam_verbs.open_kind(arr.store), queue_dir=arr.q)
    arr.store.launch_activate(lid, harness_session_id="h-" + lid)
    token2 = arr.store.coord_status()["lease"]["token"]
    seam_open.record_delivery(arr.store, built.open_id, "launch_brief", recipient=lid)
    arr.coord("open", "--ack", str(built.open_id), "--token", str(token2))
    owed = _due(arr.store)
    assert owed is not None and owed.open_id == built.open_id and owed.signal_kind == "launch"
    assert intake.lines(owed)[0].startswith(f"back: operator open #{built.open_id} (cold launch)")


# the record itself ----------------------------------------------------------------------------------------------------
def test_record_is_once_append_only_and_refuses_another_store(arr):
    arr.leave_and_return()
    arr.turn("morning")
    open_id = arr.open_and_ack()
    sid = sql(arr.store, "SELECT value FROM meta WHERE key = 'store_id'")[0][0]
    assert not intake.record(arr.q, open_id, "terminal", "s", 2.0, "another-store-id")
    assert not intake.record(arr.q, open_id, "nonsense", "s", 2.0, sid) and not intake.record(arr.q, 999, "terminal", "s", 2.0, sid)
    assert intake.record(arr.q, open_id, "terminal", "s", 2.0, sid)
    assert not intake.record(arr.q, open_id, "drop_reply", "rp-x", 2.0, sid)       # once
    conn = sqlite3.connect(arr.store.path)
    for stmt in ("UPDATE intake_views SET via = 'drop_reply'", "DELETE FROM intake_views",
                 "INSERT OR REPLACE INTO intake_views (open_id, via, ref, at) VALUES (1, 'terminal', 'z', 'z')"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(stmt)
    conn.close()


# 8 ------------------------------------------------------------------------------------------------------------------
def _counts(store):
    names = [r[0] for r in sql(store, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {n: sql(store, f"SELECT COUNT(*) FROM {n}")[0][0] for n in names}


def test_plan_lists_each_kind_of_thing_the_walk_needs(tmp_path, capsys, monkeypatch):
    w = World(tmp_path, capsys)
    QuestionStore(w.q).file(agent="claude_code", agent_session_id="seat-9", what="which release first?",
                            options=[("a", "yes"), ("b", "no")], rec="a", blocks="x", cwd=str(tmp_path))
    monkeypatch.setattr(morning, "_undecided", lambda qdir, conn=None: [
        {"request_id": "rq-1", "agent": "claude_code", "session": "seat-9", "tool": "Bash", "what": "git push",
         "cwd": "/x", "tier": "T2", "gap": "g", "undecided_at": "2026-11-03T01:00:00Z", "rule": "r"}])
    w.store.accept_drop("from the phone", surface="phone")
    ready.add(w.store, w.token, "levain:s4", repo="levain", title="S4 polish", who="seat-9", why="running.", to="running")
    plan = seam_plan.close_plan(w.store, w.q, datetime.now(timezone.utc) + timedelta(minutes=30), "levain")
    assert len(plan) == 7
    assert plan[0].startswith("band: 1 open (#1)") or plan[0].startswith("band: 1 open (new)")
    assert plan[1].startswith("undecided SIGNs and crossed budgets: 1 undecided SIGN(s) (rq-1)")
    assert plan[2].startswith("drops with no reply: 1 (dr-") and "coord reply DROP_ID" in plan[2]
    assert plan[3].startswith("board: running 1 (levain:s4)") and "in_flight" in plan[3]
    assert plan[4] == "unacknowledged opens: 0"
    assert plan[5].startswith("away: off") and "coord away DROP_ID" in plan[5]
    assert plan[6].startswith("body: state_line, in_flight, next and contingencies") and "attested_walked" in plan[6]
    assert "no earlier handoff" in plan[6]


def test_plan_on_an_empty_store_says_zero_for_every_section(tmp_path, capsys):
    w = World(tmp_path, capsys)
    plan = seam_plan.close_plan(w.store, w.q, datetime.now(timezone.utc), "levain")
    assert [ln.split(":")[0] for ln in plan] == ["band", "undecided SIGNs and crossed budgets", "drops with no reply",
                                                 "board", "unacknowledged opens", "away", "body"]
    assert plan[0] == "band: 0 open" and plan[1].endswith(": 0") and plan[2].endswith(": 0")
    assert "nothing running" in plan[3] and plan[4].endswith(": 0")


def test_plan_reads_cannot_derive_for_a_queue_directory_it_cannot_read(tmp_path, capsys):
    w = World(tmp_path, capsys)
    afile = tmp_path / "afile"
    afile.write_text("not a directory")
    plan = seam_plan.close_plan(w.store, afile, datetime.now(timezone.utc), "levain")
    assert plan[0].startswith("band: cannot-derive (") and "the queue directory could not be read" in plan[0]
    assert plan[1].startswith("undecided SIGNs and crossed budgets: cannot-derive (")
    assert plan[5].startswith("away: cannot-derive (")
    assert plan[2] == "drops with no reply: 0" and plan[3].startswith("board:") and "cannot-derive" not in plan[3]


def test_plan_writes_nothing_and_refuses_a_body_file(tmp_path, capsys):
    w = World(tmp_path, capsys)
    w.store.accept_drop("from the phone", surface="phone")
    before = _counts(w.store)
    head = sql(w.store, "SELECT MAX(gseq) FROM gseq")
    out = w.run("coord", "close", "--plan", "--queue-dir", str(w.q))                  # no --token needed
    assert out.startswith("operator walk (each line is read from the stores; nothing was written):\n1. band:")
    assert len(out.splitlines()) == 8
    assert _counts(w.store) == before and sql(w.store, "SELECT MAX(gseq) FROM gseq") == head
    got = main(["coord", "close", "--plan", "--body-file", str(body_file(tmp_path)), "--queue-dir", str(w.q)])
    assert got == 1 and "takes no --body-file" in capsys.readouterr().err
    assert _counts(w.store) == before


# 10 -----------------------------------------------------------------------------------------------------------------
@pytest.fixture
def pending(tmp_path):
    import os

    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = CoordStore(q)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    d, _ = s.accept_drop("first words", surface="cli")
    s.launch_id = s.woken_launch(d.drop_id)
    s.launch_claim_spawn(s.launch_id, os.getpid())
    return s


def _arrival_section(text):
    assert "## When he arrives or leaves" in text
    return text.split("## When he arrives or leaves")[1].split("\n## ")[0]


def test_the_brief_tells_the_coordinator_to_open_light_first_and_the_walk_order(pending):
    text = cl._brief_text(pending, pending.launch(pending.launch_id))
    sec = _arrival_section(text)
    assert "coord open --light --token N" in sec and "before you reply" in sec
    assert cl.DAY_PARAGRAPH in sec and "back:" in cl.DAY_PARAGRAPH
    assert sec.index("coord away DROP_ID") < sec.index("coord close --plan") < sec.index("coord close --kind rotation|operator")
    assert text.index("## When he arrives or leaves") < text.index("## How your turn ends")


def test_the_arrival_section_is_in_the_brief_when_the_open_could_not_be_built(tmp_path):
    q = tmp_path / "q2"
    q.mkdir(mode=0o700)
    s = CoordStore(q)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    d, _ = s.accept_drop("first words", surface="cli")
    lid = s.woken_launch(d.drop_id)                       # no spawn claim: starter_build refuses
    text = cl._brief_text(s, s.launch(lid))
    assert "Levain could not build your open:" in text
    sec = _arrival_section(text)
    assert "coord open --light --token N" in sec and "coord close --plan" in sec and "--kind rotation|operator" in sec


def test_option_b_is_one_paragraph(pending, monkeypatch):
    monkeypatch.setattr(cl, "DAY_PARAGRAPH", cl._DAY_STATUS_ONLY)
    sec = _arrival_section(cl._brief_text(pending, pending.launch(pending.launch_id)))
    assert cl._DAY_STATUS_ONLY in sec and "at most 5 plain lines" not in sec


# L3 round 1 fixes (project_memory/1103-20_L3_c15s5_r1.txt) -----------------------------------------------------------
def _cold_launch_acked(arr):
    """A cold launch a drop woke after he came back: its `operator`/`launch` open, delivered and acknowledged."""
    from levain.coordinator import lease as _lease, seam_build, seam_open, seam_verbs
    import os

    arr.coord("release", "--by", "coordinator", "--token", str(arr.token))
    old = arr.store.launch_by_harness_session(SID)
    with arr.store._write() as (conn, now):
        _lease._launch_event(conn, now, old.launch_id, "ended", "session done")
    d, _ = arr.store.accept_drop("back, from the phone", surface="phone")
    lid = arr.store.woken_launch(d.drop_id)
    arr.store.launch_claim_spawn(lid, os.getpid())
    built = seam_build.starter_build(arr.store, lid, pid=os.getpid(), kind=seam_verbs.open_kind(arr.store),
                                     queue_dir=arr.q)
    arr.store.launch_activate(lid, harness_session_id="h-" + lid)
    token2 = arr.store.coord_status()["lease"]["token"]
    seam_open.record_delivery(arr.store, built.open_id, "launch_brief", recipient=lid)
    arr.coord("open", "--ack", str(built.open_id), "--token", str(token2))
    return built


def test_an_older_pending_away_return_is_not_lost_behind_a_carded_cold_launch(arr):
    """codex #1: the newest candidate is a cold launch over a handoff whose card was shown; the away-return before it,
    acknowledged and never carded, is still owed."""
    arr.leave_and_return()
    arr.turn("morning")
    arr.open_and_ack()
    arr.turn("plan?")                                    # open 1's card is shown and recorded
    assert len(arr.views()) == 1
    arr.away(True)
    arr.away(False)
    second = arr.open_and_ack()                          # a second return, acknowledged, its card not yet shown
    built = _cold_launch_acked(arr)
    assert built.open_id > second and built.signal_kind == "launch"
    owed = _due(arr.store)
    assert owed is not None and owed.open_id == second


def test_a_stale_terminal_card_is_not_delivered_when_another_delivery_won(arr):
    """codex #2: the card was staged for the terminal, a reply recorded it first; the terminal's Stop must not commit a
    second card: it asks once for the block without the card lines, and that reprint commits."""
    arr.leave_and_return()
    arr.turn("morning")
    open_id = arr.open_and_ack()
    ctx = arr.ups("plan?")
    assert "back:" in ctx
    sid = sql(arr.store, "SELECT value FROM meta WHERE key = 'store_id'")[0][0]
    assert intake.record(arr.q, open_id, "drop_reply", "rp-won", 2.0, sid)           # the other delivery wins
    retry = arr.stop(arr.reply(ctx))
    assert retry and "already shown" in json.loads(retry)["reason"]
    assert arr.views() == [(open_id, "drop_reply", "rp-won")]
    card_free = "\n".join(ln for ln in ctx.splitlines() if not ln.strip().startswith(("back:", "handoff #", "next:")))
    assert "back:" not in card_free
    assert arr.stop(arr.reply(card_free)) == ""
    assert arr.views() == [(open_id, "drop_reply", "rp-won")]
    assert "back:" not in arr.ups("and now?")


def test_record_says_already_shown_apart_from_a_failed_write(arr):
    arr.leave_and_return()
    arr.turn("morning")
    open_id = arr.open_and_ack()
    sid = sql(arr.store, "SELECT value FROM meta WHERE key = 'store_id'")[0][0]
    assert intake.record_outcome(arr.q, open_id, "terminal", "s", 2.0, "another-store-id") == intake.FAILED
    assert intake.record_outcome(arr.q, open_id, "terminal", "s", 2.0, sid) == intake.RECORDED
    assert intake.record_outcome(arr.q, open_id, "drop_reply", "rp-x", 2.0, sid) == intake.ALREADY


def test_a_drop_reply_after_a_terminal_record_proceeds_without_the_card(tmp_path, capsys):
    """deepseek #2 does not hold: the reply reads `due` and inserts inside ONE write transaction, so a row recorded
    before it is simply not owed (no IntegrityError); pinned here."""
    w = World(tmp_path, capsys)
    w.run("coord", "close", "--kind", "operator", "--body-file", str(body_file(tmp_path, _body())), "--request-id", "r1",
          "--token", str(w.token), "--queue-dir", str(w.q))
    w.away("claude_code")
    w.away("claude_code", on=False)
    open_id = int(w.coord("open").split("--ack ")[1].split()[0])
    w.coord("open", "--ack", str(open_id))
    sid = sql(w.store, "SELECT value FROM meta WHERE key = 'store_id'")[0][0]
    assert intake.record(w.q, open_id, "terminal", "s", 2.0, sid)
    d, _ = w.store.accept_drop("morning from the phone", surface="phone")
    got = drop_reply.write_reply(w.store, d.drop_id, "on it", token=w.token)
    assert "back:" not in got.block and len(sql(w.store, "SELECT * FROM intake_views")) == 1


def test_plan_counts_every_unanswered_drop_and_lists_the_closed_window_ones_apart(tmp_path, capsys):
    """codex #3: a drop a second old is already owed an answer; one past the window cannot be, and says what to do."""
    w = World(tmp_path, capsys)
    fresh, _ = w.store.accept_drop("just now", surface="phone")
    now = datetime.now(timezone.utc)
    plan = seam_plan.close_plan(w.store, w.q, now, "levain")
    assert plan[2].startswith(f"drops with no reply: 1 ({fresh.drop_id})") and "coord reply DROP_ID" in plan[2]
    late = seam_plan.close_plan(w.store, w.q, now + timedelta(hours=26), "levain")
    assert "past the reply window" in late[2] and fresh.drop_id in late[2] and "coord reply DROP_ID" not in late[2]
    assert "handoff" in late[2]
    old = seam_plan.close_plan(w.store, w.q, now + timedelta(days=9), "levain")
    assert "past the reply window" in old[2] and fresh.drop_id in old[2]
    w.store.accept_drop("terminal words", surface="cli")                         # answered in the session: never listed
    assert seam_plan.close_plan(w.store, w.q, now, "levain")[2].count("dr-") == 1


def test_plan_names_the_ack_command_that_works_and_sorts_out_the_opens_that_cannot_be_acknowledged(tmp_path, capsys):
    """codex #4: a rotation open needs --read-back-file; an open never delivered to this launch cannot be acknowledged."""
    from levain.coordinator import seam_build

    w = World(tmp_path, capsys)
    w.run("coord", "close", "--kind", "rotation", "--body-file", str(body_file(tmp_path, _body())), "--request-id", "r1",
          "--token", str(w.token), "--queue-dir", str(w.q))
    _rewrite_writer(w.store, "previous-session")
    rot_id = int(w.coord("open").split("--ack ")[1].split()[0])                  # built and delivered to this launch
    orphan = seam_build.build(w.store, kind="light", signal_kind="silence", signal_key="dr-x", queue_dir=w.q,
                              token=w.token)                                       # built, never handed to anyone
    plan = seam_plan.close_plan(w.store, w.q, datetime.now(timezone.utc), "levain")[4]
    assert plan.startswith("unacknowledged opens: 2 ")
    assert f"coord open --ack {rot_id} --read-back-file PATH --token N" in plan
    assert f"coord open --ack {orphan.open_id} " not in plan
    assert "not acknowledgeable" in plan and f"#{orphan.open_id}" in plan.split("not acknowledgeable")[1]
    (tmp_path / "rb.txt").write_text("the handoff restated")
    w.coord("open", "--ack", str(rot_id), "--read-back-file", str(tmp_path / "rb.txt"))        # the printed form works


def test_plan_survives_a_card_that_is_none(tmp_path, capsys, monkeypatch):
    """deepseek #1: morning.card returning None must read as cannot-derive on its two lines, not as a NameError."""
    w = World(tmp_path, capsys)
    monkeypatch.setattr(morning, "card", lambda *a, **kw: None)
    plan = seam_plan.close_plan(w.store, w.q, datetime.now(timezone.utc), "levain")
    assert len(plan) == 7
    assert plan[1].startswith("undecided SIGNs and crossed budgets: cannot-derive (") and "NameError" not in plan[1]
    assert plan[5].startswith("away: cannot-derive (") and "NameError" not in plan[5]


def test_plan_on_no_store_says_so(tmp_path, capsys):
    """deepseek #3 does not hold: open_readonly raises StoreAbsent for a missing store; pinned here."""
    q = tmp_path / "empty"
    q.mkdir(mode=0o700)
    assert main(["coord", "close", "--plan", "--queue-dir", str(q)]) == 1
    assert "there is no coordinator store here" in capsys.readouterr().err
