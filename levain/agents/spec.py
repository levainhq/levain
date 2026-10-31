"""levain.agents.spec — AgentSpec: immutable, content-hashed versions of a named agent (A0 r6 §3.4, §2.3, §5.10;
DESIGN §3.2; BUILD_PLAN P2), and the three lifecycle axes.

A spec is a plain mapping with exactly these fields (anything else is refused, naming the field):

- ``name``       ``[a-z][a-z0-9_-]{0,63}``: a label. The identity is ``agent_id`` (``ag-<32 hex>``, minted at registration).
- ``role``       ``{"name": str, "role_hash": 64 hex}``: the pinned RoleSpec (P1).
- ``input``      ``{"source": str, "liveness": str}``.
- ``output``     ``{"class": push|corpus|recovery, "destination": str, "reader": str, "moment": str,
                 "drill_cadence": int seconds}`` (``moment`` may be left out of a corpus output, it is "when asked";
                 ``drill_cadence`` only on a recovery output).
- ``win``        ``{"kind": verifier|human|inherited|verified-restore, "check": str}``.
- ``trigger``    ``{"kind": "schedule", "cron": 5 fields, "zone": IANA (optional), "window": {"start","end"} (optional)}``.
- ``stop_at``    integer seconds after launch; REQUIRED (the supervisor's deadline is ``launched_at`` + this, §2.3).
- ``overlap`` ``skip``; ``missed`` ``skip``; ``on_failure`` ``{"pause_after": N}`` (default 2); ``memory``
  ``stateful|stateless`` (default stateless); ``placement`` ``local``; ``feeds`` list of agent names (default []).

A missing ``input`` / ``output`` / ``win`` / ``stop_at`` is NOT a parse error: parsing leaves it ``None`` and
:mod:`levain.agents.refuse` refuses it under its rule, so the refusal names the rule. A wrong type or an unknown
field is a parse error (rule 3).

``spec_hash`` is sha256(``b"levain-agentspec/1\\n"`` + canonical({"agent_id", "spec"})), ``authority._canonical`` as the
canonical form (the style of P1's role_hash). The agent_id is hashed because ``agent_specs.spec_hash`` is UNIQUE: a
re-registered retired name carries the same content under a new id and must not collide. The version is the table's
counter and is not hashed. The stored ``spec`` column is the canonical text of the *finalized* spec (zone filled in,
defaults written out), so what was signed is what is read back.

Lifecycle (DESIGN §3.4): ``registration`` registered|retired, ``operation`` active|paused, and ``arming``, which is
derived from the authority log (P5a) and is never stored here. On the agent it was set on, a pause survives a
same-content re-register, a ``--revise`` (a new version) and a retire; only :func:`set_operation` with ``active``
(resume) clears it, and nothing else in this module writes ``operation``. A retire followed by a register of the same
name is NOT that agent: it mints a new ``agent_id`` (DESIGN §3.2) whose operation axis starts ``active``.

Known edges (not built):

- two concurrent ``register --revise`` runs can each pass the feeder-cycle check against the graph as it was before
  either wrote; one operator's CLI does not run two at once, so this is not reachable in real use.
- on a console that cannot encode the verdict text (non-UTF-8), ``register`` raises UnicodeEncodeError after the
  transaction has committed: the specs are registered though the exit code says otherwise.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from levain.agents.store import AgentStore, canonical_zone
from levain.authority import _canonical

__all__ = ["SpecError", "SPEC_FIELDS", "parse_spec", "parse_spec_text", "load_json", "finalize_spec", "spec_hash",
           "canonical_spec", "register_spec", "register_specs", "mint_agent_id", "find_revert", "list_agents", "get_agent", "set_operation", "retire_agent",
           "resolve_agent", "feeder_group", "stored_specs", "can_fire"]

SPEC_HASH_PREFIX = b"levain-agentspec/1\n"
SPEC_FIELDS = ("name", "role", "input", "output", "win", "trigger", "stop_at", "overlap", "missed", "on_failure",
               "memory", "placement", "feeds")
NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
AGENT_ID = re.compile(r"ag-[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")
OUTPUT_CLASSES = ("push", "corpus", "recovery")
WIN_KINDS = ("verifier", "human", "inherited", "verified-restore")
MEMORY = ("stateful", "stateless")
MAX_INT = 2**31 - 1
WHEN_ASKED = "when asked"
DEFAULT_PAUSE_AFTER = 2

_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
# names are legal only in their own field: month names in month (index 3), day names in day-of-week (index 4)
_CRON_NAMES = ({}, {}, {},
               {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
                "nov": 11, "dec": 12},
               {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6})
_CRON_FIELDS = ("minute", "hour", "day-of-month", "month", "day-of-week")


class SpecError(ValueError):
    """A spec file or an agent lookup is refused; the message names the field or the name."""


def _exact(obj: Any, allowed: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise SpecError(f"{where} must be an object, not {type(obj).__name__}")
    for key in obj:
        if key not in allowed:
            raise SpecError(f"unknown field {key!r} in {where} (allowed: {', '.join(allowed)})")
    return obj


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{where} must be a string, not {type(value).__name__}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SpecError(f"{where} holds a lone surrogate, which is not valid text") from None
    return value


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_INT:
        raise SpecError(f"{where} must be an integer from 1 to {MAX_INT}")
    return value


def _name(value: Any, where: str) -> str:
    value = _str(value, where)
    if not NAME.fullmatch(value):
        raise SpecError(f"{where} {value[:60]!r} does not match [a-z][a-z0-9_-]{{0,63}}")
    if AGENT_ID.fullmatch(value):
        # an agent is named by its id or its name, and a ref of the id shape is always read as an id: a name of
        # that shape could never be addressed by name, and would stand in for another agent's id
        raise SpecError(f"{where} {value!r} has the shape of an agent_id (ag-<32 hex>): a name may not, since "
                        f"names and ids address agents in one namespace")
    return value


def _cron(text: str) -> str:
    parts = text.split()
    if len(parts) != 5:
        raise SpecError(f"trigger.cron {text[:60]!r} must have 5 fields (minute hour day month weekday)")
    for part, (lo, hi), names, field in zip(parts, _CRON_BOUNDS, _CRON_NAMES, _CRON_FIELDS):
        for item in part.split(","):
            m = re.fullmatch(r"(\*|[0-9a-z]+(?:-[0-9a-z]+)?)(?:/([0-9]+))?", item.lower())
            if not m:
                raise SpecError(f"trigger.cron field {part!r} is not a cron field")
            if m.group(2) is not None and int(m.group(2)) < 1:
                raise SpecError(f"trigger.cron field {part!r} has a zero step")
            if m.group(1) != "*":
                for end in m.group(1).split("-"):
                    if not end.isdigit() and end not in names:
                        raise SpecError(f"trigger.cron {field} field {part!r}: {end!r} is not a number"
                                        + (" or a name this field takes" if names else
                                           " (names are legal only in the month and day-of-week fields)"))
                    n = names.get(end, int(end) if end.isdigit() else None)
                    if n is None or not lo <= n <= hi:
                        raise SpecError(f"trigger.cron field {part!r}: {end!r} is outside {lo}-{hi}")
    return " ".join(parts)


def _hhmm(value: Any, where: str) -> str:
    value = _str(value, where)
    if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise SpecError(f"{where} {value[:20]!r} is not HH:MM")
    return value


def _trigger(value: Any) -> dict[str, Any]:
    if value is None:
        raise SpecError("missing field 'trigger'")
    t = _exact(value, ("kind", "cron", "zone", "window"), "trigger")
    if t.get("kind") != "schedule":
        raise SpecError(f"trigger.kind {t.get('kind')!r} is not 'schedule' (the only 1.0 trigger)")
    if "cron" not in t:
        raise SpecError("missing field 'cron' in trigger")
    zone = t.get("zone")
    if zone is not None:
        typed = _str(zone, "trigger.zone")
        zone = canonical_zone(typed)
        if zone is None:
            raise SpecError(f"trigger.zone {typed[:60]!r} is not an IANA zone this host knows")
    window = t.get("window")
    if window is not None:
        w = _exact(window, ("start", "end"), "trigger.window")
        for k in ("start", "end"):
            if k not in w:
                raise SpecError(f"missing field {k!r} in trigger.window")
        window = {"start": _hhmm(w["start"], "trigger.window.start"), "end": _hhmm(w["end"], "trigger.window.end")}
    return {"kind": "schedule", "cron": _cron(_str(t["cron"], "trigger.cron")), "zone": zone, "window": window}


def _input(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    i = _exact(value, ("source", "liveness"), "input")
    return {"source": _str(i.get("source", ""), "input.source"),
            "liveness": _str(i.get("liveness", ""), "input.liveness")}


def _output(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    o = _exact(value, ("class", "destination", "reader", "moment", "drill_cadence"), "output")
    cls = o.get("class")
    if cls not in OUTPUT_CLASSES:
        raise SpecError(f"output.class {str(cls)[:30]!r} is not one of {', '.join(OUTPUT_CLASSES)}")
    out: dict[str, Any] = {"class": cls, "destination": _str(o.get("destination", ""), "output.destination"),
                           "reader": _str(o.get("reader", ""), "output.reader"),
                           "moment": _str(o["moment"], "output.moment") if o.get("moment") is not None else None,
                           "drill_cadence": None}
    if o.get("drill_cadence") is not None:
        if cls != "recovery":
            raise SpecError("output.drill_cadence belongs to a recovery output (refinement c)")
        out["drill_cadence"] = _int(o["drill_cadence"], "output.drill_cadence")
    return out


def _win(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    w = _exact(value, ("kind", "check"), "win")
    if w.get("kind") not in WIN_KINDS:
        raise SpecError(f"win.kind {str(w.get('kind'))[:30]!r} is not one of {', '.join(WIN_KINDS)}")
    return {"kind": w["kind"], "check": _str(w.get("check", ""), "win.check")}


def _role(value: Any) -> dict[str, str]:
    if value is None:
        raise SpecError("missing field 'role'")
    r = _exact(value, ("name", "role_hash"), "role")
    for k in ("name", "role_hash"):
        if k not in r:
            raise SpecError(f"missing field {k!r} in role")
    digest = _str(r["role_hash"], "role.role_hash")
    if not HEX64.fullmatch(digest):
        raise SpecError("role.role_hash must be 64 lowercase hex")
    return {"name": _str(r["name"], "role.name"), "role_hash": digest}


def parse_spec(obj: Any) -> dict[str, Any]:
    """Validate a spec mapping and return a fresh normalised copy (``None`` where input/output/win/stop_at is
    absent: :mod:`levain.agents.refuse` refuses those). Raise :class:`SpecError` naming the field."""
    spec = _exact(obj, SPEC_FIELDS, "spec")
    if "name" not in spec:
        raise SpecError("missing field 'name'")
    for fixed, allowed in (("overlap", "skip"), ("missed", "skip"), ("placement", "local")):
        if spec.get(fixed, allowed) != allowed:
            raise SpecError(f"{fixed} {str(spec[fixed])[:30]!r} is not {allowed!r} (the only 1.0 value)")
    if spec.get("memory", "stateless") not in MEMORY:
        raise SpecError(f"memory {str(spec['memory'])[:30]!r} is not one of {', '.join(MEMORY)}")
    on_failure = _exact(spec.get("on_failure", {"pause_after": DEFAULT_PAUSE_AFTER}), ("pause_after",), "on_failure")
    feeds = spec.get("feeds", [])
    if not isinstance(feeds, list):
        raise SpecError("feeds must be a list of agent names")
    feeds = [_name(f, f"feeds[{i}]") for i, f in enumerate(feeds)]
    if len(set(feeds)) != len(feeds):
        raise SpecError("feeds names an agent twice")
    return {"name": _name(spec["name"], "name"), "role": _role(spec.get("role")),
            "input": _input(spec.get("input")), "output": _output(spec.get("output")), "win": _win(spec.get("win")),
            "trigger": _trigger(spec.get("trigger")),
            "stop_at": _int(spec["stop_at"], "stop_at") if spec.get("stop_at") is not None else None,
            "overlap": "skip", "missed": "skip",
            "on_failure": {"pause_after": _int(on_failure.get("pause_after", DEFAULT_PAUSE_AFTER),
                                               "on_failure.pause_after")},
            "memory": spec.get("memory", "stateless"), "placement": "local", "feeds": sorted(feeds)}


def _no_floats(text: str) -> Any:
    raise SpecError(f"a float ({text}) is refused: specs carry no floats or non-finite numbers")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise SpecError(f"field {key!r} appears twice")
        out[key] = val
    return out


def load_json(text: str) -> Any:
    """JSON with duplicate keys and floats refused, as :class:`SpecError`."""
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_float=_no_floats, parse_constant=_no_floats)
    except SpecError:
        raise
    except json.JSONDecodeError as exc:
        raise SpecError(f"not valid JSON: {exc}") from None
    except (ValueError, RecursionError) as exc:
        raise SpecError(f"not readable as a spec: {type(exc).__name__}: {str(exc)[:100]}") from None


def parse_spec_text(text: str) -> dict[str, Any]:
    return parse_spec(load_json(text))


def finalize_spec(spec: dict[str, Any], default_zone: str | None) -> dict[str, Any]:
    """The spec as stored: the install's default zone written into a zone-less schedule and a corpus output's
    moment written as "when asked". Raise :class:`SpecError` when something refuse.py would refuse is still unset."""
    spec = copy.deepcopy(parse_spec(spec))
    if spec["trigger"]["zone"] is None:
        if default_zone is not None and canonical_zone(default_zone) != default_zone:
            raise SpecError(f"the default zone {str(default_zone)[:60]!r} is not an IANA zone name this host knows: "
                            f"set it again with `levain agents zone <IANA>`")
        spec["trigger"]["zone"] = default_zone
    # a corpus output's "" and its absence are one intent, so one stored text and one hash
    if spec["output"] is not None and spec["output"]["class"] == "corpus" and not spec["output"]["moment"]:
        spec["output"]["moment"] = WHEN_ASKED
    missing = [k for k in ("input", "output", "win", "stop_at") if spec[k] is None]
    if spec["trigger"]["zone"] is None:
        missing.append("trigger.zone")
    if missing:
        raise SpecError(f"the spec is not complete: {', '.join(missing)} unset")
    return spec


def canonical_spec(spec: dict[str, Any]) -> str:
    return _canonical(spec)


def spec_hash(agent_id: str, spec: dict[str, Any]) -> str:
    """sha256(``b"levain-agentspec/1\\n"`` + canonical({"agent_id", "spec"})), 64 hex; ``spec`` is finalized."""
    if not AGENT_ID.fullmatch(agent_id):
        raise SpecError("agent_id must be ag-<32 hex>")
    return hashlib.sha256(SPEC_HASH_PREFIX + _canonical({"agent_id": agent_id, "spec": spec}).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- the store -------------------------------------------------------------------------------------------------

def _check_row(agent_id: str, name: str, digest: str, text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        raise SpecError(f"agent {agent_id}: the stored spec is a {type(text).__name__}, not text")
    spec = parse_spec_text(text)
    if spec["name"] != name:
        raise SpecError(f"agent {agent_id}: the stored spec names {spec['name']!r}, not {name!r}")
    if spec_hash(agent_id, spec) != digest or _canonical(spec) != text:
        raise SpecError(f"agent {agent_id}: the stored spec does not hash to its spec_hash")
    return spec


def stored_specs(store: AgentStore) -> dict[str, dict[str, Any]]:
    """name -> the current spec of every registered (not retired) agent, each re-hashed on the way out."""
    with store.read() as conn:
        rows = conn.execute("SELECT a.agent_id, a.name, s.spec_hash, s.spec FROM agents a JOIN agent_specs s "
                            "ON s.agent_id = a.agent_id AND s.version = a.current_version "
                            "WHERE a.registration = 'registered'").fetchall()
    return {name: _check_row(agent_id, name, digest, text) for agent_id, name, digest, text in rows}


def mint_agent_id() -> str:
    """A new ``ag-<32 hex>`` (A0 r6 §3.1): every registration of a name no live agent holds gets a fresh one."""
    return "ag-" + secrets.token_hex(16)


def revert_message(name: str, agent_id: str, version: int) -> str:
    return (f"the content is version {version} of agent {agent_id} ({name!r}), an earlier version: an agent version "
            f"is written once and never again (its spec_hash is unique), so a revert cannot be the next version. "
            f"Change a field so the content is new and --revise it, or retire {agent_id} and register this content "
            f"as a new agent (a new agent_id)")


def earlier_version(conn: Any, agent_id: str, text: str) -> int | None:
    """The version of ``agent_id`` whose stored canonical text is ``text`` (None when no version holds it)."""
    row = conn.execute("SELECT version FROM agent_specs WHERE agent_id = ? AND spec = ? ORDER BY version LIMIT 1",
                       (agent_id, text)).fetchone()
    return None if row is None else row[0]


def find_revert(store: AgentStore, spec: dict[str, Any], default_zone: str | None) -> str | None:
    """The refusal text when ``spec`` (finalized) equals a NON-current version of the live agent holding its name."""
    try:
        text = _canonical(finalize_spec(spec, default_zone))
    except SpecError:
        return None
    with store.read() as conn:
        held = conn.execute("SELECT agent_id, current_version FROM agents WHERE name = ? AND registration = "
                            "'registered'", (spec["name"],)).fetchone()
        if held is None:
            return None
        version = earlier_version(conn, held[0], text)
    return None if version is None or version == held[1] else revert_message(spec["name"], held[0], version)


def _register_in(conn: Any, spec: dict[str, Any], default_zone: str | None,
                 revise: bool) -> tuple[str, str, int, bool]:
    final = finalize_spec(spec, default_zone)
    text = _canonical(final)
    held = conn.execute("SELECT agent_id, current_version FROM agents WHERE name = ? AND registration = "
                        "'registered'", (final["name"],)).fetchone()
    if held is None:
        agent_id = mint_agent_id()
        digest = spec_hash(agent_id, final)
        conn.execute("INSERT INTO agents (agent_id, name, current_version, registration, operation, "
                     "failure_streak, created_at) VALUES (?,?,?,?,?,?,?)",
                     (agent_id, final["name"], 1, "registered", "active", 0, _now()))
        conn.execute("INSERT INTO agent_specs (agent_id, version, spec_hash, role_hash, spec, created_at) "
                     "VALUES (?,?,?,?,?,?)", (agent_id, 1, digest, final["role"]["role_hash"], text, _now()))
        return agent_id, digest, 1, True
    agent_id, current = held
    row = conn.execute("SELECT spec_hash, spec FROM agent_specs WHERE agent_id = ? AND version = ?",
                       (agent_id, current)).fetchone()
    if row is not None and row[1] == text:
        return agent_id, row[0], current, False
    if not revise:
        raise SpecError(f"rule 4: name {final['name']!r} is held by agent {agent_id}, which is not retired "
                        f"(its content differs: pass --revise to write version {current + 1}, or retire it)")
    earlier = earlier_version(conn, agent_id, text)
    if earlier is not None:
        raise SpecError("rule 4: " + revert_message(final["name"], agent_id, earlier))
    top = conn.execute("SELECT MAX(version) FROM agent_specs WHERE agent_id = ?", (agent_id,)).fetchone()[0]
    version = (top or 0) + 1
    digest = spec_hash(agent_id, final)
    conn.execute("INSERT INTO agent_specs (agent_id, version, spec_hash, role_hash, spec, created_at) "
                 "VALUES (?,?,?,?,?,?)", (agent_id, version, digest, final["role"]["role_hash"], text, _now()))
    conn.execute("UPDATE agents SET current_version = ? WHERE agent_id = ?", (version, agent_id))
    return agent_id, digest, version, True


def register_specs(store: AgentStore, specs: list[dict[str, Any]], default_zone: str | None, *,
                   revise: bool = False) -> list[tuple[str, str, int, bool]]:
    """Write validated, already-refusal-checked specs in ONE transaction: every one is written or none is (a
    :class:`SpecError` or a store error from any of them rolls the whole batch back). Two specs of one name in a
    batch are refused before anything is written."""
    names = [s["name"] for s in specs]
    dup = sorted(n for n, c in Counter(names).items() if c > 1)
    if dup:
        raise SpecError(f"rule 4: {', '.join(repr(n) for n in dup)} appears more than once in one registration")
    with store.write() as conn:
        return [_register_in(conn, s, default_zone, revise) for s in specs]


def register_spec(store: AgentStore, spec: dict[str, Any], default_zone: str | None, *,
                  revise: bool = False) -> tuple[str, str, int, bool]:
    """Write a validated, already-refusal-checked spec. Return ``(agent_id, spec_hash, version, created)``.

    A name no live agent holds mints a NEW agent_id (version 1). A name a live agent holds: the same content is
    returned as is (``created`` False); different content with ``revise`` writes the next version of that agent
    (versions are immutable; the operation axis is untouched); without ``revise`` it is refused (rule 4). Content
    equal to an EARLIER version of that agent (a revert) is refused, naming the version (spec_hash is unique)."""
    return register_specs(store, [spec], default_zone, revise=revise)[0]


_AGENT_COLS = "agent_id, name, current_version, registration, operation, failure_streak, created_at"
_AGENT_KEYS = ("agent_id", "name", "current_version", "registration", "operation", "failure_streak", "created_at")


def _agent_dict(row: tuple) -> dict[str, Any]:
    return dict(zip(_AGENT_KEYS, row))


def list_agents(store: AgentStore, *, include_retired: bool = False) -> list[dict[str, Any]]:
    with store.read() as conn:
        rows = conn.execute(f"SELECT {_AGENT_COLS} FROM agents "
                            + ("" if include_retired else "WHERE registration = 'registered' ")
                            + "ORDER BY name, created_at").fetchall()
    return [_agent_dict(r) for r in rows]


def resolve_agent(conn: Any, ref: str) -> dict[str, Any]:
    """An agent by ``agent_id`` or by the name of a live (not retired) agent."""
    if not isinstance(ref, str):
        raise SpecError("an agent is named by a string")
    if AGENT_ID.fullmatch(ref):
        row = conn.execute(f"SELECT {_AGENT_COLS} FROM agents WHERE agent_id = ?", (ref,)).fetchone()
    else:
        row = conn.execute(f"SELECT {_AGENT_COLS} FROM agents WHERE name = ? AND registration = 'registered'",
                           (ref,)).fetchone()
        if row is None:
            ids = [r[0] for r in conn.execute("SELECT agent_id FROM agents WHERE name = ?", (ref,))]
            if ids:
                raise SpecError(f"no live agent named {ref[:60]!r}; retired: {', '.join(ids)} (name one by id)")
    if row is None:
        raise SpecError(f"no agent {ref[:60]!r}")
    return _agent_dict(row)


def get_agent(store: AgentStore, ref: str, version: int | None = None) -> dict[str, Any]:
    """The agent row plus its spec (``version`` or the current one), spec_hash and every version number."""
    with store.read() as conn:
        agent = resolve_agent(conn, ref)
        ver = agent["current_version"] if version is None else version
        row = conn.execute("SELECT spec_hash, spec FROM agent_specs WHERE agent_id = ? AND version = ?",
                           (agent["agent_id"], ver)).fetchone()
        versions = [r[0] for r in conn.execute("SELECT version FROM agent_specs WHERE agent_id = ? ORDER BY version",
                                               (agent["agent_id"],))]
    if row is None:
        raise SpecError(f"agent {agent['agent_id']} has no version {ver}")
    agent.update(version=ver, spec_hash=row[0], spec=_check_row(agent["agent_id"], agent["name"], row[0], row[1]),
                 versions=versions)
    return agent


def can_fire(registration: str, armed: bool, operation: str) -> bool:
    """DESIGN §3.4: an agent fires only when registered, armed and active."""
    return registration == "registered" and armed and operation == "active"


def _group(conn: Any, agent: dict[str, Any]) -> list[dict[str, Any]]:
    """The agent and the registered agents joined to it through ``feeds``. Feeder groups are live names, so an agent
    that is not the live holder of its name (a retired one) is a group of itself: its name may now belong to a new
    agent, and walking from the name would reach that agent and its group. Every row read is re-checked
    (:func:`_check_row`), so a corrupt row is a :class:`SpecError`, never a guess."""
    rows = conn.execute("SELECT a.agent_id, a.name, a.current_version, a.registration, a.operation, "
                        "a.failure_streak, a.created_at, s.spec_hash, s.spec FROM agents a JOIN agent_specs s "
                        "ON s.agent_id = a.agent_id AND s.version = a.current_version "
                        "WHERE a.registration = 'registered'").fetchall()
    by_name: dict[str, dict[str, Any]] = {}
    edges: dict[str, set[str]] = {}
    for r in rows:
        by_name[r[1]] = _agent_dict(r[:7])
        edges.setdefault(r[1], set())
        for f in _check_row(r[0], r[1], r[7], r[8])["feeds"]:
            edges[r[1]].add(f)
            edges.setdefault(f, set()).add(r[1])
    seen: set[str] = set()
    live = by_name.get(agent["name"])
    todo = [agent["name"]] if live is not None and live["agent_id"] == agent["agent_id"] else []
    while todo:
        n = todo.pop()
        if n not in seen and n in by_name:
            seen.add(n)
            todo.extend(edges[n] - seen)
    group = [by_name[n] for n in sorted(seen)]
    if not any(g["agent_id"] == agent["agent_id"] for g in group):
        group.append(agent)
    return group


def feeder_group(store: AgentStore, ref: str) -> list[dict[str, Any]]:
    """The agent and every registered agent connected to it through ``feeds`` (either direction), by name."""
    with store.read() as conn:
        return _group(conn, resolve_agent(conn, ref))


def set_operation(store: AgentStore, ref: str, operation: str) -> list[tuple[dict[str, Any], bool]]:
    """``pause`` / ``resume`` the agent and its whole feeder group together (DESIGN §3.2). Return
    ``[(agent, changed)]``. Setting ``active`` is resume, the only writer of it, and resets the failure streak of each
    member it moves from paused; a member whose operation does not change is not touched (its streak stands)."""
    if operation not in ("paused", "active"):
        raise SpecError("operation is paused or active")
    out = []
    with store.write() as conn:
        for member in _group(conn, resolve_agent(conn, ref)):
            changed = member["operation"] != operation
            if changed and operation == "active":
                conn.execute("UPDATE agents SET operation = 'active', failure_streak = 0 WHERE agent_id = ?",
                             (member["agent_id"],))
                member = {**member, "failure_streak": 0}
            elif changed:
                conn.execute("UPDATE agents SET operation = 'paused' WHERE agent_id = ?", (member["agent_id"],))
            out.append(({**member, "operation": operation}, changed))
    return out


def retire_agent(store: AgentStore, ref: str) -> tuple[dict[str, Any], str, list[str]]:
    """Set the registration axis to ``retired`` (DESIGN §4.3); the operation axis is untouched. Return the agent,
    the output's reader and the other members of its feeder group (what the retirement leaves dangling)."""
    with store.write() as conn:
        agent = resolve_agent(conn, ref)
        if agent["registration"] == "retired":
            raise SpecError(f"agent {agent['agent_id']} is already retired")
        group = [g["name"] for g in _group(conn, agent) if g["agent_id"] != agent["agent_id"]]
        text = conn.execute("SELECT spec FROM agent_specs WHERE agent_id = ? AND version = ?",
                            (agent["agent_id"], agent["current_version"])).fetchone()[0]
        reader = (parse_spec_text(text)["output"] or {}).get("reader", "")
        conn.execute("UPDATE agents SET registration = 'retired' WHERE agent_id = ?", (agent["agent_id"],))
    return {**agent, "registration": "retired"}, reader, group
