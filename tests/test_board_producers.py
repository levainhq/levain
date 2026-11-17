"""Producer-block evidence, fail-closed per producer (T4, board levain#89): a positive claim from a readable producer
stands, absence is concluded only when every expected producer is readable and complete, and the native producer makes
``levain board state`` work for an operator who has no flow."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.desk import board_producers as bp
from levain.desk import board_state as bs

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
FETCHED = "2026-11-12T19:58:00Z"


def _stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def blk(producer, rows, *, age=1.0, complete=True, store_id="s1", **kw):
    return {"producer": producer, "store_id": store_id, "captured_at": _stamp(age), "complete": complete, "rows": rows, **kw}


def _item(n, **kw):
    rec = {"v": 1, "op": "add", "id": f"levain#{n}", "at": "2026-11-12T10:00:00Z", "by": "t", "repo": "levain",
           "title": f"item {n}", "words": "", "origin": "t", "done": "it lands", "done_kind": "merged", "tier": "sonnet",
           "depends_on": []}
    rec.update(kw)
    return json.dumps(rec)


def ev2(*, seats, band, waiters, producers=("levain", "flow"), **over):
    e = {"evidence_v": 2, "fetched_at": FETCHED, "unreadable": {}, "main": [], "chain": [], "refs": [], "merged": [],
         "branches": [], "documents": {}, "unattributed": [], "seats": seats, "band": band, "waiters": waiters,
         "producers": producers if isinstance(producers, dict) else list(producers)}
    e.update(over)
    return e


def board_of(evidence, *items, store_id="s1"):
    folded = bs.fold(list(items))
    b = bs.derive_all(folded, bs.Evidence.from_dict(evidence, now=NOW, store_id=store_id), now=NOW)
    return {s.id: s for s in b.items}, b


def whole(producer, *, seats=(), band=(), waiters=(), **kw):
    return {"seats": [blk(producer, list(seats), **kw)], "band": [blk(producer, list(band), **kw)],
            "waiters": [blk(producer, list(waiters), **kw)]}


def merged(*parts):
    out = {"seats": [], "band": [], "waiters": []}
    for p in parts:
        for k in out:
            out[k] += p[k]
    return out


def test_every_producer_whole_and_readable_concludes_absence():
    got, _ = board_of(ev2(**merged(whole("levain"), whole("flow"))), _item(1))
    assert got["levain#1"].state == "ready"


def test_a_malformed_seat_row_makes_that_producer_incomplete_and_only_absence_items_go_unknown():
    seats = [blk("levain", [{"name": "s", "items": ["levain#2"], "since": None, "branch": None}, {"name": "broken"}]),
             blk("flow", [])]
    parts = merged(whole("flow"), whole("levain"))
    parts["seats"] = seats
    got, b = board_of(ev2(**parts), _item(1), _item(2))
    assert got["levain#1"].state == "unknown"
    assert got["levain#1"].reason.startswith("seats: producer levain unreadable (malformed row: ")
    # the row beside the broken one is not used either: its producer is unreadable as a whole, but the flow block says nothing of #2
    assert got["levain#2"].state == "unknown"
    assert any(a.why.startswith("seats unreadable: producer levain unreadable") for a in b.audit)


def test_a_stale_producer_names_its_age_and_the_source():
    parts = merged(whole("levain"), whole("flow", age=16))
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "unknown"
    assert "seats: producer flow unreadable (stale 16 min)" in got["levain#1"].reason
    assert "waiters: producer flow unreadable (stale 16 min)" in got["levain#1"].reason


def test_fifteen_minutes_is_still_readable():
    parts = merged(whole("levain", age=14.9), whole("flow", age=14.9))
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "ready" and bs.PRODUCER_MAX_AGE_S == 900


def test_a_block_from_another_store_is_not_readable_and_says_so():
    parts = merged(whole("levain"), whole("flow", store_id="other"))
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "unknown" and "evidence from another store" in got["levain#1"].reason


def test_with_no_store_id_known_a_block_naming_any_id_is_readable():
    parts = merged(whole("levain", store_id=None), whole("flow", store_id="whatever"))
    got, _ = board_of(ev2(**parts), _item(1), store_id=None)
    assert got["levain#1"].state == "ready"


def test_a_block_naming_no_store_when_the_store_has_one_is_unreadable():
    parts = merged(whole("levain"), whole("flow", store_id=None))
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "unknown" and "names no store_id" in got["levain#1"].reason


def test_a_positive_claim_from_a_readable_producer_survives_another_producer_being_unreadable():
    seat = {"name": "lane", "items": ["levain#1"], "since": FETCHED, "branch": None}
    parts = merged(whole("levain", seats=[seat]), whole("flow"))
    parts["seats"] = [blk("levain", [seat]), blk("flow", [], age=41)]          # flow's SEATS are stale; its band and waiters are fresh
    got, _ = board_of(ev2(**parts), _item(1), _item(2))
    assert got["levain#1"].state == "in_flight"          # the claim stands: in_flight rests on the band and the waiters, which are whole
    assert got["levain#2"].state == "unknown"            # the absence does not
    assert "producer flow unreadable (stale 41 min)" in got["levain#2"].reason


def test_a_positive_claim_rests_on_every_higher_state_whose_source_is_unreadable():
    """F3: precedence is part of the predicate. An item a seat holds could still be needs_you or landing if the band or the
    waiters, which rank above in_flight, cannot be read; an item a band row names is needs_you whatever else is unread."""
    seat = {"name": "lane", "items": ["levain#1"], "since": FETCHED, "branch": None}
    row = {"id": "np-aaaaaaaa", "state": "open", "items": ["levain#2"], "by": "", "grade": "unknown"}
    parts = merged(whole("levain", seats=[seat], band=[row]), whole("flow"))
    parts["band"] = [blk("levain", [row]), blk("flow", [], age=41)]
    got, _ = board_of(ev2(**parts), _item(1), _item(2))
    assert got["levain#1"].state == "unknown" and "band: producer flow unreadable (stale 41 min)" in got["levain#1"].reason
    assert got["levain#2"].state == "needs_you"
    parts = merged(whole("levain", seats=[seat]), whole("flow"))
    parts["waiters"] = [blk("levain", []), blk("flow", [], age=41)]
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "unknown" and "waiters: producer flow unreadable" in got["levain#1"].reason
    parts = merged(whole("levain", seats=[seat]), whole("flow"))
    parts["seats"] = [blk("levain", [seat]), blk("flow", [], age=41)]
    assert board_of(ev2(**parts), _item(1))[0]["levain#1"].state == "in_flight"


def test_an_open_band_row_and_a_waiter_are_positive_claims_too():
    row = {"id": "np-aaaaaaaa", "state": "open", "items": ["levain#1"], "by": "", "grade": "unknown"}
    waiter = {"branch": "queue/x", "items": ["levain#2"]}
    parts = merged(whole("levain", band=[row]), whole("flow"))
    parts["waiters"] = [blk("levain", [waiter]), blk("flow", [], age=41)]
    got, _ = board_of(ev2(**parts), _item(1), _item(2))
    assert got["levain#1"].state == "needs_you" and got["levain#2"].state == "landing"


def test_an_item_closed_in_git_is_done_whatever_a_producer_says():
    main = [{"sha": "a" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#1"], "closes": ["levain#1"]}]
    got, _ = board_of(ev2(**merged(whole("levain", age=99), whole("flow", age=99)), main=main), _item(1))
    assert got["levain#1"].state == "done"


def test_a_decided_item_never_reads_unknown_for_a_producer():
    decide = json.dumps({"v": 1, "op": "decide", "id": "levain#1", "at": "2026-11-12T11:00:00Z", "by": "t",
                         "decision": "cut", "words": "no", "date": "2026-11-12"})
    got, _ = board_of(ev2(**merged(whole("levain", age=99), whole("flow", age=99))), _item(1), decide)
    assert got["levain#1"].state == "cut"


def test_a_missing_block_from_an_expected_producer_blocks_absence():
    parts = whole("levain")
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "unknown" and "producer flow unreadable (no block supplied)" in got["levain#1"].reason


def test_two_blocks_for_one_producer_are_one_producers_rows_together():
    seat_a = {"name": "a", "items": ["levain#1"], "since": FETCHED, "branch": None}
    seat_b = {"name": "b", "items": ["levain#2"], "since": FETCHED, "branch": None}
    parts = whole("levain")
    parts["seats"] = [blk("levain", [seat_a]), blk("levain", [seat_b])]
    got, _ = board_of(ev2(**parts, producers=("levain",)), _item(1), _item(2), _item(3))
    assert got["levain#1"].state == "in_flight" and got["levain#2"].state == "in_flight" and got["levain#3"].state == "ready"
    parts["seats"] = [blk("levain", [seat_a]), blk("levain", [], complete=False, why="part unread")]
    got, _ = board_of(ev2(**parts, producers=("levain",)), _item(1), _item(3))
    assert got["levain#1"].state == "in_flight"          # a row of a block that said it was partial still stands
    assert got["levain#3"].state == "unknown" and "producer levain unreadable (incomplete: part unread)" in got["levain#3"].reason


def test_a_block_that_says_incomplete_blocks_absence_and_carries_its_why():
    parts = merged(whole("levain"), whole("flow"))
    parts["band"] = [blk("levain", [], complete=False, why="a source was not read"), blk("flow", [])]
    got, _ = board_of(ev2(**parts), _item(1))
    assert "band: producer levain unreadable (incomplete: a source was not read)" in got["levain#1"].reason


def test_a_per_source_expectation_leaves_waiters_to_the_producer_that_runs_them():
    parts = merged(whole("levain"), whole("flow"))
    parts["waiters"] = [blk("flow", [])]
    prod = {"seats": ["levain", "flow"], "band": ["levain", "flow"], "waiters": ["flow"]}
    got, _ = board_of(ev2(**parts, producers=prod), _item(1))
    assert got["levain#1"].state == "ready"


def test_no_producer_for_a_source_reads_not_supplied():
    parts = merged(whole("levain"))
    parts["waiters"] = []
    got, _ = board_of(ev2(**parts, producers={"seats": ["levain"], "band": ["levain"], "waiters": []}), _item(1))
    assert got["levain#1"].reason == "waiters: not supplied"


def test_a_resolved_band_row_carries_its_grade():
    rows = [{"id": "np-1", "state": "resolved", "items": [], "by": "phone", "grade": "signed-phone"},
            {"id": "np-2", "state": "resolved", "items": [], "by": "cli", "grade": "unknown"}]
    ev = bs.Evidence.from_dict(ev2(**merged(whole("levain", band=rows), whole("flow"))), now=NOW, store_id="s1")
    assert ev.band_resolved == {"np-1": "phone", "np-2": "cli"} and ev.band_grade == {"np-1": "signed-phone", "np-2": "unknown"}


@pytest.mark.parametrize("row, why", [
    ({"id": "np-1", "state": "resolved", "items": [], "by": "cli"}, "grade is required"),
    ({"id": "np-1", "state": "resolved", "by": "cli", "grade": "unknown"}, "items is required"),
    ({"id": "np-1", "state": "resolved", "items": [], "grade": "unknown"}, "by is required"),
    ({"id": "np-1", "items": [], "by": "cli", "grade": "unknown"}, "state"),
    ({"id": "np-1", "state": "resolved", "items": "levain#1", "by": "cli", "grade": "unknown"}, "list"),
    ({"id": "np-1", "state": "resolved", "items": ["levain#1"], "by": 7, "grade": "unknown"}, "strings"),
    ({"id": "np-1", "state": "resolved", "items": ["levain#1"], "by": "", "grade": "unknown"}, "no resolver"),
])
def test_a_band_row_that_is_not_whole_makes_that_producer_incomplete_and_closes_nothing(row, why):
    """F9: a row missing a key (or holding the wrong type, or resolved by no one) is malformed; it never marks an item done."""
    parts = merged(whole("levain", band=[row]), whole("flow"))
    item = _item(1, done="np-1", done_kind="band")
    got, b = board_of(ev2(**parts), item)
    assert got["levain#1"].state == "unknown" and "producer levain unreadable (malformed row" in got["levain#1"].reason
    assert why in got["levain#1"].reason or why == "state"


def test_two_producers_that_resolve_one_row_differently_name_both_and_take_the_weaker_grade_in_either_order():
    a = {"id": "np-1", "state": "resolved", "items": [], "by": "phone", "grade": "signed-phone"}
    b = {"id": "np-1", "state": "resolved", "items": [], "by": "cli", "grade": "unsigned-terminal"}
    item = _item(1, done="np-1", done_kind="band")
    outs = []
    for first, second in ((a, b), (b, a)):
        parts = merged(whole("levain", band=[first]), whole("flow", band=[second]))
        got, _ = board_of(ev2(**parts), item)
        ev = bs.Evidence.from_dict(ev2(**parts), now=NOW, store_id="s1")
        assert ev.band_grade == {"np-1": "unsigned-terminal"}
        outs.append(got["levain#1"].reason)
    assert outs[0] == outs[1] == "band np-1 resolved by cli (unsigned-terminal); phone (signed-phone)"


def test_a_band_done_item_rests_on_the_band_absence():
    parts = merged(whole("levain", band=[{"id": "np-9", "state": "resolved", "items": [], "by": "phone", "grade": "signed-phone"}]),
                   whole("flow"))
    item = _item(1, done="np-9", done_kind="band")
    got, _ = board_of(ev2(**parts), item)
    assert got["levain#1"].state == "done"
    parts["band"] = [blk("levain", parts["band"][0]["rows"]), blk("flow", [], age=30)]
    got, _ = board_of(ev2(**parts), item)
    assert got["levain#1"].state == "unknown" and "band: producer flow unreadable (stale 30 min)" in got["levain#1"].reason


def test_flat_evidence_v1_is_one_block_from_flow_captured_at_fetched_at():
    flat = {"evidence_v": 1, "fetched_at": FETCHED, "unreadable": {}, "main": [], "chain": [], "refs": [], "merged": [],
            "branches": [], "documents": {}, "unattributed": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": []}
    got, _ = board_of(flat, _item(1))
    assert got["levain#1"].state == "ready"
    old = dict(flat, fetched_at=_stamp(20))
    got, _ = board_of(old, _item(1))
    assert got["levain#1"].state == "unknown" and "producer flow unreadable (stale 20 min)" in got["levain#1"].reason


def test_a_v2_block_with_no_clock_is_unreadable_not_assumed_fresh():
    folded = bs.fold([_item(1)])
    ev = bs.Evidence.from_dict(ev2(**merged(whole("levain"), whole("flow"))), now=None)
    assert "no clock" in ev.unreadable["seats"]
    assert bs.derive_all(folded, ev, now=NOW).items[0].state == "unknown"


# ------------------------------------------------------------------------------------------------ external files
def _write(dirpath: Path, name: str, body) -> None:
    (dirpath / f"evidence.{name}.json").write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def test_an_external_file_is_read_in_one_read_and_its_blocks_are_returned(tmp_path):
    _write(tmp_path, "flow", {"evidence_v": 2, "blocks": {"seats": blk("flow", []), "band": blk("flow", []), "waiters": blk("flow", [])}})
    blocks, failures = bp.read_external(tmp_path, ["flow"])
    assert failures == {} and all(len(blocks[s]) == 1 for s in ("seats", "band", "waiters"))


@pytest.mark.parametrize("body, why", [
    ("{torn", "cannot be read"),
    ('{"evidence_v": 1, "blocks": {}}', "not an evidence_v 2 file"),
    (json.dumps({"evidence_v": 2, "blocks": {"seats": {"producer": "x"}}}), "another producer"),
])
def test_an_unreadable_external_file_is_that_producers_failure(tmp_path, body, why):
    _write(tmp_path, "flow", body)
    _blocks, failures = bp.read_external(tmp_path, ["flow"])
    assert why in failures["flow"]


def test_a_missing_file_or_a_bad_name_is_a_failure_not_a_path(tmp_path):
    _blocks, failures = bp.read_external(tmp_path, ["flow", "../x"])
    assert "no evidence.flow.json" in failures["flow"] and "not a producer name" in failures["../x"]


def test_a_failed_external_producer_blocks_absence_and_names_why():
    got, _ = board_of(ev2(**whole("levain"), producer_failures={"flow": "no evidence.flow.json in the store"}), _item(1))
    assert "producer flow unreadable (no evidence.flow.json in the store)" in got["levain#1"].reason


# ------------------------------------------------------------------------------------------------ the native producer
def test_native_waiters_are_declared_absent_only_when_told_so():
    assert "waiters" not in bp.native_blocks(now=NOW, store_id="s1", consent_dir=Path("/nonexistent-consent"))
    got = bp.native_blocks(now=NOW, store_id="s1", no_land_waiter=True, consent_dir=Path("/nonexistent-consent"))
    assert got["waiters"]["complete"] is True and got["waiters"]["rows"] == []


def test_native_seats_with_no_desk_store_are_whole_and_empty(tmp_path):
    got = bp.native_blocks(now=NOW, store_id="s1", consent_dir=tmp_path)
    assert got["seats"]["complete"] is True and got["seats"]["rows"] == [] and got["seats"]["producer"] == "levain"
    assert got["seats"]["captured_at"] == "2026-11-12T20:00:00Z"


_NO = object()


class _Launch:
    def __init__(self, n, sid, items=(), harness="claude"):
        self.launch_id, self.harness, self.harness_session_id, self.at = f"ln-{n}", harness, sid, "2026-11-12T19:00:00Z"
        if items is not _NO:
            self.board_items = items


def _seats_world(monkeypatch, tmp_path, active, reads):
    """A desk store holding ``active`` and a claude whose successive ``agents --json`` reads are ``reads``."""
    from levain.desk import launch

    (tmp_path / "dispatch.db").write_bytes(b"")
    monkeypatch.setattr(bp, "_active_launches", lambda d: (tmp_path, [ln for ln in active if ln.harness == "claude"]))
    monkeypatch.setattr(launch, "_harness", lambda: ("claude", {}))
    seq = iter(reads)
    calls = []
    monkeypatch.setattr(launch, "_agents_checked", lambda exe, *, cwd, env: calls.append(1) or next(seq))
    return calls


def test_native_seats_are_whole_when_every_launch_is_listed_and_carries_its_items(monkeypatch, tmp_path):
    calls = _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", ["levain#1"]), _Launch(2, "b", ("levain#2",))],
                         [([{"sessionId": "a"}, {"sessionId": "b"}], 0)])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is True and len(calls) == 1
    assert [(r["name"], r["items"]) for r in got["rows"]] == [("ln-1", ["levain#1"]), ("ln-2", ["levain#2"])]


def test_no_active_claude_launch_is_a_whole_empty_block(monkeypatch, tmp_path):
    calls = _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", harness="codex")], [])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is True and got["rows"] == [] and calls == []


@pytest.mark.parametrize("items", [_NO, "levain#1", None, 7, ["levain#1", 3]])
def test_a_launch_without_a_board_items_list_makes_native_seats_incomplete_naming_it(monkeypatch, tmp_path, items):
    """F1: no launch record carries board_items until T7 writes it, so any launch lacking it leaves the block incomplete;
    the seat it can see still goes in."""
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", items)], [([{"sessionId": "a"}], 0)])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is False and "launch ln-1 carries no board_items (T7 writes it)" in got["why"]
    assert [r["name"] for r in got["rows"]] == ["ln-1"] and got["rows"][0]["items"] == []


def test_a_launch_with_no_session_yet_and_a_launch_the_list_does_not_show_make_it_incomplete(monkeypatch, tmp_path):
    _seats_world(monkeypatch, tmp_path, [_Launch(1, None, []), _Launch(2, "b", ["levain#2"]), _Launch(3, "c", ["levain#3"])],
                 [([{"sessionId": "b"}], 0)])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is False and "launch ln-1 has no session yet" in got["why"]
    assert "launch ln-3 not listed by claude agents" in got["why"] and "ln-2" not in got["why"]
    assert [r["name"] for r in got["rows"]] == ["ln-2"]


def test_an_unlisted_launch_is_not_re_read_after_a_sleep(monkeypatch, tmp_path):
    """F2: a read never blocks. One read; the launch the list misses is reported, not waited for."""
    import time

    calls = _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", [])], [([], 0), ([{"sessionId": "a"}], 0)])
    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("the board read slept"))
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is False and "launch ln-1 not listed by claude agents" in got["why"] and len(calls) == 1


def test_one_agents_row_that_is_not_an_object_makes_native_seats_incomplete(monkeypatch, tmp_path):
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", [])], [([{"sessionId": "a"}], 1)])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is False and "not objects" in got["why"]


def test_an_unreadable_agents_list_makes_native_seats_incomplete(monkeypatch, tmp_path):
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", [])], [None])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is False and "could not be read" in got["why"]


def test_native_seats_open_the_desk_store_read_only(tmp_path):
    """F22: the read opens the coordination store through its read-only door; it makes no table and no file."""
    from levain.desk.store import CoordStore

    CoordStore(tmp_path)._connect().close()                    # a desk store that exists, with no launch in it
    before = sorted(p.name for p in tmp_path.iterdir())
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is True and got["rows"] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == before or {p.name for p in tmp_path.iterdir()} - set(before) <= {"dispatch.db-wal", "dispatch.db-shm"}


def test_agents_checked_counts_the_rows_agents_drops(monkeypatch):
    from levain.desk import launch

    class CP:
        returncode, stdout = 0, '[{"sessionId": "a"}, 7, "x"]'
    monkeypatch.setattr(launch, "_run", lambda *a, **k: CP())
    assert launch._agents_checked("claude", cwd="/", env={}) == ([{"sessionId": "a"}], 2)
    assert launch._agents("claude", cwd="/", env={}) == [{"sessionId": "a"}]       # the stop and reap reader still drops them


def test_a_failed_or_pending_band_makes_native_band_incomplete(monkeypatch, tmp_path):
    from levain import band_view

    class R:
        failed, pending = True, False
    monkeypatch.setattr(band_view, "render_full", lambda *a, **k: R())
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["band"]
    assert got["complete"] is False and "authority source" in got["why"]
    R.failed, R.pending = False, True
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["band"]
    assert got["complete"] is False and "no number" in got["why"]


def test_native_band_rows_carry_open_and_resolved_with_the_grade(tmp_path):
    from levain import band_view
    from levain.consent.questions import QuestionStore

    store = QuestionStore(tmp_path)
    q1 = store.file(agent="claude_code", agent_session_id="s", what="decide levain#5 now", options=[("y", "yes"), ("n", "no")], rec="y", blocks="b", cwd=str(tmp_path))
    q2 = store.file(agent="claude_code", agent_session_id="s", what="another", options=[("y", "yes"), ("n", "no")], rec="y", blocks="b", cwd=str(tmp_path))
    store.answer(q2.id, option="y", channel="phone")
    now = datetime.now(timezone.utc)
    pend = bp.native_blocks(now=now, store_id="s1", consent_dir=tmp_path)["band"]
    assert pend["complete"] is False and "no number yet" in pend["why"]      # an open item nobody has shown is not yet whole
    band_view.render_full(store, now=now)                                     # showing it numbers it
    got = bp.native_blocks(now=now, store_id="s1", consent_dir=tmp_path)["band"]
    assert got["complete"] is True, got
    by_id = {r["id"]: r for r in got["rows"]}
    assert by_id[q1.id]["state"] == "open" and by_id[q1.id]["items"] == ["levain#5"]
    assert by_id[q2.id]["state"] == "resolved" and by_id[q2.id]["by"] == "phone" and by_id[q2.id]["grade"] in ("unknown", "unsigned-phone")


# ------------------------------------------------------------------------------------------------ the fix round (levain#91)
def _stale_flow_seats(*rows, **over):
    parts = merged(whole("levain"), whole("flow"))
    parts["seats"] = [blk("levain", list(rows)), blk("flow", [], age=41)]
    return ev2(**parts, **over)


def test_a_block_cannot_claim_the_flat_form_from_json():
    """F4: flat is a type only the CLI's own conversion builds, never a key a producer's JSON can set."""
    parts = merged(whole("levain"), whole("flow"))
    parts["seats"] = [blk("levain", []), blk("flow", [], store_id=None, form="flat")]       # would be readable if the flag were honoured
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "unknown" and "producer flow unreadable (block carries a form key" in got["levain#1"].reason
    flat = bs.FlatBlock(producer="flow", store_id=None, captured_at=_stamp(1), complete=True, rows=[])
    parts["seats"] = [blk("levain", []), flat]
    assert board_of(ev2(**parts), _item(1))[0]["levain#1"].state == "ready"


def test_an_external_file_with_a_form_key_or_a_non_object_block_fails_that_producer(tmp_path):
    for body in ({"seats": dict(blk("x", []), form="flat")}, {"seats": "nope"}, {"seats": None}):
        _write(tmp_path, "x", {"evidence_v": 2, "blocks": body})
        _blocks, failures = bp.read_external(tmp_path, ["x"])
        assert "x" in failures, body


def test_an_empty_reason_is_a_reason_and_never_a_reading_of_healthy():
    """F7: truthiness is never the test; an empty reason is replaced by one that says so."""
    got, _ = board_of(ev2(**whole("levain"), producer_failures={"flow": ""}), _item(1))
    assert "producer flow unreadable (no reason given)" in got["levain#1"].reason
    ev = bs.Evidence.from_dict(ev2(**merged(whole("levain"), whole("flow")), unreadable={"seats": ""}), now=NOW, store_id="s1")
    assert ev.unreadable["seats"] == "unreadable (no reason given)"
    got, _ = board_of(ev2(**merged(whole("levain"), whole("flow")), unreadable={"waiters": ""}), _item(1))
    assert got["levain#1"].state == "unknown" and "waiters: unreadable (no reason given)" in got["levain#1"].reason
    parts = merged(whole("levain"), whole("flow"))
    parts["band"] = [blk("levain", []), blk("flow", [], complete=False, why="")]
    got, _ = board_of(ev2(**parts), _item(1))
    assert got["levain#1"].state == "unknown" and "producer flow unreadable (incomplete)" in got["levain#1"].reason


def test_a_captured_at_more_than_a_minute_ahead_is_unreadable():
    for ahead, state in ((0.9, "ready"), (1.2, "unknown")):
        parts = merged(whole("levain"), whole("flow", age=-ahead))
        got, _ = board_of(ev2(**parts), _item(1))
        assert got["levain#1"].state == state, ahead
    assert "producer flow unreadable (captured in the future)" in got["levain#1"].reason


def test_an_unreadable_declaration_takes_only_the_absence_and_the_other_producers_rows_stand():
    """F15: flow declaring a source unreadable (or failing) discards no positive row another producer, or its own, supplied."""
    seat = {"name": "lane", "items": ["levain#1"], "since": FETCHED, "branch": None}
    waiter = {"branch": "queue/x", "items": ["levain#3"]}
    row = {"id": "np-aaaaaaaa", "state": "open", "items": ["levain#4"], "by": "", "grade": "unknown"}
    parts = merged(whole("levain", seats=[seat], band=[row], waiters=[waiter]), whole("flow"))
    got, _ = board_of(ev2(**parts, unreadable={"seats": "the roster could not be read", "band": "no band", "waiters": "down"}),
                      _item(1), _item(2), _item(3), _item(4))
    assert got["levain#4"].state == "needs_you"                                   # a row stands
    assert got["levain#1"].state == "unknown" and got["levain#3"].state == "unknown"    # the absences above them are not known
    assert got["levain#2"].state == "unknown"
    got, _ = board_of(ev2(**parts, producer_failures={"flow": "no file"}), _item(1), _item(4))
    assert got["levain#4"].state == "needs_you" and "producer flow unreadable (no file)" in got["levain#1"].reason


def test_an_orphan_branch_conflict_needs_the_seats_and_says_so_when_it_has_not_got_them():
    """F16: with the seats not all read, a branch no READ seat holds may be held; the conflict is "not checked", not made up."""
    chain = [{"ref": "queue/x", "tip": "c" * 40, "commits": [{"sha": "c" * 40, "items": ["levain#1"]}]}]
    branches = [{"name": "seat/own", "tip": "d" * 40, "date": "2026-11-12T12:00:00Z", "ahead": 2, "items": ["levain#1"]}]
    got, b = board_of(ev2(**merged(whole("levain"), whole("flow")), chain=chain, branches=branches), _item(1))
    assert got["levain#1"].state == "unknown" and got["levain#1"].reason.startswith("conflict: in chain queue/x vs orphan branch seat/own")
    got, b = board_of(_stale_flow_seats(chain=chain, branches=branches), _item(1))
    assert got["levain#1"].state == "landing"
    assert any(p.startswith("levain#1: not checked: seats producer flow unreadable (stale 41 min)") for p in b.problems)


def test_a_dangling_band_link_is_not_declared_when_the_band_was_not_all_read():
    link = [{"kind": "band", "ref": "np-zzzzzzzz"}]
    got, _ = board_of(ev2(**merged(whole("levain"), whole("flow"))), _item(1, depends_on=link))
    assert got["levain#1"].reason == "band:np-zzzzzzzz is not in the band"
    parts = merged(whole("levain"), whole("flow"))
    parts["band"] = [blk("levain", []), blk("flow", [], age=41)]
    got, b = board_of(ev2(**parts), _item(1, depends_on=link))
    assert got["levain#1"].state == "unknown" and "is not in the band" not in got["levain#1"].reason
    assert any(p.startswith("levain#1: not checked: band producer flow unreadable (stale 41 min)") for p in b.problems)


def test_a_closed_item_skips_the_live_seat_check_aloud_when_the_seats_were_not_all_read():
    """F17: the item is done (git closed it); the check against a live seat could not be run, and its problems line says so."""
    main = [{"sha": "a" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#1"], "closes": ["levain#1"]}]
    got, b = board_of(_stale_flow_seats(main=main), _item(1))
    assert got["levain#1"].state == "done"
    assert any(p.startswith("levain#1: not checked: seats") for p in b.problems)
    seat = {"name": "lane", "items": ["levain#1"], "since": FETCHED, "branch": None}
    got, _ = board_of(_stale_flow_seats(seat, main=main), _item(1))
    assert got["levain#1"].reason.startswith("conflict: closed by")           # a seat that WAS read still conflicts


@pytest.mark.parametrize("text, found", [
    ("decide levain#5 now", ["levain#5"]), ("see levain#5.", ["levain#5"]), ("app#2.txt", []), ("a/levain#5", []),
    ("xlevain#5y", []), ("levain#5x", []), ("(levain#5, flow#7)", ["levain#5", "flow#7"]), ("levain#5.2", []),
    ("https://x.y/z#12", [])])
def test_an_item_id_in_free_text_matches_only_as_a_whole_token(text, found):
    assert bp._items_in(text) == found


def _numbered_store(tmp_path, *whats):
    from levain import band_view
    from levain.consent.questions import QuestionStore

    store = QuestionStore(tmp_path)
    qs = [store.file(agent="claude_code", agent_session_id="s", what=w, options=[("y", "yes"), ("n", "no")], rec="y", blocks="b",
                     cwd=str(tmp_path)) for w in whats]
    now = datetime.now(timezone.utc)
    band_view.render_full(store, now=now)                       # showing them numbers them
    return store, qs, now


def _extra(ref, what, *, source="consent", unreadable=None, blocks=""):
    from levain.band_sources import Extra

    return Extra(source=source, ref=ref, filed="2026-11-12T10:00:00Z", tag="x", what=what, options=(), rec="", rec_letter=None,
                 blocks=blocks, origin="t", unreadable=unreadable)


def test_native_band_carries_the_open_authority_rows_with_their_items(monkeypatch, tmp_path):
    """F8: the consent, sign and extend rows the band shows are open rows; one whose shape cannot be read is incomplete."""
    from levain import band_sources

    _store, _qs, now = _numbered_store(tmp_path, "an ordinary question")
    rows = [_extra("rq-1", "ALLOW the land of levain#9"), _extra("rq-2", "SIGN it", source="sign", blocks="blocks levain#10")]
    monkeypatch.setattr(band_sources, "read_extras", lambda *a, **k: band_sources.Extras(items=list(rows)))
    got = bp.native_blocks(now=now, store_id="s1", consent_dir=tmp_path)["band"]
    assert got["complete"] is True, got
    by_id = {r["id"]: r for r in got["rows"]}
    assert by_id["consent:rq-1"]["items"] == ["levain#9"] and by_id["sign:rq-2"]["items"] == ["levain#10"]
    assert by_id["consent:rq-1"]["state"] == "open"
    rows.append(_extra("rq-3", "x", unreadable="a record that cannot be read"))
    got = bp.native_blocks(now=now, store_id="s1", consent_dir=tmp_path)["band"]
    assert got["complete"] is False and "authority row consent:rq-3 cannot be read" in got["why"]
    assert "consent:rq-1" in {r["id"] for r in got["rows"]}                  # the rows that were read still go in


def test_native_band_is_incomplete_when_the_store_holds_the_whole_cap(monkeypatch, tmp_path):
    """F11: a recent() that returns its whole limit may have older rows behind it."""
    _store, _qs, now = _numbered_store(tmp_path, "one", "two")
    monkeypatch.setattr(bp, "BAND_ROWS_MAX", 3)
    assert bp.native_blocks(now=now, store_id=None, consent_dir=tmp_path)["band"]["complete"] is True
    monkeypatch.setattr(bp, "BAND_ROWS_MAX", 2)
    got = bp.native_blocks(now=now, store_id=None, consent_dir=tmp_path)["band"]
    assert got["complete"] is False and "more than 2 band rows: older rows not read" in got["why"] and len(got["rows"]) == 2
    assert bp.BAND_ROWS_MAX != 1_000_000


def test_native_grades_come_from_the_answer_sidecar_when_the_answer_cannot_say(monkeypatch, tmp_path):
    """F12: a native answer carries no signing flag; the answer_context sidecar's sign_state says signed or not."""
    from levain import band_view

    store, qs, now = _numbered_store(tmp_path, "one", "two", "three")
    store.answer(qs[0].id, option="y", channel="phone")
    store.answer(qs[1].id, option="y", channel="cli")
    store.answer(qs[2].id, option="y", channel="cli")
    states = {qs[0].id: {"sign_state": "signed"}, qs[1].id: {"sign_state": "unsigned"}, qs[2].id: None}
    monkeypatch.setattr(band_view, "answer_sign_of", lambda st, qid: states[qid])
    got = {r["id"]: r["grade"] for r in bp.native_blocks(now=now, store_id=None, consent_dir=tmp_path)["band"]["rows"]}
    assert got == {qs[0].id: "signed-phone", qs[1].id: "unsigned-terminal", qs[2].id: "unknown"}


# ------------------------------------------------------------------------------------------------ the fix round 2 (levain#91)
def test_native_band_reads_the_authority_rows_even_when_a_source_failed(monkeypatch, tmp_path):
    """K1 (F8): a failed read still makes the block incomplete, but the authority rows that could be read go in as open rows."""
    from levain import band_sources, band_view

    _store, _qs, now = _numbered_store(tmp_path, "an ordinary question")

    class R:
        failed, pending = True, False
    monkeypatch.setattr(band_view, "render_full", lambda *a, **k: R())
    monkeypatch.setattr(band_sources, "read_extras", lambda *a, **k: band_sources.Extras(items=[_extra("rq-1", "ALLOW the land of levain#9")]))
    got = bp.native_blocks(now=now, store_id="s1", consent_dir=tmp_path)["band"]
    assert got["complete"] is False and "authority source" in got["why"]
    assert "consent:rq-1" in {r["id"] for r in got["rows"]}


def test_a_launch_that_is_done_is_skipped_before_the_board_items_check(monkeypatch, tmp_path):
    """K6: a finished launch holds nothing, so one with no board_items does not make the block incomplete."""
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", _NO), _Launch(2, "b", ["levain#2"])],
                 [([{"sessionId": "a", "state": "done"}, {"sessionId": "b"}], 0)])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is True, got
    assert [r["name"] for r in got["rows"]] == ["ln-2"]


def test_an_orphan_note_on_a_done_item_says_so_when_the_seats_were_not_all_read():
    """K7: F17's problems line, for the orphan-branch note of a closed item."""
    main = [{"sha": "a" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#1"], "closes": ["levain#1"]}]
    branches = [{"name": "seat/own", "tip": "d" * 40, "date": "2026-11-12T12:00:00Z", "ahead": 2, "items": ["levain#1"]}]
    got, b = board_of(_stale_flow_seats(main=main, branches=branches), _item(1))
    assert got["levain#1"].state == "done" and "orphan branch" not in got["levain#1"].reason
    assert any(p.startswith("levain#1: not checked: seats") for p in b.problems)


def test_a_declared_reason_is_stripped_and_a_blank_one_is_replaced():
    """K8."""
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z"}
    ev = bs.Evidence.from_dict({**base, "unreadable": {"seats": "  roster down \n", "band": " \t "}}, now=None)
    assert ev.unreadable["seats"] == "roster down" and ev.unreadable["band"] == "unreadable (no reason given)"


def test_evidence_from_dict_requires_now():
    """K4: no default, so a caller cannot forget the instant blocks are judged at."""
    with pytest.raises(TypeError):
        bs.Evidence.from_dict({"evidence_v": 1})


def test_a_whitespace_reason_or_why_is_replaced_like_an_empty_one():
    """M3: a block's why and a producer's failure reason are stripped before the truthiness test."""
    got, _ = board_of(ev2(**whole("levain"), producer_failures={"flow": "  \n"}), _item(1))
    reason = got["levain#1"].reason
    assert "producer flow unreadable (no reason given)" in reason and "(  " not in reason
    parts = merged(whole("levain"), whole("flow"))
    parts["band"] = [blk("levain", []), blk("flow", [], complete=False, why="   ")]
    got, _ = board_of(ev2(**parts), _item(1))
    assert "producer flow unreadable (incomplete)" in got["levain#1"].reason
    parts["band"] = [blk("levain", []), blk("flow", [], complete=False, why="  real why ")]
    got, _ = board_of(ev2(**parts), _item(1))
    assert "incomplete: real why)" in got["levain#1"].reason


def test_native_band_keeps_its_open_question_rows_when_the_authority_reread_raises(monkeypatch, tmp_path):
    """M1: a read_extras that raises costs the authority rows only; the question rows stand and the block is incomplete."""
    from levain import band_sources

    _store, qs, now = _numbered_store(tmp_path, "ALLOW the land of levain#9")

    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(band_sources, "read_extras", boom)
    got = bp.native_blocks(now=now, store_id="s1", consent_dir=tmp_path)["band"]
    assert got["complete"] is False and "the band could not read an authority source (OSError)" in got["why"]
    assert [r["id"] for r in got["rows"]] == [qs[0].id] and got["rows"][0]["items"] == ["levain#9"]
    got = {**got, "captured_at": _stamp(1.0)}                       # the test store's clock is not the board's NOW
    board, _ = board_of(ev2(seats=[blk("levain", [])], waiters=[blk("levain", [])], band=[got], producers=("levain",)), _item(9))
    assert board["levain#9"].state == "needs_you"
