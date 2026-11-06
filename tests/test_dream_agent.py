"""levain.dream.agent (D1-2): the template, the input check, the win, the window filer, the notices and the fire body.

Real pieces throughout: the real RoleSpec/AgentSpec parsers and ``check_spec``, a real agent store, the real
``controller.claim`` and ``schedule`` for the fake-clock nights, a real anneal store consolidated by the real
``consolidate_store`` (a fake in-process mind, nothing spent), a real coordinator review store, and the real
``Supervisor`` for the fire body. The ``on_input`` hook POINT is not on main, so the nights drive it with a loop that
stands in for it and is named so (``night``): it asks the dream's own input check and writes the skip the way the
hook point will (the recorded reason of ``InputVerdict.recorded``). The ``agent`` feed is P8's, so cards are filed
under ``digest``, as test_dream_card does.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from levain.agents import controller as ctl, refuse, role as role_mod, schedule, spec as spec_mod, store as agent_store
from levain.autonomic.db import DB_NAME
from levain.coordinator import review
from levain.coordinator.store import CoordStore
from levain.dream import agent as da
from levain.dream import card as cd
from levain.dream import executor as ex
from levain.dream import ready as rd
from levain.dream import snapshot as sn
from tests.test_dream_ready import Entity
from tests.test_dream_snapshot import Fake, add_episode, dream, text_n

ZONE = "America/New_York"
NY = ZoneInfo(ZONE)
FEED = "digest"
# the shape register_dream mints (``ag-...``); the literal "dream" is the agent's NAME, never its id
AID = "ag-3f9a1c0de2b4"


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ role and AgentSpec

@pytest.fixture
def astore(tmp_path):
    s = agent_store.open_agent_store(tmp_path / "agents")
    agent_store.set_default_zone(s, ZONE)
    return s


def test_the_role_and_spec_pass_the_real_parsers_and_every_refusal_rule(astore):
    role = role_mod.parse_role(da.dream_role())
    assert role["executor"] == "dream" and role["harness"] == ""
    spec = spec_mod.parse_spec(da.dream_spec(role_mod.role_hash(role)))
    # the name "dream" is reserved: the dream's own registration passes it, an operator's registration does not
    assert refuse.check_spec(spec, role=role, default_zone=ZONE, names_held=frozenset(), as_dream=True) == []
    assert [r.rule for r in refuse.check_spec(spec, role=role, default_zone=ZONE, names_held=frozenset())] == ["name"]
    assert spec["stop_at"] == 3600 and spec["trigger"]["cron"] == "*/20 22-23,0-4 * * *"
    assert spec["trigger"]["window"] == {"start": "22:00", "end": "05:00"}
    assert spec["on_failure"] == {"pause_after": 2} and spec["overlap"] == "skip" and spec["missed"] == "skip"
    assert refuse.derived_flags(role)["review_required"] is True


def test_register_is_idempotent_and_the_stored_spec_is_the_template(astore):
    first = da.register_dream(astore)
    again = da.register_dream(astore)
    assert first.created and not again.created and first.spec_hash == again.spec_hash
    stored = spec_mod.stored_specs(astore)["dream"]
    assert stored["trigger"]["zone"] == ZONE and stored["win"] == {"kind": "verifier", "check": da.WIN_CHECK}
    assert stored["input"]["liveness"] == da.LIVENESS
    assert role_mod.role_by_hash(astore, first.role_hash)["executor"] == "dream"


def test_register_dream_attaches_the_dream_to_the_stores_one_operator_agent(astore):
    # K2b: the dream is a role the one agent uses; a registration makes the record and the attachment together
    from levain.agents import operator as op

    with astore.read() as conn:
        assert op.materialise(conn, install_dir=None) is None
    reg = da.register_dream(astore)
    with astore.read() as conn:
        agent = op.materialise(conn, install_dir=None)
    assert [(r.agent_id, r.role_name) for r in agent.roles] == [(reg.agent_id, "dream")]
    da.register_dream(astore)                                   # the same content again: still one record, one row
    with astore.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_operator").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_attachments").fetchone()[0] == 1


def test_a_refused_dream_registration_writes_no_operator_record(tmp_path):
    bare = agent_store.open_agent_store(tmp_path / "bare")
    with pytest.raises(da.EnableRefused):
        da.register_dream(bare)
    with bare.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_operator").fetchone()[0] == 0


def test_an_install_with_no_default_zone_refuses_and_names_the_verb(tmp_path):
    bare = agent_store.open_agent_store(tmp_path / "bare")
    with pytest.raises(da.EnableRefused, match="levain agents zone"):
        da.register_dream(bare)
    assert spec_mod.list_agents(bare) == []


def test_a_changed_spec_needs_revise_and_never_overwrites(astore, monkeypatch):
    da.register_dream(astore)
    real = da.dream_spec
    monkeypatch.setattr(da, "dream_spec", lambda h, binding=None: {**real(h, binding), "stop_at": 3000})
    with pytest.raises(da.EnableRefused, match="rule 4"):
        da.register_dream(astore)
    assert da.register_dream(astore, revise=True).version == 2


def test_the_enable_verb_registers_prints_the_card_and_signs_nothing(tmp_path, capsys):
    from levain.dream.cli import add_parsers
    import argparse

    store_dir = tmp_path / "ag"
    agent_store.set_default_zone(agent_store.open_agent_store(store_dir), ZONE)
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(store_dir), "--json"])
    assert args.func(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["created"] and doc["zone"] == ZONE and "between 22:00 and 05:00 (America/New_York)" in doc["signing_card"]
    assert "$20.00 a run and $40.00 a night" in doc["signing_card"] and "levain dream rollback" in doc["signing_card"]
    bare = tmp_path / "bare"
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(bare)])
    assert args.func(args) == 1 and "levain agents zone" in capsys.readouterr().err


def _trigger():
    return {"cron": da.CRON, "window": dict(rd.WINDOW)}


@pytest.mark.parametrize("opens,n", [((2026, 10, 30), 21), ((2026, 10, 31), 24), ((2026, 11, 7), 21), ((2027, 3, 13), 18)])
def test_the_window_has_21_slots_a_night_24_on_fall_back_and_18_on_spring_forward(opens, n):
    start = datetime(*opens, 21, 59, tzinfo=NY)
    end = datetime(*opens, 5, 0, tzinfo=NY) + timedelta(days=1)
    slots = schedule.slots_between(_trigger(), ZONE, start, end)
    assert len(slots) == n
    assert schedule.window_key(_trigger(), ZONE, slots[0]) == rd.window_key_for(date(*opens))
    assert len({schedule.window_key(_trigger(), ZONE, s) for s in slots}) == 1


# --------------------------------------------------------------------------------------------------- the input check

def occurrence_row(day=date(2026, 11, 2), kind="scheduled"):
    return {"occurrence_id": "oc-x", "agent_id": AID, "kind": kind, "window_key": rd.window_key_for(day),
            "launched_at": "2026-11-03T06:00:00Z"}


def test_the_input_check_is_ready_on_a_clean_entity_and_records_nothing_when_ready(tmp_path):
    ent = Entity(tmp_path)
    now = datetime(2026, 11, 3, 1, 0, tzinfo=NY)
    v = da.day_ready(ent.db, occurrence_row(), now=now, zone=ZONE, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir)
    assert v.ok and v.state == "ready"


def test_the_input_check_fails_closed_on_every_way_it_cannot_run(tmp_path):
    ent = Entity(tmp_path)
    now = datetime(2026, 11, 3, 1, 0, tzinfo=NY)
    for bad in (dict(store_path=tmp_path / "absent" / "memory.db"),
                dict(row={"occurrence_id": "oc-x", "kind": "scheduled", "window_key": "garbage"}),
                dict(zone="Not/AZone")):
        kw = dict(store_path=ent.db, row=occurrence_row(), now=now, zone=ZONE, agent_dir=ent.agent_dir,
                  coord_dir=ent.coord_dir)
        kw.update(bad)
        v = da.day_ready(kw.pop("store_path"), kw.pop("row"), **kw)
        assert not v.ok and v.state == rd.UNREADABLE, bad
    assert not da.unwired_on_input(occurrence_row()).ok


def test_a_skip_reason_round_trips_and_an_off_form_reason_is_never_quiet():
    v = da.InputVerdict(False, "quiet", "live-seat", "check 5: a session is live")
    assert da.parse_recorded(v.recorded()) == ("quiet", "live-seat", "check 5: a session is live")
    assert da.parse_recorded("something else")[0] == "unknown" and da.parse_recorded(None)[0] == "unknown"


# --------------------------------------------------------------------------------------- fake-clock nights (the loop
# below stands in for P6-5's on_input hook point: it claims a real slot row, asks the dream's input check, and ends
# the row the way the hook point will; a ready slot runs a real consolidate and ends done)

class Night:
    def __init__(self, tmp_path, *, episodes=3, review_feed=True):
        self.ent = Entity(tmp_path, episodes=episodes)
        self.conn = sqlite3.connect(self.ent.agent_dir / DB_NAME, isolation_level=None)
        self.n = 0

    def _end(self, oid, state, reason, at):
        self.conn.execute("UPDATE agent_occurrences SET state=?, reason=?, ended_at=? WHERE occurrence_id=?",
                          (state, reason, at, oid))

    def run(self, day: date, *, consolidate=True, fail=False, live_seat_after: int | None = None, text="n"):
        trig = _trigger()
        start = datetime(day.year, day.month, day.day, 21, 59, tzinfo=NY)
        end = datetime(day.year, day.month, day.day, 5, 0, tzinfo=NY) + timedelta(days=1)
        slots = schedule.slots_between(trig, ZONE, start, end)
        out = []
        for i, slot in enumerate(slots):
            if live_seat_after is not None and i == live_seat_after:
                self.ent.awake(["thread-a"], until=(slot + timedelta(days=1)).timestamp())
            self.conn.execute("BEGIN IMMEDIATE")
            oid, outcome = ctl.claim(self.conn, agent_id=AID, kind="scheduled", key=schedule.slot_key(slot),
                                     spec_hash="sh", binding_id="b", window_key=schedule.window_key(trig, ZONE, slot),
                                     now=slot)
            self.conn.execute("COMMIT")
            assert outcome == ctl.CLAIMED
            at = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
            row = {"occurrence_id": oid, "agent_id": AID, "kind": "scheduled",
                   "window_key": schedule.window_key(trig, ZONE, slot)}
            v = da.day_ready(self.ent.db, row, now=slot, zone=ZONE, agent_dir=self.ent.agent_dir,
                             coord_dir=self.ent.coord_dir)
            if not v.ok:
                self._end(oid, "skipped-no-input", v.recorded(), at)
                out.append((oid, "skipped-no-input"))
                continue
            # anneal stamps a wrap with the REAL clock, so the launched/ended stamps of a run use it too (the slot and
            # its key stay on the fake clock); a run whose stamps disagree with its wrap would make check 3 read the
            # wrap as foreign
            real = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
            t0 = real()
            self.conn.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=0, link_id='l', "
                              "link_incarnation='i', launched_at=? WHERE occurrence_id=?", (t0, oid))
            if fail:
                self._end(oid, "failed", "budget", real())
                out.append((oid, "failed"))
                continue
            self.n += 1
            r = dream(self.ent.db, oid, text_n(self.n))
            assert r.outcome == "saved"
            self._end(oid, "done", "completed", real())
            out.append((oid, "done"))
        return slots, out

    def rows(self):
        return rd._read_occurrences(self.ent.agent_dir)


def filed(store, **kw):
    from levain.coordinator.store import StoreAbsent

    try:
        return review.list_items(store, feed=FEED, **kw)
    except StoreAbsent:
        return []


def count(out):
    c = {}
    for _, s in out:
        c[s] = c.get(s, 0) + 1
    return c


def test_a_full_21_slot_night_is_one_done_and_twenty_skipped_no_input(tmp_path):
    n = Night(tmp_path)
    slots, out = n.run(date(2026, 10, 30))
    assert len(slots) == 21 and count(out) == {"done": 1, "skipped-no-input": 20}
    assert out[0][1] == "done"
    skipped = [r for r in n.rows() if r["state"] == "skipped-no-input"]
    assert {da.parse_recorded(r["reason"])[:2] for r in skipped} == {("quiet", "already-dreamed")}


def test_the_fall_back_night_has_24_slots_and_still_one_done(tmp_path):
    n = Night(tmp_path)
    slots, out = n.run(date(2026, 10, 31))
    assert len(slots) == 24 and count(out) == {"done": 1, "skipped-no-input": 23}


def test_three_nights_on_one_persistent_store(tmp_path):
    n = Night(tmp_path)
    root = n.ent.store_dir / "dream"
    order: list[str] = []
    for d in (date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)):
        add_episode(n.ent.db, f"episode of {d}")
        slots, out = n.run(d)
        order.extend(oid for oid, s in out if s == "done")      # real-clock stamps tie within a second: keep run order
        assert count(out).get("done") == 1, d      # check 1, 3 and 6 against the prior night's done: each night gets its own
    done = [r for r in n.rows() if r["state"] == "done"]
    assert len(done) == 3
    kept = [name for name, m in sn.snapshots(root) if m.get("post_files")]
    assert len(kept) == sn.KEEP_SNAPSHOTS == 2     # retention pruned the first night's snapshot
    assert sorted(order) == sorted(r["occurrence_id"] for r in done)
    first_rb = sn.rollback(n.ent.db, root / order[2])
    assert first_rb.ok, first_rb
    second_rb = sn.rollback(n.ent.db, root / order[1])
    assert not second_rb.ok, "a rollback after a rollback must refuse: only the newest dream is one step back"


def test_a_night_with_a_live_seat_after_the_first_slots_files_one_card_and_a_second_such_night_files_none(tmp_path):
    n = Night(tmp_path)
    n.ent.awake(["thread-a"], until=utc(2030, 1, 1).timestamp())
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    rs = CoordStore(q)
    day1, day2 = date(2026, 10, 30), date(2026, 10, 31)
    n.run(day1)
    after = datetime(2026, 10, 31, 5, 0, tzinfo=NY)
    r1 = da.close_window(agent_id=AID, day=day1, now=after, zone=ZONE, rows=n.rows(), review_store=rs, feed=FEED, occ_dir=tmp_path)
    assert r1.action == "filed" and r1.cls == "live-seat"
    assert r1.action == "filed" and len(filed(rs)) == 1
    again = da.close_window(agent_id=AID, day=day1, now=after, zone=ZONE, rows=n.rows(), review_store=rs, feed=FEED)
    assert again.action == "duplicate"
    n.run(day2)
    r2 = da.close_window(agent_id=AID, day=day2, now=after + timedelta(days=1), zone=ZONE, rows=n.rows(), review_store=rs, feed=FEED)
    assert r2.action == "standing" and len(filed(rs)) == 1
    item = filed(rs)[0]
    assert item.body["kind"] == "did-not-consolidate" and item.body["reason"].startswith("live-seat: night did not")
    assert item.authority_relevant is False


def test_a_cleared_standing_item_lets_the_next_night_file_again(tmp_path):
    n = Night(tmp_path)
    n.ent.awake(["thread-a"], until=utc(2030, 1, 1).timestamp())
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    rs = CoordStore(q)
    d1, d2 = date(2026, 10, 30), date(2026, 10, 31)
    n.run(d1)
    n.run(d2)
    da.close_window(agent_id=AID, day=d1, now=datetime(2026, 10, 31, 5, 0, tzinfo=NY), zone=ZONE, rows=n.rows(), review_store=rs, feed=FEED)
    first = filed(rs)[0]
    review.clear_item(rs, first.item_id, mode="manual")
    r = da.close_window(agent_id=AID, day=d2, now=datetime(2026, 11, 1, 5, 0, tzinfo=NY), zone=ZONE, rows=n.rows(), review_store=rs, feed=FEED)
    assert r.action == "filed" and len(filed(rs, include_cleared=True)) == 2


def test_a_failing_night_files_exactly_one_failed_card_not_one_per_slot(tmp_path):
    n = Night(tmp_path)
    slots, out = n.run(date(2026, 10, 30), fail=True)
    assert count(out) == {"failed": 1, "skipped-no-input": 20}
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    rs = CoordStore(q)
    r = da.close_window(agent_id=AID, day=date(2026, 10, 30), now=datetime(2026, 10, 31, 5, 0, tzinfo=NY), zone=ZONE, rows=n.rows(),
                        review_store=rs, feed=FEED)
    assert r.action == "filed" and r.cls == "failed" and "budget" in r.detail
    assert len(filed(rs)) == 1


def test_a_quiet_day_with_nothing_to_consolidate_files_no_card(tmp_path):
    n = Night(tmp_path, episodes=0)
    slots, out = n.run(date(2026, 10, 30))
    assert count(out) == {"skipped-no-input": 21}
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    r = da.close_window(agent_id=AID, day=date(2026, 10, 30), now=datetime(2026, 10, 31, 5, 0, tzinfo=NY), zone=ZONE, rows=n.rows(),
                        review_store=CoordStore(q), feed=FEED)
    assert r.action == "quiet-day" and r.cls == ""


# ----------------------------------------------------------------------------------------- close_window, the gates

def row(state, *, reason=None, key="2026-10-31T02:00:00Z", launched=None, day=date(2026, 10, 30)):
    return {"agent_id": AID, "kind": "scheduled", "window_key": rd.window_key_for(day), "state": state,
            "reason": reason, "key": key, "occurrence_id": f"oc-{key}", "launched_at": launched}


@pytest.fixture
def rs(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    return CoordStore(q)


CLOSED = datetime(2026, 10, 31, 5, 0, tzinfo=NY)


def close(rs, rows, **kw):
    kw.setdefault("now", CLOSED)
    kw.setdefault("agent_id", AID)
    return da.close_window(day=date(2026, 10, 30), zone=ZONE, rows=rows, review_store=rs, feed=FEED, **kw)


def test_the_window_decides_nothing_before_the_dream_day_ends(rs):
    r = close(rs, [row("skipped-no-input", reason="unreadable/unreadable: x")], now=CLOSED - timedelta(seconds=1))
    assert r.action == "waiting" and filed(rs) == []


def test_a_run_still_launched_or_admitted_holds_the_filer(rs):
    for state in ("launched", "admitted"):
        r = close(rs, [row("skipped-no-input", reason="unreadable/unreadable: x"), row(state, key="2026-10-31T08:40:00Z")])
        assert r.action == "waiting"
    assert filed(rs) == []


def test_a_done_row_files_nothing_and_a_window_with_no_rows_files_nothing(rs):
    assert close(rs, [row("done"), row("skipped-no-input", reason="x")]).action == "consolidated"
    assert close(rs, []).action == "no-occurrences"
    assert filed(rs) == []


def test_every_unreadable_or_unknown_or_unrecognised_quiet_reason_is_news(rs):
    r = close(rs, [row("skipped-no-input", reason="unreadable/unreadable: x")])
    assert r.action == "filed" and filed(rs)[0].body["reason"].startswith("unreadable: night did not")
    for reason in ("quiet/baton: held", "quiet/wrap-open: w", "quiet/ceiling: c", "garbled", None):
        cls, _ = da.classify_window([row("skipped-no-input", reason=reason)])
        assert cls != "", reason


def test_consolidated_today_and_already_dreamed_are_not_news():
    for kind in ("consolidated-today", "already-dreamed", "nothing-to-consolidate"):
        assert da.classify_window([row("skipped-no-input", reason=f"quiet/{kind}: r")])[0] == ""


def test_the_last_slots_reason_is_the_one_filed():
    rows = [row("skipped-no-input", reason="quiet/nothing-to-consolidate: n", key="2026-10-31T02:00:00Z"),
            row("skipped-no-input", reason="quiet/live-seat: seat", key="2026-10-31T08:40:00Z")]
    assert da.classify_window(rows) == ("live-seat", "seat")


def test_a_slept_laptop_files_laptop_slept_and_two_missed_windows_raise_one_attention_row(rs):
    seen = []
    key = rd.window_key_for(date(2026, 10, 30))
    r = close(rs, [], missed_windows=[key], miss_streak=2, attention=lambda k, t: seen.append((k, t)))
    assert r.action == "filed" and r.cls == "laptop-slept" and r.attention == [f"dream-miss-streak:{AID}"]
    assert seen[0][0] == f"dream-miss-streak:{AID}"
    r1 = close(rs, [], missed_windows=[key], miss_streak=1, attention=lambda k, t: seen.append((k, t)))
    assert r1.action == "duplicate" and len(seen) == 1


def test_missed_rows_alone_are_a_slept_laptop(rs):
    assert close(rs, [row("missed"), row("missed", key="k2")]).cls == "laptop-slept"


def test_without_the_agent_feed_the_card_is_refused_and_nothing_is_filed(rs):
    r = da.close_window(agent_id=AID, day=date(2026, 10, 30), now=CLOSED, zone=ZONE, review_store=rs,
                        rows=[row("skipped-no-input", reason="quiet/live-seat: s")])
    assert (r.action == "refused") == ("agent" not in review.FEEDS)
    if r.action == "refused":
        assert review.list_items(rs) == [] and r.why


# -------------------------------------------------------------------------------------- pause, renewal, drill, win

def test_the_pause_names_the_two_failures_and_the_resume_command_once_per_key():
    seen = {}
    agent = {"agent_id": "ag-1", "name": "dream", "operation": "paused", "failure_streak": 2}
    failed = [{"occurrence_id": "oc-a", "reason": "budget", "ended_at": "2026-10-30T03:00:00Z"},
              {"occurrence_id": "oc-b", "reason": "deadline", "ended_at": "2026-10-31T03:00:00Z"},
              {"occurrence_id": "oc-c", "reason": "refused", "ended_at": "2026-11-01T03:00:00Z"}]
    assert da.pause_notice(agent, failed, attention=lambda k, t: seen.update({k: t}))
    assert list(seen) == ["dream-paused:ag-1"] and "oc-b" in seen[list(seen)[0]] and "oc-c" in seen[list(seen)[0]]
    assert "oc-a" not in seen[list(seen)[0]] and "levain agents resume dream" in seen[list(seen)[0]]
    assert not da.pause_notice({**agent, "operation": "active"}, failed, attention=lambda k, t: 1 / 0)
    assert not da.pause_notice({**agent, "failure_streak": 1}, failed, attention=lambda k, t: 1 / 0)


def test_the_renewal_card_comes_three_days_out_once_and_is_his_to_clear(rs):
    seen = []
    link = {"id": "al-0123456789ab", "expires_at": "2026-11-20T00:00:00Z"}
    att = lambda k, t: seen.append(k)   # noqa: E731
    assert da.renewal_check(link, now=utc(2026, 11, 16), review_store=rs, attention=att, feed=FEED).action == "not-due"
    r = da.renewal_check(link, now=utc(2026, 11, 17, 1), review_store=rs, attention=att, feed=FEED)
    assert r.action == "filed" and 2.9 < r.days_left < 3
    assert da.renewal_check(link, now=utc(2026, 11, 18), review_store=rs, attention=att, feed=FEED).action == "duplicate"
    item = filed(rs)[0]
    assert item.authority_relevant is True and item.body["kind"] == "renewal-due" and seen == []
    assert da.renewal_check(link, now=utc(2026, 11, 20), review_store=rs, attention=att, feed=FEED).action == "expired"
    assert seen == ["dream-link-expired:al-0123456789ab"]
    assert da.renewal_check({"id": "x"}, now=utc(2026, 11, 20), review_store=rs, attention=att).action == "refused"


def test_the_drill_runs_on_its_own_seven_day_cadence_and_only_with_a_snapshot(scratch_store):
    db = scratch_store
    root = db.parent / "dream"
    seen, resolved = [], []
    kw = dict(attention=lambda k, t: seen.append((k, t)), resolve=resolved.append)
    assert da.drill_tick(db, root, **kw).action == "no-snapshot"
    assert dream(db, "o1", text_n(1)).outcome == "saved"
    assert da.drill_tick(db, root, **kw).action == "ok" and resolved == ["dream-drill"]
    assert da.drill_tick(db, root, **kw).action == "not-due"
    later = datetime.now(timezone.utc) + timedelta(days=7, seconds=5)
    assert da.drill_tick(db, root, now=later, **kw).action == "ok"
    bk = root / "o1" / "snap" / sn.DB_BACKUP
    bk.write_bytes(bk.read_bytes()[:-100] + b"\0" * 100)
    r = da.drill_tick(db, root, now=later + timedelta(days=8), **kw)
    assert r.action == "failed" and seen and seen[0][0] == "dream-drill" and sn.DB_BACKUP in seen[0][1]


def test_a_deferred_drill_raises_the_row_only_after_two_cadences(tmp_path):
    seen = []

    class Res:
        outcome, steps = "deferred", []

    db = tmp_path / "s" / "memory.db"
    root = tmp_path / "s" / "dream"
    (root / "o1" / "snap").mkdir(parents=True)
    sn._write_json(root / "o1" / "snap" / sn.MANIFEST, {"occurrence": "o1", "taken_at": "2026-10-01T00:00:00Z",
                                                        "post_files": {"continuity": "a" * 64}, "files": {}})
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    sn._write_json(root / sn.DRILL_RECORD, {"outcome": "ok", "at": (now - timedelta(days=8)).isoformat()})
    assert da.drill_tick(db, root, now=now, attention=lambda k, t: seen.append(k), drill=lambda *a, **k: Res).action == "deferred"
    assert seen == []
    sn._write_json(root / sn.DRILL_RECORD, {"outcome": "ok", "at": (now - timedelta(days=15)).isoformat()})
    da.drill_tick(db, root, now=now, attention=lambda k, t: seen.append(k), drill=lambda *a, **k: Res)
    assert seen == ["dream-drill"]


@pytest.fixture
def scratch_store(tmp_path):
    from anneal_memory import FLOW_SCHEMA, Store

    d = tmp_path / "store"
    d.mkdir()
    with Store(str(d / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
    return d / "memory.db"


# ------------------------------------------------------------------------------------------------------ the fire body

from tests.test_agents_supervise_fire import Ex, _fire, _supervisor, rig  # noqa: E402,F401


def _consolidate(db, occ_dir, text="n"):
    def fn():
        on_snap, on_saved = sn.hooks()
        return ex.consolidate_store(db, mind=Fake(text_n(7)), occurrence_dir=occ_dir, max_seconds=3000,
                                    on_snapshot=on_snap, on_saved=on_saved)
    return fn


def test_the_fire_body_runs_the_effect_files_the_card_and_ends_done_with_the_win_verified(rig, scratch_store, tmp_path):
    from levain.agents import supervise

    db = scratch_store
    occ_dir = db.parent / "dream" / "oc-run"
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    review_store = CoordStore(q)
    verdicts = []

    def verify(end):
        v = da.win_check(end, locate=lambda oid: (db, occ_dir, review_store), feed=FEED)
        verdicts.append(v)
        return v

    sup, journal = _supervisor(rig, hooks=supervise.Hooks(verify=verify))
    oid = _fire(rig, "dream-run", journal)
    rep = da.run_body(sup, oid, 0, store_path=db, occ_dir=occ_dir, executor=Ex(), consolidate=_consolidate(db, occ_dir),
                      review_store=review_store, day="2026-11-02", feed=FEED)
    assert rep.outcome == "saved" and rep.card == "filed" and rep.state == "done" and rep.code == 0
    assert verdicts == [None]
    assert rig.row(oid)["state"] == "done"
    assert json.loads((occ_dir / da.RESULT_FILE).read_text())["outcome"] == "saved"
    assert filed(review_store)[0].body["kind"] == "consolidated"


def test_a_card_that_cannot_be_filed_ends_the_run_failed_not_done(rig, scratch_store, tmp_path):
    db = scratch_store
    occ_dir = db.parent / "dream" / "oc-run"
    sup, journal = _supervisor(rig)
    oid = _fire(rig, "dream-run", journal)
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    rep = da.run_body(sup, oid, 0, store_path=db, occ_dir=occ_dir, executor=Ex(), consolidate=_consolidate(db, occ_dir),
                      review_store=CoordStore(q), feed="not-a-feed")
    assert rep.outcome == "saved" and rep.card.startswith("refused") and rep.state == "failed" and rep.code == 1
    assert rig.row(oid)["state"] == "failed" and rig.row(oid)["reason"] == "card_not_filed"


def test_a_refused_compose_ends_failed_with_the_outcome_and_files_a_failed_card(rig, scratch_store, tmp_path):
    db = scratch_store
    occ_dir = db.parent / "dream" / "oc-run"
    sup, journal = _supervisor(rig)
    oid = _fire(rig, "dream-run", journal)
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    review_store = CoordStore(q)

    class Refuses(Fake):
        def compose(self, req):
            return ex.ComposeResult(text="not a neocortex", model="fake", unit="tokens", input_tokens=1, output_tokens=1)

    def fn():
        return ex.consolidate_store(db, mind=Refuses(""), occurrence_dir=occ_dir, max_seconds=3000)

    rep = da.run_body(sup, oid, 0, store_path=db, occ_dir=occ_dir, executor=Ex(), consolidate=fn,
                      review_store=review_store, feed=FEED)
    assert rep.outcome != "saved" and rep.state == "failed" and rep.code == 1
    assert rig.row(oid)["reason"] == rep.outcome
    assert filed(review_store)[0].body["kind"] == "failed"


def test_an_empty_run_is_done_with_no_card_and_a_win_that_accepts_the_empty_report(rig, scratch_store, tmp_path):
    from levain.agents import supervise

    db = scratch_store
    occ_dir = db.parent / "dream" / "oc-run"
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    review_store = CoordStore(q)
    sup, journal = _supervisor(rig, hooks=supervise.Hooks(
        verify=lambda end: da.win_check(end, locate=lambda oid: (db, occ_dir, review_store), feed=FEED)))
    oid = _fire(rig, "dream-run", journal)

    class Empty:
        def as_dict(self):
            return {"outcome": "empty", "exit_code": 0}

    rep = da.run_body(sup, oid, 0, store_path=db, occ_dir=occ_dir, executor=Ex(), consolidate=Empty,
                      review_store=review_store, feed=FEED)
    assert rep.state == "done" and rep.card == "none" and filed(review_store) == []


def test_the_win_check_turns_a_done_without_its_card_or_its_wrap_into_a_reason(scratch_store, tmp_path):
    db = scratch_store
    occ_dir = db.parent / "dream" / "oc-w"
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    review_store = CoordStore(q)
    end = type("E", (), {"occurrence_id": "oc-w", "state": "done"})()
    loc = lambda oid: (db, occ_dir, review_store)   # noqa: E731
    assert "cannot verify" in da.win_check(end, locate=loc)            # no result.json yet
    on_snap, on_saved = sn.hooks()
    res = ex.consolidate_store(db, mind=Fake(text_n(3)), occurrence_dir=occ_dir, max_seconds=3000,
                               on_snapshot=on_snap, on_saved=on_saved)
    (occ_dir / da.RESULT_FILE).write_text(json.dumps(res.as_dict(), default=str))
    assert da.win_check(end, locate=loc, feed=FEED) == "win: the card is not filed"
    c = cd.from_spec({"kind": "consolidated", "occurrence": "oc-w"}, occ_dir=occ_dir, db_path=db)
    cd.file_card(review_store, c, feed=FEED)
    assert da.win_check(end, locate=loc, feed=FEED) is None
    m = sn.read_manifest(occ_dir)
    m["wrap_id"] = 999
    sn._write_manifest(occ_dir, m)
    assert "records no completed wrap" in da.win_check(end, locate=loc, feed=FEED)
    assert da.win_check(type("E", (), {"occurrence_id": "oc-w", "state": "failed"})(), locate=lambda o: None) is None
    assert "cannot be located" in da.win_check(end, locate=lambda o: None)


def test_the_win_registration_seam_registers_only_when_the_hook_exists():
    got = []
    assert da.register_win_check(None, lambda o: None) is False
    assert da.register_win_check(lambda name, fn: got.append(name), lambda o: None) is True and got == [da.WIN_CHECK]

    def boom(name, fn):
        raise RuntimeError("x")

    assert da.register_win_check(boom, lambda o: None) is False


def test_a_run_that_cannot_be_built_exits_1_and_runs_nothing():
    run = da.make_run(lambda store, oid, epoch: (_ for _ in ()).throw(RuntimeError("no authority")))
    assert run(None, "oc-x", 0) == 1


# ------------------------------------------------------------------------------ the registered id, the exclusion (L3 r1)

def _claim(conn, slot, *, agent_id=AID, kind="scheduled", window="w"):
    conn.execute("BEGIN IMMEDIATE")
    oid, outcome = ctl.claim(conn, agent_id=agent_id, kind=kind, key=schedule.slot_key(slot), spec_hash="sh",
                             binding_id="b", window_key=window, now=slot)
    conn.execute("COMMIT")
    assert outcome == ctl.CLAIMED
    return oid


def test_the_id_the_dream_runs_under_is_the_one_register_dream_minted(astore):
    reg = da.register_dream(astore)
    assert reg.agent_id.startswith("ag-") and reg.agent_id != da.NAME
    assert not hasattr(da, "AGENT_ID_DEFAULT")


def test_the_input_check_and_the_filer_take_the_agent_id_from_the_row_and_never_default_it(tmp_path):
    import inspect

    assert inspect.signature(da.close_window).parameters["agent_id"].default is inspect.Parameter.empty
    assert "agent_id" not in inspect.signature(da.day_ready).parameters
    ent = Entity(tmp_path)
    now = datetime(2026, 11, 3, 1, 0, tzinfo=NY)
    for bad in ({"agent_id": None}, {"agent_id": ""}, {"agent_id": "  "}, {"agent_id": 7}, {"occurrence_id": ""}):
        v = da.day_ready(ent.db, {**occurrence_row(), **bad}, now=now, zone=ZONE, agent_dir=ent.agent_dir,
                         coord_dir=ent.coord_dir)
        assert not v.ok and v.state == rd.UNREADABLE, bad
    gone = {k: v for k, v in occurrence_row().items() if k != "agent_id"}
    assert not da.day_ready(ent.db, gone, now=now, zone=ZONE, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir).ok
    with pytest.raises(ValueError):
        da.close_window(agent_id="", day=date(2026, 10, 30), now=CLOSED, zone=ZONE, rows=[], review_store=None)


def test_a_prior_failed_run_of_the_registered_agent_is_seen_by_the_input_check(tmp_path):
    n = Night(tmp_path)
    slots, _ = n.run(date(2026, 10, 30), fail=True)
    assert [r["state"] for r in n.rows()].count("failed") == 1      # one attempt, then the window is spent (check 6)
    row = {"occurrence_id": "oc-next", "agent_id": AID, "kind": "scheduled", "window_key": n.rows()[0]["window_key"]}
    v = da.make_on_input(n.ent.db, zone=ZONE, agent_dir=n.ent.agent_dir, coord_dir=n.ent.coord_dir,
                         clock=lambda: slots[-1])(row)
    assert not v.ok and v.state == rd.QUIET


def test_the_occurrence_under_evaluation_is_excluded_by_the_id_the_hook_is_given_and_no_other(tmp_path):
    ent = Entity(tmp_path)
    conn = sqlite3.connect(ent.agent_dir / DB_NAME, isolation_level=None)
    now = datetime(2026, 11, 3, 1, 0, tzinfo=NY)
    key = rd.window_key_for(date(2026, 11, 2))
    mine = _claim(conn, now, window=key)
    row = {"occurrence_id": mine, "agent_id": AID, "kind": "scheduled", "window_key": key}
    kw = dict(now=now, zone=ZONE, agent_dir=ent.agent_dir, coord_dir=ent.coord_dir)
    assert da.day_ready(ent.db, row, **kw).ok          # its own admitted row is not "a dream in flight"
    other = "oc-other-admitted"          # a second in-flight row (a crash leftover; claim itself would overlap-skip)
    conn.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, window_key, spec_hash,"
                 " binding_id, run_id, admitted_at) VALUES (?, ?, 'scheduled', 'k-other', 'admitted', ?, 'sh', 'b',"
                 " 'r-other', '2026-11-03T05:00:00Z')", (other, AID, key))
    v = da.day_ready(ent.db, row, **kw)
    assert not v.ok and v.kind == "in-flight" and other in v.reason       # a different admitted row still is
    assert not da.day_ready(ent.db, {**row, "occurrence_id": "oc-not-mine"}, **kw).ok   # id decides, not the position


def test_a_failed_row_that_never_launched_is_a_failed_window_not_a_quiet_day():
    quiet = row("skipped-no-input", reason="quiet/nothing-to-consolidate: n", key="2026-10-31T02:00:00Z")
    for state in ("failed", "stopped"):
        died = row(state, reason="claim_refused", key="2026-10-31T01:00:00Z", launched=None)
        cls, detail = da.classify_window([died, quiet])
        assert cls == "failed" and "claim_refused" in detail, state


def test_the_standing_rule_holds_across_three_nights_not_just_two(rs):
    def night(day):
        return da.close_window(agent_id=AID, day=day, now=datetime(day.year, day.month, day.day, 5, tzinfo=NY)
                               + timedelta(days=1), zone=ZONE, review_store=rs, feed=FEED, rows=[
            row("skipped-no-input", reason="quiet/live-seat: seat", day=day)])

    d1 = date(2026, 10, 30)
    assert night(d1).action == "filed"
    assert night(d1 + timedelta(days=1)).action == "standing"
    assert night(d1 + timedelta(days=2)).action == "standing"      # night 3 looks past night 2's suppressed item
    assert len(filed(rs)) == 1
    # newest-first processing cannot file a second card for a class that already stands
    assert night(d1 - timedelta(days=1)).action == "standing"
    assert len(filed(rs)) == 1


def test_a_card_filed_by_someone_else_in_the_same_window_reads_duplicate_not_filed(rs, monkeypatch):
    real = cd.file_card

    def lost_race(store, c, **kw):
        item, _ = real(store, c, **kw)
        return item, False

    monkeypatch.setattr(cd, "file_card", lost_race)
    r = close(rs, [row("skipped-no-input", reason="unreadable/unreadable: x")])
    assert r.action == "duplicate"


def test_an_unreadable_snapshot_root_is_a_failed_drill_with_its_row_not_no_snapshot(tmp_path):
    import os

    root = tmp_path / "s" / "dream"
    (root / "o1").mkdir(parents=True)
    seen = []
    os.chmod(root, 0)
    try:
        r = da.drill_tick(tmp_path / "s" / "memory.db", root, attention=lambda k, t: seen.append(k))
    finally:
        os.chmod(root, 0o700)
    assert r.action == "failed" and seen == ["dream-drill"]


def test_a_drill_failure_whose_row_could_not_be_raised_is_raised_again_on_the_next_tick(tmp_path):
    db = tmp_path / "s" / "memory.db"
    root = tmp_path / "s" / "dream"
    (root / "o1" / "snap").mkdir(parents=True)
    sn._write_json(root / "o1" / "snap" / sn.MANIFEST, {"occurrence": "o1", "taken_at": "2026-10-01T00:00:00Z",
                                                        "post_files": {"continuity": "a" * 64}, "files": {}})
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)

    class Bad:
        outcome = "failed"
        steps = [{"name": "hash", "status": "failed", "detail": "torn"}]

        def as_dict(self):
            return {"outcome": "failed", "at": now.isoformat(), "steps": self.steps}

    def drill(*a, **k):                       # records like the real drill: the record lands before the row is raised
        sn._write_json(root / sn.DRILL_RECORD, Bad().as_dict())
        return Bad()

    def boom(k, t):
        raise OSError("attention store down")

    assert da.drill_tick(db, root, now=now, attention=boom, drill=drill).action == "failed"
    seen = []
    again = da.drill_tick(db, root, now=now + timedelta(hours=1), attention=lambda k, t: seen.append((k, t)),
                          drill=lambda *a, **k: 1 / 0)
    assert again.action == "not-due" and seen and seen[0][0] == "dream-drill" and "torn" in seen[0][1]
    # and a good drill whose resolve failed is resolved again
    sn._write_json(root / sn.DRILL_RECORD, {"outcome": "ok", "at": now.isoformat(), "steps": []})
    res = []
    assert da.drill_tick(db, root, now=now + timedelta(hours=2), attention=lambda k, t: 1 / 0,
                         resolve=res.append).action == "not-due"
    assert res == ["dream-drill"]


def test_a_marker_moved_after_the_role_is_an_enable_refusal_not_a_bare_store_refusal(astore, monkeypatch):
    """fixk2b (L3 land12 r1, complement LOW 3): register_spec(attach=True) checks the marker in its own transaction, so a
    marker another process moved between add_role and register_spec raised AgentStoreRefused out of register_dream, whose
    contract is EnableRefused for every refusal."""
    real = role_mod.add_role

    def add_then_move(store, role):
        out = real(store, role)
        c = sqlite3.connect(astore.directory / DB_NAME, isolation_level=None)
        c.execute("UPDATE meta SET value = 'levain-agent-schema/5' WHERE key = 'agent_schema'")
        c.close()
        return out

    monkeypatch.setattr(role_mod, "add_role", add_then_move)
    with pytest.raises(da.EnableRefused, match="unknown agent schema marker 'levain-agent-schema/5'"):
        da.register_dream(astore)


@pytest.mark.parametrize("revise", [False, True])
def test_a_marker_moved_before_the_call_is_an_enable_refusal_from_the_first_store_read(astore, revise):
    """Diogenes 11-06 LOW: only register_spec sat inside the try, so a store whose marker moved BEFORE register_dream ran
    raised a bare AgentStoreRefused from the earlier store calls (the default zone, the held names, the same-content
    look, add_role). Reproduced on origin/main; EnableRefused is the contract for every refusal."""
    c = sqlite3.connect(astore.directory / DB_NAME, isolation_level=None)
    c.execute("UPDATE meta SET value = 'levain-agent-schema/5' WHERE key = 'agent_schema'")
    c.close()
    with pytest.raises(da.EnableRefused, match="unknown agent schema marker 'levain-agent-schema/5'"):
        da.register_dream(astore, revise=revise)


def test_a_defect_in_the_shipped_spec_is_not_dressed_as_an_enable_refusal(astore, monkeypatch):
    """L1/L2/L3 r0 (four lineages): parse_spec is pure, so a SpecError from our own template is a bug the caller's
    catch-all must show as one; only the store calls translate into EnableRefused."""
    def bad(*a, **k):
        raise spec_mod.SpecError("template defect")

    monkeypatch.setattr(spec_mod, "parse_spec", bad)
    with pytest.raises(spec_mod.SpecError, match="template defect"):
        da.register_dream(astore)
