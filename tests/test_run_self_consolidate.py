"""K4a [6] — the SECOND BOUNDED PHASE in `levain run --task`, and the due-condition it reads.

Two things under test:
  - `consolidate_readiness` — the SINGLE definition of "a wrap is due", shared with `wrap_nudge`,
    and its tri-state (readable-and-due / readable-not-due / UNREADABLE).
  - `_self_consolidate` — when the phase runs, when it declines, and the rule that it can never
    rewrite the turn's exit code.
"""
from __future__ import annotations

import json
from pathlib import Path

from anneal_memory import FLOW_SCHEMA, Store
from levain import run as runmod
from levain.firing.anneal import (
    DEFAULT_WRAP_NUDGE_THRESHOLD,
    ConsolidateReadiness,
    consolidate_readiness,
    wrap_nudge,
)
from levain.session import EXIT_GATED, EXIT_NO_REPLY, EXIT_OK, EXIT_TIMEOUT, EXIT_USAGE


def _entity_with_episodes(tmp_path: Path, n: int) -> tuple[Path, Path]:
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    db = ent / ".levain" / "memory.db"
    store = Store(str(db), section_schema=FLOW_SCHEMA)
    for i in range(n):
        store.record(content=f"Turn {i}: did the scheduled work.", episode_type="observation",
                     source="seat")
    store.close()
    return ent, db


# ======================================================================================
# the due-condition
# ======================================================================================

def test_readiness_is_due_at_the_threshold(tmp_path):
    _, db = _entity_with_episodes(tmp_path, 5)
    r = consolidate_readiness(db, 5)
    assert (r.readable, r.due, r.episodes) == (True, True, 5)


def test_readiness_is_not_due_below_the_threshold(tmp_path):
    _, db = _entity_with_episodes(tmp_path, 4)
    r = consolidate_readiness(db, 5)
    assert (r.readable, r.due) == (True, False)


def test_a_missing_store_is_readable_and_empty_not_unreadable(tmp_path):
    """A brand-new entity has no store file yet. Reporting that as UNREADABLE would cry wolf on
    every seat's first interval, which is how a real warning gets trained into noise."""
    r = consolidate_readiness(tmp_path / "nope" / "memory.db", 5)
    assert (r.readable, r.due, r.episodes) == (True, False, 0)


def test_a_corrupt_store_is_UNREADABLE_and_that_is_distinct_from_empty(tmp_path):
    """THE TRI-STATE'S WHOLE POINT. An int-returning predicate would report 0 here, which is
    indistinguishable from 'nothing to do' — and for a SEAT, 'nothing to do' every interval forever
    is exactly the silent death this keystone closes."""
    bad = tmp_path / "memory.db"
    bad.write_bytes(b"this is not a sqlite database")
    r = consolidate_readiness(bad, 5)
    assert r.readable is False
    assert r.due is False
    assert r.episodes is None


def test_readiness_never_raises_on_a_directory_in_place_of_a_store(tmp_path):
    d = tmp_path / "memory.db"
    d.mkdir()
    assert consolidate_readiness(d, 5).readable is False


def test_nudge_and_seat_cannot_disagree_about_when_a_wrap_is_due(tmp_path):
    """THE NO-DRIFT PIN. The human-facing nudge and the seat's self-consolidate must answer the same
    question the same way — an operator told 'a wrap is due' by one surface while the other declines
    to run one has no way to reconcile the two."""
    _, db = _entity_with_episodes(tmp_path, 7)
    for threshold in (1, 6, 7, 8, 50):
        due = consolidate_readiness(db, threshold).due
        nudged = wrap_nudge(db, threshold) is not None
        assert due == nudged, f"disagreement at threshold {threshold}"


def test_default_threshold_is_the_shared_constant(tmp_path):
    _, db = _entity_with_episodes(tmp_path, 0)
    assert consolidate_readiness(db, None).threshold == DEFAULT_WRAP_NUDGE_THRESHOLD


def test_unreadable_is_never_due_even_at_threshold_zero():
    assert ConsolidateReadiness(episodes=None, threshold=0).due is False


# ======================================================================================
# the phase: when it runs and when it declines
# ======================================================================================

def _spy_wrap(calls: list, rc: int = 0):
    def _w(path, **kw):
        calls.append(kw)
        return rc
    return _w


def test_phase_is_skipped_when_the_turn_never_started(tmp_path, capsys, monkeypatch):
    ent, _ = _entity_with_episodes(tmp_path, 50)
    calls: list = []
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap(calls))
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_USAGE, model="m", base_url="b", api_key=None,
        unattended=True, every=1, max_seconds=60,
    )
    assert calls == []
    assert "SKIPPED" in capsys.readouterr().err


def test_phase_is_skipped_when_the_turn_timed_out(tmp_path, capsys, monkeypatch):
    """A stalled endpoint would very likely stall the compose too, and the seat's process would then
    occupy its label for turn-bound PLUS consolidate-bound against a dead socket — one sick endpoint
    costing two cadences instead of one."""
    ent, _ = _entity_with_episodes(tmp_path, 50)
    calls: list = []
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap(calls))
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_TIMEOUT, model="m", base_url="b", api_key=None,
        unattended=True, every=1, max_seconds=60,
    )
    assert calls == []
    assert "endpoint stalled" in capsys.readouterr().err


def test_phase_STILL_RUNS_after_a_gated_halt(tmp_path, monkeypatch):
    """Deliberate, not an omission. A gated halt captures no episode, but EARLIER turns did, and
    their metabolizing does not depend on a human having reviewed the held action yet. Declining
    here would let recall degrade for exactly as long as the human takes to look."""
    ent, _ = _entity_with_episodes(tmp_path, 50)
    calls: list = []
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap(calls))
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_GATED, model="m", base_url="b", api_key=None,
        unattended=True, every=1, max_seconds=60,
    )
    assert len(calls) == 1


def test_phase_runs_after_an_ordinary_reply_and_after_no_reply(tmp_path, monkeypatch):
    for code in (EXIT_OK, EXIT_NO_REPLY):
        ent, _ = _entity_with_episodes(tmp_path / f"c{code}", 50)
        calls: list = []
        monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap(calls))
        runmod._self_consolidate(
            ent, turn_exit_code=code, model="m", base_url="b", api_key=None,
            unattended=True, every=1, max_seconds=60,
        )
        assert len(calls) == 1, f"should consolidate after exit {code}"


def test_phase_declines_quietly_when_not_due(tmp_path, capsys, monkeypatch):
    ent, _ = _entity_with_episodes(tmp_path, 2)
    calls: list = []
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap(calls))
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_OK, model="m", base_url="b", api_key=None,
        unattended=True, every=40, max_seconds=60,
    )
    assert calls == []
    err = capsys.readouterr().err
    assert "not due" in err and "2/40" in err


def test_an_unreadable_store_warns_rather_than_reading_as_nothing_to_do(tmp_path, capsys, monkeypatch):
    ent, db = _entity_with_episodes(tmp_path, 5)
    db.write_bytes(b"corrupt")
    calls: list = []
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap(calls))
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_OK, model="m", base_url="b", api_key=None,
        unattended=True, every=1, max_seconds=60,
    )
    assert calls == []
    err = capsys.readouterr().err
    assert "⚠" in err
    assert "not the same as it being empty" in err


def test_the_bound_and_the_drive_mode_are_passed_through_to_the_wrap(tmp_path, monkeypatch):
    ent, _ = _entity_with_episodes(tmp_path, 50)
    calls: list = []
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap(calls))
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_OK, model="the-model", base_url="http://x", api_key="k",
        unattended=True, every=1, max_seconds=123,
    )
    assert calls[0]["max_seconds"] == 123
    assert calls[0]["unattended"] is True
    # The entity composes with its OWN model, not a hardcoded default.
    assert calls[0]["composer"] == "the-model"


def test_a_failing_consolidate_reports_the_compounding_cost(tmp_path, capsys, monkeypatch):
    ent, _ = _entity_with_episodes(tmp_path, 50)
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap([], rc=1))
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_OK, model="m", base_url="b", api_key=None,
        unattended=True, every=1, max_seconds=60,
    )
    err = capsys.readouterr().err
    assert "did not complete" in err
    # A single failure is survivable; a RUN of them is the seat ceasing to metabolize. Say so.
    assert "Recall degrades" in err


def test_a_consolidate_that_explodes_cannot_break_a_finished_turn(tmp_path, capsys, monkeypatch):
    """The turn is already done and captured. A maintenance phase raising must not convert a
    completed turn into a traceback exit — that misreports the one thing the ladder exists to
    report. Caught at BaseException because the interesting failures here are not Exceptions."""
    ent, _ = _entity_with_episodes(tmp_path, 50)

    def _boom(path, **kw):
        raise BaseException("out of band")  # noqa: TRY002

    monkeypatch.setattr("levain.wrap.wrap_entity", _boom)
    runmod._self_consolidate(
        ent, turn_exit_code=EXIT_OK, model="m", base_url="b", api_key=None,
        unattended=True, every=1, max_seconds=60,
    )
    assert "FAILED unexpectedly" in capsys.readouterr().err


def test_the_phase_returns_none_so_it_cannot_feed_an_exit_code(tmp_path, monkeypatch):
    """STRUCTURAL, not stylistic: returning None makes 'the consolidate rewrote the turn's exit
    code' unrepresentable at the call site rather than merely discouraged there."""
    ent, _ = _entity_with_episodes(tmp_path, 50)
    monkeypatch.setattr("levain.wrap.wrap_entity", _spy_wrap([], rc=1))
    out = runmod._self_consolidate(
        ent, turn_exit_code=EXIT_OK, model="m", base_url="b", api_key=None,
        unattended=True, every=1, max_seconds=60,
    )
    assert out is None
