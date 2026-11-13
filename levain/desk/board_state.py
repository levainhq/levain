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
  dependency cycle, a link that names nothing, evidence that contradicts itself), then the closed
  rule table :data:`_RULES` runs in order and the first rule that fires wins. A gate past its recheck date is the one
  unknown cause that lives in the table (rule 7): it makes only an item whose wait rests on the gate unknown.
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
    "FUTURE_SKEW_S", "Folded", "Item", "Link", "MAX_EVIDENCE_AGE_S", "SCHEMA", "STATES", "STATE_COLUMN", "State", "TIERS",
    "derive_all", "derive_state", "fold", "parse_timestamp",
]

SCHEMA = "ready-v2"
#: Evidence older than this reads unknown (6 h: judged in the design page, to be set by the land waiter's own fetch
#: cadence; one named constant so the line moves in one place).
MAX_EVIDENCE_AGE_S = 21600
#: Evidence fetched MORE than this far ahead of ``now`` reads unknown ("fetched_at is in the future"): a fetcher with a
#: wrong clock must not keep every item fresh for ever. A smaller lead is ordinary clock skew between two machines.
FUTURE_SKEW_S = 300
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
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_UTC_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
_DIGITS = re.compile(r"[0-9]+")
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


def _calendar_date(text: str, what: str) -> str:
    """``text`` if it is a real calendar date written YYYY-MM-DD (ASCII digits), else a :class:`_Bad`."""
    if not _DATE.fullmatch(text):
        raise _Bad(f"{what} must be a date written YYYY-MM-DD, not {text[:40]!r}")
    try:
        _date.fromisoformat(text)
    except ValueError:
        raise _Bad(f"{what} {text!r} is not a calendar date") from None
    return text


def _utc_stamp(text: str, what: str) -> str:
    """``text`` if it is an ISO-8601 UTC timestamp (``...Z`` or ``...+00:00``) naming a real date and time."""
    if not _UTC_STAMP.fullmatch(text):
        raise _Bad(f"{what} must be an ISO-8601 UTC timestamp like 2026-11-12T10:00:00Z, not {text[:40]!r}")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise _Bad(f"{what} {text!r} is not a real date and time") from None
    return text


def _link(raw: Any) -> Link:
    if not isinstance(raw, Mapping):
        raise _Bad("a depends_on entry must be an object")
    kind = raw.get("kind")
    if kind not in _LINK_KINDS:
        raise _Bad(f"unknown link kind {kind!r}")
    if kind == "gate":
        recheck = raw.get("recheck")
        if not isinstance(recheck, str):
            raise _Bad("a gate needs a recheck date (YYYY-MM-DD)")
        return Link("gate", words=_req_str(raw, "words", "gate words"), recheck=_calendar_date(recheck, "a gate's recheck"))
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
        repo = _req_str(rec, "repo")
        if repo != iid.rpartition("#")[0]:
            raise _Bad(f"repo {repo!r} disagrees with the id prefix of {iid}")
        item = Item(                                    # validate first: a malformed duplicate is a torn line, not a soft one
            id=iid, repo=repo, title=_field_value("title", rec.get("title")),
            words=_field_value("words", rec.get("words", "")), origin=_req_str(rec, "origin"),
            done=_field_value("done", rec.get("done")), done_kind=_field_value("done_kind", rec.get("done_kind", "merged")),
            tier=_field_value("tier", rec.get("tier")), depends_on=_field_value("depends_on", rec.get("depends_on", [])),
            added_at=at, added_by=by)
        if iid in items:
            raise _Soft(f"{iid}: added twice (the first add stands)")
        items[iid] = item
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
    words, when = _req_str(rec, "words"), _calendar_date(_req_str(rec, "date"), "decision date")
    _utc_stamp(at, "decide at")                         # a ruling carries WHEN it was made: a stamp that is not one tears the file
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
        if not isinstance(raw, str):                    # a file read in binary mode must not look like an empty store
            problems.append(f"line {n}: torn: not text ({type(raw).__name__})")
            torn_why = torn_why or f"line {n}: not text ({type(raw).__name__})"
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            try:
                rec = json.loads(line)
            except RecursionError:                      # a line of 100,000 brackets overflows the parser's stack: torn, not a crash
                raise _Bad("not JSON (nested too deeply)") from None
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


def _need(obj: Mapping[str, Any], key: str, what: str) -> Any:
    """A field the interface spec makes REQUIRED: absent is a malformed source, never an empty value (an item must not
    read ready because a record forgot to say what it carries)."""
    if key not in obj:
        raise _Bad(f"{what}: {key} is required")
    return obj[key]


def _p_main(raw: Any) -> tuple[MainCommit, ...]:
    out = []
    for r in _seq(raw, "main"):
        c = _obj(r, "a main commit")
        out.append(MainCommit(_req_str(c, "sha"), _opt_str(c.get("date"), "date"),
                              _strs(_need(c, "items", "a main commit"), "items"),
                              _strs(_need(c, "closes", "a main commit"), "closes")))
    return tuple(out)


def _p_chain(raw: Any) -> tuple[Chain, ...]:
    out = []
    for r in _seq(raw, "chain"):
        c = _obj(r, "a chain link")
        commits = []
        for cc in _seq(_need(c, "commits", "a chain link"), "commits"):
            k = _obj(cc, "a chain commit")
            commits.append(ChainCommit(_req_str(k, "sha"), _strs(_need(k, "items", "a chain commit"), "items")))
        out.append(Chain(_req_str(c, "ref"), _opt_str(c.get("tip"), "tip"), tuple(commits)))
    return tuple(out)


def _p_branches(raw: Any) -> tuple[Branch, ...]:
    out = []
    for r in _seq(raw, "branches"):
        b = _obj(r, "a branch")
        ahead = _need(b, "ahead", "a branch")
        if type(ahead) is not int or ahead < 0:
            raise _Bad("a branch: ahead must be a non-negative integer")
        out.append(Branch(_req_str(b, "name"), _req_str(b, "tip"), _opt_str(b.get("date"), "date"), ahead,
                          _strs(_need(b, "items", "a branch"), "items")))
    return tuple(out)


def _p_seats(raw: Any) -> tuple[Seat, ...]:
    out = []
    for r in _seq(raw, "seats"):
        s = _obj(r, "a seat")
        out.append(Seat(_req_str(s, "name"), _strs(_need(s, "items", "a seat"), "items"),
                        _opt_str(s.get("since"), "since"), _opt_str(s.get("branch"), "branch")))
    return tuple(out)


def _p_waiters(raw: Any) -> tuple[Waiter, ...]:
    out = []
    for r in _seq(raw, "waiters"):
        w = _obj(r, "a waiter")
        out.append(Waiter(_req_str(w, "branch"), _strs(_need(w, "items", "a waiter"), "items")))
    return tuple(out)


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
    that is missing or malformed lands in ``unreadable`` with its reason, every item whose rule needs that source
    reads unknown, and :func:`derive_all` reports every unreadable source on the board itself (an empty store, or a
    source no item needs, is not a clean board). A missing source is NOT an empty one: absence of a signal is never
    rendered as health."""
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

    @classmethod
    def from_dict(cls, obj: Any) -> "Evidence":
        unreadable: dict[str, str] = {}
        if not isinstance(obj, Mapping):
            unreadable["evidence"] = "not an object"
            obj = {}
        elif type(obj.get("evidence_v")) is not int or obj["evidence_v"] != 1:
            unreadable["evidence"] = f"evidence_v {obj.get('evidence_v')!r} is not 1"
        given = obj.get("unreadable")
        if given is not None:                           # {source: reason} or nothing: any other shape fails closed
            pairs = given.items() if isinstance(given, Mapping) else [(n, "") for n in given] if isinstance(given, (list, tuple)) else []
            malformed = not isinstance(given, Mapping)
            for k, v in pairs:
                named = isinstance(k, str) and bool(k)
                malformed = malformed or not named or not isinstance(v, str)
                if named:                               # a source that was NAMED stays unreadable even if its reason is junk
                    unreadable[k] = v if isinstance(v, str) and v else "unreadable"
            if malformed:
                unreadable["evidence"] = "malformed unreadable (expected an object of source -> reason)"

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
        unattributed = src("unattributed", _p_unattributed, ())    # no item needs it, so the BOARD reports it unread
        raw_fetched = obj.get("fetched_at")
        fetched_at = raw_fetched if isinstance(raw_fetched, str) and raw_fetched.strip() else None
        return cls(fetched_at, parse_timestamp(fetched_at), unreadable, main, chain, branches, refs_merged[0],
                   refs_merged[1], seats, band[0], band[1], waiters, documents, unattributed)

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


def _commit_key(c: MainCommit) -> tuple[float, str]:
    """Latest wins; a tie in time is broken by sha, so a reason never depends on the order the evidence was listed in."""
    return _ts_key(c.date), c.sha


def _branch_key(b: Branch) -> tuple[float, str]:
    return _ts_key(b.date), b.name


class _Index:
    """The evidence keyed by item id, built once per evidence object. Where a rule takes the first of several (chain
    refs, waiters, band rows) the list is sorted here; seats are sorted by :meth:`live_seats` and branches are picked
    with ``max`` on :func:`_branch_key`, so what a reason names never depends on the order the evidence was listed in."""

    def __init__(self, ev: Evidence) -> None:
        self.closes: dict[str, MainCommit] = {}
        self.touched: dict[str, list[MainCommit]] = {}
        for c in ev.main:
            for iid in dict.fromkeys(c.items + c.closes):
                self.touched.setdefault(iid, []).append(c)
            for iid in c.closes:
                if iid not in self.closes or _commit_key(c) > _commit_key(self.closes[iid]):
                    self.closes[iid] = c
        self.chain: dict[str, list[tuple[str, int]]] = {}
        for ch in ev.chain:
            counts: dict[str, int] = {}
            for cc in ch.commits:
                for iid in dict.fromkeys(cc.items):
                    counts[iid] = counts.get(iid, 0) + 1
            for iid, n in counts.items():
                self.chain.setdefault(iid, []).append((ch.ref, n))
        for rows in self.chain.values():
            rows.sort()
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
        for w in sorted(ev.waiters, key=lambda x: x.branch):
            for iid in dict.fromkeys(w.items):
                self.waiters.setdefault(iid, []).append(w)
        self.band_naming: dict[str, list[str]] = {}
        for np_id, ids in sorted(ev.band_open.items()):
            for iid in dict.fromkeys(ids):
                self.band_naming.setdefault(iid, []).append(np_id)

    def live_seats(self, iid: str) -> list[Seat]:
        """Seats that name the id, or hold a branch that carries it, by name."""
        out = list(self.seats_named.get(iid, ()))
        for b in self.branches.get(iid, ()):
            for s in self.seats_on_branch.get(b.name, ()):
                if s not in out:
                    out.append(s)
        return sorted(out, key=lambda s: s.name)

    def orphans(self, iid: str) -> list[Branch]:
        """Branches carrying the id with commits of their own that no live seat holds. ``ahead`` is the READER's count
        of the branch's own commits whose work is on neither origin/main nor any chain ref (a one-to-one patch-id match,
        the trailer and subject only for an empty diff), so a branch that was cherry-picked or re-chained and never
        deleted has ``ahead`` 0 and is no orphan: this function never judges by tip sha, which a replay changes."""
        return [b for b in self.branches.get(iid, ()) if b.ahead > 0 and b.name not in self.seats_on_branch]


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


def _utc(now: datetime) -> datetime:
    """``now`` in UTC. A naive clock reading is refused: it could be any zone, and the evidence and the gate dates are
    judged in UTC, so guessing would give two boards for one instant."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise TypeError("now must be a timezone-aware datetime: a naive clock reading cannot be compared with UTC evidence")
    return now.astimezone(timezone.utc)


def _short(sha: str) -> str:
    return sha[:9]


class _Ctx:
    def __init__(self, items: Mapping[str, Item], ev: Evidence, now: datetime, torn: str | None) -> None:
        self.items = items
        self.ev = ev
        self.idx = ev.index
        self.now = _utc(now)
        self.torn = torn
        self.memo: dict[str, State] = {}
        self.stack: set[str] = set()
        self.causes: dict[str, str] = {}      # item id -> the name of the check that made it unknown
        self._cycles: dict[str, tuple[str, ...] | None] = {}

    def make(self, item: Item, state: str, reason: str, refs: Iterable[str] = ()) -> State:
        return State(item.id, item.repo, item.title, item.tier if item.tier in TIERS else None, state,
                     STATE_COLUMN[state], reason, tuple(refs), self.ev.fetched_at)

    def state_of(self, iid: str) -> State:
        if iid not in self.memo:
            self._prime(iid)
        return self._one(iid)

    def _prime(self, iid: str) -> None:
        """Compute every dependency of ``iid`` BEFORE it, iteratively (an explicit stack, post-order), so that reading
        a dependency in :func:`_compute` is a memo hit and a wait chain of any length never recurses."""
        seen = {iid}
        todo = [(iid, iter(self._deps(iid)))]
        order: list[str] = []
        while todo:
            node, deps = todo[-1]
            for dep in deps:
                if dep not in seen and dep not in self.memo:
                    seen.add(dep)
                    todo.append((dep, iter(self._deps(dep))))
                    break
            else:
                todo.pop()
                order.append(node)
        for node in order:
            self._one(node)

    def _one(self, iid: str) -> State:
        if iid in self.memo:
            return self.memo[iid]
        item = self.items[iid]
        if iid in self.stack:               # a guard behind the cycle check: a cycle member never reads a dependency
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
        return [l.ref for l in it.depends_on if l.kind == "item" and l.ref is not None and l.ref in self.items]

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
    why: dict[str, list[str]] = {}                      # one cause, one phrase: sources that alias a failure ("git") name it once
    for s in _needs(item):
        if r := ctx.ev.unreadable_reason(s):
            why.setdefault(r, []).append(s)
    bad = [f"{srcs[0]} unreadable: {r}" + (f" (also {', '.join(srcs[1:])})" if len(srcs) > 1 else "") for r, srcs in why.items()]
    return ("; ".join(bad), ()) if bad else None


def _evidence_problem(ev: Evidence, now: datetime) -> str | None:
    """Why the evidence's fetch time cannot be trusted (unknown, too old, or in the future), or None. The ONE place the
    age is judged: an item's unknown reason and the board-level ``stale_evidence`` line both read it."""
    if ev.fetched_ts is None:
        return "origin/main as of unknown (no fetch time in the evidence)"
    age = (now - ev.fetched_ts).total_seconds()
    if age > MAX_EVIDENCE_AGE_S:
        return f"origin/main as of {ev.fetched_at} (older than {MAX_EVIDENCE_AGE_S // 3600} h)"
    if age < -FUTURE_SKEW_S:
        return f"origin/main as of {ev.fetched_at}: fetched_at is in the future (more than {FUTURE_SKEW_S // 60} min ahead of now)"
    return None


def _chk_stale(ctx: _Ctx, item: Item):
    why = _evidence_problem(ctx.ev, ctx.now)
    return (why, ()) if why else None


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


def _trailer_close(ctx: _Ctx, item: Item) -> MainCommit | None:
    """The commit whose ``Board-Closes`` trailer closes the item, and only for a ``merged`` item: that trailer IS its
    closure. For ``document``/``run``/``band`` the closure is the item's own evidence (the file, the np row), so a
    trailer beside a file that is absent closes nothing and contradicts nothing (rule 6 reads it as landed work)."""
    return ctx.idx.closes.get(item.id) if item.done_kind == "merged" else None


def _chk_conflict_closed_seat(ctx: _Ctx, item: Item):
    closed, seats = _trailer_close(ctx, item), ctx.idx.live_seats(item.id)
    if closed and seats:
        return (f"conflict: closed by {_short(closed.sha)} on main vs live seat {seats[0].name}"
                f"{f' since {seats[0].since}' if seats[0].since else ''}", (closed.sha, seats[0].name))   # seats: by name
    return None


def _chk_conflict_closed_band(ctx: _Ctx, item: Item):
    closed, rows = _trailer_close(ctx, item), ctx.idx.band_naming.get(item.id, [])
    if closed and rows:
        return f"conflict: closed by {_short(closed.sha)} on main vs open band row {rows[0]}", (closed.sha, rows[0])
    return None


def _chk_conflict_chain_orphan(ctx: _Ctx, item: Item):
    chain, orphans = ctx.idx.chain.get(item.id), ctx.idx.orphans(item.id)
    if chain and orphans:
        b = max(orphans, key=_branch_key)
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
        c = _trailer_close(ctx, item)
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
    rows = sorted(set(rows))
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
    # Chain and seat evidence were ruled out by the rules above; a branch with commits of its own is not (rule 8).
    # There is NO test for a closing trailer here, on purpose: a ``merged`` item that a trailer closed was taken by rule 2,
    # and for ``document``/``run``/``band`` the trailer is not what closes it (the file or the np row is), so commits
    # on main that name it while its own evidence is absent are landed work nobody closed.
    landed = _landed(ctx, item)
    if landed is None or ctx.idx.orphans(item.id):
        return None
    count, last = landed
    return ctx.make(item, "landed_not_closed", _landed_why(count, last), [last.sha])


def _landed(ctx: _Ctx, item: Item) -> tuple[int, MainCommit] | None:
    """``(how many commits on origin/main name the item, the latest of them)``, or None when none does."""
    touched = ctx.idx.touched.get(item.id)
    return (len(touched), max(touched, key=_commit_key)) if touched else None


def _landed_why(count: int, last: MainCommit) -> str:
    return f"{count} commits on main, last {_short(last.sha)}; close it or name what remains"


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
    # A gate past its recheck date (the UTC date of ``now``) makes the item unknown ONLY here, where the item's state
    # would rest on the gate: a not-done item waiting on it. A done or decided item never reaches this rule, and one
    # already needs_you, landing, in flight or landed keeps that state (derive_all audits the gate for it).
    old = _expired_gates(item, ctx.now.date())
    if old:
        ctx.causes[item.id] = "gate_expired"
        return ctx.make(item, "unknown", f"gate \"{old[0].words}\" past its recheck {old[0].recheck}", ())
    ref, label, why = unmet[0]
    more = f" (+{len(unmet) - 1} more)" if len(unmet) > 1 else ""
    return ctx.make(item, "blocked", f"waits on {label}: {why}{more}", [u[0] for u in unmet])


def _rule_ready_with_start(ctx: _Ctx, item: Item) -> State | None:
    orphans = ctx.idx.orphans(item.id)
    if not orphans:
        return None
    b = max(orphans, key=_branch_key)
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
            ctx.causes[item.id] = name
            return ctx.make(item, "unknown", found[0], found[1])
    for _name, rule in _RULES:
        st = rule(ctx, item)
        if st is not None:
            return st
    return ctx.make(item, "unknown", "no rule fired", ())


def derive_state(item: Item, evidence: Evidence, items: Mapping[str, Item], *, now: datetime) -> State:
    """One item's state. ``items`` is the whole store (a dependency is read through to its own state)."""
    return _Ctx({**items, item.id: item}, evidence, now, None).state_of(item.id)


def _id_key(iid: str) -> tuple[str, int, str, str]:
    """Sort key: repo, then the number. The number is compared as digits (length, then text, leading zeros stripped) and
    never converted to an int: a trailer id comes from evidence and may be 5,000 digits or carry a non-ASCII digit
    (``levain#²``), which ``int`` refuses. Anything that is not ASCII digits sorts first; the id breaks a tie
    (levain#01 against levain#1)."""
    repo, _, n = iid.rpartition("#")
    digits = n.lstrip("0") if _DIGITS.fullmatch(n) else ""
    return repo, len(digits), digits, iid


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
    """The whole board: every item's state, the audit list, and everything that could not be read. ``now`` must be
    timezone-aware (a naive one is a TypeError). Every unreadable source is reported on the board itself, an audit
    ``unknown`` entry and a problem line each, whether or not an item needed it: an empty store or a source no item
    reads (the unattributed scan) is never a clean board, and neither is evidence too old, undated or fetched in the
    future (``stale_evidence``). An item is listed in the audit once."""
    now = _utc(now)
    ctx = _Ctx(folded.items, evidence, now, folded.torn_why or ("torn" if folded.torn else None))
    states = [ctx.state_of(i) for i in sorted(folded.items, key=_id_key)]
    audit: list[AuditEntry] = []
    problems = list(folded.problems)
    if folded.torn:
        audit.append(AuditEntry("unknown", None, f"items file torn: {folded.torn_why}"))
    for source in sorted(evidence.unreadable):
        why = f"{source} unreadable: {evidence.unreadable[source]}"
        audit.append(AuditEntry("unknown", None, why))
        problems.append(why)
    stale = _evidence_problem(evidence, now)
    if stale:                           # with nothing to read (or only decided items) an old fetch would otherwise be a clean board
        why = f"stale_evidence: {stale}"
        audit.append(AuditEntry("unknown", None, why))
        problems.append(why)
    for s in states:                    # one entry per item, whatever it carries
        if s.state == "unknown":
            kind = "gate_expired" if ctx.causes.get(s.id) == "gate_expired" else "unknown"
            why = s.reason
            gone = _landed(ctx, folded.items[s.id]) if kind == "gate_expired" else None
            if gone:                    # landed work behind an orphan branch and this gate: rule 6 was skipped, the gate won; say both
                why = f"{why}; {_landed_why(*gone)}"
            audit.append(AuditEntry(kind, s.id, why))
            continue
        old = [] if s.state in ("done", "cut", "parked", "out_of_1_0") else _expired_gates(folded.items[s.id], now.date())
        landed = _landed(ctx, folded.items[s.id]) if s.state in ("blocked", "ready_with_start") else None
        if old:                         # a live state beside an expired gate keeps its state; the gate is the one entry
            audit.append(AuditEntry("gate_expired", s.id, f"gate \"{old[0].words}\" past its recheck {old[0].recheck}"))
        elif s.state == "landed_not_closed":
            audit.append(AuditEntry("landed_not_closed", s.id, s.reason))
        elif landed:                    # rule 6 was ruled out only by an orphan branch: the state follows rules 7-8, the audit still says it
            audit.append(AuditEntry("landed_not_closed", s.id, f"{_landed_why(*landed)}; reads {s.state} because an orphan branch carries more"))
    unattributed = tuple(sorted(evidence.unattributed, key=lambda u: (u.sha, u.subject)))
    for u in unattributed:
        audit.append(AuditEntry("unattributed_work", None, f"{_short(u.sha)} {u.subject}".strip()))
    if not folded.torn:                 # with a torn store most ids would look unknown for a reason already reported
        for iid, where in sorted(_trailer_ids(evidence).items(), key=lambda kv: _id_key(kv[0])):
            if iid not in folded.items:
                audit.append(AuditEntry("unknown_id_in_trailer", iid, f"named by {', '.join(sorted(set(where)))}; the store does not hold it"))
    audit.sort(key=lambda a: AUDIT_KINDS.index(a.kind))
    return Board(SCHEMA, evidence.fetched_at, tuple(states), tuple(audit), unattributed, tuple(problems))
