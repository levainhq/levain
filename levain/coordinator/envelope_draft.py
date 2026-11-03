"""levain.coordinator.envelope_draft — the signed draft envelope's record, digest and shapes (H1-1), pure.

A DRAFT is one attempt to get one envelope signed for one thread: a SERIES (``ed-`` + 32 hex) of REVISIONS 1..N, each
a proposal (an envelope body without its ``draft`` key, whose editable values may still be ``null``) plus the records
it cites. A signature over the revision's body covers the draft's id, revision and digest (the body's ``draft`` key),
so one signature commits the exact digest he saw: a revision written after the render is another digest, and the store
refuses a signature over the old one (:mod:`levain.coordinator.envelope_drafts`, :meth:`EnvelopeStore.accept_envelope`).

This module holds what needs no store: the two tables and their triggers (:data:`ENVELOPE_DRAFT_SCHEMA`), the shape
checks of a proposal and its cites, the digest (:func:`draft_digest`) and the two diffs a signer is shown. A cite is a
CLAIM OF RELEVANCE (the drafter names a record for a field), never proof the record supports it; and the writer
principal (``by``) is neither signed nor shown, because before S1 nothing authenticates it. Stdlib only.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.contracts.canon import canonical, canonical_bytes, parse
from levain.coordinator import envelope as env
from levain.coordinator.store import _append_only

__all__ = [
    "CITES_MAX", "CITE_KINDS", "DRAFT_FORMAT", "DraftError", "EDITABLE", "ENVELOPE_DRAFT_SCHEMA", "MIN_LEFT",
    "against_diff", "check_cites", "check_fields", "check_proposal", "draft_body", "draft_digest", "empty_base",
    "field_diff", "is_complete", "load_cites", "load_proposal",
]

DRAFT_FORMAT = "levain-envelope-draft/2"
EDITABLE = ("where", "lands", "pushes", "publishes", "budget", "stop_at")
CITE_KINDS = ("drop", "envelope")
CITES_MAX = 64
# an envelope with less than this left to run is not worth a signature: the sign verb and the draft's born-open rule
# share the one number
MIN_LEFT = timedelta(minutes=5)

_CITE_KEYS = {"field", "kind", "ref", "digest"}
_REFS = {"drop": re.compile(r"dr-[0-9a-f]{32}"), "envelope": re.compile(r"ev-[0-9a-f]{32}")}
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_PROPOSAL_KEYS = frozenset(env.BODY_KEYS - {"draft"})


class DraftError(ValueError):
    """A draft value, proposal or cite list that is not what a revision holds; the message is the line shown."""


ENVELOPE_DRAFT_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS envelope_drafts (draft_id TEXT NOT NULL, "
    f"revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND {env.DRAFT_REVISIONS_MAX}), "
    "thread_id TEXT NOT NULL REFERENCES threads(thread_id), gseq INTEGER NOT NULL UNIQUE, parent TEXT, "
    "proposal TEXT NOT NULL, cites TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, "
    "by TEXT NOT NULL CHECK (by IN ('operator', 'coordinator')), at TEXT NOT NULL, "
    "PRIMARY KEY (draft_id, revision), CHECK ((revision = 1) = (parent IS NULL)))",
    "CREATE TABLE IF NOT EXISTS envelope_draft_withdrawals (draft_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
    "gseq INTEGER NOT NULL UNIQUE, by TEXT NOT NULL CHECK (by = 'operator'), at TEXT NOT NULL, "
    "FOREIGN KEY (draft_id, revision) REFERENCES envelope_drafts(draft_id, revision))",
    *_append_only("envelope_drafts", "a draft revision"),
    *_append_only("envelope_draft_withdrawals", "a draft withdrawal"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    "CREATE TRIGGER IF NOT EXISTS envelope_drafts_no_replace BEFORE INSERT ON envelope_drafts WHEN EXISTS (SELECT 1 "
    "FROM envelope_drafts WHERE (draft_id = NEW.draft_id AND revision = NEW.revision) OR gseq = NEW.gseq OR "
    "digest = NEW.digest OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a draft revision is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS envelope_draft_withdrawals_no_replace BEFORE INSERT ON envelope_draft_withdrawals "
    "WHEN EXISTS (SELECT 1 FROM envelope_draft_withdrawals WHERE draft_id = NEW.draft_id OR gseq = NEW.gseq OR "
    "rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a draft withdrawal is never replaced'); END",
    # the CAS, in the database: a revision follows its parent, on its series' thread
    "CREATE TRIGGER IF NOT EXISTS envelope_drafts_next BEFORE INSERT ON envelope_drafts WHEN NEW.revision != "
    "COALESCE((SELECT MAX(revision) FROM envelope_drafts WHERE draft_id = NEW.draft_id), 0) + 1 OR (NEW.revision > 1 "
    "AND (NEW.parent IS NOT (SELECT digest FROM envelope_drafts WHERE draft_id = NEW.draft_id AND revision = "
    "NEW.revision - 1) OR NEW.thread_id IS NOT (SELECT thread_id FROM envelope_drafts WHERE draft_id = NEW.draft_id "
    "AND revision = 1))) BEGIN SELECT RAISE(ABORT, 'the draft moved: a revision follows its latest parent'); END",
    # signed and withdrawn never revert: neither takes another revision
    "CREATE TRIGGER IF NOT EXISTS envelope_drafts_closed BEFORE INSERT ON envelope_drafts WHEN EXISTS (SELECT 1 FROM "
    "envelope_draft_withdrawals WHERE draft_id = NEW.draft_id) OR EXISTS (SELECT 1 FROM envelopes WHERE "
    "json_extract(body, '$.draft.draft_id') = NEW.draft_id) BEGIN SELECT RAISE(ABORT, 'a signed or withdrawn draft "
    "takes no revision'); END",
    "CREATE TRIGGER IF NOT EXISTS envelope_drafts_one_unclosed BEFORE INSERT ON envelope_drafts WHEN NEW.revision = 1 "
    "AND EXISTS (SELECT 1 FROM envelope_drafts d WHERE d.thread_id = NEW.thread_id AND d.revision = 1 AND "
    "d.draft_id <> NEW.draft_id AND NOT EXISTS (SELECT 1 FROM envelope_draft_withdrawals w WHERE w.draft_id = "
    "d.draft_id) AND NOT EXISTS (SELECT 1 FROM envelopes e WHERE json_extract(e.body, '$.draft.draft_id') = "
    "d.draft_id)) BEGIN SELECT RAISE(ABORT, 'a thread holds one unclosed draft series: revise it or withdraw it'); "
    "END",
    "CREATE TRIGGER IF NOT EXISTS envelope_draft_withdrawals_open BEFORE INSERT ON envelope_draft_withdrawals WHEN "
    "NEW.revision IS NOT (SELECT MAX(revision) FROM envelope_drafts WHERE draft_id = NEW.draft_id) OR EXISTS "
    "(SELECT 1 FROM envelopes WHERE json_extract(body, '$.draft.draft_id') = NEW.draft_id) BEGIN SELECT "
    "RAISE(ABORT, 'only the latest revision of an unsigned draft is withdrawn'); END",
)


# --- shapes -----------------------------------------------------------------------------------------------------
def check_fields(fields: Any) -> dict[str, Any]:
    """The editable values a writer passes, every key of :data:`EDITABLE` present (``None`` = no input holds it), a
    ``budget`` a dict whose keys are each a budget key or ``None``. Shape of the container only: a value's own shape
    is :func:`check_proposal`'s."""
    if not isinstance(fields, Mapping) or not set(fields) <= set(EDITABLE):
        raise DraftError(f"the editable values are among {list(EDITABLE)}")
    out = {k: fields.get(k) for k in EDITABLE}
    b = out["budget"]
    if b is not None:
        if not isinstance(b, Mapping) or set(b) != set(env.BUDGET_KEYS):
            raise DraftError(f"budget names every one of {list(env.BUDGET_KEYS)}, a count or null each")
        out["budget"] = {k: b[k] for k in env.BUDGET_KEYS}
    return out


def is_complete(proposal: Mapping[str, Any]) -> bool:
    """True when no editable value, and no budget key, is still ``None``."""
    if any(proposal[k] is None for k in EDITABLE):
        return False
    return all(proposal["budget"][k] is not None for k in env.BUDGET_KEYS)


def _filled(p: Mapping[str, Any]) -> dict[str, Any]:
    """``p`` with every ``None`` replaced by the smallest value that has the shape, so :func:`env.parse_body` can check
    what IS named. A null is not checked (it names nothing); the whole body is checked again, unfilled, before any
    signature."""
    q = dict(p)
    for kind in ("lands", "pushes", "publishes"):
        if q[kind] is None:
            q[kind] = []
    if q["where"] is None:
        repos = [e["repo"] for kind in ("lands", "pushes") if isinstance(q[kind], list) for e in q[kind]
                 if isinstance(e, dict) and isinstance(e.get("repo"), str)]
        q["where"] = [{"repo": r, "paths": ["."]} for r in dict.fromkeys(repos)] or \
            [{"repo": "/placeholder", "paths": ["."]}]
    b = q["budget"]
    q["budget"] = {k: 0 for k in env.BUDGET_KEYS} if b is None else {k: (0 if b.get(k) is None else b[k])
                                                                      for k in env.BUDGET_KEYS}
    if q["stop_at"] is None:
        try:
            q["stop_at"] = utc_stamp(parse_stamp(q["issued_at"]) + timedelta(seconds=1))
        except (ConsentError, TypeError):
            raise DraftError("issued_at is a UTC timestamp") from None
    return q


def check_proposal(proposal: Any) -> dict[str, Any]:
    """A proposal held to its shape: exactly the body's keys minus ``draft``, each editable value ``None`` or a valid
    one (the body's own checks, with each ``None`` filled by a placeholder), the other keys never ``None``, and the
    stop within :data:`levain.dispatch.contract.MAX_LIFETIME` of the issue time. Raises :class:`DraftError`."""
    if not isinstance(proposal, dict) or set(proposal) != _PROPOSAL_KEYS:
        raise DraftError(f"a proposal has exactly the keys {sorted(_PROPOSAL_KEYS)}")
    if any(proposal[k] is None for k in _PROPOSAL_KEYS - set(EDITABLE) - {"supersedes"}):
        raise DraftError("only the editable values, and supersedes, may be null")
    if proposal["format"] != env.BODY_FORMAT or proposal["origin"] != "worded":
        raise DraftError(f"a draft proposal is a worded {env.BODY_FORMAT} envelope")
    if len(proposal["binds"]) != 1:
        raise DraftError("a draft binds exactly one thread")
    check_fields({k: proposal[k] for k in EDITABLE})
    ref = {"draft_id": "ed-" + "0" * 32, "revision": 1, "digest": "sha256:" + "0" * 64}
    try:
        body = env.parse_body(canonical({**_filled(proposal), "draft": ref}))
    except env.EnvelopeError as exc:
        raise DraftError(str(exc)) from None
    except (ValueError, TypeError, KeyError) as exc:
        raise DraftError(f"a proposal this reader cannot read: {type(exc).__name__}") from None
    from levain.dispatch.contract import MAX_LIFETIME

    if proposal["stop_at"] is not None and \
            parse_stamp(body["stop_at"]) - parse_stamp(body["issued_at"]) > MAX_LIFETIME:
        raise DraftError(f"an envelope stops at most {int(MAX_LIFETIME.total_seconds() // 3600)} hours after it is "
                         "issued")
    return proposal


def check_cites(cites: Any, proposal: Mapping[str, Any]) -> list[dict[str, str]]:
    """A cite list held to its shape: 0 to :data:`CITES_MAX` objects with exactly ``field``, ``kind``, ``ref``,
    ``digest``; ``field`` editable, ``kind`` one of :data:`CITE_KINDS`, ``ref`` that kind's id; sorted by
    ``(field, kind, ref)`` with no duplicate triple; and every named editable value cited at least once. The store
    checks each record against itself (kind by kind, at write and at sign)."""
    if not isinstance(cites, list) or len(cites) > CITES_MAX:
        raise DraftError(f"cites are a list of 0 to {CITES_MAX} entries")
    for c in cites:
        if not isinstance(c, dict) or set(c) != _CITE_KEYS:
            raise DraftError(f"a cite has exactly the keys {sorted(_CITE_KEYS)}")
        if c["field"] not in EDITABLE:
            raise DraftError(f"a cite's field is one of {list(EDITABLE)}")
        if c["kind"] not in CITE_KINDS:
            raise DraftError(f"a cite's kind is one of {list(CITE_KINDS)}")
        if not isinstance(c["ref"], str) or _REFS[c["kind"]].fullmatch(c["ref"]) is None:
            raise DraftError(f"a {c['kind']} cite's ref is that record's id")
        if not isinstance(c["digest"], str) or _SHA.fullmatch(c["digest"]) is None:
            raise DraftError("a cite's digest is sha256: and 64 hex")
    keys = [(c["field"], c["kind"], c["ref"]) for c in cites]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise DraftError("cites are sorted by (field, kind, ref), each triple once")
    cited = {c["field"] for c in cites}
    for k in EDITABLE:
        named = proposal[k] is not None and (k != "budget" or any(v is not None for v in proposal[k].values()))
        if named and k not in cited:
            raise DraftError(f"{k} is named with no record cited for it")
    return cites


def load_proposal(text: Any) -> dict[str, Any]:
    """A stored proposal, read back: refused unless ``text`` is its own canonical encoding and a valid proposal."""
    try:
        got = parse(text.encode("utf-8")) if isinstance(text, str) else None
    except (ValueError, UnicodeEncodeError):
        got = None
    if not isinstance(got, dict) or canonical(got) != text:
        raise DraftError("a stored proposal is its own canonical encoding")
    return check_proposal(got)


def load_cites(text: Any, proposal: Mapping[str, Any]) -> list[dict[str, str]]:
    """A stored cite list, read back: its own canonical encoding, sorted, valid."""
    try:
        got = parse(text.encode("utf-8")) if isinstance(text, str) else None
    except (ValueError, UnicodeEncodeError):
        got = None
    if got is None or canonical(got) != text:
        raise DraftError("stored cites are their own canonical encoding")
    return check_cites(got, proposal)


# --- the digest -------------------------------------------------------------------------------------------------
def draft_digest(store_id: str, draft_id: str, thread_id: str, revision: int, parent: str | None,
                 proposal: Mapping[str, Any], cites: Sequence[Mapping[str, str]]) -> str:
    """``sha256:<hex>`` over the format string, a NUL and the canonical bytes of the revision's record: the id of the
    store, the series, the thread, the revision and its parent's digest, the whole proposal (nulls and ``seen``
    included) and every cite's field, kind, id and digest. Left out on purpose: ``at``, ``gseq`` (bookkeeping), ``by``
    (unauthenticated before S1) and every label and diff (each is derived from signed bytes and append-only rows).
    The preimage starts with the format string, which no envelope, member or effect digest preimage can start with."""
    record = {"format": DRAFT_FORMAT, "store_id": store_id, "draft_id": draft_id, "thread_id": thread_id,
              "revision": revision, "parent": parent, "proposal": dict(proposal),
              "cites": [dict(c) for c in cites]}
    return "sha256:" + hashlib.sha256(DRAFT_FORMAT.encode("ascii") + b"\x00" + canonical_bytes(record)).hexdigest()


def draft_body(proposal: Mapping[str, Any], draft_id: str, revision: int, digest: str) -> dict[str, Any]:
    """The envelope body a signature covers: the proposal and the ``draft`` reference to its revision."""
    return {**proposal, "draft": {"draft_id": draft_id, "revision": revision, "digest": digest}}


# --- the two diffs a signer is shown ----------------------------------------------------------------------------
def empty_base(body: Mapping[str, Any]) -> dict[str, Any]:
    """What a head that grants nothing is diffed as: no where, no effect, a stop at its issue time (the same envelope
    a kill leaves, :meth:`levain.coordinator.envelopes.EnvelopeStore._effective`), so every value of a successor is
    new."""
    return {**body, "where": [], **{k: [] for k in env.EFFECT_LISTS}, "stop_at": body["issued_at"]}


def field_diff(prev: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per editable field, ``{"field", "before", "after"}`` where this revision's value differs from its parent's
    (``prev`` None: a first revision, every named field is new)."""
    out = []
    for k in EDITABLE:
        before = None if prev is None else prev[k]
        if before != new[k]:
            out.append({"field": k, "before": before, "after": new[k]})
    return out


def against_diff(base: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    """A complete proposal against ``base`` (a body-shaped dict: an effective envelope, or :func:`empty_base`): the
    paths and effects added and removed, each effect that keeps its target with other criteria as changed, the budget
    keys and the stop that differ."""
    where: dict[str, list[str]] = {"added": [], "removed": []}
    have = {w["repo"]: tuple(w["paths"]) for w in base["where"]}
    want = {w["repo"]: tuple(w["paths"]) for w in proposal["where"]}
    for repo, paths in want.items():
        where["added"] += [f"{repo}  {p}" for p in paths if not env.path_within(p, have.get(repo, ()))]
    for repo, paths in have.items():
        where["removed"] += [f"{repo}  {p}" for p in paths if not env.path_within(p, want.get(repo, ()))]
    effects: dict[str, dict[str, list[str]]] = {}
    for kind in env.EFFECT_LISTS:
        before = {env.effect_key(kind, e): e for e in base[kind]}
        after = {env.effect_key(kind, e): e for e in proposal[kind]}
        effects[kind] = {
            "added": [env.effect_identity(kind, e) for key, e in after.items() if key not in before],
            "removed": [env.effect_identity(kind, e) for key, e in before.items() if key not in after],
            "changed": [env.effect_identity(kind, e) for key, e in after.items()
                        if key in before and before[key] != e]}
    return {"where": where, "effects": effects,
            "budget": {k: (base["budget"][k], proposal["budget"][k]) for k in env.BUDGET_KEYS
                       if base["budget"][k] != proposal["budget"][k]},
            "stop_at": None if base["stop_at"] == proposal["stop_at"] else (base["stop_at"], proposal["stop_at"])}
