"""levain.desk.board_state — the board stores ITEMS and DERIVES their state (READY_SCHEMA v2).

An item is stored (what was asked, in whose words, what closes it, what it waits on, where it came from) and its
state never is. State is computed on every read by one pure function from evidence the machine already keeps:
commits on origin/main and in the landing chain that carry the item's id in a trailer, branches, live seats, open
band rows, land waiters, and the derived state of the items it depends on. When evidence is missing, stale or
contradicts itself the item reads ``unknown`` with the reason; it never falls back to a default column.

Three entry points, all pure (no file, no clock, no environment: ``now`` is a parameter):

* :func:`fold` reads the append-only item records (one JSON object per line) into the current items. A line that
  cannot be trusted marks the whole file torn, so every item reads unknown; a clean record that merely names an
  unknown id is reported in ``problems`` only.
* :func:`derive_state` is one item's state. Checks that give ``unknown`` run first (an unreadable or stale source, a
  dependency cycle, a link that names nothing, an expired gate, evidence that contradicts itself), then the closed
  rule table :data:`_RULES` runs in order and the first rule that fires wins.
* :func:`derive_all` is the whole :class:`Board`, with the audit list the pace gate and the desk fail closed on.

The state, column and audit-kind sets each have ONE definition here (:data:`STATE_COLUMN`, :data:`AUDIT_KINDS`):
the emit sites and the schema document test read those, so the documented set cannot drift from the emitted one.
Stdlib only, and no ``levain`` import, so the module can be copied or installed anywhere and a conformance run needs
nothing else.
"""
from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date as _date, datetime, timezone
from functools import cached_property
from typing import Any, Callable

__all__ = [
    "AUDIT_KINDS", "AuditEntry", "Amendment", "Board", "COLUMNS", "DECISIONS", "DONE_KINDS", "Decision", "Evidence",
    "Folded", "Item", "Link", "MAX_EVIDENCE_AGE_S", "SCHEMA", "STATES", "STATE_COLUMN", "State", "TIERS",
    "derive_all", "derive_state", "fold", "parse_timestamp",
]

SCHEMA = "ready-v2"
#: Evidence older than this reads unknown (6 h: judged in the design page, to be set by the land waiter's own fetch
#: cadence; one named constant so the line moves in one place).
MAX_EVIDENCE_AGE_S = 21600
TIERS = ("sonnet", "opus", "haiku")
DONE_KINDS = ("merged", "document", "band", "run")
DECISIONS = ("cut", "parked", "out-of-1.0")
_OPS = ("add", "amend", "decide")
_LINK_KINDS = ("item", "band", "link", "gate")
_AMEND_FIELDS = ("title", "words", "done", "done_kind", "tier", "depends_on")

#: Every state a read can show and the board column it sits in. ``unknown`` is outside the six-column set on purpose:
#: the board shows it in a band of its own at the top and the gates fail closed on it.
STATE_COLUMN: dict[str, str] = {
    "cut": "off", "parked": "off", "out_of_1_0": "off",
    "done": "done",
    "needs_you": "needs_you",
    "landing": "waiting",
    "in_flight": "running",
    "landed_not_closed": "backlog",
    "blocked": "backlog",
    "ready_with_start": "ready",
    "needs_grooming": "backlog",
    "ready": "ready",
    "unknown": "unknown",
}
STATES = tuple(STATE_COLUMN)
COLUMNS = tuple(dict.fromkeys(STATE_COLUMN.values()))
AUDIT_KINDS = ("unknown", "landed_not_closed", "gate_expired", "unattributed_work", "unknown_id_in_trailer")
_DECISION_STATE = {"cut": "cut", "parked": "parked", "out-of-1.0": "out_of_1_0"}

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*#[0-9]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_GIT_SOURCES = ("main", "chain", "branches", "refs")


class _Bad(Exception):
    """A value that is not the shape the contract states (the caller decides what that makes of the file)."""


class _Soft(Exception):
    """A clean record that cannot apply (it names an id the file never added): reported, not fatal."""


def parse_timestamp(text: Any) -> datetime | None:
    """An ISO-8601 stamp as an aware datetime; a naive one is read as UTC (evidence is UTC), junk is None."""
    if isinstance(text, datetime):
        d = text
    else:
        s = str(text or "").strip()
        if not s:
            return None
        if s[-1] in "Zz":
            s = s[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ the item store
@dataclass(frozen=True)
class Link:
    """One typed dependency. ``item``/``band``/``link`` carry a ``ref``; ``gate`` carries its ``words`` and a
    ``recheck`` date (a gate nothing can check is allowed only while its date has not passed)."""
    kind: str
    ref: str | None = None
    words: str | None = None
    recheck: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "gate":
            return f'gate "{self.words}" (recheck {self.recheck})'
        return str(self.ref)


@dataclass(frozen=True)
class Decision:
    decision: str
    words: str
    date: str
    at: str
    by: str


@dataclass(frozen=True)
class Amendment:
    at: str
    by: str
    fields: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class Item:
    id: str
    repo: str
    title: str
    words: str
    origin: str
    done: str | None
    done_kind: str
    tier: str | None
    depends_on: tuple[Link, ...]
    added_at: str
    added_by: str
    decision: Decision | None = None
    amendments: tuple[Amendment, ...] = ()


@dataclass(frozen=True)
class Folded:
    items: Mapping[str, Item]
    problems: tuple[str, ...] = ()
    torn: bool = False
    torn_why: str = ""


def _req_str(obj: Mapping[str, Any], key: str, what: str | None = None) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise _Bad(f"{what or key} must be a non-empty string")
    return v


def _opt_str(v: Any, what: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise _Bad(f"{what} must be a string or null")
    return v.strip() or None


def _link(raw: Any) -> Link:
    if not isinstance(raw, Mapping):
        raise _Bad("a depends_on entry must be an object")
    kind = raw.get("kind")
    if kind not in _LINK_KINDS:
        raise _Bad(f"unknown link kind {kind!r}")
    if kind == "gate":
        recheck = raw.get("recheck")
        if not isinstance(recheck, str) or not _DATE.fullmatch(recheck):
            raise _Bad("a gate needs a recheck date (YYYY-MM-DD)")
        try:
            _date.fromisoformat(recheck)
        except ValueError:
            raise _Bad("a gate's recheck is not a calendar date") from None
        return Link("gate", words=_req_str(raw, "words", "gate words"), recheck=recheck)
    return Link(kind, ref=_req_str(raw, "ref", f"{kind} link ref"))


def _links(raw: Any) -> tuple[Link, ...]:
    if not isinstance(raw, (list, tuple)):
        raise _Bad("depends_on must be a list")
    return tuple(_link(r) for r in raw)


def _field_value(name: str, raw: Any) -> Any:
    if name == "title":
        if not isinstance(raw, str) or not raw.strip():
            raise _Bad("title must be a non-empty string")
        return raw
    if name == "words":
        if not isinstance(raw, str):
            raise _Bad("words must be a string")
        return raw
    if name in ("done", "tier"):
        return _opt_str(raw, name)
    if name == "done_kind":
        if raw not in DONE_KINDS:
            raise _Bad(f"done_kind {raw!r} is not one of {DONE_KINDS}")
        return raw
    return _links(raw)


def _common(rec: Mapping[str, Any]) -> tuple[str, str, str, str]:
    if type(rec.get("v")) is not int or rec["v"] != 1:
        raise _Bad(f"unknown record version {rec.get('v')!r}")
    op = rec.get("op")
    if op not in _OPS:
        raise _Bad(f"unknown op {op!r}")
    iid = _req_str(rec, "id")
    if not _ID.fullmatch(iid):
        raise _Bad(f"id {iid!r} is not <repo>#<n>")
    return op, iid, _req_str(rec, "at"), _req_str(rec, "by")


def _apply(rec: Mapping[str, Any], items: dict[str, Item]) -> None:
    op, iid, at, by = _common(rec)
    if op == "add":
        if iid in items:
            raise _Soft(f"{iid}: added twice (the first add stands)")
        items[iid] = Item(
            id=iid, repo=_req_str(rec, "repo"), title=_field_value("title", rec.get("title")),
            words=_field_value("words", rec.get("words", "")), origin=_req_str(rec, "origin"),
            done=_field_value("done", rec.get("done")), done_kind=_field_value("done_kind", rec.get("done_kind", "merged")),
            tier=_field_value("tier", rec.get("tier")), depends_on=_field_value("depends_on", rec.get("depends_on", [])),
            added_at=at, added_by=by)
        return
    if op == "amend":
        fields = rec.get("fields")
        if not isinstance(fields, Mapping) or not fields:
            raise _Bad("amend needs a non-empty fields object")
        extra = sorted(set(fields) - set(_AMEND_FIELDS))
        if extra:
            raise _Bad(f"amend names fields that are not amendable: {extra}")
        values = {k: _field_value(k, v) for k, v in fields.items()}
        why = _req_str(rec, "why")
        if iid not in items:
            raise _Soft(f"{iid}: amended but never added")
        old = items[iid]
        items[iid] = replace(old, **values, amendments=old.amendments + (Amendment(at, by, tuple(sorted(values)), why),))
        return
    decision = rec.get("decision")
    if decision not in (*DECISIONS, "reopen"):
        raise _Bad(f"decision {decision!r} is not one of {(*DECISIONS, 'reopen')}")
    words, when = _req_str(rec, "words"), _req_str(rec, "date")
    if iid not in items:
        raise _Soft(f"{iid}: decided but never added")
    items[iid] = replace(items[iid], decision=None if decision == "reopen" else Decision(decision, words, when, at, by))


def fold(lines: Iterable[str]) -> Folded:
    """The current items from the record lines, in file order. Blank lines are skipped; every other line must be a
    well-formed record of a known version and op, or the file is torn (a crash mid-append looks exactly like this)."""
    items: dict[str, Item] = {}
    problems: list[str] = []
    torn_why = ""
    for n, raw in enumerate(lines, 1):
        line = raw.strip() if isinstance(raw, str) else ""
        if not line:
            continue
        try:
            try:
                rec = json.loads(line)
            except ValueError as e:
                raise _Bad(f"not JSON ({e})") from None
            if not isinstance(rec, Mapping):
                raise _Bad("a record must be a JSON object")
            _apply(rec, items)
        except _Soft as e:
            problems.append(f"line {n}: {e}")
        except _Bad as e:
            problems.append(f"line {n}: torn: {e}")
            torn_why = torn_why or f"line {n}: {e}"
    return Folded(items, tuple(problems), bool(torn_why), torn_why)


# ------------------------------------------------------------------------------------------------ the evidence
@dataclass(frozen=True)
class MainCommit:
    sha: str
    date: str | None
    items: tuple[str, ...]
    closes: tuple[str, ...]


@dataclass(frozen=True)
class ChainCommit:
    sha: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class Chain:
    ref: str
    tip: str | None
    commits: tuple[ChainCommit, ...]


@dataclass(frozen=True)
class Branch:
    name: str
    tip: str
    date: str | None
    ahead: int
    items: tuple[str, ...]


@dataclass(frozen=True)
class Seat:
    name: str
    items: tuple[str, ...]
    since: str | None
    branch: str | None


@dataclass(frozen=True)
class Waiter:
    branch: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class Unattributed:
    sha: str
    subject: str


def _seq(raw: Any, what: str) -> Sequence[Any]:
    if not isinstance(raw, (list, tuple)):
        raise _Bad(f"{what} must be a list")
    return raw


def _obj(raw: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise _Bad(f"{what} must be an object")
    return raw


def _strs(raw: Any, what: str) -> tuple[str, ...]:
    out = _seq(raw, what)
    if not all(isinstance(s, str) and s for s in out):
        raise _Bad(f"{what} must be a list of non-empty strings")
    return tuple(out)


def _p_main(raw: Any) -> tuple[MainCommit, ...]:
    return tuple(MainCommit(_req_str(c := _obj(r, "a main commit"), "sha"), _opt_str(c.get("date"), "date"),
                            _strs(c.get("items", []), "items"), _strs(c.get("closes", []), "closes"))
                 for r in _seq(raw, "main"))


def _p_chain(raw: Any) -> tuple[Chain, ...]:
    return tuple(Chain(_req_str(c := _obj(r, "a chain link"), "ref"), _opt_str(c.get("tip"), "tip"),
                       tuple(ChainCommit(_req_str(k := _obj(cc, "a chain commit"), "sha"), _strs(k.get("items", []), "items"))
                             for cc in _seq(c.get("commits", []), "commits")))
                 for r in _seq(raw, "chain"))


def _p_branches(raw: Any) -> tuple[Branch, ...]:
    out = []
    for r in _seq(raw, "branches"):
        b = _obj(r, "a branch")
        ahead = b.get("ahead", 0)
        if type(ahead) is not int or ahead < 0:
            raise _Bad("ahead must be a non-negative integer")
        out.append(Branch(_req_str(b, "name"), _req_str(b, "tip"), _opt_str(b.get("date"), "date"), ahead,
                          _strs(b.get("items", []), "items")))
    return tuple(out)


def _p_seats(raw: Any) -> tuple[Seat, ...]:
    return tuple(Seat(_req_str(s := _obj(r, "a seat"), "name"), _strs(s.get("items", []), "items"),
                      _opt_str(s.get("since"), "since"), _opt_str(s.get("branch"), "branch"))
                 for r in _seq(raw, "seats"))


def _p_waiters(raw: Any) -> tuple[Waiter, ...]:
    return tuple(Waiter(_req_str(w := _obj(r, "a waiter"), "branch"), _strs(w.get("items", []), "items"))
                 for r in _seq(raw, "waiters"))


def _p_band(raw: Any) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    b = _obj(raw, "band")
    if "open" not in b or "resolved" not in b:
        raise _Bad("band needs both open and resolved")
    opened = {str(k): _strs(v, "a band row's item list") for k, v in _obj(b["open"], "band.open").items()}
    resolved = {}
    for k, v in _obj(b["resolved"], "band.resolved").items():
        if not isinstance(v, str):
            raise _Bad("band.resolved values must be strings")
        resolved[str(k)] = v
    return opened, resolved


def _p_documents(raw: Any) -> dict[str, bool]:
    d = _obj(raw, "documents")
    if not all(isinstance(v, bool) for v in d.values()):
        raise _Bad("documents values must be true or false")
    return {str(k): v for k, v in d.items()}


def _p_unattributed(raw: Any) -> tuple[Unattributed, ...]:
    return tuple(Unattributed(_req_str(u := _obj(r, "a commit"), "sha"), str(u.get("subject") or ""))
                 for r in _seq(raw, "unattributed"))


@dataclass(frozen=True)
class Evidence:
    """The merged evidence object (``evidence_v`` 1), parsed. Built by :meth:`from_dict`, which never raises: a source
    that is missing or malformed lands in ``unreadable`` with its reason, and every item whose rule needs that source
    reads unknown. A missing source is NOT an empty one: absence of a signal is never rendered as health."""
    fetched_at: str | None
    fetched_ts: datetime | None
    unreadable: Mapping[str, str]
    main: tuple[MainCommit, ...]
    chain: tuple[Chain, ...]
    branches: tuple[Branch, ...]
    refs: frozenset[str]
    merged: frozenset[str]
    seats: tuple[Seat, ...]
    band_open: Mapping[str, tuple[str, ...]]
    band_resolved: Mapping[str, str]
    waiters: tuple[Waiter, ...]
    documents: Mapping[str, bool]
    unattributed: tuple[Unattributed, ...]
    problems: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, obj: Any) -> "Evidence":
        problems: list[str] = []
        unreadable: dict[str, str] = {}
        if not isinstance(obj, Mapping):
            unreadable["evidence"] = "not an object"
            obj = {}
        elif type(obj.get("evidence_v")) is not int or obj["evidence_v"] != 1:
            unreadable["evidence"] = f"evidence_v {obj.get('evidence_v')!r} is not 1"
        given = obj.get("unreadable")
        if isinstance(given, Mapping):
            unreadable.update({str(k): str(v or "unreadable") for k, v in given.items()})
        elif isinstance(given, (list, tuple)):
            unreadable.update({str(k): "unreadable" for k in given})
        elif given is not None:
            problems.append("evidence.unreadable is neither an object nor a list")

        def src(name: str, parse: Callable[[Any], Any], empty: Any, keys: Sequence[str] | None = None) -> Any:
            keys = keys or (name,)
            if any(k not in obj for k in keys):
                unreadable.setdefault(name, "not supplied")
                return empty
            try:
                return parse(obj[name] if keys == (name,) else {k: obj[k] for k in keys})
            except _Bad as e:
                unreadable.setdefault(name, f"malformed: {e}")
                return empty

        main = src("main", _p_main, ())
        chain = src("chain", _p_chain, ())
        branches = src("branches", _p_branches, ())
        refs_merged = src("refs", lambda d: (frozenset(_strs(d["refs"], "refs")), frozenset(_strs(d["merged"], "merged"))),
                          (frozenset(), frozenset()), keys=("refs", "merged"))
        seats = src("seats", _p_seats, ())
        band = src("band", _p_band, ({}, {}))
        waiters = src("waiters", _p_waiters, ())
        documents = src("documents", _p_documents, {})
        if "unattributed" in obj:
            try:
                unattributed = _p_unattributed(obj["unattributed"])
            except _Bad as e:
                unattributed = ()
                problems.append(f"evidence.unattributed is malformed: {e}")
        else:
            unattributed = ()
            problems.append("evidence.unattributed was not supplied: unattributed work is not audited")
        raw_fetched = obj.get("fetched_at")
        fetched_at = raw_fetched if isinstance(raw_fetched, str) and raw_fetched.strip() else None
        return cls(fetched_at, parse_timestamp(fetched_at), unreadable, main, chain, branches, refs_merged[0],
                   refs_merged[1], seats, band[0], band[1], waiters, documents, unattributed, tuple(problems))

    def unreadable_reason(self, source: str) -> str | None:
        """Why ``source`` cannot be trusted, or None. ``git`` stands for the four git-derived sources and ``notes`` for
        the trailers carried in refs/notes/board (part of ``main``)."""
        for key in (source, "git" if source in _GIT_SOURCES else None, "notes" if source == "main" else None):
            if key and self.unreadable.get(key):
                return f"{key}: {self.unreadable[key]}" if key != source else self.unreadable[key]
        return None

    @cached_property
    def index(self) -> "_Index":
        return _Index(self)


def _ts_key(text: str | None) -> float:
    d = parse_timestamp(text)
    return d.timestamp() if d else float("-inf")


class _Index:
    """The evidence keyed by item id, built once per evidence object."""

    def __init__(self, ev: Evidence) -> None:
        self.closes: dict[str, MainCommit] = {}
        self.touched: dict[str, list[MainCommit]] = {}
        self.main_shas = {c.sha for c in ev.main}
        for c in ev.main:
            for iid in dict.fromkeys(c.items + c.closes):
                self.touched.setdefault(iid, []).append(c)
            for iid in c.closes:
                if iid not in self.closes or _ts_key(c.date) > _ts_key(self.closes[iid].date):
                    self.closes[iid] = c
        self.chain: dict[str, list[tuple[str, int]]] = {}
        self.chain_shas: set[str] = set()
        for ch in ev.chain:
            counts: dict[str, int] = {}
            for cc in ch.commits:
                self.chain_shas.add(cc.sha)
                for iid in dict.fromkeys(cc.items):
                    counts[iid] = counts.get(iid, 0) + 1
            if ch.tip:
                self.chain_shas.add(ch.tip)
            for iid, n in counts.items():
                self.chain.setdefault(iid, []).append((ch.ref, n))
        self.branches: dict[str, list[Branch]] = {}
        for b in ev.branches:
            for iid in dict.fromkeys(b.items):
                self.branches.setdefault(iid, []).append(b)
        self.seats_named: dict[str, list[Seat]] = {}
        self.seats_on_branch: dict[str, list[Seat]] = {}
        for s in ev.seats:
            for iid in dict.fromkeys(s.items):
                self.seats_named.setdefault(iid, []).append(s)
            if s.branch:
                self.seats_on_branch.setdefault(s.branch, []).append(s)
        self.waiters: dict[str, list[Waiter]] = {}
        for w in ev.waiters:
            for iid in dict.fromkeys(w.items):
                self.waiters.setdefault(iid, []).append(w)
        self.band_naming: dict[str, list[str]] = {}
        for np_id, ids in ev.band_open.items():
            for iid in dict.fromkeys(ids):
                self.band_naming.setdefault(iid, []).append(np_id)

    def live_seats(self, iid: str) -> list[Seat]:
        """Seats that name the id, or hold a branch that carries it."""
        out = list(self.seats_named.get(iid, ()))
        for b in self.branches.get(iid, ()):
            for s in self.seats_on_branch.get(b.name, ()):
                if s not in out:
                    out.append(s)
        return out

    def orphans(self, iid: str) -> list[Branch]:
        """Branches carrying the id with commits of their own that no live seat holds and that are on neither main nor
        the landing chain."""
        return [b for b in self.branches.get(iid, ())
                if b.ahead > 0 and b.name not in self.seats_on_branch
                and b.tip not in self.main_shas and b.tip not in self.chain_shas]


# ------------------------------------------------------------------------------------------------ the derived state
@dataclass(frozen=True)
class State:
    id: str
    repo: str
    title: str
    tier: str | None
    state: str
    column: str
    reason: str
    refs: tuple[str, ...]
    as_of: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "repo": self.repo, "title": self.title, "tier": self.tier, "state": self.state,
                "column": self.column, "reason": self.reason, "refs": list(self.refs), "as_of": self.as_of}


@dataclass(frozen=True)
class AuditEntry:
    kind: str
    id: str | None
    why: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "why": self.why}


@dataclass(frozen=True)
class Board:
    schema: str
    as_of: str | None
    items: tuple[State, ...]
    audit: tuple[AuditEntry, ...]
    unattributed: tuple[Unattributed, ...]
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "as_of": self.as_of, "items": [s.to_dict() for s in self.items],
                "audit": [a.to_dict() for a in self.audit],
                "unattributed": [{"sha": u.sha, "subject": u.subject} for u in self.unattributed],
                "problems": list(self.problems)}


def _aware(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _short(sha: str) -> str:
    return sha[:9]


class _Ctx:
    def __init__(self, items: Mapping[str, Item], ev: Evidence, now: datetime, torn: str | None) -> None:
        self.items = items
        self.ev = ev
        self.idx = ev.index
        self.now = _aware(now)
        self.torn = torn
        self.memo: dict[str, State] = {}
        self.stack: set[str] = set()
        self._cycles: dict[str, tuple[str, ...] | None] = {}

    def make(self, item: Item, state: str, reason: str, refs: Iterable[str] = ()) -> State:
        return State(item.id, item.repo, item.title, item.tier if item.tier in TIERS else None, state,
                     STATE_COLUMN[state], reason, tuple(refs), self.ev.fetched_at)

    def state_of(self, iid: str) -> State:
        if iid in self.memo:
            return self.memo[iid]
        item = self.items[iid]
        if iid in self.stack:               # a guard behind the cycle check: recursion can never run away
            return self.make(item, "unknown", f"cycle through {iid}", (iid,))
        self.stack.add(iid)
        try:
            st = _compute(self, item)
        finally:
            self.stack.discard(iid)
        self.memo[iid] = st
        return st

    def cycle(self, iid: str) -> tuple[str, ...] | None:
        """The shortest ``depends_on`` cycle through ``iid`` (item links only; a decided item's links do not count)."""
        if iid not in self._cycles:
            self._cycles[iid] = self._find_cycle(iid)
        return self._cycles[iid]

    def _deps(self, iid: str) -> list[str]:
        it = self.items.get(iid)
        if it is None or it.decision is not None:
            return []
        return [l.ref for l in it.depends_on if l.kind == "item" and l.ref in self.items]

    def _find_cycle(self, start: str) -> tuple[str, ...] | None:
        parent: dict[str, str | None] = {start: None}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for dep in self._deps(node):
                if dep == start:
                    path = [node]
                    while parent[path[-1]] is not None:
                        path.append(parent[path[-1]])           # type: ignore[arg-type]
                    return (*reversed(path), start)
                if dep not in parent:
                    parent[dep] = node
                    queue.append(dep)
        return None


def _needs(item: Item) -> list[str]:
    need = ["evidence", "main", "chain", "branches", "seats", "band", "waiters"]
    if any(l.kind == "link" for l in item.depends_on):
        need.append("refs")
    if item.done_kind in ("document", "run"):
        need.append("documents")
    return need


# --- the checks that give ``unknown`` (each: (ctx, item) -> (reason, refs) or None); the order here is the order run
def _chk_torn(ctx: _Ctx, item: Item):
    return (f"items file torn: {ctx.torn}", ()) if ctx.torn else None


def _chk_unreadable(ctx: _Ctx, item: Item):
    bad = [f"{s} unreadable: {r}" for s in _needs(item) if (r := ctx.ev.unreadable_reason(s))]
    return ("; ".join(bad), ()) if bad else None


def _chk_stale(ctx: _Ctx, item: Item):
    ev = ctx.ev
    if ev.fetched_ts is None:
        return "origin/main as of unknown (no fetch time in the evidence)", ()
    if (ctx.now - ev.fetched_ts).total_seconds() > MAX_EVIDENCE_AGE_S:
        return f"origin/main as of {ev.fetched_at} (older than {MAX_EVIDENCE_AGE_S // 3600} h)", ()
    return None


def _chk_cycle(ctx: _Ctx, item: Item):
    path = ctx.cycle(item.id) if item.decision is None else None
    return (f"cycle {' -> '.join(path)}", path) if path else None


def _chk_dangling_item(ctx: _Ctx, item: Item):
    gone = [l.ref for l in item.depends_on if l.kind == "item" and l.ref not in ctx.items]
    return (f"item:{gone[0]} is not in the store", tuple(gone)) if gone else None


def _chk_dangling_band(ctx: _Ctx, item: Item):
    ev = ctx.ev
    refs = [l.ref for l in item.depends_on if l.kind == "band"]
    if item.done_kind == "band" and item.done:
        refs.append(item.done)
    gone = [r for r in refs if r not in ev.band_open and r not in ev.band_resolved]
    return (f"band:{gone[0]} is not in the band", tuple(gone)) if gone else None


def _chk_dangling_link(ctx: _Ctx, item: Item):
    gone = [l.ref for l in item.depends_on if l.kind == "link" and l.ref not in ctx.ev.refs and l.ref not in ctx.ev.merged]
    return (f"link:{gone[0]} is not a ref on origin", tuple(gone)) if gone else None


def _chk_unchecked_document(ctx: _Ctx, item: Item):
    if item.done_kind in ("document", "run"):
        path = (item.done or "").strip()
        if not path or path not in ctx.ev.documents:
            return f"{item.done_kind} evidence {path or '(no path in done)'} was not checked", ()
    return None


def _expired_gates(item: Item, today: _date) -> list[Link]:
    return [l for l in item.depends_on if l.kind == "gate" and _date.fromisoformat(l.recheck or "") < today]


def _chk_gate_expired(ctx: _Ctx, item: Item):
    old = _expired_gates(item, ctx.now.date())
    return (f"gate \"{old[0].words}\" past its recheck {old[0].recheck}", ()) if old else None


def _trailer_close(ctx: _Ctx, item: Item) -> MainCommit | None:
    return ctx.idx.closes.get(item.id)


def _chk_conflict_closed_seat(ctx: _Ctx, item: Item):
    closed, seats = _trailer_close(ctx, item), ctx.idx.live_seats(item.id)
    if closed and seats:
        return (f"conflict: closed by {_short(closed.sha)} on main vs live seat {seats[0].name}"
                f"{f' since {seats[0].since}' if seats[0].since else ''}", (closed.sha, seats[0].name))
    return None


def _chk_conflict_closed_band(ctx: _Ctx, item: Item):
    closed, rows = _trailer_close(ctx, item), ctx.idx.band_naming.get(item.id, [])
    if closed and rows:
        return f"conflict: closed by {_short(closed.sha)} on main vs open band row {rows[0]}", (closed.sha, rows[0])
    return None


def _chk_conflict_chain_orphan(ctx: _Ctx, item: Item):
    chain, orphans = ctx.idx.chain.get(item.id), ctx.idx.orphans(item.id)
    if chain and orphans:
        b = orphans[0]
        return (f"conflict: in chain {chain[0][0]} vs orphan branch {b.name} {_short(b.tip)} "
                f"({b.ahead} commits the chain lacks)", (chain[0][0], b.name))
    return None


#: ``(name, check)`` in the order run. A decided item (rule 1) skips every check but the first: a ruling is stored, not
#: derived, so no evidence can contradict it, while a torn file may have lost the ``reopen`` that undoes it.
_UNKNOWN_CHECKS: tuple[tuple[str, Callable[[_Ctx, Item], Any]], ...] = (
    ("torn", _chk_torn),
    ("unreadable", _chk_unreadable),
    ("stale", _chk_stale),
    ("cycle", _chk_cycle),
    ("dangling_item", _chk_dangling_item),
    ("dangling_band", _chk_dangling_band),
    ("dangling_link", _chk_dangling_link),
    ("unchecked_document", _chk_unchecked_document),
    ("gate_expired", _chk_gate_expired),
    ("conflict_closed_seat", _chk_conflict_closed_seat),
    ("conflict_closed_band", _chk_conflict_closed_band),
    ("conflict_chain_orphan", _chk_conflict_chain_orphan),
)
_CHECKS_FOR_DECIDED = ("torn",)


# --- the rule table (design 3.2): first rule that fires wins
def _rule_decision(ctx: _Ctx, item: Item) -> State | None:
    d = item.decision
    return ctx.make(item, _DECISION_STATE[d.decision], f"{d.words} ({d.date})") if d else None


def _rule_done(ctx: _Ctx, item: Item) -> State | None:
    kind, ev = item.done_kind, ctx.ev
    if kind == "merged":
        c = ctx.idx.closes.get(item.id)
        return ctx.make(item, "done", f"closed by {_short(c.sha)} ({c.date or 'no date'})", (c.sha,)) if c else None
    path = (item.done or "").strip()
    if kind in ("document", "run"):
        return ctx.make(item, "done", f"{kind} {path} exists", (path,)) if ev.documents.get(path) is True else None
    if path in ev.band_resolved and path not in ev.band_open:
        return ctx.make(item, "done", f"band {path} resolved by {ev.band_resolved[path]}", (path,))
    return None


def _rule_needs_you(ctx: _Ctx, item: Item) -> State | None:
    rows = list(ctx.idx.band_naming.get(item.id, ()))
    if item.done_kind == "band" and item.done and item.done.strip() in ctx.ev.band_open:
        rows.append(item.done.strip())
    rows = list(dict.fromkeys(rows))
    return ctx.make(item, "needs_you", f"band {', '.join(rows)}", rows) if rows else None


def _rule_landing(ctx: _Ctx, item: Item) -> State | None:
    chain, waiters = ctx.idx.chain.get(item.id), ctx.idx.waiters.get(item.id)
    if chain:
        ref, n = chain[0]
        return ctx.make(item, "landing", f"in chain link {ref} ({n} commits)", [r for r, _ in chain])
    if waiters:
        return ctx.make(item, "landing", f"CI on {waiters[0].branch}", [w.branch for w in waiters])
    return None


def _rule_in_flight(ctx: _Ctx, item: Item) -> State | None:
    seats = ctx.idx.live_seats(item.id)
    if not seats:
        return None
    s = seats[0]
    detail = f"seat {s.name} since {s.since or 'unknown'}" + (f", branch {s.branch}" if s.branch else "")
    return ctx.make(item, "in_flight", detail, [x.name for x in seats])


def _rule_landed_not_closed(ctx: _Ctx, item: Item) -> State | None:
    # chain and seat evidence were ruled out by the rules above; a branch with commits of its own is not (rule 8)
    touched = ctx.idx.touched.get(item.id)
    if not touched or item.id in ctx.idx.closes or ctx.idx.orphans(item.id):
        return None
    last = max(touched, key=lambda c: _ts_key(c.date))
    return ctx.make(item, "landed_not_closed",
                    f"{len(touched)} commits on main, last {_short(last.sha)}; close it or name what remains",
                    [last.sha])


def _unmet(ctx: _Ctx, item: Item) -> list[tuple[str, str, str]]:
    """``(ref, label, why)`` per dependency not yet met, in link order."""
    out = []
    for l in item.depends_on:
        if l.kind == "item":
            dep = ctx.items.get(l.ref or "")
            if dep is None:
                out.append((l.ref or "", l.label, "not in the store"))
                continue
            s = ctx.state_of(dep.id)
            if s.state != "done":
                out.append((dep.id, l.label, f"{s.state} ({s.reason})" if s.reason else s.state))
        elif l.kind == "band":
            if l.ref in ctx.ev.band_open or l.ref not in ctx.ev.band_resolved:
                out.append((l.ref or "", l.label, "open on the band"))
        elif l.kind == "link":
            if l.ref not in ctx.ev.merged:
                out.append((l.ref or "", l.label, "not merged into origin/main"))
        else:
            out.append((l.words or "", l.label, "not machine-checkable"))
    return out


def _rule_blocked(ctx: _Ctx, item: Item) -> State | None:
    unmet = _unmet(ctx, item)
    if not unmet:
        return None
    ref, label, why = unmet[0]
    more = f" (+{len(unmet) - 1} more)" if len(unmet) > 1 else ""
    return ctx.make(item, "blocked", f"waits on {label}: {why}{more}", [u[0] for u in unmet])


def _rule_ready_with_start(ctx: _Ctx, item: Item) -> State | None:
    orphans = ctx.idx.orphans(item.id)
    if not orphans:
        return None
    b = max(orphans, key=lambda x: _ts_key(x.date))
    return ctx.make(item, "ready_with_start",
                    f"orphan branch {b.name} {_short(b.tip)} ({b.date or 'no date'}), {b.ahead} own commits",
                    [b.name, b.tip])


def _grooming_gap(item: Item) -> str | None:
    no_done, no_tier = not item.done, item.tier not in TIERS
    if no_done and no_tier:
        return "needs grooming: no DONE line or tier"
    return "needs grooming: no DONE line" if no_done else "needs grooming: no tier" if no_tier else None


def _rule_needs_grooming(ctx: _Ctx, item: Item) -> State | None:
    gap = _grooming_gap(item)
    return ctx.make(item, "needs_grooming", gap) if gap else None


def _rule_ready(ctx: _Ctx, item: Item) -> State | None:
    return ctx.make(item, "ready", "no evidence of work, nothing it waits on")


_RULES: tuple[tuple[str, Callable[[_Ctx, Item], State | None]], ...] = (
    ("decision", _rule_decision),
    ("done", _rule_done),
    ("needs_you", _rule_needs_you),
    ("landing", _rule_landing),
    ("in_flight", _rule_in_flight),
    ("landed_not_closed", _rule_landed_not_closed),
    ("blocked", _rule_blocked),
    ("ready_with_start", _rule_ready_with_start),
    ("needs_grooming", _rule_needs_grooming),
    ("ready", _rule_ready),
)


def _compute(ctx: _Ctx, item: Item) -> State:
    for name, check in _UNKNOWN_CHECKS:
        if item.decision is not None and name not in _CHECKS_FOR_DECIDED:
            continue
        found = check(ctx, item)
        if found:
            return ctx.make(item, "unknown", found[0], found[1])
    for _name, rule in _RULES:
        st = rule(ctx, item)
        if st is not None:
            return st
    return ctx.make(item, "unknown", "no rule fired", ())


def derive_state(item: Item, evidence: Evidence, items: Mapping[str, Item], *, now: datetime) -> State:
    """One item's state. ``items`` is the whole store (a dependency is read through to its own state)."""
    return _Ctx({**items, item.id: item}, evidence, now, None).state_of(item.id)


def _id_key(iid: str) -> tuple[str, int]:
    repo, _, n = iid.rpartition("#")
    return repo, int(n) if n.isdigit() else 0


def _trailer_ids(ev: Evidence) -> dict[str, list[str]]:
    """Every item id a commit, branch, seat or waiter names, with where it was named."""
    seen: dict[str, list[str]] = {}
    for c in ev.main:
        for iid in (*c.items, *c.closes):
            seen.setdefault(iid, []).append(f"main {_short(c.sha)}")
    for ch in ev.chain:
        for cc in ch.commits:
            for iid in cc.items:
                seen.setdefault(iid, []).append(f"chain {ch.ref} {_short(cc.sha)}")
    for b in ev.branches:
        for iid in b.items:
            seen.setdefault(iid, []).append(f"branch {b.name}")
    for s in ev.seats:
        for iid in s.items:
            seen.setdefault(iid, []).append(f"seat {s.name}")
    for w in ev.waiters:
        for iid in w.items:
            seen.setdefault(iid, []).append(f"waiter {w.branch}")
    return seen


def derive_all(folded: Folded, evidence: Evidence, *, now: datetime) -> Board:
    """The whole board: every item's state, the audit list, and whatever could not be read."""
    now = _aware(now)
    ctx = _Ctx(folded.items, evidence, now, folded.torn_why or ("torn" if folded.torn else None))
    states = [ctx.state_of(i) for i in sorted(folded.items, key=_id_key)]
    audit: list[AuditEntry] = []
    if folded.torn:
        audit.append(AuditEntry("unknown", None, f"items file torn: {folded.torn_why}"))
    for s in states:
        if s.state == "unknown":
            audit.append(AuditEntry("unknown", s.id, s.reason))
        elif s.state == "landed_not_closed":
            audit.append(AuditEntry("landed_not_closed", s.id, s.reason))
    for iid in sorted(folded.items, key=_id_key):
        item = folded.items[iid]
        if item.decision is None:
            for g in _expired_gates(item, now.date()):
                audit.append(AuditEntry("gate_expired", iid, f"gate \"{g.words}\" past its recheck {g.recheck}"))
    for u in evidence.unattributed:
        audit.append(AuditEntry("unattributed_work", None, f"{_short(u.sha)} {u.subject}".strip()))
    if not folded.torn:                 # with a torn store most ids would look unknown for a reason already reported
        for iid, where in sorted(_trailer_ids(evidence).items(), key=lambda kv: _id_key(kv[0])):
            if iid not in folded.items:
                audit.append(AuditEntry("unknown_id_in_trailer", iid, f"named by {', '.join(dict.fromkeys(where))}; the store does not hold it"))
    audit.sort(key=lambda a: AUDIT_KINDS.index(a.kind))
    return Board(SCHEMA, evidence.fetched_at, tuple(states), tuple(audit), evidence.unattributed,
                 (*folded.problems, *evidence.problems))
