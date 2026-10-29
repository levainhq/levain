"""levain.band_sources — the band's AUTHORITY sources (Q17-ALL S3, design §4 D12 and §5 "Authority: the trust boundary").

Beside the open questions the band lists, each as one numbered item: a consent request that is UNDECIDED, an open
coordinator SIGN record, and an open budget-extend SIGN. An ANSWER record (the coordinator's escalation of a question)
is never an item of its own: it rides its question's item (:attr:`Extras.escalations`). KNOW, RECEIPT, review items
and notes are not read here.

NOTHING HERE IS AUTHORITY (SIG-ONLY, D8). This module reads three stores read-only (``queue.db``, the coordinator's
``dispatch.db``) and builds display rows plus the TEXT of the existing signing ceremony each item is settled by
(:func:`sheet_text`). It records nothing and rules nothing: a decision on one of these items exists only as the
signature the existing path verifies (the phone ``question_rule`` sheet, ``levain consent rule``, a levain sign request
opened by :func:`levain.consent.sign.deep_link`, ``levain envelope sign``).

Rec (D11, Q2 ruled (a)): an item shows the coordinator's rec when its record carries one, else the literal text
:data:`NO_REC` (``rec: none (authority item)``) for a raw gate request.
Stdlib plus levain's own readers.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from levain.coordinator.review import NO_REC

__all__ = ["Extra", "Extras", "NO_REC", "read_extras", "rec_letter_of", "sheet_text", "SOURCES"]

SOURCES = ("consent", "sign", "extend")
_LETTERS = "abcdef"
_FIELD = 300
_REC_LETTER = re.compile(r"\s*([a-f])(?![A-Za-z0-9])")
_RO_TIMEOUT = 2.0


@dataclass(frozen=True)
class Extra:
    """One authority item as the band shows it. ``options`` are its labels in display order (a, b); ``rec`` the text
    after ``rec: `` and ``rec_letter`` the letter it names (``None`` when it names none). ``unreadable`` is set for a
    record that cannot be read (listed, never dropped, D13). ``data`` is what the ceremony text needs."""
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


@dataclass
class Extras:
    """What the authority sources hold now. ``items``: open ones, oldest first. ``complete``: the sources read in full
    (a numbered ref of such a source that is absent from ``items`` has closed; a source not read in full closes
    nothing). ``escalations``: question id -> ``(coordinator's text, rec)`` of an open ANSWER record. ``failed``: one
    line per source that could not be read (the band says so, never drops it silently)."""
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


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_RO_TIMEOUT)


def _line(text: Any, limit: int = _FIELD) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def _gap_of(qdir: Path, request_id: str) -> int | None:
    path = qdir / "queue.db"
    if not path.exists():
        return None
    conn = _ro(path)
    try:
        row = conn.execute("SELECT gap FROM gaps WHERE request_id = ?", (request_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else int(row[0])


def _consent(qdir: Path) -> list[dict[str, Any]]:
    from levain.coordinator import morning

    return morning._undecided(qdir)


def _records(qdir: Path, clock: Any = None) -> tuple[dict[str, Any], dict[str, str]]:
    """``(waiting_on_you's answer, reply_id -> filed time)`` of the coordinator's store; empty when there is none."""
    from levain.autonomic.durable_sqlite import read_transaction
    from levain.coordinator import review
    from levain.coordinator.store import CoordStore, StoreAbsent

    store = CoordStore(qdir, clock=clock) if clock is not None else CoordStore(qdir)
    try:
        with read_transaction(store.open_readonly) as conn:
            w = review.waiting_on_you(store, sign_open=review.queue_request_open(qdir, clock=clock),
                                      answer_open=review.question_open(qdir), conn=conn)
            at = {r[0]: r[1] for r in conn.execute("SELECT reply_id, at FROM replies WHERE kind = 'sign'")}
    except StoreAbsent:
        return {"threads": {}, "loose": [], "unresolved": []}, {}
    return w, at


def _recs(w: dict[str, Any]) -> list[dict[str, Any]]:
    out = [r for rs in w["threads"].values() for r in rs] + list(w["loose"])
    return out


def read_extras(queue_dir: Path | str, clock: Any = None) -> Extras:
    """Read the three authority sources, each on its own: one that cannot be read lands in ``failed`` and the others
    are still listed. ``clock`` (a callable giving an aware datetime) stands in for now. Read-only."""
    from levain.coordinator import budget

    qdir = Path(queue_dir)
    out = Extras()
    undecided: dict[str, dict[str, Any]] = {}
    try:
        for u in _consent(qdir):
            undecided[u["request_id"]] = u
        out.complete.add("consent")
    except Exception as exc:  # noqa: BLE001 - said in the band, never a crashed render
        out.failed.append(f"consent requests: {type(exc).__name__}"[:120])
    pointed: set[str] = set()
    try:
        w, at = _records(qdir, clock)
        threads = {r["reply_id"]: t for t, rs in w["threads"].items() for r in rs}
        for rec in sorted([*_recs(w), *w["unresolved"]], key=lambda r: (at.get(r["reply_id"], ""), r["reply_id"])):
            if rec["kind"] == "answer":
                if rec["reply_id"] in {r["reply_id"] for r in w["unresolved"]}:
                    continue                      # a question that cannot be read: its own item says so
                out.escalations[rec["ref_id"]] = (_line(rec["text"]), _line(rec.get("rec") or NO_REC))
                continue
            unresolved = rec in w["unresolved"]
            extend = rec["ref_store"] == budget.SIGN_REF
            ref = rec["reply_id"]
            thread = threads.get(ref)
            if extend:
                options = ("sign the extension", "leave it unsigned")
                source, tag = "extend", "coordinator · budget"
                data: dict[str, Any] = {"thread_id": rec["ref_id"].partition("/")[0],
                                        "envelope_id": rec["ref_id"].partition("/")[2]}
                head = "SIGN: extend a thread budget"
                blocks = f"thread {data['thread_id']}'s new steps"
                origin = f"budget SIGN {rec['ref_id']}"
            else:
                options = ("sign (allow)", "deny")
                source, tag = "sign", "coordinator"
                u = undecided.get(rec["ref_id"])
                data = {"request_id": rec["ref_id"], "thread_id": thread,
                        "gap": u["gap"] if u else _gap_of(qdir, rec["ref_id"]),
                        "agent": u["agent"] if u else None, "session": u["session"] if u else None}
                head = "SIGN"
                blocks = f"thread {thread}" if thread else "the gated call"
                origin = f"consent request {rec['ref_id']}" + (f" (gap #{data['gap']})" if data["gap"] else "")
                pointed.add(rec["ref_id"])
            text = f"{head}: {_line(rec['text'])}" if not unresolved else f"UNREADABLE: SIGN {ref} could not be read"
            rec_text = _line(rec.get("rec") or NO_REC, 500)
            out.items.append(Extra(source, ref, at.get(ref, ""), tag, text, options, rec_text,
                                   rec_letter_of(rec.get("rec"), len(options)), blocks, origin,
                                   "its state could not be read" if unresolved else None, data))
        out.complete.update(("sign", "extend"))
    except Exception as exc:  # noqa: BLE001
        out.failed.append(f"coordinator SIGN records: {type(exc).__name__}"[:120])
    if "consent" in out.complete:
        for rid, u in undecided.items():
            if rid in pointed:
                continue                          # a SIGN record already lists this request, with its rec
            out.items.append(Extra(
                "consent", rid, str(u["undecided_at"] or ""), f"{u['agent']}", f"UNDECIDED: {u['tool']}: {u['what']}",
                ("allow", "deny"), NO_REC, None, f"the {u['agent']} session's gated call",
                f"consent request gap #{u['gap']}", None,
                {"request_id": rid, "gap": u["gap"], "agent": u["agent"], "session": u["session"]}))
    out.items.sort(key=lambda e: (e.filed, e.ref))
    return out


def _origin(environ: Mapping[str, str]) -> str | None:
    from levain.consent.sign import cockpit_origin

    raw = environ.get("LEVAIN_COCKPIT_ORIGIN")
    if not raw:
        return None
    try:
        return cockpit_origin(raw)
    except ValueError:
        return None


def _sign_file_link(qdir: Path, request_id: str, index: int, environ: Mapping[str, str]) -> str | None:
    """The deep link into an OPEN levain sign request filed under ``request_id``, the pick preselected; ``None`` when
    there is no such request, it is not open, or it cannot be shown."""
    from levain.consent import sign

    try:
        req = sign.read_request(qdir, request_id)
    except sign.SignRefused:
        return None
    if req.refused or sign.state_of(qdir, request_id) != "open":
        return None
    choice = req.options[index][0] if req.options and index < len(req.options) else None
    origin = _origin(environ)
    if origin is None:
        return (f"open the cockpit's sign sheet for sign request {request_id} (version {req.sha256[:16]}"
                + (f", option {choice}" if choice else "") + ")")
    return sign.deep_link(origin, request_id, choice=choice, version=req.sha256)


def sheet_text(extra: Extra, letter: str, qdir: Path | str, environ: Mapping[str, str] | None = None) -> str:
    """The existing signing ceremony of ``extra`` with option ``letter`` preselected, as text. Nothing is recorded."""
    env = os.environ if environ is None else environ
    qdir = Path(qdir)
    index = _LETTERS.index(letter)
    d = extra.data
    if extra.source == "consent":
        verb = "allow" if index == 0 else "deny"
        return (f"`levain consent rule {d['gap']} {verb} --agent {d['agent']} --session {d['session']} "
                "--signing-key KEY` (or its card)")
    if extra.source == "extend":
        if index != 0:
            return "leaving it unsigned is the decline: nothing to sign"
        return (f"`levain envelope extend {d['thread_id']} --budget K=N,... -o FILE`, then `levain envelope sign FILE`")
    link = _sign_file_link(qdir, d["request_id"], index, env)
    if link is not None:
        return link
    verb = "allow" if index == 0 else "deny"
    if d.get("gap"):
        return (f"`levain consent rule {d['gap']} {verb} --agent {d.get('agent') or 'AGENT'} "
                f"--session {d.get('session') or 'SESSION'} --signing-key KEY`")
    return f"rule request {d['request_id']} {verb} from its own card; Levain found no gap number for it"
