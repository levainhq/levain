"""D1-N: the night's notices have a production caller (the agents tick), and the store path is required.

The design row (D1_design2 section 2, row D1-N) and the signed answers (Q6 (a), Q2 (b)): the tick calls
``close_window`` once a window has closed, together with the pause notice, the link-renewal card and the restore drill,
and ``store_path`` is a required argument. Everything here is driven through ``tick_driver.run_tick`` over a real agent
store with the dream registered and installed, a real anneal store, a real desk review store and a real signed link
log. Nothing calls ``close_window`` but the tick. The ``agent`` feed is the reader: ``levain desk review list`` shows
it.
"""
from __future__ import annotations

import inspect
import io
import re
import shlex
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain import cli as levain_cli
from levain.agents import store as agent_store
from levain.agents import tick_driver as T
from levain.autonomic.db import DB_NAME
from levain.cockpit.consent import utc_stamp      # the authority log's one timestamp form (it carries microseconds)
from levain.desk import review
from levain.desk.store import CoordStore
from levain.dream import agent as da
from levain.dream import ready as rd
from levain.dream import snapshot as sn
from tests.test_agents_launch import BINDING, Rig
from tests.test_authority_agent_link import link as link_record
from tests.test_dream_agent import ZONE, hold_a_wrap_open
from tests.test_dream_ready import Entity
from tests.test_dream_snapshot import _VALID, dream

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

AGENT = "agent"                       # the feed the notices are read in (review.FEEDS)


class Night:
    """A dream install on a scratch HOME-free world: the agent store (real, with the dream registered and installed),
    the anneal store, the desk review store and the cockpit's authority log. ``now`` is the tick's clock."""

    def __init__(self, tmp: Path) -> None:
        (tmp / "rig").mkdir()
        self.rig = Rig(tmp / "rig")
        self.ent = Entity(tmp / "ent")
        agent_store.set_default_zone(self.rig.store, ZONE)
        self.reg = da.register_dream(self.rig.store)
        da.write_install(self.rig.store.directory, store_path=self.ent.db, cockpit_root=self.rig.world.root,
                         coord_dir=self.ent.coord_dir, zone=ZONE)
        self.now = self.rig.world.clock.now + timedelta(days=3, hours=14)
        self.n = 0
        self.stream = io.StringIO()

    # time
    @property
    def today(self):
        return rd.dream_day_at(self.now, ZONE)

    @property
    def last_closed(self):
        return self.today - timedelta(days=1)

    # the review store, read cold
    def items(self, **kw):
        try:
            return review.list_items(CoordStore(self.ent.coord_dir), feed=AGENT, **kw)
        except Exception:      # the store does not exist until something is filed
            return []

    def kinds(self):
        return sorted(i.body.get("kind", "?") for i in self.items(include_cleared=True))

    # rows
    def row(self, day, state, reason="r", *, hour=2, oid=None, at=None):
        self.n += 1
        oid = oid or f"oc-{self.n}"
        stamp = at or f"{(day + timedelta(days=1)).isoformat()}T0{hour}:00:00Z"
        con = sqlite3.connect(self.rig.store.directory / DB_NAME, isolation_level=None)
        try:
            con.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, window_key, "
                        "spec_hash, binding_id, run_id, admitted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (oid, self.reg.agent_id, "scheduled", f"k-{oid}", "admitted", rd.window_key_for(day),
                         self.reg.spec_hash, BINDING, f"run-{oid}", stamp))
            if state not in ("admitted", "skipped-no-input", "missed"):
                con.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=0, link_id='l', "
                            "link_incarnation='i', launched_at=? WHERE occurrence_id=?", (stamp, oid))
            if state not in ("admitted", "launched"):
                con.execute("UPDATE agent_occurrences SET state=?, reason=?, ended_at=? WHERE occurrence_id=?",
                            (state, reason, stamp, oid))
        finally:
            con.close()
        return oid

    def wrap_open_night(self, day, slots=3):
        for i in range(slots):
            self.row(day, "skipped-no-input", f"quiet/wrap-open: {self.ent.db}", hour=i, at=None)

    # the link
    def sign_link(self, days_left: float) -> str:
        """A live link for the dream's spec expiring ``days_left`` after ``now``."""
        self.rig.n += 1
        expires = self.now + timedelta(days=days_left)
        # a link's life is at most 30 days from its signing, which the world's clock fixes
        signed_at = self.rig.world.clock.now + timedelta(minutes=1)
        assert expires - signed_at <= timedelta(days=30), "move the test's now"
        self.rig.world.link({"op": "create_grant", "grant": link_record(
            self.rig.world, self.rig.n, agent=self.reg.agent_id, store=self.rig.sid, spec_hash=self.reg.spec_hash,
            binding_id=BINDING, expires_at=utc_stamp(expires))}, "laptop")
        return f"al-{self.rig.n:012x}"

    def pause(self, streak=2):
        with self.rig.store.write() as c:
            c.execute("UPDATE agents SET operation='paused', failure_streak=? WHERE agent_id=?",
                      (streak, self.reg.agent_id))

    # one tick, over the real pieces
    def tick(self, later=timedelta(0), **kw):
        self.stream = io.StringIO()
        clock = kw.pop("clock", None) or (lambda: self.now + later)
        return T.run_tick(self.rig.store, clock=clock, authority=kw.pop("authority", self.rig.authority),
                          launcher_of=lambda: None, stream=self.stream, **kw)

    @property
    def err(self) -> str:
        return self.stream.getvalue()


@pytest.fixture
def night(tmp_path) -> Night:
    return Night(tmp_path)


def commands(text: str, start: str) -> list[str]:
    return [c for c in re.findall(r"`([^`]+)`", text) if c.startswith(start)]


# --------------------------------------------------- the tick files the night's card (the row's DONE line, per piece)

def test_the_tick_itself_files_one_did_not_consolidate_card_whose_commands_run(night):
    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.last_closed)
    assert night.tick() == T.EXIT_OK, night.err
    cards = night.items()
    assert [c.body["kind"] for c in cards] == ["did-not-consolidate"], night.err
    remedy = cards[0].body["remedy"]
    assert "<the store's memory.db>" not in remedy and str(night.ent.db) in remedy
    status, cancel = [shlex.split(c) for c in commands(remedy, "/") if " wrap-" in c]
    out = subprocess.run(status, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "wrap in progress" in out.stdout, out.stdout + out.stderr
    token = re.search(r"token:\s+([0-9a-f]{32})", out.stdout).group(1)
    out = subprocess.run(cancel[:-1] + [token], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "wrap cancelled" in out.stdout, out.stdout + out.stderr


def test_the_tick_runs_every_minute_and_still_files_one_card_per_window_and_class(night):
    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.last_closed - timedelta(days=1))
    night.wrap_open_night(night.last_closed)
    for minute in range(4):
        assert night.tick(later=timedelta(minutes=minute)) == T.EXIT_OK, night.err
    assert night.kinds() == ["did-not-consolidate"]                    # two closed windows, four ticks, one card


def test_a_cleared_card_is_not_refiled_by_the_next_tick(night):
    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.last_closed)
    night.tick()
    review.clear_item(CoordStore(night.ent.coord_dir), night.items()[0].item_id, mode="manual")
    night.tick(later=timedelta(minutes=1))
    assert len(night.items(include_cleared=True)) == 1


def test_a_window_still_open_or_with_a_run_in_flight_files_nothing(night):
    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.today)                                   # today's window has not closed
    night.row(night.last_closed, "skipped-no-input", "quiet/wrap-open: w")
    night.row(night.last_closed, "launched")                            # a run in flight holds the filer
    assert night.tick() == T.EXIT_OK, night.err
    assert night.items(include_cleared=True) == []


def test_the_windows_the_controller_reports_missed_reach_the_filer(night, monkeypatch):
    """A night the laptop slept through leaves no row: only this tick's TickResult knows. The tick hands it on."""
    from levain.agents import controller as ctl

    result = ctl.TickResult()
    result.missed_windows[night.reg.agent_id] = [rd.window_key_for(night.last_closed)]
    monkeypatch.setattr(ctl, "tick", lambda *a, **k: result)
    assert night.tick() == T.EXIT_OK, night.err
    cards = night.items()
    assert [c.body["kind"] for c in cards] == ["did-not-consolidate"]
    assert cards[0].body["reason"].startswith("laptop-slept")


def test_a_consolidated_night_files_nothing(night):
    night.row(night.last_closed, "done", "completed")
    assert night.tick() == T.EXIT_OK, night.err
    assert night.items(include_cleared=True) == []


# --------------------------------------------------------------------------------- the pause notice, the link, the drill

def test_a_paused_dream_with_a_failure_streak_raises_one_notice_a_reader_sees(night):
    night.row(night.last_closed, "failed", "budget", hour=1)
    night.row(night.last_closed, "failed", "deadline", hour=3)
    night.pause()
    for minute in range(3):
        assert night.tick(later=timedelta(minutes=minute)) == T.EXIT_OK, night.err
    notes = [i for i in night.items() if i.body.get("kind") == "attention"]
    assert len(notes) == 1 and "levain agents resume dream" in notes[0].body["reason"]
    assert "attention [dream-paused:" in night.err                      # the existing stderr line is kept beside it


def test_the_desk_review_list_shows_the_words_of_a_notice_not_a_blank_line(night, capsys):
    """Found by running the tick end to end: the CLI's list printed ``body["text"]`` only, and an agent card or an
    attention item carries its words in ``reason``, so a reader ran the verb and saw ``agent: `` and nothing."""
    from levain.cli import main

    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.last_closed)
    night.row(night.last_closed, "failed", "budget")
    night.row(night.last_closed, "failed", "deadline", hour=3)
    night.pause()
    night.tick()
    assert main(["coord", "review", "list", "--json", "--feed", AGENT, "--queue-dir", str(night.ent.coord_dir)]) == 0
    texts = sorted(r["text"] for r in __import__("json").loads(capsys.readouterr().out)["items"])
    assert len(texts) == 2 and all(texts), texts
    assert any("did not consolidate" in t for t in texts) and any("levain agents resume dream" in t for t in texts)


def test_a_link_inside_three_days_of_expiry_files_one_renewal_card(night):
    night.sign_link(days_left=2)
    for minute in range(3):
        assert night.tick(later=timedelta(minutes=minute)) == T.EXIT_OK, night.err
    renew = [i for i in night.items() if i.body.get("kind") == "renewal-due"]
    assert len(renew) == 1 and renew[0].authority_relevant
    assert str(night.rig.world.root) in renew[0].body["remedy"]


def test_a_link_with_time_left_files_no_renewal_card(night):
    night.sign_link(days_left=20)
    assert night.tick() == T.EXIT_OK, night.err
    assert night.items(include_cleared=True) == []


def test_the_restore_drill_runs_in_the_tick_over_a_real_snapshot(night):
    assert dream(night.ent.db, "o1", _VALID).outcome == "saved"
    root = night.ent.db.parent / "dream"
    assert sn.last_drill(root) is None
    assert night.tick() == T.EXIT_OK, night.err
    last = sn.last_drill(root)
    assert last is not None and last["outcome"] == "ok", (last, night.err)


def test_a_failed_drill_raises_an_attention_item_a_reader_sees(night, monkeypatch):
    assert dream(night.ent.db, "o1", _VALID).outcome == "saved"
    real = sn.drill

    def bad(store_path, root, **kw):
        res = real(store_path, root, **kw)
        res.outcome = "failed"
        res.steps = [{"name": "hash", "status": "failed", "detail": "a snapshot file moved"}]
        return res

    monkeypatch.setattr(sn, "drill", bad)
    night.tick()
    notes = [i for i in night.items() if i.body.get("kind") == "attention"]
    assert len(notes) == 1 and "restore drill failed" in notes[0].body["reason"]


# ---------------------------------------------------------- each failure of a notice is itself visible, never swallowed

def test_a_notice_step_that_raises_is_named_and_the_others_still_run(night, monkeypatch):
    night.row(night.last_closed, "failed", "budget")
    night.row(night.last_closed, "failed", "deadline", hour=3)
    night.pause()

    def boom(**kw):
        raise RuntimeError("the review store is on fire")

    monkeypatch.setattr(da, "close_window", boom)
    code = night.tick()
    assert code == T.EXIT_NOTICES
    assert "levain agents tick:" in night.err and "close_window" in night.err and "on fire" in night.err
    keys = sorted(i.body["key"] for i in night.items())
    # the failure is itself an item a reader sees (once, however many windows failed), and the pause notice still ran
    assert keys == ["dream-notice-failed:close_window", f"dream-paused:{night.reg.agent_id}"], keys


def test_a_card_the_feed_refuses_is_named_not_read_as_filed(night, monkeypatch):
    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.last_closed)
    monkeypatch.setattr(review, "FEEDS", tuple(f for f in review.FEEDS if f != AGENT))
    assert night.tick() == T.EXIT_NOTICES
    assert "refused" in night.err


def test_an_unreadable_dream_install_is_named_by_the_notice_phase_not_read_as_absent(night):
    (night.rig.store.directory / da.INSTALL_FILE).write_text("{not json")
    assert night.tick() == T.EXIT_OK                  # as the tick's own probes treat it: named, not an exit
    assert "dream install cannot be read" in night.err and "notices were not run" in night.err


def test_a_store_with_no_dream_install_runs_no_notices_and_says_nothing(tmp_path):
    (tmp_path / "rig").mkdir()
    rig = Rig(tmp_path / "rig")
    out = io.StringIO()
    code = T.run_tick(rig.store, clock=rig.now, authority=rig.authority, launcher_of=lambda: None, stream=out)
    assert code == T.EXIT_OK and "notice" not in out.getvalue()


# ----------------------------------------------------------------------------------------------- the tick's time bound

def test_the_notice_phase_costs_a_bounded_number_of_calls_per_tick(night, monkeypatch):
    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.last_closed)
    night.sign_link(days_left=2)
    counts = {"close": 0, "refresh": 0, "install": 0, "drill": 0}
    real_close, real_install, real_drill = da.close_window, da.read_install, da.drill_tick

    def close(**kw):
        counts["close"] += 1
        return real_close(**kw)

    def install(*a, **kw):
        counts["install"] += 1
        return real_install(*a, **kw)

    def drill(*a, **kw):
        counts["drill"] += 1
        return real_drill(*a, **kw)

    class Counting:
        def __init__(self, inner):
            self.inner = inner

        def refresh(self):
            counts["refresh"] += 1
            return self.inner.refresh()

        def peek(self, *a, **k):
            return self.inner.peek(*a, **k)

    monkeypatch.setattr(da, "close_window", close)
    monkeypatch.setattr(da, "read_install", install)
    monkeypatch.setattr(da, "drill_tick", drill)
    # the baseline: the same tick with the notice phase switched off (the reaper and the claims replay the log)
    monkeypatch.setattr(da, "tick_notices", lambda *a, **k: da.NoticeReport())
    assert night.tick(authority=Counting(night.rig.authority)) == T.EXIT_OK, night.err
    baseline = counts["refresh"]
    assert baseline >= 1
    monkeypatch.undo()
    monkeypatch.setattr(da, "close_window", close)
    monkeypatch.setattr(da, "read_install", install)
    monkeypatch.setattr(da, "drill_tick", drill)
    counts.update(refresh=0, close=0, install=0, drill=0)
    assert night.tick(authority=Counting(night.rig.authority)) == T.EXIT_OK, night.err
    assert counts["close"] == da.NOTICE_DAYS and counts["drill"] == 1
    assert counts["refresh"] == baseline, "the notices share the tick's replay of the authority log, they add none"
    assert counts["install"] == 1, counts


def test_a_tick_already_late_skips_the_notices_and_says_so(night):
    hold_a_wrap_open(night.ent)
    night.wrap_open_night(night.last_closed)
    reads = iter(range(1000))
    t0 = night.now

    def clock():                                   # every read of the clock is a minute later than the last
        return t0 + timedelta(minutes=next(reads) * 3)

    code = night.tick(clock=clock, soft_s=540.0, hard_s=600.0)
    assert code == T.EXIT_NOTICES
    assert "notices were not run" in night.err and "next tick" in night.err
    assert night.items(include_cleared=True) == []


def test_the_notice_phase_runs_after_the_renewals_and_the_claims():
    src = inspect.getsource(T.run_tick)
    assert src.index("sup.reap(") < src.index("_ctl.tick(") < src.index("_run_notices("), \
        "the lease renewals and the claims come first"

