"""levain.agents.signed_role: the signed role's withhold, derived from evidence the signature covers (levain 1107, W2).

His passkey signs an agent LINK, and the link's record names ``spec_hash`` and ``agent_id``. ``spec_hash`` is
sha256(``SPEC_HASH_PREFIX`` + canonical({agent_id, spec})) and the spec pins ``role.role_hash`` =
sha256(``ROLE_HASH_PREFIX`` + canonical(role)); the role's ``ceiling.withhold`` is therefore covered by that
signature. The run server never holds the agent store, so the operator-uid client carries the spec text, the role text
and the agent id beside the proof (:class:`SignedRole`), and :func:`signed_withhold` re-derives the chain here:
each hash, each text being its own canonical form, and the withhold read out of the role. A snapshot whose withhold is
anything else than this is not the one he signed (Phill np-fc4ea962 (a): "signing arrives with the signed role at W2").

PURE and light on purpose: hashlib, json and ``authority._canonical`` only, no agent store, no sqlite, so the dispatch
run server (a service under the root Python) can import it. ``levain.agents.spec`` and ``levain.agents.role`` take the
two hash prefixes from HERE, so the writer of a hash and its verifier cannot drift.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from levain.authority import _canonical

__all__ = ["SPEC_HASH_PREFIX", "ROLE_HASH_PREFIX", "SIGNED_TEXT_MAX", "SignedRole", "SignedRoleRefused", "signed_withhold", "withhold_problem"]

SPEC_HASH_PREFIX = b"levain-agentspec/1\n"
ROLE_HASH_PREFIX = b"levain-rolespec/1\n"
#: [judged] bytes of one text on the wire; a spec or role is a few KiB, and one frame holds all three members
SIGNED_TEXT_MAX = 64 * 1024
_AGENT_ID = re.compile(r"ag-[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class SignedRoleRefused(ValueError):
    """The evidence does not derive the signed role; ``name`` says which link of the chain broke."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name, self.detail = name, detail
        super().__init__(f"{name}: {detail}" if detail else name)


@dataclass(frozen=True)
class SignedRole:
    """The canonical spec text and role text of an occurrence, with its agent id: what :func:`signed_withhold` re-hashes."""
    agent_id: str
    spec: str
    role: str

    def __post_init__(self) -> None:
        if type(self.agent_id) is not str or _AGENT_ID.fullmatch(self.agent_id) is None:
            raise ValueError("signed.agent_id is not ag-<32 hex>")
        for name in ("spec", "role"):
            v = getattr(self, name)
            if type(v) is not str or not v or len(v.encode("utf-8", "replace")) > SIGNED_TEXT_MAX:
                raise ValueError(f"signed.{name} is not a short text")


def _json(text: str, what: str) -> dict:
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise SignedRoleRefused(f"{what}-unreadable", "not JSON") from None
    if type(obj) is not dict:
        raise SignedRoleRefused(f"{what}-unreadable", "not an object")
    try:
        canonical_form = _canonical(obj)
    except (ValueError, TypeError, RecursionError):
        raise SignedRoleRefused(f"{what}-unreadable", "not canonical-able") from None
    if canonical_form != text:
        raise SignedRoleRefused(f"{what}-not-canonical", "the text is not its own canonical form")
    return obj


def signed_withhold(signed: SignedRole, spec_hash: str | None) -> tuple[str, ...]:
    """The sorted withhold patterns of the role ``signed`` proves, bound to ``spec_hash`` (the hash the verified link
    names). Raises :class:`SignedRoleRefused` at the first broken link: the spec's hash (``spec-hash``), the role's
    (``role-hash``), a text that is not its canonical form, or a withhold that is not a list of strings. ``spec_hash``
    None compares nothing against a link (the operator-uid side, whose store already re-hashed the spec to its
    occurrence's hash); the server always passes the verified link's."""
    if spec_hash is not None and (type(spec_hash) is not str or _HEX64.fullmatch(spec_hash) is None):
        raise SignedRoleRefused("spec-hash", "the link names no spec hash")
    spec = _json(signed.spec, "spec")
    got = hashlib.sha256(SPEC_HASH_PREFIX + _canonical({"agent_id": signed.agent_id, "spec": spec}).encode()).hexdigest()
    if spec_hash is not None and got != spec_hash:
        raise SignedRoleRefused("spec-hash", "the spec text is not the one the link names")
    pin = spec.get("role")
    role_hash = pin.get("role_hash") if type(pin) is dict else None
    if type(role_hash) is not str or _HEX64.fullmatch(role_hash) is None:
        raise SignedRoleRefused("role-hash", "the spec pins no role hash")
    if hashlib.sha256(ROLE_HASH_PREFIX + signed.role.encode("utf-8", "strict")).hexdigest() != role_hash:
        raise SignedRoleRefused("role-hash", "the role text is not the one the spec pins")
    role = _json(signed.role, "role")
    ceiling = role.get("ceiling")
    withhold: Any = ceiling.get("withhold", []) if type(ceiling) is dict else []
    if type(withhold) is not list or any(type(p) is not str for p in withhold):
        raise SignedRoleRefused("withhold", "the role's ceiling.withhold is not a list of patterns")
    return tuple(sorted(withhold))


def withhold_problem(signed: tuple[str, ...], launched: tuple[str, ...]) -> tuple[str, str] | None:
    """``(name, detail)`` when the withhold a launch (or a manifest) carries is not the signed role's, else None. Both
    uids call this one function. A signed pattern missing from ``launched`` is a LOOSENING (``withhold-loosened``:
    the snapshot would carry a path the role withholds); a pattern the role never declared is ``withhold-mismatch``
    (more withheld than he signed is not loosening, but it is not the snapshot his signature names, and the digest the
    run records would no longer follow from the role)."""
    have, want = set(launched), set(signed)
    if want - have:
        return "withhold-loosened", f"{len(want - have)} of the signed role's withhold patterns are not in the snapshot's"
    if have - want:
        return "withhold-mismatch", f"{len(have - want)} withhold patterns are not the signed role's"
    return None
