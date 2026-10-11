"""levain.cockpit.labels — the label a row's provenance carries at render, read from the journal
(K2b-3b design §0.1-§0.3, §2, §8d).

A label is a pure function of (stored value, journal, install class, keys): per authority field, the
latest ``labels`` row in the current epoch whose ``value_sha256`` equals the sha256 of the stored
value's JCS. Its class comes from :func:`label_class` over the JOINED pending's
``decision_kind`` and the JOURNAL's ``meta.install_class``, lowered to ``signed by a revoked key`` when
the signer's ``compromised_since <= fired_at``. A row's label is its weakest field's.

:class:`BrokerLabeller` reads through :meth:`CockpitDB.read` only, in ONE transaction per snapshot, and
exists only where the broker is ACTIVE (§8d row 1). Its index is memoised on the journal revision
``(install_class, epoch, label_rev)``, which triggers move (§0.3); a memo is replaced, never mutated, so
a request holding an old snapshot keeps a consistent one.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from levain.autonomic.journal import JournalCorruptError
from levain.cockpit.authority import LabelKey, unavailable
from levain.cockpit.install import GOVERNED, REPORTED
from levain.cockpit.consent import ConsentError, parse_stamp, value_sha256
from levain.cockpit.firejournal import CockpitDB, _parse_record

__all__ = ["BrokerLabeller", "LABEL_ORDER", "LabelKey", "LabelSnapshot", "UNAVAILABLE_SNAPSHOT", "label_class"]

# strongest first; a row's label is the WEAKEST field label (the highest index)
LABEL_ORDER = ("verified", "signed · reported", "claims cockpit token", "signed by a revoked key", "unverified")
UNVERIFIED = LABEL_ORDER[-1]
REVOKED = "signed by a revoked key"
_RANK = {label: i for i, label in enumerate(LABEL_ORDER)}


@dataclass(frozen=True)
class _Fire:
    """One label row that survived its checks, with its authority facts from the joined pending."""
    operation_id: str
    value_sha256: str
    label: str
    fired_at: str
    fired: datetime
    rowid: int
    signer: dict[str, Any] | None
    credential: str | None


_NO_FIELD = {"label": UNVERIFIED, "signer": None, "fired_at": None, "operation_id": None}


@dataclass(frozen=True)
class LabelSnapshot:
    """One read of the journal's labels. ``revision`` is ``(install_class or "", epoch, label_rev)``,
    None with ``error`` set or when no labeller is attached (``error`` None too: provenance is null
    everywhere, today's output). ``degraded`` counts label rows dropped for corruption; each degrades
    only its own resource to unverified (§8d row 3)."""
    revision: tuple[str, int, int] | None
    error: str | None
    degraded: int = 0
    _index: Mapping[tuple[str, str], tuple[_Fire, ...]] = field(default_factory=dict, repr=False,
                                                               compare=False)
    _degraded_keys: frozenset[str] = field(default=frozenset(), repr=False, compare=False)

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None:
        """flow §3.2 ``Provenance`` for ``key``; None for a None key, or when no labeller is
        attached. Never stronger than the journal proves: an error, a failed gate, a degraded resource
        or a value with no matching fire is ``unverified`` with nulls."""
        if key is None or (self.revision is None and self.error is None):
            return None
        blind = self.error is not None or (key.gate is not None and key.gate != "match") \
            or key.resource_key in self._degraded_keys
        fires: dict[str, _Fire | None] = {
            f: None if blind else self._fire(key.resource_key, f, v, key.operation_id)
            for f, v in key.values.items()}
        fields = {f: dict(_NO_FIELD) if fire is None else {
            "label": fire.label, "signer": fire.signer, "fired_at": fire.fired_at,
            "operation_id": fire.operation_id} for f, fire in fires.items()}
        weakest = max((fields[f]["label"] for f in fields), key=_RANK.__getitem__, default=UNVERIFIED)
        # the row's facts are the weakest field's fire, the OLDEST on a tie (flow §4.5)
        at_weakest = [fire for f, fire in fires.items() if fire is not None and fire.label == weakest]
        row_fire = min(at_weakest, key=lambda x: (x.fired, x.rowid), default=None)
        return {"label": weakest,
                "signer": None if row_fire is None else row_fire.signer,
                "credential": None if row_fire is None else row_fire.credential,
                "fired_at": None if row_fire is None else row_fire.fired_at,
                "operation_id": None if row_fire is None else row_fire.operation_id,
                "fields": fields}

    def label(self, key: LabelKey | None) -> str:
        prov = self.provenance(key)
        return UNVERIFIED if prov is None else prov["label"]

    def _fire(self, resource_key: str, name: str, value: Any, operation_id: str | None) -> _Fire | None:
        try:
            digest = value_sha256(value)
        except (ConsentError, TypeError, ValueError, RecursionError):
            return None                 # a value outside the JCS domain was never what a fire signed
        cands = [c for c in self._index.get((resource_key, name), ())
                 if c.value_sha256 == digest
                 and (operation_id is None or c.operation_id == operation_id)]
        return max(cands, key=lambda c: (c.fired, c.rowid), default=None)


def label_class(decision_kind: str | None, install_class: str | None) -> str:
    """A fire's label class (§5) before revocation: ALLOWED_REPORTED -> ``claims cockpit token``;
    ALLOWED in a reported install -> ``signed · reported``; ALLOWED governed -> ``verified``."""
    if decision_kind == "ALLOWED_REPORTED":
        return "claims cockpit token"
    if decision_kind == "ALLOWED":
        if install_class == REPORTED:
            return "signed · reported"
        # verified only in a governed journal: an empty or unknown class fails closed
        return "verified" if install_class == GOVERNED else "unverified"
    return "unverified"


# the ONE "provenance unavailable" snapshot: the engine answers the same for a labeller that raises
UNAVAILABLE_SNAPSHOT = unavailable


_ROWS = (
    "SELECT l.rowid AS l_rowid, l.resource_key, l.field, l.operation_id, l.value_sha256, "
    "l.decision_kind AS l_kind, l.signer_fp AS l_fp, l.credential AS l_cred, l.fired_at, "
    "p.rowid AS p_rowid, p.pending_id, p.record, p.record_sha256, p.decision_kind AS p_kind, "
    "p.signer_fp AS p_fp, p.credential AS p_cred, p.decided_at AS p_decided, "
    "k.principal AS k_principal, k.enrolled_at AS k_enrolled, k.compromised_since AS k_compromised "
    "FROM labels l LEFT JOIN pendings p ON p.pending_id = l.operation_id "
    "LEFT JOIN keys k ON k.fingerprint = p.signer_fp WHERE l.epoch = ?")


class BrokerLabeller:
    """The broker's labeller: :meth:`snapshot` never raises."""

    def __init__(self, db: CockpitDB) -> None:
        self.db = db
        self._memo: LabelSnapshot | None = None

    def snapshot(self) -> LabelSnapshot:
        try:
            with self.db.read() as conn:
                meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN "
                                         "('install_class', 'epoch', 'label_rev')").fetchall())
                if "epoch" not in meta or "label_rev" not in meta:
                    return UNAVAILABLE_SNAPSHOT("the journal has no epoch or label_rev")
                install_class = meta.get("install_class") or ""
                revision = (install_class, int(meta["epoch"]), int(meta["label_rev"]))
                memo = self._memo
                if memo is not None and memo.revision == revision:
                    return memo
                snap = _build(conn, revision)
        except Exception as exc:        # a label is never a gate on reading: any fault is "unavailable"
            return UNAVAILABLE_SNAPSHOT(f"{type(exc).__name__}: {str(exc)[:200]}")
        self._memo = snap
        return snap


def _build(conn: sqlite3.Connection, revision: tuple[str, int, int]) -> LabelSnapshot:
    install_class, epoch, _ = revision
    index: dict[tuple[str, str], list[_Fire]] = {}
    degraded_keys: set[str] = set()
    degraded = 0
    records_ok: dict[str, bool] = {}
    for r in conn.execute(_ROWS, (epoch,)).fetchall():
        op = r["operation_id"]
        fire = None
        if r["pending_id"] is not None:
            if op not in records_ok:
                try:
                    _parse_record(op, r["record"], r["record_sha256"])
                    records_ok[op] = True
                except JournalCorruptError:
                    records_ok[op] = False
            if records_ok[op]:
                fire = _fire_of(r, install_class)
        if fire is None:
            degraded += 1
            degraded_keys.add(r["resource_key"])
            continue
        index.setdefault((r["resource_key"], r["field"]), []).append(fire)
    return LabelSnapshot(revision=revision, error=None, degraded=degraded,
                         _index={k: tuple(v) for k, v in index.items()},
                         _degraded_keys=frozenset(degraded_keys))


def _fire_of(r: sqlite3.Row, install_class: str) -> _Fire | None:
    """The label row's fire, its authority facts from the JOINED pending; None when a copy disagrees
    with the pending or a stamp does not parse (the row is dropped, its resource degraded)."""
    kind = r["p_kind"]
    if kind not in ("ALLOWED", "ALLOWED_REPORTED") or r["l_kind"] != kind or r["l_fp"] != r["p_fp"] \
            or r["l_cred"] != r["p_cred"] or r["fired_at"] != r["p_decided"]:
        return None
    if kind == "ALLOWED" and r["p_fp"] is None:
        return None
    try:
        fired = parse_stamp(r["fired_at"])
        label = label_class(kind, install_class)
        signer = None
        if kind == "ALLOWED":
            signer = {"name": r["k_principal"], "fingerprint": r["p_fp"], "enrolled_at": r["k_enrolled"]}
            if r["k_compromised"] is not None and parse_stamp(r["k_compromised"]) <= fired:
                label = REVOKED
    except ConsentError:
        return None
    return _Fire(operation_id=r["operation_id"], value_sha256=r["value_sha256"], label=label,
                 fired_at=r["fired_at"], fired=fired, rowid=r["p_rowid"], signer=signer,
                 credential=r["p_cred"] if kind == "ALLOWED_REPORTED" else None)
