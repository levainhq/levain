"""levain.agents.operator — the operator agent record's registry side (K2b, key 1105-36; DESIGN_k2_operator_record).

A word trap, kept in every name here. A SPEC is the registered AgentSpec (``agents`` table, ``ag-<32 hex>``). "The
operator agent" is a different object: the ONE record in ``agent_operator`` (``oa-<32 hex>``), which the registry
materialises (np-4e087a4f (b): only job and role are writable; the agent is derived from an operator-level record and the
dream is a role attached to it). A role is attached by a spec: a row in ``agent_attachments`` ties the spec's ``ag-`` id
and the role NAME it pins to the record.

What lives here, and nothing else:

* :func:`attach_in` — one attach, inside the caller's write transaction. The first attach creates the record. A spec that
  is retired, that pins a job, or whose role row does not pass its own checks is refused; an attachment that already
  exists is re-read and checked before "already attached" is said, so corruption is never reported as idempotence.
* :func:`check_revise` — a revise of an attached spec keeps the role (same NAME, still a role): a newer version of the same
  role passes (the dream's upgrades), another role or a job is refused. ``spec._register_in`` calls it.
* :func:`materialise` — the read side. Every attachment is read through the spec's and the role's own row checks; a missing,
  failing or mismatched row RAISES :class:`OperatorAgentUnreadable` naming it, it is never skipped.
* :func:`unattached_dream` — a live spec that passes :func:`refuse.is_dream` and has no attachment (an install whose dream
  was registered before K2b, until its next ``levain dream enable``): reported as a fact, never as health.

**Attachment confers nothing in 1.0.** No fire, link, binding, ceiling or recall path reads these tables or imports this
module (tests/test_agents_operator.py pins both). The record is unsigned and per store (D-SIGN a, ruling C): the day an
attachment grants anything (memory recall, a wider ceiling), the record and the attach move into the signed authority log.

Known edges, one line each: no detach verb (retire the spec and register anew); no retire of the record (D-RET a); a second
agent store, a ``--store-dir`` store or a shadow install materialises its own agent (ruling C's named limit).
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.agents import refuse, role as _role
from levain.agents.spec import SpecError, _check_row as _spec_check_row

__all__ = ["AttachRefused", "OperatorAgentUnreadable", "AttachedRole", "InstallFacts", "OperatorAgent", "Attached",
           "attach_in", "attach", "check_revise", "materialise", "unattached_dream"]

RECORD_ID = re.compile(r"oa-[0-9a-f]{32}")

# the spec's current version and its pinned role row, LEFT JOINed so a missing part is named, never dropped
_SPEC_COLS = ("a.name, a.current_version, a.registration, a.operation, s.spec_hash, s.spec, s.role_hash, "
              "r.name, r.version, r.spec")
_SPEC_JOINS = (" LEFT JOIN agent_specs s ON s.agent_id = a.agent_id AND s.version = a.current_version"
               " LEFT JOIN agent_roles r ON r.role_hash = s.role_hash")


class AttachRefused(SpecError):
    """An attach, or a revise of an attached spec, is refused; nothing was written."""


class OperatorAgentUnreadable(SpecError):
    """The operator record or one of its attachments cannot be read as itself (a missing agent, a spec or role row that
    fails its own check, a role other than the one attached). The message names the row; a caller never skips it."""


@dataclass(frozen=True)
class AttachedRole:
    agent_id: str           # the spec's ag- id
    name: str               # the spec's name
    version: int            # the spec's current version
    role_name: str         # the role NAME the attachment pins
    role_version: int       # the version of that role the spec's current version pins
    attached_at: str
    operation: str          # active | paused (the spec's own axis, shown, not decided here)


@dataclass(frozen=True)
class InstallFacts:
    """What the install facts file says about this store's install. ``kind`` is live | shadow | None; ``label`` is the
    words to show (an unreadable file is its own refusal text, never "missing" and never "no memory")."""
    kind: str | None
    label: str
    memory_path: Path | None


@dataclass(frozen=True)
class OperatorAgent:
    record_id: str
    created_at: str
    roles: tuple[AttachedRole, ...]
    retired_roles: tuple[AttachedRole, ...]
    install: InstallFacts | None        # None: no install directory was given to consult
    dream_unattached: str | None       # the ag- id of a live dream spec that carries no attachment


@dataclass(frozen=True)
class Attached:
    record_id: str
    agent_id: str
    role_name: str
    attached: bool          # False: the attachment was already there (and passed its checks)
    record_created: bool    # True: this attach made the operator record


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _judge(agent_id: str, row: tuple[Any, ...]) -> tuple[dict[str, Any], dict[str, Any], str, int]:
    """The spec and its pinned role from one joined row, each through its own row check. Returns ``(spec, role,
    role_name, role_version)``; raises :class:`OperatorAgentUnreadable` naming the row for anything that fails."""
    name, cur, _reg, _op, digest, text, s_role_hash, rname, rver, rtext = row
    if digest is None or text is None:
        raise OperatorAgentUnreadable(f"agent {agent_id} ({name!r}): its current version {cur} has no spec row")
    try:
        spec = _spec_check_row(agent_id, name, digest, text)
    except SpecError as exc:
        raise OperatorAgentUnreadable(f"agent {agent_id} ({name!r}): its stored spec is refused: {exc}") from exc
    pinned = spec["role"]
    if pinned["role_hash"] != s_role_hash:
        raise OperatorAgentUnreadable(f"agent {agent_id} ({name!r}): its stored role_hash does not match the role its "
                                      f"spec pins ({pinned['name']!r})")
    if rname is None:
        raise OperatorAgentUnreadable(f"agent {agent_id} ({name!r}): the role {pinned['name']!r} "
                                      f"({s_role_hash[:12]}) it pins is not in the store")
    try:
        role = _role._check_row(rname, s_role_hash, rver, rtext)
    except _role.RoleError as exc:
        raise OperatorAgentUnreadable(f"agent {agent_id} ({name!r}): its role {rname!r} v{rver} is refused: {exc}") from exc
    if rname != pinned["name"]:
        raise OperatorAgentUnreadable(f"agent {agent_id} ({name!r}): its spec pins role {pinned['name']!r} but the row "
                                      f"that hash names is {rname!r}")
    return spec, role, rname, rver


def _record(conn: Any) -> tuple[str, str] | None:
    """``(record_id, created_at)`` of the store's one operator record, None when there is none; a malformed one raises."""
    rows = conn.execute("SELECT record_id, created_at FROM agent_operator").fetchall()
    if not rows:
        return None
    if len(rows) > 1:     # the schema holds this at one; a store that holds more was altered
        raise OperatorAgentUnreadable(f"the store holds {len(rows)} operator agent records, not one")
    record_id, created_at = rows[0]
    if not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id):
        raise OperatorAgentUnreadable(f"the operator agent record id {str(record_id)[:40]!r} is not oa-<32 hex>")
    if not isinstance(created_at, str):
        raise OperatorAgentUnreadable(f"the operator agent record {record_id}: created_at is not text")
    return record_id, created_at


def attach_in(conn: Any, agent_id: str) -> Attached:
    """Attach the registered spec ``agent_id`` (an ``ag-`` id) to the store's operator agent, in the caller's write
    transaction. The first attach creates the record in the same transaction. Raises :class:`AttachRefused` (retired, not
    registered, a job) or :class:`OperatorAgentUnreadable` (a row that fails its check); nothing is written on a raise."""
    row = conn.execute(f"SELECT {_SPEC_COLS} FROM agents a{_SPEC_JOINS} WHERE a.agent_id = ?", (agent_id,)).fetchone()
    if row is None:
        raise AttachRefused(f"no agent {agent_id}: only a registered spec is attached")
    if row[2] != "registered":
        raise AttachRefused(f"agent {agent_id} ({row[0]!r}) is retired: only a registered spec is attached (register "
                            f"it anew, which mints a new id)")
    _spec, role, rname, _rver = _judge(agent_id, row)
    kind = role.get("kind", _role.DEFAULT_KIND)
    if kind != "role":
        raise AttachRefused(f"agent {agent_id} ({row[0]!r}) pins role {rname!r}, a {kind}: a {kind} is not attached to "
                            f"the agent: jobs get no name")
    record = _record(conn)
    held = conn.execute("SELECT role_name FROM agent_attachments WHERE agent_id = ?", (agent_id,)).fetchone()
    if held is not None:
        if held[0] != rname:
            raise OperatorAgentUnreadable(f"the attachment of agent {agent_id} ({row[0]!r}) names role {held[0]!r}, but "
                                          f"its spec pins {rname!r}")
        if record is None:
            raise OperatorAgentUnreadable(f"agent {agent_id} is attached but the store has no operator agent record")
        return Attached(record[0], agent_id, rname, attached=False, record_created=False)
    created = record is None
    record_id = "oa-" + secrets.token_hex(16) if record is None else record[0]
    if created:
        conn.execute("INSERT INTO agent_operator (slot, record_id, created_at) VALUES (1, ?, ?)", (record_id, _now()))
    conn.execute("INSERT INTO agent_attachments (agent_id, role_name, attached_at) VALUES (?,?,?)",
                 (agent_id, rname, _now()))
    return Attached(record_id, agent_id, rname, attached=True, record_created=created)


def attach(store: Any, ref: str) -> tuple[dict[str, Any], Attached]:
    """``levain agents attach``: resolve ``ref`` (a live name or an ``ag-`` id) and attach it, one transaction.
    Returns the agent row and the :class:`Attached` result."""
    from levain.agents.spec import resolve_agent

    with store.write() as conn:
        agent = resolve_agent(conn, ref)
        return agent, attach_in(conn, agent["agent_id"])


def check_revise(conn: Any, agent_id: str, new_role_hash: str) -> None:
    """Refuse a revise of an attached spec that does not keep its role. A newer version of the SAME role name passes; a
    different role, a job, or a role row the store does not hold is refused. No-op for a spec that is not attached."""
    held = conn.execute("SELECT role_name FROM agent_attachments WHERE agent_id = ?", (agent_id,)).fetchone()
    if held is None:
        return
    head = (f"spec {agent_id} is attached to the agent as role {held[0]!r}; a revise keeps that role: retire it and "
            f"register anew to change it")
    row = conn.execute("SELECT name, version, spec FROM agent_roles WHERE role_hash = ?", (new_role_hash,)).fetchone()
    if row is None:
        raise AttachRefused(f"{head} (the new version pins role {new_role_hash[:12]}, which is not registered)")
    try:
        role = _role._check_row(row[0], new_role_hash, row[1], row[2])
    except _role.RoleError as exc:
        raise AttachRefused(f"{head} (the new version's role row is refused: {exc})") from exc
    if row[0] != held[0]:
        raise AttachRefused(f"{head} (the new version pins role {row[0]!r})")
    if role.get("kind", _role.DEFAULT_KIND) != "role":
        raise AttachRefused(f"{head} (the new version pins a {role.get('kind')}, and a job is not attached)")


def unattached_dream(conn: Any) -> str | None:
    """The ``ag-`` id of a live spec that passes :func:`refuse.is_dream` and carries no attachment, else None. Raises
    :class:`OperatorAgentUnreadable` when the dream's own rows fail their checks."""
    rows = conn.execute(f"SELECT a.agent_id, {_SPEC_COLS} FROM agents a{_SPEC_JOINS} WHERE a.name = ? AND "
                        f"a.registration = 'registered' AND NOT EXISTS "
                        f"(SELECT 1 FROM agent_attachments t WHERE t.agent_id = a.agent_id)",
                        (refuse.DREAM_NAME,)).fetchall()
    for agent_id, *rest in rows:
        spec, role, _, _ = _judge(agent_id, tuple(rest))
        if refuse.is_dream(spec, role):
            return agent_id
    return None


def _install(install_dir: Path | str | None) -> InstallFacts | None:
    if install_dir is None:
        return None
    from levain.dream import agent as dream_agent

    # ONE read of the facts file: the kind and the memory path come from the same validated snapshot (a second read
    # could see a file `dream enable` replaced in between). The kind is derived the way dream.agent.install_kind does.
    try:
        inst = dream_agent.read_install(install_dir)
    except dream_agent.InstallRefused as exc:
        if isinstance(exc.__cause__, FileNotFoundError):     # no file (or no directory): the read's own answer
            return InstallFacts(None, "no install facts", None)
        return InstallFacts(None, f"install facts unreadable: {exc}", None)   # no validated snapshot names a kind
    kind = "shadow" if inst.shadow_flow_root is not None else "live"
    return InstallFacts(kind, kind, inst.store_path)


def materialise(conn: Any, *, install_dir: Path | str | None = None) -> OperatorAgent | None:
    """The store's one operator agent, read on ``conn``; None when the store has no record. Every attachment is joined to
    its spec and role and put through their row checks; anything missing or failing raises
    :class:`OperatorAgentUnreadable` naming the row. ``install_dir`` is the agent store's directory (a bare connection does
    not carry it): the install facts are read from there; None leaves ``install`` None (not consulted)."""
    record = _record(conn)
    if record is None:
        extra = conn.execute("SELECT COUNT(*) FROM agent_attachments").fetchone()[0]
        if extra:
            raise OperatorAgentUnreadable(f"the store holds {extra} attachment(s) but no operator agent record")
        return None
    rows = conn.execute(f"SELECT t.agent_id, t.role_name, t.attached_at, {_SPEC_COLS} FROM agent_attachments t "
                        f"LEFT JOIN agents a ON a.agent_id = t.agent_id{_SPEC_JOINS} "
                        f"ORDER BY t.attached_at, t.agent_id").fetchall()
    live: list[AttachedRole] = []
    retired: list[AttachedRole] = []
    for agent_id, held_role, attached_at, *rest in rows:
        row = tuple(rest)
        if row[0] is None:
            raise OperatorAgentUnreadable(f"the attachment of {agent_id} (role {held_role!r}) names no agent in the store")
        _spec, role_doc, rname, rver = _judge(agent_id, row)
        if rname != held_role:
            raise OperatorAgentUnreadable(f"the attachment of agent {agent_id} ({row[0]!r}) names role {held_role!r}, but "
                                          f"its spec pins {rname!r}")
        kind = role_doc.get("kind", _role.DEFAULT_KIND)
        if kind != "role":     # the state attach_in refuses: a job is never an attached role, so it is not shown as one
            raise OperatorAgentUnreadable(f"the attachment of agent {agent_id} ({row[0]!r}) pins {rname!r}, a {kind}: "
                                          f"a {kind} is not attached to the agent")
        item = AttachedRole(agent_id, row[0], row[1], held_role, rver, attached_at, row[3])
        (live if row[2] == "registered" else retired).append(item)
    return OperatorAgent(record[0], record[1], tuple(live), tuple(retired), _install(install_dir), unattached_dream(conn))
