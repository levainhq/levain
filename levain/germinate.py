"""``levain intentions germinate``: an agenda intention that has come due is put in front of the operator (TS-2).

Deterministic, no model call. For each DUE agenda (open, disposition ``agenda``, no ``germinated`` stamp, and ``next``
due: a date-only ``next`` is due on or before the install's local day; a TIMED ``next`` (``YYYY-MM-DDTHH:MM``, local)
is due from that minute; an agenda with no ``next`` is never due) it does, in order:

1. adopt an open question whose ``q-…`` id the intention's text or notes name (file nothing; ref = that id);
2. ``band``: file ONE band row through ``QuestionStore.file_once``, no ping;
3. ``nudge``: the same filing, then one ping. The ping is made whenever the row is still open and the intention is not yet
   stamped, so a ping that failed (returned False or raised) leaves the intention open and due and the next run retries it
   (``ping_failed`` in the result, non-zero exit); only a ping that went out is followed by the stamp. A crash between
   the ping and the stamp therefore pings again on the re-run (at-least-once: a lost nudge is the failure this exists
   to prevent), still into the ONE row;
4. ``board:<repo>`` / ``desk``: a desk drop (``outbox_id`` derived from the intention, words always checked against
   the intention as it is now), or, with no desk store that can take a drop (``desk_present``), the band
   filing with the reason in ``blocks``. ``desk`` files the intention's words verbatim and uncut (an intention too long for a
   drop is an error for that intention, never a truncation);
5. stamp ``germinated={on, target, ref}`` as a compare-and-set against the intention as it was read (its version and ``agenda``
   disposition), so an intention edited while this ran is left open, reported under ``skipped``, and filed again from its new
   words on the next run. An agenda that filed or adopted a QUESTION (``adopted``, ``band``, ``nudge``, ``band-fallback``)
   is then LEFT OPEN while that question is open: its stamp names the row, the row rings in the band, and the operator's
   controls on that intention move the intention and the row together; once the question is ANSWERED the next run closes the
   agenda (``done`` / ``answered`` / ``explored`` by its type). An agenda that handed a desk drop (``board``,
   ``desk``) is composted, as before, with a second compare-and-set.

An agenda that waits on an EVENT (``on_event: done:<id>``, ``check_by``: the second part of a split, ``levain.split``) is never
due by a date. It wakes, through the same filing and stamp as a due one, when the intention it names has been finished (resolved
downward with a completion kind), read from that intention's own resolution on every run, so a writer that hurried the wake and
died leaves nothing to repair. When its ``check_by`` day comes first it rings as one unstamped band row ("still waiting on
...") whose answer keeps or drops it (``checkby`` in the result). ``event=<key>`` limits a run to the parts waiting on that
exact key: nothing dated, no check-by pass.

One bad intention never stops the others: its error is recorded under ``errors`` and the run goes on; the CLI prints every
outcome that did complete and exits non-zero when any intention errored. A missing ``.levain/memory.spores.json`` is
refused with a reason (exit 2), never reported as "nothing due". A run holds a per-install lock (``--dry-run`` does
not), so two overlapping runs (cron plus a hand run, the 06:10 run plus a minute ``--timed`` run) take turns.

The dedupe key is the intention's immutable ``origin_key``, never its id: the question store is per USER while ``intention-NNN``
ids are per INSTALL, so an id key would let install B's due ``intention-004`` adopt install A's held row and compost with no
row of its own. Filing is idempotent (``file_once`` / the outbox id), so a crash between any two steps re-runs into the
same one row; a drop intention stamped but not yet composted is finished on the next run (a stamp naming a question is finished
only once that question is answered).

Known edges (closed under the reachability line, TS-2 L3): ``board:<repo>`` does not carry the repo into the drop: a
drop's ``Source`` takes only an absolute repo path, a ``refs/heads/`` ref and a full commit id, none of which a repo
NAME determines, and the words stay his verbatim; the repo is recorded in the intention's ``germinated`` stamp (target)
only. A drop has no field for the intention's ``created`` or ``next`` either (``sent_at`` is the sender's clock stamp, not an
authorship date, and a bare date is not a moment), so neither is carried. A board/desk intention edited AFTER its drop was
filed raises ``DropConflict`` on every run (his new words are never silently dropped, and the old drop is never
re-labelled), until he resolves it by hand. A band row already filed keeps its first ``what`` text if the intention is
edited before the stamp. A nudge whose ping can never succeed (no channel) stays open and errors each run; the band row
is still there. Two due intentions naming the same open ``q-…`` both adopt it. ``--dry-run`` reads the intention store through
``levain.intentions.list_open_readonly``, which does not backfill a missing ``origin_key`` (a keyless row gets a temporary
in-memory key). The default ``--agent`` is
``claude_code`` (a Codex install passes ``--agent codex``). An empty-text intention cannot exist (anneal refuses it). The
band row's ``Due MM-DD`` carries no year.

Also a known edge: the per-install run lock uses ``fcntl`` (macOS/Linux only; levain does not target Windows).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from levain.intentions import (DEFAULT_CHECK_BY_DAYS, _parse_when, event_anchor, finished_by_completion, is_event_agenda,
                               is_timed, norm_event, parse_target, valid_check_by)
from levain.intention_legacy import STORE_FILE_NAME

__all__ = ["GerminateRefused", "KEY_PREFIX", "Outcome", "add_parsers", "germinate", "is_due", "outbox_id_for",
           "run_germinate"]

KEY_PREFIX = "levain-germinate:"
TARGET_BAND, TARGET_NUDGE, TARGET_BOARD, TARGET_DESK = "band", "nudge", "board", "desk"
_QID = re.compile(r"\bq-[0-9a-f]{24}\b")
_TITLE_MAX = 160


class GerminateRefused(RuntimeError):
    """The run cannot start (no intentions file): said with a reason, never reported as 'nothing due'."""


@dataclass(frozen=True)
class Outcome:
    intention_id: str
    origin_key: str
    target: str          # band | nudge | desk | board:<repo> as the intention asked
    via: str             # what actually happened: adopted | band | nudge | board | desk | band-fallback | finished
    ref: str             # the q-… id or the drop id
    filed: bool          # this run created the row or drop
    pinged: bool
    done: bool           # stamped and composted (False on a dry run, and False for a repeating agenda that advanced)
    event: str = ""      # the event key that woke it (a split's second part), "" for a dated wake
    advanced: str = ""   # a repeating agenda's next occurrence (it stays open; via "advanced"), "" otherwise

    def as_dict(self) -> dict[str, Any]:
        out = {"intention_id": self.intention_id, "origin_key": self.origin_key, "target": self.target, "via": self.via,
               "ref": self.ref, "filed": self.filed, "pinged": self.pinged, "done": self.done}
        if self.event:
            out["event"] = self.event
        if self.advanced:
            out["advanced"] = self.advanced
        return out


def _row_key(item: dict[str, Any], okey: str) -> str:
    """The band row's idempotency key. A repeating agenda's row is keyed by its occurrence (``next``) too: an ANSWERED row
    still holds its key, so one key for every occurrence meant the second occurrence found the first's answered row and filed
    nothing. A retry of one occurrence keeps the same ``next`` and so reuses its row."""
    from levain.recur import rule_of
    return KEY_PREFIX + okey + (f":{item.get('next')}" if rule_of(item) else "")


def outbox_id_for(origin_key: str) -> str:
    """The desk outbox id for an intention: the store's grammar is ``[A-Za-z0-9._-]`` (no colon, so the frame's
    ``germinate:<origin_key>`` cannot be one), so it is a hash of the origin key, still one id per origin key."""
    return "germinate-" + hashlib.sha256(origin_key.encode("utf-8")).hexdigest()[:40]


def _day(value: Any) -> date | None:
    """anneal's lenient read: a ``YYYY-MM-DD`` prefix, else None (display only; the DUE test is :func:`is_due`)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_agenda(item: dict[str, Any]) -> bool:
    d = item.get("disposition")
    return isinstance(d, str) and d.strip().casefold() == "agenda"


def is_due(item: dict[str, Any], today: date, now: datetime | None = None) -> bool:
    """SD-4: agenda, open (the caller lists open intentions), no ``germinated``, and ``next`` due: a TIMED ``next`` from that
    minute (a malformed timed value is never due and never read as a date), a date-only ``next`` on or before today."""
    if not _is_agenda(item) or item.get("germinated") or item.get("on_event"):
        return False       # an agenda that waits on an event wakes on it (the split), never on a date
    raw = item.get("next")
    if is_timed(raw):
        when = _parse_when(raw)
        return when is not None and (now or datetime.now()) >= when
    nxt = _day(raw)
    return nxt is not None and nxt <= today


def _target_of(item: dict[str, Any]) -> tuple[str, str | None]:
    """``(target, repo)``: ``band`` (default and the fallback for an unknown value), ``nudge``, ``desk``,
    ``board:<repo>``. The keyword is matched without regard to case; the repo keeps its own. Read through
    :func:`levain.intentions.parse_target`, the one reading ``executor_of`` shares, so "who acts" never disagrees with what a wake does."""
    parsed = parse_target(item.get("target"))
    if parsed is None or parsed[0] == TARGET_BAND:
        return TARGET_BAND, None
    return parsed


def _line(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _title(item: dict[str, Any], sid: str) -> str:
    """The first non-empty line of the intention's text, else its id."""
    lines = [ln for ln in (item.get("text") or "").splitlines() if ln.strip()]
    return _line(lines[0], _TITLE_MAX) if lines else sid


def _adopt(item: dict[str, Any], open_ids: set[str]) -> str | None:
    seen = [item.get("text") or "", *[n for n in (item.get("notes") or []) if isinstance(n, str)]]
    for chunk in seen:
        for qid in _QID.findall(chunk):
            if qid in open_ids:
                return qid
    return None


@contextmanager
def _run_lock(intentions_json: Path, dry_run: bool) -> Iterator[None]:
    """One germinate run at a time per install (blocking); a dry run reads only and takes none. No ``fcntl`` (Windows):
    no lock, as anneal's own guarded writes then refuse a stale one."""
    if dry_run:
        yield
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        yield
        return
    path = intentions_json.with_name(intentions_json.name + ".germinate.lock")
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def germinate(intentions_json: Path | str, queue_dir: Path | str, *, install: Path | str, agent: str = "claude_code",
              today: date | None = None, now: datetime | None = None, dry_run: bool = False, timed_only: bool = False,
              ping: Callable[..., bool] | None = None, event: str | None = None) -> dict[str, Any]:
    """Germinate every due agenda in ``intentions_json`` (``timed_only``: only those whose ``next`` carries a time).
    Returns ``{"today", "due": [...], "finish": [...], "errors": [...], "skipped": [...]}``; ``due`` and ``finish``
    hold :class:`Outcome` dicts; ``checkby`` (present only when an event agenda reached its check-by) holds what the
    check-by did to each. ``event`` limits the run to the agendas waiting on that exact key (a wake after the event,
    nothing dated, no check-by pass); the key is verified against the store, never taken on the caller's word. ``dry_run`` writes nothing (it files nothing, pings nothing, and creates no store).
    ``now`` is the local naive clock for a timed ``next`` (default: the host's). ``ping`` is the notify seam (default
    :func:`levain.consent.band._send_ping`, read at call time). Raises :class:`GerminateRefused` with no intentions file."""
    intentions_json = Path(intentions_json)
    if not intentions_json.is_file():
        raise GerminateRefused(f"{intentions_json} does not exist: this is not a Levain install with an intention store "
                               "(check --path); nothing was read or written")
    with _run_lock(intentions_json, dry_run):
        return _germinate(intentions_json, Path(queue_dir), Path(install), agent, today, now, dry_run, timed_only, ping, event)


def _germinate(intentions_json: Path, qdir: Path, install: Path, agent: str, today: date | None, now: datetime | None,
               dry_run: bool, timed_only: bool, ping: Callable[..., bool] | None,
               event: str | None = None) -> dict[str, Any]:
    from anneal_memory.spores import IntentionError, intention_version

    from levain.consent import band
    from levain.consent.questions import ANSWER_CHANNELS, DB_NAME, QuestionStore, later_date
    from levain.desk.store import CoordStore
    from levain.intentions import answered_descend_kind, guarded_intention_store, ringing_ref
    from levain.recur import advance_changes, repeats_words

    now = now or datetime.now()
    today = today or now.date()
    result: dict[str, Any] = {"today": today.isoformat(), "due": [], "finish": [], "errors": [], "skipped": []}
    store = guarded_intention_store(intentions_json)
    if dry_run:     # a dry run writes nothing: no origin_key backfill, no lock file (a keyless row gets a temporary key)
        from levain.intentions import list_open_readonly
        open_items = list_open_readonly(intentions_json, today=today)
    else:
        open_items = store.list_open(today=today)
    due = [s for s in open_items if is_due(s, today, now) and (not timed_only or is_timed(s.get("next")))]
    event_agendas = [s for s in open_items if is_event_agenda(s)]
    # a dry run on a machine with no band yet must not create one
    questions = None if dry_run and not (qdir / DB_NAME).exists() else QuestionStore(qdir)
    open_qs = questions.open_questions() if questions is not None else []
    open_ids = {q.id for q in open_qs}
    # a row the operator deferred is open but not due: a due agenda never adopts it (the band's own due predicate)
    adoptable = {q.id for q in open_qs if later_date(q, today.isoformat()) is None}

    def _stamp_ref(s: dict[str, Any]) -> str:
        st = s.get("germinated")
        ref = st.get("ref") if isinstance(st, dict) else None
        return ref if isinstance(ref, str) else ""

    def _answered(ref: str) -> bool:
        """The stamped question exists and carries an answer (a missing or unreadable one is not an answer)."""
        try:
            q = questions.get(ref) if questions is not None and ref else None
        except Exception:  # noqa: BLE001
            return False
        return q is not None and q.answer is not None

    # a stamped agenda is finished when its stamp names a desk drop (handed off, not a live row) or a question that has
    # been ANSWERED (the intention is done); one whose question still rings stays open, and one whose question is missing waits
    finish = [s for s in open_items if _is_agenda(s) and s.get("germinated")
              and (_stamp_ref(s).startswith("dr-") or not _stamp_ref(s)
                   or (ringing_ref(s, open_ids) is None and _answered(_stamp_ref(s))))]
    if event is not None:    # an event-only run wakes that event's agendas and nothing else
        due, finish = [], []
    coord = CoordStore(qdir)
    ping = ping or band._send_ping
    has_board = coord.desk_present()
    stamp_for = {"on": today.isoformat()}

    def _err(sid: Any, okey: Any, exc: BaseException | str, **extra: Any) -> None:
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        result["errors"].append({"intention_id": sid, "origin_key": okey, "error": text, **extra})

    for item in finish:
        sid, okey = item.get("id"), item.get("origin_key")
        try:
            stamp = item["germinated"] if isinstance(item["germinated"], dict) else {}
            out = Outcome(str(sid), str(okey or ""), str(stamp.get("target") or item.get("target") or ""),
                          "finished", str(stamp.get("ref") or ""), False, False, False)
            handed_off = str(stamp.get("ref") or "").startswith("dr-") or not stamp.get("ref")
            advance = None if handed_off else advance_changes(item, now)
            if advance is not None:      # it stays open: say so (a dry run says it too), never "closed"
                out = Outcome(**{**out.__dict__, "via": "advanced", "advanced": advance["next"]})
            if not dry_run:
                try:
                    # an answered question closes the intention as done by type; a handed-off drop composts, as before.
                    # A REPEATING agenda whose question was answered advances to its next occurrence instead (levain.recur)
                    if advance is not None:
                        store.update(sid, expect_disposition="agenda", expected_version=intention_version(item),
                                     today=today, **advance)
                    else:
                        kind = "composted" if handed_off else answered_descend_kind(item.get("type"))
                        store.descend(sid, kind=kind, expect_disposition="agenda",
                                      expected_version=intention_version(item), today=today)
                except IntentionError as exc:
                    if "changed since read" not in str(exc) and "disposition changed" not in str(exc):
                        raise
                    result["skipped"].append({"intention_id": sid, "origin_key": okey, "ref": str(stamp.get("ref") or ""),
                                              "reason": f"changed while finishing ({exc}); left open"})
                    continue
                if advance is None:
                    out = Outcome(**{**out.__dict__, "done": True})
            result["finish"].append(out.as_dict())
        except Exception as exc:  # noqa: BLE001 - one bad intention never stops the others
            _err(sid, okey, exc)

    # A split's second part wakes when the part it waits on was FINISHED. Read after the finish pass above, so an anchor
    # that pass just closed as done wakes its follower in the same run; and read from the store (the anchor's own
    # resolution), so the wake never depends on a crashed or skipped emitter having run.
    woke_by: dict[str, str] = {}
    woken: list[dict[str, Any]] = []
    for item in event_agendas:
        anchor = event_anchor(item)
        if anchor is None or (event is not None and norm_event(item.get("on_event")) != norm_event(event)):
            continue
        try:
            finished = finished_by_completion(store.get(anchor))
        except Exception as exc:  # noqa: BLE001 - one unreadable anchor never stops the others
            _err(item.get("id"), item.get("origin_key"), exc)
            continue
        if finished:
            woke_by[str(item.get("id"))] = anchor
            woken.append(item)

    for item in [*due, *woken]:
        sid, okey = item.get("id"), item.get("origin_key")
        anchor_id = woke_by.get(str(sid), "")
        try:
            if not isinstance(sid, str) or not isinstance(okey, str) or not okey:
                raise ValueError("the intention has no id or no origin_key; it cannot be keyed, so it is skipped")
            if anchor_id and not dry_run:
                # BEFORE anything is committed: the stamp below is the wake's one commit point. A ring that cannot be closed
                # raises here, the part is left exactly as it was (the link intact) and the next run wakes it; closing is
                # idempotent (an already-closed ring is not open), so that retry never closes or files twice
                _close_stale_rings(questions, agent, okey, sid, f"done:{anchor_id}", result)
            target, repo = _target_of(item)
            nxt = _day(item.get("next"))
            title = _title(item, sid)
            adopted = _adopt(item, adoptable)
            filed = pinged = False
            ping_failed = ""
            drop_target = target == TARGET_DESK or target.startswith(TARGET_BOARD + ":")
            if adopted is not None:
                via, ref = "adopted", adopted
            elif drop_target and has_board:
                via = TARGET_DESK if target == TARGET_DESK else "board"
                outbox = outbox_id_for(okey)
                if dry_run:
                    ref = ""
                else:
                    # always sent with the intention's CURRENT words: a stored drop under this id holding other words raises
                    # DropConflict (his new words are never lost to an old drop's ref)
                    text = item.get("text") or ""
                    words = text  # verbatim and uncut: too long for a drop is an error for this intention, never a truncation
                    drop, filed = coord.accept_drop(words, surface="cli", outbox_id=outbox)
                    ref = drop.drop_id
            else:
                via = target if target in (TARGET_BAND, TARGET_NUDGE) else "band-fallback"
                ref = ""
                if not dry_run:
                    blocks = (f"agenda {sid} in {install.name} woke because {anchor_id} was done; it waits on you"
                              if anchor_id else
                              f"agenda {sid} in {install.name} came due "
                              f"{nxt.isoformat() if nxt else today.isoformat()}; it waits on you")
                    if repo:
                        blocks += f" (board {repo}: no board here, so it is on the band)"
                    elif target == TARGET_DESK:
                        blocks += " (desk: no desk here, so it is on the band)"
                    q, filed = questions.file_once(
                        agent=agent, key=_row_key(item, okey),
                        what=(f"After {anchor_id} was done: {title} ({sid})" if anchor_id
                              else f"Due {(nxt or today).strftime('%m-%d')}: {title} ({sid})" + repeats_words(item)),
                        options=[("done", "Done")], rec="done", blocks=_line(blocks, 600), cwd=str(install.resolve()),
                        held_by=ANSWER_CHANNELS)
                    ref = q.id
                    if via == TARGET_NUDGE and q.answer is None:
                        try:
                            pinged = bool(ping(agent, q, qdir, kind="attention"))
                            if not pinged:
                                ping_failed = "the ping was not sent"
                        except Exception as exc:  # noqa: BLE001 - a ping is a ping; it must not be lost silently
                            ping_failed = f"the ping failed: {type(exc).__name__}: {exc}"
            if ping_failed:
                # the row is filed; the intention stays open and due so the next run pings again
                _err(sid, okey, f"ping_failed: {ping_failed}; the intention is left open and the next run retries",
                     ping_failed=True, ref=ref, target=target)
                continue
            done = False
            if not dry_run:
                try:
                    stamp = {**stamp_for, "target": target, "ref": ref}
                    if anchor_id:     # the wake consumes the link: the part is an ordinary agenda from here (a snooze holds)
                        stamp["event"] = str(item.get("on_event"))
                    updated = store.update(sid, germinated=stamp, **({"on_event": None} if anchor_id else {}),
                                           expect_disposition="agenda", expected_version=intention_version(item),
                                           today=today)
                    if via not in ("adopted", "band", "nudge", "band-fallback"):
                        store.descend(sid, kind="composted", expect_disposition="agenda",
                                      expected_version=intention_version(updated), today=today)
                    done = True
                except IntentionError as exc:
                    if "changed since read" not in str(exc) and "disposition changed" not in str(exc):
                        raise
                    result["skipped"].append({"intention_id": sid, "origin_key": okey, "ref": ref,
                                              "reason": f"changed while germinating ({exc}); left open"})
                    continue
            result["due"].append(Outcome(sid, okey, target, via, ref, filed, pinged, done,
                                         f"done:{anchor_id}" if anchor_id else "").as_dict())
        except Exception as exc:  # noqa: BLE001 - one bad intention never stops the others
            _err(sid, okey, exc)
    if event is None:
        _check_by_pass(event_agendas, woke_by, store, questions, today, dry_run, agent, install, result, _err)
    return result


def _close_stale_rings(questions: Any, agent: str, okey: str, sid: str, event: str, result: dict[str, Any]) -> None:
    """A check-by ring filed before the event fired is the machine's own row and is now answered by the event: close it (a text
    answer on the cli channel) and say so in the result, so it is never left open beside the wake's row. Runs BEFORE the wake
    commits (the stamp); a failure raises, so the part is left unwoken with its link and the next run retries."""
    try:
        closed = questions.close_open(f"{KEY_PREFIX}checkby:{okey}", text=f"{event} happened; this question is closed",
                                      channel="cli")
    except Exception as exc:  # noqa: BLE001 - re-raised with what it means for the wake
        raise RuntimeError(f"the check-by question could not be closed, so {sid} is not woken yet and the next run retries: "
                           f"{exc}") from exc
    for q in closed:
        result.setdefault("checkby", []).append({"intention_id": sid, "origin_key": okey, "event": event, "check_by": "",
                                                 "via": "superseded", "ref": q.id})


def _check_by_day(item: dict[str, Any]) -> date | None:
    """The day an event agenda must be asked about: its ``check_by``, or (a hand-made row with none, or a bad one) seven days
    after it was created, so no event agenda can wait without end."""
    if valid_check_by(item.get("check_by")):
        return datetime.strptime(item["check_by"], "%Y-%m-%d").date()
    made = _day(item.get("created"))
    return None if made is None else made + timedelta(days=DEFAULT_CHECK_BY_DAYS)


def _check_by_pass(event_agendas: list[dict[str, Any]], woke_by: dict[str, str], store: Any, questions: Any, today: date,
                   dry_run: bool, agent: str, install: Path, result: dict[str, Any],
                   _err: Callable[..., None]) -> None:
    """An event agenda whose event has not fired by its check-by rings in the band as one row, "still waiting on <event>: still
    want this?", unstamped (the event link stays live: the event may still wake it). The row's key names the check-by day, so
    an answer is honoured exactly once and a moved check-by is asked again on its new day: ``keep`` moves the check-by seven
    days on, ``drop`` (the option, or that word typed) ends the agenda, any other answer is a keep. A re-run after a crash between the answer and its effect
    applies the effect again, and it is the same effect."""
    from anneal_memory.spores import IntentionError, intention_version

    from levain.consent.questions import ANSWER_CHANNELS
    from levain.intentions import dropped_descend_kind

    for item in event_agendas:
        sid, okey = item.get("id"), item.get("origin_key")
        if str(sid) in woke_by:
            continue
        try:
            day = _check_by_day(item)
            if day is None or day > today or not isinstance(sid, str) or not isinstance(okey, str) or not okey:
                continue
            on_event = str(item.get("on_event"))
            entry = {"intention_id": sid, "origin_key": okey, "event": on_event, "check_by": day.isoformat(),
                     "via": "ringing", "ref": ""}
            if dry_run or questions is None:
                result.setdefault("checkby", []).append(entry)
                continue
            q, _filed = questions.file_once(
                agent=agent, key=f"{KEY_PREFIX}checkby:{okey}:{day.isoformat()}",
                what=_line(f"Still waiting on {on_event}: {_title(item, sid)} ({sid}). Still want this?", 900),
                options=[("keep", "Still want it"), ("drop", "Drop it")], rec="keep",
                blocks=_line(f"agenda {sid} in {install.name} waits on {on_event}; its check-by {day.isoformat()} has come", 600),
                cwd=str(install.resolve()), held_by=ANSWER_CHANNELS)
            entry["ref"] = q.id
            if q.answer is not None:
                try:
                    if q.answer.value.strip().casefold() == "drop":      # the option, or the word typed
                        store.descend(sid, kind=dropped_descend_kind(item.get("type")), expect_disposition="agenda",
                                      expected_version=intention_version(item), today=today)
                        entry["via"] = "dropped"
                    else:
                        store.update(sid, check_by=(today + timedelta(days=DEFAULT_CHECK_BY_DAYS)).isoformat(),
                                     expect_disposition="agenda", expected_version=intention_version(item), today=today)
                        entry["via"] = "kept"
                except IntentionError as exc:
                    if "changed since read" not in str(exc) and "disposition changed" not in str(exc):
                        raise
                    result["skipped"].append({"intention_id": sid, "origin_key": okey, "ref": q.id,
                                              "reason": f"changed while its check-by answer was applied ({exc}); left open"})
                    continue
            result.setdefault("checkby", []).append(entry)
        except Exception as exc:  # noqa: BLE001 - one bad intention never stops the others
            _err(sid, okey, exc)


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    from levain.consent.queue import default_queue_dir

    install = Path(args.path).expanduser().resolve()
    intentions = install / ".levain" / STORE_FILE_NAME
    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    return install, intentions, qdir


def run_germinate(args: argparse.Namespace, out: Any = None) -> int:
    out = out if out is not None else sys.stdout
    try:
        install, intentions, qdir = _paths(args)
        res = germinate(intentions, qdir, install=install, agent=args.agent, dry_run=args.dry_run,
                        timed_only=getattr(args, "timed", False))
    except GerminateRefused as exc:
        print(f"levain: intentions germinate refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a CLI verb says why and exits non-zero
        print(f"levain: intentions germinate failed: {exc}", file=sys.stderr)
        return 1
    rc = 1 if res["errors"] else 0
    for e in res["errors"]:
        print(f"levain: intentions germinate: {e['intention_id']}: {e['error']}", file=sys.stderr)
    if args.as_json:
        print(json.dumps(res, indent=2, sort_keys=True), file=out)
        return rc
    verb = "would germinate" if args.dry_run else "germinated"
    for o in res["due"]:
        live = " (stamped, live)" if o["done"] and o["via"] in ("adopted", "band", "nudge", "band-fallback") else ""
        print(f"{verb} {o['intention_id']} ({o['target']}) via {o['via']}" + (f" -> {o['ref']}" if o["ref"] else "") + live, file=out)
    for o in res["finish"]:
        if o.get("advanced"):
            print(f"{'would advance' if args.dry_run else 'advanced'} {o['intention_id']} to its next occurrence {o['advanced']} (stays open)", file=out)
        else:
            print(f"{'would finish' if args.dry_run else 'finished'} {o['intention_id']} (stamped, now closed)", file=out)
    for c in res.get("checkby", []):
        print(f"{c['intention_id']} woke on {c['event']}; its check-by question {c['ref']} was closed" if c["via"] == "superseded"
              else f"{c['intention_id']} still waits on {c['event']}; its check-by {c['check_by']} has come ({c['via']})", file=out)
    for s in res["skipped"]:
        print(f"skipped {s['intention_id']}: {s['reason']}", file=out)
    if not res["due"] and not res["finish"] and not res["errors"] and not res["skipped"] and not res.get("checkby"):
        print("no agenda is due", file=out)
    return rc


def intentions_verbs(subparsers: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """The one ``levain intentions`` parser's verb table, created on first use. Germinate and keep-migrate both live
    under ``intentions``; whichever registers first makes the parser, the other adds its verb to it."""
    p = subparsers.choices.get("intentions")
    if p is None:
        p = subparsers.add_parser("intentions", help="Intention verbs for an install (germinate, keep-migrate).",
                                  description="Verbs for the install's intention store.", allow_abbrev=False)
        p.set_defaults(func=lambda a: (p.print_help(), 2)[1])
        p._levain_verbs = p.add_subparsers(dest="intentions_cmd", metavar="<verb>", required=True)
    return p._levain_verbs


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    sub = intentions_verbs(subparsers)
    g = sub.add_parser("germinate", allow_abbrev=False,
                       help="Put every due agenda in front of the operator (band row, nudge, desk or board drop).",
                       description="Each open agenda intention whose date (or, for a timed next, minute) has come (and not "
                                   "yet germinated) is filed once as the intention's target says, stamped, and composted. "
                                   "Deterministic; no model call. Safe to run again: a re-run files nothing twice.")
    g.add_argument("--path", type=Path, default=Path.cwd(), help="Install directory (default: cwd).")
    g.add_argument("--queue-dir", type=Path, default=None, help="The band / desk directory (default: the "
                                                                "levain home's consent dir).")
    g.add_argument("--agent", choices=("claude_code", "codex"), default="claude_code",
                   help="The band's name for this install's harness.")
    g.add_argument("--dry-run", action="store_true", help="List the due set and write nothing.")
    g.add_argument("--timed", action="store_true",
                   help="File only agendas whose next carries a time (YYYY-MM-DDTHH:MM) and is now due, any target; "
                        "the same lock and stamp as a plain run, so the two are idempotent together.")
    g.add_argument("--json", action="store_true", dest="as_json", help="Print the result as JSON.")
    g.set_defaults(func=run_germinate)
