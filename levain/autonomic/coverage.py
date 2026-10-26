"""levain.autonomic.coverage — whether a signed graduation policy covers the effect the gate will run.

A binding effect may run below ``CONFIRM`` without a per-action signature only under a graduation policy: an
envelope in the authority log that signs ``actions`` (each at its whole risk dict), a ``lowest_posture`` and an
``expires_at``, and whose every defining link is signed by a key root's allowed-signers file lists in namespace
``levain-authority`` (14b DESIGN_REV3 §3-§4). This module is that rule as pure functions over plain values:

- :class:`Effect` is the gate's own value: the action the executor will run, the risk the gate's frozen manifest
  gives it (``None`` when the manifest does not know it), the sealed tools risk, and the honest-config trigger and
  family.
- :func:`covered` (the policy signs this action at this risk, and its trigger and family lists admit it),
  :func:`anchored` (both signers root-listed), :func:`live` (anchored, unexpired, inside the cap at ``now``, and
  signed with a guard).
- :func:`guard_problem`: what a policy's signed ``guard`` must be (every entry a rationale, a kill predicate this
  package can evaluate and a drill it does not confidently let through, each inside the sign-time caps). The
  authority log refuses to sign a policy it finds a problem in; a replayed policy whose guard has one is refused by
  :meth:`GraduationPolicy.from_record` (it covers nothing), and a policy signed with no guard covers nothing
  (:func:`live`).
- :func:`operative`: the rung a candidate below ``CONFIRM`` runs at, and the fence's cover value.
- :class:`CoverageSnapshot`: what a source caches. Its ``token`` is a property over its own ``head``, the digest
  of the root-file bytes it holds and its ``store_id``, and its signers are parsed from those same bytes, so no
  caller can label one replay with another's token, or one store's snapshot with another store's (DESIGN_REV3_1
  D2). :class:`Uncovered` is what a failed refresh caches.
- :func:`parse_root_signers`: the root file's lines; any option but ``namespaces=`` means not listed.

What binds a writer of the levain home is the action, its risk, the rung, the expiry and the store: a policy covers
only in the store whose id it signs (:attr:`CoverageSnapshot.store_id`), so a policy signed for one store and copied
into another covers nothing there. Triggers and families are honest-config: rows the writer controls, compared here
only as the honest engine's narrowing.

Building the snapshot from the authority log (replay, the born and current signers of each policy) is not this
module's: it imports nothing outside ``levain.autonomic`` (``tests/test_autonomic_leaf.py``), so a source outside
the package implements :class:`CoverageSource` and hands the gate plain values.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from levain.autonomic.kill import Kleene, kill_outcome, kill_purity_error
from levain.autonomic.policy import risk_floor
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk, RiskClass

__all__ = [
    "AUTHORITY_NAMESPACE", "COVER_MOVED", "COVER_NONE", "GRADUATION_GRANT_MAX", "GUARD_DRILL_MAX_BYTES",
    "GUARD_ENTRY_FIELDS", "GUARD_KILL_MAX_BYTES", "GUARD_KILL_MAX_DEPTH", "GUARD_MAX_ENTRIES",
    "GUARD_RATIONALE_MAX", "KEY_TYPES",
    "Cover", "CoverageSnapshot", "CoverageSource", "CoverageView", "Effect", "EffectiveCover", "GraduationPolicy",
    "RootSigner", "Uncovered", "anchored", "authority_keys", "covered", "guard_problem", "live", "operative",
    "parse_root_signers", "peek_cover_snapshot", "peek_snapshot", "risk_dict", "signers_digest", "stricter_risk",
]

# The fence cover value of an effect no live anchored policy covers. A covered value always holds an "@".
COVER_NONE = "none"
# What a peek returns when the authority head or the root file moved since the cached snapshot: it equals no
# decided fence, so the journal finds the effect STALE and the gate decides it again.
COVER_MOVED = "cover:moved"

# The longest a policy may run past ``now``: the consent grants' cap (R1 Q3). A live policy's expiry is at most
# this far ahead (DESIGN_REV3 C-8).
GRADUATION_GRANT_MAX = timedelta(days=30)

# The namespace the authority log is signed in; a root line lists a key for coverage only if it names it.
AUTHORITY_NAMESPACE = "levain-authority"

# The key types the enrol door accepts (``levain.cockpit.keys``); a root line of any other type lists nothing.
KEY_TYPES = frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                       "ssh-rsa", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"})

# The rungs a policy may sign as its lowest. ABOVE_LOOP is never one (DESIGN_REV3 §3).
_LOWEST = {"COOLING_OFF": Posture.COOLING_OFF, "ON_LOOP": Posture.ON_LOOP}

# The record's fields (DESIGN_REV3 §3): those a policy must carry, and those it may. ``guard`` is optional in the
# record so a log signed before it replays; a policy without one covers nothing (:func:`live`).
_REQUIRED = frozenset({"id", "kind", "actions", "lowest_posture", "expires_at", "store_id"})
_OPTIONAL = frozenset({"triggers", "families", "min_clean_fires", "guard"})

# A signed guard entry (autogrant DESIGN D68 (1), D75): the dissent, the kill and the drill, all three, on every entry.
GUARD_ENTRY_FIELDS = frozenset({"rationale", "kill_predicate", "kill_drill"})
# The sign-time caps on a guard (1026-20 RULINGS R13-11): the gate evaluates every kill at every fire, so what a
# policy may carry is bounded where it is signed.
GUARD_MAX_ENTRIES = 8
GUARD_RATIONALE_MAX = 2000          # characters
GUARD_KILL_MAX_BYTES = 4096         # of the kill predicate's canonical UTF-8 text
GUARD_KILL_MAX_DEPTH = 8            # nodes from the root to the deepest leaf, the root counted
GUARD_DRILL_MAX_BYTES = 4096        # of the drill's canonical UTF-8 text

# The one timestamp form of the authority log (``levain.cockpit.consent.utc_stamp``).
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# a base64 blob: whole quads, padding only in the last one
_BLOB = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)")


def risk_dict(risk: ActionRisk | None) -> dict[str, Any] | None:
    """``risk`` as the authority record signs it, the same rendering as the gate's fence."""
    if risk is None:
        return None
    return {"cls": int(risk.cls), "reversible": risk.reversible, "external": risk.external,
            "financial": risk.financial}


def _canonical(obj: Any) -> str:
    # one text per value, so equality is by JSON type: ``true`` is not ``1``, and ``1.0`` is not ``1``
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _str_keys(obj: Any) -> bool:
    # every mapping key at every depth is a string: json.dumps would render ``{1: x}`` as ``{"1": x}``
    if isinstance(obj, Mapping):
        return all(isinstance(k, str) and _str_keys(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return all(_str_keys(v) for v in obj)
    return True


def _kill_depth(node: Any) -> int:
    # nodes from ``node`` to its deepest leaf, ``node`` counted; called only on a predicate kill_purity_error passed,
    # whose nesting it already bounds
    op = node.get("op")
    if op in ("and", "or"):
        return 1 + max(_kill_depth(c) for c in node["clauses"])
    if op == "not":
        return 1 + _kill_depth(node["clause"])
    return 1


class _NotJSON(Exception):
    """A guard field that is not JSON with string keys (``_json_form``)."""


def _json_form(value: Any) -> tuple[str, int]:
    # ``value``'s canonical text and its UTF-8 size; _NotJSON for a value that is not JSON with string keys,
    # UnicodeEncodeError for text no UTF-8 holds (a lone surrogate)
    try:
        if not _str_keys(value):
            raise _NotJSON
        text = _canonical(value)
    except (TypeError, ValueError, RecursionError):
        raise _NotJSON from None
    return text, len(text.encode("utf-8"))


def guard_problem(guard: Any) -> str | None:
    """Why ``guard`` cannot be a graduation policy's signed guard, or ``None``. A guard is a non-empty list of at most
    :data:`GUARD_MAX_ENTRIES` entries, and EVERY entry is complete (DESIGN D83): exactly :data:`GUARD_ENTRY_FIELDS`, a
    non-blank ``rationale`` of at most :data:`GUARD_RATIONALE_MAX` characters, a ``kill_predicate`` this package's
    kill substrate evaluates (``kill_purity_error``) within :data:`GUARD_KILL_MAX_BYTES` and
    :data:`GUARD_KILL_MAX_DEPTH`, and a ``kill_drill`` (a concrete event, an object within
    :data:`GUARD_DRILL_MAX_BYTES`), every field JSON text UTF-8 can hold.

    The drill must not be one the kill confidently lets through (``kill_outcome`` FALSE: the compiler's drill gate).
    A drill the kill cannot decide (UNKNOWN: a field it names is absent, or an operand it cannot compare) is accepted:
    ``kill_trips``, the fire-time reading, treats UNKNOWN as a trip (fail-safe). Such a drill does not show the kill
    can ever answer TRUE.

    Never raises: an evaluation inside it that raises is answered as a problem naming the entry and the field."""
    if not isinstance(guard, (list, tuple)) or not guard:
        return "the guard is a non-empty list of entries"
    if len(guard) > GUARD_MAX_ENTRIES:
        return f"the guard holds {len(guard)} entries, more than {GUARD_MAX_ENTRIES}"
    for i, entry in enumerate(guard):
        what = f"guard[{i}]"
        if not isinstance(entry, Mapping):
            return f"{what} is not an object"
        try:
            keys = set(entry)
        except Exception as exc:  # noqa: BLE001 - a mapping that cannot list its keys is a problem, never a raise
            return f"{what} cannot be read ({type(exc).__name__})"
        if keys != GUARD_ENTRY_FIELDS:
            return f"{what} must carry exactly {', '.join(sorted(GUARD_ENTRY_FIELDS))}"
        why = _entry_problem(what, entry)
        if why is not None:
            return why
    return None


def _entry_problem(what: str, entry: Mapping[str, Any]) -> str | None:
    # one complete entry's problem (guard_problem's rule); ``at`` names the field each step reads, so an evaluation
    # that raises is answered for that field
    at = "rationale"
    try:
        rationale = entry["rationale"]
        if not isinstance(rationale, str) or not rationale.strip():
            return f"{what}.rationale must be non-blank text (why this might be wrong)"
        if len(rationale) > GUARD_RATIONALE_MAX:
            return f"{what}.rationale is longer than {GUARD_RATIONALE_MAX} characters"
        rationale.encode("utf-8")
        # one JSON form of each field: what the kill substrate reads is plain dicts and lists, never a caller's
        # mapping type
        at = "kill_predicate"
        kill_text, kill_size = _json_form(entry["kill_predicate"])
        if kill_size > GUARD_KILL_MAX_BYTES:
            return f"{what}.kill_predicate is longer than {GUARD_KILL_MAX_BYTES} bytes"
        kill = json.loads(kill_text)
        why = kill_purity_error(kill)
        if why is not None:
            return f"{what}.kill_predicate is not a kill the gate can evaluate: {why}"
        depth = _kill_depth(kill)
        if depth > GUARD_KILL_MAX_DEPTH:
            return f"{what}.kill_predicate nests {depth} deep, more than {GUARD_KILL_MAX_DEPTH}"
        at = "kill_drill"
        drill_text, drill_size = _json_form(entry["kill_drill"])
        if drill_size > GUARD_DRILL_MAX_BYTES:
            return f"{what}.kill_drill is longer than {GUARD_DRILL_MAX_BYTES} bytes"
        drill = json.loads(drill_text)
        if not isinstance(drill, dict):
            return f"{what}.kill_drill must be an object (a concrete event)"
        if kill_outcome(kill, drill) is Kleene.FALSE:
            return f"{what}.kill_drill does not trip its kill: a drill is an event the kill stops"
    except _NotJSON:
        return f"{what}.{at} is not JSON with string keys"
    except UnicodeError:
        return f"{what}.{at} is not text UTF-8 can hold (a lone surrogate)"
    except Exception as exc:  # noqa: BLE001 - the rule answers every entry with a problem string, never a raise
        return f"{what}.{at}: the gate cannot evaluate it ({type(exc).__name__})"
    return None


def stricter_risk(a: ActionRisk | None, b: ActionRisk | None) -> ActionRisk | None:
    """The risk that is at least as strict as both: the higher class, and each tag toward involvement."""
    if a is None or b is None:
        return a if b is None else b
    return ActionRisk(RiskClass(max(a.cls, b.cls)), reversible=a.reversible and b.reversible,
                      external=a.external or b.external, financial=a.financial or b.financial)


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(_STAMP_FORMAT)


def _aware(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    return now


@dataclass(frozen=True)
class Effect:
    """The effect the gate will run, built by the gate from its own reads.

    ``action_name`` is what the executor runs (the request's, or the pending's on resolve). ``action_risk`` is the
    gate's frozen manifest's risk for it, ``None`` when the manifest does not know the action (so uncovered).
    ``tools_risk`` is the binding's sealed tools risk, ``None`` for a manual effect. ``trigger_type`` and ``family``
    are read from the binding: honest-config only, ``None`` for a manual effect."""

    action_name: str
    action_risk: ActionRisk | None
    tools_risk: ActionRisk | None
    trigger_type: str | None
    family: str | None


@dataclass(frozen=True)
class GraduationPolicy:
    """One graduation policy of a replayed authority log, with what anchoring needs.

    ``born_signer`` and ``current_signer`` are the public keys (``<type> <base64>``) of the signers of the link that
    brought this incarnation in and of the link that last renewed it. ``incarnation`` is the authority log's value
    for it; with the expiry, it is the fence's cover value. ``store_id`` is the store the policy was signed for; it
    covers in no other. Build it with :meth:`from_record`.

    ``born`` is the seq of the link that brought this incarnation in, and ``terms_text`` the canonical text of the
    signed record minus its expiry: the bytes the incarnation hashes. Construction refuses an incarnation that is not
    ``<born>:<sha256 of terms_text>`` and terms that disagree with any field, so a policy can carry no action, rung,
    store or :attr:`guard` other than the terms its incarnation names."""

    grant_id: str
    incarnation: str
    actions: tuple[tuple[str, str], ...]        # (action name, the canonical text of its signed risk dict), sorted
    lowest_posture: Posture
    expires_at: datetime
    triggers: frozenset[str] | None
    families: frozenset[str] | None
    born_signer: str | None
    current_signer: str | None
    store_id: str
    born: int
    terms_text: str
    # the signed terms construction parsed from terms_text (1026-21 RULINGS G1-6): what guarded, guard and a
    # subclass's terms read, so nothing parses them again per peek
    _terms: Any = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        # a policy built directly, not through from_record, is held to the shape from_record builds: any other is a
        # ValueError, so no caller can hand the snapshot a rung, an expiry or an action from_record refuses
        if not isinstance(self.grant_id, str) or not self.grant_id:
            raise ValueError("a graduation policy's id is a non-empty string")
        if not isinstance(self.incarnation, str) or not self.incarnation:
            raise ValueError("a graduation policy's incarnation is a non-empty string")
        _check_actions(self.actions)
        if not isinstance(self.lowest_posture, Posture) or self.lowest_posture not in _LOWEST.values():
            raise ValueError(f"lowest_posture {self.lowest_posture!r} is not one of {sorted(_LOWEST)}")
        if (not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None
                or self.expires_at.utcoffset() is None):
            raise ValueError("expires_at is an aware datetime")
        for what, names in (("triggers", self.triggers), ("families", self.families)):
            if names is not None and not (isinstance(names, frozenset) and names
                                          and all(isinstance(n, str) and n for n in names)):
                raise ValueError(f"{what} is None or a non-empty frozenset of names")
        for what, key in (("born_signer", self.born_signer), ("current_signer", self.current_signer)):
            if key is not None and not isinstance(key, str):
                raise ValueError(f"{what} is None or a public key string")
        if not isinstance(self.store_id, str) or not self.store_id:
            raise ValueError(f"store_id {str(self.store_id)[:40]!r} is not a store's id")
        object.__setattr__(self, "_terms", self._check_terms())

    def _check_terms(self) -> dict[str, Any]:
        # the terms are the bytes the incarnation names, and they say what every field says
        if type(self.born) is not int or self.born < 1:
            raise ValueError("born is the seq of the link that brought the policy in")
        if not isinstance(self.terms_text, str):
            raise ValueError("terms_text is the canonical text of the signed terms")
        digest = hashlib.sha256(self.terms_text.encode("utf-8")).hexdigest()
        if self.incarnation != f"{self.born}:{digest}":
            raise ValueError("the terms are not the ones the policy's incarnation names")
        try:
            terms = json.loads(self.terms_text)
            ok = isinstance(terms, dict) and _canonical(terms) == self.terms_text
        except (TypeError, ValueError, RecursionError):
            ok = False
        if not ok:
            raise ValueError("terms_text is not the canonical text of a JSON object")
        if "expires_at" in terms:
            raise ValueError("the terms are the record minus its expiry")
        try:
            actions = terms.get("actions")
            agree = (terms.get("kind") == "graduation" and terms.get("id") == self.grant_id
                     and terms.get("lowest_posture") == self.lowest_posture.name
                     and terms.get("store_id") == self.store_id
                     and isinstance(actions, dict)
                     and tuple(sorted((n, _canonical(r)) for n, r in actions.items())) == self.actions
                     and _names(terms.get("triggers"), "triggers") == self.triggers
                     and _names(terms.get("families"), "families") == self.families)
        except (TypeError, ValueError, RecursionError):
            agree = False
        if not agree:
            raise ValueError("the policy's fields are not its signed terms")
        if "guard" in terms:
            why = guard_problem(terms["guard"])
            if why is not None:
                raise ValueError(f"the policy's signed guard covers nothing: {why}")
        return terms

    @property
    def guarded(self) -> bool:
        """Whether the policy was signed with a guard (construction holds it to :func:`guard_problem`)."""
        return "guard" in self._terms

    @property
    def guard(self) -> tuple[dict[str, Any], ...]:
        """The guard the policy was signed with, as construction parsed it from :attr:`terms_text` (``()`` when it
        carries none): a fresh copy on every read, so no caller can change what another reads."""
        return tuple(copy.deepcopy(self._terms.get("guard") or ()))

    @classmethod
    def from_record(cls, record: Mapping[str, Any], *, incarnation: str, born: int, born_signer: str | None,
                    current_signer: str | None) -> "GraduationPolicy":
        """The policy a §3 record describes, or ``ValueError`` for a record that is not one, including a record
        whose ``guard`` :func:`guard_problem` refuses (it then covers nothing). ``born`` is the seq of the link that
        brought it in; its terms text is the record minus ``expires_at``, canonical. Nothing of ``record`` is kept by
        reference."""
        if not isinstance(record, Mapping):
            raise ValueError("a graduation policy is a mapping")
        names = set(record)
        if not _REQUIRED <= names:
            raise ValueError(f"a graduation policy needs {sorted(_REQUIRED - names)}")
        if names - _REQUIRED - _OPTIONAL:
            # by repr: a key that is not a string is named, never compared with one
            raise ValueError(f"a graduation policy carries no {sorted(names - _REQUIRED - _OPTIONAL, key=repr)}")
        if record["kind"] != "graduation":
            raise ValueError(f"kind {record['kind']!r} is not a graduation policy")
        gid = record["id"]
        if not isinstance(gid, str) or not gid:
            raise ValueError("a graduation policy's id is a non-empty string")
        if not isinstance(incarnation, str) or not incarnation:
            raise ValueError("a graduation policy's incarnation is a non-empty string")
        actions = record["actions"]
        if not isinstance(actions, Mapping) or not actions:
            raise ValueError("a graduation policy signs at least one action")
        signed: list[tuple[str, str]] = []
        for name, risk in actions.items():
            if not isinstance(name, str) or not name or not isinstance(risk, Mapping):
                raise ValueError("each signed action is a name and its risk dict")
            try:
                if not _str_keys(risk):
                    raise ValueError
                signed.append((name, _canonical(dict(risk))))
            except (TypeError, ValueError, RecursionError):
                raise ValueError(f"the risk dict of {name!r} is not JSON with string keys") from None
        lowest = record["lowest_posture"]
        if not isinstance(lowest, str) or lowest not in _LOWEST:
            raise ValueError(f"lowest_posture {lowest!r} is not one of {sorted(_LOWEST)}")
        expires = record["expires_at"]
        if not isinstance(expires, str) or _STAMP.fullmatch(expires) is None:
            raise ValueError(f"expires_at {str(expires)[:40]!r} is not in the one timestamp form")
        try:
            expires_at = datetime.strptime(expires, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"expires_at {expires!r} is not a date") from None
        store_id = record["store_id"]
        if not isinstance(store_id, str) or not store_id:
            raise ValueError(f"store_id {str(store_id)[:40]!r} is not a store's id")
        try:
            terms_text = _canonical({k: v for k, v in record.items() if k != "expires_at"})
        except (TypeError, ValueError, RecursionError):
            raise ValueError("a graduation policy's terms are not JSON") from None
        return cls(grant_id=gid, incarnation=incarnation, actions=tuple(sorted(signed)),
                   lowest_posture=_LOWEST[lowest], expires_at=expires_at,
                   triggers=_names(record.get("triggers"), "triggers"),
                   families=_names(record.get("families"), "families"),
                   born_signer=born_signer, current_signer=current_signer, store_id=store_id,
                   born=born, terms_text=terms_text)

    def signed_risk(self, action_name: str) -> str | None:
        """The canonical text of the risk dict this policy signs ``action_name`` at, or ``None``."""
        for name, risk in self.actions:
            if name == action_name:
                return risk
        return None

    @property
    def cover_value(self) -> str:
        """``<incarnation>@<expires_at>``: a renew (a later expiry) changes it, so a decided fence goes stale."""
        return f"{self.incarnation}@{_stamp(self.expires_at)}"


def _check_actions(actions: Any) -> None:
    # a non-empty tuple of (name, risk text) pairs, one per name, each text the canonical rendering of a JSON object
    # with string keys: what from_record builds, and the only text covered() can match
    if not isinstance(actions, tuple) or not actions:
        raise ValueError("a graduation policy signs at least one action, as a tuple of pairs")
    seen: set[str] = set()
    for pair in actions:
        if not (isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], str) and pair[0]
                and isinstance(pair[1], str)):
            raise ValueError("each signed action is a (name, risk text) pair")
        name, text = pair
        if name in seen:
            raise ValueError(f"the action {name!r} is signed twice")
        seen.add(name)
        try:
            risk = json.loads(text)
            ok = isinstance(risk, dict) and _canonical(risk) == text
        except (TypeError, ValueError, RecursionError):
            ok = False
        if not ok:
            raise ValueError(f"the risk text of {name!r} is not the canonical text of a risk dict")


def _names(value: Any, what: str) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{what} must be null or a non-empty list of names (got {value!r:.80})")
    return frozenset(value)


def covered(policy: GraduationPolicy, effect: Effect) -> bool:
    """Whether ``policy`` covers ``effect``: it signs the effect's action at the whole risk the gate's manifest gives
    it, and its trigger and family lists, where present, name the effect's. An action the manifest does not know is
    covered by nothing. Liveness and anchoring are :func:`live`'s."""
    if effect.action_risk is None:
        return False
    signed = policy.signed_risk(effect.action_name)
    if signed is None or signed != _canonical(risk_dict(effect.action_risk)):
        return False
    if policy.triggers is not None and effect.trigger_type not in policy.triggers:
        return False
    if policy.families is not None and effect.family not in policy.families:
        return False
    return True


def _key_of(public_key: str | None) -> str | None:
    # ``<type> <base64>``, a trailing comment dropped: keys compare by type and blob
    if not isinstance(public_key, str):
        return None
    parts = public_key.split()
    return f"{parts[0]} {parts[1]}" if len(parts) >= 2 else None


def anchored(policy: GraduationPolicy, root_keys: frozenset[str]) -> bool:
    """Whether both links that define ``policy`` now (the one that brought its incarnation in, and the one that last
    renewed it) are signed by keys in ``root_keys`` (:func:`authority_keys` of root's file).

    A key anchors whatever principal its root line names: unlike ``ssh-keygen -Y verify``, the principal is not
    matched. Root owns that file, so a line root wrote under another principal is still root's word for the key."""
    born, current = _key_of(policy.born_signer), _key_of(policy.current_signer)
    return born is not None and current is not None and born in root_keys and current in root_keys


def live(policy: GraduationPolicy, now: datetime, root_keys: frozenset[str]) -> bool:
    """Whether ``policy`` counts at ``now``: unexpired (``now < expires_at``), no further ahead than
    :data:`GRADUATION_GRANT_MAX`, :func:`anchored`, and signed with a guard (:attr:`GraduationPolicy.guarded`). That
    it is in the replayed policy is its source's to guarantee: a source hands over only those.

    A policy signed with no guard (a log from before the guard) counts for nothing (1026-20 RULINGS R13-4: an ON_LOOP
    policy lowers an ON_LOOP binding from CONFIRM to ON_LOOP, so it is a confirm-class grant and needs the guard
    np-6bd5f97e (a) requires); it covers again only once it is revoked and signed anew with one."""
    now = _aware(now)
    return (now < policy.expires_at <= now + GRADUATION_GRANT_MAX and anchored(policy, root_keys)
            and policy.guarded)


@dataclass(frozen=True)
class Cover:
    """What a view says of one effect: the strictest ``lowest_posture`` of the live anchored policies covering it
    (``None``: none does) and the fence's cover value (that policy's :attr:`GraduationPolicy.cover_value`, or
    :data:`COVER_NONE`). ``policy`` is the policy whose cover value it is: the one whose signed guard authorizes the
    effect. It takes no part in equality: within one view a cover value names one policy."""

    posture: Posture | None
    value: str
    policy: GraduationPolicy | None = field(default=None, compare=False)


_UNCOVERED = Cover(None, COVER_NONE)


def _is_cover(cover: Any) -> bool:
    # what a view answered is a Cover that keeps this module's invariants: no rung with exactly COVER_NONE, or a rung a
    # policy may sign with a value holding an "@" (so never COVER_NONE or COVER_MOVED). Anything else covers nothing
    if not isinstance(cover, Cover) or not isinstance(cover.value, str):
        return False
    if cover.posture is None:
        return cover.value == COVER_NONE
    return isinstance(cover.posture, Posture) and cover.posture in _LOWEST.values() and "@" in cover.value


@runtime_checkable
class CoverageView(Protocol):
    """A fixed coverage state: what one refresh produced. ``token`` names the state it was built from
    (``(head, signers_digest, store_id)``); :meth:`cover` is pure and re-runs expiry at its own ``now``."""

    @property
    def token(self) -> tuple[tuple[int, str], str, str]: ...

    def cover(self, effect: Effect, now: datetime) -> Cover: ...


@runtime_checkable
class CoverageSource(Protocol):
    """Where the gate gets coverage (DESIGN_REV3 §5.3, DESIGN_REV3_1 D2-D3). No method raises: any failure is
    uncovered.

    :meth:`refresh` runs outside any store transaction: it reads the root file's bytes once, replays the authority
    log, builds a :class:`CoverageSnapshot` from that replay's own head, those bytes and the store's own id (its
    ``meta`` row ``store_id``), caches it and returns it.
    A failed refresh caches and returns :class:`Uncovered` at the token a peek would read.

    :meth:`peek` reads the root file's digest and then the authority head (the last authority read), and returns
    :func:`peek_snapshot` of the cached view at the token of those two and the store's own id: the cover value, or
    :data:`COVER_MOVED`.

    :meth:`peek_cover` is the same read, answering :func:`peek_cover_snapshot`: the value with the rung and the
    policy that set it, all three from ONE answer of ONE cached view (no second read of the cache, no refresh), so
    the policy a fire takes its guard from is the one whose cover value the fence compares. Where several live
    policies cover an effect, the strictest rung wins, and at that rung the least cover value: that one policy
    authorizes the effect, and its guard is the one that applies."""

    def refresh(self) -> CoverageView: ...

    def peek(self, effect: Effect, now: datetime) -> str: ...

    def peek_cover(self, effect: Effect, now: datetime) -> "EffectiveCover": ...


def signers_digest(root_bytes: bytes) -> str:
    """The identity of root's allowed-signers file: a sha256 over its bytes."""
    return "sha256:" + hashlib.sha256(root_bytes).hexdigest()


def _check_head(head: Any) -> None:
    if not (isinstance(head, tuple) and len(head) == 2 and isinstance(head[0], int)
            and not isinstance(head[0], bool) and isinstance(head[1], str)):
        raise TypeError("head is (seq, head_hash)")


@dataclass(frozen=True)
class CoverageSnapshot:
    """One refresh's coverage: the replayed log's ``head``, the root file's bytes as read once, the replayed
    graduation policies, and the id of the store the gate runs in (only a policy signed for it covers). The signers
    digest and the parsed signers are derived from ``root_bytes`` here, and the ``token`` from those fields, so a
    snapshot cannot carry a token apart from the state it was built from."""

    head: tuple[int, str]
    root_bytes: bytes
    policies: tuple[GraduationPolicy, ...]
    store_id: str
    signers_digest: str = field(init=False)
    signers: tuple["RootSigner", ...] = field(init=False, repr=False)
    _root_keys: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _check_head(self.head)
        if not isinstance(self.root_bytes, bytes):
            raise TypeError("root_bytes is the root file's bytes")
        if not isinstance(self.policies, tuple) or not all(isinstance(p, GraduationPolicy) for p in self.policies):
            raise TypeError("policies is a tuple of GraduationPolicy")
        if not isinstance(self.store_id, str) or not self.store_id:
            raise TypeError("store_id is the current store's id")
        signers = parse_root_signers(self.root_bytes)
        object.__setattr__(self, "signers_digest", signers_digest(self.root_bytes))
        object.__setattr__(self, "signers", signers)
        object.__setattr__(self, "_root_keys", authority_keys(signers))

    @property
    def token(self) -> tuple[tuple[int, str], str, str]:
        return (self.head, self.signers_digest, self.store_id)

    def cover(self, effect: Effect, now: datetime) -> Cover:
        now = _aware(now)
        hits = [p for p in self.policies
                if p.store_id == self.store_id and covered(p, effect) and live(p, now, self._root_keys)]
        if not hits:
            return _UNCOVERED
        strictest = max(p.lowest_posture for p in hits)
        # ties at the strictest rung: one value whatever the policies' order, so a re-read gives the same fence
        chosen = min((p for p in hits if p.lowest_posture == strictest), key=lambda p: p.cover_value)
        return Cover(strictest, chosen.cover_value, chosen)


@dataclass(frozen=True)
class Uncovered:
    """What a failed refresh caches: nothing is covered at ``token``. Never stale in the loosening direction, so it
    needs no coherence between its fields."""

    head: tuple[int, str]
    signers_digest: str
    store_id: str

    def __post_init__(self) -> None:
        _check_head(self.head)
        if not isinstance(self.signers_digest, str):
            raise TypeError("signers_digest is a string")
        if not isinstance(self.store_id, str) or not self.store_id:
            raise TypeError("store_id is the current store's id")

    @property
    def token(self) -> tuple[tuple[int, str], str, str]:
        return (self.head, self.signers_digest, self.store_id)

    def cover(self, effect: Effect, now: datetime) -> Cover:
        _aware(now)
        return _UNCOVERED


def operative(candidate: Posture, effect: Effect, view: CoverageView | None,
              now: datetime) -> tuple[Posture, str | None]:
    """The rung ``effect`` runs at, and the fence's cover value (``None`` when the clamp was not consulted).

    A candidate at or above ``CONFIRM`` keeps its rung, with no cover value (the clamp is not consulted).
    Below ``CONFIRM``: uncovered (no view, a view that raises or answers anything but a :class:`Cover` keeping the
    module's invariants (``None`` with :data:`COVER_NONE`, or a rung a policy may sign with a value holding an
    "@"), no live anchored policy covering the effect) is ``CONFIRM``; covered is the stricter of the candidate and the strictest
    covering policy's ``lowest_posture``. On every path the floor of the stricter of the action's and the tools'
    risk can only raise the rung."""
    floor_risk = stricter_risk(effect.action_risk, effect.tools_risk)
    floor = Posture.ABOVE_LOOP if floor_risk is None else risk_floor(floor_risk)
    if candidate >= Posture.CONFIRM:
        return max(candidate, floor), None
    try:
        cover = _UNCOVERED if view is None else view.cover(effect, now)
        if not _is_cover(cover):
            cover = _UNCOVERED
    except Exception:  # noqa: BLE001 - a view that cannot answer covers nothing
        cover = _UNCOVERED
    rung = Posture.CONFIRM if cover.posture is None else max(candidate, cover.posture)
    return max(rung, floor), cover.value


@dataclass(frozen=True)
class EffectiveCover:
    """What one peek gives the fire check (:meth:`CoverageSource.peek_cover`): the cover ``value`` (a policy's cover
    value, :data:`COVER_NONE` or :data:`COVER_MOVED`), the rung it sets (``None`` unless covered) and the ``policy``
    whose cover value it is. ``policy`` is ``None`` when the value is not a covered one, and also when the view did
    not name a policy that gives exactly this value at this rung: what to do with a covered value no policy
    authorizes is the fire check's."""

    value: str
    posture: Posture | None
    policy: GraduationPolicy | None


_MOVED = EffectiveCover(COVER_MOVED, None, None)


def peek_cover_snapshot(view: CoverageView | None, token: tuple[tuple[int, str], str, str], effect: Effect,
                        now: datetime) -> EffectiveCover:
    """The pure half of :meth:`CoverageSource.peek_cover`: from ONE ``view.cover`` answer, the value, the rung and the
    policy ``view`` gives ``effect`` at ``now`` when ``token`` (the head and digest just read, and the current
    store's id) is the view's own, else :data:`COVER_MOVED`. A view that raises or answers anything but a
    :class:`Cover` keeping the module's invariants is :data:`COVER_MOVED` too: it equals no decided fence, so the
    effect is decided again."""
    if view is None:
        return _MOVED
    try:
        if view.token != token:
            return _MOVED
        cover = view.cover(effect, now)
    except Exception:  # noqa: BLE001 - a view that cannot answer forces a re-decide
        return _MOVED
    if not _is_cover(cover):
        return _MOVED
    policy = getattr(cover, "policy", None)
    if not (cover.posture is not None and isinstance(policy, GraduationPolicy)
            and policy.cover_value == cover.value and policy.lowest_posture == cover.posture):
        policy = None
    return EffectiveCover(cover.value, cover.posture, policy)


def peek_snapshot(view: CoverageView | None, token: tuple[tuple[int, str], str, str], effect: Effect,
                  now: datetime) -> str:
    """The pure half of a peek: the value :func:`peek_cover_snapshot` reads (one implementation, so the value a peek
    gives and the value a fire's policy was read with cannot differ)."""
    return peek_cover_snapshot(view, token, effect, now).value


@dataclass(frozen=True)
class RootSigner:
    """One listed line of root's allowed-signers file: its principals field, the namespaces its ``namespaces=``
    option names (``None`` when the line has no options), and the key as ``<type> <base64>``."""

    principals: str
    namespaces: frozenset[str] | None
    key: str


def _fields(line: str) -> list[str] | None:
    # split on whitespace outside double quotes; an unterminated quote is no line
    out: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in line:
        if ch == '"':
            quoted = not quoted
            cur.append(ch)
        elif ch.isspace() and not quoted:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if quoted:
        return None
    if cur:
        out.append("".join(cur))
    return out


def _options(text: str) -> list[str] | None:
    # split on commas outside double quotes
    out: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            cur.append(ch)
        elif ch == "," and not quoted:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return None if quoted or "" in out else out


def _namespaces(option: str) -> frozenset[str] | None:
    # ``namespaces="a,b"``: the name as ssh matches it (any case), the value quoted, no escapes
    name, sep, value = option.partition("=")
    if not sep or name.lower() != "namespaces":
        return None
    if len(value) < 2 or value[0] != '"' or value[-1] != '"' or '"' in value[1:-1] or "\\" in value:
        return None
    entries = value[1:-1].split(",")
    if not all(entries):
        return None
    return frozenset(entries)


def _line(text: str) -> RootSigner | None:
    parts = _fields(text)
    if parts is None or len(parts) < 3:   # principals, [options], type, blob
        return None
    principals = parts[0]
    if principals.startswith('"'):
        if len(principals) < 2 or not principals.endswith('"'):
            return None
        principals = principals[1:-1]
    if not principals or principals in KEY_TYPES:
        return None
    namespaces: frozenset[str] | None = None
    rest = parts[1:]
    if rest[0] not in KEY_TYPES:
        options = _options(rest[0])
        # exactly one option, and it is namespaces=: any other (cert-authority, valid-*, a second namespaces)
        # means the line lists nothing
        if options is None or len(options) != 1:
            return None
        namespaces = _namespaces(options[0])
        if namespaces is None:
            return None
        rest = rest[1:]
    if len(rest) < 2 or rest[0] not in KEY_TYPES or _BLOB.fullmatch(rest[1]) is None:
        return None
    return RootSigner(principals=principals, namespaces=namespaces, key=f"{rest[0]} {rest[1]}")


def parse_root_signers(root_bytes: bytes) -> tuple[RootSigner, ...]:
    """The listed lines of an allowed-signers file, in order. A line that is blank or a ``#`` comment, not UTF-8,
    malformed, of a key type the enrol door does not accept, or carrying any option but one ``namespaces=``, lists
    nothing; the other lines still count."""
    if not isinstance(root_bytes, bytes):
        raise TypeError("parse_root_signers reads the file's bytes")
    out: list[RootSigner] = []
    for raw in root_bytes.split(b"\n"):
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if not text or text.startswith("#"):
            continue
        signer = _line(text)
        if signer is not None:
            out.append(signer)
    return tuple(out)


def authority_keys(signers: Iterable[RootSigner]) -> frozenset[str]:
    """The keys root lists for the authority log: lines whose ``namespaces=`` names :data:`AUTHORITY_NAMESPACE`
    literally. A line with any pattern entry that could negate (``!``) lists nothing; a wildcard alone does not
    name the namespace."""
    keys: set[str] = set()
    for s in signers:
        if s.namespaces is None or any(n.startswith("!") for n in s.namespaces):
            continue
        if AUTHORITY_NAMESPACE in s.namespaces:
            keys.add(s.key)
    return frozenset(keys)
