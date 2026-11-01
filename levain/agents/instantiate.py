"""levain.agents.instantiate — a role made into one spawned worker (BUILD_PLAN P3, the spawned half).

A coordinator that wants a worker for a thread names a role, either a REGISTERED one (``@spawn/<name>``, P1's registry)
or an INLINE one (a role mapping carried in the call and never registered). :func:`spawn_worker` (and ``levain coord
spawn``, which calls it) turns that into one worker launch:

1. the role is resolved and its hash checked (:func:`resolve_role`); the registry is always read, so an inline role may
   not borrow the name of a registered role with other content and ``@spawn/<name>`` always means one thing;
2. the launch is asked for through the lease holder's token with a spawn DRAFT as its ``role_ref``
   (:func:`levain.coordinator.lease.spawn_draft`): the role, its slot values, the DONE line and the parent launch.
   The store completes it in the request's own transaction (:func:`levain.coordinator.lease.spawn_record`): the depth
   from the ACTIVE parent's own record, the thread's drop, and the scope cut from ONE envelope head, key by key, to
   the role's ceiling (:func:`levain.coordinator.envelopes.narrow_scope`; it only narrows, so an inline role, which
   nobody reviewed, can never widen the envelope). The activation pins that same envelope, and the caps on depth and
   on concurrent spawns are checked in that transaction too;
3. the worker's brief is built from the stored record alone (:func:`worker_context`): an EPHEMERAL AgentSpec
   (:func:`ephemeral_spec`: input = the drop, output = the handoff, win = the ready item's ``DONE:`` line), never
   registered, never scheduled and never signed, with the stored scope.

The narrowing is per LAUNCH: nothing here writes to the thread's envelope, so one worker's narrow role never narrows
the thread for the coordinator or for the next worker.

BEFORE S1 NOTHING HERE IS AUTHORITY. The scope is what the worker's brief states and what the first reader's verdict
holds a spawned worker's question to; no code path reads it to allow an effect, and a test pins the callers of the
envelope-authority readers. A pre-S1 worker is a Claude Code session under the consent hook, so each gated tool call
stays a per-call consent request.

This module never reads ``max_slots`` or any agent table beyond the role registry, so the cap on concurrent spawns is
the lease's own count of live spawned workers, and the agent-schema rule for slot-taking launches is P6-3b's alone.
"""
from __future__ import annotations

import copy
import os
import sqlite3
from collections.abc import Mapping
from typing import Any

from levain import unshowable
from levain.agents import role as role_mod
from levain.agents import spec as spec_mod
from levain.authority import _canonical
from levain.coordinator import lease as lease_mod
from levain.coordinator.store import CoordError

__all__ = ["BRIEF_DROPS_MAX", "MAX_SPAWN_DEPTH", "MAX_SPAWNS_LIVE", "SpawnRefused", "WorkerContext", "ephemeral_spec", "make_draft",
           "parse_spawn_ref", "render_brief", "resolve_role", "spawn_worker", "verified_record", "where_refusal",
           "worker_context"]

MAX_SPAWN_DEPTH = lease_mod.MAX_SPAWN_DEPTH
MAX_SPAWNS_LIVE = lease_mod.MAX_SPAWNS_LIVE
# [seat] a spawned worker's stop time, in seconds, when the caller names none
DEFAULT_STOP_S = 2 * 60 * 60
# [seat] how many of a thread's drops a worker's brief and spec quote; past it they say how many more the thread
# holds, so a thread that keeps growing can never make the brief too large to build
BRIEF_DROPS_MAX = 20
_EPHEMERAL = "ephemeral"


class SpawnRefused(CoordError):
    """A spawn that is refused; the message names the rule. ``transient`` is True when the cause may pass by itself
    (the store could not be read just now), so a launcher leaves the launch pending for a later pass; False when the
    launch can never start as asked."""

    def __init__(self, message: str = "", *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


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
    A spawned role is a session executor. The registry is read for both: an inline role whose name a registered role
    holds with other content is refused, and so is one whose registered namesake cannot be read (a corrupt row is no
    proof the name is free)."""
    if (ref is None) == (inline is None):
        raise SpawnRefused("a spawn names a registered role (@spawn/<name>) or carries an inline role, one of them")
    if agent_store is None:
        raise SpawnRefused("a spawn reads the role registry, and none was given")
    try:
        if ref is not None:
            name = parse_spawn_ref(ref)
            digest, _version, found = role_mod.get_role(agent_store, name)
            role, inline_flag = found, False
        else:
            if not isinstance(inline, Mapping):
                raise SpawnRefused("an inline role is a mapping")
            role = role_mod.parse_role(copy.deepcopy(dict(inline)))
            digest = role_mod.role_hash(role)
            inline_flag = True
            if role_mod.latest_version(agent_store, role["name"]) is not None:
                taken, _v, _r = role_mod.get_role(agent_store, role["name"])
                if taken != digest:
                    raise SpawnRefused(f"an inline role may not take the name {role['name']!r}: a registered role of "
                                       "that name has other content")
    except role_mod.RoleError as exc:
        raise SpawnRefused(str(exc)) from None
    except sqlite3.Error as exc:
        raise SpawnRefused(f"the role registry could not be read ({exc})") from None
    if role["executor"] != "session":
        raise SpawnRefused(f"role {role['name']!r} has executor {role['executor']!r}; a spawned worker is a session")
    return role, digest, inline_flag


def _one_line(text: str) -> str:
    """A slot value as the brief writes it: every unshowable character, newline and tab included, a visible mark, so
    one value is one line and can never start a line (a section) of the brief."""
    return unshowable.mark(text, allow="")


def render_brief(role: Mapping[str, Any], slots: Mapping[str, Any]) -> str:
    """The role's brief template with the caller's slot values written in. Exactly the declared slots, each of its
    declared type (``str``, ``int`` or ``path``, never a bool; a path is relative, with no ``..`` step, so it names no
    place outside the directory the worker is given); a text value is UTF-8 with no hidden character, and is
    written as one marked line (:func:`_one_line`): the template is the role's instruction text, a slot value is
    data."""
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
            values[name] = v
            continue
        if not isinstance(v, str) or not v:
            raise SpawnRefused(f"slot {name!r} is {'a path' if kind == 'path' else 'text'}, not empty")
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            raise SpawnRefused(f"slot {name!r} is not UTF-8 text") from None
        if unshowable.first_hidden(v, allow="\n\t", selectors=False) is not None:
            raise SpawnRefused(f"slot {name!r} holds a character that cannot be shown")
        if kind == "path" and ("\x00" in v or "\n" in v or v.startswith("/") or ".." in v.split("/")):
            raise SpawnRefused(f"slot {name!r} is one relative path, with no '..' step")
        values[name] = _one_line(v)
    return role["brief"]["template"].format_map(values)


def make_draft(role: Mapping[str, Any], digest: str, *, inline: bool, slots: Mapping[str, Any], done: str | None,
               parent_launch_id: str | None = None) -> str:
    """The canonical text of a spawn DRAFT, which a launch request carries as its ``role_ref``. The role rides inside
    it so the worker's brief is built from the launch alone, whichever process starts it; the store adds the depth,
    the drop, the envelope and the scope."""
    if done is not None:
        if not isinstance(done, str):
            raise SpawnRefused("the DONE line is text")
        try:
            done.encode("utf-8")
        except UnicodeEncodeError:
            raise SpawnRefused("the DONE line is not UTF-8 text") from None
    try:
        text = _canonical({"spawn": role_mod.spawn_namespace(role["name"]), "role_hash": digest, "inline": inline,
                           "role": dict(role), "slots": dict(slots), "done": done,
                           "parent_launch_id": parent_launch_id})
        lease_mod.spawn_draft(text)       # the shape the store checks, refused here with its own message first
    except (lease_mod.LeaseRefused, role_mod.RoleError, ValueError, TypeError) as exc:
        raise SpawnRefused(str(exc)) from None
    return text


def verified_record(text: object) -> dict[str, Any]:
    """The spawn record in ``text`` with its role re-hashed: a record whose role is not the one its hash names, or
    whose spawn name is not the role's, or that cannot be read at all, is a :class:`SpawnRefused`."""
    try:
        rec = lease_mod.spawn_record(text)
        role = role_mod.parse_role(rec["role"])
        if role_mod.role_hash(role) != rec["role_hash"]:
            raise SpawnRefused("the spawn record's role does not hash to its role_hash")
        if rec["spawn"] != role_mod.spawn_namespace(role["name"]):
            raise SpawnRefused("the spawn record names another role than the one it holds")
    except (lease_mod.LeaseRefused, role_mod.RoleError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SpawnRefused(f"{type(exc).__name__}: {exc}" if not isinstance(exc, CoordError) else str(exc)) from None
    rec["role"] = role
    return rec


def ephemeral_spec(role: Mapping[str, Any], digest: str, *, drop_id: str, thread_id: str, title: str,
                   done: str | None, scope: Mapping[str, Any], stop_s: int = DEFAULT_STOP_S,
                   more_drops: tuple[str, ...] = (), unbriefed: int = 0) -> dict[str, Any]:
    """The worker's AgentSpec, never registered: input = the drop (and ``more_drops``, the thread's other drops the
    brief quotes, then how many more, ``unbriefed``, it does not), output = the handoff to the coordinator, win = the
    ready item's ``DONE:`` line (the thread's title when the item names none). The three parts go through the registry
    spec's own parsers, so they are the shapes a registered spec would hold. ``stop_at`` is guidance the brief states;
    nothing ends the worker at that time."""
    name = role_mod.spawn_namespace(role["name"])
    win = done if done else f"the thread is done: {title}"
    source = f"drop {drop_id}" + (f" and drops {', '.join(more_drops)}" if more_drops else "") \
        + (f" and {unbriefed} more on the thread" if unbriefed else "")
    return {
        "name": name, "kind": _EPHEMERAL, "role": {"name": role["name"], "role_hash": digest},
        "input": spec_mod._input({"source": source,
                                  "liveness": "the drops are the operator's words as the thread holds them"}),
        "output": spec_mod._output({"class": "corpus", "destination": f"thread {thread_id}",
                                    "reader": "coordinator", "moment": "when the worker ends"}),
        "win": spec_mod._win({"kind": "human", "check": win}),
        "stop_at": spec_mod._int(stop_s, "stop_at"),
        "scope": copy.deepcopy(dict(scope)),
    }


class WorkerContext:
    """What the worker brief is built from, for one launch: the record, the role's rendered brief, the ephemeral spec,
    the scope the store cut, at request time, from the one envelope it names, and the drops the WORK thread holds now
    (``drops``: ``(drop_id, words)``, the record's own drop first, then the newest others in arrival order, at most
    :data:`BRIEF_DROPS_MAX` in all; ``unbriefed``: how many older ones the thread holds beyond them)."""

    def __init__(self, record: dict[str, Any], rendered: str, spec: dict[str, Any], scope: dict[str, Any],
                 envelope_id: str | None, drop_id: str, thread: Any, drops: list[tuple[str, str]],
                 unbriefed: int = 0) -> None:
        self.record, self.rendered, self.spec, self.scope = record, rendered, spec, scope
        self.envelope_id, self.drop_id, self.thread, self.drops = envelope_id, drop_id, thread, drops
        self.unbriefed = unbriefed


def worker_context(store: Any, launch: Any) -> WorkerContext:
    """The context of spawned-worker launch ``launch``, from its stored record (scope, envelope, drop) and the thread's
    title and drops. Anything that cannot be built (no record, a record that does not verify, no single thread, a
    thread that is not open work holding the record's drop) is a :class:`SpawnRefused`, so a launcher refuses it before
    its spawn claim. A store that could not be read (an SQLite error such as a locked database, or an I/O error) is a
    TRANSIENT refusal: nothing about the launch is wrong, so it may start on a later pass. A :class:`CoordError` (an
    unknown id, a malformed thread) is permanent: no later pass reads it differently."""
    if not getattr(launch, "role_ref", None):
        raise SpawnRefused(f"launch {launch.launch_id} carries no spawned role")
    record = verified_record(launch.role_ref)
    if len(launch.threads) != 1:
        raise SpawnRefused(f"launch {launch.launch_id} holds {len(launch.threads)} threads; a spawned worker holds one")
    try:
        thread = store.thread(launch.threads[0])
        if thread.kind != "work" or thread.state not in lease_mod.LIVE_THREAD_STATES \
                or record["drop_id"] not in thread.members:
            raise SpawnRefused(f"thread {thread.thread_id} is no longer open work holding drop {record['drop_id']}")
        # the thread's members are held sorted by id, so the others are put in arrival order and the NEWEST kept: past
        # the cap, the operator's latest words are the ones a worker most needs
        others = sorted((store.drop(m) for m in thread.members if m != record["drop_id"]),
                        key=lambda d: (d.received_at, d.drop_id))
        kept = others[len(others) - min(len(others), max(BRIEF_DROPS_MAX - 1, 0)):]
        more = tuple(d.drop_id for d in kept)
        unbriefed = len(others) - len(kept)
        spec = ephemeral_spec(record["role"], record["role_hash"], drop_id=record["drop_id"],
                              thread_id=thread.thread_id, title=thread.title, done=record["done"],
                              scope=record["scope"], more_drops=more, unbriefed=unbriefed)
        rendered = render_brief(record["role"], record["slots"])
        drops = [(record["drop_id"], store.drop(record["drop_id"]).words), *((d.drop_id, d.words) for d in kept)]
    except SpawnRefused:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise SpawnRefused(f"the store could not be read for the worker's brief ({type(exc).__name__}: "
                           f"{str(exc)[:200]})", transient=True) from None
    except (CoordError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SpawnRefused(f"the worker's brief cannot be built ({type(exc).__name__}: {str(exc)[:200]})") from None
    return WorkerContext(record, rendered, spec, copy.deepcopy(record["scope"]), record["envelope_id"],
                         record["drop_id"], thread, drops, unbriefed)


def where_refusal(real_cwd: str, scope: Mapping[str, Any]) -> str | None:
    """Why a worker may not run in ``real_cwd`` (a resolved directory) under ``scope`` (None: it may). A scope with no
    ``where`` sets no bound here. Otherwise the directory must be a ``where`` repo's top, or lie inside a directory
    that repo's paths name (``.`` is the whole repo, ``dir/`` a directory): a path that names one file keeps the
    worker at the repo's top, never in a directory beside the file. Each repo is resolved the same way the directory
    was, so a repo stored through a symlink still holds the directories inside it; a repo of ``/`` holds every one."""
    where = scope.get("where") or []
    if not where:
        return None
    for w in where:
        top = os.path.realpath(w["repo"])
        if real_cwd == top:
            return None
        prefix = top if top.endswith("/") else top + "/"
        if not real_cwd.startswith(prefix):
            continue
        rel = real_cwd[len(prefix):]
        if any(p == "." or (p.endswith("/") and (rel + "/").startswith(p)) for p in w["paths"]):
            return None
    named = "; ".join(f"{w['repo']}: {', '.join(w['paths'])}" for w in where)
    return f"{real_cwd} is outside the scope's where ({named[:400]}); a worker runs inside what its scope names"


def spawn_worker(store: Any, token: int, *, thread_id: str, cwd: str, ref: str | None = None,
                 inline: Mapping[str, Any] | None = None, slots: Mapping[str, Any] | None = None,
                 agent_store: Any = None, harness: str | None = None, model: str | None = None,
                 parent_launch: str | None = None, done: str | None = None) -> Any:
    """Ask for a worker on ``thread_id`` under the lease holder's ``token``. ``parent_launch`` names the ACTIVE spawned
    worker that is spawning this one, and the store derives the depth from its record. A worker holds no lease token
    and no door verb, so the only spawner is the lease holder: the depth is the LINEAGE THE LEASE HOLDER DECLARES (a
    request that names no parent is depth 1), not a bound a caller cannot get around. What binds is the lease and the
    cap on live spawns (:data:`MAX_SPAWNS_LIVE`). ``harness`` and ``model`` default
    to the role's own, and one that names another is refused. Returns the pending launch; the watcher or ``levain
    coord launch`` starts it. Every refusal is a :class:`SpawnRefused` and leaves nothing written."""
    role, digest, is_inline = resolve_role(agent_store, ref=ref, inline=inline)
    harness = role["harness"] if harness is None else harness
    model = role["model"] if model is None else model
    if harness != role["harness"] or model != role["model"]:
        raise SpawnRefused(f"role {role['name']!r} runs {role['harness']} model {role['model']!r}; a spawn of it runs "
                           "the same")
    if harness != "claude":
        raise SpawnRefused(f"role {role['name']!r} runs on {harness}: a {harness} worker is not built, only claude")
    values = dict(slots or {})
    render_brief(role, values)            # refused now, never at the worker's start
    text = make_draft(role, digest, inline=is_inline, slots=values, done=done, parent_launch_id=parent_launch)
    try:
        return store.launch_request(token, harness=harness, role="worker", threads=[thread_id], model=model,
                                    cwd=cwd, role_ref=text)
    except CoordError as exc:
        raise SpawnRefused(str(exc)) from None
