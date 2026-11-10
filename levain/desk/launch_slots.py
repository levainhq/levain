"""levain.desk.launch_slots: the desk's side of the shared slot table (BUILD_PLAN P6-3b; A0 §2.4).

A desk launch takes one slot of ``agent_slots`` (``owner_kind='launch'``, ``owner_id`` the launch id) just
before it claims its spawn, so named fires and launches share one cap (:mod:`levain.agents.slots`; this module adds
nothing to the agent-side semantics). The slot is a lease. The launch's own process releases it when the launch does
not start; the watcher (:func:`sweep`, once per pass) renews it while the launch is not over and releases it once the
launch is over, since the launch and the slot live in two files and no one transaction can write both.

The slot is taken and released in the agent store's own transactions. What this module adds is the wiring:

* **The store.** The agent store of an install is ``<levain home>/autonomic``, the desk's is
  the desk home (``<levain home>/desk`` for a new install, an old install's old-named directory). Only the install's own desk directory is slotted (:func:`agent_dir`): a
  desk store anywhere else (a test's, a copy's) has no agent store beside it and launches unslotted, so it never
  writes the operator's real ``autonomic.db``.
* **The fallback.** The agent schema is checked per process per store (:func:`slot_state`, a read that writes and
  creates nothing; a refusal or the no-schema answer is kept, any other fault is asked again). No agent schema, or a ``/3`` store, launches unslotted and silent. Any other reason launches
  unslotted with one attention row keyed ``agent-schema-refused``. The agent schema never refuses a launch.
* **No slot free.** :class:`SlotFull`: the launch stays pending and the thread stays queued, as for every other
  refusal that leaves a launch pending.
* **A crash between take and release.** The slot expires; the next :func:`take` reclaims it through
  :func:`levain.agents.slots.probe_over` over the desk's own ``dispatch.db`` (the probes the agent path uses,
  :func:`levain.agents.controller.dispatch_probes`), and :func:`sweep` releases it once the launch is over.

An unreadable slot table or a garbled cap is a refusal that leaves the launch pending (:class:`SlotFault`): the cap
fails closed, as on the agent side. Only the agent SCHEMA fails open. A garbled cap has no repair surface (no CLI
writes ``max_slots``): it is repaired in the store by hand.

Only a desk launch takes a slot here. A spawned worker is capped by ``levain.desk.lease.MAX_SPAWNS_LIVE``
and takes none; "slot" in this module is always the agent slot of ``agent_slots``.
"""
from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from levain.agents import slots

__all__ = ["ATTENTION_KEY", "Hold", "SlotFull", "SlotFault", "agent_dir", "slot_state", "reset", "take", "release",
           "sweep"]

ATTENTION_KEY = "agent-schema-refused"
_UNSLOTTED = "unslotted"
_SLOTS = "slots"

# per process, per agent store directory: (mode, reason). A refusal and the no-schema answer are kept (A0 §2.4);
# a transient fault is not (slot_state)
_STATE: dict[str, tuple[str, str | None]] = {}
_RAISED: set[str] = set()


class SlotFull(Exception):
    """The cap is reached; the launch stays pending."""


class SlotFault(Exception):
    """The slot table or the cap could not be read or written; the launch stays pending (fail closed for the cap)."""


@dataclass(frozen=True)
class Hold:
    """A slot this call took (``fresh``) or found already held under the launch's id (not fresh: another pass's,
    which this call never releases)."""
    launch_id: str
    slot: int
    fresh: bool
    directory: Path


def reset() -> None:
    """Forget the per-process schema answers and raised attention keys (tests)."""
    _STATE.clear()
    _RAISED.clear()


def _stderr_attention(key: str, text: str) -> None:
    print(f"attention [{key}]: {text}", file=sys.stderr)


def agent_dir(desk_dir: Path | str) -> Path | None:
    """The agent store directory beside ``desk_dir``, or ``None`` when that is not the install's own
    desk directory (see the module docstring)."""
    from levain.autonomic.db import default_store_dir
    from levain.desk.launch import default_desk_dir

    try:
        if Path(desk_dir).resolve() != default_desk_dir().resolve():
            return None
    except OSError:
        return None
    return default_store_dir()


def slot_state(directory: Path, attention: Callable[[str, str], None] | None = None) -> bool:
    """True when launches take slots on the agent store at ``directory``; False when they launch unslotted. A refusal
    (``AgentStoreRefused``) and the no-schema / ``/3`` answer are KEPT for the process, as A0 §2.4 says. Any other
    fault (a locked database, an ``OSError``) says nothing about the schema: that call launches unslotted, the answer
    is NOT kept, and the next call checks again.

    The attention row is a line on stderr, ``attention [agent-schema-refused]: ...``, raised at most once per process
    per directory (the agent store has no attention table and no desk-side writer exists; the shape is
    ``levain.agents.cli._raise_attention``'s). A transient fault is therefore heard once, not on every launch it
    touches."""
    from levain.agents.store import STORE_V3_REASON, AgentStoreRefused, open_agent_store_view

    key = str(directory)
    answer = _STATE.get(key)
    if answer is None:
        try:
            view = open_agent_store_view(directory)
        except AgentStoreRefused as exc:
            answer = _STATE[key] = (_UNSLOTTED, None if str(exc) == STORE_V3_REASON else str(exc))
        except Exception as exc:  # noqa: BLE001 - nothing about the agent store may refuse a desk launch
            answer = (_UNSLOTTED, f"{type(exc).__name__}: {exc}"[:300])        # this call only: not kept
        else:
            answer = _STATE[key] = (_UNSLOTTED, None) if view.path is None else (_SLOTS, None)
    mode, reason = answer
    if reason is not None and key not in _RAISED:
        _RAISED.add(key)
        try:
            (attention or _stderr_attention)(ATTENTION_KEY, f"launches run unslotted: {reason}")
        except Exception:  # noqa: BLE001 - the attention row is a courtesy
            pass
    return mode == _SLOTS


def _open(directory: Path):
    from levain.agents.store import AgentStoreRefused, open_agent_store

    try:
        return open_agent_store(directory)
    except AgentStoreRefused:
        return None          # the schema moved since the check: this call launches unslotted


def _now(clock: Callable[[], datetime] | None) -> datetime:
    return (clock or (lambda: datetime.now(timezone.utc)))()


def take(coord, launch_id: str, *, clock: Callable[[], datetime] | None = None,
         attention: Callable[[str, str], None] | None = None) -> Hold | None:
    """Take ``launch_id``'s slot. ``None``: launches run unslotted here. Raises :class:`SlotFull` or
    :class:`SlotFault`; either leaves nothing taken."""
    from levain.agents.controller import dispatch_probes

    directory = agent_dir(coord.directory)
    if directory is None or not slot_state(directory, attention):
        return None
    try:
        store = _open(directory)
        if store is None:
            return None
        now = _now(clock)                                          # after the open: a lock wait must not age it
        with store.read() as conn:
            expired = slots.expired_launches(conn, now)
        over = slots.probe_over(expired, *dispatch_probes(coord.directory))   # outside the write lock
        with store.write() as conn:
            fresh = all(owner != launch_id for _s, _k, owner, _e in slots.held(conn))
            got = slots.take(conn, slots.LAUNCH, launch_id, now, over)
            cap = slots.max_slots(conn) if got is None else 0
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise SlotFault(f"the slot table could not be used ({type(exc).__name__}: {str(exc)[:200]})") from exc
    if got is None:
        raise SlotFull(f"all {cap} launch/agent slots are taken")
    return Hold(launch_id, got, fresh, directory)


def release(hold: Hold) -> bool:
    """Release a slot this call took. Never raises: a release that fails leaves the lease to expire and be reclaimed.
    A hold this call did not take is not released."""
    if not hold.fresh:
        return False
    try:
        store = _open(hold.directory)
        if store is None:
            return False
        with store.write() as conn:
            return slots.release(conn, hold.launch_id)
    except Exception:  # noqa: BLE001 - see the docstring
        return False


def sweep(coord, *, clock: Callable[[], datetime] | None = None,
          attention: Callable[[str, str], None] | None = None) -> list[str]:
    """The watcher's pass over the launch slots: release the slot of every launch that is over, renew the lease of
    every launch that is not. A launch whose state cannot be told (no event, an unreadable ``dispatch.db``) keeps its
    slot untouched. Never raises; the lines say what changed."""
    from levain.agents.controller import dispatch_probes

    try:
        directory = agent_dir(coord.directory)
        if directory is None or not slot_state(directory, attention):
            return []
        store = _open(directory)
        if store is None:
            return []
        now = _now(clock)
        with store.read() as conn:
            mine = [owner for _s, kind, owner, _e in slots.held(conn) if kind == slots.LAUNCH]
        if not mine:
            return []
        launch_over, _alive = dispatch_probes(coord.directory)
        verdict: dict[str, bool] = {}
        for launch_id in mine:
            try:
                answer = launch_over(launch_id)
            except Exception:  # noqa: BLE001 - cannot tell keeps the slot
                continue
            if isinstance(answer, bool):
                verdict[launch_id] = answer
        lines: list[str] = []
        with store.write() as conn:
            for launch_id, over in verdict.items():
                if over:
                    if slots.release(conn, launch_id):
                        lines.append(f"launch {launch_id}: slot released (the launch is over)")
                else:
                    slots.renew(conn, launch_id, now)
        return lines
    except Exception:  # noqa: BLE001 - the sweep is bookkeeping; a fault leaves the leases to expire
        return []
