"""levain.autonomic.coverage — whether a signed graduation policy covers the effect the gate will run.

A binding effect may run below ``CONFIRM`` without a per-action signature only under a graduation policy: an
envelope in the authority log that signs ``actions`` (each at its whole risk dict), a ``lowest_posture`` and an
``expires_at``, and whose every defining link is signed by a key root's allowed-signers file lists in namespace
``levain-authority`` (14b DESIGN_REV3 §3-§4). This module is that rule as pure functions over plain values:

- :class:`Effect` is the gate's own value: the action the executor will run, the risk the gate's frozen manifest
  gives it (``None`` when the manifest does not know it), the sealed tools risk, and the honest-config trigger and
  family.
- :func:`covered` (the policy signs this action at this risk, and its trigger and family lists admit it),
  :func:`anchored` (both signers root-listed), :func:`live` (anchored, unexpired, inside the cap at ``now``).
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

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from levain.autonomic.policy import risk_floor
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk, RiskClass

__all__ = [
    "AUTHORITY_NAMESPACE", "COVER_MOVED", "COVER_NONE", "GRADUATION_GRANT_MAX", "KEY_TYPES",
    "Cover", "CoverageSnapshot", "CoverageSource", "CoverageView", "Effect", "GraduationPolicy", "RootSigner",
    "Uncovered", "anchored", "authority_keys", "covered", "live", "operative", "parse_root_signers",
    "peek_snapshot", "risk_dict", "signers_digest", "stricter_risk",
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

# The record's fields (DESIGN_REV3 §3): those a policy must carry, and those it may.
_REQUIRED = frozenset({"id", "kind", "actions", "lowest_posture", "expires_at", "store_id"})
_OPTIONAL = frozenset({"triggers", "families", "min_clean_fires"})

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
    covers in no other. Build it with :meth:`from_record`."""

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

    @classmethod
    def from_record(cls, record: Mapping[str, Any], *, incarnation: str, born_signer: str | None,
                    current_signer: str | None) -> "GraduationPolicy":
        """The policy a §3 record describes, or ``ValueError`` for a record that is not one. Nothing of ``record``
        is kept by reference."""
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
        return cls(grant_id=gid, incarnation=incarnation, actions=tuple(sorted(signed)),
                   lowest_posture=_LOWEST[lowest], expires_at=expires_at,
                   triggers=_names(record.get("triggers"), "triggers"),
                   families=_names(record.get("families"), "families"),
                   born_signer=born_signer, current_signer=current_signer, store_id=store_id)

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
    :data:`GRADUATION_GRANT_MAX`, and :func:`anchored`. That it is in the replayed policy is its source's to
    guarantee: a source hands over only those."""
    now = _aware(now)
    return now < policy.expires_at <= now + GRADUATION_GRANT_MAX and anchored(policy, root_keys)


@dataclass(frozen=True)
class Cover:
    """What a view says of one effect: the strictest ``lowest_posture`` of the live anchored policies covering it
    (``None``: none does) and the fence's cover value (that policy's :attr:`GraduationPolicy.cover_value`, or
    :data:`COVER_NONE`)."""

    posture: Posture | None
    value: str


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
    """Where the gate gets coverage (DESIGN_REV3 §5.3, DESIGN_REV3_1 D2-D3). Neither method raises: any failure is
    uncovered.

    :meth:`refresh` runs outside any store transaction: it reads the root file's bytes once, replays the authority
    log, builds a :class:`CoverageSnapshot` from that replay's own head, those bytes and the store's own id (its
    ``meta`` row ``store_id``), caches it and returns it.
    A failed refresh caches and returns :class:`Uncovered` at the token a peek would read.

    :meth:`peek` reads the root file's digest and then the authority head (the last authority read), and returns
    :func:`peek_snapshot` of the cached view at the token of those two and the store's own id: the cover value, or
    :data:`COVER_MOVED`."""

    def refresh(self) -> CoverageView: ...

    def peek(self, effect: Effect, now: datetime) -> str: ...


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
        return Cover(strictest, min(p.cover_value for p in hits if p.lowest_posture == strictest))


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


def peek_snapshot(view: CoverageView | None, token: tuple[tuple[int, str], str, str], effect: Effect,
                  now: datetime) -> str:
    """The pure half of a peek: the cover value ``view`` gives ``effect`` at ``now`` when ``token`` (the head and
    digest just read, and the current store's id) is the view's own, else :data:`COVER_MOVED`. A view that raises or
    answers anything but a :class:`Cover` keeping the module's invariants is :data:`COVER_MOVED` too: it equals no
    decided fence, so the effect is decided again."""
    if view is None:
        return COVER_MOVED
    try:
        if view.token != token:
            return COVER_MOVED
        cover = view.cover(effect, now)
    except Exception:  # noqa: BLE001 - a view that cannot answer forces a re-decide
        return COVER_MOVED
    return cover.value if _is_cover(cover) else COVER_MOVED


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
