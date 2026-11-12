"""levain.dream.start (D1-M): ``levain dream start``, the one verb that starts a dream by hand.

It is not a second way in. A hand start goes through the SAME three gates a scheduled window meets, in the same
order, and every one of them is the controller's own code:

* **the link.** The agent must be armed: :func:`levain.agents.tick_driver.binding_of_factory`, the function the tick
  arms a window with, finds the highest-born LIVE signed link of the dream's current spec on the authority log. No
  live link, nothing is claimed (the tick claims nothing for an unarmed agent either). The launch then judges the link
  again (:func:`levain.agents.controller.launch`: the agent registered and active, its spec unmoved, no open bar, the
  link live and its signer still enrolled).
* **the ceiling.** The launch reserves under the link's SIGNED ``run_ceiling`` and budgets (``accounting.reserve``),
  counting the day's manual runs and the scheduled runs of the window that holds now (A0 §2.6: one signed limit covers
  both kinds). On top of it the day-ready check 1 stops at :data:`levain.dream.ready.RUN_CEILING` launched runs.
* **the admission.** The occurrence is ``kind = 'manual'``, so the dream's own input hook
  (:func:`levain.dream.agent.install_on_input`, via ``controller.launch``'s default hook) runs
  :func:`levain.dream.ready.evaluate` with ``manual=True``: check 6 (the window's one attempt) is waived, because a
  hand start is the catch-up for exactly that failure, and every other check, a wrap open and a baton held elsewhere
  included, refuses as it does at night.

What this module adds is only the glue the tick does not have: it CLAIMS a manual occurrence, LAUNCHES it
(``controller.launch``) and SPAWNS the fire (``controller.spawn_fire``), and from there the fire is the scheduled
fire (``controller.fire`` -> ``run_agent`` -> the dream's run body). The audit trail is therefore the one a
scheduled run writes: the occurrence row (kind ``manual``, its window key ``m:<local date>``), the launch columns, the
reservation, the run in the journal, the fire's identity record, ``result.json`` / ``usage.json`` / ``progress.json``
in the occurrence directory, the snapshot, and the card on the desk.

A hand start that claims a row and does not launch it ends that row (``stopped``, reason ``hand_start_*``): an
``admitted`` row blocks every scheduled slot behind it, and nothing launches a manual row on a tick. A launch that did
happen is never ended here. An admitted manual row left by a start of an earlier local day is not resumed (it would
launch under that day's window key and escape today's count): it is ended the same way and a fresh one is claimed.

Nothing here reads or writes the anneal store; the fire does, as it does at night.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Started:
    """``code`` 0 only when the occurrence launched and its fire was spawned. ``outcome`` is ``launched``, ``refused``
    (nothing was claimed or written), ``error`` (the start broke between its claim and its launch's answer) or the
    controller's own outcome for a launch that did not happen. ``text`` is what the operator is told."""
    code: int
    outcome: str
    text: str
    occurrence_id: str | None = None
    resumed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "outcome": self.outcome, "occurrence_id": self.occurrence_id, "resumed": self.resumed,
                "text": self.text}


def _refused(text: str) -> Started:
    return Started(1, "refused", text)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_STAMP)


def window_for(now: datetime, zone: str):
    """The :class:`levain.agents.controller.Window` a MANUAL launch at ``now`` counts over: today in the install's
    zone (local midnight to the next, as UTC text; ``Limits.span_start`` is inclusive, ``span_end`` exclusive) and the
    scheduled window holding now, or None outside 22:00-05:00 (A0 §2.6)."""
    from levain.agents.controller import Window
    from levain.dream import ready

    z = ZoneInfo(zone)
    local = now.astimezone(z)
    start = datetime(local.year, local.month, local.day, tzinfo=z)
    end = datetime(*(local + timedelta(days=1)).timetuple()[:3], tzinfo=z)
    return Window(_stamp(start), _stamp(end), ready.window_holding(now, zone))


def manual_window_key(now: datetime, zone: str) -> str:
    """The window key a hand start files under, ``m:<local date>``: the form check 1 and the accounting count manual runs by."""
    return f"m:{now.astimezone(ZoneInfo(zone)).date().isoformat()}"


def _why_no_input(inst: Any, store: Any, oid: str, now: datetime) -> str:
    """The day-ready verdict, read again (read-only) after the controller ended the row ``skipped-no-input``: the row
    keeps only ``no_input``, the verdict's own words are not stored. A shadow install judges flow's day instead of the
    six checks, so no verdict is read for it."""
    from levain.dream import agent as da
    from levain.dream import ready

    if inst.shadow_flow_root is not None:
        return "a shadow install judges flow's committed day, not the six checks; `levain dream shadow` reads it"
    row = next((r for r in ready._read_occurrences(Path(store.directory)) if r.get("occurrence_id") == oid), None)
    if row is None:
        return "the verdict could not be read again"
    v = da.day_ready(inst.store_path, row, now=now, zone=inst.zone, agent_dir=store.directory, coord_dir=inst.coord_dir)
    return f"{v.state}/{v.kind}: {v.reason} (read again just now, after the row ended)"


REASON_UNCLASSIFIED = "hand_start_unclassified"   # the input check could not answer (or the authority could not be read)
REASON_NO_SLOT = "hand_start_no_slot"              # every execution slot was held
REASON_INTERRUPTED = "hand_start_interrupted"      # the start broke or was interrupted between its claim and its launch
_ROW_COLS = ("agent_id", "kind", "key", "state", "epoch", "window_key", "spec_hash", "binding_id", "admitted_at")


def _row_of(store: Any, oid: str) -> dict[str, Any] | None:
    with store.read() as conn:
        row = conn.execute("SELECT " + ", ".join(_ROW_COLS) + " FROM agent_occurrences WHERE occurrence_id = ?",
                           (oid,)).fetchone()
    return None if row is None else dict(zip(_ROW_COLS, row), occurrence_id=oid)


def _end_unlaunched(store: Any, oid: str, reason: str, clock: Callable[[], datetime]) -> str:
    """End the ``admitted`` row this verb claimed (or resumed) when it did not launch, and return the row's state read
    afterwards: ``stopped`` when this ended it, the row's own state when it was no longer admitted (a launch that DID
    happen is never ended: the write is guarded by ``state = 'admitted'`` and the epoch just read), ``admitted`` when
    the end itself could not be written, ``unknown`` when the row cannot be read. It never raises.

    The end is the controller's own for an admitted row that ends without launching
    (:func:`levain.agents.controller._end_after_input`, the one write the input check's ends use), with the state
    ``stopped`` (the state a launch-time refusal of an admitted row uses) and a reason that says what happened. Not
    ``missed``: that state also feeds the miss streak, whose attention row reads "the laptop slept through them", which
    a failed hand start is not."""
    from levain.agents import controller as ctl

    state = "unknown"
    try:
        for _ in range(2):                    # a second look when the epoch moved under the first
            occ = _row_of(store, oid)
            if occ is None:
                return "unknown"
            state = occ["state"]
            if state != "admitted":
                return state
            ctl._end_after_input(store, occ, ctl.STOPPED, reason, clock())
        occ = _row_of(store, oid)
        return "unknown" if occ is None else occ["state"]
    except Exception:  # noqa: BLE001 - this runs on the way out of a failure; the row's state is reported as it stands
        try:
            occ = _row_of(store, oid)
            return state if occ is None else occ["state"]
        except Exception:  # noqa: BLE001
            return state


def _after_end(oid: str, left: str, reason: str, why: str) -> str:
    """What the operator is told about a hand start that did not launch, by what became of its row."""
    if left == "stopped":
        return (f"{oid} did not launch ({why}). It was ended stopped ({reason}), so it holds nothing and no scheduled "
                "slot is blocked behind it; `levain dream start` starts a fresh occurrence when the cause is gone")
    if left == "admitted":
        return (f"{oid} did not launch ({why}) and could not be ended: it is still admitted, and until it launches or "
                "is ended every scheduled slot of the dream is skipped behind it, tonight's included. `levain dream "
                "start` retries that same occurrence")
    if left == "launched":
        return (f"{oid} did not return normally ({why}), but its launch had already happened: the row is launched and "
                "is not ended here. `levain agents show dream` says whether its fire is running")
    return (f"{oid} did not launch ({why}); its row is now {left}, not admitted, so it blocks no scheduled slot. "
            "`levain agents show dream` says more")


def start(store: Any, *, clock: Callable[[], datetime] | None = None, authority: Any = None,
          spawn: Callable[..., Any] | None = None) -> Started:
    """Start the dream now, by hand, through the link, the ceiling and the controller (see the module docstring).

    ``store`` is an opened agent store. ``clock``, ``authority`` and ``spawn`` are seams for tests; production passes
    none (the wall clock, an :class:`~levain.agent_authority.AgentAuthority` over the install's cockpit root, and
    :func:`levain.agents.controller.spawn_fire`)."""
    from levain.agent_authority import AgentAuthority
    from levain.agents import controller as ctl
    from levain.agents import spec as _spec
    from levain.agents import tick_driver as td
    from levain.dream import agent as da
    from levain.dream import executor as ex
    from levain.dream import ready

    clock = clock or (lambda: datetime.now(timezone.utc))
    spawn = spawn or ctl.spawn_fire
    try:
        agent = _spec.get_agent(store, da.NAME)
    except _spec.SpecError as exc:
        return _refused(f"there is no registered dream to start ({exc}); `levain dream enable` registers it")
    if agent["operation"] != "active":
        return _refused(f"the dream is {agent['operation']}, so nothing starts it; `levain agents resume dream` lifts a "
                        "pause once the cause is fixed")
    try:
        inst = da.read_install(store.directory)
    except (ValueError, OSError, RuntimeError) as exc:
        return _refused(f"the dream's install facts cannot be read ({exc}); `levain dream enable --store-dir <dir>` "
                        "writes them")
    auth = authority or AgentAuthority(inst.cockpit_root)
    now = clock()
    if now.tzinfo is None:
        raise ValueError("the clock must be timezone-aware")
    agent_id, spec_hash = agent["agent_id"], agent["spec_hash"]
    # (1) the link: the tick's own arming. Unarmed, nothing is claimed.
    binding = td.binding_of_factory(store, auth, clock)(agent_id, spec_hash)
    if binding is None:
        return _refused("no live signed link covers the dream's current spec (or the authority log could not be "
                        "replayed), so nothing was claimed; the dream launches only under a live signed link")
    try:
        executor = ex.DreamExecutor(agent_store_dir=store.directory, occurrence_id="oc-" + "0" * 32,
                                    occ_dir=inst.store_path.parent / "dream", route="claude", model=inst.model,
                                    facts_path=inst.facts_path)
    except ex.ExposureRefused as exc:
        return _refused(f"the dream cannot state its exposure, so nothing was claimed ({exc})")
    live = [r for r in ready._read_occurrences(Path(store.directory))
            if r.get("agent_id") == agent_id and r.get("state") in ("admitted", "launched")]
    resumed = False
    oid: str | None = None
    today = manual_window_key(now, inst.zone)
    if live:
        mine = [r for r in live if r.get("kind") == "manual" and r.get("state") == "admitted"]
        if len(live) == 1 and len(mine) == 1 and mine[0].get("window_key") == today:
            # an earlier hand start of TODAY was claimed and could not launch (a store busy, a cover that moved): this
            # run IS the retry; no second occurrence is claimed beside it
            oid, resumed = mine[0]["occurrence_id"], True
        elif len(live) == 1 and len(mine) == 1:
            # one of an earlier local day (a start killed outright after its claim): resumed, it would launch under that
            # day's window key and escape today's manual count, so it is ended and a fresh occurrence is claimed
            stale = mine[0]["occurrence_id"]
            left = _end_unlaunched(store, stale, REASON_INTERRUPTED, clock)
            if left in ("admitted", "launched"):
                return _refused(f"{stale} is a hand start of an earlier day ({mine[0].get('window_key')}) that never "
                                f"launched, and it could not be ended (it is {left}); nothing was claimed. "
                                "`levain agents show dream` says more")
        else:
            r = live[0]
            return _refused(f"{r.get('occurrence_id')} ({r.get('kind')}, {r.get('state')}) is still in flight, and the "
                            "dream runs one occurrence at a time; nothing was claimed")
    if oid is None:
        key = f"hand-{_stamp(now)}-{secrets.token_hex(3)}"   # never a dedupe: two starts a second apart are two starts
        with store.write() as conn:
            oid, outcome = ctl.claim(conn, agent_id=agent_id, kind="manual", key=key, spec_hash=spec_hash,
                                     binding_id=binding, window_key=today, now=now)
        if outcome != ctl.CLAIMED:
            return Started(1, outcome, f"the claim answered {outcome}: another occurrence of the dream is live or this "
                                       "second was already claimed; nothing was started", oid)
    # (2) the ceiling and (3) the admission, both inside the controller's launch. From here to the launch's answer this
    # verb owns an `admitted` row, and an `admitted` row blocks every scheduled slot behind it (`claim` files them
    # skipped-overlap), so every way out of this stretch that did not launch ends the row (_end_unlaunched).
    try:
        launch_over, session_alive = ctl.dispatch_probes(inst.coord_dir)
        # the admission is the dream's own hook, PASSED: launch's defaulted on_input resolves it from the signed name, and
        # a default that ever came back None would admit with no day-ready at all, silently. Its clock is this start's
        # one instant, so the day it judges is the day the claim, the limits and the launch are filed under.
        res = ctl.launch(store, oid, authority=auth, executor=executor, window=window_for(now, inst.zone), now=now,
                         launch_over=launch_over, session_alive=session_alive,
                         on_input=da.install_on_input(store.directory, clock=lambda: now))
    except Exception as exc:  # noqa: BLE001 - whatever broke, the row this verb owns is not left blocking tonight
        left = _end_unlaunched(store, oid, REASON_INTERRUPTED, clock)
        return Started(1, "error", _after_end(oid, left, REASON_INTERRUPTED, f"{type(exc).__name__}: {exc}"), oid, resumed)
    except BaseException:   # an interrupt: end the row, then let it go on up
        _end_unlaunched(store, oid, REASON_INTERRUPTED, clock)
        raise
    verb = "resumed" if resumed else "claimed"
    if res.outcome == ctl.LAUNCHED:
        try:
            spawn(store.directory, oid)
        except Exception as exc:  # noqa: BLE001 - a launched row with no fire is reported, and the reaper owns it
            return Started(1, "launched", f"{oid} launched but its fire could not be started ({type(exc).__name__}: "
                                          f"{exc}); the row is launched with nothing running, and lapses to takeover "
                                          f"when its {ctl.LEASE_S}s lease runs out", oid, resumed)
        occ_dir = inst.store_path.parent / "dream" / oid
        return Started(0, "launched", f"{verb} and launched {oid}; its fire runs detached. Its progress and result are "
                                      f"under {occ_dir} (progress.json, result.json), and its card is filed on the desk "
                                      "when it ends", oid, resumed)
    if res.outcome == ctl.SKIPPED_NO_INPUT:
        return Started(1, res.outcome, f"{oid} ended skipped-no-input: nothing was started. "
                                       f"{_why_no_input(inst, store, oid, now)}", oid, resumed)
    if res.outcome == ctl.NOT_ADMITTED:
        # the row is by definition not admitted: another start launched it, or something ended it. It is not this
        # start's to end or to retry.
        return Started(1, res.outcome, f"{oid} was no longer admitted when this start reached it ({res.reason}): "
                                       "another start launched or ended it, so this one started nothing. "
                                       "`levain agents show dream` says which", oid, resumed)
    if res.outcome in (ctl.NO_SLOT, ctl.UNCLASSIFIED):
        # the controller wrote nothing and left the row admitted for ITS next tick; nothing launches a manual row on a
        # tick, so this verb ends it
        token = REASON_NO_SLOT if res.outcome == ctl.NO_SLOT else REASON_UNCLASSIFIED
        left = _end_unlaunched(store, oid, token, clock)
        return Started(1, res.outcome, _after_end(oid, left, token, f"{res.outcome}: {res.reason}"), oid, resumed)
    return Started(1, res.outcome, f"{oid} ended {res.outcome}: {res.reason}; nothing was started", oid, resumed)
