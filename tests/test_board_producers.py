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
    parts = merged(whole("levain", seats=[seat]), whole("flow", age=41))
    got, _ = board_of(ev2(**parts), _item(1), _item(2))
    assert got["levain#1"].state == "in_flight"          # the claim stands
    assert got["levain#2"].state == "unknown"            # the absence does not
    assert "producer flow unreadable (stale 41 min)" in got["levain#2"].reason


def test_an_open_band_row_and_a_waiter_are_positive_claims_too():
    row = {"id": "np-aaaaaaaa", "state": "open", "items": ["levain#1"], "by": "", "grade": "unknown"}
    waiter = {"branch": "queue/x", "items": ["levain#2"]}
    parts = merged(whole("levain", band=[row]), whole("flow", age=41, waiters=[waiter]))
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


def test_two_blocks_for_one_producer_are_unreadable_not_merged():
    parts = whole("levain")
    parts["seats"] = parts["seats"] * 2
    got, _ = board_of(ev2(**parts, producers=("levain",)), _item(1))
    assert "producer levain unreadable (two blocks for one producer)" in got["levain#1"].reason


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


def test_a_resolved_band_row_carries_its_grade_and_a_row_without_one_reads_unknown():
    rows = [{"id": "np-1", "state": "resolved", "items": [], "by": "phone", "grade": "signed-phone"},
            {"id": "np-2", "state": "resolved", "items": [], "by": "cli"}]
    ev = bs.Evidence.from_dict(ev2(**merged(whole("levain", band=rows), whole("flow"))), now=NOW, store_id="s1")
    assert ev.band_resolved == {"np-1": "phone", "np-2": "cli"} and ev.band_grade == {"np-1": "signed-phone", "np-2": "unknown"}


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
    ev = bs.Evidence.from_dict(ev2(**merged(whole("levain"), whole("flow"))))
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


class _Launch:
    def __init__(self, n, sid, items=()):
        self.launch_id, self.harness, self.harness_session_id, self.at = f"ln-{n}", "claude", sid, "2026-11-12T19:00:00Z"
        self.board_items = items


def _seats_world(monkeypatch, tmp_path, active, reads):
    """A desk store holding ``active`` and a claude whose successive ``agents --json`` reads are ``reads``."""
    from levain.desk import launch, store as store_mod

    (tmp_path / "dispatch.db").write_bytes(b"")
    monkeypatch.setattr(store_mod, "CoordStore", lambda d: type("S", (), {"launches_active": lambda self: active, "directory": tmp_path})())
    monkeypatch.setattr(launch, "_harness", lambda: ("claude", {}))
    seq = iter(reads)
    monkeypatch.setattr(launch, "_agents_checked", lambda exe, *, cwd, env: next(seq))
    return launch


def test_native_seats_emit_only_listed_launches_after_the_recheck(monkeypatch, tmp_path):
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a", ["levain#1"]), _Launch(2, "b", ["levain#2"])],
                 [([{"sessionId": "a"}], 0), ([{"sessionId": "a"}], 0)])
    slept = []
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path, sleep=slept.append)
    assert got["seats"]["complete"] is True and slept
    assert [r["name"] for r in got["seats"]["rows"]] == ["ln-1"] and got["seats"]["rows"][0]["items"] == ["levain#1"]


def test_a_launch_missed_by_one_read_and_seen_by_the_second_stays_a_seat(monkeypatch, tmp_path):
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a")], [([], 0), ([{"sessionId": "a"}], 0)])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path, sleep=lambda s: None)
    assert [r["name"] for r in got["seats"]["rows"]] == ["ln-1"]


def test_one_agents_row_that_is_not_an_object_makes_native_seats_incomplete(monkeypatch, tmp_path):
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a")], [([{"sessionId": "a"}], 1)])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path, sleep=lambda s: None)["seats"]
    assert got["complete"] is False and got["rows"] == [] and "not objects" in got["why"]


def test_an_unreadable_agents_list_makes_native_seats_incomplete(monkeypatch, tmp_path):
    _seats_world(monkeypatch, tmp_path, [_Launch(1, "a")], [None])
    got = bp.native_blocks(now=NOW, store_id=None, consent_dir=tmp_path)["seats"]
    assert got["complete"] is False and "could not be read" in got["why"]


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
