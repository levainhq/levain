"""levain.desk.board_state — the board stores ITEMS and DERIVES their state (READY_SCHEMA v3).

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
Stdlib only, and no ``levain`` import beyond its stdlib-only sibling :mod:`levain.desk.board_chain` (the one chain checker
both stores' folds share), so the module needs one sibling file (the chain checker) and nothing else: copy both.
"""
from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date as _date, datetime, timezone
from functools import cached_property
from typing import Any, Callable

from levain.desk import board_chain

__all__ = [
    "AUDIT_KINDS", "AuditEntry", "Amendment", "Board", "COLUMNS", "DECISIONS", "DONE_KINDS", "Decision", "Evidence",
    "EVIDENCE_V", "FLAT_EVIDENCE_V", "FLAT_PRODUCER", "FUTURE_SKEW_S", "FlatBlock", "Folded", "GRADE_ORDER", "Item", "Link",
    "MAX_EVIDENCE_AGE_S", "PRODUCER_FUTURE_SKEW_S", "PRODUCER_MAX_AGE_S", "PRODUCER_SOURCES", "SCHEMA", "BandRow", "STATES",
    "STATE_COLUMN", "STATE_KIND", "State", "TIERS", "derive_all", "derive_state", "fold", "parse_timestamp",
]

SCHEMA = "ready-v3"
#: The evidence version this module reads and the producers write: ``seats``, ``band`` and ``waiters`` are lists of producer
#: blocks. The one other version it reads is :data:`FLAT_EVIDENCE_V` (flow's flat form), taken as ONE block from
#: :data:`FLAT_PRODUCER`.
EVIDENCE_V = 2
FLAT_EVIDENCE_V = 1
FLAT_PRODUCER = "flow"
#: A producer block whose ``captured_at`` is older than this is not readable. One named constant so the line moves in one place.
PRODUCER_MAX_AGE_S = 900
#: A producer block captured MORE than this far ahead of ``now`` is not readable ("captured in the future").
PRODUCER_FUTURE_SKEW_S = 60
#: The sources that arrive as producer blocks.
PRODUCER_SOURCES = ("seats", "band", "waiters")
#: How a resolved band row was answered, weakest first. Two producers that resolved one row differently are read at the
#: weaker grade; a grade this list does not hold is weaker than every one it does.
GRADE_ORDER = ("unknown", "unsigned-terminal", "unsigned-phone", "signed-terminal", "signed-phone")
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
    "landed_not_closed": "ready",
    "blocked": "backlog",
    "ready_with_start": "ready",
    "needs_grooming": "backlog",
    "ready": "ready",
    "unknown": "unknown",
}
#: The kind of work a ``ready`` column state is, and ``None`` for every other state: a ``landed_not_closed`` item is ready
#: CLOSE work (Phill np-2f5f3a6c), a ``ready_with_start`` item has a branch to start from, a plain ``ready`` item is new work.
STATE_KIND: dict[str, str | None] = {s: None for s in STATE_COLUMN}
STATE_KIND.update(landed_not_closed="close", ready_with_start="start", ready="work")
STATES = tuple(STATE_COLUMN)
#: The columns in board order (the order readers draw them); a test holds this set to the values of :data:`STATE_COLUMN`.
COLUMNS = ("off", "done", "needs_you", "waiting", "running", "backlog", "ready", "unknown")
AUDIT_KINDS = ("unknown", "landed_not_closed", "gate_expired", "unattributed_work", "unknown_id_in_trailer")
_DECISION_STATE = {"cut": "cut", "parked": "parked", "out-of-1.0": "out_of_1_0"}

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*#[0-9]+")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_UTC_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
_DIGITS = re.compile(r"[0-9]+")
_GIT_SOURCES = ("main", "chain", "branches", "refs")


class _Bad(Exception):
    """A value that is not the shape the contract states (the caller decides what that makes of the file)."""


class FlatBlock(dict):
    """A producer block of flow's flat form (rows with no store id): the ONLY way a block is read as flat. It is a type, not a
    key, so no JSON a producer wrote can claim it; :mod:`levain.desk.board_cli` and the v1 reader below are the only builders."""


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
    acked: tuple[int, ...] = ()                                     # lines an ``ack-torn`` skipped
    torn_lines: tuple[tuple[int, str], ...] = ()                    # (line, sha256 of its bytes) of every torn line, acked or not
    dropped: tuple[tuple[int, int], ...] = ()                       # (line, the line of the ack that dropped it): torn lines the fold skipped
    kept: tuple[tuple[int, int], ...] = ()                          # (line, the line of the ack): chain-broken lines KEPT and applied
    malformed: tuple[int, ...] = ()                                 # lines that are torn by themselves (shape), chain break or not
    keys: Mapping[str, Any] = field(default_factory=dict)           # the ``key`` of the FIRST ``add`` of each id (the item keeps none)
    records: tuple[Mapping[str, Any], ...] = ()                     # the records the fold accepted, in file order (acks excluded)


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
    well-formed record of a known version and op, or the file is torn (a crash mid-append looks exactly like this).
    ``lines`` are the file's lines split on ``\\n`` only, so the numbers and the hashes are the file's own.

    A broken CHAIN (:func:`levain.desk.board_chain.check_chain`: an unchained record after the cutover, a ``prev`` that does
    not match the line before, a cutover that does not commit the legacy prefix) is one more way a line is torn, named in
    ``torn_why``. An ``ack-torn`` record ``{op, line, sha256, why}`` whose ``sha256`` is the hash of that line's bytes makes
    the fold DROP that torn line. An ack of a CHAIN break (the line parses) also carries ``want`` and ``have`` and binds that exact
    break: it is honoured only while both still hold, and the line is then KEPT and applied. An ack of a line that is not torn is
    a problem and skips nothing. Acks are collected only from lines that themselves parse and chain. Acknowledged lines are in
    ``acked``; every torn line, acknowledged or not, is in ``torn_lines``."""
    lines = list(lines)
    items: dict[str, Item] = {}
    problems: list[str] = []
    torn_why = ""
    breaks = board_chain.check_chain(lines).by_line()
    parsed: list[Any] = []                              # per line: the record, or the _Bad that parsing it raised, or None (blank)
    acks: dict[int, list[tuple[int, board_chain.Ack]]] = {}   # target line -> (the ack's own line, ack): ANY that matches accepts it
    for n, raw in enumerate(lines, 1):
        entry: Any = None
        if isinstance(raw, str) and raw.strip():
            try:
                entry = json.loads(raw)                 # the text as it stands, as the checker and the writer parse it
            except RecursionError:                      # a line of 100,000 brackets overflows the parser's stack: torn, not a crash
                entry = _Bad("not JSON (nested too deeply)")
            except ValueError as e:
                entry = _Bad(f"not JSON ({e})")
            if not isinstance(entry, (Mapping, _Bad)):
                entry = _Bad("a record must be a JSON object")
            if isinstance(entry, Mapping) and entry.get("op") == board_chain.ACK_OP:
                try:
                    ack = board_chain.parse_ack(entry)
                except board_chain.AckError as e:
                    entry = _Bad(str(e))
                else:
                    if n not in breaks and ack is not None:
                        acks.setdefault(ack.line, []).append((n, ack))   # only from a line that itself parses and chains
        parsed.append(entry)
    acked: list[int] = []
    torn_lines: list[tuple[int, str]] = []
    dropped: list[tuple[int, int]] = []
    kept: list[tuple[int, int]] = []
    malformed: list[int] = []
    keys: dict[str, Any] = {}
    records: list[Mapping[str, Any]] = []
    for n, raw in enumerate(lines, 1):
        if not isinstance(raw, str):                    # a file read in binary mode must not look like an empty store
            problems.append(f"line {n}: torn: not text ({type(raw).__name__})")
            torn_why = torn_why or f"line {n}: not text ({type(raw).__name__})"
            malformed.append(n)
            continue
        rec = parsed[n - 1]
        if rec is None:
            continue
        brk = breaks.get(n)
        sha = board_chain.line_hash(raw)
        mine = acks.get(n, ())
        bad: _Bad | None = None
        soft: _Soft | None = None
        try:
            if isinstance(rec, _Bad):
                raise rec
            if brk is not None and board_chain.prev_ill_formed(rec):    # ack lines too, as repos_fold judges them
                raise _Bad(board_chain.PREV_BAD)
            if rec.get("op") != board_chain.ACK_OP:
                if brk is None:
                    _apply(rec, items)
                else:
                    _apply(rec, dict(items))            # a chain-broken line is judged for shape on a copy; it applies only if acked
        except _Soft as e:
            soft = e
        except _Bad as e:
            bad = e
        if bad is not None:
            malformed.append(n)
            torn_lines.append((n, sha))
            # a malformed line is dropped by a TORN-style ack of its bytes; a chain ack clears only the chain break (never this)
            hit = next((pos for pos, a in mine if a.sha256 == sha and not a.chain), None)
            if hit is not None:
                acked.append(n)                         # skipped exactly; listed in ``acked``, not a problem
                dropped.append((n, hit))
            else:
                if brk is not None and any(board_chain.honours(a, sha, brk) for _, a in mine):
                    note = f"; its chain break is acknowledged, but the line is torn for itself: acknowledge it as a torn line"
                elif any(a.chain and a.sha256 == sha for _, a in mine):
                    note = "; a chain-style ack names it, but the line is torn for itself (not a chain break): acknowledge it as a torn line"
                elif n in acks:
                    note = "; an ack-torn names it with a different sha256" if brk is None else (
                        "; an ack-torn names it, but not as it stands now")
                else:
                    note = ""
                problems.append(f"line {n}: torn: {bad}{note}")
                torn_why = torn_why or f"line {n}: {bad}" + (f" ({brk.problem})" if brk is not None else "")
            continue
        if brk is not None:
            hit = next((pos for pos, a in mine if board_chain.honours(a, sha, brk)), None)
            if hit is None:
                changed = "; an ack-torn names it, but not this break as it stands now" if n in acks else ""
                problems.append(f"line {n}: torn: {brk.problem}{changed}")
                torn_why = torn_why or f"line {n}: {brk.problem}{changed}"
                torn_lines.append((n, sha))
                continue
            torn_lines.append((n, sha))
            acked.append(n)                             # a chain break acknowledged: the record is KEPT, and applied
            kept.append((n, hit))
            if rec.get("op") != board_chain.ACK_OP:
                try:
                    _apply(rec, items)
                except _Soft as e:
                    soft = e
        if rec.get("op") == board_chain.ACK_OP:
            if n in acks and brk is None:
                problems.append(f"line {n}: ack-torn names this line, which is not torn")
            continue
        if n in acks and brk is None:
            problems.append(f"line {n}: ack-torn names this line, which is not torn")
        if soft is not None:
            problems.append(f"line {n}: {soft}")
        records.append(rec)
        if rec.get("op") == "add" and isinstance(rec.get("id"), str):
            keys.setdefault(rec["id"], rec.get("key"))
    return Folded(items, tuple(problems), bool(torn_why), torn_why, tuple(acked), tuple(torn_lines), tuple(dropped), tuple(kept),
                  tuple(malformed), keys, tuple(records))


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


@dataclass(frozen=True)
class BandRow:
    """One band row a producer read: ``state`` is ``open`` or ``resolved``; ``by`` and ``grade`` (``signed-phone``,
    ``signed-terminal``, ``unsigned-terminal``, ... or ``unknown``) say how a resolved row was answered."""
    id: str
    state: str
    items: tuple[str, ...]
    by: str
    grade: str


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


def _p_band(raw: Any) -> tuple[BandRow, ...]:
    """The flat form: ``{open: {np: [items]}, resolved: {np: by}}``, read as rows (a resolved row has no grade here)."""
    b = _obj(raw, "band")
    if "open" not in b or "resolved" not in b:
        raise _Bad("band needs both open and resolved")
    rows = [BandRow(str(k), "open", _strs(v, "a band row's item list"), "", "unknown") for k, v in _obj(b["open"], "band.open").items()]
    for k, v in _obj(b["resolved"], "band.resolved").items():
        if not isinstance(v, str) or not v.strip():
            raise _Bad("band.resolved values must be non-empty strings (who resolved it)")
        rows.append(BandRow(str(k), "resolved", (), v, "unknown"))
    return tuple(rows)


def _p_band_rows(raw: Any) -> tuple[BandRow, ...]:
    """A producer block's rows: ``{id, state: open|resolved, items, by, grade}``, every key required. A row missing one, or
    holding one of the wrong type, is malformed (never read as a default); a resolved row names who resolved it."""
    out = []
    for r in _seq(raw, "band rows"):
        o = _obj(r, "a band row")
        state = _req_str(o, "state")
        if state not in ("open", "resolved"):
            raise _Bad(f"a band row's state is open or resolved, not {state!r}")
        by, grade = _need(o, "by", "a band row"), _need(o, "grade", "a band row")
        if not isinstance(by, str) or not isinstance(grade, str) or not grade.strip():
            raise _Bad("a band row's by and grade must be strings (grade not empty)")
        if state == "resolved" and not by.strip():
            raise _Bad("a resolved band row names no resolver")
        out.append(BandRow(_req_str(o, "id"), state, _strs(_need(o, "items", "a band row"), "a band row's items"), by, grade))
    return tuple(out)


def _p_documents(raw: Any) -> dict[str, bool]:
    d = _obj(raw, "documents")
    if not all(isinstance(v, bool) for v in d.values()):
        raise _Bad("documents values must be true or false")
    return {str(k): v for k, v in d.items()}


def _p_unattributed(raw: Any) -> tuple[Unattributed, ...]:
    return tuple(Unattributed(_req_str(u := _obj(r, "a commit"), "sha"), str(u.get("subject") or ""))
                 for r in _seq(raw, "unattributed"))


@dataclass(frozen=True)
class _Block:
    """One producer's block for one source, judged. ``rows`` are its claims: they stand whenever the block's store, age and
    every row were readable, whatever its own ``complete`` says (a producer that saw only part still saw those rows).
    ``why`` is None when the block is also whole (its own ``complete`` true) and otherwise says why absence cannot rest on it."""
    producer: str
    rows: tuple[Any, ...]
    why: str | None


def _read_block(source: str, raw: Any, *, now: datetime | None, store_id: str | None) -> _Block:
    if not isinstance(raw, Mapping):
        return _Block("?", (), "block is not an object")
    producer = raw.get("producer")
    if not isinstance(producer, str) or not producer:
        return _Block("?", (), "block names no producer")
    flat = isinstance(raw, FlatBlock)

    def bad(why: str) -> _Block:
        return _Block(producer, (), why)

    if "form" in raw and not flat:
        return bad("block carries a form key (only the flat reader may set one)")
    sid = raw.get("store_id")
    if sid is not None and not isinstance(sid, str):
        return bad("store_id is not a string")
    if store_id is not None and sid != store_id and not (flat and sid is None):
        return bad("evidence from another store" if sid is not None else "names no store_id")
    if now is None:
        if not flat:
            return bad("no clock to judge captured_at")
    else:
        ts = parse_timestamp(raw.get("captured_at")) if isinstance(raw.get("captured_at"), str) else None
        if ts is None:
            return bad("no captured_at")
        age = (_utc(now) - ts).total_seconds()
        if age > PRODUCER_MAX_AGE_S:
            return bad(f"stale {int(age // 60)} min")
        if age < -PRODUCER_FUTURE_SKEW_S:
            return bad("captured in the future")
    parse = {"seats": _p_seats, "waiters": _p_waiters, "band": _p_band if flat else _p_band_rows}[source]
    try:
        rows = tuple(parse(raw.get("rows")))
    except _Bad as e:
        return bad(f"malformed row: {e}")
    if not isinstance(raw.get("complete"), bool):
        return bad("complete is not true or false")
    if not raw["complete"]:
        why = raw.get("why")
        why = why.strip() if isinstance(why, str) else ""
        return _Block(producer, rows, "incomplete" + (f": {why}" if why else ""))
    return _Block(producer, rows, None)


def _compose(source: str, obj: Mapping[str, Any], *, now: datetime | None, store_id: str | None,
             expected: Sequence[str] | None, failures: Mapping[str, str]) -> tuple[tuple[Any, ...], str | None]:
    """``(rows, why absence cannot be concluded)`` for one source. A row from ANY readable block stands, whoever wrote it and
    whether or not its producer is expected or failed; absence is concluded (``why`` None) only when every expected producer
    has at least one block, every block of it whole, and it has not failed. Two blocks from one producer are one producer's
    rows together, whole only if each is."""
    if obj.get("evidence_v") == FLAT_EVIDENCE_V:
        # flow's flat form: one block from FLAT_PRODUCER, captured when the evidence was fetched
        raws: Any = [FlatBlock(producer=FLAT_PRODUCER, store_id=None, captured_at=obj.get("fetched_at"),
                               complete=True, rows=obj[source])] if source in obj else []
    else:
        raws = obj.get(source)
        if raws is None:
            raws = []
        elif not isinstance(raws, (list, tuple)):
            return (), "blocks must be a list"
    blocks = [_read_block(source, b, now=now, store_id=store_id) for b in raws]
    named: dict[str, list[_Block]] = {}
    for b in blocks:
        named.setdefault(b.producer, []).append(b)
    names = list(expected) if expected is not None else [p for p in named if p != "?"]
    why: list[str] = []
    if "?" in named:                            # a block that names no producer cannot be matched to one that is expected
        why.append(f"a block is unreadable ({named['?'][0].why})")
    for p in names:
        got = named.get(p, [])
        if p in failures:
            why.append(f"producer {p} unreadable ({failures[p].strip() or 'no reason given'})")
        elif not got:
            why.append(f"producer {p} unreadable (no block supplied)")
        elif (first := next((b.why for b in got if b.why is not None), None)) is not None:
            why.append(f"producer {p} unreadable ({first})")
    if not names:
        why.append("not supplied")
    rows = tuple(r for b in blocks for r in b.rows)
    return rows, "; ".join(why) or None


@dataclass(frozen=True)
class Evidence:
    """The merged evidence object (``evidence_v`` :data:`EVIDENCE_V`, or flow's flat form), parsed. Built by :meth:`from_dict`, which never raises: a source
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
    band_grade: Mapping[str, str] = field(default_factory=dict)       # resolved band row -> how it was answered

    @classmethod
    def from_dict(cls, obj: Any, *, now: datetime | None, store_id: str | None = None) -> "Evidence":
        """``now`` judges each producer block's ``captured_at`` (a block of the v2 form read with no ``now`` is unreadable;
        flow's flat form is then judged by the evidence's own age only). ``store_id`` is the store's id: a block naming
        another one is unreadable, and with None (the store has no id) a block naming any id is readable. The object may
        carry ``producers`` (the producers expected for every source, or ``{source: [producers]}``) and ``producer_failures`` (``{producer: why}`` for
        one that could not be read at all); without ``producers`` the expected ones are those that supplied a block. Positive rows
        of every readable block stand whatever else fails; a source that is unreadable takes only the absence."""
        unreadable: dict[str, str] = {}
        if not isinstance(obj, Mapping):
            unreadable["evidence"] = "not an object"
            obj = {}
        elif type(obj.get("evidence_v")) is not int or obj["evidence_v"] not in (FLAT_EVIDENCE_V, EVIDENCE_V):
            unreadable["evidence"] = f"evidence_v {obj.get('evidence_v')!r} is not {FLAT_EVIDENCE_V} or {EVIDENCE_V}"
        given = obj.get("unreadable")
        if given is not None:                           # {source: reason} or nothing: any other shape fails closed
            pairs = given.items() if isinstance(given, Mapping) else [(n, "") for n in given] if isinstance(given, (list, tuple)) else []
            malformed = not isinstance(given, Mapping)
            for k, v in pairs:
                named = isinstance(k, str) and bool(k)
                malformed = malformed or not named or not isinstance(v, str)
                if named:                               # a source that was NAMED stays unreadable even if its reason is junk or empty
                    unreadable[k] = (v.strip() or "unreadable (no reason given)") if isinstance(v, str) else "unreadable"
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
        given_p = obj.get("producers")          # a list (expected for every source) or {source: [producers]}; absent: those that supplied a block

        def expected_for(s: str) -> list[str] | None:
            names = given_p.get(s) if isinstance(given_p, Mapping) else given_p
            return [p for p in names if isinstance(p, str) and p] if isinstance(names, (list, tuple)) else None
        fails = obj.get("producer_failures")
        fails = {str(k): str(v) for k, v in fails.items()} if isinstance(fails, Mapping) else {}
        composed = {s: _compose(s, obj, now=now, store_id=store_id, expected=expected_for(s), failures=fails) for s in PRODUCER_SOURCES}
        for s, (_rows, why) in composed.items():            # the reason absence cannot be concluded: the source reads unreadable
            if why is None:
                continue
            if s not in unreadable:
                unreadable[s] = why
            elif why != "not supplied":                     # a declared reason and a block's own both stand
                unreadable[s] = f"{unreadable[s]}; {why}"
        # An ``unreadable`` declaration, a failed or an incomplete producer takes the ABSENCE of a source; the positive rows of
        # every readable block stand beside it (a source its writer called unreadable still proves what it did show).
        seats, waiters = composed["seats"][0], composed["waiters"][0]
        band_open: dict[str, tuple[str, ...]] = {}
        resolutions: dict[str, set[tuple[str, str]]] = {}
        for r in composed["band"][0]:
            if r.state == "open":
                band_open[r.id] = tuple(dict.fromkeys(band_open.get(r.id, ()) + r.items))
            else:
                resolutions.setdefault(r.id, set()).add((r.by, r.grade))
        band_resolved: dict[str, str] = {}
        band_grade: dict[str, str] = {}
        for rid, pairs in resolutions.items():              # two producers that disagree: both are named, the weaker grade stands
            ordered = sorted(pairs)
            band_resolved[rid] = ordered[0][0] if len(ordered) == 1 else "; ".join(f"{by} ({grade})" for by, grade in ordered)
            band_grade[rid] = min((g for _, g in ordered), key=lambda g: (GRADE_ORDER.index(g) if g in GRADE_ORDER else -1, g))
        documents = src("documents", _p_documents, {})
        unattributed = src("unattributed", _p_unattributed, ())    # no item needs it, so the BOARD reports it unread
        raw_fetched = obj.get("fetched_at")
        fetched_at = raw_fetched if isinstance(raw_fetched, str) and raw_fetched.strip() else None
        return cls(fetched_at, parse_timestamp(fetched_at), unreadable, main, chain, branches, refs_merged[0],
                   refs_merged[1], seats, band_open, band_resolved, waiters, documents, unattributed, band_grade)

    def unreadable_reason(self, source: str) -> str | None:
        """Why ``source`` cannot be trusted, or None. ``git`` stands for the four git-derived sources and ``notes`` for
        the trailers carried in refs/notes/board (part of ``main``)."""
        for key in (source, "git" if source in _GIT_SOURCES else None, "notes" if source == "main" else None):
            if key and key in self.unreadable:
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
    kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "repo": self.repo, "title": self.title, "tier": self.tier, "state": self.state,
                "column": self.column, "reason": self.reason, "refs": list(self.refs), "as_of": self.as_of,
                "kind": self.kind}


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
    def __init__(self, items: Mapping[str, Item], ev: Evidence, now: datetime, torn: str | None,
                 repo_ev: Mapping[str, Evidence] | None = None) -> None:
        self.items = items
        self.ev = ev
        self.idx = ev.index
        self.repo_ev = repo_ev              # None: one repo's evidence reads every item (the single-repo board)
        self.now = _utc(now)
        self.torn = torn
        self.memo: dict[str, State] = {}
        self.stack: set[str] = set()
        self.causes: dict[str, str] = {}      # item id -> the name of the check that made it unknown
        self.notes: dict[str, list[str]] = {}     # item id -> the checks that were skipped for it, each with its reason
        self._cycles: dict[str, tuple[str, ...] | None] = {}

    def make(self, item: Item, state: str, reason: str, refs: Iterable[str] = ()) -> State:
        return State(item.id, item.repo, item.title, item.tier if item.tier in TIERS else None, state,
                     STATE_COLUMN[state], reason, tuple(refs), self.ev.fetched_at, STATE_KIND[state])

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
        saved = (self.ev, self.idx)         # a dependency judged under ITS repo must not leave its caller under that repo
        self.focus(item)
        try:
            if iid in self.stack:           # a guard behind the cycle check: a cycle member never reads a dependency
                return self.make(item, "unknown", f"cycle through {iid}", (iid,))
            self.stack.add(iid)
            try:
                st = _compute(self, item)
            finally:
                self.stack.discard(iid)
            self.memo[iid] = st
            return st
        finally:
            self.ev, self.idx = saved

    def note(self, item: Item, source: str) -> None:
        """Say that a check which needs ``source``'s absence was not run for ``item``, because that source is unreadable."""
        line = f"{item.id}: not checked: {source} {self.ev.unreadable[source]}"
        if line not in self.notes.setdefault(item.id, []):
            self.notes[item.id].append(line)

    def focus(self, item: Item) -> None:
        """Point ``ev``/``idx`` at the evidence of the repo ``item`` belongs to. A repo that was not read is not judged
        under another repo's evidence (that reads "no evidence of work", which is health): it reads unknown."""
        if self.repo_ev is None:
            return
        ev = self.repo_ev.get(item.repo)
        if ev is None:
            ev = Evidence.from_dict({"unreadable": {"evidence": f"repo {item.repo} not read (no evidence supplied for it)"}}, now=self.now)
        self.ev, self.idx = ev, ev.index

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
    need = ["evidence", "main", "chain", "branches"]        # seats, band and waiters are judged per predicate (_absence_reason)
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
    if gone and "band" in ev.unreadable:        # "not in the band" is an absence: the rows that were read are not the whole band
        ctx.note(item, "band")
        return None
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
    if closed and seats:                        # a seat that was read stands, whatever else was not
        return (f"conflict: closed by {_short(closed.sha)} on main vs live seat {seats[0].name}"
                f"{f' since {seats[0].since}' if seats[0].since else ''}", (closed.sha, seats[0].name))   # seats: by name
    if closed and "seats" in ctx.ev.unreadable:
        ctx.note(item, "seats")
    return None


def _chk_conflict_closed_band(ctx: _Ctx, item: Item):
    closed, rows = _trailer_close(ctx, item), ctx.idx.band_naming.get(item.id, [])
    if closed and rows:
        return f"conflict: closed by {_short(closed.sha)} on main vs open band row {rows[0]}", (closed.sha, rows[0])
    if closed and "band" in ctx.ev.unreadable:
        ctx.note(item, "band")
    return None


def _chk_conflict_chain_orphan(ctx: _Ctx, item: Item):
    chain = ctx.idx.chain.get(item.id)
    if chain and "seats" in ctx.ev.unreadable and not _trailer_close(ctx, item):
        # an orphan is a branch no live seat holds: with the seats not all read, a branch with commits of its own may be held
        if any(b.ahead > 0 for b in ctx.idx.branches.get(item.id, ())):
            ctx.note(item, "seats")
        return None
    orphans = ctx.idx.orphans(item.id)
    if chain and orphans and not _trailer_close(ctx, item):     # a closing trailer on main wins: the orphan is a note on done
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
        if not c:
            return None
        orphans = [] if "seats" in ev.unreadable else ctx.idx.orphans(item.id)     # an orphan is a branch no seat holds
        if "seats" in ev.unreadable and any(b.ahead > 0 for b in ctx.idx.branches.get(item.id, ())):
            ctx.note(item, "seats")             # the orphan note was not checked: say so
        note = f"; orphan branch {(b := max(orphans, key=_branch_key)).name} {_short(b.tip)} carries {b.ahead} commits main lacks" if orphans else ""
        return ctx.make(item, "done", f"closed by {_short(c.sha)} ({c.date or 'no date'}){note}", (c.sha,))
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
                # a ruling that the work will not happen: the wait can never end, a human decides the dependent
                shown = "dependency cut: decide" if s.state in ("cut", "out_of_1_0") else s.state
                out.append((dep.id, l.label, f"{shown} ({s.reason})" if s.reason else shown))
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


#: Per resulting state, the sources whose ABSENCE the state rests on: every higher-precedence state whose predicate reads a
#: producer source (needs_you: band; landing: the waiters; in_flight: the seats) had to be absent for this one to be reached.
#: A positive claim (a seat, an open row, a waiter) is its own state's evidence, but it is still overridden by the states
#: above it, so ``in_flight`` rests on the band and the waiters not holding it. ``done`` rests on none, except a ``band``
#: item whose row is "not still open".
_ABSENCE_OF: dict[str, tuple[str, ...]] = {
    "needs_you": (), "landing": ("band",), "in_flight": ("band", "waiters"),
    "landed_not_closed": PRODUCER_SOURCES, "blocked": PRODUCER_SOURCES, "ready_with_start": PRODUCER_SOURCES,
    "needs_grooming": PRODUCER_SOURCES, "ready": PRODUCER_SOURCES,
}


def _absence_reason(ev: Evidence, item: Item, state: str) -> str | None:
    """Why ``state`` cannot be concluded, or None: the absence it rests on needs every expected producer readable and complete."""
    if state == "done" and item.done_kind == "band":      # "its np row is not still open" is an absence too
        sources: tuple[str, ...] = ("band",)
    else:
        sources = _ABSENCE_OF.get(state, ())
    bad = [f"{s}: {ev.unreadable[s]}" for s in sources if s in ev.unreadable]
    return "; ".join(bad) or None


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
            if item.decision is None and (why := _absence_reason(ctx.ev, item, st.state)):
                ctx.causes[item.id] = "absence"
                return ctx.make(item, "unknown", why, ())
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


#: Supplied by flow, the same for every repo's evidence. Not "documents": a path is repo-relative, each repo's is its own
#: (board_cli stats the primary's and the others' separately and merges flow's staged paths into the primary only).
_FLOW_SOURCES = frozenset({"seats", "band", "waiters"})


def _trailer_ids(ev: Evidence, git_only: bool = False) -> dict[str, list[str]]:
    """Every item id a commit, branch, seat or waiter names, with where it was named. ``git_only`` leaves out the seats
    and waiters: flow supplies those board-wide, so a foreign repo's evidence repeats the home's."""
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
    if git_only:
        return seen
    for s in ev.seats:
        for iid in s.items:
            seen.setdefault(iid, []).append(f"seat {s.name}")
    for w in ev.waiters:
        for iid in w.items:
            seen.setdefault(iid, []).append(f"waiter {w.branch}")
    return seen


def _in_scope(ctx: _Ctx, items: Mapping[str, Item], repo_evidence: Mapping[str, Evidence] | None) -> set[str]:
    """The ids the board lists. With cross-repo evidence, an item of a repo that was not read is listed (unknown) only if
    an item of a read repo links it through an undecided link: ONE hop, the dependency the read item waits on (what that
    foreign item waits on is not the board's business, and a decided item's links no longer matter, so a repo only a
    decided item mentions is out of scope, not a dirty board). Every id that is left off is named by the caller (a problem
    line): nothing leaves the board silently. If no item belongs to a read repo (a misnamed repo) or the store is torn
    (a retained decision may have lost the ``reopen`` that undoes it) every item stays listed, each reading unknown."""
    if repo_evidence is None or ctx.torn:
        return set(items)
    seen = {i for i, it in items.items() if it.repo in repo_evidence}
    if not seen:
        return set(items)
    for iid in list(seen):
        seen.update(dep for dep in ctx._deps(iid) if dep in items)
    return seen


def derive_all(folded: Folded, evidence: Evidence, *, now: datetime,
               repo_evidence: Mapping[str, Evidence] | None = None, primary_repo: str | None = None,
               label_primary: bool = False, unread_repos: Mapping[str, str] | None = None) -> Board:
    """The whole board: every item's state, the audit list, and everything that could not be read. ``now`` must be
    timezone-aware (a naive one is a TypeError). Every unreadable source is reported on the board itself, an audit
    ``unknown`` entry and a problem line each, whether or not an item needed it: an empty store or a source no item
    reads (the unattributed scan) is never a clean board, and neither is evidence too old, undated or fetched in the
    future (``stale_evidence``). An item is listed in the audit once.

    ``label_primary`` puts the primary's repo name on its own git audit lines too (``<repo>: ...``, as every other repo's
    have), for a board with no privileged repo; the flow-supplied sources stay unlabeled (they are board-wide).
    ``unread_repos`` (``{repo: why}``) names repos in ``repo_evidence`` that were NOT read (their evidence holds only
    unreadable sources, so their items read unknown): each says its ``why`` once, an audit entry and a problem line, in
    place of one line per source.

    ``repo_evidence`` (``{repo: Evidence}``) is the cross-repo board: an item, and each dependency it waits on, is read
    from the evidence of the repo it belongs to, and an item whose repo has no entry reads unknown. ``evidence`` is the
    primary one (it names the board's ``as_of``); put it in the mapping too if its repo owns items. ``primary_repo``
    names its key there (the other repos are every other key); left None it is the key holding that same object. Left
    None, one repo's evidence reads every item. An item of a repo that is not in ``repo_evidence`` is left off the board
    unless an item of a read repo links it through an undecided link (one hop; it then reads unknown); each such repo is
    named in a problem line. Known edge: with ``primary_repo`` None and a primary evidence object that is not a value of
    ``repo_evidence``, the home repo is read as a foreign one (``board_cli`` always passes both, so the CLI cannot reach it)."""
    now = _utc(now)
    ctx = _Ctx(folded.items, evidence, now, folded.torn_why or ("torn" if folded.torn else None), repo_evidence)
    if primary_repo is None:
        primary_repo = next((r for r, e in (repo_evidence or {}).items() if e is evidence), None)
    others = [(r, e) for r, e in sorted((repo_evidence or {}).items()) if r != primary_repo]
    shown = _in_scope(ctx, folded.items, repo_evidence)
    states = [ctx.state_of(i) for i in sorted(shown, key=_id_key)]
    audit: list[AuditEntry] = []
    problems = list(folded.problems)
    left_off: dict[str, list[str]] = {}
    for iid in sorted(set(folded.items) - shown, key=_id_key):      # off the board by scope, never silently: one line per repo
        left_off.setdefault(folded.items[iid].repo, []).append(iid)
    for repo, ids in sorted(left_off.items()):
        problems.append(f"repo {repo} not read: {len(ids)} item(s) left off the board ({', '.join(ids[:5])}{', ...' if len(ids) > 5 else ''}); "
                        "no item of a read repo waits on them")
    if folded.torn:
        audit.append(AuditEntry("unknown", None, f"items file torn: {folded.torn_why}"))
    pfx = f"{primary_repo}: " if label_primary and primary_repo else ""
    for source in sorted(evidence.unreadable):
        why = f"{pfx if source not in _FLOW_SOURCES else ''}{source} unreadable: {evidence.unreadable[source]}"
        audit.append(AuditEntry("unknown", None, why))
        problems.append(why)
    for st in states:                   # a check skipped because a source was not read is said, never silently passed
        problems.extend(ctx.notes.get(st.id, ()))
    stale = _evidence_problem(evidence, now)
    if stale:                           # with nothing to read (or only decided items) an old fetch would otherwise be a clean board
        why = f"stale_evidence: {pfx}{stale}"
        audit.append(AuditEntry("unknown", None, why))
        problems.append(why)
    for s in states:                    # one entry per item, whatever it carries
        if s.state == "unknown":
            kind = "gate_expired" if ctx.causes.get(s.id) == "gate_expired" else "unknown"
            why = s.reason
            ctx.focus(folded.items[s.id])
            gone = _landed(ctx, folded.items[s.id]) if kind == "gate_expired" else None
            if gone:                    # landed work behind an orphan branch and this gate: rule 6 was skipped, the gate won; say both
                why = f"{why}; {_landed_why(*gone)}"
            audit.append(AuditEntry(kind, s.id, why))
            continue
        old = [] if s.state in ("done", "cut", "parked", "out_of_1_0") else _expired_gates(folded.items[s.id], now.date())
        ctx.focus(folded.items[s.id])
        landed = _landed(ctx, folded.items[s.id]) if s.state in ("blocked", "ready_with_start") else None
        if old:                         # a live state beside an expired gate keeps its state; the gate is the one entry
            audit.append(AuditEntry("gate_expired", s.id, f"gate \"{old[0].words}\" past its recheck {old[0].recheck}"))
        elif s.state == "landed_not_closed":
            audit.append(AuditEntry("landed_not_closed", s.id, s.reason))
        elif landed:                    # rule 6 was ruled out only by an orphan branch: the state follows rules 7-8, the audit still says it
            audit.append(AuditEntry("landed_not_closed", s.id, f"{_landed_why(*landed)}; reads {s.state} because an orphan branch carries more"))
    for repo, ev in others:             # the other repos' own sources, each said under its repo
        if unread_repos and repo in unread_repos:
            why = f"{repo}: {unread_repos[repo]}"
            audit.append(AuditEntry("unknown", None, why))
            problems.append(why)
            continue
        for source in sorted(ev.unreadable):
            if source in _FLOW_SOURCES and source in evidence.unreadable:
                continue                # the flow-supplied extra reaches every repo's evidence (every item fails closed); said once,
                                        # by the primary (keyed on the source, not the text: two repos' git can fail with the same
                                        # words). A foreign copy that failed alone (the primary read it) is that repo's to say.
            why = f"{repo}: {source} unreadable: {ev.unreadable[source]}"
            audit.append(AuditEntry("unknown", None, why))
            problems.append(why)
        stale = _evidence_problem(ev, now)
        if stale:
            why = f"stale_evidence: {repo}: {stale}"
            audit.append(AuditEntry("unknown", None, why))
            problems.append(why)
    unattributed = tuple(sorted(evidence.unattributed, key=lambda u: (u.sha, u.subject)))
    for u in unattributed:
        audit.append(AuditEntry("unattributed_work", None, f"{pfx}{_short(u.sha)} {u.subject}".strip()))
    if pfx:
        unattributed = tuple(Unattributed(u.sha, f"{pfx}{u.subject}") for u in unattributed)
    for repo, ev in others:
        for u in sorted(ev.unattributed, key=lambda u: (u.sha, u.subject)):
            audit.append(AuditEntry("unattributed_work", None, f"{repo}: {_short(u.sha)} {u.subject}".strip()))
            unattributed += (Unattributed(u.sha, f"{repo}: {u.subject}"),)
    if not folded.torn:                 # with a torn store most ids would look unknown for a reason already reported
        for iid, where in sorted(_trailer_ids(evidence).items(), key=lambda kv: _id_key(kv[0])):
            if iid not in folded.items:
                audit.append(AuditEntry("unknown_id_in_trailer", iid, f"{pfx}named by {', '.join(sorted(set(where)))}; the store does not hold it"))
        for repo, ev in others:
            for iid, where in sorted(_trailer_ids(ev, git_only=True).items(), key=lambda kv: _id_key(kv[0])):
                if iid not in folded.items:
                    audit.append(AuditEntry("unknown_id_in_trailer", iid, f"{repo}: named by {', '.join(sorted(set(where)))}; the store does not hold it"))
        for repo, ev in (repo_evidence or {}).items():      # work named for an item of ANOTHER repo is read by neither: say it
            for iid, where in sorted(_trailer_ids(ev).items(), key=lambda kv: _id_key(kv[0])):
                it = folded.items.get(iid)
                git = sorted({w for w in where if w.split(" ", 1)[0] in ("main", "chain", "branch")})   # seats and waiters are flow-supplied, board-wide
                if it is not None and it.repo != repo and git and iid in shown:
                    audit.append(AuditEntry("unknown_id_in_trailer", iid, f"{repo}: named by {', '.join(git)}; the item belongs to repo {it.repo}, whose evidence does not hold that work"))
    audit.sort(key=lambda a: AUDIT_KINDS.index(a.kind))
    return Board(SCHEMA, evidence.fetched_at, tuple(states), tuple(audit), unattributed, tuple(problems))
