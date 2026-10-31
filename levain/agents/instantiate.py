"""levain.agents.instantiate — a role made into one spawned worker (BUILD_PLAN P3, the spawned half).

A coordinator that wants a worker for a thread names a role, either a REGISTERED one (``@spawn/<name>``, P1's registry)
or an INLINE one (a role mapping carried in the call and never registered). :func:`spawn_worker` turns that into one
worker launch:

1. the role is resolved and its hash checked (:func:`resolve_role`); an inline role may not borrow the name of a
   registered role with other content, so ``@spawn/<name>`` always means one thing;
2. the thread's head envelope, minus its dropped effects, is cut down to the role's ceiling KEY BY KEY
   (:func:`levain.coordinator.envelopes.narrow_scope`). A role's ceiling only narrows: an inline role, which nobody
   reviewed, gets no other treatment than a registered one, so it can never widen the envelope. A thread with no
   envelope gets an empty scope, because there is nothing to narrow from;
3. an EPHEMERAL AgentSpec is derived (:func:`ephemeral_spec`): input = the drop, output = the handoff, win = the ready
   item's ``DONE:`` line. It is never registered, never scheduled and never signed: it is the worker's description,
   built the same way every time from the launch's record;
4. the launch is asked for through the lease holder's token with the spawn record as its ``role_ref``
   (:func:`levain.coordinator.lease.spawn_record`). The caps on depth and on concurrent spawns are checked by the
   store in the same transaction as the write.

BEFORE S1 NOTHING HERE IS AUTHORITY. The scope this module computes is what the worker's brief states and what a
reader sees; no code path reads it to allow anything, and a test pins the callers of the envelope-authority readers. A pre-S1 worker is a Claude Code session under the consent hook, so each gated tool call
stays a per-call consent request. Narrowing that WRITES (a ``drop_effect`` row per effect the role's ceiling lacks) is
:func:`record_narrowing`, which uses the envelope store's own append-only narrowing and so can only narrow.

This module never reads ``max_slots`` or any agent table beyond the role registry, so the cap on concurrent spawns is
the lease's own count of live spawned workers, and the agent-schema rule for slot-taking launches is P6-3b's alone.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from levain import unshowable
from levain.agents import role as role_mod
from levain.agents import spec as spec_mod
from levain.authority import _canonical
from levain.coordinator import lease as lease_mod
from levain.coordinator.envelopes import dropped_effects, narrow_scope
from levain.coordinator.store import CoordError

__all__ = ["MAX_SPAWN_DEPTH", "MAX_SPAWNS_LIVE", "SpawnRefused", "WorkerContext", "ephemeral_spec", "make_record",
           "parse_spawn_ref", "record_narrowing", "render_brief", "resolve_role", "spawn_worker", "verified_record",
           "worker_context"]

MAX_SPAWN_DEPTH = lease_mod.MAX_SPAWN_DEPTH
MAX_SPAWNS_LIVE = lease_mod.MAX_SPAWNS_LIVE
# [seat] a spawned worker's stop time, in seconds, when the caller names none
DEFAULT_STOP_S = 2 * 60 * 60
EMPTY_SCOPE: dict[str, Any] = {"where": [], "lands": [], "pushes": [], "publishes": []}
_EPHEMERAL = "ephemeral"


class SpawnRefused(CoordError):
    """A spawn that is refused; the message names the rule."""


def parse_spawn_ref(ref: object) -> str:
    """The role name in ``@spawn/<name>``, or a :class:`SpawnRefused`."""
    if not isinstance(ref, str) or not ref.startswith(lease_mod.SPAWN_PREFIX):
        raise SpawnRefused("a registered role is named @spawn/<name>")
    name = ref[len(lease_mod.SPAWN_PREFIX):]
    try:
        role_mod.spawn_namespace(name)
    except role_mod.RoleError as exc:
        raise SpawnRefused(str(exc)) from None
    return name


def resolve_role(agent_store: Any, *, ref: str | None = None,
                 inline: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], str, bool]:
    """``(role, role_hash, inline)`` for exactly one of ``ref`` (a registered role, its latest version) and ``inline``.
    A spawned role is a session executor; an inline role whose name a registered role holds with other content is
    refused (``agent_store`` None means no registry is read, and then only inline roles resolve)."""
    if (ref is None) == (inline is None):
        raise SpawnRefused("a spawn names a registered role (@spawn/<name>) or carries an inline role, one of them")
    try:
        if ref is not None:
            name = parse_spawn_ref(ref)
            if agent_store is None:
                raise SpawnRefused("no role registry was given, so a registered role cannot be read")
            digest, _version, found = role_mod.get_role(agent_store, name)
            role, inline_flag = found, False
        else:
            role = role_mod.parse_role(copy.deepcopy(dict(inline)))
            digest = role_mod.role_hash(role)
            inline_flag = True
            if agent_store is not None:
                try:
                    taken, _v, _r = role_mod.get_role(agent_store, role["name"])
                except role_mod.RoleError:
                    taken = None
                if taken is not None and taken != digest:
                    raise SpawnRefused(f"an inline role may not take the name {role['name']!r}: a registered role of "
                                       "that name has other content")
    except role_mod.RoleError as exc:
        raise SpawnRefused(str(exc)) from None
    if role["executor"] != "session":
        raise SpawnRefused(f"role {role['name']!r} has executor {role['executor']!r}; a spawned worker is a session")
    return role, digest, inline_flag


def render_brief(role: Mapping[str, Any], slots: Mapping[str, Any]) -> str:
    """The role's brief template with the caller's slot values written in. Exactly the declared slots, each of its
    declared type (``str``, ``int`` or ``path``, never a bool); text values carry no unshowable character, because a
    value rides into the worker's brief."""
    declared = role["brief"]["slots"]
    if set(slots) != set(declared):
        missing, extra = sorted(set(declared) - set(slots)), sorted(set(slots) - set(declared))
        raise SpawnRefused("the slots are exactly the role's declared ones"
                           + (f"; missing {missing}" if missing else "") + (f"; unknown {extra}" if extra else ""))
    values: dict[str, Any] = {}
    for name, kind in declared.items():
        v = slots[name]
        if kind == "int":
            if type(v) is not int:
                raise SpawnRefused(f"slot {name!r} is an int")
        elif not isinstance(v, str) or not v:
            raise SpawnRefused(f"slot {name!r} is {'a path' if kind == 'path' else 'text'}, not empty")
        elif unshowable.first_hidden(v, allow="\n\t", selectors=False) is not None:
            raise SpawnRefused(f"slot {name!r} holds a character that cannot be shown")
        elif kind == "path" and ("\x00" in v or "\n" in v):
            raise SpawnRefused(f"slot {name!r} is one path")
        values[name] = v
    return role["brief"]["template"].format_map(values)


def make_record(role: Mapping[str, Any], digest: str, *, inline: bool, depth: int, slots: Mapping[str, Any],
                done: str | None) -> str:
    """The canonical text of a spawn record, which a launch's ``role_ref`` holds. The role rides inside it so the
    worker's brief is built from the launch alone, whichever process starts it."""
    text = _canonical({"spawn": role_mod.spawn_namespace(role["name"]), "role_hash": digest, "depth": depth,
                       "inline": inline, "role": dict(role), "slots": dict(slots), "done": done})
    lease_mod.spawn_record(text)          # the shape the store checks, refused here with its own message first
    return text


def verified_record(text: object) -> dict[str, Any]:
    """The spawn record in ``text`` with its role re-hashed: a record whose role is not the one its hash names, or
    whose spawn name is not the role's, is a :class:`SpawnRefused`."""
    try:
        rec = lease_mod.spawn_record(text)
        role = role_mod.parse_role(rec["role"])
        if role_mod.role_hash(role) != rec["role_hash"]:
            raise SpawnRefused("the spawn record's role does not hash to its role_hash")
        if rec["spawn"] != role_mod.spawn_namespace(role["name"]):
            raise SpawnRefused("the spawn record names another role than the one it holds")
    except (lease_mod.LeaseRefused, role_mod.RoleError) as exc:
        raise SpawnRefused(str(exc)) from None
    rec["role"] = role
    return rec


def ephemeral_spec(role: Mapping[str, Any], digest: str, *, drop_id: str, thread_id: str, title: str,
                   done: str | None, scope: Mapping[str, Any], stop_s: int = DEFAULT_STOP_S) -> dict[str, Any]:
    """The worker's AgentSpec, never registered: input = the drop, output = the handoff to the coordinator, win = the
    ready item's ``DONE:`` line (the thread's title when the item names none). The three parts go through the
    registry spec's own parsers, so they are the shapes a registered spec would hold."""
    name = role_mod.spawn_namespace(role["name"])
    win = done if done else f"the thread is done: {title}"
    return {
        "name": name, "kind": _EPHEMERAL, "role": {"name": role["name"], "role_hash": digest},
        "input": spec_mod._input({"source": f"drop {drop_id}",
                                  "liveness": "the drop is the operator's words as the thread holds them"}),
        "output": spec_mod._output({"class": "corpus", "destination": f"thread {thread_id}",
                                    "reader": "coordinator", "moment": "when the worker ends"}),
        "win": spec_mod._win({"kind": "human", "check": win}),
        "stop_at": spec_mod._int(stop_s, "stop_at"),
        "scope": copy.deepcopy(dict(scope)),
    }


class WorkerContext:
    """What the worker brief is built from, for one launch: the record, the role's rendered brief, the ephemeral spec
    and the scope the role's ceiling leaves of the thread's envelope."""

    def __init__(self, record: dict[str, Any], rendered: str, spec: dict[str, Any], scope: dict[str, Any],
                 envelope_id: str | None, drop_id: str, thread: Any, words: str) -> None:
        self.record, self.rendered, self.spec, self.scope = record, rendered, spec, scope
        self.envelope_id, self.drop_id, self.thread, self.words = envelope_id, drop_id, thread, words


def _scope_of(store: Any, thread_id: str) -> tuple[dict[str, Any], str | None]:
    """``(envelope scope, the head's id)``: the head envelope as the thread has it now (dropped effects out), or the
    empty scope when the thread has no envelope."""
    head = store.head(thread_id) if hasattr(store, "head") else None       # a plain CoordStore holds no envelopes
    if head is None:
        return copy.deepcopy(EMPTY_SCOPE), None
    eff = store.effective(head.envelope_id, thread_id)
    scope = {k: copy.deepcopy(eff[k]) for k in ("where", "lands", "pushes", "publishes")}
    return scope, head.envelope_id


def worker_context(store: Any, launch: Any) -> WorkerContext:
    """The context of spawned-worker launch ``launch``, from its record and the store as it is now. A launch with no
    ``role_ref`` is a :class:`SpawnRefused`."""
    if not getattr(launch, "role_ref", None):
        raise SpawnRefused(f"launch {launch.launch_id} carries no spawned role")
    record = verified_record(launch.role_ref)
    thread = store.thread(launch.threads[0])
    scope, envelope_id = _scope_of(store, thread.thread_id)
    narrowed = narrow_scope(scope, record["role"]["ceiling"])
    drop_id = thread.members[0]
    spec = ephemeral_spec(record["role"], record["role_hash"], drop_id=drop_id, thread_id=thread.thread_id,
                          title=thread.title, done=record["done"], scope=narrowed)
    return WorkerContext(record, render_brief(record["role"], record["slots"]), spec, narrowed, envelope_id, drop_id,
                         thread, store.drop(drop_id).words)


def record_narrowing(store: Any, thread_id: str, ceiling: Mapping[str, Any]) -> int:
    """Write, through the envelope store's own ``drop_effect`` narrowing, every effect of the thread's head envelope
    that ``ceiling`` lacks. Rows only narrow and are scoped to this thread. Returns how many were written; a thread
    with no envelope writes none. The path and repo keys (``where``) have no row kind, so they stay in the spawn
    record and the brief."""
    head = store.head(thread_id) if hasattr(store, "head") else None
    if head is None:
        return 0
    scope, _id = _scope_of(store, thread_id)
    wrote = 0
    for identity in dropped_effects(scope, narrow_scope(scope, ceiling)):
        wrote += bool(store.attenuate(head.envelope_id, "drop_effect", identity, thread_id=thread_id))
    return wrote


def spawn_worker(store: Any, token: int, *, thread_id: str, cwd: str, ref: str | None = None,
                 inline: Mapping[str, Any] | None = None, slots: Mapping[str, Any] | None = None,
                 agent_store: Any = None, harness: str = "claude", model: str | None = None,
                 parent_launch: str | None = None, done: str | None = None) -> Any:
    """Ask for a worker on ``thread_id`` under the lease holder's ``token``. ``parent_launch`` (the spawned worker
    that is spawning this one) sets the depth. Returns the pending launch; the watcher or ``levain coord launch``
    starts it. Refusals leave nothing written."""
    thread = store.thread(thread_id)
    if thread.kind != "work" or not thread.members:
        raise SpawnRefused("only a WORK thread that holds a drop gets a worker")
    role, digest, is_inline = resolve_role(agent_store, ref=ref, inline=inline)
    depth = 1
    if parent_launch is not None:
        parent = store.launch(parent_launch)
        if parent is None or parent.role != "worker" or not parent.role_ref:
            raise SpawnRefused("a spawn's parent is a spawned worker launch")
        depth = lease_mod.spawn_record(parent.role_ref)["depth"] + 1
    values = dict(slots or {})
    render_brief(role, values)            # refused now, never at the worker's start
    text = make_record(role, digest, inline=is_inline, depth=depth, slots=values, done=done)
    try:
        return store.launch_request(token, harness=harness, role="worker", threads=[thread_id], model=model, cwd=cwd,
                                    role_ref=text)
    except lease_mod.LeaseRefused as exc:
        raise SpawnRefused(str(exc)) from None
