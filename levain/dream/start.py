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
    (nothing was claimed or written) or the controller's own outcome for a launch that did not happen. ``text`` is what
    the operator is told."""
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
    if live:
        mine = [r for r in live if r.get("kind") == "manual" and r.get("state") == "admitted"]
        if len(live) == 1 and len(mine) == 1:
            # an earlier hand start was claimed and could not launch (a store busy, a cover that moved): this run IS
            # the retry; no second occurrence is claimed beside it
            oid, resumed = mine[0]["occurrence_id"], True
        else:
            r = live[0]
            return _refused(f"{r.get('occurrence_id')} ({r.get('kind')}, {r.get('state')}) is still in flight, and the "
                            "dream runs one occurrence at a time; nothing was claimed")
    else:
        key = f"hand-{_stamp(now)}-{secrets.token_hex(3)}"   # never a dedupe: two starts a second apart are two starts
        with store.write() as conn:
            oid, outcome = ctl.claim(conn, agent_id=agent_id, kind="manual", key=key, spec_hash=spec_hash,
                                     binding_id=binding, window_key=manual_window_key(now, inst.zone), now=now)
        if outcome != ctl.CLAIMED:
            return Started(1, outcome, f"the claim answered {outcome}: another occurrence of the dream is live or this "
                                       "second was already claimed; nothing was started", oid)
    # (2) the ceiling and (3) the admission, both inside the controller's launch
    launch_over, session_alive = ctl.dispatch_probes(inst.coord_dir)
    # the admission is the dream's own hook, PASSED: launch's defaulted on_input resolves it from the signed name, and a
    # default that ever came back None would admit with no day-ready at all, silently
    res = ctl.launch(store, oid, authority=auth, executor=executor, window=window_for(now, inst.zone), now=now,
                     launch_over=launch_over, session_alive=session_alive,
                     on_input=da.install_on_input(store.directory))
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
        return Started(1, res.outcome, f"{oid} did not launch ({res.outcome}: {res.reason}); it stays admitted, and "
                                       "running `levain dream start` again retries that same occurrence",
                       oid, resumed)
    return Started(1, res.outcome, f"{oid} ended {res.outcome}: {res.reason}; nothing was started", oid, resumed)
