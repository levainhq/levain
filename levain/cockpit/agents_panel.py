"""levain.cockpit.agents_panel: the agents panel's backend (BUILD_PLAN P9a).

One READ-ONLY list panel over the agent store. It reads the store through :func:`levain.agents.store.open_agent_store_view`
(which creates nothing and writes nothing) and the authority log through :class:`levain.agent_authority.AgentAuthority`
(which never writes), and draws what those already say; it decides nothing. Every write an agent row offers is a
kernel verb (:data:`AGENT_VERBS`, defined in :mod:`levain.cockpit.verbs`) whose handler calls the owner of that act
in :mod:`levain.agents` (``set_operation``, ``retire_agent``, ``renew_agent_link``); the panel never calls one.

A row is one agent, ``agent:<agent_id>``, in one of four groups (the ordering ``agents.state``):

- **Firing**: registered, active, and a live agent link covers its current spec (the tick would let it fire);
- **Paused**: registered and its operation is paused (a pause wins over an unarmed link: it is what he did);
- **Unarmed**: registered and active, but nothing lets it fire. The row says why in the words the launch gate uses
  (``no_link``, ``expired``, ``revoked``, ``signer_revoked``), or that a revoke's bar is open, or that arming could not
  be read. An authority log that cannot be read is NEVER read as health: such a row is Unarmed and says so;
- **Retired**: the registration axis, listed after the rest and not counted.

"Armed" here is judged on the agent's current spec hash and the store's own id, the link's liveness, the closed bars
and the signers' standing, as the launch gate judges them. The launch also matches the occurrence's binding, which the
panel does not know, so a Firing row means "a live link covers this spec", not "the next launch is certain".

The "cleared or not" column is not here (wired after P8a).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain import authority as _auth
from levain.agent_authority import AgentAuthority, AgentAuthorityError
from levain.agents.store import AgentStoreRefused, open_agent_store_view
from levain.autonomic.db import StoreFormatError
from levain.cockpit.engine import ProviderSpec, ReadContext
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn

__all__ = ["AGENTS_ID", "AGENT_ORDER", "AGENT_ROW_PREFIX", "AGENT_VERBS", "AgentsWiring", "RETIRED_SHOWN",
           "agents_discoverer"]

AGENTS_ID = "agents"
AGENT_ROW_PREFIX = "agent:"
AGENT_ORDER = "agents.state"
AGENT_FACETS = frozenset({"agent_group", "agent_link"})
# the verbs an agent row offers (levain.cockpit.verbs registers them); `run now` has no owner yet and is not here
AGENT_VERBS = ("agent_pause", "agent_resume", "agent_renew", "agent_retire")
# the retired agents listed after the live ones, by name; older ones are in `levain agents list --all`
RETIRED_SHOWN = 25
_VERSION_FIELDS = ("*",)

_AGENT_SQL = ("SELECT a.agent_id, a.name, a.current_version, a.registration, a.operation, a.failure_streak,"
              " s.spec_hash FROM agents a JOIN agent_specs s ON s.agent_id = a.agent_id"
              " AND s.version = a.current_version")


@dataclass(frozen=True)
class AgentsWiring:
    """What the agents panel and its verbs reach: ``store_dir`` the autonomic store directory (None: this levain
    home's), ``cockpit_root`` the cockpit's data root (the authority log, and where a renew appends), ``signing`` a
    callable returning ``(signer_fp, sign)`` of a laptop-presence key (None: this cockpit holds none, so a renew is
    refused and says so), ``clock`` the instant the panel judges arming at. A cockpit carries it as
    ``cockpit.agents_wiring``; the verbs read it from there."""

    store_dir: Path | None = None
    cockpit_root: Path | None = None
    signing: Callable[[], tuple[str, Callable[[bytes], str]]] | None = None
    clock: Callable[[], datetime] | None = None

    def now(self) -> datetime:
        return (self.clock or (lambda: datetime.now(timezone.utc)))()


def _one_line(text: object, limit: int = 300) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


class _Arming:
    """The authority log read once for one panel read: the verified replay, or why there is none."""

    def __init__(self, wiring: AgentsWiring) -> None:
        self.authority: AgentAuthority | None = None
        self.state: _auth.AuthoritySnapshot | None = None
        self.why: str | None = None
        if wiring.cockpit_root is None:
            self.why = "this cockpit names no authority log"
            return
        try:
            auth = AgentAuthority(wiring.cockpit_root)
            self.state = auth.refresh()
            self.authority = auth
        except Exception as exc:  # noqa: BLE001 - a log that cannot be replayed is never "nothing is armed"
            self.why = f"the authority log cannot be read ({type(exc).__name__})"


def _links(arming: _Arming, agent_id: str, spec_hash: str, store_id: str) -> list[str]:
    """The agent's links for its CURRENT spec, highest-born first (the launch's candidates, without the binding)."""
    state = arming.state
    assert state is not None
    return sorted((g for g in _auth.agent_links(state, agent_id, store_id)
                   if state.policy.grants[g]["spec_hash"] == spec_hash),
                  key=lambda g: state.grant_born.get(g) or 0, reverse=True)


def _judge(arming: _Arming, conn: sqlite3.Connection, agent: dict[str, Any], store_id: str, now: datetime
           ) -> tuple[str, str, str, str]:
    """``(group, words, link id or "", expiry or "")`` of one REGISTERED agent."""
    from levain.agents.controller import _no_link_reason

    if arming.state is None or arming.authority is None:
        return "unarmed", f"arming unknown: {arming.why}", "", ""
    cands = _links(arming, agent["agent_id"], agent["spec_hash"], store_id)
    top = cands[0] if cands else ""
    expiry = str(arming.state.policy.grants[top]["expires_at"]) if top else ""
    if agent["operation"] != "active":
        return "paused", "paused", top, expiry
    if conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                    (agent["agent_id"],)).fetchone() is not None:
        return "unarmed", "barred: a revoke is closing out", top, expiry
    closed = conn.execute("SELECT MAX(revoke_seq) FROM agent_bars WHERE agent_id = ? AND closed_at IS NOT NULL",
                          (agent["agent_id"],)).fetchone()[0] or 0
    try:
        for gid in cands:
            live, _inc = arming.authority.peek(gid, agent_id=agent["agent_id"], store_id=store_id, now=now,
                                               closed_bar_revoke_seq=closed)
            if live:
                return "firing", "armed", gid, str(arming.state.policy.grants[gid]["expires_at"])
    except AgentAuthorityError as exc:
        return "unarmed", f"arming unknown: the authority moved while it was read ({type(exc).__name__})", top, expiry
    reason = _no_link_reason(arming.state, top or None, agent_id=agent["agent_id"], store_id=store_id, closed=closed,
                             now=now)
    return "unarmed", {"no_link": "no signed link covers this spec", "expired": "its link has expired",
                       "revoked": "its links were revoked",
                       "signer_revoked": "a signer of its link is no longer trusted"}.get(reason, reason), top, expiry


def _row(agent: dict[str, Any], group: str, words: str, link: str, expiry: str) -> RowIn:
    streak = f" · {agent['failure_streak']} failed in a row" if agent["failure_streak"] else ""
    until = f" · link until {expiry}" if expiry and group != "retired" else ""
    body = _one_line(f"{agent['name']} · v{agent['current_version']} · {words}{streak}{until}")
    stored = {"agent_id": agent["agent_id"], "name": agent["name"], "version": agent["current_version"],
              "spec_hash": agent["spec_hash"], "registration": agent["registration"],
              "operation": agent["operation"], "failure_streak": agent["failure_streak"], "group": group,
              "words": words, "link": link, "expires_at": expiry}
    return RowIn(id=AGENT_ROW_PREFIX + agent["agent_id"], title=_one_line(agent["name"], 120), body=body,
                 facets={"agent_group": group, "agent_link": link}, stored=stored)


def read_rows(wiring: AgentsWiring, *, only: str | None = None) -> tuple[tuple[RowIn, ...], str | None] | None:
    """The agent rows (all, or the one agent id ``only``) and a note, or None when this install has no agent store.
    Writes nothing: the store is opened through the read-only view and the log through the read-only authority."""
    view = open_agent_store_view(wiring.store_dir)
    if view.path is None:
        return None
    now = wiring.now()
    arming = _Arming(wiring)
    rows: list[RowIn] = []
    older = 0
    with view.read() as conn:
        store_id_row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        if store_id_row is None:
            raise StoreFormatError("the agent store holds no store id")
        store_id = str(store_id_row[0])
        found = [dict(zip(("agent_id", "name", "current_version", "registration", "operation", "failure_streak",
                           "spec_hash"), r))
                 for r in conn.execute(_AGENT_SQL + " ORDER BY a.name, a.created_at").fetchall()]
        retired_seen = 0
        for a in found:
            if only is not None and a["agent_id"] != only:
                continue
            if a["registration"] == "retired":
                retired_seen += 1
                if only is None and retired_seen > RETIRED_SHOWN:
                    older += 1
                    continue
                rows.append(_row(a, "retired", "retired", "", ""))
                continue
            group, words, link, expiry = _judge(arming, conn, a, store_id, now)
            rows.append(_row(a, group, words, link, expiry))
    note = None
    if arming.why is not None:
        note = f"Arming is unknown: {arming.why}; no agent is shown as firing."
    if older:
        note = (note + " " if note else "") + (f"Retired shows {RETIRED_SHOWN}; {older} older: "
                                                "levain agents list --all")
    return tuple(rows), note


def _unreadable(exc: Exception) -> Result:
    if isinstance(exc, (AgentStoreRefused, StoreFormatError, sqlite3.DatabaseError, OSError)):
        return Fault(f"the agent store cannot be read: {type(exc).__name__}: {_one_line(exc)}")
    return Fault(f"agents panel fault, not the store: {type(exc).__name__}: {_one_line(exc)}")


def agents_discoverer(wiring: Callable[[], AgentsWiring]) -> Callable[[ReadContext], list[ProviderSpec]]:
    """The agents panel for the install NOW (register with ``Cockpit.discover(..., retire=True)``): none while the
    install has no agent store, so an install that never used agents draws no empty card. ``wiring`` is asked on every
    read."""
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        try:
            if open_agent_store_view(wiring().store_dir).path is None:
                return []
        except (AgentStoreRefused, StoreFormatError, sqlite3.Error, OSError):
            pass                       # a store that is there and unreadable: the panel draws and says so

        # a fresh callable per pass: a retired panel's state is matched to its spec by the callable's identity
        def agents_read(ctx: ReadContext) -> Result:
            try:
                got = read_rows(wiring())
            except Exception as exc:  # noqa: BLE001 - an unreadable store is this panel's fault, never a raise
                return _unreadable(exc)
            if got is None:
                return Absent("the agent store is gone")
            return Read(rows=got[0], note=got[1])

        def agents_read_one(ctx: ReadContext, row_id: str) -> Result:
            if not isinstance(row_id, str) or not row_id.startswith(AGENT_ROW_PREFIX):
                return Absent("no such agent")
            try:
                got = read_rows(wiring(), only=row_id[len(AGENT_ROW_PREFIX):])
            except Exception as exc:  # noqa: BLE001
                return _unreadable(exc)
            if got is None or not got[0]:
                return Absent("no such agent")
            return Read(rows=got[0])

        return [ProviderSpec(AGENTS_ID, "list", "Agents", "gauge", agents_read, region="operate", rank=30,
                             order=AGENT_ORDER, version_fields=_VERSION_FIELDS, size="medium", span=1,
                             empty="No agent is registered.", facets=AGENT_FACETS, read_one=agents_read_one,
                             verbs=AGENT_VERBS)]
    return discover
