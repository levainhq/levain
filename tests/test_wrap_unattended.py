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

def _strand_a_wrap(db: Path, *, age_seconds: float = 24 * 3600, bound: bool = False) -> None:
    """Leave a wrap in progress, exactly as a hard-exited consolidate would.

    Built from the store's REAL episode ids rather than invented ones: `wrap_started` records which
    episodes the orphaned wrap froze, and a wrap stranded over ids that do not exist would be a
    state anneal can never actually produce — a test proving the guard handles a situation that
    cannot arise.

    ``age_seconds`` matters because the self-heal requires the orphan to be provably DEAD, not merely
    present: an orphan is by construction older than the bound that killed its process, so a RECENT
    in-progress wrap is far more likely to be a live non-Levain writer. Default is a day old — an
    unambiguous corpse. ``bound=True`` leaves the wrap token-bound, as a consolidate that handed
    anneal its own token (``prepare_wrap(wrap_token=...)``) leaves it.
    """
    from datetime import datetime, timedelta, timezone

    with Store(str(db), section_schema=None) as store:
        ids = [str(e.id) for e in store.episodes_since_wrap()]
        store.wrap_started(token="orphan-token", episode_ids=ids, token_bound=bound)
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


def test_a_timeout_inside_prepare_cancels_by_the_token_levain_minted(tmp_path, monkeypatch):
    """THE NARROW WINDOW the token is minted early for.

    anneal marks a wrap started inside `prepare_wrap`, so an out-of-band exit can land after the
    store says "in progress" but before `prepare_wrap` has returned. Levain hands anneal its own
    token (`wrap_token=`) before the call, so the exit handler holds the token that names the open
    wrap and cancels by it. The wrap.lock proves nothing about a non-Levain client (an
    `anneal-memory` CLI or MCP call never takes it), so a tokenless cancel here could clear that
    client's wrap; the compare-and-swap cannot.
    """
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)

    from anneal_memory import continuity as cont
    real_prepare = cont.prepare_wrap
    given: list[str] = []

    def _prepare_then_stall(store, **kw):
        given.append(kw.get("wrap_token"))
        real_prepare(store, **kw)          # the store now records a wrap in progress …
        raise TurnTimeout(30.0)            # … and prepare_wrap never returns

    cancels: list[dict] = []
    real_cancel = Store.wrap_cancelled

    def _spy(self, **kw):
        cancels.append(kw)
        return real_cancel(self, **kw)

    monkeypatch.setattr(cont, "prepare_wrap", _prepare_then_stall)
    monkeypatch.setattr(Store, "wrap_cancelled", _spy)

    assert wrap_entity(ent, max_seconds=30) == EXIT_TIMEOUT
    assert len(given) == 1 and isinstance(given[0], str) and len(given[0]) == 32
    assert cancels and all(c.get("expect_token") == given[0] for c in cancels), cancels
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None, "a timeout inside prepare stranded the wrap"


def test_a_wall_clock_stop_inside_the_store_error_handler_still_cancels_by_token(tmp_path, monkeypatch, capsys):
    """codex MED (0.5.7 rounds): a TurnTimeout raised while the `except AnnealMemoryError` handler was
    cancelling escaped the sibling `except BaseException` clause (a sibling does not catch what a
    handler raises), so the wrap stayed open. The first cancel here is the one that gets cut short."""
    from anneal_memory import AnnealMemoryError

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)

    real_read = Store.section_schema_for_wrap

    def failing_read_from_levain(self):
        import sys
        if sys._getframe(1).f_globals.get("__name__") == "levain.wrap":
            raise AnnealMemoryError("simulated schema read failure")
        return real_read(self)

    cancels: list[dict] = []
    real_cancel = Store.wrap_cancelled

    def _stop_the_first_cancel(self, **kw):
        cancels.append(kw)
        if len(cancels) == 1:
            raise TurnTimeout(30.0)
        return real_cancel(self, **kw)

    monkeypatch.setattr(Store, "section_schema_for_wrap", failing_read_from_levain)
    monkeypatch.setattr(Store, "wrap_cancelled", _stop_the_first_cancel)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    assert wrap_entity(ent, max_seconds=30) == EXIT_TIMEOUT
    assert len(cancels) == 2 and all(c.get("expect_token") for c in cancels)
    assert cancels[0]["expect_token"] == cancels[1]["expect_token"]
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


@pytest.mark.parametrize("where", ["during the age read", "between the observation and the cancel"])
def test_a_peers_fresh_wrap_that_replaces_the_orphan_mid_discard_is_never_cancelled(tmp_path, capsys, monkeypatch, where):
    """L2 (0.5.7 round), RAN: the age proof was computed on orphan A, and the token that got cancelled
    was read later, so a peer that cleared A and opened its own wrap B in that gap had B cancelled by
    the self-heal. The token is now read BEFORE the age, and the cancel names it."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db, bound=True)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    def peer_replaces_the_wrap():
        with Store(str(db), section_schema=None) as peer:
            peer.wrap_cancelled(force=True)
            peer.wrap_started(token="b" * 32, episode_ids=[str(e.id) for e in peer.episodes_since_wrap()], token_bound=True)

    if where == "during the age read":
        real_started = Store.get_wrap_started_at
        fired: list[int] = []

        def started_then_swap(self):
            value = real_started(self)
            if not fired:
                fired.append(1)
                peer_replaces_the_wrap()
            return value

        monkeypatch.setattr(Store, "get_wrap_started_at", started_then_swap)
    else:
        real_discard = wrapmod._discard_prior_wrap

        def swap_then_discard(store, token):
            peer_replaces_the_wrap()
            return real_discard(store, token)

        monkeypatch.setattr(wrapmod, "_discard_prior_wrap", swap_then_discard)

    assert wrap_entity(ent, unattended=True) == 2
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.load_wrap_snapshot()["token"] == "b" * 32, "the peer's wrap was cancelled"


@pytest.mark.parametrize("kind", ["unattended self-heal", "operator --reset"])
def test_a_prior_wrap_that_goes_idle_before_the_discard_is_already_discarded(tmp_path, monkeypatch, kind):
    """codex (0.5.7 r1): every WrapOwnershipError during the discard was reported as "replaced by
    another consolidate" and exited 2. If the wrap simply finished or was cancelled in between, the
    store is idle and the consolidate should go on."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db, bound=True)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)
    real_discard = wrapmod._discard_prior_wrap

    def idle_then_discard(store, token):
        with Store(str(db), section_schema=None) as peer:
            peer.wrap_cancelled(force=True)
        return real_discard(store, token)

    monkeypatch.setattr(wrapmod, "_discard_prior_wrap", idle_then_discard)
    rc = wrap_entity(ent, unattended=True) if kind == "unattended self-heal" else wrap_entity(ent, reset=True)
    assert rc == 0
    assert (ent / ".levain" / "memory.continuity.md").exists()


def test_reset_with_unreadable_wrap_metadata_changes_nothing_and_names_the_entitys_store(tmp_path, capsys, monkeypatch):
    """r2/r3 (0.5.7): forcing the cancel when no token can be read would also clear any wrap that
    replaced the unreadable one, so `--reset` there cancels only a PARTIAL state and otherwise
    refuses. The refusal must aim the operator at THIS entity's store: a bare `anneal-memory
    wrap-status` reads ~/.anneal-memory (codex r3 HIGH)."""
    import sys
    from anneal_memory import StoreError

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db, bound=True)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)
    real = Store.load_wrap_snapshot

    def unreadable_for_levain(self):
        if sys._getframe(1).f_globals.get("__name__") == "levain.wrap":
            raise StoreError("simulated unreadable wrap metadata")
        return real(self)

    monkeypatch.setattr(Store, "load_wrap_snapshot", unreadable_for_levain)
    assert wrap_entity(ent, reset=True) == 2
    out = capsys.readouterr().out
    from levain.manifest import anneal_invocation
    assert anneal_invocation("--db", str(db), "wrap-status") in out
    assert anneal_invocation("--db", str(db), "wrap-cancel", "--partial") in out
    assert "--wrap-token" in out and "~/.anneal-memory" in out
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.load_wrap_snapshot()["token"] == "orphan-token", "the wrap was cleared"


@pytest.mark.parametrize("kind", ["unattended self-heal", "operator --reset"])
def test_a_token_bound_orphan_from_an_earlier_levain_wrap_is_still_discarded(tmp_path, capsys, monkeypatch, kind):
    """A wrap Levain opens carries a caller token, and anneal refuses a TOKENLESS cancel of a
    token-bound wrap. The orphan-discard and `--reset` paths used to call a bare cancel, which would
    now raise and leave every later consolidate refusing: the stranded-seat failure the self-heal
    exists to prevent. Both the self-heal and `--reset` cancel by the token they observed."""
    ent = _openhands_entity(tmp_path)
    db = _with_store(ent)
    _strand_a_wrap(db, bound=True)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)

    if kind == "unattended self-heal":
        assert wrap_entity(ent, unattended=True) == 0
    else:
        assert wrap_entity(ent, reset=True) == 0
    assert (ent / ".levain" / "memory.continuity.md").exists()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


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


# ======================================================================================
# The partnership check at the point of use (0.5.6): a set-schema between levain's check and
# the save cannot change the schema the wrap saves under. RUN against the real store.
# ======================================================================================
def test_a_schema_change_before_the_wrap_starts_cancels_it(tmp_path, monkeypatch, capsys):
    """codex, the 0.5.6 fix-diff round: another process sets the ops schema after levain's early
    check and before prepare_wrap. The check after prepare_wrap sees it, cancels by token, saves
    nothing."""
    import anneal_memory.continuity as cont
    from anneal_memory import DEFAULT_SCHEMA

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    real_prepare = cont.prepare_wrap

    def prepare_after_a_concurrent_set_schema(store, **kw):
        with Store(str(db), section_schema=None) as other:
            other.set_section_schema(DEFAULT_SCHEMA)
        return real_prepare(store, **kw)

    monkeypatch.setattr(cont, "prepare_wrap", prepare_after_a_concurrent_set_schema)
    composed = []
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: composed.append(k) or _VALID_NEOCORTEX)

    assert wrap_entity(ent) == 2
    assert "stopped; nothing was saved" in capsys.readouterr().out
    assert composed == []
    assert not (ent / ".levain" / "memory.continuity.md").exists()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_a_schema_change_after_the_wrap_starts_is_refused_and_the_save_stays_partnership(
        tmp_path, monkeypatch):
    """Once prepare_wrap has started the wrap, anneal refuses a schema change, so the compose
    prompt and the save both see the partnership schema."""
    from anneal_memory import DEFAULT_SCHEMA

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    refused, seen = [], {}

    def compose_while_another_process_tries_set_schema(*a, **k):
        seen.update(k)
        with Store(str(db), section_schema=None) as other:
            try:
                other.set_section_schema(DEFAULT_SCHEMA)
            except ValueError:
                refused.append(True)
        return _VALID_NEOCORTEX

    monkeypatch.setattr(wrapmod, "_compose", compose_while_another_process_tries_set_schema)
    assert wrap_entity(ent) == 0
    assert refused == [True]
    assert "## Understanding" in seen["instructions"]
    with Store(str(db), section_schema=None) as store:
        assert [s["heading"] for s in store.section_schema] == [s["heading"] for s in FLOW_SCHEMA]
    assert "## Understanding" in (ent / ".levain" / "memory.continuity.md").read_text()


def test_a_store_error_after_the_wrap_starts_cancels_it(tmp_path, monkeypatch, capsys):
    """codex, the 0.5.6 hunk look: a store error after prepare_wrap returned a token fell into a
    handler that assumed nothing was written, and left the wrap in progress."""
    from anneal_memory import AnnealMemoryError

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)

    import sys

    real_read = Store.section_schema_for_wrap

    def failing_read_from_levain(self):
        # anneal's prepare_wrap reads the schema too; fail only levain's read AFTER it, so the
        # wrap is really open when the error lands.
        if sys._getframe(1).f_globals.get("__name__") == "levain.wrap":
            assert self.get_wrap_started_at() is not None, "precondition: the wrap is open"
            raise AnnealMemoryError("simulated schema read failure")
        return real_read(self)

    monkeypatch.setattr(Store, "section_schema_for_wrap", failing_read_from_levain)
    monkeypatch.setattr(wrapmod, "_compose", lambda *a, **k: _VALID_NEOCORTEX)
    assert wrap_entity(ent) == 2
    assert "could not read" in capsys.readouterr().out
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_cancel_if_ours_leaves_another_tokens_wrap_alone(tmp_path):
    """The token compare and the clear are one anneal call (expect_token)."""
    from anneal_memory.continuity import prepare_wrap

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    with Store(str(db), section_schema=None) as store:
        result = prepare_wrap(store)
        assert result.get("status") == "ready"
        wrapmod._cancel_if_ours(store, "not-our-token")
        assert store.get_wrap_started_at() is not None
        wrapmod._cancel_if_ours(store, result["wrap_token"])
        assert store.get_wrap_started_at() is None


def test_a_non_ready_prepare_cancels_nothing(tmp_path, monkeypatch, capsys):
    """codex + complement, the 0.5.7 round: a non-ready prepare_wrap started no wrap of ours, so the
    bare cancel that followed could only clear a wrap another process opened meanwhile."""
    import anneal_memory.continuity as cont

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    real_prepare = cont.prepare_wrap
    peer = {}

    def a_peer_wraps_then_ours_downgrades(store, **kw):
        with Store(str(db), section_schema=None) as other:
            peer["token"] = real_prepare(other)["wrap_token"]
        return {"status": "downgraded", "wrap_token": None, "message": "simulated"}

    monkeypatch.setattr(cont, "prepare_wrap", a_peer_wraps_then_ours_downgrades)
    assert wrap_entity(ent) == 1
    assert "anneal declined to start a wrap" in capsys.readouterr().out
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.load_wrap_snapshot()["token"] == peer["token"]


def test_cancel_if_ours_without_a_token_clears_nothing(tmp_path):
    """complement, the 0.5.7 round: expect_token=None is anneal's bare cancel."""
    from anneal_memory.continuity import prepare_wrap

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    with Store(str(db), section_schema=None) as store:
        assert prepare_wrap(store).get("status") == "ready"
        assert wrapmod._cancel_if_ours(store, None) is False
        assert store.get_wrap_started_at() is not None


def test_cancel_if_ours_on_a_partial_state_is_unresolved():
    """codex, the 0.5.7 fix-diff round: a partial lifecycle state may be our damaged wrap."""
    from anneal_memory import WrapOwnershipError

    class _Store:
        def wrap_cancelled(self, **kw):
            raise WrapOwnershipError(expected="t", actual=None, partial_state=True)

    class _Idle:
        def wrap_cancelled(self, **kw):
            raise WrapOwnershipError(expected="t", actual=None, partial_state=False)

    assert wrapmod._cancel_if_ours(_Store(), "t") is False
    assert wrapmod._cancel_if_ours(_Idle(), "t") is True


def test_an_unknown_status_with_a_token_cancels_that_wrap(tmp_path, monkeypatch, capsys):
    """codex, the 0.5.7 fix-diff round: a status this levain does not know may still open a wrap."""
    import anneal_memory.continuity as cont

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)
    real_prepare = cont.prepare_wrap

    def opens_then_reports_a_new_status(store, **kw):
        r = dict(real_prepare(store, **kw))
        r["status"] = "some-future-status"
        return r

    monkeypatch.setattr(cont, "prepare_wrap", opens_then_reports_a_new_status)
    assert wrap_entity(ent) == 1
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is None


def test_a_dry_run_whose_cancel_fails_is_not_a_success(tmp_path, monkeypatch, capsys):
    """codex, the 0.5.7 round: a failed cancel was swallowed and the dry run reported success
    while its wrap stayed open."""
    from anneal_memory import StoreError

    ent = _openhands_entity(tmp_path)
    db = _with_store(ent, episodes=2)

    def locked(self, **kw):
        raise StoreError("simulated: database is locked")

    monkeypatch.setattr(Store, "wrap_cancelled", locked)
    assert wrap_entity(ent, dry_run=True) == 1
    assert "could NOT be cancelled" in capsys.readouterr().out
    monkeypatch.undo()
    with Store(str(db), section_schema=None) as store:
        assert store.get_wrap_started_at() is not None
