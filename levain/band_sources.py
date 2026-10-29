"""levain.band_sources — the band's AUTHORITY sources (Q17-ALL S3, design §4 D12 and §5 "Authority: the trust boundary").

Beside the open questions the band lists, each as one numbered item: a consent request that is UNDECIDED, an open
coordinator SIGN record, and an open budget-extend SIGN. An ANSWER record (the coordinator's escalation of a question)
is never an item of its own: it rides its question's item (:attr:`Extras.escalations`). KNOW, RECEIPT, review items
and notes are not read here.

NOTHING HERE IS AUTHORITY (SIG-ONLY, D8). This module reads three stores read-only (``queue.db``, the coordinator's
``dispatch.db``) and builds display rows plus the TEXT of the existing signing ceremony each item is settled by
(:func:`sheet_text`). It records nothing and rules nothing: a decision on one of these items exists only as the
signature the existing path verifies (the phone ``question_rule`` sheet, ``levain consent rule``, a levain sign request
on the cockpit's sign sheet, ``levain envelope sign``). A sign request is named in words and, when the cockpit has
stored its origin (:mod:`levain.consent.cockpit_origin`), carries its :func:`levain.consent.sign.deep_link`; with no
stored origin it carries no link.

Rec (D11, Q2 ruled (a)): an item shows the coordinator's rec when its record carries one, else the literal text
:data:`NO_REC` (``rec: none (authority item)``) for a raw gate request.

A command the operator copies (S3 fix round 1): every value in it that a record carries (a session id, an agent, a gap
number, a thread id, a request id) is something an agent could have written, and the operator runs the line in his
shell. So each such value must be one plain token (:data:`TOKEN`: letters, digits and ``._:@+=-``, not starting with
``-``); a value outside it renders NO command, only words saying why and naming the item's card. The line is then
built with :func:`shlex.quote` on every argument as well (a no-op on a plain token; a second wall, not the first).

S3 fix round 2:
- A levain sign request shows ITS OWN options with their letters (:func:`sign_request_menu`), then its deny; a plain
  one shows exactly ``sign`` and ``deny``. Each letter carries its action and option key explicitly, so a pick opens
  the action shown beside the letter. A letter past them, or a request changed since (another version), opens
  nothing and says why (:class:`NotOffered`). Deny goes to the request's own deny on its sheet; it is never a
  signature.
- One band key per underlying request (:attr:`Extra.band_key`): a consent request is ``("consent", id)`` whether it is
  shown as UNDECIDED or through a coordinator SIGN, so a SIGN filed later enriches the item and keeps its number.
- One store, one snapshot, one deadline: ``queue.db``, ``questions.db`` and ``dispatch.db`` are each opened once per
  read, their lock wait capped to what is left of the caller's deadline; a store held past it is "not read".
Stdlib plus levain's own readers.
"""
from __future__ import annotations

import contextlib
import re
import shlex
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from levain.coordinator.review import NO_REC

__all__ = ["Extra", "Extras", "NO_REC", "NotOffered", "TOKEN", "UnsafeValue", "consent_rule_command", "read_extras",
           "rec_letter_of", "sheet_text", "sign_request_menu", "SOURCES"]

SOURCES = ("consent", "sign", "extend")
_LETTERS = "abcdef"
_FIELD = 300
_OPTION_SHOWN = 80
_REC_LETTER = re.compile(r"\s*([a-f])(?![A-Za-z0-9])")
_RO_TIMEOUT = 2.0
#: one argument of a command the operator copies into his shell, when its value came from a record
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+=-]{0,127}")
_TOKEN_WORDS = "letters, digits and ._:@+=- only, not starting with -"


@dataclass(frozen=True)
class Extra:
    """One authority item as the band shows it. ``options`` are its labels in display order (a, b, ...); ``rec`` the
    text after ``rec: `` and ``rec_letter`` the letter it names (``None`` when it names none). ``unreadable`` is set for
    a record that cannot be read (listed, never dropped, D13). ``data`` is what the ceremony text needs: for a levain
    sign request, ``menu`` is one ``(action, option key)`` per displayed letter (S3 fix round 2), so a letter is
    mapped to the action the operator SAW beside it, never to a position in the request file. ``key`` is the band key
    (S3 fix round 2, codex 3): ONE per underlying request, so a consent request keeps its number whether it is shown
    as UNDECIDED or through the coordinator's SIGN pointing at it; ``None`` is ``(source, ref)``."""
    source: str
    ref: str
    filed: str
    tag: str
    what: str
    options: tuple[str, ...]
    rec: str
    rec_letter: str | None
    blocks: str
    origin: str
    unreadable: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    key: tuple[str, str] | None = None

    @property
    def band_key(self) -> tuple[str, str]:
        return self.key if self.key is not None else (self.source, self.ref)


@dataclass
class Extras:
    """What the authority sources hold now. ``items``: open ones, oldest first. ``complete``: the band-key sources
    read in full (a numbered key of such a source that is absent from ``items`` has closed; a source not read in full
    closes nothing). A ``consent`` key can be listed by the consent queue or by a coordinator SIGN, so ``consent`` is
    complete only when BOTH were read. ``escalations``: question id -> ``(coordinator's text, rec)`` of an open ANSWER
    record. ``failed``: one line per source that could not be read (the band says so, never drops it silently)."""
    items: list[Extra] = field(default_factory=list)
    complete: set[str] = field(default_factory=set)
    escalations: dict[str, tuple[str, str]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


def rec_letter_of(rec: str | None, options: int) -> str | None:
    """The display letter a rec text starts with (``a: tests green``), when that option exists; else ``None``."""
    m = _REC_LETTER.match(rec or "")
    if m is None or _LETTERS.index(m.group(1)) >= options:
        return None
    return m.group(1)


def _wait(deadline: float | None, most: float = _RO_TIMEOUT) -> float:
    """How long one connection may wait on a lock: ``most``, capped to what is left before ``deadline``
    (``time.monotonic()``; ``None``: no deadline). Past it, 0: a store no one holds still reads, a held one fails."""
    if deadline is None:
        return most
    return max(0.0, min(most, deadline - time.monotonic()))


def _ro(path: Path, deadline: float | None = None) -> sqlite3.Connection:
    wait = _wait(deadline)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=wait, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(wait * 1000)}")
    except BaseException:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def _snapshot(path: Path, deadline: float | None) -> Iterator[sqlite3.Connection | None]:
    """ONE read-only snapshot of ``path`` for every read of that store in this pass (its lock wait capped by
    ``deadline``), ``None`` when there is no such file. A lock held past the deadline raises SQLite's busy error."""
    try:
        empty = path.stat().st_size == 0
    except FileNotFoundError:
        empty = True
    if empty:
        yield None
        return
    conn = _ro(path, deadline)
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()     # the snapshot starts here, or busy raises
        yield conn
    finally:
        conn.close()


def _line(text: Any, limit: int = _FIELD) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def _gap_of(conn: sqlite3.Connection | None, request_id: str) -> int | None:
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT gap FROM gaps WHERE request_id = ?", (request_id,)).fetchone()
        return None if row is None else int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _consent(qdir: Path, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    from levain.coordinator import morning

    if conn is None:
        return []                                 # no queue.db: nothing is undecided
    return morning._undecided(qdir, conn)


def _records(qdir: Path, clock: Any = None, *, deadline: float | None = None,
             coord_conn: sqlite3.Connection | None = None, queue_conn: sqlite3.Connection | None = None,
             questions_conn: sqlite3.Connection | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """``(waiting_on_you's answer, reply_id -> filed time)`` of the coordinator's store; empty when there is none.
    ``coord_conn``: a coordinator snapshot the caller holds (the morning card's), else one of our own whose lock wait
    is capped by ``deadline``. The queue and question stores are each read through the ONE snapshot passed in."""
    from levain.autonomic.durable_sqlite import read_transaction
    from levain.coordinator import review
    from levain.coordinator.store import CoordStore, StoreAbsent

    store = CoordStore(qdir, clock=clock) if clock is not None else CoordStore(qdir)
    sign_open = review.queue_request_open(qdir, clock=clock, conn=queue_conn) if queue_conn is not None \
        else (lambda _rid: None)                  # no queue.db: the request's state cannot be read (unresolved)
    answer_open = review.question_open(qdir, conn=questions_conn) if questions_conn is not None \
        else (lambda _qid: None)

    def read(conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, str]]:
        w = review.waiting_on_you(store, sign_open=sign_open, answer_open=answer_open, conn=conn)
        # a row with no time (NULL ``at``) sorts first rather than failing the whole source
        at = {r[0]: ("" if r[1] is None else str(r[1]))
              for r in conn.execute("SELECT reply_id, at FROM replies WHERE kind = 'sign'")}
        return w, at

    if coord_conn is not None:
        return read(coord_conn)
    try:
        with read_transaction(lambda: store.open_readonly(busy_s=_wait(deadline, 30.0))) as conn:
            return read(conn)
    except StoreAbsent:
        return {"threads": {}, "loose": [], "unresolved": []}, {}


def _recs(w: dict[str, Any]) -> list[dict[str, Any]]:
    out = [r for rs in w["threads"].values() for r in rs] + list(w["loose"])
    return out


def _why(what: str, exc: BaseException) -> str:
    from levain.autonomic.durable_sqlite import _is_busy

    if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
        return f"{what}: busy past the band's time bound"[:120]
    return f"{what}: {type(exc).__name__}"[:120]


def read_extras(queue_dir: Path | str, clock: Any = None, *, deadline: float | None = None,
                coord_conn: sqlite3.Connection | None = None) -> Extras:
    """Read the three authority sources, each on its own: one that cannot be read lands in ``failed`` and the others
    are still listed. ``clock`` (a callable giving an aware datetime) stands in for now. Read-only.

    ``deadline`` (``time.monotonic()``, the hook's ONE bound): every store is opened once, as one snapshot, its lock
    wait capped to what is left; a store still held past it lands in ``failed`` ("! not read"), never a stalled
    prompt. ``coord_conn``: the coordinator snapshot a caller already holds (the morning card), read instead of a
    second one."""
    from levain.consent.questions import current_read_deadline

    qdir = Path(queue_dir)
    if deadline is None:
        deadline = current_read_deadline()        # the hook's one bound, when this read runs under it
    out = Extras()
    undecided: dict[str, dict[str, Any]] = {}
    consent_read = sign_read = False
    with contextlib.ExitStack() as stack:
        queue_conn: sqlite3.Connection | None = None
        try:
            queue_conn = stack.enter_context(_snapshot(qdir / "queue.db", deadline))
            for u in _consent(qdir, queue_conn):
                undecided[u["request_id"]] = u
            consent_read = True
        except Exception as exc:  # noqa: BLE001 - said in the band, never a crashed render
            queue_conn = None
            out.failed.append(_why("consent requests", exc))
        pointed: set[str] = set()
        try:
            questions_conn = stack.enter_context(_snapshot(qdir / "questions.db", deadline))
            w, at = _records(qdir, clock, deadline=deadline, coord_conn=coord_conn, queue_conn=queue_conn,
                             questions_conn=questions_conn)
            threads = {r["reply_id"]: t for t, rs in w["threads"].items() for r in rs}
            unresolved_ids = {str(r.get("reply_id")) for r in w["unresolved"]}
            for rec in sorted([*_recs(w), *w["unresolved"]],
                              key=lambda r: (at.get(r.get("reply_id")) or "", str(r.get("reply_id")))):
                try:
                    _one_record(rec, str(rec.get("reply_id")) in unresolved_ids, threads, at, undecided, qdir, out,
                                pointed, queue_conn)
                except Exception as exc:  # noqa: BLE001 - one record that cannot be read is listed, never the source lost
                    if rec.get("kind") == "answer":
                        continue                  # an escalation that cannot be read: its question is still listed
                    bad = _unreadable_sign(rec, at, f"its record could not be read ({type(exc).__name__})")
                    if bad.band_key[0] == "consent":
                        if bad.band_key[1] in pointed:
                            continue              # its request is already listed under the same key
                        pointed.add(bad.band_key[1])
                    out.items.append(bad)
            sign_read = True                      # an unreadable request state is listed (unresolved), never dropped
        except Exception as exc:  # noqa: BLE001
            out.failed.append(_why("coordinator SIGN records", exc))
    if sign_read:
        out.complete.update(("sign", "extend"))
    if consent_read and sign_read:
        out.complete.add("consent")               # a consent key is listed by either source: closed only when both read
    if consent_read:
        for rid, u in undecided.items():
            if rid in pointed:
                continue                          # a SIGN record lists this request, under the same key and number
            out.items.append(Extra(
                "consent", rid, str(u["undecided_at"] or ""), _line(u["agent"], 40),
                f"UNDECIDED: {_line(u['tool'], 60)}: {_line(u['what'])}", ("allow", "deny"), NO_REC, None,
                f"the {_line(u['agent'], 40)} session's gated call", f"consent request gap #{_line(u['gap'], 20)}",
                None, {"request_id": rid, "gap": u["gap"], "agent": u["agent"], "session": u["session"]}))
    out.items.sort(key=lambda e: (e.filed, e.ref))
    return out


def _sign_key(rec: Mapping[str, Any]) -> tuple[str, str]:
    """The band key of a coordinator SIGN record: its consent request (``("consent", request id)``, the key the
    request has as UNDECIDED too), or for a record that names no request ``("sign", reply id)``."""
    ref_id = rec.get("ref_id")
    if isinstance(ref_id, str) and ref_id:
        return ("consent", ref_id)
    return ("sign", str(rec.get("reply_id") or "?"))


def _unreadable_sign(rec: Mapping[str, Any], at: Mapping[str, str], why: str) -> Extra:
    from levain.coordinator import budget

    ref = str(rec.get("reply_id") or "?")
    extend = rec.get("ref_store") == budget.SIGN_REF
    return Extra("extend" if extend else "sign", ref, at.get(ref) or "", "coordinator",
                 f"UNREADABLE: SIGN {_line(ref, 80)} could not be read", (), NO_REC, None, "unknown",
                 f"coordinator record {_line(ref, 80)}", why, {}, None if extend else _sign_key(rec))


def _one_record(rec: dict[str, Any], unresolved: bool, threads: dict[str, str], at: dict[str, str],
                undecided: dict[str, dict[str, Any]], qdir: Path, out: Extras, pointed: set[str],
                queue_conn: sqlite3.Connection | None = None) -> None:
    """One coordinator record onto ``out``: an ANSWER rides its question, a SIGN or budget SIGN is an item."""
    from levain.coordinator import budget

    if rec["kind"] == "answer":
        if unresolved:
            return                                # a question that cannot be read: its own item says so
        out.escalations[rec["ref_id"]] = (_line(rec["text"]), _line(rec.get("rec") or NO_REC))
        return
    if rec.get("unreadable"):
        bad = _unreadable_sign(rec, at, str(rec["unreadable"]))
        if bad.band_key[0] == "consent":
            if bad.band_key[1] in pointed:
                return                            # its request is already listed under the same key
            pointed.add(bad.band_key[1])          # listed (unreadable) under its request's key, once
        out.items.append(bad)
        return
    extend = rec["ref_store"] == budget.SIGN_REF
    ref = rec["reply_id"]
    thread = threads.get(ref)
    key: tuple[str, str] | None = None
    if extend:
        options: tuple[str, ...] = ("sign the extension", "leave it unsigned")
        source, tag = "extend", "coordinator · budget"
        data: dict[str, Any] = {"thread_id": rec["ref_id"].partition("/")[0],
                                "envelope_id": rec["ref_id"].partition("/")[2]}
        head = "SIGN: extend a thread budget"
        blocks = f"thread {_line(data['thread_id'], 80)}'s new steps"
        origin = f"budget SIGN {_line(rec['ref_id'], 120)}"
    else:
        options = ("sign (allow)", "deny")
        source, tag = "sign", "coordinator"
        key = _sign_key(rec)
        if rec["ref_id"] in pointed:
            return                                # one item per request: a second SIGN on it adds no second item
        u = undecided.get(rec["ref_id"])
        data = {"request_id": rec["ref_id"], "thread_id": thread,
                "gap": u["gap"] if u else _gap_of(queue_conn, rec["ref_id"]),
                "agent": u["agent"] if u else None, "session": u["session"] if u else None}
        head = "SIGN"
        blocks = f"thread {_line(thread, 80)}" if thread else "the gated call"
        origin = (f"consent request {_line(rec['ref_id'], 120)}"
                  + (f" (gap #{_line(data['gap'], 20)})" if data["gap"] else "") + (", undecided" if u else ""))
        menu = sign_request_menu(qdir, rec["ref_id"]) if not unresolved else None
        if menu is not None:                      # a levain sign request: ITS options, ITS deny, by explicit key
            options = menu["labels"]
            data.update({k: menu[k] for k in ("menu", "version", "texts", "no_letters")})
            origin = f"{menu['origin']}; {origin}"
            link = _sign_link(qdir, rec["ref_id"], menu["version"])
            if link is not None:                  # the stored origin's deep link; none stored, none shown
                data["link"] = link
                origin = f"{origin}; link {link}"
    text = (f"{head}: {_line(rec['text'])}" if not unresolved
            else f"UNREADABLE: SIGN {_line(ref, 80)} could not be read")
    rec_text = _line(rec.get("rec") or NO_REC, 500)
    out.items.append(Extra(source, ref, at.get(ref) or "", tag, text, options, rec_text,
                           rec_letter_of(rec.get("rec"), len(options)), blocks, origin,
                           "its state could not be read" if unresolved else None, data, key))
    if not extend:
        pointed.add(rec["ref_id"])                # listed by this SIGN: its UNDECIDED request is not listed again


def sign_request_menu(qdir: Path | str, request_id: object) -> dict[str, Any] | None:
    """The band's menu for the levain sign request filed under ``request_id``, or ``None`` when there is no such
    request file. Built from the request's OWN options (S3 fix round 2, codex 2):

    - a choice request: one letter per option, its label the option's text, then one letter for its deny ("none of
      these"); each letter carries its action and the option KEY explicitly (``menu``), so a pick is mapped by key;
    - a plain request: exactly its ceremony's two actions, ``sign`` and ``deny``, never a deny that is a signature;
    - a request that cannot be signed (refused, expired, already decided) or whose options do not fit the band's six
      letters: NO letters (``no_letters`` says why); it is settled on the cockpit's sign sheet itself.

    ``version`` is the request file's SHA-256 the menu was read from: a pick on a file changed since opens nothing."""
    from datetime import datetime, timezone

    from levain.consent import sign

    if not isinstance(request_id, str):
        return None
    try:
        req = sign.read_request(Path(qdir), request_id)
    except sign.SignRefused:
        return None                               # no such file: not a sign request
    words = _request_words(request_id)
    origin = f"cockpit sign sheet, sign {words} (version {req.sha256[:16]})"
    base = {"version": req.sha256, "origin": origin, "texts": (), "menu": (), "labels": (), "no_letters": None}
    state = sign.state_of(Path(qdir), request_id)
    if req.refused is not None:
        return {**base, "no_letters": "the sign request cannot be signed (it is malformed): deny it on its sheet"}
    if state != "open":
        return {**base, "no_letters": f"the sign request is already {state}"}
    if req.expires_at is not None and req.expires_at <= datetime.now(timezone.utc):
        return {**base, "no_letters": "the sign request has expired: nothing can be signed"}
    if not req.options:
        return {**base, "menu": (("sign", None), ("deny", None)), "labels": ("sign it", "deny it (nothing is signed)")}
    if len(req.options) + 1 > len(_LETTERS):
        return {**base, "no_letters": f"its {len(req.options)} options and its deny do not fit the band's "
                                      f"{len(_LETTERS)} letters: choose on the cockpit's sign sheet"}
    menu = tuple(("choose", k) for k, _ in req.options) + (("deny", None),)
    labels = tuple(f"({k}) {_line(t, _OPTION_SHOWN)}" for k, t in req.options) + ("deny: none of these",)
    texts = tuple(t for _, t in req.options) + ("",)
    return {**base, "menu": menu, "labels": labels, "texts": texts}


def _sign_link(qdir: Path | str, request_id: object, version: object) -> str | None:
    """The https deep link to the cockpit's sign sheet for ``request_id``, built from the origin the cockpit stored
    (:mod:`levain.consent.cockpit_origin`, validated on read), or None when none is stored or the link cannot be built."""
    from levain.consent import sign
    from levain.consent.cockpit_origin import read_origin

    origin = read_origin(qdir)
    if origin is None:
        return None
    try:
        return sign.deep_link(origin, request_id, version=version)  # type: ignore[arg-type]
    except ValueError:
        return None


class UnsafeValue(ValueError):
    """A value a record carries cannot go into a command the operator copies: ``what`` names it (never its text)."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def _token(what: str, value: object) -> str:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise UnsafeValue(what)
    return value


def _gap_arg(gap: object) -> str:
    if isinstance(gap, bool) or not isinstance(gap, int) or gap < 1:
        raise UnsafeValue("gap number")
    return str(gap)


def _command(*argv: str) -> str:
    """One copyable command, every argument shell-quoted (each is a checked token or a fixed word already)."""
    return "`" + shlex.join(argv) + "`"


def consent_rule_command(gap: object, verb: str, agent: object, session: object) -> str:
    """``levain consent rule <gap> <verb> --agent <agent> --session <session> --signing-key KEY``, without backticks.
    ``verb`` is ``allow``, ``deny`` or the template ``allow|deny`` (the operator picks one). Every value from the
    record must be a plain token (:data:`TOKEN`), else :class:`UnsafeValue` and no command at all."""
    if verb not in ("allow", "deny", "allow|deny"):
        raise ValueError(f"verb {verb!r}")
    argv = ["levain", "consent", "rule", _gap_arg(gap), verb, "--agent", _token("agent", agent),
            "--session", _token("session id", session), "--signing-key", "KEY"]
    return " ".join(a if a == "allow|deny" else shlex.quote(a) for a in argv)


def _no_command(what: str, card: str) -> str:
    return (f"no command shown: the record's {what} is not a plain token ({_TOKEN_WORDS}), so Levain will not put it "
            f"into a line for your shell; {card}")


def _request_words(request_id: object) -> str:
    try:
        return f"request {_token('request id', request_id)}"
    except UnsafeValue:
        return "the request (its id is not a plain token, so it is not repeated here)"


class NotOffered(ValueError):
    """A pick that names no action the band displayed for the item (a letter past its options, a sign request whose
    options changed since): ``why`` says so, and nothing is opened."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _sheet_of_menu(qdir: Path, extra: Extra, index: int, letter: str) -> str:
    """The ceremony for displayed letter ``letter`` of a levain sign request: the action and option KEY that letter
    carried in the band (``extra.data['menu']``), checked against the request file as it is NOW (same version, the
    key still there with the same text, still open). Anything else is :class:`NotOffered`; nothing is opened."""
    from levain.consent import sign

    d = extra.data
    menu = d.get("menu") or ()
    if index >= len(menu):
        raise NotOffered(f"no option {letter}: the band showed {_shown(extra)}" if menu else
                         f"no option {letter}: {d.get('no_letters') or 'the band showed no letters for it'}")
    action, key = menu[index]
    request_id = d.get("request_id")
    try:
        req = sign.read_request(qdir, request_id) if isinstance(request_id, str) else None
    except sign.SignRefused:
        req = None
    if req is None or req.refused is not None or sign.state_of(qdir, request_id) != "open":
        raise NotOffered("its sign request is no longer open to sign or deny")
    if req.sha256 != d.get("version"):
        raise NotOffered("its sign request changed since the band read it; the options you saw are not its options")
    words = _request_words(request_id)
    where = f"the cockpit's sign sheet for sign {words} (version {req.sha256[:16]})"
    if action == "deny":
        return (f"open {where} and press Deny there: that records the denial"
                + (" (none of its options)" if req.options else "") + "; nothing is signed")
    if action == "sign":
        if req.options:
            raise NotOffered("its sign request now offers options; the band's sign/deny no longer fits it")
        return f"open {where} and sign it there with your passkey (it has no options)"
    texts = d.get("texts") or ()
    if (action != "choose" or not isinstance(key, str) or sign._OPTION_KEY.fullmatch(key) is None
            or dict(req.options).get(key) != (texts[index] if index < len(texts) else None)):
        raise NotOffered("the option you picked is not on the sign request as the band showed it")
    return (f"open {where} and choose option ({key}) {_line(texts[index], _OPTION_SHOWN)!r} there: it is signed only "
            "when you confirm it with your passkey")


def _shown(extra: Extra) -> str:
    return ", ".join(f"{_LETTERS[i]}) {lab}" for i, lab in enumerate(extra.options[:len(_LETTERS)])) or "no letters"


def sheet_text(extra: Extra, letter: str, qdir: Path | str) -> str:
    """The existing ceremony of ``extra`` for displayed letter ``letter``, as text. Nothing is recorded. A command is
    shown only when every value it carries is a plain token (module docstring); else words say why. A letter the band
    did not display for the item is :class:`NotOffered`: a pick never reaches an action the operator did not see."""
    qdir = Path(qdir)
    if letter not in _LETTERS:
        raise NotOffered(f"no option {letter!r}")
    index = _LETTERS.index(letter)
    d = extra.data
    if extra.source == "sign" and ("menu" in d or "no_letters" in d):
        return _sheet_of_menu(qdir, extra, index, letter)
    if index >= len(extra.options):
        raise NotOffered(f"no option {letter}: the band showed {_shown(extra)}")
    verb = "allow" if index == 0 else "deny"
    if extra.source == "consent":
        try:
            return f"`{consent_rule_command(d.get('gap'), verb, d.get('agent'), d.get('session'))}` (or its card)"
        except UnsafeValue as bad:
            return _no_command(bad.what, f"rule {_request_words(d.get('request_id'))} {verb} from its card")
    if extra.source == "extend":
        if index != 0:
            return "leaving it unsigned is the decline: nothing to sign"
        try:
            thread = _token("thread id", d.get("thread_id"))
        except UnsafeValue as bad:
            return _no_command(bad.what, "extend the thread's budget from its card")
        return (f"{_command('levain', 'envelope', 'extend', thread, '--budget', 'K=N,...', '-o', 'FILE')}, then "
                f"{_command('levain', 'envelope', 'sign', 'FILE')}")
    # a coordinator SIGN on a consent request (no sign request file under its id): the consent rule, allow or deny
    request_id = d.get("request_id")
    if extra.source == "sign" and sign_request_menu(qdir, request_id) is not None:
        raise NotOffered("it is a sign request whose own options the band did not show; ask for a fresh band")
    if d.get("gap"):
        try:
            return f"`{consent_rule_command(d.get('gap'), verb, d.get('agent') or 'AGENT', d.get('session') or 'SESSION')}`"
        except UnsafeValue as bad:
            return _no_command(bad.what, f"rule {_request_words(request_id)} {verb} from its card")
    return f"rule {_request_words(request_id)} {verb} from its own card; Levain found no gap number for it"
