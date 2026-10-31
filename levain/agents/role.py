"""levain.agents.role — the RoleSpec registry (A0 r6/r7 §3.1, §4.2; BUILD_PLAN P1).

A role is a plain mapping with exactly these fields (anything else is refused, naming the field):

- ``name``      ``[a-z][a-z0-9-]{0,47}``; the spawned namespace is ``@spawn/<name>``.
- ``brief``     ``{"template": str, "slots": {slot name: "str" | "int" | "path"}}``. The template's ``{slot}``
                placeholders must be exactly the declared slots, each written ``{slot}`` with no conversion and no format
                spec (``{{`` and ``}}`` are literal braces).
- ``ceiling``   a closed mapping, any of ``where`` (list of ``{"repo": str, "paths": [str]}``) and ``lands`` /
                ``pushes`` / ``publishes`` (lists of str): the envelope vocabulary (A0 §3.2). A0 names no shape for
                a role's ceiling; this is the decision, kept to what the link's ``capabilities`` already speak.
- ``model``     str; non-empty for a session executor.
- ``harness``   ``claude`` | ``codex`` for a session executor, ``""`` for any other executor (D1 R-D1).
- ``executor``  ``session`` | ``dream``.

There is no float anywhere, and no signature: authorisation comes through the agent-link, which signs the AgentSpec
hash, which pins :func:`role_hash` (A0 §3.2).

``role_hash`` is sha256(``b"levain-rolespec/1\\n"`` + canonical(role)) with ``authority._canonical`` as the canonical
form. The version is the registry's counter per name (UNIQUE(name, version)) and is NOT a role field, so it is not
hashed: the same content is the same hash whichever version holds it. The ``agent_roles.spec`` column holds the
canonical text, which is what P5b's ``compile`` re-hashes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import string
from datetime import datetime, timezone
from typing import Any

from levain.agents.store import AgentStore
from levain.authority import _canonical

__all__ = ["RoleError", "ROLE_FIELDS", "ROLE_NAME", "parse_role", "parse_role_text", "canonical_role", "role_hash",
           "instance_ceiling", "spawn_namespace", "add_role", "get_role", "latest_version",
           "role_by_hash"]

ROLE_HASH_PREFIX = b"levain-rolespec/1\n"
ROLE_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")
ROLE_FIELDS = ("name", "brief", "ceiling", "model", "harness", "executor")
EXECUTORS = ("session", "dream")
HARNESSES = ("claude", "codex")
SLOT_TYPES = ("str", "int", "path")
CEILING_LISTS = ("lands", "pushes", "publishes")
MAX_VERSION = 2**63 - 1  # SQLite's INTEGER ceiling


class RoleError(ValueError):
    """A role file or a role lookup is refused; the message names the field or the name."""


def _exact(obj: Any, allowed: tuple[str, ...], where: str, *, required: bool = True) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise RoleError(f"{where} must be an object, not {type(obj).__name__}")
    for key in obj:
        if key not in allowed:
            raise RoleError(f"unknown field {key!r} in {where} (allowed: {', '.join(allowed)})")
    if required:
        for key in allowed:
            if key not in obj:
                raise RoleError(f"missing field {key!r} in {where}")
    return obj


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise RoleError(f"{where} must be a string, not {type(value).__name__}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise RoleError(f"{where} holds a lone surrogate, which is not valid text") from None
    return value


def _str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list):
        raise RoleError(f"{where} must be a list")
    return [_str(v, f"{where}[{i}]") for i, v in enumerate(value)]


def _brief(value: Any) -> dict[str, Any]:
    brief = _exact(value, ("template", "slots"), "brief")
    template = _str(brief["template"], "brief.template")
    slots = brief["slots"]
    if not isinstance(slots, dict):
        raise RoleError("brief.slots must be an object")
    for slot, kind in slots.items():
        if not isinstance(slot, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", slot):
            raise RoleError(f"brief.slots: slot name {slot!r} is not [a-z][a-z0-9_]*")
        if kind not in SLOT_TYPES:
            raise RoleError(f"brief.slots.{slot}: type {kind!r} is not one of {', '.join(SLOT_TYPES)}")
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise RoleError(f"brief.template is not a valid template: {exc}") from None
    for _, field, spec, conv in parsed:
        if field is not None and (spec or conv is not None):
            raise RoleError(f"brief.template: placeholder {field!r} carries a "
                            f"{'conversion !' + conv if conv is not None else 'format spec :' + spec}; "
                            f"a placeholder is exactly {{slot}}")
    used = {f for _, f, _, _ in parsed if f is not None}
    if used - set(slots):
        raise RoleError(f"brief.template uses undeclared slot {sorted(used - set(slots))[0]!r}")
    if set(slots) - used:
        raise RoleError(f"brief.slots declares slot {sorted(set(slots) - used)[0]!r} the template never uses")
    return {"template": template, "slots": dict(slots)}


def _ceiling(value: Any) -> dict[str, Any]:
    ceiling = _exact(value, ("where",) + CEILING_LISTS, "ceiling", required=False)
    out: dict[str, Any] = {}
    for key, val in ceiling.items():
        if key == "where":
            if not isinstance(val, list):
                raise RoleError("ceiling.where must be a list")
            out[key] = []
            for i, w in enumerate(val):
                w = _exact(w, ("repo", "paths"), f"ceiling.where[{i}]")
                out[key].append({"repo": _str(w["repo"], f"ceiling.where[{i}].repo"),
                                 "paths": _str_list(w["paths"], f"ceiling.where[{i}].paths")})
        else:
            out[key] = _str_list(val, f"ceiling.{key}")
    return out


def parse_role(obj: Any) -> dict[str, Any]:
    """Validate a role mapping and return a fresh, normalised copy; raise :class:`RoleError` naming the field."""
    role = _exact(obj, ROLE_FIELDS, "role")
    name = _str(role["name"], "name")
    if not ROLE_NAME.fullmatch(name):
        raise RoleError(f"name {name!r} does not match [a-z][a-z0-9-]{{0,47}}")
    executor = role["executor"]
    if executor not in EXECUTORS:
        raise RoleError(f"executor {executor!r} is not one of {', '.join(EXECUTORS)}")
    harness = _str(role["harness"], "harness")
    if executor == "session" and harness not in HARNESSES:
        raise RoleError(f"harness {harness!r} is not one of {', '.join(HARNESSES)} (a session executor needs one)")
    if executor != "session" and harness != "":
        raise RoleError(f"harness must be empty for the {executor} executor, not {harness!r}")
    model = _str(role["model"], "model")
    if executor == "session" and not model:
        raise RoleError("model must not be empty for a session executor")
    return {"name": name, "brief": _brief(role["brief"]), "ceiling": _ceiling(role["ceiling"]), "model": model,
            "harness": harness, "executor": executor}


def _no_floats(text: str) -> Any:
    raise RoleError(f"a float ({text}) is refused: roles carry no floats or non-finite numbers")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise RoleError(f"field {key!r} appears twice")
        out[key] = val
    return out


def parse_role_text(text: str) -> dict[str, Any]:
    """Parse a role file's JSON text (duplicate keys and floats refused) and validate it."""
    try:
        obj = json.loads(text, object_pairs_hook=_pairs, parse_float=_no_floats, parse_constant=_no_floats)
    except RoleError:
        raise
    except json.JSONDecodeError as exc:
        raise RoleError(f"not valid JSON: {exc}") from None
    except (ValueError, RecursionError) as exc:  # an over-long integer, a nesting too deep to read
        raise RoleError(f"not readable as a role: {type(exc).__name__}: {str(exc)[:100]}") from None
    return parse_role(obj)


def canonical_role(role: dict[str, Any]) -> str:
    """The canonical text of a validated role (the text stored in ``agent_roles.spec``)."""
    return _canonical(parse_role(role))


def role_hash(role: dict[str, Any]) -> str:
    """A0 §3.1: sha256(``b"levain-rolespec/1\\n"`` + canonical(role)), 64 hex. Validates first."""
    return hashlib.sha256(ROLE_HASH_PREFIX + canonical_role(role).encode("utf-8")).hexdigest()


def instance_ceiling(role: dict[str, Any]) -> dict[str, Any]:
    """The ceiling an instance carries: a deep copy, so no instance shares a list or dict with the role's."""
    return copy.deepcopy(parse_role(role)["ceiling"])


def spawn_namespace(name: str) -> str:
    name = _str(name, "name")
    if not ROLE_NAME.fullmatch(name):
        raise RoleError(f"name {name!r} does not match [a-z][a-z0-9-]{{0,47}}")
    return f"@spawn/{name}"


def _check_row(column_name: Any, digest: str, version: Any, text: Any) -> dict[str, Any]:
    """The checks every read of a stored role passes: the spec is text, it parses as a role, it hashes to its key
    and is that role's canonical text, and it names the row's name column. Raise :class:`RoleError` otherwise."""
    if not isinstance(text, str):
        raise RoleError(f"role {column_name!r} v{version}: the stored spec is a {type(text).__name__}, not text")
    role = parse_role_text(text)
    if role_hash(role) != digest or _canonical(role) != text:
        raise RoleError(f"role {column_name!r} v{version}: the stored text does not hash to its role_hash")
    if role["name"] != column_name:
        raise RoleError(f"role {column_name!r} v{version}: the stored text names {role['name']!r}, "
                        f"not {column_name!r}")
    return role


def _version(version: Any) -> int | None:
    if version is None:
        return None
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= MAX_VERSION:
        raise RoleError(f"version must be an integer from 1 to {MAX_VERSION}")
    return version


def add_role(store: AgentStore, role: dict[str, Any]) -> tuple[str, str, int, bool]:
    """Register ``role``; return ``(name, role_hash, version, created)``. The same content already on file is
    returned as it is (``created`` False); new content under a known name takes the next version."""
    role = parse_role(role)
    text = _canonical(role)
    digest = role_hash(role)
    with store.write() as conn:
        row = conn.execute("SELECT name, version, spec FROM agent_roles WHERE role_hash = ?", (digest,)).fetchone()
        if row is not None:
            _check_row(row[0], digest, row[1], row[2])
            return row[0], digest, row[1], False
        top = conn.execute("SELECT MAX(version) FROM agent_roles WHERE name = ?", (role["name"],)).fetchone()[0]
        version = (top or 0) + 1
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("INSERT INTO agent_roles (role_hash, name, version, spec, created_at) VALUES (?,?,?,?,?)",
                     (digest, role["name"], version, text, stamp))
    return role["name"], digest, version, True


def get_role(store: AgentStore, name: str, version: int | None = None) -> tuple[str, int, dict[str, Any]]:
    """The role ``name`` (latest version unless ``version``), as ``(role_hash, version, role)``. The stored text is
    re-hashed: a row whose text does not hash to its key is refused."""
    name = _str(name, "name")
    version = _version(version)
    with store.read() as conn:
        if version is None:
            row = conn.execute("SELECT role_hash, version, spec FROM agent_roles WHERE name = ? "
                               "ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        else:
            row = conn.execute("SELECT role_hash, version, spec FROM agent_roles WHERE name = ? AND version = ?",
                               (name, version)).fetchone()
    if row is None:
        raise RoleError(f"no role named {name!r}" + (f" at version {version}" if version is not None else ""))
    digest, ver, text = row
    return digest, ver, _check_row(name, digest, ver, text)


def role_by_hash(store: AgentStore, digest: str) -> dict[str, Any] | None:
    """The role whose role_hash is ``digest``, or None when no role has it. The row passes the same checks as every
    other read of a stored role (:func:`_check_row`): a row that fails them raises :class:`RoleError`."""
    with store.read() as conn:
        row = conn.execute("SELECT name, version, spec FROM agent_roles WHERE role_hash = ?", (digest,)).fetchone()
    return None if row is None else _check_row(row[0], digest, row[1], row[2])


def latest_version(store: AgentStore, name: str) -> int | None:
    """The newest registered version of ``name``, or None when the name is unknown."""
    name = _str(name, "name")
    with store.read() as conn:
        return conn.execute("SELECT MAX(version) FROM agent_roles WHERE name = ?", (name,)).fetchone()[0]
