"""K4a [6] — the BOUNDED SELF-CONSOLIDATE, end to end through a real anneal store.

The compose beat is monkeypatched (the live model is the L4 gate, not a unit test), but
`prepare_wrap` and `validated_save_continuity` run FOR REAL, so these exercise the actual
interaction between the bound, the crystallization refusal and anneal's wrap lifecycle.

Three properties, each FALSIFIED AGAINST A CONTROL rather than merely asserted — the discipline K3
and K4a ⑥ both used, and the one K3's glm HIGH caught a test skipping:

  1. an unattended wrap METABOLIZES but cannot CRYSTALLIZE (control: human-present CAN);
  2. an unattended wrap DISCARDS an orphaned prior wrap (control: human-present REFUSES);
  3. a bound that fires CANCELS the wrap rather than stranding it (control: a wrap that completes).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from anneal_memory import FLOW_SCHEMA, Store
from levain import wrap as wrapmod
from levain.firing.crystallization import CrystallizationRefused
from levain.firing.deadline import TurnTimeout
from levain.session import EXIT_TIMEOUT
from levain.wrap import wrap_entity

_VALID_NEOCORTEX = """\
## State
Consolidating unattended.

## Active Threads
- Running on a schedule — pointer: the seat.

## Patterns
Nothing graduated yet.

## Decisions
Nothing committed yet.

## Context
The seat metabolized its accumulated episodes with no human present.

## Understanding
Early days — the operator installed me and walked away.
"""


def _openhands_entity(tmp_path: Path, name: str = "ent") -> Path:
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True)
    (d / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    return d


def _with_store(entity: Path, *, episodes: int = 2) -> Path:
    db = entity / ".levain" / "memory.db"
    store = Store(str(db), section_schema=FLOW_SCHEMA)
    for i in range(episodes):
        store.record(
            content=f"Turn {i}: the seat ran its scheduled task and reported what it did.",
            episode_type="observation",
            source="seat-session",
        )
    store.close()
    return db


# ======================================================================================
# 1. METABOLIZE YES, CRYSTALLIZE NO — falsified against the human-present control
# ======================================================================================

def _save_that_tries_to_crystallize(real_save):
    """Wrap anneal's save so it ATTEMPTS a promotion, which the shipped path never does.

    The bound is currently true by OMISSION — Levain calls `parse_crystal_decisions` nowhere — so a
    test over the shipped path alone would pass identically with the refusal deleted, proving
    nothing. Simulating the call that a future commit might add is what makes this a test of the
    GUARD rather than of the omission (`wiring_checked_content_unchecked_passes_green`).
    """
    def _save(store, text, *, wrap_token=None, crystal_store=None, **kw):
        if crystal_store is not None:
            crystal_store.crystallize(name="a_pattern", explanation="promoted", level=3)
        return real_save(store, text, wrap_token=wrap_token, crystal_store=crystal_store, **kw)
    return _save


def test_unattended_wrap_is_refused_when_it_tries_to_crystallize(tmp_path, capsys, monkeypatch):
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    from anneal_memory import continuity as cont
    monkeypatch.setattr(
        cont, "validated_save_continuity",
        _save_that_tries_to_crystallize(cont.validated_save_continuity),
    )

    rc = wrap_entity(ent, unattended=True)

    assert rc == 1
    err = capsys.readouterr().err
    assert "crystallized tier" in err
    assert "MAY NEVER CRYSTALLIZE" in err
    # The identity was NOT written, and the wrap did not strand: the episodes come back next time.
    assert not (ent / ".levain" / "memory.continuity.md").exists()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_human_present_wrap_MAY_crystallize_the_control(tmp_path, monkeypatch):
    """THE CONTROL. Same entity, same compose, same attempted promotion — presence is the only
    variable. Without this the refusal test could pass because the promotion never happens at all,
    which is the failure mode where a guard is credited for an effect it did not cause."""
    ent = _openhands_entity(tmp_path)
    _with_store(ent)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    from anneal_memory import continuity as cont
    monkeypatch.setattr(
        cont, "validated_save_continuity",
        _save_that_tries_to_crystallize(cont.validated_save_continuity),
    )

    rc = wrap_entity(ent, unattended=False)

    assert rc == 0, "a human-present wrap must still be allowed to crystallize"
    assert (ent / ".levain" / "memory.continuity.md").exists()


def test_unattended_wrap_still_metabolizes_normally(tmp_path, monkeypatch):
    """The bound must not cost the FEATURE. If refusing crystallization also broke the ordinary
    consolidate, [6] would have shipped a seat that still never metabolizes — the exact thing it
    exists to fix, arriving as a side effect of its own safety mechanism."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=3)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, unattended=True) == 0
    continuity = ent / ".levain" / "memory.continuity.md"
    assert continuity.exists()
    assert "## Understanding" in continuity.read_text(encoding="utf-8")
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_unattended_wrap_leaves_the_crystal_store_byte_unchanged(tmp_path, monkeypatch):
    """The un-fakeable oracle: compare the bytes, not the code path."""
    ent = _openhands_entity(tmp_path)
    _with_store(ent)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    from levain.firing.isolation import entity_store_paths
    crystal_path, _ = entity_store_paths(ent.resolve())
    crystal_path.parent.mkdir(parents=True, exist_ok=True)
    crystal_path.write_text(json.dumps({"crystal": []}), encoding="utf-8")
    before = crystal_path.read_bytes()

    assert wrap_entity(ent, unattended=True) == 0
    assert crystal_path.read_bytes() == before


# ======================================================================================
# 2. THE ORPHANED WRAP — falsified against the human-present control
# ======================================================================================

def _strand_a_wrap(db: Path, *, age_seconds: float = 24 * 3600) -> None:
    """Leave a wrap in progress, exactly as a hard-exited consolidate would.

    Built from the store's REAL episode ids rather than invented ones: `wrap_started` records which
    episodes the orphaned wrap froze, and a wrap stranded over ids that do not exist would be a
    state anneal can never actually produce — a test proving the guard handles a situation that
    cannot arise.

    ``age_seconds`` matters because the self-heal requires the orphan to be provably DEAD, not merely
    present: an orphan is by construction older than the bound that killed its process, so a RECENT
    in-progress wrap is far more likely to be a live non-Levain writer. Default is a day old — an
    unambiguous corpse.
    """
    from datetime import datetime, timedelta, timezone

    with Store(str(db), section_schema=None) as store:
        ids = [str(e.id) for e in store.episodes_since_wrap()]
        store.wrap_started(token="orphan-token", episode_ids=ids)
        # Backdate it directly, because the age is what the guard reads and a test that could only
        # produce "now" would silently exercise the recent-wrap branch while claiming to test decay.
        when = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat().replace(
            "+00:00", "Z"
        )
        store._conn.execute(  # noqa: SLF001 — reaching in to age a timestamp is the point
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('wrap_started_at', ?)", (when,)
        )
        store._conn.commit()  # noqa: SLF001


def test_unattended_wrap_discards_an_orphan_and_proceeds(tmp_path, capsys, monkeypatch):
    """WITHOUT THIS, ONE BACKSTOP FIRING KILLS THE SEAT'S MEMORY PERMANENTLY.

    Layer 2 of the wall-clock bound is `os._exit`, which skips the cancel by design. So a hard-exited
    consolidate leaves a wrap in progress, and under the human-facing rule EVERY later consolidate
    would exit 2 asking a human to pass `--reset` — on a machine with no human. The seat would keep
    taking turns, keep capturing, and never metabolize again, behind a unit still reporting loaded.
    """
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, unattended=True) == 0
    out = capsys.readouterr().out
    assert "ORPHANED" in out
    assert "no human is present" in out
    assert (ent / ".levain" / "memory.continuity.md").exists()


def test_human_present_wrap_REFUSES_an_orphan_the_control(tmp_path, capsys, monkeypatch):
    """THE CONTROL: a human gets asked, because a human can answer."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, unattended=False) == 2
    assert "--reset" in capsys.readouterr().out
    assert not (ent / ".levain" / "memory.continuity.md").exists()


def test_the_self_heal_REFUSES_an_orphan_too_RECENT_to_be_provably_dead(tmp_path, capsys, monkeypatch):
    """THE OTHER HALF OF THE SAFETY ARGUMENT (codex L3, MED).

    `wrap.lock` is LEVAIN'S OWN lock — anneal's lifecycle never takes it — so holding it proves only
    that no other `levain wrap` is running. An `anneal-memory` CLI call, an MCP tool, or any library
    user can be mid-`prepare_wrap` on this same store right now, and discarding there destroys their
    LIVE work rather than an orphan.

    Age settles it, structurally rather than heuristically: an orphan exists only because its process
    DIED, and what kills it is the bound — so an orphan is necessarily OLDER than that bound, while a
    live writer's wrap is necessarily younger than the bound it is still running under.
    """
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db, age_seconds=5)  # five seconds old — someone is very likely still working
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, unattended=True, max_seconds=900) == 2
    out = capsys.readouterr().out
    assert "too RECENT to be provably dead" in out
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is not None, "discarded a wrap that may have been live"


def test_the_self_heal_REFUSES_when_no_lock_could_be_taken(tmp_path, capsys, monkeypatch):
    """THE SELF-HEAL'S SAFETY ARGUMENT IS CONDITIONAL, so the code must check it rather than assert it.

    "An in-progress wrap must be an orphan" holds only BECAUSE we hold the exclusive flock — a live
    peer would have been turned away at `_ANOTHER_WRAP_RUNNING`. But `_lock_wrap` returns `None`
    where `fcntl` is unavailable (Windows) and the wrap proceeds unlocked as a best effort. On that
    path the argument is false: a peer really could be mid-wrap, and auto-discarding would cancel
    its live work — the loser-cancels-winner race `_cancel_if_ours` exists to prevent, reintroduced
    by the very convenience that fixes the unattended case.

    Costing an unattended seat one skipped consolidate is recoverable; destroying a live wrap is not.
    """
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)
    # No lock obtainable — exactly what a platform without fcntl produces.
    monkeypatch.setattr(wrapmod, "_lock_wrap", lambda entity_dir: None)

    assert wrap_entity(ent, unattended=True) == 2, "auto-discarded an orphan without holding the lock"
    out = capsys.readouterr().out
    assert "no wrap lock could be taken" in out
    # The orphan is still there — we declined to touch what we could not prove was ours.
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is not None


def test_the_self_heal_DOES_apply_when_the_lock_is_held_the_control(tmp_path, monkeypatch):
    """Control for the above: same orphan, same unattended flag, lock available → it self-heals.
    Without this pair the refusal test could pass because the self-heal never works at all."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, unattended=True) == 0
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_self_healing_is_reported_not_silent(tmp_path, capsys, monkeypatch):
    """A seat that self-heals every run is a seat whose consolidates keep dying. The log is the
    only place that pattern is visible, so the message must say what a repeat MEANS."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    wrap_entity(ent, unattended=True)
    out = capsys.readouterr().out
    assert "If this recurs every run" in out


# ======================================================================================
# 3. THE BOUND — a timeout CANCELS rather than strands
# ======================================================================================

def test_timeout_during_compose_cancels_the_wrap_and_exits_5(tmp_path, capsys, monkeypatch):
    """The out-of-band exit must leave the store CLEAN.

    `TurnTimeout` is a BaseException, so none of `_consolidate`'s `except Exception` clauses see it.
    Without the explicit BaseException handler it would unwind to the `finally`, closing the store
    and releasing the lock while leaving the wrap IN PROGRESS — handing the next run a mess instead
    of not making one.
    """
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)

    def _stall(*a, **k):
        raise TurnTimeout(30.0)

    monkeypatch.setattr(wrapmod, "_compose", _stall)

    assert wrap_entity(ent, max_seconds=30) == EXIT_TIMEOUT
    err = capsys.readouterr().err
    assert "CONSOLIDATE BOUND EXCEEDED" in err
    assert "episodes are safe" in err

    # THE ASSERTION THAT MATTERS: no orphan left behind, so a plain re-run works.
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_a_timeout_BEFORE_the_token_exists_still_cancels(tmp_path, monkeypatch):
    """THE NARROW WINDOW `_cancel_our_wrap` exists for, and the reason it is not just
    `_cancel_if_ours`.

    anneal marks a wrap started inside `prepare_wrap`, so an out-of-band exit can land after the
    store says "in progress" but before a token has been returned to us. Deferring to the
    token-MATCHING guard there would compare `None` against a real token, decline to cancel, and
    strand the wrap — the exact outcome the handler is trying to prevent. We hold the exclusive
    lock, so cancelling whatever is in progress is provably cancelling our own work.
    """
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)

    from anneal_memory import continuity as cont
    real_prepare = cont.prepare_wrap

    def _prepare_then_stall(store, **kw):
        real_prepare(store, **kw)          # the store now records a wrap in progress …
        raise TurnTimeout(30.0)            # … and we never receive its token

    monkeypatch.setattr(cont, "prepare_wrap", _prepare_then_stall)

    assert wrap_entity(ent, max_seconds=30) == EXIT_TIMEOUT
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None, "a tokenless timeout stranded the wrap"


def test_keyboard_interrupt_also_cancels_rather_than_stranding(tmp_path, monkeypatch):
    """The same handler covers Ctrl-C, which previously stranded the wrap. Propagated, not
    swallowed — an interrupt must still interrupt."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)

    def _interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(wrapmod, "_compose", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        wrap_entity(ent)
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_a_completed_wrap_is_the_control_for_the_cancel_path(tmp_path, monkeypatch):
    """Control for the two above: the same code path with nothing raising must COMPLETE, so
    "no wrap in progress" is not just what an untouched store looks like."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, max_seconds=600) == 0
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None
    assert (ent / ".levain" / "memory.continuity.md").exists()


def test_the_wrap_timeout_report_does_not_claim_an_orphaned_shell(tmp_path):
    """The hard report is wrap-specific for a reason: the turn's wording warns about an orphaned
    confined bash, and a consolidate spawns none. Asserting a residual that cannot exist would be
    the module lying about its own blast radius in the line an operator reads."""
    hard = wrapmod.format_wrap_timeout_report(30.0, hard=True)
    assert "bash" not in hard.lower()
    assert "WRAP IS LEFT IN PROGRESS" in hard
    # And it must name the recovery for BOTH audiences, since only one of them can type --reset.
    assert "UNATTENDED seat clears it automatically" in hard
    assert "--reset" in hard


def test_a_crystallization_refusal_is_reported_as_a_defect_not_an_operator_error(tmp_path, capsys, monkeypatch):
    ent = _openhands_entity(tmp_path)
    _with_store(ent)

    def _refuse(*a, **k):
        raise CrystallizationRefused("crystallize")

    monkeypatch.setattr(wrapmod, "_compose", _refuse)
    assert wrap_entity(ent, unattended=True) == 1
    assert "Please report this" in capsys.readouterr().err


def test_an_UNPARSEABLE_orphan_timestamp_refuses_rather_than_discards(tmp_path, capsys, monkeypatch):
    """The staleness gate must fail SAFE when it cannot decide, and nothing covered that until the
    mutation harness flipped the `except` branch to `return True` and the suite stayed green.

    The asymmetry decides the direction: refusing costs one delayed consolidate (the next run tries
    again); discarding on a timestamp we could not even read risks destroying a live writer's wrap.
    """
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db, age_seconds=24 * 3600)
    with Store(str(db), section_schema=None) as store:
        store._conn.execute(  # noqa: SLF001
            "INSERT OR REPLACE INTO metadata (key, value) VALUES "
            "('wrap_started_at', 'not-a-timestamp')"
        )
        store._conn.commit()  # noqa: SLF001
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, unattended=True, max_seconds=900) == 2
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is not None, "discarded on an undecidable timestamp"


def test_the_staleness_horizon_is_derived_from_the_bound_not_a_magic_number():
    """An orphan is older than the bound that killed its process, so the horizon must MOVE with the
    bound. A fixed number would be wrong in both directions: too eager for a long-bounded seat, too
    slow for a short one."""
    from levain.wrap import _ORPHAN_MARGIN_SECONDS, _orphan_is_stale
    from levain.firing.deadline import HARD_EXIT_GRACE_SECONDS
    from datetime import datetime, timedelta, timezone

    def at(age: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")

    bound = 100.0
    horizon = bound + HARD_EXIT_GRACE_SECONDS + _ORPHAN_MARGIN_SECONDS
    assert _orphan_is_stale(at(horizon + 5), bound) is True
    assert _orphan_is_stale(at(horizon - 5), bound) is False
    # A LONGER bound must push the horizon out — same age, different verdict.
    assert _orphan_is_stale(at(horizon + 5), bound * 10) is False


def test_an_unbounded_consolidate_still_gets_a_finite_staleness_horizon():
    """With no bound there is no bound-derived horizon, and "never discard" would strand an
    unbounded seat permanently the first time one died. A conservative fixed fallback instead."""
    from levain.wrap import _ORPHAN_FALLBACK_SECONDS, _orphan_is_stale
    from datetime import datetime, timedelta, timezone

    def at(age: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")

    assert _orphan_is_stale(at(_ORPHAN_FALLBACK_SECONDS + 60), None) is True
    assert _orphan_is_stale(at(_ORPHAN_FALLBACK_SECONDS - 60), None) is False


def test_no_timestamp_at_all_is_never_stale():
    from levain.wrap import _orphan_is_stale

    assert _orphan_is_stale(None, 900.0) is False
    assert _orphan_is_stale("", 900.0) is False
