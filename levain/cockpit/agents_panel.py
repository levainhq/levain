"""levain.cockpit.agents_panel: the agents panel's backend (BUILD_PLAN P9a).

One READ-ONLY list panel over the agent store. It reads the store through :func:`levain.agents.store.open_agent_store_view`
(which creates nothing and writes nothing) and the authority log through :class:`levain.agent_authority.AgentAuthority`
(which never writes), and draws what those already say; it decides nothing. Every write an agent row offers is a
kernel verb (:data:`AGENT_VERBS`, defined in :mod:`levain.cockpit.verbs`) whose handler calls the owner of that act
in :mod:`levain.agents` (``set_operation``, ``retire_agent``, ``renew_agent_link``); the panel never calls one.

A row is one agent, ``agent:<agent_id>``, in one of four groups (the ordering ``agents.state``), after the pinned row:

- **Agent** (``operator``, K2b / P9a-o): the store's ONE operator agent, a row of its own over
  :func:`levain.agents.operator.materialise`, drawn first and not counted: ``operator-agent:<oa-id>``, or the fixed
  ``operator-agent:none`` while the store has no record (it then says whether the dream is registered and not attached,
  from :func:`levain.agents.operator.unattached_dream`, so a pre-K2b install shows it). Its roles, their operation, the
  install's live/shadow facts and ``dream_unattached`` are words on the row. A record or an attachment that cannot be
  read is THIS row, ``operator-agent:unreadable`` with the reason, never a fault of the whole panel. The row reads and
  offers nothing: no agent verb applies to it. The panel reaches the attachments only through that module.

- **Firing**: registered, active, and a live agent link covers its current spec (the tick would let it fire);
- **Paused**: registered and its operation is paused (a pause wins over an unarmed link: it is what he did);
- **Unarmed**: registered and active, but nothing lets it fire. The row says why in the words the launch gate uses
  (``no_link``, ``expired``, ``revoked``, ``signer_revoked``), or that a revoke's bar is open, or that arming could not
  be read. An authority log that cannot be read is NEVER read as health: such a row is Unarmed and says so;
- **Retired**: the registration axis, listed after the rest and not counted.

"Armed" here is judged on the agent's current spec hash and the store's own id, the link's liveness, the closed bars
and the signers' standing, as the launch gate judges them. The launch also matches the occurrence's binding, which the
panel does not know, so a Firing row means "a live link covers this spec", not "the next launch is certain".

Every row also carries ``agent_kind``: the kind (``job`` or ``role``) of the role its CURRENT spec pins, read from the
stored role through :mod:`levain.agents.role`'s own check, on the same snapshot as the spec. A role that cannot be read
(its row is gone, is not text, does not parse, does not hash to its key, or is not the canonical text) is
``unreadable``, the row says so in its body, and it is never read as a kind or as no kind. The panel only labels: the
split of rows into Jobs and Fleet is the view's (P9b), and the group above is unchanged by the kind.

The "cleared or not" column: a row also says how many of its review cards (the coordinator's ``agent`` feed, P8a) are
open and how many are cleared, read from the review queue of the dispatch directory the wiring names. A card belongs to
the agent whose id or name is one of the colon-separated parts of its ``dedupe_key``. An agent with no card says nothing; a queue that cannot be read is said in the panel's
note and no counts are drawn, so an unreadable queue is never read as "all cleared".
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
from levain.agents import operator as _operator
from levain.agents.role import DEFAULT_KIND, RoleError
from levain.agents.role import _check_row as _check_stored_role
from levain.agents.store import AgentStoreRefused, open_agent_store_view
from levain.autonomic.db import StoreFormatError
from levain.cockpit.engine import ProviderSpec, ReadContext
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn

__all__ = ["AGENTS_ID", "AGENT_ORDER", "AGENT_ROW_PREFIX", "AGENT_VERBS", "AgentsWiring", "OPERATOR_ROW_PREFIX",
           "RETIRED_SHOWN", "agents_discoverer", "cockpit_wiring", "wire_agents"]

AGENTS_ID = "agents"
AGENT_ROW_PREFIX = "agent:"
AGENT_ORDER = "agents.state"
OPERATOR_ROW_PREFIX = "operator-agent:"     # the pinned row of the store's one operator agent (not an ``agent:`` row)
OPERATOR_GROUP = "operator"
AGENT_FACETS = frozenset({"agent_group", "agent_link", "agent_version", "agent_store", "agent_kind"})
UNREADABLE_KIND = "unreadable"
# the verbs an agent row offers (levain.cockpit.verbs registers them); `run now` has no owner yet and is not here
AGENT_VERBS = ("agent_pause", "agent_resume", "agent_renew", "agent_retire")
# the retired agents listed after the live ones, by name; older ones are in `levain agents list --all`
RETIRED_SHOWN = 25
_VERSION_FIELDS = ("*",)

_AGENT_SQL = ("SELECT a.agent_id, a.name, a.current_version, a.registration, a.operation, a.failure_streak,"
              " s.spec_hash, s.role_hash FROM agents a JOIN agent_specs s ON s.agent_id = a.agent_id"
              " AND s.version = a.current_version")


@dataclass(frozen=True)
class AgentsWiring:
    """What the agents panel and its verbs reach: ``store_dir`` the autonomic store directory (None: this levain
    home's), ``cockpit_root`` the cockpit's data root (the authority log, and where a renew appends), ``signing`` a
    callable returning ``(signer_fp, sign)`` of a laptop-presence key (None: this cockpit holds none, so a renew is
    refused and says so), ``clock`` the instant the panel judges arming at, ``dispatch_dir`` a callable naming the coordinator's directory (None:
    no review-card counts). A cockpit carries it as
    ``cockpit.agents_wiring``, the ONE place the panel (:func:`agents_discoverer`) and the verbs both read it: a
    cockpit that carries none draws no agents panel, and an agent verb on it refuses (never a default store)."""

    store_dir: Path | None = None
    cockpit_root: Path | None = None
    signing: Callable[[], tuple[str, Callable[[bytes], str]]] | None = None
    clock: Callable[[], datetime] | None = None
    dispatch_dir: Callable[[], Any] | None = None

    def now(self) -> datetime:
        return (self.clock or (lambda: datetime.now(timezone.utc)))()


def cockpit_wiring(cockpit: Any) -> AgentsWiring | None:
    """The cockpit's agents wiring, or None when it carries none."""
    wiring = getattr(cockpit, "agents_wiring", None)
    return wiring if isinstance(wiring, AgentsWiring) else None


def wire_agents(cockpit: Any, source: Any, *, dispatch_dir: Callable[[], Any] | None = None) -> AgentsWiring:
    """Give ``cockpit`` the agents wiring of the install ``source`` names, and register the panel on it. The store is the
    one ``levain agents`` opens (this levain home's); the authority log is the ``cockpit/`` directory under the source's
    write-scope ledger root, else the install's own ``.levain/cockpit`` when that directory exists, else None. The wiring
    carries no signing key: a cockpit draws, pauses, resumes and retires, and a renew is refused until a laptop-presence key is
    wired. An install whose home has no agent store draws no panel (:func:`agents_discoverer`)."""
    scope = getattr(source, "write_scope", None)
    root = getattr(source, "install_root", None)
    if scope is not None:
        cockpit_root: Path | None = Path(scope.ledger_root) / "cockpit"
    elif root is not None and (Path(root) / ".levain" / "cockpit").is_dir():
        cockpit_root = Path(root) / ".levain" / "cockpit"
    else:
        cockpit_root = None
    wiring = AgentsWiring(store_dir=None, cockpit_root=cockpit_root, dispatch_dir=dispatch_dir)
    cockpit.agents_wiring = wiring
    cockpit.discover(agents_discoverer(cockpit), retire=True)
    return wiring


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
    if state is None:
        raise RuntimeError("_links is asked only of an arming whose log was read")
    return sorted((g for g in _auth.agent_links(state, agent_id, store_id)
                   if state.policy.grants[g]["spec_hash"] == spec_hash),
                  key=lambda g: state.grant_born.get(g) or 0, reverse=True)


def _judge(arming: _Arming, conn: sqlite3.Connection, agent: dict[str, Any], store_id: str, now: datetime
           ) -> tuple[str, str, str, str]:
    """``(group, words, link id or "", expiry or "")`` of one REGISTERED agent. A pause wins over anything the log
    says, read or not (pause and resume need only the store); a log that cannot be judged for this agent leaves its
    link details empty and an active agent Unarmed, never the whole panel a fault (the store's own errors still are)."""
    paused = agent["operation"] != "active"
    if arming.state is None or arming.authority is None:
        return ("paused", "paused", "", "") if paused else ("unarmed", f"arming unknown: {arming.why}", "", "")
    try:
        return _judge_read(arming, conn, agent, store_id, now, paused)
    except sqlite3.Error:
        raise                          # the agent store's bar read: the store is unreadable, the panel says so
    except Exception as exc:  # noqa: BLE001 - a log that cannot be judged is never health, and never one row's fault
        why = ("the authority moved while it was read" if isinstance(exc, AgentAuthorityError)
               else "the authority log could not be judged")
        return ("paused", "paused", "", "") if paused else (
            "unarmed", f"arming unknown: {why} ({type(exc).__name__})", "", "")


def _judge_read(arming: _Arming, conn: sqlite3.Connection, agent: dict[str, Any], store_id: str, now: datetime,
                paused: bool) -> tuple[str, str, str, str]:
    from levain.agents.controller import _no_link_reason

    if arming.state is None or arming.authority is None:
        raise RuntimeError("_judge_read is asked only of an arming whose log was read")
    cands = _links(arming, agent["agent_id"], agent["spec_hash"], store_id)
    top = cands[0] if cands else ""
    expiry = str(arming.state.policy.grants[top]["expires_at"]) if top else ""
    if paused:
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


def _kind(conn: sqlite3.Connection, role_hash: str, seen: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """``(kind, why)`` of the role a spec pins: the kind of the stored role (``why`` empty), or
    :data:`UNREADABLE_KIND` and the reason. The row passes the check every other read of a stored role passes
    (:func:`levain.agents.role._check_row`, on this read's own connection; the spec and role reads are separate
    statements, not one snapshot, which holds because ``agent_roles`` is append-only, so a hash resolves to one row
    whenever it is read): a row that is gone, is not text, does not parse, does not hash to its key or is not the canonical text
    is "unreadable", never an absent kind and never a default one. ``seen`` holds the answers of this read."""
    if role_hash not in seen:
        row = conn.execute("SELECT name, version, spec FROM agent_roles WHERE role_hash = ?", (role_hash,)).fetchone()
        if row is None:
            seen[role_hash] = (UNREADABLE_KIND, "no role row has the hash its spec pins")
        else:
            try:
                role = _check_stored_role(row[0], role_hash, row[1], row[2])
                seen[role_hash] = (role.get("kind", DEFAULT_KIND), "")   # the canonical text omits the default
            except Exception as exc:  # noqa: BLE001 - a role row that cannot be read is never a kind, nor one row's fault
                seen[role_hash] = (UNREADABLE_KIND, _one_line(exc, 100) if isinstance(exc, RoleError)
                                   else f"the role row could not be checked ({type(exc).__name__})")
    return seen[role_hash]


_CARD_SQL = ("SELECT i.dedupe_key, e.op FROM review_items i LEFT JOIN review_events e ON e.item_id = i.item_id AND "
             "e.seq = (SELECT MAX(seq) FROM review_events WHERE item_id = i.item_id) WHERE i.feed = 'agent'")


def _card_marks(wiring: AgentsWiring) -> tuple[list[tuple[str, bool]], str | None]:
    """``([(dedupe_key, cleared)], why)`` of the ``agent`` review cards: every card with the state its latest event
    gives it, or no cards and the reason the queue could not be read. No coordinator store is no cards, not a fault."""
    if wiring.dispatch_dir is None:
        return [], None
    try:
        from levain.autonomic.durable_sqlite import read_transaction
        from levain.coordinator.store import CoordStore, StoreAbsent

        directory = wiring.dispatch_dir()
        if directory is None:
            return [], None
        store = CoordStore(Path(directory))
        try:
            store.open_readonly().close()
        except StoreAbsent:
            return [], None
        with read_transaction(store.open_readonly) as conn:
            return [(str(k), op == "cleared") for k, op in conn.execute(_CARD_SQL).fetchall()], None
    except Exception as exc:  # noqa: BLE001 - a queue that cannot be read is said, never read as "nothing is open"
        return [], f"the review queue cannot be read ({type(exc).__name__})"


def _cards_of(agent: dict[str, Any], cards: list[tuple[str, bool]]) -> tuple[int, int]:
    """``(open, cleared)`` of the cards whose key names this agent by id or by name."""
    mine = [cleared for key, cleared in cards if {agent["agent_id"], agent["name"]} & set(key.split(":"))]
    return mine.count(False), mine.count(True)


def _row(agent: dict[str, Any], group: str, words: str, link: str, expiry: str, store_id: str,
         kind: tuple[str, str], cards: tuple[int, int] = (0, 0)) -> RowIn:
    streak = f" · {agent['failure_streak']} failed in a row" if agent["failure_streak"] else ""
    until = f" · link until {expiry}" if expiry and group != "retired" else ""
    role = f" · role unreadable: {kind[1]}" if kind[0] == UNREADABLE_KIND else ""
    held = f" · cards: {cards[0]} open, {cards[1]} cleared" if cards[0] or cards[1] else ""
    # the card counts and the unreadable-role words come last and are never the part a long line cuts
    body = _one_line(f"{agent['name']} · v{agent['current_version']} · {words}{streak}{until}",
                     300 - len(role) - len(held)) + held + role
    stored = {"agent_id": agent["agent_id"], "name": agent["name"], "version": agent["current_version"],
              "spec_hash": agent["spec_hash"], "registration": agent["registration"],
              "operation": agent["operation"], "failure_streak": agent["failure_streak"], "group": group,
              "words": words, "link": link, "expires_at": expiry, "store_id": store_id, "kind": kind[0],
              "cards_open": cards[0], "cards_cleared": cards[1]}
    # agent_version and agent_store are what a verb's write checks the agent against, inside its own transaction
    return RowIn(id=AGENT_ROW_PREFIX + agent["agent_id"], title=_one_line(agent["name"], 120), body=body,
                 facets={"agent_group": group, "agent_link": link, "agent_version": agent["current_version"],
                         "agent_store": store_id, "agent_kind": kind[0]}, stored=stored)


def _operator_row(conn: sqlite3.Connection, install_dir: Path) -> RowIn:
    """The pinned row of the store's operator agent, read on the panel's own connection through
    :mod:`levain.agents.operator` and nothing lower. A record or an attachment that fails its checks is this row, in the
    ``unreadable`` state with the reason; any other error is the panel's, as everywhere else in it."""
    try:
        agent = _operator.materialise(conn, install_dir=install_dir)
        unattached = _operator.unattached_dream(conn) if agent is None else agent.dream_unattached
    except _operator.OperatorAgentUnreadable as exc:     # before SpecError: it IS one, and is this row's, not the panel's
        why = _one_line(exc, 240)
        return RowIn(id=OPERATOR_ROW_PREFIX + "unreadable", title="Operator agent",
                     body=f"unreadable: {why}", facets={"agent_group": OPERATOR_GROUP},
                     stored={"state": "unreadable", "why": why, "group": OPERATOR_GROUP})
    tail = f" · the dream ({unattached}) is registered but not attached" if unattached else ""
    if agent is None:
        return RowIn(id=OPERATOR_ROW_PREFIX + "none", title="Operator agent",
                     body="no operator agent in this store yet" + tail, facets={"agent_group": OPERATOR_GROUP},
                     stored={"state": "none", "dream_unattached": unattached, "group": OPERATOR_GROUP})
    roles = [f"{r.name} v{r.version} as role {r.role_name} v{r.role_version} ({r.operation})" for r in agent.roles]
    gone = f" · {len(agent.retired_roles)} retired" if agent.retired_roles else ""
    install = agent.install.label if agent.install is not None else "install not consulted"
    head = _one_line(f"Operator agent {agent.record_id} · roles: {'; '.join(roles) or 'none attached'}{gone}"
                     f" · install: {install}", 300 - len(tail)) + tail
    stored = {"state": "attached", "record_id": agent.record_id, "created_at": agent.created_at, "install": install,
              "dream_unattached": unattached, "group": OPERATOR_GROUP,
              "roles": [{"agent_id": r.agent_id, "name": r.name, "version": r.version, "role_name": r.role_name,
                         "role_version": r.role_version, "operation": r.operation} for r in agent.roles],
              "retired_roles": [r.agent_id for r in agent.retired_roles]}
    return RowIn(id=OPERATOR_ROW_PREFIX + agent.record_id, title="Operator agent", body=head,
                 facets={"agent_group": OPERATOR_GROUP}, stored=stored)


def read_operator_row(wiring: AgentsWiring) -> RowIn | None:
    """The operator agent's row, or None when this install has no agent store. Writes nothing."""
    view = open_agent_store_view(wiring.store_dir)
    if view.path is None:
        return None
    with view.read() as conn:
        return _operator_row(conn, view.path.parent)


def read_rows(wiring: AgentsWiring, *, only: str | None = None) -> tuple[tuple[RowIn, ...], str | None] | None:
    """The agent rows (all, or the one agent id ``only``) and a note, or None when this install has no agent store.
    Writes nothing: the store is opened through the read-only view and the log through the read-only authority."""
    view = open_agent_store_view(wiring.store_dir)
    if view.path is None:
        return None
    now = wiring.now()
    arming = _Arming(wiring)
    cards, cards_why = _card_marks(wiring)
    rows: list[RowIn] = []
    older = 0
    with view.read() as conn:
        if only is None:
            rows.append(_operator_row(conn, view.path.parent))      # pinned first: its own group sorts before the rest
        store_id_row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        if store_id_row is None:
            raise StoreFormatError("the agent store holds no store id")
        store_id = str(store_id_row[0])
        kinds: dict[str, tuple[str, str]] = {}
        found = [dict(zip(("agent_id", "name", "current_version", "registration", "operation", "failure_streak",
                           "spec_hash", "role_hash"), r))
                 for r in (conn.execute(_AGENT_SQL + " ORDER BY a.name, a.created_at") if only is None else
                           conn.execute(_AGENT_SQL + " WHERE a.agent_id = ?", (only,))).fetchall()]
        retired_seen = 0
        for a in found:
            if a["registration"] == "retired":
                retired_seen += 1
                if only is None and retired_seen > RETIRED_SHOWN:
                    older += 1
                    continue
                rows.append(_row(a, "retired", "retired", "", "", store_id, _kind(conn, a["role_hash"], kinds),
                                 _cards_of(a, cards)))
                continue
            group, words, link, expiry = _judge(arming, conn, a, store_id, now)
            rows.append(_row(a, group, words, link, expiry, store_id, _kind(conn, a["role_hash"], kinds),
                             _cards_of(a, cards)))
    note = None
    if cards_why is not None:
        note = f"Card counts are not shown: {cards_why}."
    if arming.why is not None:
        note = (note + " " if note else "") + f"Arming is unknown: {arming.why}; no agent is shown as firing."
    if older:
        note = (note + " " if note else "") + (f"Retired shows {RETIRED_SHOWN}; {older} older: "
                                                "levain agents list --all")
    return tuple(rows), note


def _unreadable(exc: Exception) -> Result:
    if isinstance(exc, (AgentStoreRefused, StoreFormatError, sqlite3.DatabaseError, OSError)):
        return Fault(f"the agent store cannot be read: {type(exc).__name__}: {_one_line(exc)}")
    return Fault(f"agents panel fault, not the store: {type(exc).__name__}: {_one_line(exc)}")


def agents_discoverer(cockpit: Any) -> Callable[[ReadContext], list[ProviderSpec]]:
    """The agents panel for the install NOW (register with ``cockpit.discover(agents_discoverer(cockpit),
    retire=True)``): none while the cockpit carries no ``agents_wiring`` or the install has no agent store, so an
    install that never used agents draws no empty card. The wiring is read from the cockpit (:func:`cockpit_wiring`)
    on every read, the same place the agent verbs read it."""
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        held = cockpit_wiring(cockpit)
        if held is None:
            return []
        try:
            if open_agent_store_view(held.store_dir).path is None:
                return []
        except (AgentStoreRefused, StoreFormatError, sqlite3.Error, OSError):
            pass                       # a store that is there and unreadable: the panel draws and says so

        def wiring() -> AgentsWiring:
            now = cockpit_wiring(cockpit)
            if now is None:
                raise AgentStoreRefused("the cockpit no longer carries an agents wiring")
            return now

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
            if isinstance(row_id, str) and row_id.startswith(OPERATOR_ROW_PREFIX):
                try:
                    one = read_operator_row(wiring())
                except Exception as exc:  # noqa: BLE001
                    return _unreadable(exc)
                return Read(rows=(one,)) if one is not None and one.id == row_id else Absent("no such agent")
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
